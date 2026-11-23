"""Whole encounter regressions for the primary-school default questionnaire."""
import io
import json
import queue
import tempfile
import unittest
import zipfile
from email.message import Message
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from orin_voice.interview import Interview
from orin_voice.service import Conflict, InterviewService, SessionControl
from orin_voice.settings import ROOT
from orin_voice.web import handler_for


PROFILE = {'name': '测试同学', 'height_cm': 135, 'weight_kg': 30, 'gender': '女', 'age': 9}
RATINGS = [('R_MOOD', 'mood'), ('R_WORRY', 'worry'), ('R_TENSION', 'tension')]


class PrimaryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service = InterviewService(output=self.temporary.name)

    def request(self, method, path, body=None):
        cls = handler_for(self.service)
        handler = cls.__new__(cls)
        handler.command, handler.path = method, path
        handler.request_version = 'HTTP/1.1'
        handler.requestline = method + ' ' + path + ' HTTP/1.1'
        handler.server = SimpleNamespace(server_port=18088)
        handler.client_address = ('127.0.0.1', 12345)
        handler.log_message = lambda *args: None
        handler.headers = Message()
        handler.headers['Host'] = '127.0.0.1:18088'
        payload = json.dumps(body).encode() if body is not None else b''
        if body is not None:
            handler.headers['Content-Type'] = 'application/json'
            handler.headers['Content-Length'] = str(len(payload))
        handler.rfile, handler.wfile = io.BytesIO(payload), io.BytesIO()
        handler.dispatch(method)
        headers, payload = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
        return int(headers.split(b' ')[1]), payload

    def answer(self, state, action='answer', **values):
        return self.service.action(state['session_id'], action,
                                   question_id=state['current_question']['id'], **values)

    def through_tasks(self, score=2, skip=False):
        state = self.service.start('text', workflow=True, profile=PROFILE)
        for _ in range(21):
            state = self.answer(state, 'skip') if skip else self.answer(state, score=score)
        self.assertEqual(state['stage'], 'tasks')
        for task in ('task_reading', 'task_open_1', 'task_open_2', 'task_open_3'):
            self.assertEqual(state['current_question']['id'], task)
            state = self.answer(state, 'skip') if skip else self.answer(state, text='我的任务回答。')
            if not skip:
                state = self.answer(state, 'finish')
        self.assertEqual(state['stage'], 'task_reflection')
        return state

    def test_http_default_and_full_encounter_export_are_primary_without_adult_norms(self):
        status, payload = self.request('GET', '/api/questions')
        config = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(config['instrument'], 'PRIMARY-21')
        self.assertEqual(config['response_options'], {'0': '没有', '1': '偶尔', '2': '有时', '3': '经常'})
        status, payload = self.request('POST', '/api/sessions',
                                       {'mode': 'text', 'workflow': True, 'profile': PROFILE})
        self.assertEqual(status, 200)
        state = json.loads(payload)
        self.assertEqual(state['instrument'], 'PRIMARY-21')
        self.assertEqual(state['total_questions'], 28)
        sid = state['session_id']
        for _ in range(21):
            state = self.answer(state, score=2)
        self.assertEqual(state['stage'], 'tasks')
        self.assertFalse(state['task_scoring']['complete'])
        for _ in range(4):
            task_id = state['current_question']['id']
            state = self.answer(state, text='我昨天和同学一起玩。')
            self.assertEqual(state['current_question']['id'], task_id)
            self.assertEqual(state['status'], 'in_progress')
            state = self.answer(state, 'finish')
        self.assertEqual(state['stage'], 'task_reflection')
        self.assertIsNone(state['overall_summary'])
        self.assertEqual(state['completed_count'], 25)
        for (item_id, _), choice in zip(RATINGS, (1, 2, 3)):
            self.assertEqual(state['current_question']['id'], item_id)
            state = self.answer(state, score=choice)
        self.assertEqual(state['stage'], 'completed')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['completed_count'], 28)
        self.assertTrue(state['scoring']['complete'])
        self.assertTrue(state['task_scoring']['complete'])
        for key, rating in (('mood', 1), ('worry', 2), ('tension', 3)):
            part = state['scoring']['subscales'][key]
            self.assertEqual(part['raw_score'], 14)
            self.assertEqual(part['mean_score'], 2)
            self.assertNotIn('doubled_score', part)
            self.assertEqual(state['task_scoring']['subscales'][key]['mean_score'], rating)
            comparison = state['score_comparison']['dimensions'][key]
            self.assertEqual(comparison['questionnaire_mean'], 2)
            self.assertEqual(comparison['task_rating'], rating)
            self.assertEqual(comparison['difference'], rating - 2)
        self.assertFalse(state['score_comparison']['automatic_consistency_judgment'])
        for file_name in ('answers.json', 'session_package.json'):
            status, payload = self.request('GET', f'/api/sessions/{sid}/{file_name}')
            self.assertEqual(status, 200)
            exported = json.loads(payload)
            self.assertEqual(exported['task_reflections'], state['task_reflections'])
            self.assertEqual(exported['task_scoring'], state['task_scoring'])
            self.assertEqual(exported['score_comparison'], state['score_comparison'])
        status, payload = self.request('GET', f'/api/teacher/sessions/{sid}')
        self.assertEqual(status, 200)
        reviewed = json.loads(payload)
        self.assertEqual(reviewed['name'], PROFILE['name'])
        self.assertEqual(reviewed['answers']['task_reflections'], state['task_reflections'])
        with zipfile.ZipFile(self.service.session_path(sid) / 'session_package.zip') as archive:
            zipped = json.loads(archive.read('session_package.json'))
            self.assertEqual(zipped['task_scoring'], state['task_scoring'])
            self.assertEqual(zipped['score_comparison'], state['score_comparison'])

    def test_narratives_repeat_and_confirmation_never_infer_a_frequency_choice(self):
        state = self.service.start('text', workflow=True, profile=PROFILE)
        sid = state['session_id']
        for text in ('我最近经常不开心，不过有时也会开心。', '不是三分，是不是二分呢？'):
            state = self.answer(state, text=text)
            self.assertEqual(state['current_question']['id'], 'Q01')
            self.assertIsNone(state['questions'][0]['score'])
        state = self.answer(state, 'finish')
        self.assertEqual(state['current_question']['id'], 'Q01')
        state = self.answer(state, text='有时')
        self.assertEqual(state['questions'][0]['score'], 2)
        with self.assertRaises(Conflict):
            self.service.action(sid, 'answer', score=3, question_id='Q01')
        for _ in range(20):
            state = self.answer(state, 'skip')
        for _ in range(4):
            state = self.answer(state, 'skip')
        self.assertEqual(state['stage'], 'task_reflection')
        state = self.answer(state, text='我昨天很难过，也有很开心的时候。')
        self.assertEqual(state['current_question']['id'], 'R_MOOD')
        self.assertIsNone(state['task_reflections'][0]['score'])
        state = self.answer(state, 'repeat')
        state = self.answer(state, 'finish')
        self.assertEqual(state['current_question']['id'], 'R_MOOD')
        self.assertIsNone(state['task_scoring']['subscales']['mood']['mean_score'])
        self.assertFalse(state['score_comparison']['dimensions']['mood']['comparable'])
        with self.assertRaises(ValueError):
            self.service.active.interview.summary_prompt()

    def test_skipped_questionnaire_and_reflections_are_missing_not_zero(self):
        state = self.through_tasks(skip=True)
        for _ in range(3):
            state = self.answer(state, 'skip')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['completed_count'], 0)
        for result in (state['scoring'], state['task_scoring']):
            self.assertFalse(result['complete'])
            self.assertIsNone(result['total_score'])
            self.assertIsNone(result['mean_score'])
            for part in result['subscales'].values():
                self.assertIsNone(part['mean_score'])
                self.assertIsNone(part['raw_score'])
        for part in state['score_comparison']['dimensions'].values():
            self.assertFalse(part['comparable'])
            self.assertIsNone(part['difference'])

    def test_reflection_audio_keeps_original_answer_and_question_event_link(self):
        state = self.through_tasks()
        control = self.service.active
        control.respond('偶尔', np.zeros(1600, dtype=np.float32), 16000)
        control.update()
        state = control.read()
        item = state['task_reflections'][0]
        segment = item['segments'][0]
        self.assertEqual(item['score'], 1)
        self.assertEqual(item['answer_text'], '偶尔')
        self.assertEqual(segment['selected_score'], 1)
        self.assertAlmostEqual(segment['duration_s'], .1)
        event = next(e for e in control.interview.journal.snapshot() if e['id'] == segment['event_id'])
        self.assertEqual(event['question_id'], 'R_MOOD')
        self.assertEqual(event['audio_path'], segment['audio_path'])
        status, payload = self.request('GET', f"/api/sessions/{state['session_id']}/{segment['audio_path']}")
        self.assertEqual(status, 200)
        self.assertTrue(payload.startswith(b'RIFF'))

    def test_new_default_can_read_adult_dass_archive_without_relabeling_it(self):
        adult = InterviewService(output=self.temporary.name, questions=ROOT / 'configs/dass21.zh.json')
        state = adult.start('text')
        adult.action(state['session_id'], 'stop')
        path = adult.session_path(state['session_id']) / 'answers.json'
        saved = json.loads(path.read_text())
        saved['questions'][0]['question'] = '我覺得很難讓自己安靜下來'
        path.write_text(json.dumps(saved), encoding='utf-8')
        archived = self.service.get(state['session_id'])
        self.assertEqual(archived['instrument'], 'DASS-21')
        self.assertEqual(archived['questions'][0]['question'], '我觉得很难让自己安静下来')
        self.assertEqual(set(archived['scoring']['subscales']), {'depression', 'anxiety', 'stress'})
        self.assertNotIn('task_scoring', archived)

    def test_real_duplex_flow_never_loads_or_queries_llm_for_primary_scoring(self):
        from orin_voice.duplex import run
        interview = Interview(ROOT / 'configs/primary21.zh.json', self.temporary.name,
                              workflow=True, participant=PROFILE)
        control = SessionControl(interview, 'voice')
        control.commands = queue.Queue()
        for item in interview.data['questions']:
            control.commands.put(('有时', item['id']))
        for item in interview.data['tasks']:
            control.commands.put(('我和同学一起玩。', item['id']))
            control.commands.put(('回答完了', item['id']))
        for item in interview.data['task_reflections']:
            control.commands.put(('偶尔', item['id']))
        device = MagicMock()
        device.query_devices.return_value = [dict(name='pulse', max_input_channels=1,
            max_output_channels=1, default_samplerate=16000)]
        synthesizer = MagicMock()
        synthesizer.synthesize.return_value = (np.zeros(160, dtype=np.float32), 16000)
        args = SimpleNamespace(questions=str(ROOT / 'configs/primary21.zh.json'), output=self.temporary.name,
            speech_threshold=.015, silence_ms=600, input_device=None, profile='baseline',
            port=18080, gpu_layers=99, ctx_size=4096, max_tokens=160)
        with patch.dict('os.environ', PULSE_SOURCE='orin_aec_source', PULSE_SINK='orin_aec_sink'), \
             patch('orin_voice.audio.sound_device', return_value=device), \
             patch('orin_voice.audio.SpeechRecognizer'), \
             patch('orin_voice.audio.SpeechSynthesizer', return_value=synthesizer), \
             patch('orin_voice.server.LocalServer') as server, \
             patch('orin_voice.llm.LocalChat') as chat, redirect_stdout(io.StringIO()):
            run(args, control=control)
        server.assert_not_called()
        chat.assert_not_called()
        saved = json.loads((interview.directory / 'answers.json').read_text())
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['completed_count'], 28)
        self.assertEqual(saved['scoring']['mean_score'], 2)
        self.assertEqual(saved['task_scoring']['mean_score'], 1)
        self.assertTrue(control.commands.empty())


if __name__ == '__main__':
    unittest.main()
