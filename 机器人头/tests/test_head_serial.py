import math
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orin_voice.head_serial import DirectHeadController, EyePosePlanner


class EyePosePlannerTests(unittest.TestCase):
    def test_only_eye_axes_move_with_speed_bound_and_inversion(self):
        baseline=[90.]*21+[210.,180.,210.]
        planner=EyePosePlanner(baseline,[0.]*24,[180.]*21+[360.]*3,
            {'eye_speed_deg_s':8.,'eye_smoothing_alpha':1.})
        pose=planner.step(1.,1.,.04)
        self.assertAlmostEqual(pose[6],89.68)
        self.assertAlmostEqual(pose[9],89.68)
        self.assertEqual([value for i,value in enumerate(pose) if i not in (6,9)],
                         [value for i,value in enumerate(baseline) if i not in (6,9)])
        for _ in range(100):pose=planner.step(1.,1.,.04)
        self.assertAlmostEqual(pose[6],85.)
        self.assertAlmostEqual(pose[9],82.)
        for _ in range(100):pose=planner.step(0.,0.,.04)
        self.assertLess(planner.home_error(),.02)

    def test_live_limits_and_deadband(self):
        baseline=[90.]*24
        low=[80.]*24; high=[100.]*24
        low[9]=87.;high[6]=92.
        planner=EyePosePlanner(baseline,low,high,{'eye_smoothing_alpha':1.,'invert_vertical':False})
        self.assertEqual(planner.step(.01,-.01,.04),tuple(baseline))
        for _ in range(100):pose=planner.step(1.,1.,.04)
        self.assertEqual(pose[9],87.)
        self.assertEqual(pose[6],92.)

    def test_invalid_pose_or_coordinate_refused(self):
        with self.assertRaises(ValueError):EyePosePlanner([90.]*23,[0.]*24,[180.]*24)
        with self.assertRaises(ValueError):EyePosePlanner([190.]*24,[0.]*24,[180.]*24)
        planner=EyePosePlanner([90.]*24,[0.]*24,[180.]*24)
        for value in (math.nan, math.inf):
            with self.assertRaises(ValueError):planner.step(value,0.,.04)
        with self.assertRaises(ValueError):planner.step(0.,0.,0.)


class FakeTransport:
    def __init__(self):
        self.connected=True
        self.sent=[]
        self.requests=[]
        self.stale=False
        self.closed=False
    def latest_cmd1(self):
        return SimpleNamespace(host_monotonic_ns=time.monotonic_ns()-(1_000_000_000 if self.stale else 0))
    def request(self,command,payload,predicate):
        self.requests.append((command,payload))
        response=b'\x01\x01'
        assert predicate(response)
        return response
    def send(self,command,payload):self.sent.append((command,payload))
    def close(self):self.closed=True;self.connected=False


def wait_for(predicate,timeout=3):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.01)
    raise AssertionError('controller condition timed out')


class DirectHeadTests(unittest.TestCase):
    def fixture(self,directory,calibrated=True):
        baseline=tuple([90.]*21+[212.,178.,209.])
        calibration=SimpleNamespace(calibrated=calibrated,minimum_deg=(0.,)*24,
                                   maximum_deg=(180.,)*21+(360.,)*3)
        transport=FakeTransport()
        protocol=SimpleNamespace(
            discover_transport=lambda **kwargs:transport,
            read_live_calibration=lambda *args,**kwargs:calibration,
            decode_cmd1=lambda frame:SimpleNamespace(positions_deg=baseline),
            subcommand=lambda sub,data:bytes([sub])+data,
            decode_reply=lambda *args:None,
            command_sub=2,command_motor=0,
            pack_positions=lambda pose,planning_mode:tuple(pose))
        head=DirectHeadController({'eye_smoothing_alpha':1.,'target_timeout_s':.2})
        head._port=lambda:'/dev/fake-head'
        return head,transport,protocol,baseline

    def test_single_owner_live_pose_only_eyes_target_loss_and_close(self):
        with tempfile.TemporaryDirectory() as directory:
            head,transport,protocol,baseline=self.fixture(directory)
            with patch('orin_voice.head_serial.ROOT',Path(directory)), \
                 patch('orin_voice.head_serial._protocol',return_value=protocol), \
                 patch('orin_voice.head_serial.subprocess.run',return_value=SimpleNamespace(returncode=1)):
                head.start();head.start()
                wait_for(lambda:head.status()['motion_enabled'])
                time.sleep(.09)
                self.assertEqual(transport.sent,[], 'No target must not home or send all-axis Zero')
                self.assertEqual(transport.requests,[(2,b'\x01\x00')])
                head.set_target(1.,.5,visible=True)
                wait_for(lambda:len(transport.sent)>1)
                self.assertEqual(head.status()['port'],'/dev/fake-head')
                wait_for(lambda:head.status().get('target_fresh') is False)
                wait_for(lambda:abs(head.status()['eye_positions_deg']['s10']-90.)<.03)
                head.close()
                self.assertTrue(transport.closed)
                self.assertTrue(head.status()['return_to_start'])
                for command,pose in transport.sent:
                    self.assertEqual(command,0)
                    for i in range(24):
                        if i not in (6,9):self.assertEqual(pose[i],baseline[i])
                    self.assertLessEqual(abs(pose[9]-baseline[9]),8.)

    def test_calibration_false_does_not_exit_edit_or_send_position(self):
        with tempfile.TemporaryDirectory() as directory:
            head,transport,protocol,baseline=self.fixture(directory,calibrated=False)
            with patch('orin_voice.head_serial.ROOT',Path(directory)), \
                 patch('orin_voice.head_serial._protocol',return_value=protocol), \
                 patch('orin_voice.head_serial.subprocess.run',return_value=SimpleNamespace(returncode=1)):
                head.start()
                wait_for(lambda:head.status()['phase']=='error')
                head.close()
                self.assertEqual(transport.sent,[])
                self.assertEqual(transport.requests,[])
                self.assertIn('标定',head.status()['error'])
                self.assertTrue(transport.closed)

    def test_stale_status_stops_commands_and_does_not_blindly_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            head,transport,protocol,baseline=self.fixture(directory)
            with patch('orin_voice.head_serial.ROOT',Path(directory)), \
                 patch('orin_voice.head_serial._protocol',return_value=protocol), \
                 patch('orin_voice.head_serial.subprocess.run',return_value=SimpleNamespace(returncode=1)):
                head.start();wait_for(lambda:head.status()['motion_enabled'])
                head.set_target(1.,0.)
                wait_for(lambda:len(transport.sent)>0)
                transport.stale=True
                wait_for(lambda:not head.status()['running'])
                count=len(transport.sent)
                time.sleep(.1)
                self.assertEqual(count,len(transport.sent))
                self.assertFalse(head.status()['return_to_start'])
                self.assertIn('500ms',head.status()['error'])
                self.assertTrue(transport.closed)

    def test_busy_port_is_rejected_before_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            head,transport,protocol,baseline=self.fixture(directory)
            with patch('orin_voice.head_serial.ROOT',Path(directory)), \
                 patch('orin_voice.head_serial._protocol',return_value=protocol), \
                 patch('orin_voice.head_serial.subprocess.run',return_value=SimpleNamespace(returncode=0)):
                head.start();wait_for(lambda:head.status()['phase']=='error')
                self.assertIn('其他进程',head.status()['error'])
                self.assertEqual(transport.sent,[])
