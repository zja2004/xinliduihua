"""School-scoped records accompanying the portable clinical session.

Only an explicitly selected roster entry can link sessions across visits. Human
reviewers supply interpretations, confidence and risk levels. Scores remain
versioned observations from the original questionnaire.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import hashlib
import json
import re
import uuid

from .clinical_store import utc_now

DIMENSIONS = ('学习焦虑', '人际', '孤独情绪', '身体睡眠', '行为风险', '家庭支持')
LAYERS = ('量表结论', 'AI观察线索', '人工确认')
CONFIDENCE = ('待核实', '低', '中', '高')
LEVELS = ('预防', '关注', '风险', '高危')
REFERRAL_STEPS = ('draft', 'approved', 'sent', 'received', 'archived')

# These explicit disclosures request a human check. An unrecognized statement
# remains eligible for a manual alert and never produces a "safe" determination.
_SIGNALS = re.compile(
    r'(?:我(?:现在|已经|真的|又|刚才|有点|很)?(?:不想活(?:着)?了?|活不下去|'
    r'想(?:要)?(?:自杀|自残|寻死|结束(?:自己的|我的)?生命|伤害自己|弄伤自己)|'
    r'准备(?:自杀|自残|伤害自己)|要(?:自杀|自残)|(?:刚刚?|已经)?伤害了自己|'
    r'正在伤害自己|有(?:伤害自己|自杀|自残)的(?:想法|念头)|想死(?:了)?)|'
    r'^不想活了|有人(?:正在)?(?:打我|伤害我|威胁我)|我(?:现在|正在)?(?:被打了|被人打了|被人伤害)|'
    r'我(?:现在|正在)?有危险|(?:我(?:现在|真的)?需要|请)(?:大人)?(?:马上|立即)?救救我|^救救我)'
)


def risk_signal(text):
    if not isinstance(text, str):
        return None
    for clause in re.split(r'[。！？!？，,；;\n]', text[:10000]):
        clause = re.sub(r'\s+', '', clause)
        if _SIGNALS.search(clause):
            return '学生表述含需立即人工复核的安全线索'
    return None


def _text(value, field, limit=120, required=True):
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise ValueError(f'{field}应为文字')
    result = value.strip()
    if len(result) > limit or (required and not result):
        raise ValueError(f'{field}不能为空且最多 {limit} 字' if required else f'{field}最多 {limit} 字')
    return result


def _date(value):
    value = _text(value, '排期日期', 10)
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError as exc:
        raise ValueError('日期格式应为 YYYY-MM-DD') from exc
    return value


def _choice(value, choices, field):
    if value not in choices:
        raise ValueError(f'{field}可选值：' + '、'.join(choices))
    return value


def _id():
    return uuid.uuid4().hex


def _rows(db, sql, args):
    return [dict(r) for r in db.execute(sql, args)]


def _one(db, table, school, obj_id):
    # table is exclusively a literal supplied by the repository methods.
    row = db.execute(f'SELECT * FROM {table} WHERE school_id=? AND id=?',
                     (school, _text(obj_id, '记录编号'))).fetchone()
    if row is None:
        raise ValueError('本校无此记录')
    return dict(row)


def _event(db, school, kind, obj_id, action, actor, note, now):
    db.execute('INSERT INTO school_history(school_id,kind,object_id,action,actor,note,created_at) VALUES(?,?,?,?,?,?,?)',
               (school, kind, obj_id, action, actor, note, now))


def _ensure_owner(db, school, session):
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='portal_session_scope'").fetchone()
    if found:
        owner = db.execute('SELECT school_id FROM portal_session_scope WHERE session_id=?', (session,)).fetchone()
        if owner is None or owner[0] != school:
            raise ValueError('会话未分配给本校')
    else:
        raise ValueError('请先初始化工作台会话归属')
    if not db.execute('SELECT 1 FROM sessions WHERE id=?', (session,)).fetchone():
        raise ValueError('会话尚未保存')


def _enrolled(db, school, student, term):
    _one(db, 'school_students', school, student)
    row = db.execute('SELECT class_name FROM school_enrollments WHERE school_id=? AND student_id=? AND term=?',
                     (school, student, term)).fetchone()
    if not row:
        raise ValueError('学生未登记到该学期')
    return row[0]


SCHEMA = """
CREATE TABLE IF NOT EXISTS school_students(
 id TEXT PRIMARY KEY, school_id TEXT NOT NULL, student_code TEXT NOT NULL, name TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(school_id,student_code));
CREATE TABLE IF NOT EXISTS school_enrollments(
 school_id TEXT NOT NULL, student_id TEXT NOT NULL REFERENCES school_students(id), term TEXT NOT NULL,
 class_name TEXT NOT NULL, PRIMARY KEY(school_id,student_id,term));
CREATE INDEX IF NOT EXISTS school_enrollment_class ON school_enrollments(school_id,term,class_name);
CREATE TABLE IF NOT EXISTS school_sessions(
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), school_id TEXT NOT NULL,
 student_id TEXT NOT NULL REFERENCES school_students(id), term TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'in_progress', created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 summary TEXT NOT NULL DEFAULT '',scoring_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS school_sessions_student ON school_sessions(school_id,student_id,created_at);
CREATE TABLE IF NOT EXISTS school_plans(
 id TEXT PRIMARY KEY, school_id TEXT NOT NULL,class_name TEXT NOT NULL,term TEXT NOT NULL,
 due_date TEXT NOT NULL, assignee TEXT NOT NULL,created_at TEXT NOT NULL, UNIQUE(school_id,class_name,term));
CREATE TABLE IF NOT EXISTS school_appointments(
 id TEXT PRIMARY KEY,school_id TEXT NOT NULL,plan_id TEXT NOT NULL REFERENCES school_plans(id),
 student_id TEXT NOT NULL REFERENCES school_students(id),status TEXT NOT NULL DEFAULT 'scheduled',
 due_date TEXT NOT NULL,assignee TEXT NOT NULL,session_id TEXT UNIQUE REFERENCES sessions(id),
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL, UNIQUE(plan_id,student_id));
CREATE TABLE IF NOT EXISTS school_evidence(
 id TEXT PRIMARY KEY, school_id TEXT NOT NULL, student_id TEXT NOT NULL REFERENCES school_students(id),
 term TEXT NOT NULL,session_id TEXT REFERENCES sessions(id), dimension TEXT NOT NULL,layer TEXT NOT NULL,
 source TEXT NOT NULL,statement TEXT NOT NULL,confidence TEXT NOT NULL,author TEXT NOT NULL,
 confirmed_by TEXT NOT NULL DEFAULT '',confirm_note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS school_observation_sources(
 evidence_id TEXT PRIMARY KEY REFERENCES school_evidence(id), school_id TEXT NOT NULL,session_id TEXT NOT NULL,
 passage_id TEXT NOT NULL,quote TEXT NOT NULL,model TEXT NOT NULL,method TEXT NOT NULL,requested_by TEXT NOT NULL,
 review_status TEXT NOT NULL DEFAULT 'pending',UNIQUE(school_id,session_id,passage_id,quote));
CREATE TABLE IF NOT EXISTS school_alerts(
 id TEXT PRIMARY KEY,school_id TEXT NOT NULL,student_id TEXT REFERENCES school_students(id),
 session_id TEXT REFERENCES sessions(id),term TEXT NOT NULL DEFAULT '',level TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'open',summary TEXT NOT NULL,source TEXT NOT NULL,
 clue TEXT NOT NULL DEFAULT '',stage TEXT NOT NULL DEFAULT '',dedup TEXT,author TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(school_id,session_id,dedup));
CREATE INDEX IF NOT EXISTS school_alert_queue ON school_alerts(school_id,status,updated_at);
CREATE TABLE IF NOT EXISTS school_referrals(
 id TEXT PRIMARY KEY,school_id TEXT NOT NULL,student_id TEXT NOT NULL REFERENCES school_students(id),
 alert_id TEXT NOT NULL REFERENCES school_alerts(id),term TEXT NOT NULL,receiver TEXT NOT NULL,
 level TEXT NOT NULL,summary TEXT NOT NULL,resource TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',
 receipt TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS school_tasks(
 id TEXT PRIMARY KEY,school_id TEXT NOT NULL,student_id TEXT REFERENCES school_students(id),
 term TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL,title TEXT NOT NULL,due_date TEXT NOT NULL DEFAULT '',
 assignee TEXT NOT NULL,session_id TEXT UNIQUE REFERENCES sessions(id),status TEXT NOT NULL DEFAULT 'open',
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS school_history(
 id INTEGER PRIMARY KEY,school_id TEXT NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,
 action TEXT NOT NULL,actor TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS school_history_object ON school_history(school_id,kind,object_id,id);
"""


class SchoolOps:
    def __init__(self, clinical_store):
        self.store = clinical_store
        with self.store._connect() as db:
            db.executescript(SCHEMA)

    def list_students(self, school_id, term='', class_name=''):
        school = _text(school_id, '学校')
        with self.store._connect() as db:
            return self._students(db, school, term, class_name)

    @staticmethod
    def _students(db, school, term='', class_name=''):
        return _rows(db, """SELECT s.*, e.term, e.class_name FROM school_students s
            JOIN school_enrollments e ON s.id=e.student_id AND s.school_id=e.school_id
            WHERE s.school_id=? AND (?='' OR e.term=?) AND (?='' OR e.class_name=?)
            ORDER BY e.term DESC,e.class_name,s.student_code""", (school, term, term, class_name, class_name))

    def mutate(self, school_id, action, payload, actor):
        handlers = {'student': self._student, 'plan': self._plan, 'appointment': self._appointment,
                    'evidence': self._evidence, 'confirm': self._confirm, 'dismiss': self._dismiss, 'alert': self._alert,
                    'alert_update': self._alert_update, 'referral': self._referral,
                    'referral_update': self._referral_update, 'task': self._task, 'task_update': self._task_update}
        if not isinstance(payload, dict) or action not in handlers:
            raise ValueError('无效的学校工作台操作')
        school = _text(school_id, '学校')
        author = _text(actor, '操作人')
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return handlers[action](db, school, payload, author, utc_now())

    def _student(self, db, school, p, actor, now):
        code = _text(p.get('student_code'), '校内学号', 80)
        name = _text(p.get('name'), '姓名或代号')
        cls = _text(p.get('class_name'), '班级')
        term = _text(p.get('term'), '学期')
        row = db.execute('SELECT id,name FROM school_students WHERE school_id=? AND student_code=?', (school, code)).fetchone()
        student = row['id'] if row else _id()
        if row and row['name'] != name and p.get('id') != student:
            raise ValueError('学号已登记其他姓名，请核对学号；更正姓名请指定原学生编号')
        db.execute('''INSERT INTO school_students(id,school_id,student_code,name,created_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(school_id,student_code) DO UPDATE SET name=excluded.name''', (student, school, code, name, now))
        db.execute('''INSERT INTO school_enrollments VALUES(?,?,?,?) ON CONFLICT(school_id,student_id,term)
                   DO UPDATE SET class_name=excluded.class_name''', (school, student, term, cls))
        # Move unstarted assignments if a student transfers classes mid-term. Completed
        # encounters retain their historical class snapshot through the original plan.
        db.execute("""DELETE FROM school_appointments WHERE school_id=? AND student_id=? AND status='scheduled'
                   AND session_id IS NULL AND plan_id IN (SELECT id FROM school_plans WHERE school_id=? AND term=? AND class_name<>?)""",
                   (school, student, school, term, cls))
        self._populate(db, school, term, cls, now)
        _event(db, school, 'student', student, 'enroll', actor, f'{term} {cls}', now)
        return {**_one(db, 'school_students', school, student), 'term':term, 'class_name':cls}

    @staticmethod
    def _populate(db, school, term, cls, now):
        plan = db.execute('SELECT * FROM school_plans WHERE school_id=? AND term=? AND class_name=?',
                          (school, term, cls)).fetchone()
        if not plan:
            return
        students = db.execute("""SELECT student_id FROM school_enrollments e WHERE e.school_id=? AND term=? AND class_name=?
            AND NOT EXISTS(SELECT 1 FROM school_appointments a JOIN school_plans p ON p.id=a.plan_id
            WHERE a.school_id=e.school_id AND a.student_id=e.student_id AND p.term=e.term AND a.status='completed')""",
                              (school, term, cls))
        for row in students:
            student = row['student_id']
            # Plans can be created after a visit has already been archived. Reuse
            # that term's completed encounter unless an earlier plan counted it.
            archived = db.execute("""SELECT s.session_id FROM school_sessions s
                  WHERE s.school_id=? AND s.student_id=? AND s.term=? AND s.status='completed'
                  AND NOT EXISTS(SELECT 1 FROM school_appointments a WHERE a.session_id=s.session_id)
                  ORDER BY s.created_at,s.session_id LIMIT 1""", (school, student, term)).fetchone()
            session = archived['session_id'] if archived else None
            state = 'completed' if session else 'scheduled'
            db.execute("""INSERT OR IGNORE INTO school_appointments(id,school_id,plan_id,student_id,
                       due_date,assignee,status,session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (_id(), school, plan['id'], student, plan['due_date'], plan['assignee'],
                        state, session, now, now))

    def _plan(self, db, school, p, actor, now):
        cls = _text(p.get('class_name'), '班级')
        term = _text(p.get('term'), '学期')
        due = _date(p.get('due_date'))
        assignee = _text(p.get('assignee') or actor,'负责人')
        if not db.execute('SELECT 1 FROM school_enrollments WHERE school_id=? AND term=? AND class_name=?',
                          (school,term,cls)).fetchone():
            raise ValueError('请先登记该班该学期名册')
        found = db.execute('SELECT * FROM school_plans WHERE school_id=? AND term=? AND class_name=?',
                           (school,term,cls)).fetchone()
        if found and (found['due_date'],found['assignee']) != (due,assignee):
            raise ValueError('该班本学期已有计划；请从既有任务调整学生排期')
        plan = found['id'] if found else _id()
        if not found:
            db.execute('INSERT INTO school_plans VALUES(?,?,?,?,?,?,?)', (plan,school,cls,term,due,assignee,now))
            _event(db,school,'plan',plan,'dispatch',actor,f'{term} {cls}',now)
        self._populate(db,school,term,cls,now)
        return _one(db,'school_plans',school,plan)

    def _appointment(self, db, school, p, actor, now):
        row = _one(db,'school_appointments',school,p.get('id'))
        if row['status'] != 'scheduled':
            raise ValueError('已完成面访不能改期')
        due = _date(p.get('due_date') or row['due_date'])
        assignee = _text(p.get('assignee') or row['assignee'],'负责人')
        db.execute('UPDATE school_appointments SET due_date=?,assignee=?,updated_at=? WHERE school_id=? AND id=?',
                   (due,assignee,now,school,row['id']))
        _event(db,school,'appointment',row['id'],'schedule',actor,f'{due} {assignee}',now)
        return _one(db,'school_appointments',school,row['id'])

    def link_session(self, school_id, student_id, session_id, term, appointment_id=None):
        school = _text(school_id,'学校')
        session = _text(session_id,'会话编号')
        term = _text(term,'学期')
        now = utc_now()
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _ensure_owner(db,school,session)
            cls = _enrolled(db,school,student_id,term)
            existing = db.execute('SELECT * FROM school_sessions WHERE session_id=?', (session,)).fetchone()
            if existing and (existing['school_id'],existing['student_id'],existing['term']) != (school,student_id,term):
                raise ValueError('该访谈已有学生或学期归属')
            apt = None
            if appointment_id:
                apt = _one(db,'school_appointments',school,appointment_id)
                plan = _one(db,'school_plans',school,apt['plan_id'])
                if apt['student_id'] != student_id or plan['term'] != term or apt['session_id'] not in (None,session):
                    raise ValueError('排期和本场归属不符')
            else:
                apt = db.execute("""SELECT a.* FROM school_appointments a JOIN school_plans p ON a.plan_id=p.id
                    WHERE a.school_id=? AND p.class_name=? AND p.term=? AND student_id=?
                    AND a.status='scheduled' AND (a.session_id IS NULL OR a.session_id=?)""",
                    (school,cls,term,student_id,session)).fetchone()
            started = db.execute('SELECT started_at FROM sessions WHERE id=?',(session,)).fetchone()[0]
            db.execute("""INSERT OR IGNORE INTO school_sessions
                (session_id,school_id,student_id,term,created_at,updated_at) VALUES(?,?,?,?,?,?)""",
                (session,school,student_id,term,started,now))
            db.execute('UPDATE school_alerts SET student_id=?,term=? WHERE school_id=? AND session_id=? AND student_id IS NULL',
                       (student_id,term,school,session))
            if apt:
                db.execute('UPDATE school_appointments SET session_id=?,updated_at=? WHERE school_id=? AND id=?',
                           (session,now,school,apt['id']))
            return {'session_id':session,'student_id':student_id,'term':term,'school_id':school}

    def sync_session(self, school_id, session_id, answers_dict):
        school = _text(school_id,'学校')
        sid = _text(session_id,'会话编号')
        data = answers_dict
        if not isinstance(data,dict) or data.get('session_id') not in (None, sid):
            raise ValueError('访谈数据与会话编号不符')
        status = _choice(data.get('status','in_progress'),('in_progress','completed','stopped','error'),'会话状态')
        scoring = {k:data.get(k) for k in ('instrument','instrument_version','scoring','task_scoring')}
        summary = str(data.get('overall_summary') or '')[:20000]
        now = utc_now()
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _ensure_owner(db,school,sid)
            link = db.execute('SELECT * FROM school_sessions WHERE school_id=? AND session_id=?',(school,sid)).fetchone()
            if not link:
                return None
            db.execute('UPDATE school_sessions SET status=?,summary=?,scoring_json=?,updated_at=? WHERE school_id=? AND session_id=?',
                       (status,summary,json.dumps(scoring,ensure_ascii=False),now,school,sid))
            if status == 'completed':
                db.execute("UPDATE school_appointments SET status='completed',updated_at=? WHERE school_id=? AND session_id=?",
                           (now,school,sid))
                db.execute("""INSERT OR IGNORE INTO school_tasks(
                    id,school_id,student_id,term,kind,title,assignee,session_id,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (_id(),school,link['student_id'],link['term'],'report','复核本次访谈报告','心理老师',sid,now,now))
            elif status in ('stopped','error'):
                db.execute("""UPDATE school_appointments SET session_id=NULL,updated_at=?
                           WHERE school_id=? AND session_id=? AND status='scheduled'""", (now,school,sid))
            return {'session_id':sid,'status':status}

    def _evidence(self, db, school, p, actor, now):
        student = _text(p.get('student_id'),'学生编号')
        term = _text(p.get('term'),'学期')
        _enrolled(db,school,student,term)
        session = p.get('session_id') or None
        if session and not db.execute("""SELECT 1 FROM school_sessions WHERE school_id=? AND student_id=?
                   AND term=? AND session_id=?""",(school,student,term,session)).fetchone():
            raise ValueError('证据须来自同一学生本学期的会话')
        dim = _choice(p.get('dimension'),DIMENSIONS,'维度')
        layer = _choice(p.get('layer'),LAYERS,'证据层')
        confidence = _choice(p.get('confidence') or '待核实',CONFIDENCE,'可信程度')
        source = _text(p.get('source'),'来源',500)
        statement = _text(p.get('statement'),'观察／原话',3000)
        eid = _id()
        db.execute("""INSERT INTO school_evidence(id,school_id,student_id,term,session_id,dimension,
                    layer,source,statement,confidence,author,confirmed_by,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(eid,school,student,term,session,dim,layer,source,
                     statement,confidence,actor,actor if layer=='人工确认' else '',now))
        _event(db,school,'evidence',eid,'add',actor,source,now)
        return _one(db,'school_evidence',school,eid)

    def suggest(self, school_id, session_id, actor, generate):
        """Teacher-initiated local extraction. No write transaction spans model inference."""
        from .school_observations import MODEL_NAME, SPEC_VERSION, passages, request_candidates

        school = _text(school_id, '学校')
        sid = _text(session_id, '会话编号')
        actor = _text(actor, '老师')
        with self.store._connect() as db:
            _ensure_owner(db, school, sid)
            linked = db.execute('SELECT student_id,term,status FROM school_sessions WHERE school_id=? AND session_id=?',
                                (school, sid)).fetchone()
            if not linked or linked['status'] not in ('completed', 'stopped'):
                raise ValueError('请先将已结束的访谈关联到本校学生')
        answers = self.store.get_session(sid).get('answers') or {}
        if answers.get('session_id') != sid:
            raise ValueError('原始回答与关联会话不一致')
        selected, omitted = passages(answers)
        if not selected:
            raise ValueError('没有可提取的已完成开放回答，短回答、过长片段或跳过项可由老师手工查阅')
        if not callable(generate):
            raise ValueError('Psyche-R1 未就绪，暂不能提取')
        candidates, rejected = request_candidates(generate, selected)
        added, suggestions = 0, []
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _ensure_owner(db, school, sid)
            current = db.execute('SELECT student_id,term,status FROM school_sessions WHERE school_id=? AND session_id=?',
                                 (school, sid)).fetchone()
            if not current or tuple(current) != tuple(linked):
                raise ValueError('访谈归属或状态已经变化，请刷新档案')
            now = utc_now()
            for passage_id, dimension, quote in candidates:
                existing = db.execute("""SELECT evidence_id FROM school_observation_sources WHERE school_id=?
                    AND session_id=? AND passage_id=? AND quote=?""", (school, sid, passage_id, quote)).fetchone()
                if existing:
                    eid = existing['evidence_id']
                else:
                    task, ordinal = passage_id.split(':')
                    source = f'{MODEL_NAME} · 开放题 {task[-1]} · 原话片段 {ordinal} · {SPEC_VERSION}'
                    params = dict(student_id=linked['student_id'], term=linked['term'], session_id=sid,
                                  dimension=dimension, layer='AI观察线索', source=source, statement=quote, confidence='待核实')
                    eid = self._evidence(db, school, params, MODEL_NAME, now)['id']
                    db.execute("""INSERT INTO school_observation_sources(evidence_id,school_id,session_id,passage_id,
                        quote,model,method,requested_by) VALUES(?,?,?,?,?,?,?,?)""",
                        (eid, school, sid, passage_id, quote, MODEL_NAME, SPEC_VERSION, actor))
                    _event(db, school, 'evidence', eid, 'suggest', actor, '按原话定位，待老师核查语境和主题', now)
                    added += 1
                suggestions.append(_one(db, 'school_evidence', school, eid))
        return {'added': added, 'existing': len(suggestions)-added, 'source': MODEL_NAME,
                'suggestions': suggestions, 'rejected': rejected, 'omitted': omitted}

    def _dismiss(self, db, school, p, actor, now):
        row = _one(db, 'school_evidence', school, p.get('id'))
        note = _text(p.get('note'), '退回理由', 2000)
        result = db.execute("""UPDATE school_observation_sources SET review_status='dismissed'
            WHERE school_id=? AND evidence_id=? AND review_status='pending'""", (school, row['id']))
        if result.rowcount != 1:
            raise ValueError('只能退回待核查的模型摘录')
        _event(db, school, 'evidence', row['id'], 'dismiss', actor, note, now)
        return {'id':row['id'], 'review_status':'dismissed'}

    def _confirm(self, db, school, p, actor, now):
        row = _one(db,'school_evidence',school,p.get('id'))
        note = _text(p.get('note'),'核查记录',2000)
        conf = _choice(p.get('confidence') or row['confidence'],CONFIDENCE,'可信程度')
        db.execute('UPDATE school_evidence SET confirmed_by=?,confirm_note=?,confidence=? WHERE school_id=? AND id=?',
                   (actor,note,conf,school,row['id']))
        review = db.execute('SELECT review_status FROM school_observation_sources WHERE school_id=? AND evidence_id=?',
                            (school, row['id'])).fetchone()
        if review and review['review_status'] == 'dismissed':
            raise ValueError('退回的摘录不能直接确认，可重新查看原话、手工登记依据')
        if review:
            db.execute("UPDATE school_observation_sources SET review_status='confirmed' WHERE school_id=? AND evidence_id=?",
                       (school, row['id']))
        _event(db,school,'evidence',row['id'],'confirm',actor,f'{conf} · {note}',now)
        return _one(db,'school_evidence',school,row['id'])

    def capture_risk(self, school_id, session_id, text, stage, event_id=None):
        signal = risk_signal(text)
        if not signal:
            return None
        school = _text(school_id,'学校')
        session = _text(session_id,'会话编号')
        stage = _text(stage or '对话','访谈阶段',100)
        clue = text[:3000]
        dedup = hashlib.sha256((str(event_id) if event_id is not None else stage+'\n'+clue).encode()).hexdigest()
        now = utc_now()
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _ensure_owner(db,school,session)
            found = db.execute('SELECT * FROM school_alerts WHERE school_id=? AND session_id=? AND dedup=?',
                               (school,session,dedup)).fetchone()
            if found:
                return dict(found)
            link = db.execute('SELECT student_id,term FROM school_sessions WHERE school_id=? AND session_id=?',
                              (school,session)).fetchone()
            aid = _id()
            db.execute("""INSERT INTO school_alerts(id,school_id,student_id,session_id,term,summary,
                       source,clue,stage,dedup,author,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (aid,school,link[0] if link else None,session,link[1] if link else '',signal,
                        '原话线索·待人工复核',clue,stage,dedup,'访谈程序',now,now))
            _event(db,school,'alert',aid,'open','访谈程序','原话线索登记到本机老师工作台',now)
            return _one(db,'school_alerts',school,aid)

    def _alert(self, db, school, p, actor, now):
        student = _text(p.get('student_id'),'学生编号')
        term = _text(p.get('term'),'学期')
        _enrolled(db,school,student,term)
        level = _choice(p.get('level') or '',('',)+LEVELS,'人工分级')
        summary = _text(p.get('summary'),'关注原因',2500)
        sid = p.get('session_id') or None
        if sid and not db.execute('SELECT 1 FROM school_sessions WHERE school_id=? AND student_id=? AND term=? AND session_id=?',
                                  (school,student,term,sid)).fetchone():
            raise ValueError('关联会话与学生或学期不符')
        aid = _id()
        db.execute("""INSERT INTO school_alerts(id,school_id,student_id,session_id,term,level,summary,
                   source,author,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (aid,school,student,sid,term,level,summary,'老师登记',actor,now,now))
        _event(db,school,'alert',aid,'open',actor,summary,now)
        return _one(db,'school_alerts',school,aid)

    def _alert_update(self, db, school, p, actor, now):
        row = _one(db,'school_alerts',school,p.get('id'))
        status = _choice(p.get('status') or row['status'],('open','acknowledged','closed'),'预警状态')
        if row['status']=='closed' or (row['status']=='acknowledged' and status=='open'):
            raise ValueError('该状态不能倒退，已有新的线索时请登记新预警')
        level = _choice(p.get('level') or row['level'],('',)+LEVELS,'人工分级')
        note = _text(p.get('note'),'处置依据',2500)
        db.execute('UPDATE school_alerts SET level=?,status=?,updated_at=? WHERE school_id=? AND id=?',
                   (level,status,now,school,row['id']))
        _event(db,school,'alert',row['id'],status,actor,f'{level or "未分级"} · {note}',now)
        return _one(db,'school_alerts',school,row['id'])

    def _referral(self, db, school, p, actor, now):
        alert = _one(db,'school_alerts',school,p.get('alert_id'))
        if not alert['student_id'] or not alert['level']:
            raise ValueError('请先关联学生并由老师确认预警等级')
        receiver = _text(p.get('receiver'),'接收方',200)
        summary = _text(p.get('summary'),'档案摘要',3000)
        resource = _text(p.get('resource'),'建议资源',700)
        rid = _id()
        db.execute("""INSERT INTO school_referrals(id,school_id,student_id,alert_id,term,receiver,level,summary,
            resource,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (rid,school,alert['student_id'],alert['id'],alert['term'],receiver,alert['level'],summary,resource,now,now))
        _event(db,school,'referral',rid,'draft',actor,'已登记接收方和拟转介摘要',now)
        return _one(db,'school_referrals',school,rid)

    def _referral_update(self, db, school, p, actor, now):
        row = _one(db,'school_referrals',school,p.get('id'))
        status = _choice(p.get('status'),REFERRAL_STEPS,'转介状态')
        if REFERRAL_STEPS.index(status) != REFERRAL_STEPS.index(row['status'])+1:
            raise ValueError('请按审核、人工送出、登记回执、归档的顺序操作')
        note = _text(p.get('note'),'回执或操作记录',2500)
        receipt = note if status=='received' else row['receipt']
        db.execute('UPDATE school_referrals SET status=?,receipt=?,updated_at=? WHERE school_id=? AND id=?',
                   (status,receipt,now,school,row['id']))
        _event(db,school,'referral',row['id'],status,actor,note,now)
        return _one(db,'school_referrals',school,row['id'])

    def _task(self, db, school, p, actor, now):
        student = p.get('student_id') or None
        term = _text(p.get('term'),'学期',required=False)
        if student:
            _enrolled(db,school,student,term)
        due = _date(p['due_date']) if p.get('due_date') else ''
        title = _text(p.get('title'),'待办事项',300)
        assignee = _text(p.get('assignee') or actor,'负责人')
        tid = _id()
        db.execute("""INSERT INTO school_tasks(id,school_id,student_id,term,kind,title,due_date,assignee,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",(tid,school,student,term,'todo',title,due,assignee,now,now))
        _event(db,school,'task',tid,'open',actor,title,now)
        return _one(db,'school_tasks',school,tid)

    def _task_update(self, db, school, p, actor, now):
        row = _one(db,'school_tasks',school,p.get('id'))
        status = _choice(p.get('status') or 'done',('open','done'),'办理状态')
        note = _text(p.get('note') or ('已复核报告' if row['kind']=='report' else '已办理'),'办理记录',2000)
        if status != row['status']:
            db.execute('UPDATE school_tasks SET status=?,updated_at=? WHERE school_id=? AND id=?',
                       (status,now,school,row['id']))
            _event(db,school,'task',row['id'],status,actor,note,now)
        return _one(db,'school_tasks',school,row['id'])

    def alerts_feed(self, school_id):
        school = _text(school_id, '学校')
        with self.store._connect() as db:
            pending = _rows(db, """SELECT id,created_at,level,status,student_id,session_id
                FROM school_alerts WHERE school_id=? AND status='open' ORDER BY created_at DESC""", (school,))
        return {'alerts': pending, 'count': len(pending)}

    def workspace(self, school_id, term=''):
        school = _text(school_id,'学校')
        term = _text(term,'学期',required=False)
        with self.store._connect() as db:
            db.execute('BEGIN')
            students = self._students(db,school,term)
            completed = {(r['student_id'], r['term']) for r in db.execute(
                """SELECT DISTINCT student_id,term FROM school_sessions WHERE school_id=?
                   AND (?='' OR term=?) AND status='completed'""", (school, term, term))}
            plans = _rows(db,'SELECT * FROM school_plans WHERE school_id=? AND (?=\'\' OR term=?) ORDER BY due_date',
                          (school,term,term))
            appointments = _rows(db,"""SELECT a.*,p.term,p.class_name,s.name,s.student_code FROM school_appointments a
                JOIN school_plans p ON p.id=a.plan_id AND p.school_id=a.school_id
                JOIN school_students s ON s.id=a.student_id AND s.school_id=a.school_id
                WHERE a.school_id=? AND (?='' OR p.term=?) ORDER BY a.due_date,s.student_code""",(school,term,term))
            alerts = _rows(db,"""SELECT * FROM school_alerts WHERE school_id=? AND (?='' OR term=? OR term='')
                ORDER BY CASE WHEN status='closed' THEN 1 ELSE 0 END,updated_at DESC""", (school,term,term))
            tidied = {}
            for key,table in (('referrals','school_referrals'),('tasks','school_tasks')):
                tidied[key] = _rows(db,f"SELECT * FROM {table} WHERE school_id=? AND (?='' OR term=?) ORDER BY updated_at DESC",
                                    (school,term,term))
        today = date.today().isoformat()
        for a in appointments:
            a['overdue'] = a['status']=='scheduled' and a['due_date'] < today
        required = {(s['id'],s['term']) for s in students}
        count = len(required & completed)
        coverage = {'required':len(required),'completed':count,'coverage_rate': count/len(required) if required else None,
                    'overdue':sum(1 for a in appointments if a['overdue']),
                    'unscheduled':len(required - completed - {(a['student_id'],a['term']) for a in appointments})}
        for plan in plans:
            matched = [a for a in appointments if a['plan_id']==plan['id']]
            plan.update(required=len(matched),completed=sum(a['status']=='completed' for a in matched))
        return {'students':students,'plans':plans,'appointments':appointments,'alerts':alerts,**tidied,'coverage':coverage,
                'evidence_dimensions':list(DIMENSIONS),'layers':list(LAYERS),'confidence_levels':list(CONFIDENCE),
                'risk_levels':list(LEVELS)}

    def student_file(self, school_id, student_id):
        school = _text(school_id,'学校')
        with self.store._connect() as db:
            db.execute('BEGIN')
            student = _one(db,'school_students',school,student_id)
            groups = {}
            for key,table,order in (('enrollments','school_enrollments','term DESC'),('sessions','school_sessions','created_at'),
                    ('evidence','school_evidence','created_at'),('alerts','school_alerts','created_at'),
                    ('referrals','school_referrals','created_at')):
                groups[key] = _rows(db,f'SELECT * FROM {table} WHERE school_id=? AND student_id=? ORDER BY {order}',(school,student_id))
            extraction = {r['evidence_id']:dict(r) for r in db.execute("""SELECT o.evidence_id,o.passage_id,
                o.model,o.method,o.requested_by,o.review_status FROM school_observation_sources o JOIN school_evidence e
                ON o.school_id=e.school_id AND o.evidence_id=e.id WHERE e.school_id=? AND e.student_id=?""", (school,student_id))}
            for item in groups['evidence']:
                if item['id'] in extraction:
                    item.update(extraction[item['id']])
            groups['dismissed_evidence'] = [i for i in groups['evidence'] if i.get('review_status') == 'dismissed']
            groups['evidence'] = [i for i in groups['evidence'] if i.get('review_status') != 'dismissed']
            groups['scale_reports'] = []
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_assessments'").fetchone():
                reports = _rows(db, """SELECT a.session_id,a.instrument,a.version,a.result,a.created_at,l.term,
                    r.reviewer,r.evidence_ref FROM school_sessions l JOIN report_assessments a
                    ON a.school_id=l.school_id AND a.session_id=l.session_id
                    JOIN report_reviews r USING(school_id,instrument,version)
                    WHERE l.school_id=? AND l.student_id=? AND r.decision='approved' ORDER BY a.created_at""",
                    (school,student_id))
                for report in reports:
                    values = json.loads(report.pop('result'))
                    groups['scale_reports'].append({**report, **values})
                    if values.get('interpretable') is not True:
                        continue
                    for d in values.get('dimensions', []):
                        if d.get('name') not in DIMENSIONS:
                            continue
                        groups['evidence'].append({
                            'id': f"scale:{report['session_id']}:{report['instrument']}:{report['version']}:{d['name']}",
                            'student_id':student_id, 'session_id':report['session_id'], 'term':report['term'],
                            'dimension':d['name'], 'layer':'量表结论', 'source':f"{report['instrument']} {report['version']} · {report['evidence_ref']}",
                            'statement':f"原始分 {d['raw']}，{d['level'] or '无解释分级'}。{values.get('uncertainty','')}",
                            'confidence':values.get('confidence_basis') or '待核实', 'author':report['reviewer'],
                            'confirmed_by':'', 'confirm_note':'', 'read_only':True, 'created_at':report['created_at']})
            history = _rows(db,"""SELECT h.* FROM school_history h WHERE h.school_id=? AND (
                (h.kind='student' AND h.object_id=?) OR EXISTS
                (SELECT 1 FROM school_alerts a WHERE h.kind='alert' AND a.school_id=h.school_id AND a.id=h.object_id AND a.student_id=?)
                OR EXISTS (SELECT 1 FROM school_referrals r WHERE h.kind='referral' AND r.school_id=h.school_id AND r.id=h.object_id AND r.student_id=?)
                OR EXISTS (SELECT 1 FROM school_evidence e WHERE h.kind='evidence' AND e.school_id=h.school_id AND e.id=h.object_id AND e.student_id=?))
                ORDER BY h.created_at,h.id""",(school,student_id,student_id,student_id,student_id))
        terms = {e['term']:{'term':e['term'],'class_name':e['class_name'],'sessions':0,'completed':0,
                            'observations':0,'alerts':0,'referrals':0,'scores':[]} for e in groups['enrollments']}
        for session in groups['sessions']:
            values = json.loads(session.pop('scoring_json'))
            session.update(values)
            term = terms[session['term']]
            term['sessions'] += 1
            term['completed'] += session['status']=='completed'
            for source in ('scoring','task_scoring'):
                data = values.get(source) or {}
                if data.get('complete') is True and isinstance(data.get('total_score'),(int,float)):
                    term['scores'].append({'session_id':session['session_id'],'at':session['created_at'],
                        'instrument':data.get('instrument') or values.get('instrument'),'version':values.get('instrument_version'),
                        'score':data['total_score'],'range':data.get('total_range')})
        for key,metric in (('evidence','observations'),('alerts','alerts'),('referrals','referrals')):
            for item in groups[key]:
                terms[item['term']][metric] += 1
        years = defaultdict(lambda: {'sessions':0,'completed':0,'observations':0,'alerts':0,'referrals':0,'scores':[],'terms':[]})
        for term,r in terms.items():
            # Explicit academic-year terms work directly; calendar spring belongs to the
            # preceding autumn. Unknown labels are kept separate, not silently merged.
            start = re.match(r'^(20\d{2}|19\d{2})(?:[-/—至](?:20|19)?\d{2,4}|年?\s*(春|秋))',term)
            if start:
                year = int(start[1]) - (1 if start[2]=='春' else 0)
                y = f'{year}–{year+1}'
            else:
                y = term
            rec = years[y]
            rec['year']=y
            rec['terms'].append(term)
            rec['scores'].extend(r['scores'])
            for k in ('sessions','completed','observations','alerts','referrals'):
                rec[k]+=r[k]
        timeline = []
        for kind,items,key in (('访谈',groups['sessions'],'session_id'),('证据',groups['evidence'],'id'),
                               ('预警',groups['alerts'],'id'),('转介',groups['referrals'],'id')):
            timeline.extend({'kind':kind,'id':i[key],'at':i['created_at'],'term':i['term']} for i in items)
        timeline.sort(key=lambda e:e['at'],reverse=True)
        return {'student':student,**groups,'history':history,'timeline':timeline,'semesters':list(terms.values()),
                'years':list(years.values()),'dimensions':list(DIMENSIONS),'layers':list(LAYERS)}
