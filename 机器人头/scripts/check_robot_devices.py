#!/usr/bin/env python3
"""Read-only robot input/hardware diagnostics. Never opens a serial actuator."""
import glob
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def command(args):
    try:
        process=subprocess.run(args,capture_output=True,text=True,timeout=8)
        return {'ok':process.returncode==0,'output':process.stdout.strip(),
                'error':process.stderr.strip() if process.returncode else None}
    except (OSError,subprocess.TimeoutExpired) as error:
        return {'ok':False,'error':str(error)}

report={
    'root':str(ROOT), 'camera_devices':glob.glob('/dev/video*'),
    'serial_devices':glob.glob('/dev/ttyACM*')+glob.glob('/dev/ttyUSB*'),
    'usb':command(['lsusb']),
    'audio_sources':command(['pactl','list','short','sources']),
    'audio_sinks':command(['pactl','list','short','sinks']),
    'python_dependencies':{name:bool(importlib.util.find_spec(name)) for name in
                           ['cv2','numpy','sounddevice','sherpa_onnx','serial']},
}
if report['python_dependencies']['cv2']:
    import cv2
    cascade=Path(cv2.data.haarcascades)/'haarcascade_frontalface_default.xml'
    report['face_detector']={'opencv_version':cv2.__version__, 'cascade':str(cascade),
                             'exists':cascade.is_file()}
print(json.dumps(report,ensure_ascii=False,indent=2))
