# -*- coding: utf-8 -*-
# File: common.py

import numpy as np
import cv2

from tensorpack.dataflow import RNGDataFlow
from tensorpack.dataflow.imgaug import ImageAugmentor, ResizeTransform
from utils.np_box_ops import iou as np_iou  # noqa
from utils.box_ops import pairwise_iou,pairwise_inner
import tensorflow as tf
import tensorflow.compat.v1 as tfc

class DataFromListOfDict(RNGDataFlow):
    def __init__(self, lst, keys, shuffle=False):
        self._lst = lst
        self._keys = keys
        self._shuffle = shuffle
        self._size = len(lst)

    def __len__(self):
        return self._size

    def __iter__(self):
        if self._shuffle:
            self.rng.shuffle(self._lst)
        for dic in self._lst:
            dp = [dic[k] for k in self._keys]
            yield dp


class CustomResize(ImageAugmentor):
    """
    Try resizing the shortest edge to a certain number
    while avoiding the longest edge to exceed max_size.
    """

    def __init__(self, short_edge_length, max_size, interp=cv2.INTER_LINEAR):
        """
        Args:
            short_edge_length ([int, int]): a [min, max] interval from which to sample the
                shortest edge length.
            max_size (int): maximum allowed longest edge length.
        """
        super(CustomResize, self).__init__()
        if isinstance(short_edge_length, int):
            short_edge_length = (short_edge_length, short_edge_length)
        self._init(locals())

    def get_transform(self, img):
        h, w = img.shape[:2]
        size = self.rng.randint(
            self.short_edge_length[0], self.short_edge_length[1] + 1)
        scale = size * 1.0 / min(h, w)
        if h < w:
            newh, neww = size, scale * w
        else:
            newh, neww = scale * h, size
        if max(newh, neww) > self.max_size:
            scale = self.max_size * 1.0 / max(newh, neww)
            newh = newh * scale
            neww = neww * scale
        neww = int(neww + 0.5)
        newh = int(newh + 0.5)
        return ResizeTransform(h, w, newh, neww, self.interp)


def box_to_point4(boxes):
    """
    Convert boxes to its corner points.

    Args:
        boxes: nx4

    Returns:
        (nx4)x2
    """
    b = boxes[:, [0, 1, 2, 3, 0, 3, 2, 1]]
    b = b.reshape((-1, 2))
    return b


def point4_to_box(points):
    """
    Args:
        points: (nx4)x2
    Returns:
        nx4 boxes (x1y1x2y2)
    """
    p = points.reshape((-1, 4, 2))
    minxy = p.min(axis=1)   # nx2
    maxxy = p.max(axis=1)   # nx2
    return np.concatenate((minxy, maxxy), axis=1)


def polygons_to_mask(polys, height, width):
    """
    Convert polygons to binary masks.

    Args:
        polys: a list of nx2 float array. Each array contains many (x, y) coordinates.

    Returns:
        a binary matrix of (height, width)
    """
    polys = [p.flatten().tolist() for p in polys]
    assert len(polys) > 0, "Polygons are empty!"

    import pycocotools.mask as cocomask
    rles = cocomask.frPyObjects(polys, height, width)
    rle = cocomask.merge(rles)
    return cocomask.decode(rle)


def clip_boxes(boxes, shape):
    """
    Args:
        boxes: (...)x4, float
        shape: h, w
    """
    orig_shape = boxes.shape
    boxes = boxes.reshape([-1, 4])
    h, w = shape
    boxes[:, [0, 1]] = np.maximum(boxes[:, [0, 1]], 0)
    boxes[:, 2] = np.minimum(boxes[:, 2], w)
    boxes[:, 3] = np.minimum(boxes[:, 3], h)
    return boxes.reshape(orig_shape)


def filter_boxes_inside_shape(boxes, shape):
    """
    Args:
        boxes: (nx4), float
        shape: (h, w)

    Returns:
        indices: (k, )
        selection: (kx4)
    """
    assert boxes.ndim == 2, boxes.shape
    assert len(shape) == 2, shape
    h, w = shape
    indices = np.where(
        (boxes[:, 0] >= 0) &
        (boxes[:, 1] >= 0) &
        (boxes[:, 2] <= w) &
        (boxes[:, 3] <= h))[0]
    return indices, boxes[indices, :]

def get_mask_single_inner(curr_damage_anchors_batch, house_bboxes, iou_thr):
    # iou_matrix = pairwise_iou(curr_damage_anchors_batch, house_bboxes)
    iou_matrix = pairwise_inner(curr_damage_anchors_batch, house_bboxes)
    iou_max = tf.math.reduce_max(input_tensor=iou_matrix, axis=1)
    mask = tf.greater(iou_max, tf.constant(iou_thr, dtype=tf.float32))
    return mask


def get_mask_single_iou(curr_damage_anchors_batch, house_bboxes, iou_thr):
    iou_matrix = pairwise_iou(curr_damage_anchors_batch, house_bboxes)
    iou_max = tf.math.reduce_max(input_tensor=iou_matrix, axis=1)
    mask = tf.greater(iou_max, tf.constant(iou_thr, dtype=tf.float32))
    return mask


def filter_anchors_inner(house_bboxes, damage_anchors, iou_thr):

    all_masks = []

    for i in range(len(damage_anchors)):
        curr_damage_anchors = damage_anchors[i]
        ori_shape = curr_damage_anchors.shape
        # print("ori_shape = ", ori_shape)
        curr_damage_anchors = np.reshape(curr_damage_anchors, (-1, 4))
        batch_size = 21 * 21 * 3
        all_mask = tf.convert_to_tensor(value=[], dtype=tf.float64)
        for i in range(curr_damage_anchors.shape[0] // batch_size):
            curr_mask = get_mask_single_inner(curr_damage_anchors[batch_size * i : batch_size * (i+1)], house_bboxes, iou_thr)
            if i == 0:
                all_mask = curr_mask
            else:
                all_mask = tf.concat([all_mask, curr_mask], axis=0)
        all_mask = tf.reshape(all_mask, ori_shape[:3])
        all_masks.append(all_mask)
    return all_masks



try:
    import pycocotools.mask as cocomask

    # Much faster than utils/np_box_ops
    def np_iou(A, B):
        def to_xywh(box):
            box = box.copy()
            box[:, 2] -= box[:, 0]
            box[:, 3] -= box[:, 1]
            return box

        ret = cocomask.iou(
            to_xywh(A), to_xywh(B),
            np.zeros((len(B),), dtype=np.bool))
        # can accelerate even more, if using float32
        return ret.astype('float32')

except ImportError:
    from utils.np_box_ops import iou as np_iou  # noqa
