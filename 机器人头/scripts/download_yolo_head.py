#!/usr/bin/env python3
"""Fetch the publisher's ONNX asset, verify SHA-256, then install atomically."""
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://github.com/lindevs/yolov8-face/releases/download/1.0.1/yolov8n-face-lindevs.onnx'
SHA256 = '8d0bfb0c3383c5bd7a78dd24ef79a21e2aa456619b6ab5e53867092d1c7dc414'
TARGET = ROOT / 'models/vision/yolov8n-face-lindevs.onnx'


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            result.update(block)
    return result.hexdigest()


def receipt():
    TARGET.with_suffix('.receipt.json').write_text(json.dumps({
        'source': URL, 'sha256': SHA256, 'bytes': TARGET.stat().st_size,
        'model': 'lindevs YOLOv8n-Face 1.0.1', 'runtime': 'OpenCV DNN CPU',
        'input': [1, 3, 640, 640], 'output': [1, 5, 8400],
        'scope': 'visible face detection, not full-body or back-of-head detection'
    }, ensure_ascii=False, indent=2)+'\n')


def main():
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    if TARGET.is_file() and digest(TARGET) == SHA256:
        receipt()
        print('YOLO 模型已下载且 SHA-256 正确：' + str(TARGET))
        return
    temporary = TARGET.with_suffix('.onnx.part')
    request = urllib.request.Request(URL, headers={'User-Agent':'orin-robot-head/1.0'})
    with urllib.request.urlopen(request, timeout=30) as source, temporary.open('wb') as output:
        while block := source.read(1024*1024):
            output.write(block)
    actual = digest(temporary)
    if actual != SHA256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f'YOLO 模型 SHA-256 不匹配：{actual}')
    temporary.replace(TARGET)
    receipt()
    print('YOLO 模型下载并校验完成：' + str(TARGET))


if __name__ == '__main__':
    main()
