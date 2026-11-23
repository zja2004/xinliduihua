"""Single-owner V2.5 eye tracking, preserving all non-eye axes.

Only the receive thread in the existing SerialTransport reads the serial port.
This module never starts device_io_node, homes the head or writes calibration.
"""
from __future__ import annotations

import copy
import fcntl
import importlib
import math
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from .settings import ROOT

EYE_INDICES = (6, 9)  # S7 vertical, S10 horizontal


def _protocol(config):
    source = Path(config.get('driver_source',
        '/home/nvidia/.local/share/orin-robot-head/ros_ws/src/device_io'))
    if not (source / 'device_io/protocol_v25.py').is_file():
        raise RuntimeError('缺少已配置的 V2.5 协议依赖，请先运行 scripts/setup_head_hardware.sh')
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    protocol = importlib.import_module('device_io.protocol_v25')
    serial = importlib.import_module('device_io.serial_transport')
    calibration = importlib.import_module('device_io.calibration')
    return SimpleNamespace(discover_transport=serial.discover_transport,
        read_live_calibration=calibration.read_live_calibration,
        decode_cmd1=protocol.decode_cmd1, pack_positions=protocol.pack_positions_v25,
        decode_reply=protocol.decode_subcommand_reply, subcommand=protocol.subcommand_request,
        command_sub=protocol.CMD_SUBCOMMAND, command_motor=protocol.CMD_MOTOR_CONTROL)


class EyePosePlanner:
    """Pure bounded planner; output differs from the baseline on S7/S10 only."""
    def __init__(self, baseline, minimum, maximum, config=None):
        config = config or {}
        if any(len(values) != 24 for values in (baseline, minimum, maximum)):
            raise ValueError('必须提供完整 24 轴实时位置和限位')
        if any(not all(math.isfinite(float(v)) for v in values) for values in (baseline, minimum, maximum)):
            raise ValueError('实时位置和限位必须是有限数值')
        if any(not low <= value <= high for value,low,high in zip(baseline,minimum,maximum)):
            raise ValueError('当前姿态不在实时限位内，不开始运动')
        self.baseline = tuple(float(value) for value in baseline)
        self.minimum, self.maximum = tuple(minimum), tuple(maximum)
        self.positions = list(self.baseline)
        self.horizontal_range = float(config.get('eye_horizontal_range_deg', 8.))
        self.vertical_range = float(config.get('eye_vertical_range_deg', 5.))
        self.speed = float(config.get('eye_speed_deg_s', 8.))
        self.alpha = float(config.get('eye_smoothing_alpha', .2))
        self.deadband = float(config.get('eye_deadband', .035))
        if not (0 < self.horizontal_range <= 15 and 0 < self.vertical_range <= 10 and
                0 < self.speed <= 20 and 0 < self.alpha <= 1 and 0 <= self.deadband <= .3):
            raise ValueError('眼球范围、速度或平滑参数无效')
        self.h_sign = -1 if config.get('invert_horizontal', True) else 1
        self.v_sign = -1 if config.get('invert_vertical', True) else 1

    def step(self, x, y, dt):
        if not all(math.isfinite(value) for value in (x,y,dt)) or dt <= 0:
            raise ValueError('注视坐标和时间间隔无效')
        x,y = max(-1.,min(1.,x)),max(-1.,min(1.,y))
        x = 0. if abs(x) < self.deadband else x
        y = 0. if abs(y) < self.deadband else y
        desired = (self.baseline[6] + y*self.vertical_range*self.v_sign,
                   self.baseline[9] + x*self.horizontal_range*self.h_sign)
        for index,target in zip(EYE_INDICES,desired):
            target = max(self.minimum[index], min(self.maximum[index], target))
            delta = (target-self.positions[index])*self.alpha
            limit = self.speed*min(dt,.1)  # a delayed worker cannot jump farther
            self.positions[index] += max(-limit,min(limit,delta))
        result = list(self.baseline)
        for index in EYE_INDICES:
            result[index] = round(self.positions[index],2)
        return tuple(result)

    def home_error(self):
        return max(abs(self.positions[index]-self.baseline[index]) for index in EYE_INDICES)


class DirectHeadController:
    def __init__(self, config=None):
        self.config = dict(config or {})
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.target = (0.,0.,False)
        self.target_at = -math.inf
        self.transport = None
        self._state = {'backend':'serial', 'running':False, 'connected':False,
            'calibration_ready':False, 'motion_enabled':False, 'error':None,
            'target':{'x':0.,'y':0.,'visible':False}, 'commands_sent':0,
            'eye_positions_deg':None, 'reported_eye_positions_deg':None,
            'return_to_start':None, 'port':None, 'phase':'stopped'}

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.status()
            self.stop_event = threading.Event()
            self.target = (0.,0.,False)
            self.target_at = -math.inf
            self._state.update(running=True, connected=False, calibration_ready=False,
                motion_enabled=False,error=None,phase='connecting',commands_sent=0,
                return_to_start=None)
            self.thread = threading.Thread(target=self._run,name='head-serial-eyes',daemon=True)
            self.thread.start()
            return self.status()

    def set_target(self, x_norm, y_norm, visible=True):
        x,y = float(x_norm),float(y_norm)
        if not all(math.isfinite(v) for v in (x,y)):
            raise ValueError('人脸目标坐标必须是有限数值')
        with self.lock:
            x,y = max(-1.,min(1.,x)),max(-1.,min(1.,y))
            self.target = (x,y,bool(visible))
            self.target_at = time.monotonic()
            self._state['target'] = {'x':x,'y':y,'visible':bool(visible)}

    def status(self):
        with self.lock:
            return copy.deepcopy(self._state)

    def _update(self, **values):
        with self.lock:
            self._state.update(values)

    def _port(self):
        configured = self.config.get('serial_port') or self.config.get('port')
        if configured and configured != 'auto':
            port = Path(configured)
            if not port.exists():
                raise RuntimeError('人头串口不存在：'+str(port))
            return str(port)
        ports = sorted(Path('/dev/serial/by-id').glob('*Head_Controller*'))
        if len(ports) != 1:
            raise RuntimeError('没有唯一可识别的 Head Controller 串口，请检查连接')
        return str(ports[0])

    def _fresh_state(self, protocol):
        frame = self.transport.latest_cmd1()
        if frame is None or (time.monotonic_ns()-frame.host_monotonic_ns)/1e9 > .5:
            raise RuntimeError('头板状态超过 500ms 未更新，已停止发送眼球动作')
        state = protocol.decode_cmd1(frame)
        if state is None:
            raise RuntimeError('头板实时状态格式无效')
        self._update(reported_eye_positions_deg={'s7':state.positions_deg[6], 's10':state.positions_deg[9]})
        return state

    def _send(self, protocol, pose):
        self._fresh_state(protocol)
        self.transport.send(protocol.command_motor, protocol.pack_positions(pose,planning_mode=1))
        with self.lock:
            self._state['commands_sent'] += 1
            self._state['eye_positions_deg'] = {'s7':pose[6], 's10':pose[9]}

    def _restore(self, protocol, planner, last_pose, period):
        if self._state['commands_sent'] == 0:
            self._update(return_to_start=True)
            return
        deadline = time.monotonic()+3
        self._update(phase='returning')
        while time.monotonic() < deadline and planner.home_error() > .02:
            pose = planner.step(0.,0.,period)
            if pose != last_pose:
                self._send(protocol,pose)
                last_pose = pose
            time.sleep(period)
        self._update(return_to_start=planner.home_error() <= .02)

    def _run(self):
        serial_lock = None
        planner = protocol = None
        last_pose = None
        hz = float(self.config.get('eye_control_hz',25.))
        if not 10 <= hz <= 50:
            self._update(running=False,error='眼球控制频率应在 10–50Hz 之间',phase='error')
            return
        period=1/hz
        timeout=float(self.config.get('target_timeout_s',1.))
        try:
            if not .2 <= timeout <= 5:
                raise ValueError('人脸目标超时应在 0.2–5 秒之间')
            runtime = ROOT / '.runtime/head-hardware'
            runtime.mkdir(parents=True,exist_ok=True)
            serial_lock=(runtime/'head-serial.lock').open('a')
            try:
                fcntl.flock(serial_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError('另一机器人进程已占用人头控制锁') from error
            port=self._port()
            opened=subprocess.run(['fuser',str(Path(port).resolve())],capture_output=True,timeout=3)
            if opened.returncode == 0:
                raise RuntimeError('人头串口正在被其他进程使用，请先结束旧动作程序')
            protocol=_protocol(self.config)
            self.transport=protocol.discover_transport(port=port,
                baud_candidates=(int(self.config.get('serial_baud',115200)),),response_timeout_s=1.5)
            calibration=protocol.read_live_calibration(self.transport,timeout_s=1.5)
            if not calibration.calibrated:
                raise RuntimeError('控制板标定标志无效，不启用眼球运动')
            response=self.transport.request(protocol.command_sub,protocol.subcommand(1,b'\x00'),
                predicate=lambda data:len(data)==2 and data[0]==1)
            protocol.decode_reply(response,1,0)
            state=self._fresh_state(protocol)
            planner=EyePosePlanner(state.positions_deg,calibration.minimum_deg,calibration.maximum_deg,self.config)
            last_pose=planner.baseline
            self._update(connected=True,calibration_ready=True,motion_enabled=True,phase='tracking',port=port,
                         eye_positions_deg={'s7':last_pose[6],'s10':last_pose[9]})
            previous=time.monotonic()
            while not self.stop_event.wait(period):
                now=time.monotonic()
                self._fresh_state(protocol)
                with self.lock:
                    x,y,visible=self.target
                    visible=visible and now-self.target_at <= timeout
                if not visible:
                    x=y=0.
                pose=planner.step(x,y,now-previous)
                previous=now
                if pose != last_pose:
                    self._send(protocol,pose)
                    last_pose=pose
                self._update(target_fresh=visible)
        except Exception as error:
            self._update(error=str(error),phase='error',motion_enabled=False)
        finally:
            if self.transport:
                if planner and protocol and self.transport.connected:
                    try:
                        self._restore(protocol,planner,last_pose,period)
                    except Exception as error:
                        self._update(return_to_start=False,return_error=str(error))
                try:self.transport.close()
                except Exception:pass
                self.transport=None
            if serial_lock:
                try:fcntl.flock(serial_lock,fcntl.LOCK_UN)
                finally:serial_lock.close()
            with self.lock:
                self._state.update(running=False,connected=False,calibration_ready=False,motion_enabled=False,
                    phase='error' if self._state['error'] else 'stopped')

    def close(self, timeout=5):
        self.stop_event.set()
        thread=self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        if thread and thread.is_alive():
            self._update(error='人头控制线程尚未退出，正在等待安全停止',motion_enabled=False)
        return self.status()
