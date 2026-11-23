import copy
import json
import tempfile
import unittest
from pathlib import Path

from orin_voice.clinical_store import ClinicalStore
from orin_voice.school_content import FORMAT, PILOT, SchoolContent
from orin_voice.school_ops import SchoolOps


class ContentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        store = ClinicalStore(Path(self.temp.name))
        self.ops = SchoolOps(store)
        self.c = SchoolContent(store.db_path)
        self.seed = json.loads(PILOT.read_text())['items']

    def runas(self, act, p=None, role='teacher', actor='老师A', school='甲'):
        return self.c.call(act,p or {},school_id=school,actor=actor,role=role)

    def test_staff_review_does_not_grant_classroom_records(self):
        for role in ('reviewer','school_admin'):
            for action in ('roster','class_detail','activity','finish_class'):
                with self.assertRaises(PermissionError):
                    self.runas(action,{'class_id':'any'},role=role)

    def publish(self, item, previous=None):
        row = self.runas('save',{'item':item,'previous':previous})
        self.runas('submit',{'id':row['id']})
        return self.runas('review',{'id':row['id'],'decision':'publish','note':'演示核对意见'},
                          'reviewer','测试复核员')

    def test_draft_version_withdraw_and_tenant_scope(self):
        item=self.seed[0]
        phrase=item['body']['keywords'][0]
        self.assertIsNone(self.c.approved_response('甲',phrase))
        v=self.runas('save',{'item':item})
        key=v['id']
        self.runas('submit',{'id':key})
        for role,actor in [('school_admin','老师A'),('teacher','其他老师')]:
            with self.assertRaises(PermissionError):
                self.runas('review',{'id':key,'decision':'publish','note':'意见'},role,actor)
        self.runas('review',{'id':key,'decision':'publish','note':'测试意见'},'reviewer','审核B')
        expected=item['body']['reply']+item['body']['followup']
        self.assertEqual(self.c.approved_response('甲',phrase),expected)
        self.assertIsNone(self.c.approved_response('乙',phrase))
        for act,p in [('get',{'id':key}),('history',{'id':key}),('export',{'ids':[key]})]:
            with self.assertRaises(ValueError):
                self.runas(act,p,school='乙')
        new=copy.deepcopy(item)
        new['body']['reply']='谢谢你告诉我。'
        self.assertEqual(self.publish(new,key)['version'],2)
        self.assertNotEqual(self.c.approved_response('甲',phrase),expected)
        rows=self.runas('history',{'id':key})['items']
        self.assertEqual([r['state'] for r in rows],['published','retired'])
        self.runas('withdraw',{'id':rows[0]['id'],'note':'重新评估'},'reviewer','审核B')
        self.assertIsNone(self.c.approved_response('甲',phrase))
        with self.assertRaises(PermissionError):
            self.runas('list',role='principal')

    def test_content_pack_is_atomic_idempotent_and_review_is_local(self):
        result=self.runas('pilot')
        self.assertEqual(result['added'],20)
        self.assertEqual(self.runas('pilot')['added'],0)
        pilot_lessons=[i for i in self.seed if i['kind']=='lesson']
        self.assertEqual([i['body']['week'] for i in pilot_lessons],list(range(1,17)))
        self.assertEqual(len({i['body']['slides'][1]['text'] for i in pilot_lessons}),16)
        pack={'format':FORMAT,'items':[self.seed[0],{'kind':'script'}]}
        with self.assertRaises(ValueError):
            self.runas('import',{'package':pack},school='乙')
        self.assertFalse(self.runas('list',school='乙')['items'])
        r=self.publish(self.seed[1])
        pack=self.runas('export',{'ids':[r['id']]})
        self.assertIn('reviewer',pack['items'][0]['provenance'])
        imp=self.runas('import',{'package':pack},school='乙')
        v=self.runas('get',{'id':imp['ids'][0]},school='乙')
        self.assertEqual(v['state'],'draft')
        self.assertIsNone(v['reviewer'])
        self.assertEqual(v['item']['provenance']['reviewer'],'测试复核员')
        self.assertIsNone(self.c.approved_response('乙',self.seed[1]['body']['keywords'][0]))

    def test_published_class_learning_with_scoped_roster(self):
        generated=self.runas('generate',{'kind':'lesson','stage':'初中','topic':'考试压力'})['item']
        self.assertIn('同伴对话',generated['body']['slides'][1]['text'])
        v=self.publish(generated)
        p={'student_code':'T1','name':'甲同学','term':'秋','class_name':'一班'}
        a=self.ops.mutate('甲','student',p,'老师')
        b=self.ops.mutate('乙','student',p,'老师')
        room=self.runas('start_class',{'id':v['id'],'term':'秋','class_name':'一班'})['class_id']
        with self.assertRaises(ValueError):
            self.runas('class_detail',{'class_id':room},school='乙')
        with self.assertRaises(ValueError):
            self.runas('record',{'class_id':room,'student_id':b['id'],'kind':'completed'})
        self.runas('record',{'class_id':room,'kind':'page','page':1})
        self.runas('record',{'class_id':room,'student_id':a['id'],'kind':'interaction','note':'提出一个可执行计划'})
        self.runas('record',{'class_id':room,'student_id':a['id'],'kind':'completed'})
        self.runas('finish_class',{'class_id':room,'feedback':'能用自己的话复述','observation':'下次增加讨论时间'})
        d=self.runas('class_detail',{'class_id':room})
        self.assertEqual(d['class']['item']['body'],generated['body'])
        self.assertEqual([e['kind'] for e in d['events']],['page','interaction','completed'])
        self.assertEqual([m['id'] for m in d['members']],[a['id']])
        with self.assertRaises(ValueError):
            self.runas('record',{'class_id':room,'kind':'page','page':2})
        self.assertFalse(self.runas('classes',school='乙')['classes'])
