'''
Author: Shuailin Chen
Created Date: 2021-08-28
Last Modified: 2021-08-29
	content: 
'''
from copy import deepcopy
import torch
from torch import Tensor
import torch.nn.functional as F
import numpy as np

from mmseg.ops import resize
from mmseg.core import add_prefix

from ..builder import SEGMENTORS
from .semi_v2 import SemiV2


@SEGMENTORS.register_module()
class ReCo(SemiV2):
    ''' my implementation of regional contrast algorithm for semi-supervised semantic segmentation
    '''

    def __init__(self, momentum, strong_thres, weak_thres, tmperature, num_queries, num_negatives, **kargs):
        super().__init__(**kargs)
        self.momentum = momentum
        self.strong_thres = strong_thres
        self.weak_thres = weak_thres
        self.tmperature = tmperature
        self.num_queries = num_queries
        self.num_negatives = num_negatives

    def init_weights(self):
        super().init_weights()

        # EMA model
        self.backbone_ema = deepcopy(self.backbone)
        self.decode_head_ema = deepcopy(self.decode_head)
        if self.with_neck:
            self.neck_ema = deepcopy(self.neck)

    @staticmethod
    def _ema_update(ema, model, decay):
        for ema_param, param in zip(ema.parameters(), model.parameters()):
            ema_param.data = decay * ema_param.data + (1 - decay) * param.data

    def ema_update_whole(self):
        ''' Update the EMA (mean teacher) model '''
        decay = min(1 - 1 / (self.step + 1), self.momentum)
        self.step += 1

        ReCo._ema_update(self.backbone_ema, self.backbone, decay)
        ReCo._ema_update(self.decode_head_ema, self.decode_head, decay)
        if self.with_neck:
            ReCo._ema_update(self.neck_ema, self.neck, decay)

    def ema_forward(self, img):
        ''' Forward function of EMA model '''
        x = self.backbone_ema(img)
        if self.with_neck:
            x = self.neck_ema(x)
        preds, _ = self.decode_head_ema.forward(img)
        return preds

    def main_forward(self, img):
        ''' Forward function of the main model 
        
        Returns:
            preds (Tensor): prediction logits
            reps (Tensor): representation tensor, for purpose of regional contrast
        '''
        x = self.backbone(img)
        if self.with_neck:
            x = self.neck(x)
        preds, reps = self.decode_head.forward(img)
        return preds, reps

    def compute_unsupervised_loss(self, predict, target, logits):
        ''' Adapted from original ReCo 
        '''

        # resize first
        predict = resize(
                input=predict,
                size=target.shape[1:],
                mode='bilinear',
                align_corners=self.align_corners)
        logits = resize(
                input=logits,
                size=target.shape[1:],
                mode='bilinear',
                align_corners=self.align_corners)

        # 选取confidence大于预设值的做交叉熵
        batch_size = predict.shape[0]
        # TODO: 这里为什么要用float呢
        valid_mask = (target >= 0).float()   # only count valid pixels

        weighting = logits.view(batch_size, -1).ge(self.strong_thres).sum(-1) / valid_mask.view(batch_size, -1).sum(-1)
        loss = F.cross_entropy(predict, target, reduction='none', ignore_index=-1)
        # TODO: 这个交叉熵怎么也不会小于等于零啊，为什么要选择呢，搞不懂
        weighted_loss = torch.mean(torch.masked_select(weighting[:, None, None] * loss, loss > 0))
        return weighted_loss

    def label_onehot(self, inputs):
        ''' Convert label to one-hot vector '''
        batch_size, im_h, im_w = inputs.shape
        num_classes = self.decode_head.num_classes
        # remap invalid pixels (-1) into 0, otherwise we cannot create one-hot vector with negative labels.
        # we will still mask out those invalid values in valid mask
        inputs = torch.relu(inputs)
        outputs = torch.zeros([batch_size, num_classes, im_h, im_w]).to(inputs.device)
        return outputs.scatter_(1, inputs.unsqueeze(1), 1.0)

    def forward_train(self, labeled: dict, unlabeled: dict, **kargs):
        
        # generate pseudo labels for unlabeled data
        with torch.no_grad():
            ema_pred = self.ema_forward(unlabeled['img'])
            # TODO: interpolate may not be need
            # ema_pred = F.interpolate(ema_pred, size=labeled['gt_semantic_seg'].sahpe[1:], mode='bilinear', align_corners=self.align_corners)
            pseudo_logits, pseudo_labels = torch.max(ema_pred, dim=1)

        loss = dict()
        preds_l, reps_l = self.main_forward(labeled['img'])
        preds_u, reps_u = self.main_forward(unlabeled['img'])

        rep_all = torch.cat((reps_l, reps_u))
        pred_all = torch.cat((preds_l, preds_u))

        # supervised loss
        sup_loss = self.decode_head.losses(preds_l, labeled['gt_semantic_seg'])

        # pseudo label loss
        unsup_loss = self.compute_unsupervised_loss(preds_u, pseudo_labels, pseudo_logits)

        # ReCo loss
        with torch.no_grad():
            # mask
            pseudo_mask = pseudo_logits.ge(self.weak_thres)
            mask_all = torch.cat(labeled['gt_semantic_seg'].unsqueeze(1),
                                pseudo_mask.unsqueeze(1))

            # label
            one_hot_label = self.label_onehot(labeled['gt_semantic_seg'])
            one_hot_pseudo_label = self.label_onehot(pseudo_labels)
            label_all = torch.cat(one_hot_label, one_hot_pseudo_label)

            # predicted probability
            prob_l = torch.softmax(preds_l, dim=1)
            prob_u = torch.softmax(preds_u, dim=1)
            prob_all = torch.cat((prob_l, prob_u))

        reco_loss = self.compute_reco_loss(rep_all, label_all, mask_all, prob_all)
        loss.update({'sup loss': sup_loss, 'unsup loss': unsup_loss, 'reco loss': reco_loss})

        # auxiliary head
        if self.with_auxiliary_head:
            loss_aux = self._auxiliary_head_forward_train(
                labeled['img'], labeled['img_metas'], labeled['gt_semantic_seg'])
            loss.update(loss_aux)

        return loss

    def compute_reco_loss(self, rep, label, mask, prob):
        ''' 
        mask: labeled image的全部，unlabeled image中confidence大于某个值的
        '''
        batch_size, num_feat, im_w_, im_h = rep.shape
        num_classes = label.shape[1]   # 应该是 num_classes
        device = rep.device

        # compute valid binary mask for each pixel
        valid_pixel = label * mask

        # permute representation for indexing: batch x im_h x im_w x feature_channel
        rep = rep.permute(0, 2, 3, 1)

        # compute prototype (class mean representation) for each class across all valid pixels
        seg_feat_all_list = []
        seg_feat_hard_list = []
        seg_num_list = []
        seg_proto_list = []
        for i in range(num_classes):
            valid_pixel_seg = valid_pixel[:, i]  # select binary mask for i-th class
            if valid_pixel_seg.sum() == 0:  # not all classes would be available in a mini-batch
                continue

            prob_seg = prob[:, i, :, :]
            # 原来unlabeledd data不仅要大于weak_thres，还要小于strong_thres，这里的hard是difficult的意思
            rep_mask_hard = (prob_seg < self.strong_thres) * valid_pixel_seg.bool()  # select hard queries

            # prototype
            seg_proto_list.append(torch.mean(rep[valid_pixel_seg.bool()], dim=0, keepdim=True))
            seg_feat_all_list.append(rep[valid_pixel_seg.bool()])
            seg_feat_hard_list.append(rep[rep_mask_hard])   # 作为query
            #query像素的数量
            seg_num_list.append(int(valid_pixel_seg.sum().item()))  

        # compute regional contrastive loss
        if len(seg_num_list) <= 1:  # in some rare cases, a small mini-batch might only contain 1 or no semantic class
            return torch.tensor(0.0)
        else:
            reco_loss = torch.tensor(0.0)
            seg_proto = torch.cat(seg_proto_list)   #prototype
            valid_seg = len(seg_num_list)
            seg_len = torch.arange(valid_seg)

            for i in range(valid_seg):
                # sample hard queries, 这里的hard是difficult的意思
                if len(seg_feat_hard_list[i]) > 0:
                    seg_hard_idx = torch.randint(len(seg_feat_hard_list[i]), size=(self.num_queries,))
                    anchor_feat_hard = seg_feat_hard_list[i][seg_hard_idx]
                    anchor_feat = anchor_feat_hard
                else:  # in some rare cases, all queries in the current query class are easy
                    continue

                # apply negative key sampling (with no gradients)
                with torch.no_grad():
                    # generate index mask for the current query class; e.g. [0, 1, 2] -> [1, 2, 0] -> [2, 0, 1]
                    seg_mask = torch.cat(([seg_len[i:], seg_len[:i]]))

                    # compute similarity for each negative segment prototype (semantic class relation graph)
                    proto_sim = torch.cosine_similarity(seg_proto[seg_mask[0]].unsqueeze(0), seg_proto[seg_mask[1:]], dim=1)
                    proto_prob = torch.softmax(proto_sim / self.tmperature, dim=0)

                    # sampling negative keys based on the generated distribution [num_queries x num_negatives]
                    negative_dist = torch.distributions.categorical.Categorical(probs=proto_prob)
                    samp_class = negative_dist.sample(sample_shape=[self.num_queries, self.num_negatives])
                    samp_num = torch.stack([(samp_class == c).sum(1) for c in range(len(proto_prob))], dim=1)

                    # sample negative indices from each negative class
                    negative_num_list = seg_num_list[i+1:] + seg_num_list[:i]
                    negative_index = negative_index_sampler(samp_num, negative_num_list)

                    # index negative keys (from other classes)
                    negative_feat_all = torch.cat(seg_feat_all_list[i+1:] + seg_feat_all_list[:i])
                    negative_feat = negative_feat_all[negative_index].reshape(self.num_queries, self.num_negatives, num_feat) # 这都能reshape回来

                    # combine positive and negative keys: keys = [positive key | negative keys] with 1 + num_negative dim
                    positive_feat = seg_proto[i].unsqueeze(0).unsqueeze(0).repeat(self.num_queries, 1, 1)
                    all_feat = torch.cat((positive_feat, negative_feat), dim=1)

                seg_logits = torch.cosine_similarity(anchor_feat.unsqueeze(1), all_feat, dim=2)
                reco_loss = reco_loss + F.cross_entropy(seg_logits / self.tmperature, torch.zeros(self.num_queries).long().to(device))
            return reco_loss / valid_seg


def negative_index_sampler(samp_num, seg_num_list):
    negative_index = []
    for i in range(samp_num.shape[0]):
        for j in range(samp_num.shape[1]):
            negative_index += np.random.randint(low=sum(seg_num_list[:j]),
                                                high=sum(seg_num_list[:j+1]),
                                                size=int(samp_num[i, j])).tolist()
    return negative_index