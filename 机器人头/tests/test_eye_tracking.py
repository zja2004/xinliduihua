import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
from orin_voice.eye_tracking import EyeTracker


class EyeTrackingTests(unittest.TestCase):
    def test_uses_displayed_selected_box_and_releases_hardware_on_stop(self):
        image=np.zeros((480,640,3),dtype=np.uint8)
        frames=[SimpleNamespace(sequence=1,frame=image,target={'bbox':[400,80,160,80],'confidence':.94}),
                SimpleNamespace(sequence=2,frame=image,target=None)]
        camera=Mock();camera.detector_kind='yolo'
        def wait(after_sequence,timeout):
            if frames:return frames.pop(0)
            time.sleep(.01);return None
        camera.wait_frame.side_effect=wait
        camera.status.return_value={'phase':'streaming'}
        hardware=Mock();hardware.status.return_value={'motion_enabled':True,'error':None}
        worker=EyeTracker(camera,hardware)
        worker.start()
        try:
            deadline=time.monotonic()+2
            while worker.status()['frames']<2 and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(worker.status()['frames'],2)
            first=hardware.track_target.call_args_list[0]
            self.assertAlmostEqual(first.args[0],.5)
            self.assertAlmostEqual(first.args[1],-.5)
            self.assertTrue(first.kwargs['visible'])
            self.assertEqual(hardware.track_target.call_args_list[1].kwargs['visible'],False)
        finally:worker.stop()
        self.assertFalse(worker.status()['enabled'])
        hardware.close.assert_called_once()
        camera.stop.assert_not_called()

    def test_device_failure_stops_tracking_instead_of_claiming_running(self):
        camera=Mock();camera.detector_kind='yolo'
        hardware=Mock();hardware.status.return_value={'error':'测试串口断连'}
        worker=EyeTracker(camera,hardware);worker.start()
        deadline=time.monotonic()+2
        while worker.is_alive() and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(worker.status()['phase'],'error')
        self.assertIn('串口断连',worker.status()['error'])
        self.assertFalse(worker.status()['enabled'])
        hardware.close.assert_called_once()
