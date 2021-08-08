'''
Author: Shuailin Chen
Created Date: 2021-07-11
Last Modified: 2021-07-14
	content: 
'''

_base_ = [
    '../_base_/models/deeplabv3plus_r50-d8.py', '../_base_/datasets/sar_building_rs2_to_gf3.py',
    '../_base_/default_runtime.py', '../_base_/schedules/schedule_20k.py'
]

# change num_classess
norm_cfg = dict(type='BN', requires_grad=True)
# norm_cfg = dict(type='SyncBN', requires_grad=True)
model = dict(
    backbone=dict(
        norm_cfg = norm_cfg,
    ),
    decode_head=dict(
        norm_cfg = norm_cfg,
        num_classes=2,
    ),
    auxiliary_head=[
        dict(
        type='FCNHead',
        in_channels=1024,
        in_index=2,
        channels=256,
        num_convs=1,
        concat_input=False,
        dropout_ratio=0.1,
        num_classes=2,
        norm_cfg=norm_cfg,
        align_corners=False,
        loss_decode=dict(
            type='CrossEntropyLoss', use_sigmoid=False, loss_weight=0.4)),
        dict(
        type='NewFCNHead',
        in_channels=(1024, 1),  # should be a list with equal length with in_index
        in_index=(0, 1,),
        channels=256,
        num_convs=0,
        concat_input=False,
        dropout_ratio=0.1,
        num_classes=2,
        norm_cfg=norm_cfg,
        align_corners=False,
        input_transform = 'multiple_select',
        loss_decode=dict(
            type='RelaxedInstanceWhiteningLoss', relax_denom=64, loss_weight=0.4))
    ]
    
)

# for schedule
optimizer = dict(type='SGD', lr=0.005, momentum=0.9, weight_decay=0.005)
lr_config = dict(policy='poly', power=0.9, min_lr=0.001, by_epoch=False)
runner = dict(type='IterBasedRunner', max_iters=800)
checkpoint_config = dict(by_epoch=False, interval=100)
evaluation = dict(interval=100, metric='mIoU')