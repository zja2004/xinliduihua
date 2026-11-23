import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from orin_voice.clinical_store import ClinicalStore


class TeacherStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'sessions'
        self.directory = self.root / 'example-session'
        self.directory.mkdir(parents=True)
        self.store = ClinicalStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, value):
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def test_details_include_original_answers_latest_events_and_recording_errors(self):
        answers = {'session_id': 'example-session', 'status': 'completed', 'mode': 'text',
                   'participant': {'name': '小李', 'height_cm': 170, 'age': 20},
                   'overall_summary': '访谈已完成', 'questions': [
                       {'id': 'Q01', 'question': '示例', 'status': 'answered', 'score': 2,
                        'answer_text': '二分', 'segments': []}],
                   'tasks': [{'id': 'task_open_1', 'kind': 'open_1', 'status': 'answered',
                              'title': '开放性问题 1', 'answer_text': '昨天散步', 'segments': []}]}
        self.write('answers.json', answers)
        self.write('video/manifest.json', {'complete': False, 'frames': 0, 'error': '摄像头读取失败'})
        self.write('full_audio/manifest.json', {'complete': True, 'error': None})
        journal = [
            {'operation': 'create', 'event': {'id': 'e1', 'role': 'assistant', 'text': '你好', 'delivery': 'planned'}},
            {'operation': 'update', 'id': 'e1', 'fields': {'delivery': 'completed'}},
            {'operation': 'create', 'event': {'id': 'e2', 'role': 'user', 'text': '我很好'}},
        ]
        (self.directory / 'events.jsonl').write_text('\n'.join(json.dumps(x) for x in journal) + '\n{"partial":', encoding='utf-8')
        detail = self.store.sync_answers_json(self.directory)
        self.assertEqual(detail['answers']['questions'][0]['score'], 2)
        self.assertEqual(detail['summary'], '访谈已完成')
        self.assertEqual(detail['events'][0]['delivery'], 'completed')
        self.assertEqual(detail['events'][1]['text'], '我很好')
        self.assertEqual(detail['recording']['video']['error'], '摄像头读取失败')
        self.assertTrue(detail['artifacts']['answers.json'])

    def test_repeated_legacy_sync_reuses_participant_and_task_rows(self):
        self.write('answers.json', {'session_id': 'example-session', 'status': 'stopped', 'questions': [],
                                   'tasks': [{'id': 'task_reading', 'answer_text': '', 'segments': []}]})
        for _ in range(4):
            detail = self.store.sync_answers_json(self.directory)
        self.assertEqual(detail['name'], '未填写')
        self.assertEqual(len(detail['tasks']), 1)
        with sqlite3.connect(self.store.db_path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM participants').fetchone()[0], 1)

    def test_review_does_not_read_session_directory_outside_data_root(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (outside / 'answers.json').write_text('{"secret": "outside"}', encoding='utf-8')
        participant = self.store.create_participant({'name': '示例'})
        self.store.create_session(participant['id'], 'outside-session', str(outside))
        detail = self.store.get_session('outside-session')
        self.assertIsNone(detail['answers'])
        self.assertTrue(detail['review_warnings'])

    def test_intake_audio_and_webm_video_are_indexed_with_correct_types(self):
        self.write('answers.json', {'session_id': 'example-session', 'status': 'stopped', 'questions': [],
                                   'intake_questions': [{'segments': [{'audio_path': 'audio/name.wav'}]}]})
        (self.directory / 'audio').mkdir()
        (self.directory / 'audio/name.wav').write_bytes(b'RIFF')
        (self.directory / 'video').mkdir()
        (self.directory / 'video/session.webm').write_bytes(b'webm')
        detail = self.store.sync_answers_json(self.directory)
        types = {item['path']: item['mime_type'] for item in detail['media']}
        self.assertEqual(types['example-session/audio/name.wav'], 'audio/wav')
        self.assertEqual(types['example-session/video/session.webm'], 'video/webm')


if __name__ == '__main__':
    unittest.main()
