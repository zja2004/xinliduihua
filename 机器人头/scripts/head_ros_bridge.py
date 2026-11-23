"""ROS-only helper: imported planners -> calibrated offset topics, no serial I/O.

PCM is a timing/viseme reference copied from actual local playback. It is never
published to /audio/playback, so this helper cannot duplicate the spoken sound.
"""
import base64
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
from robot_interfaces.msg import DeviceCalibration, DeviceStatus, EyeTarget, MouthTarget
from speak_mouth.mouth_motion import MouthMotion
from speak_mouth.viseme_planner import VisemePlanner
from speak_mouth.utterance_buffer import AudioChunk

ROOT = Path(__file__).resolve().parents[1]


class HeadBridge(Node):
    def __init__(self, config):
        super().__init__('orin_interview_head_bridge')
        self.config = config
        self.commands = queue.Queue(maxsize=128)
        self.connected = self.calibrated = False
        self.last_device_status = 0.
        self.error = None
        self.calibration = None
        self.uid = None
        self.chunk = None
        self.chunk_started = 0.
        self.last_target = 0.
        self.target = (0., 0.)
        self.current = [0., 0.]
        self.sequence = 0
        self.planner = VisemePlanner()
        self.motion = MouthMotion(Path(os.environ.get('ORIN_HEAD_ROS_WS', '/home/nvidia/.local/share/orin-robot-head/ros_ws')) / 'src/speak_mouth/config/pc_head_mandarin_mouth_demo.json')
        qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                         reliability=ReliabilityPolicy.RELIABLE)
        self.create_subscription(DeviceStatus, '/device/status', self.on_status, qos)
        self.create_subscription(DeviceCalibration, '/device/calibration', self.on_calibration, qos)
        self.eye_pub = self.create_publisher(EyeTarget, '/eye/target', 1)
        self.mouth_pub = self.create_publisher(MouthTarget, '/mouth/target', 1)
        self.create_timer(.01, self.tick)
        self.create_timer(1., self.report)
        self.stopping = threading.Event()
        threading.Thread(target=self.read_commands, daemon=True).start()

    def on_status(self, message):
        self.last_device_status = time.monotonic()
        self.connected = bool(message.connected)
        self.calibrated = bool(message.calibration_ready)
        self.error = message.error or None
        if not self.connected:
            self.calibration = None

    def on_calibration(self, message):
        values = list(message.zero_deg) + list(message.minimum_deg) + list(message.maximum_deg)
        if len(values) != 72 or not all(math.isfinite(v) for v in values):
            self.error = '头板标定数组不完整或含非有限值'
            return
        if any(not a <= b <= c for a, b, c in zip(message.minimum_deg,message.zero_deg,message.maximum_deg)):
            self.error = '头板 Zero 不在实时 Min/Max 内'
            return
        self.calibration = message
        self.motion.set_live_calibration(message.zero_deg, message.minimum_deg,
                                         message.maximum_deg, message.initial_positions_deg)

    def read_commands(self):
        for line in sys.stdin:
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    self.commands.put(data, timeout=.2)
            except (ValueError, queue.Full):
                continue
        self.stopping.set()

    def apply(self, command, now):
        event = command.get('event')
        if event == 'target':
            x,y = float(command['x']),float(command['y'])
            if not all(math.isfinite(v) and abs(v) <= 1 for v in (x,y)):
                return
            self.target = (x,y) if command.get('visible') else (0.,0.)
            self.last_target = now
        elif event == 'begin':
            self.uid = str(command['utterance_id'])
            self.planner = VisemePlanner()
            self.planner.set_text(str(command['text']))
            self.chunk = None
        elif event == 'cancel' and (not command.get('utterance_id') or command.get('utterance_id') == self.uid):
            self.chunk = None
            self.uid = None
        elif event == 'audio' and str(command['utterance_id']) == self.uid:
            pcm = base64.b64decode(command['pcm_f32le'], validate=True)
            rate = int(command['sample_rate'])
            if rate <= 0 or len(pcm) % 4 or len(pcm) > rate*4:
                return
            self.chunk = AudioChunk(None,int(command['sequence']),rate,1,1,
                                    round(len(pcm)/4/rate*1000),len(pcm)//4,pcm,self.uid)
            self.chunk_started = now

    def tick(self):
        now = time.monotonic()
        for _ in range(128):
            try:self.apply(self.commands.get_nowait(),now)
            except queue.Empty:break
            except (ValueError, KeyError, TypeError) as error:self.error=str(error)
        if self.stopping.is_set():
            rclpy.shutdown()
            return
        if self.last_device_status and now - self.last_device_status > 2:
            self.connected = self.calibrated = False
            self.calibration = None
            self.error = '头板状态已超过两秒未更新，停止发送动作'
        if not (self.config.get('calibration_confirmed') and self.connected and self.calibrated and self.calibration):
            return
        x,y = self.target if now-self.last_target <= self.config['target_timeout_s'] else (0.,0.)
        # EyeTarget order is S7/S8/S9/S10; eyelids remain at their live zero.
        desired = [y*self.config['eye_vertical_range_deg']*(-1 if self.config['invert_vertical'] else 1),
                   x*self.config['eye_horizontal_range_deg']*(-1 if self.config['invert_horizontal'] else 1)]
        for index,(axis,target) in enumerate(zip((6,9),desired)):
            low=self.calibration.minimum_deg[axis]-self.calibration.zero_deg[axis]
            high=self.calibration.maximum_deg[axis]-self.calibration.zero_deg[axis]
            target=max(low,min(high,target))
            delta=(target-self.current[index])*self.config['eye_smoothing_alpha']
            step=self.config['eye_max_step_deg']
            self.current[index]+=max(-step,min(step,delta))
        eye = EyeTarget()
        eye.stamp=self.get_clock().now().to_msg()
        eye.sequence=self.sequence
        eye.offsets_deg=[self.current[0],0.,0.,self.current[1]]
        self.eye_pub.publish(eye)
        if self.chunk and now-self.chunk_started < self.chunk.duration_s+.03:
            label=self.planner.label_for_chunk(self.chunk,max(0.,now-self.chunk_started),now)
        else:label='REST'
        self.motion.set_target(label)
        step = self.motion.step()
        mouth=MouthTarget()
        mouth.stamp=self.get_clock().now().to_msg()
        mouth.utterance_id=self.uid or ''
        mouth.offsets_deg=list(step.offsets_deg)
        self.mouth_pub.publish(mouth)
        self.sequence+=1

    def report(self):
        print(json.dumps(dict(connected=self.connected,calibration_ready=self.calibrated,
                              motion_enabled=bool(self.connected and self.calibrated and self.calibration),
                              error=self.error),ensure_ascii=False),flush=True)


def main():
    config=json.loads(os.environ['ORIN_HEAD_CONFIG'])
    rclpy.init()
    node=HeadBridge(config)
    try:rclpy.spin(node)
    finally:
        node.destroy_node()
        if rclpy.ok():rclpy.shutdown()


if __name__=='__main__':main()
