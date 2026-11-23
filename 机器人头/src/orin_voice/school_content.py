"""School-scoped, offline teaching materials and reviewed conversation prompts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

PILOT = Path(__file__).resolve().parents[2] / 'configs/school_content/pilot.json'
FORMAT = 'xinyu-content-v1'
STAGES = ('小学', '初中', '高中')
KINDS = ('script', 'article', 'lesson')
REVIEWERS = {'reviewer', 'school_admin'}
GUIDE = ('项目原创教研草稿。学龄背景参考教育部《中小学心理健康教育指导纲要（2012年修订）》 '
         'https://www.moe.gov.cn/srcsite/A06/s3325/201212/t20121211_145679.html')


def _text(value, label, limit=500, optional=False):
    if value is None and optional:
        return ''
    if not isinstance(value, str) or (not optional and not value.strip()) or len(value) > limit:
        raise ValueError(f'{label}需填写有效文字，最多 {limit} 字')
    return value.strip()


def _count(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label}应为 {low} 到 {high} 的整数')
    return value


def _choice(value, choices, label):
    if value not in choices:
        raise ValueError(f'{label}无效')
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _clean_item(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('body'), dict):
        raise ValueError('每个条目需要内容正文')
    item = {'kind': _choice(raw.get('kind'), KINDS, '材料类型'),
            'stage': _choice(raw.get('stage'), STAGES, '学龄'),
            'title': _text(raw.get('title'), '标题', 120),
            'topic': _text(raw.get('topic'), '主题', 100),
            'source': _text(raw.get('source'), '内容来源', 1000)}
    b = raw['body']
    if item['kind'] == 'script':
        keys = b.get('keywords')
        if not isinstance(keys, list) or not 1 <= len(keys) <= 20:
            raise ValueError('请填写 1 至 20 个匹配短语')
        keys = list(dict.fromkeys(_text(k, '匹配短语', 50) for k in keys))
        if any(len(k) < 2 for k in keys):
            raise ValueError('匹配短语至少两个字')
        item['body'] = {'keywords': keys, 'reply': _text(b.get('reply'), '回应', 180),
                        'followup': _text(b.get('followup'), '追问', 180)}
    else:
        slides = b.get('slides')
        if not isinstance(slides, list) or not 1 <= len(slides) <= 24:
            raise ValueError('请提供 1 至 24 页课件')
        pages = []
        for s in slides:
            if not isinstance(s, dict):
                raise ValueError('课件页格式无效')
            pages.append({'title': _text(s.get('title'), '页标题', 100),
                          'text': _text(s.get('text'), '页正文', 2400)})
        body = {'slides': pages}
        if item['kind'] == 'lesson':
            body.update(grade=_text(b.get('grade'), '年级', 40),
                        week=_count(b.get('week'), '周次', 1, 53),
                        minutes=_count(b.get('minutes'), '课时分钟', 1, 180))
            for key, label in (('objective', '目标'), ('materials', '材料'), ('interaction', '互动'),
                               ('feedback', '反馈'), ('observation', '观察要点')):
                body[key] = _text(b.get(key), label, 1200)
        item['body'] = body
    # Imported provenance describes the sending site's record. It never gives
    # the receiving site's draft a local approval or a right to publish.
    if 'provenance' in raw:
        origin = raw['provenance']
        if not isinstance(origin, dict):
            raise ValueError('内容包原审校记录格式无效')
        item['provenance'] = {
            'version': _count(origin.get('version'), '原版本', 1, 1000000),
            'reviewer': _text(origin.get('reviewer'), '原审校账号', 200),
            'reviewed_at': _text(origin.get('reviewed_at'), '原审校时间', 80),
            'review_note': _text(origin.get('review_note'), '原审校意见', 1600),
        }
    return item


def _generate(p):
    stage = _choice(p.get('stage'), STAGES, '学龄')
    topic = _text(p.get('topic'), '主题', 70)
    kind = _choice(p.get('kind'), ('lesson', 'article'), '内容类型')
    # Each theme family provides a concrete example and method, adjustable before review.
    themes = [
        (('考试', '学习', '作业', '计划'), '明天要交一项作业，故事里的同学担心来不及。',
         '把要完成的事写成三张行动卡。按先后排序，选出现在能开始的一张。', '和同伴交换卡片，检查第一步是否足够清楚。'),
        (('欺凌', '欺负', '求助', '安全'), '课间有人反复挡住一位同学的路，他想找到大人帮忙。',
         '用虚构校园地图指出两处可以找到老师的地点，写下一句求助开头。', '轮流演练说出发生的地点和希望得到的帮助，允许旁听。'),
        (('亲子', '家庭', '父母'), '故事里的同学想向家人解释一件事，大人却正忙着。',
         '在纸上写下想说的事、自己的感受和一个具体请求。挑选双方有空的时间。', '两人轮流演练一句邀请和一种愿意倾听的回应。'),
        (('同伴', '朋友', '沟通', '合作'), '两位同学对小组活动的分工有不同想法。',
         '给每个人物画一个对话框，把对方的需要用自己的话复述一次。', '两人各写一个可试方案，交流哪个兼顾双方的需要。'),
        (('情绪', '情感', '压力', '放松'), '故事里的同学第一次上台，心里有好几种感觉。',
         '给人物选两个感受词，找一处能支持选择的情节，列出可求助的人。', '比较同一件事在不同角色眼中有什么不同。'),
    ]
    chosen = next((t[1:] for t in themes if any(k in topic for k in t[0])), None)
    if chosen:
        story, activity, exchange = chosen
    else:
        story = f'为“{topic}”准备一张虚构情境卡，写出地点、两位角色和他们面对的一件具体事情。'
        activity = f'读情境卡，围绕“{topic}”为角色写下困难、两个做法及各自可能产生的结果。'
        exchange = '互换卡片，用人物当时能得到的资源解释自己的选择。'
    formats = {'小学': ('用图卡给人物选一个表情，说一句想说的话。','四年级'),
               '初中': ('写一段同伴对话，比较两位角色可能各自在意的事。','初一'),
               '高中': ('对照两种策略的短期和长期影响，写明选择的前提。','高一')}
    adaptation, grade = formats[stage]
    slides = [{'title':'读一读','text':story}, {'title':'试一试','text':activity+' '+adaptation},
              {'title':'一起想','text':exchange}, {'title':'带走一个做法','text':'回到故事，写下你愿意为人物建议的一步及理由。'}]
    body = {'slides':slides}
    if kind == 'lesson':
        body.update(grade=_text(p.get('grade') or grade, '年级', 40), week=_count(p.get('week',1),'周次',1,53),
                    minutes=35, objective=f'在虚构情境中围绕“{topic}”表达需要并选择一个行动。',
                    materials='情境卡、纸和笔', interaction=exchange, feedback=slides[-1]['text'],
                    observation='记录学生给出的不同理由和一处需要调整的活动。')
    return _clean_item({'kind':kind, 'stage':stage, 'title':f'{topic} · '+('课堂活动' if kind=='lesson' else '读与练'),
                        'topic':topic, 'source':GUIDE, 'body':body})


_SCHEMA = """
CREATE TABLE IF NOT EXISTS content_versions(
 id TEXT PRIMARY KEY, school_id TEXT NOT NULL, series TEXT NOT NULL, version INTEGER NOT NULL,
 kind TEXT NOT NULL, stage TEXT NOT NULL, title TEXT NOT NULL, topic TEXT NOT NULL,
 entry_json TEXT NOT NULL, state TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL,
 reviewer TEXT, reviewed_at TEXT, review_note TEXT, UNIQUE(school_id,series,version));
CREATE INDEX IF NOT EXISTS content_school_state ON content_versions(school_id,state,kind);
CREATE UNIQUE INDEX IF NOT EXISTS content_one_published ON content_versions(school_id,series) WHERE state='published';
CREATE TABLE IF NOT EXISTS content_imports(school_id TEXT NOT NULL,digest TEXT NOT NULL,item_id TEXT NOT NULL,
 PRIMARY KEY(school_id,digest));
CREATE TABLE IF NOT EXISTS content_classes(
 id TEXT PRIMARY KEY,school_id TEXT NOT NULL,item_id TEXT NOT NULL REFERENCES content_versions(id),
 term TEXT NOT NULL,class_name TEXT NOT NULL,teacher TEXT NOT NULL,started_at TEXT NOT NULL,
 ended_at TEXT, feedback TEXT, observation TEXT);
CREATE TABLE IF NOT EXISTS content_events(
 id INTEGER PRIMARY KEY,school_id TEXT NOT NULL,item_id TEXT NOT NULL REFERENCES content_versions(id),
 class_id TEXT REFERENCES content_classes(id),student_id TEXT,actor TEXT NOT NULL,
 kind TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS content_class_events ON content_events(school_id,class_id,id);
"""


class SchoolContent:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        with closing(self._db()) as db:
            db.executescript(_SCHEMA)

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def call(self, action, payload, *, school_id, actor, role):
        if role not in {'teacher','reviewer','school_admin'}:
            raise PermissionError('此账号没有校内教研内容权限')
        school = _text(school_id, '学校', 200)
        actor = _text(actor, '操作账号', 200)
        if not isinstance(payload, dict):
            raise ValueError('请求格式无效')
        read = {'list','get','history','generate','export','classes','class_detail','roster','activity'}
        writes = {'save','submit','review','withdraw','pilot','import','start_class','record','finish_class'}
        if role != 'teacher' and action in {'classes','class_detail','roster','activity','start_class','record','finish_class'}:
            raise PermissionError('课堂名册和学习记录仅向本校老师开放')
        if action not in read | writes:
            raise ValueError('未知内容操作')
        with closing(self._db()) as db, db:
            if action in writes:
                db.execute('BEGIN IMMEDIATE')
            return self._dispatch(db, action, payload, school, actor, role)

    @staticmethod
    def _row(db, school, item_id):
        row = db.execute('SELECT * FROM content_versions WHERE school_id=? AND id=?',
                         (school, _text(item_id,'内容编号',100))).fetchone()
        if row is None:
            raise ValueError('内容不存在')
        return row

    @staticmethod
    def _view(row, full=False):
        dto = {k:row[k] for k in ('id','series','version','kind','stage','title','topic','state','author',
                                    'created_at','reviewer','reviewed_at','review_note')}
        if full:
            dto['item'] = json.loads(row['entry_json'])
        return dto

    def _save(self, db, school, actor, item, previous=None):
        version = 1
        series = uuid.uuid4().hex
        if previous:
            row = self._row(db,school,previous)
            series, version = row['series'], row['version']+1
            original = json.loads(row['entry_json']).get('provenance')
            if original:
                item['provenance'] = original
            latest = db.execute('SELECT MAX(version) FROM content_versions WHERE school_id=? AND series=?',
                                (school,series)).fetchone()[0]
            if version != latest+1:
                raise ValueError('此版本已更新，请从最新版本创建修订')
            if row['state'] in ('draft','pending'):
                db.execute("UPDATE content_versions SET state='superseded' WHERE id=?", (row['id'],))
        key = uuid.uuid4().hex
        db.execute('''INSERT INTO content_versions
          (id,school_id,series,version,kind,stage,title,topic,entry_json,state,author,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,'draft',?,?)''',
          (key,school,series,version,item['kind'],item['stage'],item['title'],item['topic'],_json(item),actor,_now()))
        return key

    def _import(self, db, school, actor, pack):
        if not isinstance(pack,dict) or pack.get('format') != FORMAT:
            raise ValueError('请选择心语离线内容包 JSON')
        raw = pack.get('items')
        if not isinstance(raw,list) or not 1<=len(raw)<=100:
            raise ValueError('内容包应含 1 至 100 个条目')
        inserted = []
        for item in map(_clean_item,raw):
            digest = hashlib.sha256(_json(item).encode()).hexdigest()
            if db.execute('SELECT 1 FROM content_imports WHERE school_id=? AND digest=?',(school,digest)).fetchone():
                continue
            key = self._save(db,school,actor,item)
            db.execute('INSERT INTO content_imports VALUES(?,?,?)',(school,digest,key))
            inserted.append(key)
        return {'added':len(inserted),'skipped':len(raw)-len(inserted),'ids':inserted}

    def approved_response(self, school_id, text):
        """Return published primary-school copy for an identified school's greeting."""
        if not isinstance(school_id,str) or not school_id or not isinstance(text,str) or not text.strip():
            return None
        with closing(self._db()) as db:
            rows = db.execute("""SELECT entry_json FROM content_versions WHERE school_id=?
              AND kind='script' AND stage='小学' AND state='published' ORDER BY reviewed_at DESC,id""", (school_id,))
            best = None
            for row in rows:
                item = json.loads(row[0])
                body = item['body']
                lengths = [len(k) for k in body['keywords'] if k in text]
                if lengths:
                    rank = (item['topic']=='自伤风险',max(lengths))
                    if best is None or rank > best[0]:
                        best = (rank,body['reply']+body['followup'])
        return best[1] if best else None

    def _dispatch(self, db, action, p, school, actor, role):
        if action == 'list':
            where, args = ['school_id=?'], [school]
            for k, valid in (('stage',STAGES),('kind',KINDS),
                             ('state',('draft','pending','published','rejected','retired','superseded'))):
                if p.get(k):
                    where.append(k+'=?')
                    args.append(_choice(p[k], valid,k))
            if p.get('query'):
                query = _text(p['query'],'搜索',100)
                where.append('(instr(title,?)>0 OR instr(topic,?)>0)')
                args += [query,query]
            rows = db.execute('SELECT * FROM content_versions WHERE '+' AND '.join(where)+' ORDER BY created_at DESC,id',args)
            return {'items':[self._view(r) for r in rows], 'can_review':role in REVIEWERS}
        if action == 'generate':
            return {'item':_generate(p)}
        if action == 'save':
            key = self._save(db,school,actor,_clean_item(p.get('item')),p.get('previous'))
            return self._view(self._row(db,school,key),True)
        if action in ('get','history','submit','review','withdraw'):
            row = self._row(db,school,p.get('id'))
            key = row['id']
            if action == 'get':
                return self._view(row,True)
            if action == 'history':
                rows=db.execute('SELECT * FROM content_versions WHERE school_id=? AND series=? ORDER BY version DESC',(school,row['series']))
                return {'items':[self._view(r) for r in rows]}
            if action == 'submit':
                if row['state'] != 'draft':
                    raise ValueError('只能提交草稿')
                db.execute("UPDATE content_versions SET state='pending' WHERE id=?", (key,))
            if action in ('review','withdraw'):
                if role not in REVIEWERS:
                    raise PermissionError('需要审校权限')
                note = _text(p.get('note'),'审校意见',1600)
                if action == 'review':
                    if row['author'] == actor:
                        raise PermissionError('需由另一位审校人员核对本版本')
                    if row['state'] != 'pending':
                        raise ValueError('内容当前不在待审状态')
                    decision = _choice(p.get('decision'),('publish','reject'),'审校结果')
                    state = 'published' if decision=='publish' else 'rejected'
                    if state == 'published':
                        db.execute("""UPDATE content_versions SET state='retired' WHERE school_id=?
                            AND series=? AND state='published'""",(school,row['series']))
                    db.execute('UPDATE content_versions SET state=?,reviewer=?,reviewed_at=?,review_note=? WHERE id=?',
                               (state,actor,_now(),note,key))
                else:
                    if row['state'] != 'published':
                        raise ValueError('只能撤回已发布内容')
                    db.execute("UPDATE content_versions SET state='retired' WHERE id=?", (key,))
                    self._event(db,school,key,None,None,actor,'withdraw',{'note':note})
            return self._view(self._row(db,school,key),True)
        if action in ('pilot','import'):
            pack = json.loads(PILOT.read_text('utf-8')) if action=='pilot' else p.get('package')
            return self._import(db,school,actor,pack)
        if action == 'export':
            ids = p.get('ids')
            if not isinstance(ids,list) or not 1<=len(ids)<=100:
                raise ValueError('请选择 1 至 100 个已发布条目')
            items=[]
            for key in ids:
                r = self._row(db,school,key)
                if r['state'] != 'published':
                    raise ValueError('发布后可导出')
                item=json.loads(r['entry_json'])
                item['provenance']={'version':r['version'],'reviewer':r['reviewer'],'reviewed_at':r['reviewed_at'],
                                    'review_note':r['review_note']}
                items.append(item)
            return {'format':FORMAT,'items':items}
        return self._classroom(db,action,p,school,actor)

    @staticmethod
    def _event(db,school,item_id,class_id,student_id,actor,kind,detail):
        db.execute('''INSERT INTO content_events
          (school_id,item_id,class_id,student_id,actor,kind,detail,created_at)
          VALUES(?,?,?,?,?,?,?,?)''',(school,item_id,class_id,student_id,actor,kind,_json(detail),_now()))

    def _classroom(self,db,action,p,school,actor):
        if action == 'classes':
            rows = db.execute('''SELECT c.*,v.title,v.version FROM content_classes c JOIN content_versions v
              ON c.item_id=v.id WHERE c.school_id=? ORDER BY c.started_at DESC''',(school,))
            return {'classes':[dict(r) for r in rows]}
        if action == 'roster':
            rows = db.execute('''SELECT term,class_name FROM school_enrollments WHERE school_id=?
                                GROUP BY term,class_name ORDER BY term DESC,class_name''',(school,))
            return {'classes':[dict(r) for r in rows]}
        if action == 'activity':
            key = self._row(db,school,p.get('id'))['id']
            rows = db.execute('''SELECT kind,actor,detail,created_at FROM content_events
                WHERE school_id=? AND item_id=? AND class_id IS NULL ORDER BY id DESC''',(school,key))
            return {'events':[{**dict(r),'detail':json.loads(r['detail'])} for r in rows]}
        if action == 'start_class':
            r = self._row(db,school,p.get('id'))
            if r['state']!='published' or r['kind']=='script':
                raise ValueError('请选择已发布的教案或科普课件')
            term = _text(p.get('term'),'学期',100)
            name = _text(p.get('class_name'),'班级',100)
            if not db.execute('SELECT 1 FROM school_enrollments WHERE school_id=? AND term=? AND class_name=? LIMIT 1',
                              (school,term,name)).fetchone():
                raise ValueError('请先在本校学期名册登记班级')
            ongoing = db.execute('''SELECT id FROM content_classes WHERE school_id=? AND teacher=?
                AND item_id=? AND term=? AND class_name=? AND ended_at IS NULL''',
                (school,actor,r['id'],term,name)).fetchone()
            if ongoing:
                return {'class_id':ongoing['id']}
            key = uuid.uuid4().hex
            db.execute('''INSERT INTO content_classes(id,school_id,item_id,term,class_name,teacher,started_at)
                       VALUES(?,?,?,?,?,?,?)''',(key,school,r['id'],term,name,actor,_now()))
            return {'class_id':key}
        class_id = p.get('class_id')
        room = None
        if class_id:
            room = db.execute('''SELECT c.*,v.entry_json,v.state content_state FROM content_classes c
             JOIN content_versions v ON v.id=c.item_id WHERE c.school_id=? AND c.id=?''',
                              (school,_text(class_id,'课堂编号',100))).fetchone()
            if not room:
                raise ValueError('课堂不存在')
        if action in ('class_detail','finish_class') and room is None:
            raise ValueError('请选择课堂')
        if action == 'class_detail':
            members=db.execute('''SELECT s.id,s.name FROM school_students s JOIN school_enrollments e
              ON e.student_id=s.id AND e.school_id=s.school_id
              WHERE e.school_id=? AND e.term=? AND e.class_name=? ORDER BY s.name''',(school,room['term'],room['class_name']))
            events=db.execute('''SELECT student_id,kind,detail,actor,created_at FROM content_events
              WHERE school_id=? AND class_id=? ORDER BY id''',(school,class_id))
            data=dict(room)
            data['item']=json.loads(data.pop('entry_json'))
            return {'class':data,'members':[dict(r) for r in members],
                    'events':[{**dict(r),'detail':json.loads(r['detail'])} for r in events]}
        if room and room['ended_at']:
            raise ValueError('课堂已结束')
        if action == 'finish_class':
            observation=_text(p.get('observation'),'课堂观察',1600)
            feedback=_text(p.get('feedback'),'课堂反馈',1600)
            db.execute('UPDATE content_classes SET ended_at=?,feedback=?,observation=? WHERE school_id=? AND id=?',
                       (_now(),feedback,observation,school,class_id))
            return {'class_id':class_id,'ended':True}
        if action != 'record':
            raise ValueError('未知课堂操作')
        item_id=room['item_id'] if room else p.get('id')
        r=self._row(db,school,item_id)
        # A class keeps the accepted teaching version even when a new version supersedes it.
        # An explicit withdrawal stops further playback records even for an open class.
        withdrawn=db.execute("SELECT 1 FROM content_events WHERE school_id=? AND item_id=? AND kind='withdraw' LIMIT 1",
                             (school,item_id)).fetchone()
        if withdrawn or (r['state']!='published' and not (room and r['state']=='retired')):
            raise ValueError('该材料已停止播放')
        if r['kind']=='script':
            raise ValueError('此条目没有课件')
        kind=_choice(p.get('kind'),('page','completed','interaction'),'学习记录类型')
        detail={}
        if kind=='page':
            total=len(json.loads(r['entry_json'])['body']['slides'])
            detail['page']=_count(p.get('page'),'课件页码',1,total)
        if kind=='interaction':
            detail['note']=_text(p.get('note'),'互动或观察记录',1200)
        student_id=p.get('student_id') or None
        if student_id:
            if not room:
                raise ValueError('个人学习记录需关联班级课堂')
            student_id = _text(student_id,'学生编号',100)
            if not db.execute('''SELECT 1 FROM school_enrollments WHERE school_id=? AND term=?
                       AND class_name=? AND student_id=?''',(school,room['term'],room['class_name'],student_id)).fetchone():
                raise ValueError('学生不在本班名册')
        self._event(db,school,item_id,class_id,student_id,actor,kind,detail)
        return {'recorded':True}
