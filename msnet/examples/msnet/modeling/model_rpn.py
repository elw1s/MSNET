# -*- coding: utf-8 -*-

import tensorflow as tf
import numpy as np

from tensorpack.models import Conv2D, layer_register
from tensorpack.tfutils.argscope import argscope
from tensorpack.tfutils.scope_utils import auto_reuse_variable_scope, under_name_scope
from tensorpack.tfutils.summary import add_moving_summary
from tensorpack.utils.argtools import memoized

from config import config as cfg
from .model_box import clip_boxes


@layer_register(log_shape=True)
@auto_reuse_variable_scope
def rpn_head(featuremap, channel, num_anchors):
    """
    Returns:
        label_logits: fHxfWxNA
        box_logits: fHxfWxNAx4
    """
    with argscope(Conv2D, data_format='channels_first',
                  kernel_initializer=tf.compat.v1.random_normal_initializer(stddev=0.01)):
        hidden = Conv2D('conv0', featuremap, channel, 3, activation=tf.nn.relu)

        label_logits = Conv2D('class', hidden, num_anchors, 1)
        box_logits = Conv2D('box', hidden, 4 * num_anchors, 1)
        # 1, NA(*4), im/16, im/16 (NCHW)

        label_logits = tf.transpose(a=label_logits, perm=[0, 2, 3, 1])  # 1xfHxfWxNA
        label_logits = tf.squeeze(label_logits, 0)  # fHxfWxNA

        shp = tf.shape(input=box_logits)  # 1x(NAx4)xfHxfW
        box_logits = tf.transpose(a=box_logits, perm=[0, 2, 3, 1])  # 1xfHxfWx(NAx4)
        box_logits = tf.reshape(box_logits, tf.stack([shp[2], shp[3], num_anchors, 4]))  # fHxfWxNAx4
    return label_logits, box_logits


@layer_register(log_shape=True)
@auto_reuse_variable_scope
def dil_rpn_head(featuremap, channel, num_anchors):
    """
    Returns:
        label_logits: fHxfWxNA
        box_logits: fHxfWxNAx4
    """
    with argscope(Conv2D, data_format='channels_first', dilation_rate=(3, 3),
                  kernel_initializer=tf.compat.v1.random_normal_initializer(stddev=0.01)):
        hidden = Conv2D('conv0', featuremap, channel, 3, activation=tf.nn.relu)

        label_logits = Conv2D('class', hidden, num_anchors, 1)
        box_logits = Conv2D('box', hidden, 4 * num_anchors, 1)
        # 1, NA(*4), im/16, im/16 (NCHW)

        label_logits = tf.transpose(a=label_logits, perm=[0, 2, 3, 1])  # 1xfHxfWxNA
        label_logits = tf.squeeze(label_logits, 0)  # fHxfWxNA

        shp = tf.shape(input=box_logits)  # 1x(NAx4)xfHxfW
        box_logits = tf.transpose(a=box_logits, perm=[0, 2, 3, 1])  # 1xfHxfWx(NAx4)
        box_logits = tf.reshape(box_logits, tf.stack([shp[2], shp[3], num_anchors, 4]))  # fHxfWxNAx4
    return label_logits, box_logits


@under_name_scope()
def rpn_losses_ori(anchor_labels, anchor_boxes, label_logits, box_logits):
    """
    Args:
        anchor_labels: fHxfWxNA
        anchor_boxes: fHxfWxNAx4, encoded
        label_logits:  fHxfWxNA
        box_logits: fHxfWxNAx4

    Returns:
        label_loss, box_loss
    """
    with tf.device('/cpu:0'):
        valid_mask = tf.stop_gradient(tf.not_equal(anchor_labels, -1))
        pos_mask = tf.stop_gradient(tf.equal(anchor_labels, 1))
        nr_valid = tf.stop_gradient(tf.math.count_nonzero(valid_mask, dtype=tf.int32), name='num_valid_anchor')
        nr_pos = tf.identity(tf.math.count_nonzero(pos_mask, dtype=tf.int32), name='num_pos_anchor')
        # nr_pos is guaranteed >0 in C4. But in FPN. even nr_valid could be 0.

        valid_anchor_labels = tf.boolean_mask(tensor=anchor_labels, mask=valid_mask)
    valid_label_logits = tf.boolean_mask(tensor=label_logits, mask=valid_mask)

    with tf.compat.v1.name_scope('label_metrics'):
        valid_label_prob = tf.nn.sigmoid(valid_label_logits)
        summaries = []
        with tf.device('/cpu:0'):
            for th in [0.5, 0.2, 0.1]:
                valid_prediction = tf.cast(valid_label_prob > th, tf.int32)
                nr_pos_prediction = tf.reduce_sum(input_tensor=valid_prediction, name='num_pos_prediction')
                pos_prediction_corr = tf.math.count_nonzero(
                    tf.logical_and(
                        valid_label_prob > th,
                        tf.equal(valid_prediction, valid_anchor_labels)),
                    dtype=tf.int32)
                placeholder = 0.5   # A small value will make summaries appear lower.
                recall = tf.cast(tf.truediv(pos_prediction_corr, nr_pos), tf.float32)
                recall = tf.compat.v1.where(tf.equal(nr_pos, 0), placeholder, recall, name='recall_th{}'.format(th))
                precision = tf.cast(tf.truediv(pos_prediction_corr, nr_pos_prediction), tf.float32)
                precision = tf.compat.v1.where(tf.equal(nr_pos_prediction, 0),
                                     placeholder, precision, name='precision_th{}'.format(th))
                summaries.extend([precision, recall])
        add_moving_summary(*summaries)

    # Per-level loss summaries in FPN may appear lower due to the use of a small placeholder.
    # But the total RPN loss will be fine.  TODO make the summary op smarter
    placeholder = 0.
    label_loss = tf.nn.sigmoid_cross_entropy_with_logits(
        labels=tf.cast(valid_anchor_labels, tf.float32), logits=valid_label_logits)
    label_loss = tf.reduce_sum(input_tensor=label_loss) * (1. / cfg.RPN.BATCH_PER_IM)
    label_loss = tf.compat.v1.where(tf.equal(nr_valid, 0), placeholder, label_loss, name='label_loss')

    pos_anchor_boxes = tf.boolean_mask(tensor=anchor_boxes, mask=pos_mask)
    pos_box_logits = tf.boolean_mask(tensor=box_logits, mask=pos_mask)
    delta = 1.0 / 9
    box_loss = tf.compat.v1.losses.huber_loss(
        pos_anchor_boxes, pos_box_logits, delta=delta,
        reduction=tf.compat.v1.losses.Reduction.SUM) / delta
    box_loss = box_loss * (1. / cfg.RPN.BATCH_PER_IM)
    box_loss = tf.compat.v1.where(tf.equal(nr_pos, 0), placeholder, box_loss, name='box_loss')

    add_moving_summary(label_loss, box_loss, nr_valid, nr_pos)
    return [label_loss, box_loss]


@under_name_scope()
def rpn_losses(anchor_labels, anchor_boxes, label_logits, box_logits, mask):
    """
    Args:
        anchor_labels: fHxfWxNA
        anchor_boxes: fHxfWxNAx4, encoded
        label_logits:  fHxfWxNA
        box_logits: fHxfWxNAx4
        mask: SxSxNUM_ratiosx1

    Returns:
        label_loss, box_loss
    """
    # Filter here
    anchor_labels = tf.boolean_mask(tensor=anchor_labels, mask=mask)
    anchor_boxes = tf.boolean_mask(tensor=anchor_boxes, mask=mask)
    label_logits = tf.boolean_mask(tensor=label_logits, mask=mask)
    box_logits = tf.boolean_mask(tensor=box_logits, mask=mask)

    with tf.device('/cpu:0'):
        valid_mask = tf.stop_gradient(tf.not_equal(anchor_labels, -1))
        pos_mask = tf.stop_gradient(tf.equal(anchor_labels, 1))
        nr_valid = tf.stop_gradient(tf.math.count_nonzero(valid_mask, dtype=tf.int32), name='num_valid_anchor')
        nr_pos = tf.identity(tf.math.count_nonzero(pos_mask, dtype=tf.int32), name='num_pos_anchor')
        # nr_pos is guaranteed >0 in C4. But in FPN. even nr_valid could be 0.

        valid_anchor_labels = tf.boolean_mask(tensor=anchor_labels, mask=valid_mask)
    valid_label_logits = tf.boolean_mask(tensor=label_logits, mask=valid_mask)

    with tf.compat.v1.name_scope('label_metrics'):
        valid_label_prob = tf.nn.sigmoid(valid_label_logits)
        summaries = []
        with tf.device('/cpu:0'):
            for th in [0.5, 0.2, 0.1]:
                valid_prediction = tf.cast(valid_label_prob > th, tf.int32)
                nr_pos_prediction = tf.reduce_sum(input_tensor=valid_prediction, name='num_pos_prediction')
                pos_prediction_corr = tf.math.count_nonzero(
                    tf.logical_and(
                        valid_label_prob > th,
                        tf.equal(valid_prediction, valid_anchor_labels)),
                    dtype=tf.int32)
                placeholder = 0.5   # A small value will make summaries appear lower.
                recall = tf.cast(tf.truediv(pos_prediction_corr, nr_pos), tf.float32)
                recall = tf.compat.v1.where(tf.equal(nr_pos, 0), placeholder, recall, name='recall_th{}'.format(th))
                precision = tf.cast(tf.truediv(pos_prediction_corr, nr_pos_prediction), tf.float32)
                precision = tf.compat.v1.where(tf.equal(nr_pos_prediction, 0),
                                     placeholder, precision, name='precision_th{}'.format(th))
                summaries.extend([precision, recall])
        add_moving_summary(*summaries)

    # Per-level loss summaries in FPN may appear lower due to the use of a small placeholder.
    # But the total RPN loss will be fine.  TODO make the summary op smarter
    placeholder = 0.
    label_loss = tf.nn.sigmoid_cross_entropy_with_logits(
        labels=tf.cast(valid_anchor_labels, tf.float32), logits=valid_label_logits)
    label_loss = tf.reduce_sum(input_tensor=label_loss) * (1. / cfg.RPN.BATCH_PER_IM)
    label_loss = tf.compat.v1.where(tf.equal(nr_valid, 0), placeholder, label_loss, name='label_loss')

    pos_anchor_boxes = tf.boolean_mask(tensor=anchor_boxes, mask=pos_mask)
    pos_box_logits = tf.boolean_mask(tensor=box_logits, mask=pos_mask)
    delta = 1.0 / 9
    box_loss = tf.compat.v1.losses.huber_loss(
        pos_anchor_boxes, pos_box_logits, delta=delta,
        reduction=tf.compat.v1.losses.Reduction.SUM) / delta
    box_loss = box_loss * (1. / cfg.RPN.BATCH_PER_IM)
    box_loss = tf.compat.v1.where(tf.equal(nr_pos, 0), placeholder, box_loss, name='box_loss')

    add_moving_summary(label_loss, box_loss, nr_valid, nr_pos)
    return [label_loss, box_loss]


@under_name_scope()
def generate_rpn_proposals(boxes, scores, img_shape,
                           pre_nms_topk, post_nms_topk=None):
    """
    Sample RPN proposals by the following steps:
    1. Pick top k1 by scores
    2. NMS them
    3. Pick top k2 by scores. Default k2 == k1, i.e. does not filter the NMS output.

    Args:
        boxes: nx4 float dtype, the proposal boxes. Decoded to floatbox already
        scores: n float, the logits
        img_shape: [h, w]
        pre_nms_topk, post_nms_topk (int): See above.

    Returns:
        boxes: kx4 float
        scores: k logits
    """
    assert boxes.shape.ndims == 2, boxes.shape
    if post_nms_topk is None:
        post_nms_topk = pre_nms_topk

    topk = tf.minimum(pre_nms_topk, tf.size(input=scores))
    topk_scores, topk_indices = tf.nn.top_k(scores, k=topk, sorted=False)
    topk_boxes = tf.gather(boxes, topk_indices)
    topk_boxes = clip_boxes(topk_boxes, img_shape)

    if cfg.RPN.MIN_SIZE > 0:
        topk_boxes_x1y1x2y2 = tf.reshape(topk_boxes, (-1, 2, 2))
        topk_boxes_x1y1, topk_boxes_x2y2 = tf.split(topk_boxes_x1y1x2y2, 2, axis=1)
        # nx1x2 each
        wbhb = tf.squeeze(topk_boxes_x2y2 - topk_boxes_x1y1, axis=1)
        valid = tf.reduce_all(input_tensor=wbhb > cfg.RPN.MIN_SIZE, axis=1)  # n,
        topk_valid_boxes = tf.boolean_mask(tensor=topk_boxes, mask=valid)
        topk_valid_scores = tf.boolean_mask(tensor=topk_scores, mask=valid)
    else:
        topk_valid_boxes = topk_boxes
        topk_valid_scores = topk_scores

    nms_indices = tf.image.non_max_suppression(
        topk_valid_boxes,
        topk_valid_scores,
        max_output_size=post_nms_topk,
        iou_threshold=cfg.RPN.PROPOSAL_NMS_THRESH)

    proposal_boxes = tf.gather(topk_valid_boxes, nms_indices)
    proposal_scores = tf.gather(topk_valid_scores, nms_indices)
    tf.sigmoid(proposal_scores, name='probs')  # for visualization
    return tf.stop_gradient(proposal_boxes, name='boxes'), tf.stop_gradient(proposal_scores, name='scores')


@memoized
def get_all_anchors(*, stride, sizes, ratios, max_size):
    """
    Get all anchors in the largest possible image, shifted, floatbox
    Args:
        stride (int): the stride of anchors.
        sizes (tuple[int]): the sizes (sqrt area) of anchors
        ratios (tuple[int]): the aspect ratios of anchors
        max_size (int): maximum size of input image

    Returns:
        anchors: SxSxNUM_ANCHORx4, where S == ceil(MAX_SIZE/STRIDE), floatbox
        The layout in the NUM_ANCHOR dim is NUM_RATIO x NUM_SIZE.

    """
    # Generates a NAx4 matrix of anchor boxes in (x1, y1, x2, y2) format. Anchors
    # are centered on 0, have sqrt areas equal to the specified sizes, and aspect ratios as given.
    anchors = []
    for sz in sizes:
        for ratio in ratios:
            w = np.sqrt(sz * sz / ratio)
            h = ratio * w
            anchors.append([-w, -h, w, h])
    cell_anchors = np.asarray(anchors) * 0.5

    field_size = int(np.ceil(max_size / stride))
    shifts = (np.arange(0, field_size) * stride).astype("float32")
    shift_x, shift_y = np.meshgrid(shifts, shifts)
    shift_x = shift_x.flatten()
    shift_y = shift_y.flatten()
    shifts = np.vstack((shift_x, shift_y, shift_x, shift_y)).transpose()
    # Kx4, K = field_size * field_size
    K = shifts.shape[0]

    A = cell_anchors.shape[0]
    field_of_anchors = cell_anchors.reshape((1, A, 4)) + shifts.reshape((1, K, 4)).transpose((1, 0, 2))
    field_of_anchors = field_of_anchors.reshape((field_size, field_size, A, 4))
    # FSxFSxAx4
    # Many rounding happens inside the anchor code anyway
    # assert np.all(field_of_anchors == field_of_anchors.astype('int32'))
    field_of_anchors = field_of_anchors.astype("float32")
    return field_of_anchors
