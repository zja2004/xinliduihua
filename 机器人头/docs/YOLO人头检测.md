# YOLO 单目标人头／人脸检测

## 模型与范围

采用发布者 [Lindevs YOLOv8-Face](https://github.com/lindevs/yolov8-face) 的
YOLOv8n-Face ONNX，固定版本 `1.0.1`：

- [模型下载](https://github.com/lindevs/yolov8-face/releases/download/1.0.1/yolov8n-face-lindevs.onnx)
- [发布者 OpenCV DNN 示例](https://github.com/lindevs/yolov8-face/blob/master/examples/opencv-dnn-python/main.py)
- SHA-256：`8d0bfb0c3383c5bd7a78dd24ef79a21e2aa456619b6ab5e53867092d1c7dc414`
- 本地路径：`models/vision/yolov8n-face-lindevs.onnx`
- 固定输入 `1×3×640×640`，实际验证输出 `1×5×8400`，每候选为中心坐标、宽、高和单类置信度。

这个模型针对 **可见人脸区域**，适合给面对机器人的人的脸部加框；不做身份识别。
它不保证检测背对镜头的后脑，也不等同于包含全部头发、耳朵的完整人体头部分割。
没有通过阈值的目标时不生成假框；不采用全身 person 框裁剪来冒充人头。

## 选择规则

1. 将真实摄像头帧等比缩放、居中填充至 640×640。
2. ONNX 推理、按置信度过滤，然后反算至原图坐标并限制在图像范围内。
3. 使用 NMS 去除重叠候选。
4. 从当前帧的有效候选中，**只选择置信度最高的一个**；不是按框面积，也不因为上一帧曾锁定另一人而保留低分目标。
5. 同帧有多人时最多显示一个框；没有可靠目标就不画框。

默认置信度阈值 `0.4`，NMS IoU 阈值 `0.45`。置信度是模型检测分数，不是身份真实性概率。

## 环境与复现

复用已安装的 `opencv-python-headless==4.11.0.86` 和 NumPy，使用 OpenCV DNN CPU 推理；
本次没有安装 PyTorch、Ultralytics 或另一套 CUDA 运行时。

```bash
cd /home/nvidia/Desktop/机器人头
.venv/bin/python scripts/download_yolo_head.py
```

下载脚本校验发布者公布的 SHA-256，校验通过后才将临时文件替换为正式模型。
模型旁保存 `.receipt.json`，含下载 URL、校验和与模型接口。

模块调用：

```python
from orin_voice.yolo_head import YoloHeadDetector, select_best, annotate

detector = YoloHeadDetector({
    'model_path': 'models/vision/yolov8n-face-lindevs.onnx',
    'confidence_threshold': 0.4,
})
detections = detector.detect(frame)  # [{'bbox': [x, y, w, h], 'confidence': float, 'class': 'face'}]
best = select_best(detections)
preview = annotate(frame, best)  # 复制原帧后绘制，最多一个框
```

`input_size` 必须是 640；错误模型输出形状、缺失模型和推理错误明确抛出异常，不会伪造检测结果。
图像解码、实时叠框与网页状态由上层共享摄像头服务管理。

## 本次验证

2026-10-28，在 Jetson Orin NX 16GB 上，以已保存的 640×480 摄像头帧进行离线真实模型推理：

- 检出框 `[385, 290, 98, 86]`，置信度约 `0.793`。
- 首次推理约 264ms，接下来的单次推理约 190–198ms。
- 这些是这台机器的一张图片局部测量，不是摄像头端到端帧率或检测准确率统计。
- 标注样例保存在 `.runtime/camera-check/yolo-offline.jpg`。
- 6 项专项测试覆盖最高置信度选择、阈值与空结果、等比填充逆坐标、NMS、非法输出与不修改共享帧。

## 实时网页接入

打开 `http://127.0.0.1:18088/camera`。共享摄像头每帧旋转方向校正后进行一次 YOLO 推理，
经阈值和 NMS 后，按 `confidence` 取最大者，仅画一个绿色框和分数。
没有有效目标时当帧不画框，也不会沿用上一帧的人头框。
摄像头原始共享帧不带绘制痕迹；JPEG 预览中包含检测框。

`GET /api/camera/status` 增加：

- `detector: yolo`、`detector_error`。
- `selected_head: {bbox:[x,y,w,h],confidence}` 或 null。
- `detection_count`：阈值与NMS后候选数量（画面仍只画一个）。
- `inference_ms`：本帧推理耗时。

当人物唤醒启用时，`VisionPresence` 直接使用共享帧的同一个 YOLO 最高分目标，
不再另跑 Haar 选另一张脸；仅打开预览不会启动唤醒、麦克风或电机。
