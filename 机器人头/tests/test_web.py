import io
import json
import tempfile
import threading
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

from orin_voice.service import InterviewService, Conflict
from orin_voice.settings import ROOT
from orin_voice.web import argument_parser, handler_for, specification


class WebArgumentTests(unittest.TestCase):
    def test_psyche_chat_profile_and_overrides_are_parseable(self):
        args = argument_parser().parse_args([
            '--chat-profile', 'psyche', '--chat-port', '18082', '--chat-max-tokens', '1024'])
        self.assertEqual(args.chat_profile, 'psyche')
        self.assertEqual(args.chat_port, 18082)
        self.assertEqual(args.chat_max_tokens, 1024)

    def test_chat_defaults_remain_unset_for_robot_config(self):
        args = argument_parser().parse_args([])
        self.assertIsNone(args.chat_profile)
        self.assertIsNone(args.chat_port)
        self.assertIsNone(args.chat_max_tokens)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = InterviewService(output=self.directory.name, questions=ROOT / 'configs/dass21.zh.json')

    def test_text_full_interview_export_and_history(self):
        state = self.service.start('text')
        sid = state['session_id']
        self.assertEqual(state['current_question']['question'], '我觉得很难让自己安静下来')
        self.assertEqual(state['questions'][0]['source_text'], '我覺得很難讓自己安靜下來')
        for number in range(1, 22):
            state = self.service.action(sid, 'answer', score=2, question_id=f'Q{number:02}')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['completed_count'], 21)
        self.assertIsNone(state['current_question'])
        self.assertEqual(state['scoring']['subscales']['anxiety']['doubled_score'], 28)
        self.assertTrue((self.service.session_path(sid) / 'summary.txt').is_file())
        self.assertEqual(self.service.list_sessions()[0]['session_id'], sid)
        self.assertTrue(all(s['audio_path'] is None for q in state['questions'] for s in q['segments']))
        self.service.start('text')
        self.assertEqual(self.service.get(sid)['phase'], 'archived')

    def test_single_active_session_stale_question_and_invalid_scores(self):
        sid = self.service.start('text')['session_id']
        with self.assertRaises(Conflict):
            self.service.start('text')
        with self.assertRaises(Conflict):
            self.service.action(sid, 'answer', score=1, question_id='Q02')
        for score in (True, 4, -1, '2'):
            with self.assertRaises(ValueError):
                self.service.action(sid, 'answer', score=score)
        self.service.action(sid, 'answer', score=2, question_id='Q01')
        self.service.action(sid, 'previous')
        self.assertEqual(self.service.get(sid)['current_index'], 0)
        self.service.action(sid, 'stop')
        with self.assertRaises(Conflict):
            self.service.action(sid, 'answer', score=1)
        self.service.start('text')

    def test_failed_audio_is_visible_and_allows_restarting(self):
        def broken(control):
            raise RuntimeError('测试音频不可用')
        self.service.voice_runner = broken
        sid = self.service.start('voice')['session_id']
        self.service.worker.join(timeout=2)
        state = self.service.get(sid)
        self.assertEqual(state['phase'], 'error')
        self.assertIn('测试音频不可用', state['error'])
        self.assertEqual(state['status'], 'stopped')
        self.service.start('text')

    def test_voice_actions_queue_and_stop_signal(self):
        ready = threading.Event()
        def runner(control):
            control.update(phase='listening')
            ready.set()
            control.stop.wait(2)
        self.service.voice_runner = runner
        sid = self.service.start('voice')['session_id']
        self.assertTrue(ready.wait(1))
        self.service.action(sid, 'answer', score=2, question_id='Q01')
        self.assertEqual(self.service.active.commands.get_nowait(), ('2分', 'Q01'))
        self.service.action(sid, 'stop')
        self.service.worker.join(timeout=2)
        self.assertEqual(self.service.get(sid)['status'], 'stopped')

    def test_archived_traditional_display_is_simplified_without_rewriting(self):
        sid = self.service.start('text')['session_id']
        self.service.action(sid, 'stop')
        path = self.service.session_path(sid) / 'answers.json'
        data = json.loads(path.read_text())
        data['questions'][0]['question'] = '我覺得很難讓自己安靜下來'
        path.write_text(json.dumps(data))
        self.service.active = None
        self.assertEqual(self.service.get(sid)['questions'][0]['question'], '我觉得很难让自己安静下来')
        self.assertEqual(json.loads(path.read_text())['questions'][0]['question'], '我覺得很難讓自己安靜下來')

    def test_simplified_prompts_reuse_original_speech_cache(self):
        self.service.start('text')
        interview = self.service.active.interview
        canonical = interview.synthesis_text(interview.prompt)
        self.assertIn('我覺得很難讓自己安靜下來', canonical)
        self.assertIn('颇适用', interview.options_prompt)
        self.assertIn('頗適用', interview.synthesis_text(interview.options_prompt))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = InterviewService(output=self.directory.name, questions=ROOT / 'configs/dass21.zh.json')

    def request(self, method, path, body=None, origin=None, host='127.0.0.1:18088'):
        # Exercise the actual HTTP handler without sockets, which are restricted here.
        handler = handler_for(self.service).__new__(handler_for(self.service))
        handler.command = method
        handler.path = path
        handler.request_version = 'HTTP/1.1'
        handler.requestline = method + ' ' + path + ' HTTP/1.1'
        handler.server = SimpleNamespace(server_port=18088)
        handler.client_address = ('127.0.0.1', 12345)
        handler.log_message = lambda *args: None
        handler.headers = Message()
        handler.headers['Host'] = host
        data = json.dumps(body).encode() if body is not None else b''
        if body is not None:
            handler.headers['Content-Type'] = 'application/json'
            handler.headers['Content-Length'] = str(len(data))
        if origin:
            handler.headers['Origin'] = origin
        handler.rfile = io.BytesIO(data)
        handler.wfile = io.BytesIO()
        handler.dispatch(method)
        header, payload = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
        status = int(header.split(b' ')[1])
        return status, payload

    def test_http_create_answer_read_export_and_static_ui(self):
        status, payload = self.request('POST', '/api/sessions', {'mode': 'text'})
        self.assertEqual(status, 200)
        sid = json.loads(payload)['session_id']
        status, payload = self.request('POST', f'/api/sessions/{sid}/actions',
                                       {'action': 'answer', 'score': 2, 'question_id': 'Q01'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)['questions'][0]['score'], 2)
        status, payload = self.request('GET', f'/api/sessions/{sid}/answers.json')
        self.assertEqual(json.loads(payload)['completed_count'], 1)
        self.assertEqual(status, 200)
        for path, expected in [('/', '访谈工作台'), ('/app.js', 'renderAnswers'), ('/style.css', '.question-card'),
                               ('/api/docs', '调用示例')]:
            status, payload = self.request('GET', path)
            self.assertEqual(status, 200)
            self.assertIn(expected, payload.decode())

    def test_errors_origins_and_traversal(self):
        self.assertEqual(self.request('GET', '/api/status', host='evil.example:18088')[0], 403)
        self.assertEqual(self.request('POST', '/api/sessions', {'mode': 'text'}, origin='https://evil.example')[0], 403)
        self.assertEqual(self.request('POST', '/api/sessions', {'mode': 'bad'})[0], 400)
        self.assertEqual(self.request('GET', '/api/sessions/../../etc/passwd')[0], 404)
        sid = self.service.start('text')['session_id']
        self.assertEqual(self.request('POST', '/api/sessions', {'mode': 'text'})[0], 409)
        self.assertEqual(self.request('GET', f'/api/sessions/{sid}/audio/%2e%2e%2fanswers.json.wav')[0], 404)
        status, payload = self.request('GET', '/api/openapi.json')
        self.assertEqual(status, 200)
        self.assertIn('/api/sessions/{id}/actions', json.loads(payload)['paths'])

    def test_audio_download_and_missing_summary(self):
        sid = self.service.start('text')['session_id']
        audio = self.service.session_path(sid) / 'audio'
        audio.mkdir()
        (audio / 'Q01-001.wav').write_bytes(b'RIFF-test-recording')
        status, payload = self.request('GET', f'/api/sessions/{sid}/audio/Q01-001.wav')
        self.assertEqual(status, 200)
        self.assertEqual(payload, b'RIFF-test-recording')
        self.assertEqual(self.request('GET', f'/api/sessions/{sid}/summary.txt')[0], 404)


class VoiceBridgeTests(unittest.TestCase):
    def test_http_style_score_is_consumed_by_real_duplex_loop_and_saved(self):
        from unittest.mock import MagicMock, patch
        import numpy as np
        from orin_voice.duplex import run
        from orin_voice.service import SessionControl
        from orin_voice.interview import Interview
        with tempfile.TemporaryDirectory() as directory:
            interview = Interview(ROOT / 'configs/dass21.zh.json', directory)
            control = SessionControl(interview, 'voice')
            control.commands.put_nowait(('2分', 'Q01'))
            device = MagicMock()
            device.query_devices.return_value = [dict(name='pulse', max_input_channels=1,
                max_output_channels=1, default_samplerate=16000)]
            output = device.OutputStream.return_value.__enter__.return_value
            def played(samples):
                if interview.index == 1:
                    control.stop.set()
            output.write.side_effect = played
            synthesizer = MagicMock()
            synthesizer.synthesize.return_value = (np.zeros(320, dtype=np.float32), 16000)
            args = SimpleNamespace(questions=str(ROOT / 'configs/dass21.zh.json'), output=directory,
                speech_threshold=.015, silence_ms=600, input_device=None, profile='baseline',
                port=18080, gpu_layers=99, ctx_size=4096, max_tokens=160)
            with patch.dict('os.environ', PULSE_SOURCE='orin_aec_source', PULSE_SINK='orin_aec_sink'), \
                 patch('orin_voice.audio.sound_device', return_value=device), \
                 patch('orin_voice.audio.SpeechRecognizer'), \
                 patch('orin_voice.audio.SpeechSynthesizer', return_value=synthesizer):
                run(args, control=control)
            saved = json.loads((interview.directory / 'answers.json').read_text())
            self.assertEqual(saved['questions'][0]['score'], 2)
            self.assertEqual(saved['completed_count'], 1)
            self.assertEqual(saved['status'], 'stopped')
            self.assertEqual(control.read()['phase'], 'stopped')
            self.assertTrue(any('已记录2分' in event['text'] for event in control.read()['events']))


class AudioRouteTests(unittest.TestCase):
    def test_remote_mic_has_priority_without_routing_prompts_into_the_bridge(self):
        import os
        from unittest.mock import patch
        from orin_voice.service import _audio_route

        responses = {
            ('info',): '',
            ('list', 'short', 'sources'): '1 alsa_input.ME01TC\n2 xinyu_todesk_mic\n',
            ('list', 'short', 'sinks'): '1 alsa_output.speaker\n2 xinyu_todesk_voice\n',
            ('get-default-sink',): 'alsa_output.speaker',
            ('unload-module', '42'): '',
        }
        def pactl(args, **kwargs):
            result = '42' if args[1] == 'load-module' else responses[tuple(args[1:])]
            return SimpleNamespace(stdout=result)

        for override, expected_mic in [({}, 'xinyu_todesk_mic'), ({'ORIN_MIC_SOURCE': 'alsa_input.ME01TC'}, 'alsa_input.ME01TC')]:
            with self.subTest(expected_mic=expected_mic), patch.dict(os.environ, override, clear=True), \
                 patch('orin_voice.service.subprocess.run', side_effect=pactl) as run:
                with _audio_route():
                    pass
                load, = [call.args[0] for call in run.call_args_list if call.args[0][1] == 'load-module']
                self.assertIn(f'source_master={expected_mic}', load)
                self.assertIn('sink_master=alsa_output.speaker', load)

        responses[('get-default-sink',)] = 'xinyu_todesk_voice'
        for override in [{}, {'ORIN_SPEAKER_SINK': 'xinyu_todesk_voice'}]:
            with self.subTest(override=override), patch.dict(os.environ, override, clear=True), \
                 patch('orin_voice.service.subprocess.run', side_effect=pactl) as run:
                with self.assertRaisesRegex(ValueError, '用于播报的扬声器'), _audio_route():
                    pass
                self.assertFalse(any(call.args[0][1] == 'load-module' for call in run.call_args_list))

    def test_old_route_names_do_not_block_unique_new_route_or_get_unloaded(self):
        import os
        from unittest.mock import patch
        from orin_voice.service import _audio_route
        calls = []
        def pactl(args, **kwargs):
            calls.append(args)
            command = args[1:]
            if command == ['list', 'short', 'sources']:
                result = '1 alsa_input.usb-General_ME01TC\n2 orin_aec_source\n'
            elif command == ['list', 'short', 'sinks']:
                result = '1 alsa_output.usb-General_ME01TC\n2 orin_aec_sink\n'
            elif command[0] == 'load-module':
                result = '42'
            else:
                result = ''
            return SimpleNamespace(stdout=result)
        with patch('orin_voice.service.subprocess.run', side_effect=pactl), patch.dict(os.environ):
            before = os.environ.get('PULSE_SOURCE')
            with _audio_route():
                self.assertRegex(os.environ['PULSE_SOURCE'], r'^orin_aec_source_[0-9]+_[a-f0-9]{8}$')
                self.assertEqual(os.environ['PULSE_SINK'], os.environ['PULSE_SOURCE'].replace('source', 'sink', 1))
            self.assertEqual(os.environ.get('PULSE_SOURCE'), before)
        unloaded = [args for args in calls if args[1] == 'unload-module']
        self.assertEqual(unloaded, [['pactl', 'unload-module', '42']])

    def test_only_a_live_interview_lock_blocks_and_release_allows_restart(self):
        from contextlib import nullcontext
        from unittest.mock import patch
        from orin_voice.service import audio_route
        with tempfile.TemporaryDirectory() as directory, \
             patch('orin_voice.service.ROOT', Path(directory)), \
             patch('orin_voice.service._audio_route', side_effect=lambda: nullcontext()):
            with audio_route():
                with self.assertRaisesRegex(RuntimeError, '另一个访谈进程'):
                    with audio_route():
                        pass
            with audio_route():
                pass


class EmptyRecognitionTests(unittest.TestCase):
    def test_detected_audio_without_text_is_saved_and_acknowledged(self):
        from unittest.mock import MagicMock, patch
        import numpy as np
        from orin_voice.duplex import run
        from orin_voice.service import SessionControl
        from orin_voice.interview import Interview
        with tempfile.TemporaryDirectory() as directory:
            interview = Interview(ROOT / 'configs/dass21.zh.json', directory)
            control = SessionControl(interview, 'voice')
            device = MagicMock()
            device.query_devices.return_value = [dict(name='pulse', max_input_channels=1,
                max_output_channels=1, default_samplerate=16000)]
            def input_stream(**kwargs):
                stream = MagicMock()
                def opened():
                    callback = kwargs['callback']
                    for level in [.005]*6 + [0.]*20:
                        callback(np.full((480, 1), level, dtype=np.float32), 480, None, None)
                    return stream
                stream.__enter__.side_effect = opened
                return stream
            device.InputStream.side_effect = input_stream
            synthesizer = MagicMock()
            last_text = ['']
            def synthesized(text):
                last_text[0] = text
                return np.zeros(320, dtype=np.float32), 16000
            synthesizer.synthesize.side_effect = synthesized
            def played(samples):
                if '没听清' in last_text[0]:
                    control.stop.set()
            device.OutputStream.return_value.__enter__.return_value.write.side_effect = played
            recognizer = MagicMock()
            recognizer.transcribe.return_value = ''
            args = SimpleNamespace(questions=str(ROOT / 'configs/dass21.zh.json'), output=directory,
                speech_threshold=.015, silence_ms=600, input_device=None, profile='baseline',
                port=18080, gpu_layers=99, ctx_size=4096, max_tokens=160)
            with patch.dict('os.environ', PULSE_SOURCE='orin_aec_source', PULSE_SINK='orin_aec_sink'), \
                 patch('orin_voice.audio.sound_device', return_value=device), \
                 patch('orin_voice.audio.SpeechRecognizer', return_value=recognizer), \
                 patch('orin_voice.audio.SpeechSynthesizer', return_value=synthesizer):
                run(args, control=control)
            self.assertEqual(interview.data['completed_count'], 0)
            segment = interview.data['unrecognized_segments'][0]
            self.assertEqual(segment['question_id'], 'Q01')
            self.assertTrue((interview.directory / segment['audio_path']).is_file())
            self.assertTrue(any('没听清' in event['text'] for event in control.read()['events']))
            self.assertGreater(control.read()['audio']['capture_frames'], 0)


class CameraHttpTests(unittest.TestCase):
    setUp = HttpTests.setUp
    request = HttpTests.request
    def install_camera(self, frames=True):
        from unittest.mock import Mock
        hub = Mock()
        frame = SimpleNamespace(sequence=1, jpeg=b'\xff\xd8test-camera\xff\xd9')
        hub.wait_frame.side_effect = [frame, None] if frames else [None]
        hub.is_alive.return_value = True
        hub.status.return_value = {'phase':'streaming' if frames else 'error', 'error':None if frames else '摄像头未连接'}
        actions=[]
        def action(name):
            if name not in {'start','stop'}:
                raise ValueError('无效摄像头操作')
            actions.append(name)
            return {'phase':'streaming' if name=='start' else 'stopped'}
        self.service.robot=SimpleNamespace(camera=hub,camera_action=action,
                                           camera_status=lambda:{'phase':'streaming','rotation':180})
        return hub,actions,frame

    def test_camera_page_status_and_snapshot_do_not_arm_robot(self):
        hub,actions,frame=self.install_camera()
        status,body=self.request('GET','/camera')
        self.assertEqual(status,200)
        self.assertIn('摄像头实时画面',body.decode())
        self.assertEqual(self.request('GET','/camera.js')[0],200)
        status,body=self.request('GET','/api/camera/status')
        self.assertEqual(json.loads(body)['rotation'],180)
        status,body=self.request('GET','/api/camera/frame.jpg')
        self.assertEqual(status,200)
        self.assertEqual(body,frame.jpeg)
        self.assertEqual(actions,['start'])
        self.assertIsNone(self.service.active)

    def test_camera_stream_is_multipart_and_stops_when_no_new_frames(self):
        hub,actions,frame=self.install_camera()
        status,body=self.request('GET','/api/camera/stream.mjpg')
        self.assertEqual(status,200)
        self.assertIn(b'--frame\r\nContent-Type: image/jpeg',body)
        self.assertIn(frame.jpeg,body)
        self.assertEqual(hub.wait_frame.call_count,2)

    def test_camera_missing_or_invalid_action_is_explicit(self):
        self.install_camera(frames=False)
        status,body=self.request('GET','/api/camera/frame.jpg')
        self.assertEqual(status,503)
        self.assertIn('摄像头未连接',json.loads(body)['error'])
        self.assertEqual(self.request('POST','/api/camera/actions',{'action':'unknown'})[0],400)
