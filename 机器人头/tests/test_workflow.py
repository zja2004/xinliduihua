import json
import tempfile
import unittest
from pathlib import Path

from orin_voice.service import InterviewService
from orin_voice.settings import ROOT


class PsychologyWorkflowTests(unittest.TestCase):
    def test_profile_dass_tasks_and_teacher_index_are_one_session(self):
        with tempfile.TemporaryDirectory() as directory:
            service = InterviewService(output=directory, questions=ROOT / 'configs/dass21.zh.json')
            state = service.start('text', workflow=True, profile={
                'name': '测试学生', 'height_cm': 170, 'weight_kg': 60,
                'gender': '男', 'age': 18,
            })
            session_id = state['session_id']
            self.assertEqual(state['total_questions'], 25)
            self.assertEqual(state['participant']['name'], '测试学生')
            for number in range(1, 22):
                state = service.action(session_id, 'answer', score=2,
                                       question_id=f'Q{number:02}')
            self.assertEqual(state['stage'], 'tasks')
            self.assertEqual(state['current_question']['id'], 'task_reading')
            for _ in range(4):
                state = service.action(session_id, 'answer', text='测试任务回答',
                                       question_id=state['current_question']['id'])
                self.assertEqual(state['status'], 'in_progress')
                state = service.action(session_id, 'finish',
                                       question_id=state['current_question']['id'])
            self.assertEqual(state['status'], 'completed')
            package = json.loads((Path(directory) / session_id / 'session_package.json').read_text())
            self.assertEqual(package['participant']['name'], '测试学生')
            self.assertEqual(len(package['tasks']), 4)
            self.assertEqual(package['tasks'][0]['kind'], 'reading')
            indexed = service.store.get_session(session_id)
            self.assertEqual(indexed['name'], '测试学生')
            self.assertEqual(len(indexed['tasks']), 4)

    def test_voice_style_intake_and_task_pauses_do_not_advance_early(self):
        with tempfile.TemporaryDirectory() as directory:
            service = InterviewService(output=directory, questions=ROOT / 'configs/dass21.zh.json')
            state = service.start('text', workflow=True)
            sid = state['session_id']
            self.assertEqual(state['stage'], 'intake')
            for text in ['测试学生', '一百七十二厘米', '六十五点五公斤', '女', '十八岁']:
                state = service.action(sid, 'answer', text=text,
                                       question_id=state['current_question']['id'])
            self.assertEqual(state['stage'], 'assessment')
            self.assertEqual(state['participant']['height_cm'], '172')
            self.assertEqual(state['participant']['weight_kg'], '65.5')
            self.assertEqual(state['participant']['age'], '18')
            for _ in range(21):
                state = service.action(sid, 'skip', question_id=state['current_question']['id'])
            for text in ['有一回，北风跟太阳在那儿争论谁的本领大。', '说着说着，来了一个过路的。']:
                state = service.action(sid, 'answer', text=text,
                                       question_id=state['current_question']['id'])
                self.assertEqual(state['current_question']['id'], 'task_reading')
            state = service.action(sid, 'finish', question_id='task_reading')
            self.assertEqual(state['current_question']['id'], 'task_open_1')
            self.assertEqual(len(state['tasks'][0]['segments']), 2)

    def test_task_text_is_exactly_the_requested_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            service = InterviewService(output=directory, questions=ROOT / 'configs/dass21.zh.json')
            self.assertEqual(service.store.list_tasks()[0]['prompt'], (
                '有一回，北风跟太阳在那儿争论谁的本领大。说着说着，来了一个过路的，'
                '身上穿了一件厚袍子。他们俩就商量好了，说谁能先叫这个过路的把他的袍子'
                '脱下来，就算是他的本领大。北风就使劲吹起来，拼命地吹。可是，他吹得'
                '越厉害，那个人就把他的袍子裹得越紧。到末了儿，北风没辙了，只好就算'
                '了。一会儿，太阳出来一晒，那个人马上就把袍子脱了下来。所以，北风不得'
                '不承认，还是太阳比他的本领大。'))
            self.assertEqual([task['prompt'] for task in service.store.list_tasks()[1:]], [
                '请描述一下，你昨天过的怎么样?',
                '请描述一下，现在回想最近一周最开心的记忆?',
                '请描述一下，现在回想最近一周最悲伤的记忆?',
            ])
