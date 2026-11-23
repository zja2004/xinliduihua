from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path

from orin_voice.clinical_store import ClinicalStore
from orin_voice.school_ops import SchoolOps
from orin_voice import school_exchange
from orin_voice.school_reports import SchoolReports


def user(role, school='A', **props):
    return dict(role=role, school_id=school, username=role+'-'+school, **props)


def fixture_rule(code='RESEARCH-DEMO'):
    return dict(instrument=code, version='v1', title='虚构功能核对题单', owner='虚构测试权利方',
                license_ref='临时测试许可', norms='虚构年龄范围', validation_ref='测试文档',
                review_body='测试审校', threshold_basis='测试依据', uncertainty='仅验证代码',
                confidence_basis='虚构材料，无现实适用性', min_age=8, max_age=10,
                choices=['甲','乙','丙','丁'],
                questions=[dict(id='A', text='虚构甲题', reverse=True),dict(id='B',text='虚构乙题')],
                cutoffs=[1,3,5], dimensions=[{'name':'虚构校验','items':['B'],'invalid_at_or_above':3}])


class ReportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ClinicalStore(directory.name)
        self.ops = SchoolOps(self.store)
        self.reports = SchoolReports(self.store,self.ops)

    def create_encounter(self,school,sid,age=9):
        p=self.store.create_participant({'name':'姓名必须隐藏','age':age})
        path=self.store.root/sid
        path.mkdir()
        detail={'instrument':'PRIMARY-21','participant':{'name':'姓名必须隐藏'},'term':'A-private',
                'scoring':{'complete':True,'total_score':42,'subscales':{'mood':{'complete':True,'raw_score':14},
                                                                          '私密主题':{'raw_score':4}}},
                'task_scoring':{'complete':False,'total_score':None},'questions':[{'id':'Q01','score':2,
                              'status':'answered','answer_text':'私密陈述'}]}
        (path/'answers.json').write_text(json.dumps(detail))
        self.store.create_session(p['id'],sid,str(path))
        student='student-'+sid
        date='2026-11-01T00:00:00+00:00'
        with self.store._connect() as db:
            db.execute('INSERT INTO school_students VALUES(?,?,?,?,?)',(student,school,sid,'姓名必须隐藏',date))
            db.execute('INSERT INTO school_sessions(session_id,school_id,student_id,term,status,created_at,updated_at) '
                       'VALUES(?,?,?,?,?,?,?)',(sid,school,student,'A-private','completed',date,date))
        return student

    def run_action(self,action,role,data,school='A'):
        return self.reports.dispatch(action,data,user(role,school))

    def approve(self):
        rule=fixture_rule()
        self.run_action('draft','school_admin',rule)
        review={'instrument':rule['instrument'],'version':'v1','decision':'approved','evidence_ref':'TEST','notes':'仅供测试'}
        self.run_action('review','reviewer',review)
        return rule

    def test_revision_control_and_validity(self):
        sid='example-1'
        self.create_encounter('A',sid)
        self.create_encounter('A','old',age=13)
        rule=fixture_rule()
        self.run_action('draft','school_admin',rule)
        base=dict(instrument=rule['instrument'],version='v1')
        assess=dict(base,session_id=sid,answers={'A':0,'B':1})
        review=dict(base,decision='approved',evidence_ref='人工确认文档',notes='测试')
        with self.assertRaises(ValueError):
            self.run_action('assess','teacher',assess)
        with self.assertRaises(PermissionError):
            self.reports.dispatch('review',review,dict(user('reviewer'),username='school_admin-A'))
        with self.assertRaises(ValueError):
            self.run_action('draft','school_admin',fixture_rule('primary-21'))
        built=json.loads((Path(__file__).resolve().parents[1]/'configs/primary21.zh.json').read_text())
        clone=fixture_rule('renamed2026')
        clone['questions']=built['questions']
        clone.pop('dimensions')
        with self.assertRaises(ValueError):
            self.run_action('draft','school_admin',clone)
        self.run_action('review','reviewer',review)
        with self.assertRaises(ValueError):
            self.run_action('review','reviewer',review)
        with self.assertRaises(ValueError):
            self.run_action('assess','teacher',dict(assess,session_id='old'))
        with self.assertRaises(ValueError):
            self.run_action('assess','teacher',dict(assess,answers={'A':False,'B':1}))
        out=self.run_action('assess','teacher',assess)
        self.assertEqual((out['total'],out['level'],out['dimensions'][0]['raw']),(4,'风险',1))
        self.assertEqual(self.run_action('assess','teacher',assess),out)
        with self.assertRaises(ValueError):
            self.run_action('assess','teacher',dict(assess,answers={'A':2,'B':0}))
        self.create_encounter('A','invalid')
        halted=self.run_action('assess','teacher',dict(assess,session_id='invalid',answers={'A':1,'B':3}))
        self.assertFalse(halted['interpretable'])
        self.assertIsNone(halted['level'])

    def test_parent_projection_scope_and_roles(self):
        sid='synthetic-identity'
        self.create_encounter('A',sid)
        self.approve()
        self.run_action('assess','teacher',dict(instrument='RESEARCH-DEMO',version='v1',session_id=sid,
                                                 answers={'A':2,'B':0}))
        parent=self.run_action('parent','teacher',{'session_id':sid})
        self.assertEqual(parent['questionnaire']['total'],42)
        self.assertEqual(parent['supplemental_scales'][0]['review_band'],'关注')
        encoded=json.dumps(parent,ensure_ascii=False)
        for private in (sid,'姓名必须隐藏','私密陈述','私密主题','student-','A-private','虚构','TEST','Q01'):
            self.assertNotIn(private,encoded)
        teacher=self.run_action('teacher','teacher',{'session_id':sid})
        self.assertEqual(teacher['supplemental_scales'][0]['evidence_ref'],'TEST')
        for role in ('principal','reviewer','school_admin','district_admin'):
            with self.assertRaises(PermissionError):
                self.run_action('teacher',role,{'session_id':sid})
        with self.assertRaises(ValueError):
            self.run_action('parent','teacher',{'session_id':sid,'school_id':'A'},school='B')
        self.assertEqual(self.run_action('sessions','teacher',{},'B')['sessions'],[])

    def test_dashboard_comparison_resources_and_protected_small_counts(self):
        students=[self.create_encounter('A',f'id-{i}') for i in range(10)]
        self.create_encounter('B','one-person')
        date='2026-11-01'
        with self.store._connect() as db:
            db.execute('INSERT INTO school_plans VALUES(?,?,?,?,?,?,?)',('pl','A','班','秋',date,'x',date))
            for j,student in enumerate(students):
                db.execute('INSERT INTO school_appointments VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (f'due{j}','A','pl',student,'completed' if j<9 else 'scheduled',date,'x',None,date,date))
            for j,student in enumerate(students[:5]):
                db.execute('INSERT INTO school_alerts(id,school_id,student_id,level,summary,source,author,created_at,updated_at) '
                           'VALUES(?,?,?,?,?,?,?,?,?)',(f'al{j}','A',student,'关注' if j<4 else '高危','个案绝密','人工','老师',date,date))
        self.run_action('set_resources','principal',{'month':'2026-11','counselors':2,'rooms':1,'hours':10})
        query={'from_month':'2026-11','to_month':'2026-12','school_id':'B'}
        dash=self.run_action('dashboard','principal',query)
        self.assertEqual([r['school'] for r in dash['schools']],['A'])
        self.assertEqual(dash['schools'][0]['resources'][0]['hours'],10)
        line=dash['schools'][0]['months'][0]
        self.assertEqual((line['interviewed'],line['due'],line['alerts']),(10,10,5))
        self.assertIsNone(line['completed']) # 9 of 10 discloses the one outstanding child
        self.assertIsNone(line['coverage_pct'])
        self.assertIsNone(line['risk_distribution'])
        d=self.reports.dispatch('dashboard',query,user('district_admin',allowed_school_ids=['B','A']))
        self.assertEqual([s['school'] for s in d['schools']],['A','B'])
        self.assertIsNone(d['regional_months'][0]['interviewed'])
        self.assertNotIn('个案绝密',str(d))
        with self.assertRaises(PermissionError):
            self.run_action('dashboard','district_admin',query)
        with self.assertRaises(ValueError):
            self.run_action('dashboard','principal',dict(query,to_month='2035-01'))


    def test_offline_exchange_signed_scoped_idempotent_and_local_precedence(self):
        # These stores represent physically disconnected devices.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        receiving_store=ClinicalStore(directory.name)
        receiver=SchoolReports(receiving_store,SchoolOps(receiving_store))
        actor=user('district_admin','district',allowed_school_ids=['A'])
        for j in range(5):
            self.create_encounter('A',f'protected-{j}')
        query={'from_month':'2026-11','to_month':'2026-11'}
        with self.assertRaises(ValueError):
            self.run_action('export_packet','principal',query)
        pem=school_exchange.keygen(self.store,'A')
        self.assertEqual(school_exchange.keygen(self.store,'A'),pem)
        self.assertEqual(school_exchange.private_path(self.store,'A').stat().st_mode & 0o777,0o600)
        fingerprint=school_exchange.finger(pem)
        packet=self.run_action('export_packet','principal',query)
        serialized=json.dumps(packet,ensure_ascii=False)
        for marker in ('姓名必须隐藏','A-private','protected-','student_id'):
            self.assertNotIn(marker,serialized)
        with self.assertRaises(ValueError):
            receiver.dispatch('import_packet',packet,actor)
        with self.assertRaises(ValueError):
            school_exchange.trust(receiving_store,'A',pem,'0'*64)
        school_exchange.trust(receiving_store,'A',pem,fingerprint)
        with self.assertRaises(PermissionError):
            receiver.dispatch('import_packet',packet,user('district_admin',allowed_school_ids=['B']))
        tainted=deepcopy(packet)
        tainted['payload']['months'][0]['interviewed']=10
        with self.assertRaises(ValueError):
            receiver.dispatch('import_packet',tainted,actor)
        extra=deepcopy(packet)
        extra['payload']['months'][0]['note']='forbidden'
        with self.assertRaises(ValueError):
            receiver.dispatch('import_packet',extra,actor)
        self.assertEqual(receiver.dispatch('import_packet',packet,actor)['updated'],1)
        self.assertEqual(receiver.dispatch('import_packet',packet,actor)['duplicates'],1)
        feed=receiver.dispatch('dashboard',query,actor)
        school=feed['schools'][0]
        self.assertEqual(school['source'],'已验签离线汇总')
        self.assertEqual(school['provenance'][0]['fingerprint'],fingerprint)
        self.assertEqual(feed['regional_months'][0]['interviewed'],5)
        for j in range(5,10):
            self.create_encounter('A',f'protected-{j}')
        revised=self.run_action('export_packet','school_admin',query)
        self.assertEqual(receiver.dispatch('import_packet',revised,actor)['updated'],1)
        self.assertEqual(receiver.dispatch('import_packet',packet,actor)['older'],1)
        self.assertEqual(receiver.dispatch('dashboard',query,actor)['regional_months'][0]['interviewed'],10)
        with receiving_store._connect() as db:
            db.execute('INSERT INTO school_students VALUES(?,?,?,?,?)',('local','A','1','local','2026-11-04'))
        # Local owns this school: never combine the separately signed count with local case data.
        school=receiver.dispatch('dashboard',query,actor)['schools'][0]
        self.assertEqual(school['source'],'本库实时')
        self.assertIsNone(school['months'][0]['interviewed'])
