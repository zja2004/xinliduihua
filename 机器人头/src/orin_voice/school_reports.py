"""School-scoped scale registry, individual reports and disclosure-controlled dashboards."""
import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone

from . import school_exchange
from .primary21 import LABELS
from .settings import ROOT

K = 5
LEVELS = ('预防', '关注', '风险', '高危')
BUILTINS = ('PRIMARY-21', 'DASS-21')
ROLE_ACTIONS = {
    'catalog': {'teacher', 'school_admin', 'reviewer', 'principal'},
    'draft': {'school_admin'},
    'review': {'reviewer'},
    'sessions': {'teacher'},
    'assess': {'teacher'},
    'teacher': {'teacher'},
    'parent': {'teacher'},
    'dashboard': {'teacher', 'school_admin', 'principal', 'district_admin'},
    'set_resources': {'school_admin', 'principal'},
    'export_packet': {'school_admin','principal'},
    'import_packet': {'district_admin'},
}
DASS_LABELS = {'depression': '抑郁维度', 'anxiety': '焦虑维度', 'stress': '压力维度'}


def _text(obj, key, maximum=1500):
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f'请填写 {key}，最多 {maximum} 字')
    return value.strip()


def _code(obj, key):
    value = _text(obj, key, 50)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,49}', value):
        raise ValueError(f'{key} 只接受字母、数字、点、下划线和短线')
    return value


def _integer(value, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError(f'此数值必须是 {lo} 到 {hi} 的整数')
    return value


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _rule(data):
    result = {key: _text(data, key) for key in (
        'title', 'owner', 'license_ref', 'norms', 'validation_ref', 'review_body',
        'threshold_basis', 'uncertainty', 'confidence_basis')}
    result['instrument'] = _code(data, 'instrument').upper()
    result['version'] = _code(data, 'version')
    if re.sub(r'[^A-Z0-9]', '', result['instrument']) in {'PRIMARY21','DASS21'}:
        raise ValueError('内置题单保留原计分与解释范围，请选用拥有独立依据的量表编号')
    lo = _integer(data.get('min_age'), 0, 120)
    result.update(min_age=lo, max_age=_integer(data.get('max_age'), lo, 120))
    choices = data.get('choices')
    if not isinstance(choices, list) or not 2 <= len(choices) <= 11 or any(
            not isinstance(v, str) or not v.strip() or len(v) > 100 for v in choices):
        raise ValueError('请提供 2 至 11 个从零起连续计分的选项')
    result['choices'] = [v.strip() for v in choices]
    if len(set(result['choices'])) != len(choices):
        raise ValueError('选项名称不可重复')
    questions = data.get('questions')
    if not isinstance(questions, list) or not 1 <= len(questions) <= 160:
        raise ValueError('题目数量须在 1 至 160 之间')
    items = []
    for item in questions:
        if not isinstance(item, dict) or type(item.get('reverse', False)) is not bool:
            raise ValueError('题目格式不正确')
        items.append({'id': _code(item, 'id'), 'text': _text(item, 'text', 600),
                      'reverse': item.get('reverse', False)})
    if len({q['id'] for q in items}) != len(items):
        raise ValueError('题号不能重复')
    # Retitling a built-in cannot convert its stored prompts into a locally
    # reviewed four-band instrument or assign a new risk interpretation.
    texts = {re.sub(r'[^\w]', '', q['text']).casefold() for q in items}
    for fn in ('primary21.zh.json','dass21.zh.json'):
        original = json.loads((ROOT/'configs'/fn).read_text(encoding='utf-8'))
        original_texts = {re.sub(r'[^\w]', '', q['text']).casefold() for q in original['questions']}
        if original_texts <= texts:
            raise ValueError('题单包含完整内置量表，请保留内置题单的解释范围')
    result['questions'] = items
    def boundaries(values, high):
        if values in (None, []):
            return []
        if not isinstance(values, list) or len(values)!=3:
            raise ValueError('四级规则需要三个递增分界点；只记录原始分时请留空')
        points = [_integer(v, 1, high) for v in values]
        if sorted(set(points)) != points:
            raise ValueError('分界点必须严格递增')
        return points
    result['dimensions'] = []
    dims = data.get('dimensions', [])
    if not isinstance(dims, list) or len(dims)>30:
        raise ValueError('维度列表无效')
    qids = {x['id'] for x in items}
    for dim in dims:
        if not isinstance(dim,dict):
            raise ValueError('维度配置无效')
        name, keys = _text(dim,'name',100), dim.get('items')
        if (not isinstance(keys,list) or not keys or not all(isinstance(k,str) and k in qids for k in keys)
                or len(set(keys))!=len(keys)):
            raise ValueError('每个维度应引用题单内的题号且不重复')
        valid_limit = dim.get('invalid_at_or_above')
        high = len(keys)*(len(choices)-1)
        result['dimensions'].append({'name':name,'items':keys,'cutoffs':boundaries(dim.get('cutoffs'),high),
             'invalid_at_or_above': None if valid_limit is None else _integer(valid_limit,1,high)})
    if len({d['name'] for d in result['dimensions']})!=len(result['dimensions']):
        raise ValueError('维度名称不能重复')
    total_items = data.get('total_items', [q['id'] for q in items])
    if (not isinstance(total_items,list) or not total_items or not all(isinstance(i,str) and i in qids for i in total_items)
            or len(set(total_items))!=len(total_items)):
        raise ValueError('总分题号无效')
    result['total_items']=total_items
    result['cutoffs']=boundaries(data.get('cutoffs'),len(total_items)*(len(choices)-1))
    return result


def _numbers(scoring, names):
    """Construct a closed, typed projection; never copy free text into a family export."""
    if not isinstance(scoring, dict):
        return {}
    def number(value):
        return value if type(value) in (int, float) and math.isfinite(value) else None
    dims = []
    subscales = scoring.get('subscales')
    for key, label in names.items():
        src = subscales.get(key) if isinstance(subscales,dict) else None
        if isinstance(src, dict):
            dims.append({'theme': label, 'complete': src.get('complete') is True,
                         'raw_score': number(src.get('raw_score')) if src.get('complete') is True else None})
    complete = scoring.get('complete') is True
    return {'complete': complete, 'total': number(scoring.get('total_score')) if complete else None,
            'themes': dims}


class SchoolReports:
    def __init__(self, clinical_store, school_ops=None):
        self.store = clinical_store
        with self.store._connect() as db:
            db.executescript(school_exchange.SCHEMA)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS school_resource_allocations(
                  school_id TEXT NOT NULL, month TEXT NOT NULL, counselors INTEGER NOT NULL, rooms INTEGER NOT NULL,
                  hours INTEGER NOT NULL, reviewer TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(school_id,month));
                CREATE TABLE IF NOT EXISTS report_instruments(
                  school_id TEXT NOT NULL, instrument TEXT NOT NULL, version TEXT NOT NULL,
                  state TEXT NOT NULL, definition TEXT NOT NULL, creator TEXT NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(school_id,instrument,version));
                CREATE TABLE IF NOT EXISTS report_reviews(
                  school_id TEXT NOT NULL, instrument TEXT NOT NULL, version TEXT NOT NULL,
                  decision TEXT NOT NULL, reviewer TEXT NOT NULL, evidence_ref TEXT NOT NULL,
                  notes TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(school_id,instrument,version),
                  FOREIGN KEY(school_id,instrument,version) REFERENCES report_instruments);
                CREATE TABLE IF NOT EXISTS report_assessments(
                  school_id TEXT NOT NULL, instrument TEXT NOT NULL, version TEXT NOT NULL, session_id TEXT NOT NULL,
                  payload TEXT NOT NULL, result TEXT NOT NULL, recorder TEXT NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(school_id,instrument,version,session_id),
                  FOREIGN KEY(school_id,instrument,version) REFERENCES report_instruments);
            """)

    def dispatch(self, action, data, actor):
        role = actor.get('role')
        if role not in ROLE_ACTIONS.get(action, set()):
            raise PermissionError('当前账号无此权限')
        if not isinstance(data, dict):
            raise ValueError('请提交一个对象')
        if role == 'district_admin':
            allowed = actor.get('allowed_school_ids')
            if not isinstance(allowed, (list, tuple)) or not all(isinstance(v, str) and v for v in allowed):
                raise PermissionError('缺少区域授权学校清单')
            scopes = sorted(set(allowed))
        else:
            school = actor.get('school_id')
            if not isinstance(school, str) or not school:
                raise PermissionError('账号未绑定学校')
            scopes = [school]
        if action == 'dashboard':
            return self.dashboard(scopes, data, role)
        if action == 'import_packet':
            return school_exchange.accept(self.store,data,scopes)
        if action == 'export_packet':
            row=self.dashboard(scopes,data,role)['schools'][0]
            return school_exchange.sign(self.store,scopes[0],row['months'],row['resources'])
        school = scopes[0]
        if action == 'set_resources':
            month = data.get('month')
            if not isinstance(month,str) or not re.fullmatch(r'20[0-9]{2}-(0[1-9]|1[0-2])',month):
                raise ValueError('请输入资源所属年月')
            units = [_integer(data.get(k),0,100000) for k in ('counselors','rooms','hours')]
            with self.store._connect() as db:
                db.execute('INSERT INTO school_resource_allocations VALUES(?,?,?,?,?,?,?) '
                           'ON CONFLICT(school_id,month) DO UPDATE SET counselors=excluded.counselors,'
                           'rooms=excluded.rooms,hours=excluded.hours,reviewer=excluded.reviewer,updated_at=excluded.updated_at',
                           (school,month,*units,_text(actor,'username',180),_now()))
            return {'month':month,'recorded':True}
        if action == 'catalog':
            return self.catalog(school)
        if action == 'sessions':
            offset = _integer(data.get('offset',0),0,1000000000)
            limit = _integer(data.get('limit',100),1,300)
            with self.store._connect() as db:
                rows = db.execute("""SELECT s.session_id,s.student_id,s.term,s.status,s.created_at,t.name,t.student_code
                  FROM school_sessions s JOIN school_students t ON t.id=s.student_id AND t.school_id=s.school_id
                  WHERE s.school_id=? ORDER BY s.created_at DESC,s.session_id DESC LIMIT ? OFFSET ?""",(school,limit+1,offset)).fetchall()
                return {'sessions':[dict(r) for r in rows[:limit]],'next_offset':offset+limit if len(rows)>limit else None}
        if action in ('draft', 'review', 'assess'):
            username = _text(actor, 'username', 180)
            if action == 'draft':
                return self.draft(school, username, data)
            if action == 'review':
                return self.review(school, username, data)
            return self.assess(school, username, data)
        return self.individual(school, _text(data, 'session_id', 120), action)

    @staticmethod
    def _version(db, school, instrument, version):
        row = db.execute('SELECT * FROM report_instruments WHERE school_id=? AND instrument=? AND version=?',
                         (school, instrument, version)).fetchone()
        if row is None:
            raise ValueError('本校无该量表版本')
        return row

    def catalog(self, school):
        entries = []
        for code, fn in zip(BUILTINS, ('primary21.zh.json','dass21.zh.json')):
            d = json.loads((ROOT/'configs'/fn).read_text(encoding='utf-8'))
            entries.append({'instrument':code,'version':d.get('version','随部署题单'),
                            'title':d['title'],'state':'builtin','source':d['source'],
                            'scoring_source':d['scoring_source'],'uncertainty':d.get('interpretation_notice',
                            '此实现输出自评数值；适龄性、授权范围和四级阈值需单独核定。')})
        with self.store._connect() as db:
            for row in db.execute("""SELECT i.*,r.decision,r.reviewer,r.evidence_ref,r.notes,r.created_at reviewed_at
               FROM report_instruments i LEFT JOIN report_reviews r USING(school_id,instrument,version)
               WHERE i.school_id=? ORDER BY i.created_at DESC,i.rowid DESC""",(school,)):
                d = json.loads(row['definition'])
                d.update({k:row[k] for k in ('state','creator','created_at','reviewer','decision',
                                              'evidence_ref','notes','reviewed_at')})
                entries.append(d)
        return {'instruments':entries,'levels':list(LEVELS),
                'rule_method':'选项按顺序从 0 计分；反向题为最高选项分减原分。全部答齐且实际年龄在适用范围内时，按总分与三个含下界的分界点生成复核等级。',
                'mht_status':'MHT 需基金会或授权方提供实际题单、适龄常模、版权及审校材料。只按获审的选择题和规则计分。',
                'authorization':'请保存基金会或实际授权方许可、常模与阈值材料、专家签核原件，本系统记录对应的证据编号。'}

    def draft(self, school, actor, data):
        rule = _rule(data)
        code, version = rule['instrument'], rule['version']
        with self.store._connect() as db:
            cur = db.execute('INSERT OR IGNORE INTO report_instruments VALUES (?,?,?,?,?,?,?)',
                             (school,code,version,'draft',json.dumps(rule,ensure_ascii=False),actor,_now()))
            if not cur.rowcount:
                raise ValueError('相同量表版本已存在，请使用新版本号')
        return {'instrument':code,'version':version,'state':'draft'}

    def review(self, school, actor, data):
        code, version = _code(data,'instrument').upper(), _code(data,'version')
        decision = data.get('decision')
        if decision not in ('approved','rejected'):
            raise ValueError('须选择通过或退回')
        ref, notes = _text(data,'evidence_ref'), _text(data,'notes')
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._version(db,school,code,version)
            if actor == row['creator']:
                raise PermissionError('请由独立的审校账号签核')
            if row['state'] != 'draft':
                raise ValueError('该版本已有结论，修改请另建版本')
            db.execute('INSERT INTO report_reviews VALUES (?,?,?,?,?,?,?,?)',
                       (school,code,version,decision,actor,ref,notes,_now()))
            db.execute('UPDATE report_instruments SET state=? WHERE school_id=? AND instrument=? AND version=?',
                       (decision,school,code,version))
        return {'instrument':code,'version':version,'state':decision}

    @staticmethod
    def _owned(db, school, sid):
        row = db.execute("""SELECT o.student_id,o.term,o.created_at,s.status,p.age FROM school_sessions o
          JOIN sessions s ON s.id=o.session_id JOIN participants p ON p.id=s.participant_id
          WHERE o.school_id=? AND o.session_id=?""",(school,sid)).fetchone()
        if row is None:
            raise ValueError('请先在本校学籍中关联该场访谈')
        return row

    def assess(self, school, actor, data):
        code, version, sid = _code(data,'instrument').upper(), _code(data,'version'), _text(data,'session_id',120)
        answers = data.get('answers')
        if not isinstance(answers,dict):
            raise ValueError('请提交各题的明确选择')
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            owned = self._owned(db,school,sid)
            row = self._version(db,school,code,version)
            if row['state'] != 'approved' or code in BUILTINS:
                raise ValueError('仅可使用本校经独立评审发布的量表版本')
            rule = json.loads(row['definition'])
            age = owned['age']
            if type(age) not in (int,float) or not rule['min_age'] <= age <= rule['max_age']:
                raise ValueError('该场访谈年龄缺失或超出量表审校适用范围')
            questions = rule['questions']
            if set(answers) != {q['id'] for q in questions}:
                raise ValueError('题号不完整或含题单外的题目')
            maximum = len(rule['choices'])-1
            item_values = {}
            for q in questions:
                v = _integer(answers[q['id']],0,maximum)
                item_values[q['id']] = maximum-v if q['reverse'] else v
            total = sum(item_values[q] for q in rule.get('total_items',item_values))
            def grade(score, boundaries):
                return LEVELS[sum(score>=v for v in boundaries)] if boundaries else None
            dimension_scores = []
            valid = True
            for d in rule.get('dimensions',[]):
                raw = sum(item_values[q] for q in d['items'])
                if d['invalid_at_or_above'] is not None and raw>=d['invalid_at_or_above']:
                    valid = False
                dimension_scores.append({'name':d['name'],'raw':raw,'level':grade(raw,d['cutoffs'])})
            if not valid:
                for dim in dimension_scores:
                    dim['level'] = None
            result = {'instrument':code,'version':version,'title':rule['title'],'total':total,
                      'rule_fingerprint':hashlib.sha256(row['definition'].encode('utf-8')).hexdigest()[:16],
                      'level':grade(total,rule['cutoffs']) if valid else None,
                      'interpretable':valid,'dimensions':dimension_scores,
                      'confidence_basis':rule['confidence_basis'],'uncertainty':rule['uncertainty'],
                      'rule_basis':rule['threshold_basis'],'cutoffs':rule['cutoffs'],
                      'norms':rule['norms'],'validation_ref':rule['validation_ref']}
            payload = json.dumps(answers,sort_keys=True)
            prev = db.execute("""SELECT payload,result FROM report_assessments WHERE school_id=?
                              AND instrument=? AND version=? AND session_id=?""",(school,code,version,sid)).fetchone()
            if prev:
                if prev['payload'] != payload:
                    raise ValueError('此场访谈已存该版本的回答，不能覆盖原记录')
                return json.loads(prev['result'])
            db.execute('INSERT INTO report_assessments VALUES (?,?,?,?,?,?,?,?)',
                       (school,code,version,sid,payload,json.dumps(result,ensure_ascii=False),actor,_now()))
        return result

    def individual(self, school, sid, audience):
        with self.store._connect() as db:
            owned = self._owned(db,school,sid)
            rows = db.execute("""SELECT a.result,a.payload,a.recorder,a.created_at,r.reviewer,r.evidence_ref
                FROM report_assessments a JOIN report_reviews r USING(school_id,instrument,version)
                WHERE a.school_id=? AND a.session_id=? ORDER BY a.created_at""",(school,sid)).fetchall()
            # Only the designated school and a linked session may read clinical details.
            clinical = self.store.get_session(sid)
        answers = clinical.get('answers') or {}
        if not isinstance(answers,dict):
            answers = {}
        instrument = answers.get('instrument')
        if instrument not in BUILTINS:
            instrument = '未确认题单'
        spec = {}
        if instrument in BUILTINS:
            fn = 'primary21.zh.json' if instrument=='PRIMARY-21' else 'dass21.zh.json'
            spec = json.loads((ROOT/'configs'/fn).read_text(encoding='utf-8'))
        base = {'audience':'家长脱敏报告' if audience=='parent' else '老师复核报告','instrument':instrument,
                'version':(spec['version'] if instrument=='PRIMARY-21' and
                           answers.get('instrument_version')==spec['version'] else '原始记录未核实版本'),
                'basis':('每题明确选择 0 至 3 分，七题一维。任务后的三个明确自评另计。' if instrument=='PRIMARY-21'
                         else '每维七题计算原始分；家长报告仅列原始分。' if instrument=='DASS-21'
                         else '请核查原始题单和对应计分依据。'),
                'confidence':'可复核本人当次选分；未对个体心理结论赋予统计置信度。',
                'uncertainty':'自述、缺失项和当时情境会影响解释。' if instrument!='PRIMARY-21' else '小学题组尚未验证信效度，无分级常模。',
                'questionnaire':_numbers(answers.get('scoring'), LABELS if instrument=='PRIMARY-21' else DASS_LABELS),
                'supplement':_numbers(answers.get('task_scoring'),LABELS) if instrument=='PRIMARY-21' else {},
                'interpretation':'数值源于本人明确选分。缺项保留为空，需结合专业人员观察。内置题单不生成学校四级风险结论。'}
        if audience == 'parent':
            # Never export instrument title, source, notes or personalized free text supplied by staff.
            base['supplemental_scales'] = []
            for index, row in enumerate(rows,1):
                saved = json.loads(row['result'])
                base['supplemental_scales'].append({'ordinal':index,'total':saved['total'],
                    'rule_fingerprint':saved.get('rule_fingerprint','未知'),
                    'review_band':saved['level'],'interpretable':saved.get('interpretable',True)})
            return base
        base.update(session_id=sid,student_id=owned['student_id'],term=owned['term'],
                    version=answers.get('instrument_version',base['version']),
                    scoring_source=answers.get('scoring_source',spec.get('scoring_source')),
                    answers=[{'id':q.get('id'),'choice':q.get('score'),'status':q.get('status')}
                             for q in answers.get('questions',[]) if isinstance(q,dict)],
                    supplemental_scales=[dict(json.loads(r['result']),answers=json.loads(r['payload']),
                        reviewer=r['reviewer'],evidence_ref=r['evidence_ref'],recorder=r['recorder'],
                        recorded_at=r['created_at']) for r in rows])
        return base

    def dashboard(self, scopes, data, role):
        start = data.get('from_month') or _now()[:7]
        end = data.get('to_month') or start
        def index(value):
            if not isinstance(value,str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',value):
                raise ValueError('时间范围需使用 YYYY-MM')
            year,month = map(int,value.split('-'))
            if not 2000<=year<=2199:
                raise ValueError('年份超出范围')
            return year*12+month-1
        first,last = index(start),index(end)
        if not first<=last<first+36:
            raise ValueError('请选择按先后排列且不超过 36 个月的窗口')
        months = [f'{i//12:04d}-{i%12+1:02d}' for i in range(first,last+1)]
        result=[]
        with self.store._connect() as db:
            for school in scopes:
                series=self._series(db,school,start,end,months)
                result.append({'school':school,'months':series})
        with self.store._connect() as db:
            for item in result:
                item['resources'] = [dict(row) for row in db.execute(
                    'SELECT month,counselors,rooms,hours FROM school_resource_allocations '
                    'WHERE school_id=? AND month BETWEEN ? AND ? ORDER BY month',(item['school'],start,end))]
                item['source']='本库实时'
                if role=='district_admin':
                    school_exchange.apply_imports(db,item,start,end)
        region=[]
        if role=='district_admin' and result:
            # Totals may never be used to recover a masked school cell.
            for pos,month in enumerate(months):
                cells=[s['months'][pos] for s in result]
                values={key:sum(cell[key] for cell in cells) if all(cell[key] is not None for cell in cells)
                        else None for key in ('interviewed','due','completed','alerts','handled','referrals','approved','received','archived')}
                risk = None
                if all(c['risk_distribution'] is not None for c in cells):
                    risk={key:sum(c['risk_distribution'][key] for c in cells) for key in (*LEVELS,'待复核')}
                region.append(dict(values,month=month,coverage_pct=_coverage(values),risk_distribution=risk))
        published=[m for s in result for m in s['months']]
        suggestions=[]
        if any(m['coverage_pct'] is not None and m['coverage_pct']<100 for m in published):
            suggestions.append('复核仍待面访的排期，为心理老师安排可用时段。')
        if any(m['alerts'] is not None and m['handled'] is not None and m['alerts']>m['handled'] for m in published):
            suggestions.append('请核查未处置预警的人员安排和复核时限。')
        if any(m['referrals'] is not None and m['archived'] is not None and m['referrals']>m['archived'] for m in published):
            suggestions.append('请负责老师核对人工转介的接收、回执和归档情况。')
        return {'audience':'教育局' if role=='district_admin' else '学校','from_month':start,'to_month':end,
                'data_source':'已绑定学籍的访谈、面访排期、老师确认的预警和人工转介登记。仅包含授权学校。',
                'method':'按 UTC 自然月和不同学生计数。实访按本月发起且已完成的访谈；应访按到期月，学生当月排期全部完成才计入已访。预警和转介按创建月及查询时处理状态，预警分布取同一学生当月最高人工确认等级。',
                'privacy':f'人数为 1 至 {K-1} 或分母不足 {K} 时隐藏。部分/全部完成等比例同时保护互补小组。区域仅相加各校已公开单元；不提供个案。',
                'schools':result,'regional_months':region,'policy_suggestions':suggestions}

    @staticmethod
    def _series(db,school,start,end,months):
        periods={m:{'interviewed':set(),'appointments':defaultdict(list),'alerts':defaultdict(list),
                    'referrals':defaultdict(list)} for m in months}
        queries = (
            ('interviewed','SELECT created_at,student_id,status FROM school_sessions WHERE school_id=? AND status=\'completed\'','created_at'),
            ('appointments','SELECT due_date,student_id,status FROM school_appointments WHERE school_id=?','due_date'),
            ('alerts','SELECT created_at,student_id,status,level FROM school_alerts WHERE school_id=?','created_at'),
            ('referrals','SELECT created_at,student_id,status FROM school_referrals WHERE school_id=?','created_at'),
        )
        for name,stmt,date_field in queries:
            for r in db.execute(stmt+f' AND student_id IS NOT NULL AND substr({date_field},1,7) BETWEEN ? AND ?',(school,start,end)):
                bucket=periods[r[date_field][:7]][name]
                if name=='interviewed':
                    bucket.add(r['student_id'])
                else:
                    bucket[r['student_id']].append(dict(r))
        series=[]
        for month,p in periods.items():
            due=len(p['appointments'])
            alerts=len(p['alerts'])
            referrals=len(p['referrals'])
            interviewed=len(p['interviewed'])
            # No counts, including zero, are disclosed for tiny overall cohorts.
            cohort=len(set(p['interviewed'])|set(p['appointments'])|set(p['alerts'])|set(p['referrals']))
            def count(n,base=cohort):
                if cohort<K or base<K or (0<n<K) or (0<base-n<K):
                    return None
                return n
            completed=sum(all(x['status']=='completed' for x in events) for events in p['appointments'].values())
            handled=sum(all(x['status']=='closed' for x in events) for events in p['alerts'].values())
            referral_counts=[sum(all(x['status'] in statuses for x in events) for events in p['referrals'].values())
                             for statuses in (('approved','sent','received','archived'),('received','archived'),('archived',))]
            risk_bins={k:0 for k in (*LEVELS,'待复核')}
            for events in p['alerts'].values():
                levels=[LEVELS.index(x['level']) for x in events if x['level'] in LEVELS]
                risk_bins[LEVELS[max(levels)] if levels else '待复核']+=1
            risk_visible=count(alerts) is not None and alerts>=K and all(not 0<n<K for n in risk_bins.values())
            ref_visible=count(referrals) is not None and referrals>=K and all(
                diff==0 or diff>=K for diff in (referrals-referral_counts[0],referral_counts[0]-referral_counts[1],
                                                 referral_counts[1]-referral_counts[2],referral_counts[2]))
            line={'month':month,'interviewed':count(interviewed),'due':count(due),
                  'completed':count(completed,due) if count(due) is not None else None,
                  'alerts':count(alerts),'handled':count(handled,alerts) if count(alerts) is not None else None,
                  'risk_distribution':risk_bins if risk_visible else None,'referrals':count(referrals)}
            line.update({key:n if ref_visible else None for key,n in zip(('approved','received','archived'),referral_counts)})
            line['coverage_pct']=_coverage(line)
            series.append(line)
        return series


def _coverage(values):
    if values['completed'] is not None and values['due']:
        return round(100*values['completed']/values['due'],1)
    return None
