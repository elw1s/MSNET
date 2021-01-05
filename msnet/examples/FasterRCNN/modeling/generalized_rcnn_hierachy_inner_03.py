# -*- coding: utf-8 -*-
# File:
"""
parallel mode
"""
import tensorflow as tf

from tensorpack import ModelDesc
from tensorpack.models import GlobalAvgPooling, l2_regularizer, regularize_cost
from tensorpack.tfutils import optimizer
from tensorpack.tfutils.summary import add_moving_summary

from config import config as cfg
from data import get_all_anchors, get_all_anchors_fpn
from utils.box_ops import area as tf_area
from common import filter_anchors_inner

from . import model_frcnn
from . import model_mrcnn
from .backbone import image_preprocess, resnet_c4_backbone, resnet_conv5, resnet_fpn_backbone
from .model_box import RPNAnchors, clip_boxes, crop_and_resize, roi_align
from .model_cascade import CascadeRCNNHead
from .model_fpn import fpn_model, generate_fpn_proposals_ori, generate_fpn_proposals, multilevel_roi_align, multilevel_rpn_losses,multilevel_rpn_losses_ori
from .model_frcnn import (
    BoxProposals, FastRCNNHead, fastrcnn_outputs, fastrcnn_predictions, sample_fast_rcnn_targets)
from .model_mrcnn import maskrcnn_loss, maskrcnn_upXconv_head, unpackbits_masks
from .model_rpn import generate_rpn_proposals, rpn_head, rpn_losses



def merge_bbox_proposals(proposal_house, proposal_damage):
    proposal_all = {}
    for k, v_house in proposal_house.items():
        v_damage = proposal_damage[k]
        v_all = tf.concat([v_house, v_damage], axis=0)
        proposal_all[k] = v_all
    return proposal_all


class GeneralizedRCNN(ModelDesc):
    def preprocess(self, image):
        image = tf.expand_dims(image, 0)
        image = image_preprocess(image, bgr=True)
        return tf.transpose(image, [0, 3, 1, 2])

    def optimizer(self):
        lr = tf.get_variable('learning_rate', initializer=0.003, trainable=False)
        tf.summary.scalar('learning_rate-summary', lr)

        # The learning rate in the config is set for 8 GPUs, and we use trainers with average=False.
        lr = lr / 8.
        opt = tf.train.MomentumOptimizer(lr, 0.9)
        if cfg.TRAIN.NUM_GPUS < 8:
            opt = optimizer.AccumGradOptimizer(opt, 8 // cfg.TRAIN.NUM_GPUS)
        return opt

    def get_inference_tensor_names(self):
        """
        Returns two lists of tensor names to be used to create an inference callable.

        `build_graph` must create tensors of these names when called under inference context.

        Returns:
            [str]: input names
            [str]: output names
        """
        out = ['output/boxes_house', 'output/scores_house', 'output/boxes', 'output/scores', 'output/labels']
        # out = ['output/boxes', 'output/scores', 'output/labels']
        if cfg.MODE_MASK:
            out.append('output/masks')
        return ['image'], out

    def build_graph(self, *inputs):
        inputs = dict(zip(self.input_names, inputs))
        if "gt_masks_packed" in inputs:
            gt_masks = tf.cast(unpackbits_masks(inputs.pop("gt_masks_packed")), tf.uint8, name="gt_masks")
            inputs["gt_masks"] = gt_masks

        image = self.preprocess(inputs['image'])     # 1CHW

        features = self.backbone(image)
        anchor_inputs = {k: v for k, v in inputs.items() if k.startswith('anchor_')}
        proposals_house, rpn_losses_house = self.rpn_house(image, features, anchor_inputs)  # inputs?
        # proposal on house bboxes
        # proposals, rpn_losses = self.rpn
        # TODO: Check the keys
        house_bboxes = proposals_house.boxes
        # print()

        # output_house_bbox = tf.identity(house_bboxes, name='output/boxes_house')



        proposals_damage, rpn_losses_damage = self.rpn_damage(image, features, anchor_inputs,house_bboxes)  # inputs?
        # proposals = merge_bbox_proposals(proposals_house, proposals_damage)
        proposals = proposals_damage
        targets = [inputs[k] for k in ['gt_boxes_damage', 'gt_labels', 'gt_masks'] if k in inputs]
        gt_boxes_area = tf.reduce_mean(tf_area(inputs["gt_boxes_damage"]), name='mean_gt_box_area')
        add_moving_summary(gt_boxes_area)
        head_losses = self.roi_heads(image, features, proposals, targets)

        if self.training:
            wd_cost = regularize_cost(
                '.*/W', l2_regularizer(cfg.TRAIN.WEIGHT_DECAY), name='wd_cost')
            total_cost = tf.add_n(
                rpn_losses_house + rpn_losses_damage + head_losses + [wd_cost], 'total_cost')
            add_moving_summary(total_cost, wd_cost)
            return total_cost
        else:
            # Check that the model defines the tensors it declares for inference
            # For existing models, they are defined in "fastrcnn_predictions(name_scope='output')"
            G = tf.get_default_graph()
            ns = G.get_name_scope()
            for name in self.get_inference_tensor_names()[1]:
                try:
                    name = '/'.join([ns, name]) if ns else name
                    G.get_tensor_by_name(name + ':0')
                except KeyError:
                    raise KeyError("Your model does not define the tensor '{}' in inference context.".format(name))

class ResNetFPNModel(GeneralizedRCNN):

    def inputs(self):
        ret = [
            tf.TensorSpec((None, None, 3), tf.float32, 'image')]
        num_anchors = len(cfg.RPN.ANCHOR_RATIOS)
        for k in range(len(cfg.FPN.ANCHOR_STRIDES)):
            ret.extend([
                tf.TensorSpec((None, None, num_anchors), tf.int32,
                              'anchor_labels_lvl{}_house'.format(k + 2)),
                tf.TensorSpec((None, None, num_anchors, 4), tf.float32,
                              'anchor_boxes_lvl{}_house'.format(k + 2)),
                tf.TensorSpec((None, None, num_anchors), tf.int32,
                              'anchor_labels_lvl{}_damage'.format(k + 2)),
                tf.TensorSpec((None, None, num_anchors, 4), tf.float32,
                              'anchor_boxes_lvl{}_damage'.format(k + 2))
            ])
        ret.extend([
            tf.TensorSpec((None, 4), tf.float32, 'gt_boxes_damage'),
            tf.TensorSpec((None, 4), tf.float32, 'gt_boxes_house'),
            tf.TensorSpec((None,), tf.int64, 'gt_labels')])  # all > 0
        if cfg.MODE_MASK:
            ret.append(
                tf.TensorSpec((None, None, None), tf.uint8, 'gt_masks_packed')
            )
        return ret

    def slice_feature_and_anchors(self, p23456, anchors):
        for i, stride in enumerate(cfg.FPN.ANCHOR_STRIDES):
            with tf.name_scope('FPN_slice_lvl{}'.format(i)):
                anchors[i] = anchors[i].narrow_to(p23456[i])

    def backbone(self, image):
        c2345 = resnet_fpn_backbone(image, cfg.BACKBONE.RESNET_NUM_BLOCKS)
        p23456 = fpn_model('fpn', c2345)
        return p23456

    def rpn_house(self, image, features, inputs):
        assert len(cfg.RPN.ANCHOR_SIZES) == len(cfg.FPN.ANCHOR_STRIDES)

        image_shape2d = tf.shape(image)[2:]     # h,w
        all_anchors_fpn = get_all_anchors_fpn(
            strides=cfg.FPN.ANCHOR_STRIDES,
            sizes=cfg.RPN.ANCHOR_SIZES,
            ratios=cfg.RPN.ANCHOR_RATIOS,
            max_size=cfg.PREPROC.MAX_SIZE)
        multilevel_anchors = [RPNAnchors(
            all_anchors_fpn[i],
            inputs['anchor_labels_lvl{}_house'.format(i + 2)],
            inputs['anchor_boxes_lvl{}_house'.format(i + 2)]) for i in range(len(all_anchors_fpn))]


        self.slice_feature_and_anchors(features, multilevel_anchors)

        # Multi-Level RPN Proposals
        rpn_outputs = [rpn_head('rpn_house', pi, cfg.FPN.NUM_CHANNEL, len(cfg.RPN.ANCHOR_RATIOS))
                       for pi in features]
        multilevel_label_logits = [k[0] for k in rpn_outputs]
        multilevel_box_logits = [k[1] for k in rpn_outputs]
        multilevel_pred_boxes = [anchor.decode_logits(logits)
                                 for anchor, logits in zip(multilevel_anchors, multilevel_box_logits)]
        # We should filter multilevel_pred_boxes?

        proposal_boxes, proposal_scores = generate_fpn_proposals_ori(
            multilevel_pred_boxes, multilevel_label_logits, image_shape2d)
        output_house_bbox = tf.identity(proposal_boxes,
                                        name='output/boxes_house')
        tf.print("output_house_bbox = ", output_house_bbox)
        output_house_score = tf.identity(proposal_scores, name='output/scores_house')
        tf.print("output_house_score = ", output_house_score)

        # tf.print()

        if self.training:
            losses = multilevel_rpn_losses_ori(
                multilevel_anchors, multilevel_label_logits, multilevel_box_logits)
        else:
            losses = []

        return BoxProposals(proposal_boxes), losses

    def rpn_damage(self, image, features, inputs, house_bboxes):
        # Filter out the ones that are not within the house bbox
        assert len(cfg.RPN.ANCHOR_SIZES) == len(cfg.FPN.ANCHOR_STRIDES)

        image_shape2d = tf.shape(image)[2:]     # h,w
        all_anchors_fpn = get_all_anchors_fpn(
            strides=cfg.FPN.ANCHOR_STRIDES,
            sizes=cfg.RPN.ANCHOR_SIZES,
            ratios=cfg.RPN.ANCHOR_RATIOS,
            max_size=cfg.PREPROC.MAX_SIZE)
        # Filter anchors here: lvl and house bboxes
        # TODO: IOU filter.
        masks = filter_anchors_inner(house_bboxes, all_anchors_fpn, 0.3)


        multilevel_anchors = [RPNAnchors(
            all_anchors_fpn[i],
            inputs['anchor_labels_lvl{}_damage'.format(i + 2)],
            inputs['anchor_boxes_lvl{}_damage'.format(i + 2)]) for i in range(len(all_anchors_fpn))]


        self.slice_feature_and_anchors(features, multilevel_anchors)

        # Multi-Level RPN Proposals
        rpn_outputs = [rpn_head('rpn_damage', pi, cfg.FPN.NUM_CHANNEL, len(cfg.RPN.ANCHOR_RATIOS))
                       for pi in features]
        multilevel_label_logits = [k[0] for k in rpn_outputs]
        multilevel_box_logits = [k[1] for k in rpn_outputs]
        # Get target for anchor
        multilevel_pred_boxes = [anchor.decode_logits(logits)
                                 for anchor, logits in zip(multilevel_anchors, multilevel_box_logits)]

        proposal_boxes, proposal_scores = generate_fpn_proposals(
            multilevel_pred_boxes, multilevel_label_logits, image_shape2d, masks)

        if self.training:
            losses = multilevel_rpn_losses(
                multilevel_anchors, multilevel_label_logits, multilevel_box_logits, masks)
        else:
            losses = []

        return BoxProposals(proposal_boxes), losses

    def roi_heads(self, image, features, proposals, targets):
        image_shape2d = tf.shape(image)[2:]     # h,w
        assert len(features) == 5, "Features have to be P23456!"
        gt_boxes, gt_labels, *_ = targets

        if self.training:
            proposals = sample_fast_rcnn_targets(proposals.boxes, gt_boxes, gt_labels)

        fastrcnn_head_func = getattr(model_frcnn, cfg.FPN.FRCNN_HEAD_FUNC)
        if not cfg.FPN.CASCADE:
            roi_feature_fastrcnn = multilevel_roi_align(features[:4], proposals.boxes, 7)

            head_feature = fastrcnn_head_func('fastrcnn', roi_feature_fastrcnn)
            fastrcnn_label_logits, fastrcnn_box_logits = fastrcnn_outputs(
                'fastrcnn/outputs', head_feature, cfg.DATA.NUM_CATEGORY)
            fastrcnn_head = FastRCNNHead(proposals, fastrcnn_box_logits, fastrcnn_label_logits,
                                         gt_boxes, tf.constant(cfg.FRCNN.BBOX_REG_WEIGHTS, dtype=tf.float32))
        else:
            def roi_func(boxes):
                return multilevel_roi_align(features[:4], boxes, 7)

            fastrcnn_head = CascadeRCNNHead(
                proposals, roi_func, fastrcnn_head_func,
                (gt_boxes, gt_labels), image_shape2d, cfg.DATA.NUM_CATEGORY)

        if self.training:
            all_losses = fastrcnn_head.losses()

            if cfg.MODE_MASK:
                gt_masks = targets[2]
                # maskrcnn loss
                roi_feature_maskrcnn = multilevel_roi_align(
                    features[:4], proposals.fg_boxes(), 14,
                    name_scope='multilevel_roi_align_mask')
                maskrcnn_head_func = getattr(model_mrcnn, cfg.FPN.MRCNN_HEAD_FUNC)
                mask_logits = maskrcnn_head_func(
                    'maskrcnn', roi_feature_maskrcnn, cfg.DATA.NUM_CATEGORY)   # #fg x #cat x 28 x 28

                target_masks_for_fg = crop_and_resize(
                    tf.expand_dims(gt_masks, 1),
                    proposals.fg_boxes(),
                    proposals.fg_inds_wrt_gt, 28,
                    pad_border=False)  # fg x 1x28x28
                target_masks_for_fg = tf.squeeze(target_masks_for_fg, 1, 'sampled_fg_mask_targets')
                all_losses.append(maskrcnn_loss(mask_logits, proposals.fg_labels(), target_masks_for_fg))
            return all_losses
        else:
            decoded_boxes = fastrcnn_head.decoded_output_boxes()
            decoded_boxes = clip_boxes(decoded_boxes, image_shape2d, name='fastrcnn_all_boxes')
            label_scores = fastrcnn_head.output_scores(name='fastrcnn_all_scores')
            final_boxes, final_scores, final_labels = fastrcnn_predictions(
                decoded_boxes, label_scores, name_scope='output')
            if cfg.MODE_MASK:
                # Cascade inference needs roi transform with refined boxes.
                roi_feature_maskrcnn = multilevel_roi_align(features[:4], final_boxes, 14)
                maskrcnn_head_func = getattr(model_mrcnn, cfg.FPN.MRCNN_HEAD_FUNC)
                mask_logits = maskrcnn_head_func(
                    'maskrcnn', roi_feature_maskrcnn, cfg.DATA.NUM_CATEGORY)   # #fg x #cat x 28 x 28
                indices = tf.stack([tf.range(tf.size(final_labels)), tf.cast(final_labels, tf.int32) - 1], axis=1)
                final_mask_logits = tf.gather_nd(mask_logits, indices)   # #resultx28x28
                tf.sigmoid(final_mask_logits, name='output/masks')
            return []

class ResNetC4Model(GeneralizedRCNN):
    def inputs(self):
        ret = [
            tf.TensorSpec((None, None, 3), tf.float32, 'image'),
            tf.TensorSpec((None, None, cfg.RPN.NUM_ANCHOR), tf.int32, 'anchor_labels'),
            tf.TensorSpec((None, None, cfg.RPN.NUM_ANCHOR, 4), tf.float32, 'anchor_boxes'),
            tf.TensorSpec((None, 4), tf.float32, 'gt_boxes'),
            tf.TensorSpec((None,), tf.int64, 'gt_labels')]  # all > 0
        if cfg.MODE_MASK:
            ret.append(
                tf.TensorSpec((None, None, None), tf.uint8, 'gt_masks_packed')
            )   # NR_GT x height x ceil(width/8), packed groundtruth masks
        return ret

    def backbone(self, image):
        return [resnet_c4_backbone(image, cfg.BACKBONE.RESNET_NUM_BLOCKS[:3])]

    # TODO: Make two rpns here
    def rpn(self, image, features, inputs):
        featuremap = features[0]
        rpn_label_logits, rpn_box_logits = rpn_head('rpn', featuremap, cfg.RPN.HEAD_DIM, cfg.RPN.NUM_ANCHOR)
        anchors = RPNAnchors(
            get_all_anchors(
                stride=cfg.RPN.ANCHOR_STRIDE, sizes=cfg.RPN.ANCHOR_SIZES,
                ratios=cfg.RPN.ANCHOR_RATIOS, max_size=cfg.PREPROC.MAX_SIZE),
            inputs['anchor_labels'], inputs['anchor_boxes'])
        anchors = anchors.narrow_to(featuremap)

        image_shape2d = tf.shape(image)[2:]     # h,w
        pred_boxes_decoded = anchors.decode_logits(rpn_box_logits)  # fHxfWxNAx4, floatbox
        proposal_boxes, proposal_scores = generate_rpn_proposals(
            tf.reshape(pred_boxes_decoded, [-1, 4]),
            tf.reshape(rpn_label_logits, [-1]),
            image_shape2d,
            cfg.RPN.TRAIN_PRE_NMS_TOPK if self.training else cfg.RPN.TEST_PRE_NMS_TOPK,
            cfg.RPN.TRAIN_POST_NMS_TOPK if self.training else cfg.RPN.TEST_POST_NMS_TOPK)

        if self.training:
            losses = rpn_losses(
                anchors.gt_labels, anchors.encoded_gt_boxes(), rpn_label_logits, rpn_box_logits)
        else:
            losses = []
        # TODO: use logits and attention
        return BoxProposals(proposal_boxes), losses

    def roi_heads(self, image, features, proposals, targets):
        image_shape2d = tf.shape(image)[2:]     # h,w
        featuremap = features[0]

        gt_boxes, gt_labels, *_ = targets

        if self.training:
            # sample proposal boxes in training
            proposals = sample_fast_rcnn_targets(proposals.boxes, gt_boxes, gt_labels)
        # The boxes to be used to crop RoIs.
        # Use all proposal boxes in inference

        boxes_on_featuremap = proposals.boxes * (1.0 / cfg.RPN.ANCHOR_STRIDE)
        roi_resized = roi_align(featuremap, boxes_on_featuremap, 14)

        feature_fastrcnn = resnet_conv5(roi_resized, cfg.BACKBONE.RESNET_NUM_BLOCKS[-1])    # nxcx7x7
        # Keep C5 feature to be shared with mask branch
        feature_gap = GlobalAvgPooling('gap', feature_fastrcnn, data_format='channels_first')
        fastrcnn_label_logits, fastrcnn_box_logits = fastrcnn_outputs('fastrcnn', feature_gap, cfg.DATA.NUM_CATEGORY)

        fastrcnn_head = FastRCNNHead(proposals, fastrcnn_box_logits, fastrcnn_label_logits, gt_boxes,
                                     tf.constant(cfg.FRCNN.BBOX_REG_WEIGHTS, dtype=tf.float32))

        if self.training:
            all_losses = fastrcnn_head.losses()

            if cfg.MODE_MASK:
                gt_masks = targets[2]
                # maskrcnn loss
                # In training, mask branch shares the same C5 feature.
                fg_feature = tf.gather(feature_fastrcnn, proposals.fg_inds())
                mask_logits = maskrcnn_upXconv_head(
                    'maskrcnn', fg_feature, cfg.DATA.NUM_CATEGORY, num_convs=0)   # #fg x #cat x 14x14

                target_masks_for_fg = crop_and_resize(
                    tf.expand_dims(gt_masks, 1),
                    proposals.fg_boxes(),
                    proposals.fg_inds_wrt_gt, 14,
                    pad_border=False)  # nfg x 1x14x14
                target_masks_for_fg = tf.squeeze(target_masks_for_fg, 1, 'sampled_fg_mask_targets')
                all_losses.append(maskrcnn_loss(mask_logits, proposals.fg_labels(), target_masks_for_fg))
            return all_losses
        else:
            decoded_boxes = fastrcnn_head.decoded_output_boxes()
            decoded_boxes = clip_boxes(decoded_boxes, image_shape2d, name='fastrcnn_all_boxes')
            label_scores = fastrcnn_head.output_scores(name='fastrcnn_all_scores')
            final_boxes, final_scores, final_labels = fastrcnn_predictions(
                decoded_boxes, label_scores, name_scope='output')

            if cfg.MODE_MASK:
                roi_resized = roi_align(featuremap, final_boxes * (1.0 / cfg.RPN.ANCHOR_STRIDE), 14)
                feature_maskrcnn = resnet_conv5(roi_resized, cfg.BACKBONE.RESNET_NUM_BLOCKS[-1])
                mask_logits = maskrcnn_upXconv_head(
                    'maskrcnn', feature_maskrcnn, cfg.DATA.NUM_CATEGORY, 0)   # #result x #cat x 14x14
                indices = tf.stack([tf.range(tf.size(final_labels)), tf.cast(final_labels, tf.int32) - 1], axis=1)
                final_mask_logits = tf.gather_nd(mask_logits, indices)   # #resultx14x14
                tf.sigmoid(final_mask_logits, name='output/masks')
            return []



