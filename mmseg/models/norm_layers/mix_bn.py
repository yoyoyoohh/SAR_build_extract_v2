'''
Author: Shuailin Chen
Created Date: 2021-08-08
Last Modified: 2021-08-12
	content: 
'''

from mmcv.cnn import NORM_LAYERS
import torch.nn.functional as F
import torch
from torch.nn.modules.batchnorm import _BatchNorm, BatchNorm2d
from torch import Tensor
from typing import Optional


from ..segmentors import Semi


@NORM_LAYERS.register_module()
class MixBN(BatchNorm2d):
    ''' Mix BN parameters 
    
    Args:
        ratio (float): mixup ratio, must in [0, 1]. Default: 0.5    
        detach (bool): if to detach BN with respect to target domain. Default: 
            True
        model (int): mix mode. 0: target domain's mean and var as source'
            domain's mean and var. Default: 0
    '''

    def __init__(self, num_features, ratio=0.5, detach=True, mode=0,
                momentum=0.1, eps=1e-5, **kargs):
        super().__init__(num_features, eps=eps, momentum=momentum, **kargs)

        assert ratio>=0 and ratio <=1
        self.ratio = ratio
        self.detach = detach
        self.mode = mode

        # shape = (1, num_features, 1, 1)
        self.running_mean_dst = torch.zeros(num_features).cuda()
        self.running_var_dst = torch.ones(num_features).cuda()

        # self.running_mean_dst = torch.nn.regis

    # def cuda(self, device=None):
    #     super().cuda(device=device)    
    #     self.weight.cuda(device=device) 
    #     self.bias.cuda(device=device) 
    #     self.running_mean.cuda(device=device) 
    #     self.running_var.cuda(device=device) 
    #     self.running_mean_dst.cuda(device=device) 
    #     self.running_var_dst.cuda(device=device) 

    def forward(self, input:Optional[Tensor], domain):
        '''
        Args:
            domain (list): list indicates source domain and target domain, 0
                indicates source, 1 indicates target            
        '''

        if not torch.is_grad_enabled():
            # src_ouput, self.running_mean, self.running_var, _, _ = mix_bn(input, self.weight, self.bias, self.running_mean, self.running_var, self.eps, self.momentum, self.ratio, 0, 0)
            # return src_ouput
            return super().forward(input)
        else:
            src_input, dst_input = Semi.split_domins_data(input, domain=domain)

            # dst_idx = np.argwhere(domain).flatten()
            # src_idx = np.argwhere(np.logical_not(domain)).flatten()
            # dst_input = input[dst_idx, ...]
            # src_input = input[src_idx, ...]
            
            # dst params
            dst_output, _, _, mean_dst, var_dst = mix_bn(
                dst_input, self.weight, self.bias, self.running_mean_dst, self.running_var_dst,
                self.eps, self.momentum, ratio=1, mean=0, var=0)

            if self.detach:
                mean_dst = mean_dst.detach()
                var_dst = var_dst.detach()

            # src params
            if self.mode==0:
                ''' replace source mean and var with targe mean and var'''
                src_ouput, self.running_mean, self.running_var, _, _ = mix_bn(src_input, self.weight, self.bias, self.running_mean, self.running_var, self.eps, self.momentum, self.ratio, mean_dst, var_dst)
            else:
                raise NotImplementedError
            
            output = Semi.merge_domains_data((src_ouput, dst_output), domain=domain)
            
            # output = torch.empty_like(input)
            # output[dst_idx, ...] = dst_output
            # output[src_idx, ...] = src_ouput
            
            return output



def mix_bn(X, weight, bias, running_mean, running_var, eps, momentum, ratio,
            mean=0, var=0):
    ''' BatchNorm implementation from dive into deep learning
    '''

    # Use `is_grad_enabled` to determine whether the current mode is training
    # mode or prediction mode
    if not torch.is_grad_enabled():
        # deprecated !!!
        weight = weight.reshape(1, weight.shape[0], 1, 1)
        bias = bias.reshape(1, bias.shape[0], 1, 1)
        X_hat = (X - running_mean) / torch.sqrt(running_var + eps)
    else:
        local_mean = X.mean(dim=(0, 2, 3), keepdim=True)
        local_var = ((X - mean)**2).mean(dim=(0, 2, 3), keepdim=True)

        mean = ratio * local_mean + (1-ratio)*mean
        var = ratio * local_var + (1-ratio)*var

        X_hat = (X - mean) / torch.sqrt(var + eps)

        # Update the mean and variance using running average
        running_mean = momentum * mean.squeeze() \
                        + (1.0 - momentum) * running_mean
        running_var = momentum * var.squeeze() \
                        + (1.0 - momentum) * running_var

    # reshape for broadcasting
    weight = weight.reshape(1, weight.shape[0], 1, 1)
    bias = bias.reshape(1, bias.shape[0], 1, 1)
    Y = weight * X_hat + bias           # Scale and shift

    return Y, running_mean.data, running_var.data, mean, var

