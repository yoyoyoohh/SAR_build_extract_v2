'''
Author: Shuailin Chen
Created Date: 2021-07-11
Last Modified: 2021-07-16
	content: 
'''

_base_ = [
    '../_base_/models/deeplabv3plus_r50-d8.py', '../_base_/datasets/mix_bn_rs2_to_gf3.py',
    '../_base_/default_runtime.py', '../_base_/schedules/schedule_20k.py'
]


# change num_classess
# norm_cfg = dict(type='BN', requires_grad=True)
# norm_cfg = dict(type='SyncBN', requires_grad=True)
norm_cfg = dict(type='MixBN', requires_grad=True)
model = dict(
    type='Semi',
    backbone=dict(
        type='ResNetV1cMixBN',
        norm_cfg = norm_cfg,
    ),
    decode_head=dict(
        type='DepthwiseSeparableASPPHeadMixBN',
        norm_cfg = norm_cfg,
        num_classes=2,
        loss_decode=dict(
            type='CrossEntropyLossSemi', use_sigmoid=False, loss_weight=1.0)
    ),
    auxiliary_head=dict(
        type='FCNHeadMixBN',
        norm_cfg = norm_cfg,
        num_classes=2,
        loss_decode=dict(
            type='CrossEntropyLossSemi', use_sigmoid=False, loss_weight=1.0)
    ),
)

find_unused_parameters = True

# for schedule
optimizer = dict(type='SGD', lr=0.005, momentum=0.9, weight_decay=0.005)
optimizer_config = dict()
runner = dict(type='IterBasedRunner', max_iters=1600)
lr_config = dict(policy='poly', power=0.9, min_lr=0.001, by_epoch=False)
checkpoint_config = dict(by_epoch=False, interval=400)
evaluation = dict(interval=100, metric='mIoU')