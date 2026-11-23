"""Verify the deployed route protections and links across a full school visit."""
import http.cookiejar
from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from orin_voice.school_portal import SchoolPortal
from orin_voice.service import InterviewService
from orin_voice.web import handler_for


class StaffHTTPTest(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service=service=InterviewService(output=temporary.name)
        service.portal=SchoolPortal(service.store,bootstrap=False)
        for name,role,school in [('teacher-a','teacher','A'),('teacher-b','teacher','B'),('principal','principal','A')]:
            service.portal.access.account(name,'synthetic-only-passphrase',role,school)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(service))
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def client(self,username=None):
        jar=http.cookiejar.CookieJar()
        session=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        def call(path,body=None,code=200):
            req=urllib.request.Request(self.url+path,data=None if body is None else json.dumps(body).encode(),
                                       headers={'Content-Type':'application/json'})
            try:
                response=session.open(req,timeout=10)
            except urllib.error.HTTPError as error:
                response=error
            with response:
                payload=json.loads(response.read())
                self.assertEqual(response.status,code,(path,payload))
                return payload
        if username:
            self.assertEqual(call('/api/school/login',{'username':username,'password':'synthetic-only-passphrase'})['actor']['username'],username)
        return call

    def test_named_handoff_pause_and_authorized_access(self):
        staff=self.client('teacher-a')
        student=staff('/api/school/ops/student',dict(student_code='T1',name='虚构学生',term='2026秋',class_name='一班'))
        state=staff('/api/school/ops/start',dict(student_id=student['id'],term='2026秋',mode='text',handoff=True))
        sid=state['session_id']
        self.assertIsNone(staff('/api/school/me')['actor'])
        path='/api/sessions/'+sid
        self.assertEqual(staff('/api/status')['active_session_id'],sid)
        staff(path+'/answers.json',code=403)
        state=staff(path+'/actions',dict(action='answer',text='我现在有危险'))
        self.assertTrue(state['safety_hold'])
        self.assertEqual(state['stage'],'intake')
        state=staff(path+'/actions',dict(action='answer',text='2分'))
        self.assertEqual(state['completed_count'],0)
        staff(path+'/actions',dict(action='stop'))
        staff('/api/teacher/sessions',code=403)
        other=self.client('teacher-b')
        other(path,code=403)
        self.assertFalse(other('/api/teacher/sessions')['sessions'])
        principal=self.client('principal')
        principal('/api/teacher/sessions/'+sid,code=403)
        teacher=self.client('teacher-a')
        alerts=teacher('/api/school/ops/alerts_feed',{})
        self.assertEqual(alerts['count'],1)
        self.assertEqual(teacher('/api/teacher/sessions/'+sid)['id'],sid)
        self.assertEqual(teacher('/api/school/ops/student_file',{'student_id':student['id']})['alerts'][0]['session_id'],sid)

    def test_automatic_wake_only_visible_to_paired_device(self):
        paired_key=self.service.portal.access.pair_device('A')
        state=self.service.start('text',True,wake_event={'trigger':'voice'},workflow=True,school_id='A')
        anon=self.client()
        self.assertIsNone(anon('/api/status')['active_session_id'])
        req=urllib.request.Request(self.url+'/api/status',headers={'Cookie':'station='+paired_key})
        with urllib.request.urlopen(req) as response:
            self.assertEqual(json.load(response)['active_session_id'],state['session_id'])
            self.assertTrue(response.getheader('Set-Cookie').startswith('visit='))
