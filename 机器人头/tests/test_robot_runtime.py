"""Deterministic coordinator tests without camera, microphone, motors or LLM."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orin_voice.presence import PresenceGate
from orin_voice.robot_runtime import RobotRuntime


class FakeListener:
    def __init__(self, config, on_wake, *args, **kwargs):
        self.on_wake = on_wake
        self.alive = False
        self.closed = True
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1
        self.closed = False
        self.alive = True

    def stop(self):
        self.stopped += 1
        self.closed = True
        self.alive = False

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return self.alive

    def status(self):
        return {'phase': 'listening' if self.alive else 'stopped'}


class FakeService:
    def __init__(self):
        self.active = None
        self.worker = None
        self.calls = []
        self.on_start = None

    def start(self, mode, **kwargs):
        runtime = getattr(self, 'robot', None)
        if runtime:
            runtime.pause_voice()
        try:
            if self.active and self.active.read()['status'] == 'in_progress':
                raise RuntimeError('already active')
            if self.on_start:
                self.on_start()
            self.calls.append((mode, kwargs))
            self.active = SimpleNamespace(read=lambda: {'status': 'in_progress'})
            return {'session_id': 'test-session', 'status': 'in_progress'}
        finally:
            if runtime:
                runtime.release_voice()



class RobotRuntimeTests(unittest.TestCase):
    def test_stopping_preview_keeps_camera_owned_by_session_recording(self):
        self.service.active = SimpleNamespace(video_recorder=SimpleNamespace(
            status=lambda: {'running': True}))
        with patch.object(self.runtime.camera, 'stop') as stopped:
            result = self.runtime.camera_action('stop')
            stopped.assert_not_called()
            self.assertTrue(result['recording_in_use'])

    def setUp(self):
        self.dialogue = MagicMock()
        self.dialogue.status.return_value = {'phase': 'ready'}
        self.dialogue_patch = patch('orin_voice.robot_runtime.LocalDialogue', return_value=self.dialogue)
        self.dialogue_patch.start()
        self.addCleanup(self.dialogue_patch.stop)
        self.service = FakeService()
        self.config = {'session_mode': 'voice', 'wake_cooldown_seconds': 0,
                       'vision': {'enabled': False}, 'voice_wake': {'enabled': True},
                       'chat': {'enabled': False}}
        self.runtime = RobotRuntime(self.service, config=self.config)
        self.service.robot = self.runtime
        self.addCleanup(self.cleanup_runtime)

    def cleanup_runtime(self):
        self.runtime.stop_event.set()
        self.runtime.enabled = False
        if self.runtime.voice:
            self.runtime.voice.stop()
        if self.runtime.monitor:
            self.runtime.monitor.join(2)

    def run_watch_iterations(self, iterations=1):
        class LoopStop:
            def __init__(self): self.calls = 0
            def wait(inner, seconds):
                inner.calls += 1
                return inner.calls > iterations
            def set(inner): inner.calls = iterations + 1
            def clear(inner): inner.calls = 0
            def is_set(inner): return inner.calls > iterations
        original = self.runtime.stop_event
        self.runtime.stop_event = LoopStop()
        try:
            self.runtime._watch()
        finally:
            self.runtime.stop_event = original

    def test_visual_gate_and_busy_encounter_do_not_start_duplicate_sessions(self):
        self.runtime.enabled = True
        gate = PresenceGate(stable_frames=2, missing_reset_seconds=2, cooldown_seconds=0)
        for tick in range(100):
            if gate.update(True, tick * .1):
                self.runtime._event({'trigger': 'vision', 'monotonic_time': tick * .1})
        with patch('orin_voice.robot_runtime.VoiceWakeListener', FakeListener):
            self.run_watch_iterations(8)
            self.assertEqual(len(self.service.calls), 1)
            # Even a stale or manually duplicated sensor callback must not replace active encounter.
            self.runtime._event({'trigger': 'vision'})
            self.run_watch_iterations(2)
            self.assertEqual(len(self.service.calls), 1)
            self.assertIsNone(self.runtime.voice)

    def test_voice_capture_closed_and_joined_before_session_start(self):
        listener = FakeListener(self.config, self.runtime._event)
        listener.start()
        self.runtime.voice = listener
        observed = []
        self.service.on_start = lambda: observed.append(listener.closed and not listener.is_alive())
        self.runtime.wake({'trigger': 'voice'})
        self.assertEqual(observed, [True])
        self.assertEqual(listener.stopped, 1)
        self.assertIsNone(self.runtime.voice)
        self.assertEqual(self.service.calls[0][1]['wake_event']['trigger'], 'voice')

    def test_stuck_idle_capture_prevents_opening_interview_capture(self):
        listener = FakeListener(self.config, self.runtime._event)
        listener.start()
        listener.stop = lambda: None
        self.runtime.voice = listener
        with self.assertRaisesRegex(RuntimeError, '尚未关闭'):
            self.runtime.wake({'trigger': 'vision'})
        self.assertEqual(self.service.calls, [])

    def test_active_session_never_opens_idle_microphone(self):
        self.runtime.enabled = True
        self.service.active = SimpleNamespace(read=lambda: {'status': 'in_progress'})
        with patch('orin_voice.robot_runtime.VoiceWakeListener') as constructor:
            self.run_watch_iterations(5)
        constructor.assert_not_called()

    def test_live_worker_also_blocks_idle_microphone_even_with_stopped_snapshot(self):
        self.runtime.enabled = True
        self.service.active = SimpleNamespace(read=lambda: {'status': 'stopped'})
        self.service.worker = SimpleNamespace(is_alive=lambda: True)
        with patch('orin_voice.robot_runtime.VoiceWakeListener') as constructor:
            self.run_watch_iterations(5)
        constructor.assert_not_called()

    def test_disarm_clears_stale_sensor_events_before_rearm(self):
        self.runtime.enabled = True
        self.runtime._event({'trigger': 'vision', 'monotonic_time': 1})
        self.runtime.disarm()
        self.assertTrue(self.runtime.triggers.empty(), 'A queued visual event survived disarm')
        self.runtime.config['voice_wake']['enabled'] = False
        self.runtime.arm()
        self.assertTrue(self.runtime.enabled)
        self.assertEqual(self.service.calls, [])
        self.runtime.disarm()
        self.assertFalse(self.runtime.enabled)

    def test_arm_disarm_can_restart_sensors_without_reusing_live_threads(self):
        self.runtime.config['vision']['enabled'] = True
        self.runtime.config['voice_wake']['enabled'] = False
        created = []
        def vision(*args, **kwargs):
            sensor = FakeListener(*args, **kwargs)
            created.append(sensor)
            return sensor
        with patch('orin_voice.robot_runtime.VisionPresence', side_effect=vision):
            self.runtime.arm()
            first_monitor = self.runtime.monitor
            self.runtime.disarm()
            self.assertFalse(first_monitor.is_alive())
            self.runtime.arm()
            self.assertIsNot(first_monitor, self.runtime.monitor)
            self.assertEqual(len(created), 2)
            self.assertTrue(created[0].closed)
            self.assertTrue(created[1].alive)
            self.runtime.disarm()

    def test_manual_wake_cannot_race_standby_listener_construction(self):
        self.runtime.enabled = True
        constructing = threading.Event()
        release = threading.Event()
        opened = threading.Event()
        created = []
        errors = []
        def listener(*args, **kwargs):
            sensor = FakeListener(*args, **kwargs)
            original_start = sensor.start
            def opened_start():
                original_start()
                opened.set()
            sensor.start = opened_start
            created.append(sensor)
            constructing.set()
            if not release.wait(2):
                raise RuntimeError('test construction timeout')
            return sensor
        def manual():
            try:
                self.runtime.wake({'trigger': 'manual'})
            except Exception as error:
                errors.append(error)
        with patch('orin_voice.robot_runtime.VoiceWakeListener', side_effect=listener):
            watcher = threading.Thread(target=self.runtime._watch, daemon=True)
            self.runtime.monitor = watcher
            watcher.start()
            self.assertTrue(constructing.wait(1))
            starter = threading.Thread(target=manual, daemon=True)
            starter.start()
            # If lifecycle calls are serialized, wake waits here for construction.
            # If not, service.start completes while the watcher is still opening input.
            time.sleep(.03)
            release.set()
            starter.join(2)
            self.assertFalse(starter.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(opened.wait(1))
            self.assertEqual(len(self.service.calls), 1)
            self.assertFalse(any(item.alive for item in created),
                             'Standby microphone started after active session took ownership')
            self.runtime.stop_event.set()
            watcher.join(2)

    def test_disarm_cancels_automatic_event_already_dequeued_by_monitor(self):
        self.runtime.enabled = True
        self.runtime._event({'trigger': 'vision'})
        checking = threading.Event()
        release = threading.Event()
        real_busy = self.runtime._busy
        count = [0]
        def blocked_busy():
            count[0] += 1
            if count[0] == 1:
                checking.set()
                self.assertTrue(release.wait(2))
            return real_busy()
        with patch.object(self.runtime, '_busy', side_effect=blocked_busy):
            watcher = threading.Thread(target=self.runtime._watch, daemon=True)
            watcher.start()
            self.assertTrue(checking.wait(1))
            # Do not attach this thread as runtime.monitor: disarm otherwise joins
            # it while our test deliberately holds it at the decision boundary.
            self.runtime.disarm()
            release.set()
            watcher.join(2)
            self.assertFalse(watcher.is_alive())
            self.assertEqual(self.service.calls, [],
                             'An already-dequeued sensor event started a session after disarm')

    def test_old_generation_callback_is_ignored_after_rearm(self):
        self.runtime.config['vision']['enabled'] = True
        self.runtime.config['voice_wake']['enabled'] = False
        callbacks = []
        def vision(config, on_wake, on_target, camera_hub=None):
            callbacks.append(on_wake)
            return FakeListener(config, on_wake)
        with patch('orin_voice.robot_runtime.VisionPresence', side_effect=vision):
            self.runtime.arm()
            self.runtime.disarm()
            self.runtime.arm()
            callbacks[0]({'trigger':'vision'})
            self.assertTrue(self.runtime.triggers.empty())
            callbacks[1]({'trigger':'vision'})
            self.assertEqual(self.runtime.triggers.qsize(),1)
            self.runtime.disarm()

    def test_real_service_handover_closes_idle_audio_and_releases_reservation(self):
        import tempfile
        from orin_voice.service import InterviewService, Conflict
        observed = []
        release = threading.Event()
        listener = FakeListener(self.config, self.runtime._event)
        listener.start()
        def voice_runner(control):
            observed.append(listener.closed)
            release.wait(2)
        with tempfile.TemporaryDirectory() as output:
            service = InterviewService(output=output, voice_runner=voice_runner)
            runtime = RobotRuntime(service, config=self.config)
            service.robot = runtime
            runtime.voice = listener
            try:
                result = runtime.wake({'trigger': 'voice'})
                self.assertEqual(result['stage'], 'intake')
                self.assertFalse(runtime.voice_reserved)
                self.assertTrue(listener.closed)
                with self.assertRaises(Conflict):
                    service.start('voice')
                self.assertFalse(runtime.voice_reserved)
            finally:
                release.set()
                if service.worker:
                    service.worker.join(3)
            self.assertEqual(observed, [True])
