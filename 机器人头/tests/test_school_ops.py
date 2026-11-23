"""Persistent school workflows, including retry and separation between schools."""
import concurrent.futures
import json
import tempfile
import unittest

from orin_voice.clinical_store import ClinicalStore
from orin_voice.school_access import SchoolAccess
from orin_voice.school_ops import SchoolOps, risk_signal


class SchoolOpsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ClinicalStore(temporary.name)
        self.access = SchoolAccess(self.store)
        self.ops = SchoolOps(self.store)
        self.term = '2026秋'
        self.school = 'A'
        self.student = self.enroll(self.school)['id']

    def enroll(self, school, code='01', term='2026秋', cls='一班'):
        return self.ops.mutate(school,'student',dict(student_code=code,name='虚构甲',term=term,class_name=cls),'老师')

    def create_session(self, sid, school='A'):
        participant = self.store.create_participant({'name':'测试代号'})
        self.store.create_session(participant['id'],sid)
        with self.store._connect() as db:
            db.execute('INSERT INTO portal_session_scope VALUES(?,?)',(sid,school))

    def write(self, action, **values):
        return self.ops.mutate(self.school,action,values,'测试老师')

    def test_repeat_session_same_academic_year_and_class_coverage(self):
        self.enroll('A', '02')
        plan = self.write('plan',term=self.term,class_name='一班',due_date='2026-09-01')
        self.assertEqual(plan['id'], self.write('plan',term=self.term,class_name='一班',due_date='2026-09-01')['id'])
        self.create_session('s1')
        self.ops.link_session('A',self.student,'s1',self.term)
        result = {'session_id':'s1','status':'completed','instrument':'PRIMARY-21','instrument_version':'1',
                  'scoring':{'instrument':'PRIMARY-21','total_score':18,'total_range':[0,63],'complete':True}}
        for _ in range(2):
            self.ops.sync_session('A','s1',result)
        ws = self.ops.workspace('A',self.term)
        self.assertEqual(ws['coverage']['coverage_rate'],0.5)
        self.assertEqual(len(ws['tasks']),1)
        self.assertEqual(ws['tasks'][0]['kind'],'report')
        self.enroll('A','03')
        self.assertEqual(len(self.ops.workspace('A',self.term)['appointments']),3)
        self.assertEqual(self.enroll('A',term='2027春')['id'],self.student)
        self.create_session('s2')
        self.ops.link_session('A',self.student,'s2','2027春')
        self.ops.sync_session('A','s2',dict(result,session_id='s2'))
        card = self.ops.student_file('A',self.student)
        self.assertEqual(len(card['sessions']),2)
        self.assertEqual(len(card['semesters']),2)
        self.assertEqual([(y['year'], y['completed']) for y in card['years']], [('2026–2027',2)])
        self.assertEqual(len(card['years'][0]['scores']),2)
        self.assertNotEqual(self.enroll('B')['id'],self.student)
        with self.assertRaises(ValueError):
            self.ops.student_file('B',self.student)
        with self.assertRaises(ValueError):
            self.ops.link_session('B',self.enroll('B')['id'],'s1',self.term)
        self.assertFalse(self.ops.workspace('B',self.term)['tasks'])

    def test_unlinked_risk_is_idempotent_attachable_and_referrals_require_order(self):
        self.create_session('s1')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            alerts=list(pool.map(lambda _:self.ops.capture_risk('A','s1','我现在有伤害自己的想法','tasks','event1'),range(4)))
        alert=alerts[0]
        self.assertEqual(len({a['id'] for a in alerts}),1)
        self.assertIsNone(alert['student_id'])
        self.assertEqual(alert['level'],'')
        self.assertEqual(len(self.ops.workspace('A',self.term)['alerts']),1)
        with self.assertRaises(ValueError):
            self.ops.capture_risk('B','s1','我想自杀','greeting')
        self.ops.link_session('A',self.student,'s1',self.term)
        self.write('alert_update', id=alert['id'], status='acknowledged', note='已人工面谈核实', level='风险')
        r=self.write('referral',alert_id=alert['id'],receiver='测试接收方',summary='合成摘要',resource='校内面谈')
        with self.assertRaises(ValueError):
            self.write('referral_update',id=r['id'],status='received',note='越过审核')
        for status,note in [('approved','已审核'),('sent','人工提交完成'),('received','虚构回执编号甲'),('archived','资料已归档')]:
            self.write('referral_update',id=r['id'],status=status,note=note)
        self.write('alert_update',id=alert['id'],status='closed',note='已记录现场处置与后续安排')
        card=self.ops.student_file('A',self.student)
        self.assertEqual(card['referrals'][0]['receipt'],'虚构回执编号甲')
        self.assertEqual(card['alerts'][0]['session_id'],'s1')
        self.assertEqual(card['alerts'][0]['level'],'风险')
        states=[h['action'] for h in card['history'] if h['kind']=='referral']
        self.assertEqual(states,['draft','approved','sent','received','archived'])
        self.assertIsNone(risk_signal('我不想伤害自己，我准备回教室'))
        self.assertIsNone(risk_signal('考试前有点紧张。'))

    def test_plan_created_after_visit_reuses_completed_record(self):
        self.create_session('completed-before-plan')
        self.ops.link_session('A', self.student, 'completed-before-plan', self.term)
        self.ops.sync_session('A', 'completed-before-plan', {'status':'completed'})
        before = self.ops.workspace('A', self.term)
        self.assertEqual(before['coverage']['coverage_rate'], 1)
        self.assertEqual(before['coverage']['unscheduled'], 0)
        self.write('plan', term=self.term, class_name='一班', due_date='2026-11-01')
        visit = self.ops.workspace('A', self.term)['appointments'][0]
        self.assertEqual(visit['status'], 'completed')
        self.assertEqual(visit['session_id'], 'completed-before-plan')

    def test_model_observations_keep_whole_quotes_and_review_state(self):
        sid = 'model-note'
        self.create_session(sid)
        self.ops.link_session('A', self.student, sid, self.term)
        self.ops.sync_session('A', sid, {'status': 'completed'})
        folder = self.store.root / sid
        folder.mkdir()
        with self.store._connect() as db:
            db.execute('UPDATE sessions SET session_dir=? WHERE id=?',(str(folder),sid))
        original = '这周我没有觉得孤单，同桌常常陪我走回家。'
        tasks = [dict(id='task_reading',status='answered',answer_text='绝不能外送的朗读内容'),
                 dict(id='task_open_2',status='answered',segments=[{'text':original}]),
                 dict(id='task_open_3',status='skipped',answer_text='跳过项的草稿')]
        (folder/'answers.json').write_text(json.dumps(dict(session_id=sid,tasks=tasks,name='保密代号',total_score=63)))
        def model(system, message):
            self.assertNotIn('保密代号', message)
            self.assertNotIn('跳过项', message)
            self.assertNotIn('朗读内容', message)
            self.assertNotIn('63', message)
            passages=json.loads(message)
            self.assertEqual(passages,[{'id':'1','quote':original}])
            key = passages[0]['id']
            return json.dumps({'observations':[
                {'id':key,'dimension':'孤独情绪','quote':'觉得孤单'},
                {'id':key,'dimension':'编造分类','quote':original},
                {'id':'伪造索引','dimension':'人际','quote':original},
                {'id':key,'dimension':'人际','quote':original},
            ]},ensure_ascii=False)
        first = self.ops.suggest('A',sid,'虚构老师',model)
        self.assertEqual((first['added'],first['rejected']),(1,3))
        with self.assertRaises(ValueError):
            self.ops.suggest('B',sid,'外校',lambda *_: self.fail('不应向模型提供外校原话'))
        eid = first['suggestions'][0]['id']
        card = self.ops.student_file('A',self.student)
        self.assertEqual(card['evidence'][0]['confidence'],'待核实')
        self.assertEqual(card['evidence'][0]['statement'],original)
        self.assertEqual(card['evidence'][0]['review_status'],'pending')
        self.write('dismiss',id=eid,note='教师核查后退回分组')
        updated=self.ops.suggest('A',sid,'虚构老师',model)
        self.assertEqual((updated['added'],updated['existing']),(0,1))
        card=self.ops.student_file('A',self.student)
        self.assertEqual(card['evidence'],[])
        self.assertEqual(card['dismissed_evidence'][0]['id'],eid)
        with self.assertRaises(ValueError):
            self.write('confirm',id=eid,note='不能静默撤销退回')
        with self.assertRaises(ValueError):
            self.ops.suggest('A',sid,'虚构老师',lambda *_:'随意生成的观点')
        self.assertEqual(len(self.ops.student_file('A',self.student)['dismissed_evidence']),1)

    def test_unfinished_visit_is_rescheduled_and_manual_evidence_provenance(self):
        self.write('plan',term=self.term,class_name='一班',due_date='2026-10-01')
        for sid in ('s1','s2'):
            self.create_session(sid)
            self.ops.link_session('A',self.student,sid,self.term)
            if sid=='s1':
                self.ops.sync_session('A',sid,{'status':'stopped'})
        appointment=self.ops.workspace('A')['appointments'][0]
        self.assertEqual(appointment['session_id'],'s2')
        self.assertEqual(appointment['status'],'scheduled')
        evidence=self.write('evidence',student_id=self.student,term=self.term,session_id='s2',
                            dimension='人际',layer='AI观察线索',source='面访引用，待老师检查',
                            statement='同学邀请课间一起活动',confidence='待核实')
        self.write('confirm', id=evidence['id'], note='已当面核对',confidence='中')
        card=self.ops.student_file('A',self.student)
        self.assertEqual(card['evidence'][0]['confirmed_by'],'测试老师')
        self.assertEqual(card['evidence'][0]['confidence'],'中')
        other=self.enroll('B')['id']
        with self.assertRaises(ValueError):
            self.write('evidence',student_id=other,term=self.term,dimension='人际',layer='人工确认',
                       source='外校',statement='错误关联',confidence='中')
        with self.assertRaises(ValueError):
            self.ops.mutate('B','confirm',dict(id=evidence['id'],note='跨校'), '虚构老师')


if __name__=='__main__':
    unittest.main()
