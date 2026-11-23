"""HTTP-facing school operations. Roles and school scopes come only from login."""


from .school_access import SchoolAccess
from .school_ops import SchoolOps
from .school_reports import SchoolReports
from .school_content import SchoolContent


def require(actor,*roles):
    if not actor or actor['role'] not in roles:
        raise PermissionError('请使用具有此权限的账号登录')


class SchoolPortal:
    def __init__(self, store, bootstrap=True):
        self.access=SchoolAccess(store)
        self.ops=SchoolOps(store)
        self.reports=SchoolReports(store,self.ops)
        self.content=SchoolContent(store.db_path)
        self.store=store
        if bootstrap: self.access.bootstrap()

    def bind(self,school,student,sid,term,appointment=None):
        if self.access.school_for(sid)!=school:
            raise PermissionError('无权关联此访谈')
        result=self.ops.link_session(school,student,sid,term,appointment or None)
        detail=self.store.get_session(sid)
        self.ops.sync_session(school,sid,detail.get('answers') or {})
        return result

    def dispatch(self,area,action,data,actor,service):
        if not actor: raise PermissionError('请先登录学校工作台')
        role,school,user=actor['role'],actor['school_id'],actor['username']
        if area=='account':
            if action=='password':
                old_token=self.access.login(user,data.get('current'))
                self.access.logout(old_token)
                self.access.account(user,data.get('password'),role,school,actor['allowed_school_ids'],replace=True)
                return {'ok':True}
            require(actor,'school_admin')
            if action=='create' and data.get('role') in ('teacher','principal','reviewer'):
                return self.access.account(data.get('username'),data.get('password'),data['role'],school)
            raise ValueError('不支持此账号操作')
        if area=='reports':
            return self.reports.dispatch(action,data,actor)
        if area=='content':
            require(actor,'teacher','school_admin','reviewer')
            return self.content.call(action,data,school_id=school,actor=user,role=role)
        if area!='ops':
            raise FileNotFoundError('没有这个操作')
        require(actor,'teacher')
        if action=='suggest':
            dialogue=getattr(getattr(service,'robot',None),'dialogue',None)
            if not dialogue or dialogue.config.get('profile')!='psyche' or not dialogue.client:
                raise ValueError('本机 Psyche-R1 尚未就绪，请启动本地模型后重试')
            def generate(system_prompt,user_message):
                with dialogue.lock:
                    if not dialogue.client:
                        raise ValueError('模型已停止，请重新启动')
                    result=dialogue.client.complete(user_message,max_tokens=768,system_prompt=system_prompt)
                return result['reply'] if result else ''
            return self.ops.suggest(school,data.get('session_id'),user,generate)
        if action=='alerts_feed':
            return self.ops.alerts_feed(school)
        if action=='workspace':
            return self.ops.workspace(school,data.get('term',''))
        if action=='sessions':
            return {'sessions':self.access.school_sessions(school)}
        if action=='student_file':
            return self.ops.student_file(school,data.get('student_id'))
        if action=='link':
            return self.bind(school,data.get('student_id'),data.get('session_id'),data.get('term'),data.get('appointment_id'))
        if action=='start':
            term,student=data.get('term'),data.get('student_id')
            eligible=self.ops.list_students(school,term=term)
            enrolled=next((s for s in eligible if s['id']==student),None)
            if not enrolled or not term:
                raise ValueError('请先选择已有学期和在册学生')
            return service.start(data.get('mode','text'),robot=bool(data.get('robot')),workflow=True,
                                 profile={'name':enrolled['name']},school_id=school,
                                 enrollment=(student,term,data.get('appointment_id')))
        return self.ops.mutate(school,action,data,user)
