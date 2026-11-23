"""YOLOv8n-Face ONNX inference and one highest-confidence face/head box.

Model: https://github.com/lindevs/yolov8-face (release 1.0.1).
This detects visible faces; it is not full-body or back-of-head detection.
"""
from pathlib import Path
import threading

import cv2
import numpy as np

from .settings import ROOT

DEFAULT_MODEL = 'models/vision/yolov8n-face-lindevs.onnx'
MODEL_SHA256 = '8d0bfb0c3383c5bd7a78dd24ef79a21e2aa456619b6ab5e53867092d1c7dc414'


def letterbox(frame, input_size=640):
    """Centre-pad an image and return the exact inverse transform."""
    if frame is None or frame.ndim != 3 or frame.shape[2] != 3 or not frame.size:
        raise ValueError('YOLO 输入必须是非空 BGR 三通道图像')
    height, width = frame.shape[:2]
    scale = min(input_size / width, input_size / height)
    resized_width, resized_height = round(width * scale), round(height * scale)
    left, top = (input_size - resized_width) // 2, (input_size - resized_height) // 2
    canvas = np.full((input_size, input_size, 3), 114, dtype=np.uint8)
    canvas[top:top + resized_height, left:left + resized_width] = cv2.resize(
        frame, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
    return canvas, scale, left, top


def decode_detections(output, frame_shape, scale, left, top,
                      confidence_threshold=.4, nms_iou=.45):
    """Decode the model's one-class output, then inverse-letterbox and NMS."""
    values = np.asarray(output)
    if values.ndim == 3 and values.shape[0] == 1:
        values = values[0]
    if values.ndim != 2:
        raise RuntimeError(f'YOLO 输出维度异常：{values.shape}')
    if values.shape[0] == 5:
        values = values.T
    elif values.shape[1] != 5:
        raise RuntimeError(f'期望单类 YOLO 人脸输出 (1,5,N)，实际为 {np.asarray(output).shape}')
    if scale <= 0:
        raise ValueError('Invalid letterbox scale')
    height, width = frame_shape[:2]
    boxes, scores = [], []
    for row in values:
        cx, cy, bw, bh, confidence = map(float, row)
        if not np.isfinite(row).all() or not confidence_threshold <= confidence <= 1 or bw <= 0 or bh <= 0:
            continue
        x1 = max(0., min(float(width), (cx - bw / 2 - left) / scale))
        y1 = max(0., min(float(height), (cy - bh / 2 - top) / scale))
        x2 = max(0., min(float(width), (cx + bw / 2 - left) / scale))
        y2 = max(0., min(float(height), (cy + bh / 2 - top) / scale))
        if x2 <= x1 or y2 <= y1:
            continue
        boxes.append([x1, y1, x2 - x1, y2 - y1])
        scores.append(confidence)
    if not boxes:
        return []
    indices = np.asarray(cv2.dnn.NMSBoxes(boxes, scores, confidence_threshold, nms_iou)).reshape(-1)
    detections = []
    for index in indices:
        box = boxes[int(index)]
        x1, y1 = max(0, round(box[0])), max(0, round(box[1]))
        x2, y2 = min(width, round(box[0] + box[2])), min(height, round(box[1] + box[3]))
        if x2 <= x1 or y2 <= y1:
            continue
        detections.append({'bbox': [x1, y1, x2 - x1, y2 - y1],
                           'confidence': float(scores[int(index)]), 'class': 'face'})
    return sorted(detections, key=lambda item: item['confidence'], reverse=True)


def select_best(detections, confidence_threshold=.4):
    """Select exactly one valid maximum-confidence detection, never largest area."""
    valid = [item for item in detections if confidence_threshold <= item['confidence'] <= 1
             and len(item['bbox']) == 4 and item['bbox'][2] > 0 and item['bbox'][3] > 0]
    return max(valid, key=lambda item: item['confidence']) if valid else None


def annotate(frame, detection=None):
    """Draw one box on a copy. No valid target means an unchanged copy."""
    image = frame.copy()
    if isinstance(detection, (tuple, list)):
        detection = select_best(detection)
    if detection is None:
        return image
    x, y, width, height = map(int, detection['bbox'])
    color = (95, 220, 120)
    cv2.rectangle(image, (x, y), (x + width, y + height), color, 2, cv2.LINE_AA)
    text = f"Head {detection['confidence']:.2f}"
    text_size, baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, .55, 1)
    label_y = max(text_size[1] + 6, y)
    cv2.rectangle(image, (x, label_y - text_size[1] - 6),
                  (min(image.shape[1] - 1, x + text_size[0] + 8), label_y + baseline), color, -1)
    cv2.putText(image, text, (x + 4, label_y - 3), cv2.FONT_HERSHEY_SIMPLEX,
                .55, (15, 35, 20), 1, cv2.LINE_AA)
    return image


class YoloHeadDetector:
    def __init__(self, config=None, *, network=None):
        config = config or {}
        config = config.get('yolo', config)
        self.confidence_threshold = float(config.get('confidence_threshold', .4))
        self.nms_iou = float(config.get('nms_iou', .45))
        self.input_size = int(config.get('input_size', 640))
        if not 0 < self.confidence_threshold < 1 or not 0 < self.nms_iou < 1:
            raise ValueError('YOLO confidence_threshold 和 nms_iou 必须在零到一之间')
        if self.input_size != 640:
            raise ValueError('当前固定 ONNX 模型要求 input_size=640')
        model_path = Path(config.get('model_path', DEFAULT_MODEL))
        self.model_path = model_path if model_path.is_absolute() else ROOT / model_path
        if network is None:
            if not self.model_path.is_file():
                raise FileNotFoundError('YOLO 模型未下载，请运行 .venv/bin/python scripts/download_yolo_head.py')
            self.network = cv2.dnn.readNetFromONNX(str(self.model_path))
            self.network.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self.network.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        else:
            self.network = network
        self.lock = threading.Lock()

    def detect(self, frame):
        image, scale, left, top = letterbox(frame, self.input_size)
        blob = cv2.dnn.blobFromImage(image, scalefactor=1/255., size=(self.input_size, self.input_size),
                                    swapRB=True, crop=False)
        with self.lock:
            self.network.setInput(blob)
            output = self.network.forward()
        return decode_detections(output, frame.shape, scale, left, top,
                                 self.confidence_threshold, self.nms_iou)

    def best(self, frame):
        return select_best(self.detect(frame), self.confidence_threshold)
