import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
import soundfile as sf

from orin_voice.interview import Interview
from orin_voice.session_export import EventJournal, FullSessionRecorder, export_session
from orin_voice.settings import ROOT


class JournalTests(unittest.TestCase):
    def test_event_updates_replay_and_interrupted_playback_is_not_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = EventJournal(directory)
            event = journal.record('assistant', '第一题。', question_id='Q01')
            journal.update(event, delivery='playing', played_duration_s=.2)
            journal.update(event, delivery='interrupted', played_duration_s=.32)
            user = journal.record('user', '再说一遍', question_id='Q01')
            reloaded = EventJournal(directory).snapshot()
            self.assertEqual(len(reloaded), 2)
            self.assertEqual(reloaded[0]['delivery'], 'interrupted')
            self.assertEqual(reloaded[0]['played_duration_s'], .32)
            self.assertEqual(reloaded[1]['id'], user)
            self.assertGreaterEqual(reloaded[1]['session_ms'], reloaded[0]['session_ms'])
            journal.save_transcript()
            self.assertEqual(json.loads((Path(directory) / 'transcript.json').read_text())['events'], reloaded)

    def test_partial_final_record_does_not_break_future_events(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = EventJournal(directory)
            journal.record('user', '二分')
            with journal.path.open('ab') as file:
                file.write(b'{"operation":"cre')
            recovered = EventJournal(directory)
            recovered.record('assistant', '已记录。')
            self.assertEqual([e['text'] for e in EventJournal(directory).snapshot()], ['二分', '已记录。'])


class RecorderTests(unittest.TestCase):
    def test_tracks_are_continuous_input_and_only_submitted_output_with_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = FullSessionRecorder(directory, origin=100)
            recorder.enqueue('microphone', np.ones(1600, dtype=np.float32) * .1, 16000, monotonic_time=100)
            recorder.enqueue('microphone', np.zeros(1600, dtype=np.float32), 16000, monotonic_time=100.1)
            recorder.enqueue('assistant', np.ones(480, dtype=np.float32) * .2, 24000,
                             event_id='greeting', monotonic_time=100.03)
            recorder.enqueue('assistant', np.ones(480, dtype=np.float32) * .2, 24000,
                             event_id='greeting', monotonic_time=100.05)
            manifest = recorder.close()
            self.assertTrue(manifest['complete'])
            tracks = {item['speaker']: item for item in manifest['tracks']}
            self.assertEqual(tracks['microphone']['frames'], 3200)
            self.assertEqual(tracks['assistant']['frames'], 960)
            input_audio, rate = sf.read(Path(directory) / tracks['microphone']['path'])
            self.assertEqual(rate, 16000)
            self.assertAlmostEqual(input_audio[0], .1, places=4)
            self.assertEqual(input_audio[-1], 0)
            timeline = [json.loads(line) for line in (Path(directory) / manifest['timeline_path']).read_text().splitlines()]
            user_chunks = [item for item in timeline if item['track'].startswith('microphone')]
            self.assertEqual(user_chunks[1]['frame_offset'], 1600)
            self.assertEqual(user_chunks[1]['session_ms'], 100)
            self.assertFalse(recorder.enqueue('microphone', np.zeros(160), 16000))

    def test_audio_device_failure_without_microphone_frames_is_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = FullSessionRecorder(directory)
            recorder.enqueue('assistant', np.zeros(240), 24000)
            manifest = recorder.close()
            self.assertFalse(manifest['complete'])
            self.assertFalse(manifest['microphone_captured'])
            self.assertTrue(manifest['closed'])

    def test_overflow_is_explicit_and_does_not_wait_for_writer(self):
        release = threading.Event()
        original = FullSessionRecorder._write
        def waiting(recorder):
            release.wait(2)
            original(recorder)
        with tempfile.TemporaryDirectory() as directory, patch.object(FullSessionRecorder, '_write', waiting):
            recorder = FullSessionRecorder(directory, queue_blocks=1)
            try:
                self.assertTrue(recorder.enqueue('microphone', np.zeros(100), 16000))
                self.assertFalse(recorder.enqueue('microphone', np.zeros(100), 16000))
            finally:
                release.set()
            manifest = recorder.close()
            self.assertFalse(manifest['complete'])
            self.assertEqual(manifest['dropped_blocks'], 1)
            self.assertEqual(manifest['dropped_frames'], 100)


class ExportTests(unittest.TestCase):
    def test_full_session_package_keeps_question_scores_transcript_and_media(self):
        with tempfile.TemporaryDirectory() as directory:
            interview = Interview(ROOT / 'configs/dass21.zh.json', directory)
            event = interview.record_event('assistant', interview.prompt)
            interview.mark_playback(event, delivery='completed', played_duration_s=1.3)
            interview.accept('重复')
            interview.accept('二分', np.zeros(1600, dtype=np.float32), 16000)
            for _ in range(20):
                interview.accept('零分')
            summary = interview.basic_summary()
            self.assertIn('抑郁维度的得分为0分', summary)
            self.assertIn('焦虑维度的得分为0分', summary)
            self.assertIn('压力维度的得分为4分', summary)
            interview.finish_summary(summary)
            recorder = FullSessionRecorder(interview.directory, origin=interview.journal.origin)
            recorder.enqueue('microphone', np.zeros(1600, dtype=np.float32), 16000)
            recorder.enqueue('assistant', np.zeros(2400, dtype=np.float32), 24000, event_id=event)
            recorder.close()
            result = interview.export()
            package = json.loads((interview.directory / result['json_path']).read_text())
            self.assertEqual(package['completed_count'], 21)
            self.assertEqual(len(package['questions']), 21)
            self.assertEqual(package['conversation'][0]['delivery'], 'completed')
            self.assertEqual(package['conversation'][1]['text'], '重复')
            self.assertEqual(package['questions'][0]['segments'][0]['event_id'], package['conversation'][2]['id'])
            self.assertTrue(package['full_audio']['complete'])
            with zipfile.ZipFile(interview.directory / result['archive_path']) as archive:
                names = archive.namelist()
                self.assertIn('answers.json', names)
                self.assertIn('transcript.json', names)
                self.assertIn('session_package.json', names)
                self.assertIn('audio/Q01-001.wav', names)
                self.assertIn('full_audio/manifest.json', names)
                self.assertFalse(any(name.endswith('.zip') for name in names))
            self.assertIsNone(package['questions'][1]['segments'][0]['audio_path'])

    def test_stopped_session_preserves_missing_scores_and_unrecognized_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            interview = Interview(ROOT / 'configs/dass21.zh.json', directory)
            interview.record_unrecognized(np.zeros(800, dtype=np.float32), 16000)
            interview.accept('结束访谈')
            result = interview.export()
            package = json.loads((interview.directory / result['json_path']).read_text())
            self.assertEqual(package['status'], 'stopped')
            self.assertFalse(package['scoring']['complete'])
            self.assertIsNone(package['scoring']['subscales']['stress']['doubled_score'])
            self.assertEqual(len(package['unrecognized_segments']), 1)
            self.assertIn('暂不计算分数', interview.basic_summary())
            self.assertNotIn('维度的得分为0分', interview.basic_summary())

    def test_export_never_packages_external_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            interview = Interview(ROOT / 'configs/dass21.zh.json', directory)
            outside = Path(directory) / 'private.wav'
            outside.write_bytes(b'private')
            folder = interview.directory / 'audio'
            folder.mkdir()
            (folder / 'outside.wav').symlink_to(outside)
            result = interview.export()
            with zipfile.ZipFile(interview.directory / result['archive_path']) as archive:
                self.assertNotIn('audio/outside.wav', archive.namelist())


class EncounterIntegrationTests(unittest.TestCase):
    def test_robot_greeting_to_questionnaire_final_result_is_one_downstream_package(self):
        from types import SimpleNamespace
        from orin_voice.service import InterviewService
        runtime = SimpleNamespace(hardware=None, pause_voice=lambda: None, release_voice=lambda: None,
            dialogue=SimpleNamespace(reply=lambda text, history=None, cancel=None: '你好，准备好了可以开始测评。'))
        with tempfile.TemporaryDirectory() as directory:
            service = InterviewService(output=directory, questions=ROOT / 'configs/dass21.zh.json')
            service.robot = runtime
            wake = {'trigger': 'vision', 'faces': 1}
            state = service.start('text', robot=True, wake_event=wake)
            sid = state['session_id']
            service.action(sid, 'answer', text='你好')
            state = service.action(sid, 'answer', text='开始测评')
            self.assertEqual(state['stage'], 'assessment')
            self.assertEqual(state['completed_count'], 0)
            for i in range(1, 22):
                state = service.action(sid, 'answer', score=2, question_id=f'Q{i:02}')
            package = json.loads((service.session_path(sid) / 'session_package.json').read_text())
            self.assertEqual(package['wake_event'], wake)
            self.assertEqual(package['stage'], 'assessment')
            self.assertEqual(package['status'], 'completed')
            self.assertTrue(package['questionnaire_provenance']['source'])
            self.assertEqual(package['conversation'][1]['text'], '你好')
            self.assertEqual(package['conversation'][3]['text'], '开始测评')
            self.assertEqual(len(package['conversation']), 47)
            self.assertEqual(package['conversation'][-1]['delivery'], 'text')
            self.assertIn('压力维度的得分为28分', package['conversation'][-1]['text'])
            with zipfile.ZipFile(service.session_path(sid) / 'session_package.zip') as archive:
                zipped = json.loads(archive.read('session_package.json'))
                self.assertEqual(zipped['conversation'], package['conversation'])

    def test_voice_worker_error_still_exports_stopped_conversation(self):
        from orin_voice.service import InterviewService
        def failed(control):
            control.interview.record_event('assistant', '你好', delivery='failed')
            raise RuntimeError('测试播放失败')
        with tempfile.TemporaryDirectory() as directory:
            service = InterviewService(output=directory, questions=ROOT / 'configs/dass21.zh.json', voice_runner=failed)
            sid = service.start('voice')['session_id']
            service.worker.join(timeout=3)
            package = json.loads((service.session_path(sid) / 'session_package.json').read_text())
            self.assertEqual(package['status'], 'stopped')
            self.assertEqual(package['conversation'][0]['delivery'], 'failed')
            self.assertEqual(package['conversation'][-1]['kind'], 'worker_error')
            self.assertEqual(package['conversation'][-1]['text'], '测试播放失败')
            self.assertTrue((service.session_path(sid) / 'session_package.zip').is_file())

    def test_cancelled_greeting_keeps_user_transcript_and_prelude_audio(self):
        from concurrent.futures import ThreadPoolExecutor
        from types import SimpleNamespace
        from unittest.mock import Mock
        from orin_voice.service import SessionControl
        with tempfile.TemporaryDirectory() as directory:
            interview = Interview(ROOT / 'configs/dass21.zh.json', directory)
            interview.data['stage'] = 'greeting'
            runtime = SimpleNamespace(hardware=None, dialogue=SimpleNamespace(reply=Mock()))
            control = SessionControl(interview, 'voice', robot_runtime=runtime)
            cancel = threading.Event()
            cancel.set()
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = control.respond('我刚刚说了一句话', np.zeros(1600, dtype=np.float32), 16000,
                                         cancel, reply_executor=executor)
            self.assertIsNone(result)
            event = interview.journal.snapshot()[0]
            self.assertEqual(event['text'], '我刚刚说了一句话')
            self.assertIsNone(event['question_id'])
            self.assertEqual(event['stage'], 'greeting')
            self.assertTrue((interview.directory / event['audio_path']).is_file())
            runtime.dialogue.reply.assert_not_called()

    def test_interrupted_actual_playback_is_exported_as_partial_submitted_audio(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        from orin_voice.duplex import run
        from orin_voice.service import SessionControl
        with tempfile.TemporaryDirectory() as directory:
            interview = Interview(ROOT / 'configs/dass21.zh.json', directory)
            control = SessionControl(interview, 'voice')
            device = MagicMock()
            device.query_devices.return_value = [dict(name='pulse', max_input_channels=1,
                max_output_channels=1, default_samplerate=16000)]
            synthesizer = MagicMock()
            synthesizer.synthesize.return_value = (np.zeros(1600, dtype=np.float32), 16000)
            device.OutputStream.return_value.__enter__.return_value.write.side_effect = lambda samples: control.stop.set()
            args = SimpleNamespace(questions=str(ROOT / 'configs/dass21.zh.json'), output=directory,
                speech_threshold=.015, silence_ms=600, input_device=None, profile='baseline',
                port=18080, gpu_layers=99, ctx_size=4096, max_tokens=160)
            with patch.dict('os.environ', PULSE_SOURCE='orin_aec_source', PULSE_SINK='orin_aec_sink'), \
                 patch('orin_voice.audio.sound_device', return_value=device), \
                 patch('orin_voice.audio.SpeechRecognizer'), \
                 patch('orin_voice.audio.SpeechSynthesizer', return_value=synthesizer):
                run(args, control=control)
            package = json.loads((interview.directory / 'session_package.json').read_text())
            assistant = next(event for event in package['conversation'] if event['role'] == 'assistant')
            self.assertEqual(assistant['delivery'], 'interrupted')
            self.assertAlmostEqual(assistant['played_duration_s'], .02)
            track = next(track for track in package['full_audio']['tracks'] if track['speaker'] == 'assistant')
            self.assertEqual(track['frames'], 320)
            self.assertEqual(package['status'], 'stopped')
