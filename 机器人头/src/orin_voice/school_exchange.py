"""Offline public-key exchange of disclosure-controlled school statistics."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .clinical_store import ClinicalStore
from .school_access import identifier
from .settings import ROOT

SCHEMA = """
CREATE TABLE IF NOT EXISTS report_trust(school_id TEXT PRIMARY KEY, public_pem BLOB NOT NULL,
 fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report_import_month(school_id TEXT NOT NULL, month TEXT NOT NULL,
 created TEXT NOT NULL, fingerprint TEXT NOT NULL, digest TEXT NOT NULL, row_json TEXT NOT NULL,
 resource_json TEXT, imported TEXT NOT NULL, PRIMARY KEY(school_id,month));
"""
METRICS = ('interviewed','due','completed','alerts','handled','referrals','approved','received','archived')
RISK = ('预防','关注','风险','高危','待复核')
ED25519_DER_PREFIX = bytes.fromhex('302a300506032b6570032100')
FORMAT = 'school-monthly-v1'


def canonical(obj):
    return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def openssl(args, data=None):
    try:
        r=subprocess.run(['openssl',*args],input=data,capture_output=True,timeout=10,check=True)
    except (OSError,subprocess.SubprocessError) as exc:
        raise ValueError('OpenSSL 操作失败，请检查密钥格式或签名') from exc
    return r.stdout


def finger(pem):
    public_der=openssl(['pkey','-pubin','-outform','DER'],pem)
    if len(public_der)!=44 or not public_der.startswith(ED25519_DER_PREFIX):
        raise ValueError('仅支持 Ed25519 学校公钥')
    return hashlib.sha256(public_der).hexdigest()


def private_path(store,school):
    return store.root/'.school-signing'/hashlib.sha256(identifier(school).encode()).hexdigest()


def keygen(store,school):
    path=private_path(store,school)
    path.parent.mkdir(mode=0o700,exist_ok=True)
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=path.parent) as tmp:
            tmp.write(openssl(['genpkey','-algorithm','ED25519']));tmp.flush();os.fsync(tmp.fileno())
            try:
                os.link(tmp.name,path)  # never replace an existing signing identity
            except FileExistsError:
                pass
    return openssl(['pkey','-in',str(path),'-pubout'])


def trust(store,school,pem,expected,rotate=False):
    school=identifier(school)
    if len(pem)>2048:
        raise ValueError('无效公钥')
    fingerprint=finger(pem)
    if not isinstance(expected,str) or fingerprint!=expected.lower().replace(':','').strip():
        raise ValueError('与独立核对的学校公钥指纹不匹配')
    with store._connect() as db:
        db.executescript(SCHEMA)
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT fingerprint FROM report_trust WHERE school_id=?',(school,)).fetchone()
        if old and old[0]!=fingerprint and not rotate:
            raise ValueError('该校已有另一把可信公钥，轮换请显式指定 --rotate')
        db.execute('INSERT INTO report_trust VALUES(?,?,?) ON CONFLICT(school_id) DO UPDATE SET '
                   'public_pem=excluded.public_pem,fingerprint=excluded.fingerprint',(school,pem,fingerprint))
    return fingerprint


def sign(store,school,months,resources):
    private=private_path(store,school)
    if not private.is_file():
        raise ValueError('请由本机管理员先为本校配置离线汇总签名密钥')
    payload={'format':FORMAT,'school':school,'created':datetime.now(timezone.utc).isoformat(timespec='microseconds'),
             'months':months,'resources':resources}
    validate(payload)
    with tempfile.NamedTemporaryFile() as message:
        message.write(canonical(payload));message.flush()
        signature=openssl(['pkeyutl','-sign','-rawin','-inkey',str(private),'-in',message.name])
    return {'payload':payload,'signature':base64.b64encode(signature).decode('ascii')}


def fields(obj,names):
    if not isinstance(obj,dict) or set(obj)!=set(names):
        raise ValueError('汇总包包含缺失字段或非白名单内容')


def month(value):
    if not isinstance(value,str) or not re.fullmatch(r'(20|21)\d{2}-(0[1-9]|1[0-2])',value):
        raise ValueError('汇总月份无效')


def count(v):
    if v is not None and (type(v) is not int or v<0 or v>100000000 or 0<v<5):
        raise ValueError('学生人数必须省略小样本')


def validate(p):
    fields(p,('format','school','created','months','resources'))
    if p['format']!=FORMAT:
        raise ValueError('不支持此格式')
    identifier(p['school'])
    raw=p['created']
    if not isinstance(raw,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00',raw):
        raise ValueError('签发时间需使用 UTC')
    current=datetime.now(timezone.utc)
    if datetime.fromisoformat(raw)>current+timedelta(minutes=5):
        raise ValueError('签发时间晚于当前设备时钟，请先校准时间')
    rows=p['months']
    if not isinstance(rows,list) or not 1<=len(rows)<=36:
        raise ValueError('签名包只支持 1 至 36 个月')
    months=set()
    for r in rows:
        fields(r,(*METRICS,'month','coverage_pct','risk_distribution'))
        month(r['month'])
        if r['month'] in months: raise ValueError('单包内月份重复')
        months.add(r['month'])
        for k in METRICS: count(r[k])
        for a,b in [('due','completed'),('alerts','handled'),('referrals','approved'),('approved','received'),('received','archived')]:
            total,n=r[a],r[b]
            if n is not None and (total is None or n>total or 0<total-n<5):
                raise ValueError('应隐藏可推算出小组人数的细分')
        pct=r['coverage_pct']
        expected=(round(100*r['completed']/r['due'],1) if r['due'] and r['completed'] is not None else None)
        if (pct is not None and type(pct) not in (int,float)) or pct!=expected:
            raise ValueError('覆盖率与已公开分子分母不匹配')
        dist=r['risk_distribution']
        if dist is not None:
            fields(dist,RISK)
            for v in dist.values():
                count(v)
                if v is None: raise ValueError('隐藏风险分布须整组隐藏')
            if sum(dist.values())!=r['alerts'] or (r['alerts'] or 0)<5:
                raise ValueError('预警人数与分布不符')
    resources=p['resources']
    if not isinstance(resources,list) or len(resources)>len(months): raise ValueError('资源记录过多')
    seen=set()
    for r in resources:
        fields(r,['month','counselors','rooms','hours'])
        month(r['month'])
        if r['month'] not in months or r['month'] in seen: raise ValueError('资源月份重复或超范围')
        seen.add(r['month'])
        if any(type(r[k]) is not int or not 0<=r[k]<=100000 for k in ('counselors','rooms','hours')):
            raise ValueError('资源必须为非负整数')


def accept(store,packet,allowed):
    fields(packet,['payload','signature']);p=packet['payload']
    validate(p)
    if p['school'] not in allowed:
        raise PermissionError('区域账号未获该校授权')
    sig=packet['signature']
    if not isinstance(sig,str) or len(sig)!=88:
        raise ValueError('签名格式无效')
    try:
        sig=base64.b64decode(sig,validate=True)
    except ValueError as exc:
        raise ValueError('签名格式无效') from exc
    if len(sig)!=64:raise ValueError('签名无效')
    body=canonical(p)
    digest=hashlib.sha256(body).hexdigest()
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        key=db.execute('SELECT * FROM report_trust WHERE school_id=?',(p['school'],)).fetchone()
        if not key:
            raise ValueError('本机尚未预置该校可信公钥')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for filename,data in (('public.pem',key['public_pem']),('body',body),('signature',sig)):
                (root/filename).write_bytes(data)
            openssl(['pkeyutl','-verify','-rawin','-pubin','-inkey',str(root/'public.pem'),
                     '-in',str(root/'body'),'-sigfile',str(root/'signature')])
        resources={r['month']:r for r in p['resources']}
        updated=0; duplicate=0; older=0
        imported=datetime.now(timezone.utc).isoformat(timespec='seconds')
        for r in p['months']:
            old=db.execute('SELECT created,digest FROM report_import_month WHERE school_id=? AND month=?',
                           (p['school'],r['month'])).fetchone()
            if old and old['digest']==digest:
                duplicate+=1;continue
            if old and old['created']>p['created']:
                older+=1;continue
            if old and old['created']==p['created']:
                raise ValueError('同校同月出现相同签发时间的不同签名包，请核对来源')
            db.execute('INSERT OR REPLACE INTO report_import_month VALUES(?,?,?,?,?,?,?,?)',
                       (p['school'],r['month'],p['created'],key['fingerprint'],digest,canonical(r).decode(),
                        canonical(resources[r['month']]).decode() if r['month'] in resources else None,imported))
            updated+=1
    return {'school':p['school'],'updated':updated,'duplicates':duplicate,'older':older,
            'fingerprint':key['fingerprint'],'signed_at':p['created'],'imported_at':imported}


def apply_imports(db,item,start,end):
    school=item['school']
    # A locally managed roster has priority for the whole school. Never sum
    # a local source and an offline package for the same cohort.
    local=db.execute('SELECT 1 FROM school_students WHERE school_id=? LIMIT 1',(school,)).fetchone()
    has_resources=db.execute('SELECT 1 FROM school_resource_allocations WHERE school_id=? LIMIT 1',(school,)).fetchone()
    if local or has_resources:
        item['source']='本库实时';return
    imported={row['month']:row for row in db.execute('SELECT * FROM report_import_month WHERE school_id=? '
              'AND month BETWEEN ? AND ?',(school,start,end))}
    item['source']='已验签离线汇总' if imported else '本库暂无记录'
    if not imported:return
    for i,r in enumerate(item['months']):
        row=imported.get(r['month'])
        if row is None:continue
        item['months'][i]=json.loads(row['row_json'])
        item.setdefault('provenance',[]).append({'month':r['month'],'signed_at':row['created'],
                'imported_at':row['imported'],'fingerprint':row['fingerprint'],'digest':row['digest']})
        if row['resource_json']:
            item['resources'].append(json.loads(row['resource_json']))


def main():
    ap=argparse.ArgumentParser(description='学校月报签名及区域设备公钥信任设置')
    ap.add_argument('--output',default=str(ROOT/'sessions'),help='部署使用的 sessions 数据目录')
    sub=ap.add_subparsers(dest='operation',required=True)
    k=sub.add_parser('keygen');k.add_argument('--school',required=True);k.add_argument('--public-out',required=True)
    t=sub.add_parser('trust');t.add_argument('--school',required=True);t.add_argument('--public-key',required=True)
    t.add_argument('--fingerprint',required=True);t.add_argument('--rotate',action='store_true')
    a=ap.parse_args()
    try:
        store=ClinicalStore(a.output)
        if a.operation=='keygen':
            pem=keygen(store,a.school)
            dest=Path(a.public_out)
            if dest.exists() and dest.read_bytes()!=pem:
                raise ValueError('目标文件已存在且内容不同')
            if not dest.exists():
                with dest.open('xb') as f:f.write(pem)
            print('Ed25519 公钥 SHA-256',finger(pem))
        else:
            print('已信任学校',identifier(a.school),trust(store,a.school,Path(a.public_key).read_bytes(),a.fingerprint,a.rotate))
    except (OSError,ValueError) as exc:
        ap.exit(1,str(exc)+'\n')


if __name__=='__main__':
    main()
