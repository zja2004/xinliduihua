"""Local accounts and school ownership for the staff portal."""
import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time

from .clinical_store import ClinicalStore
from .settings import ROOT

ROLES = ('teacher', 'school_admin', 'reviewer', 'principal', 'district_admin')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[\w.-]{1,64}', value) or value in {'.', '..'}:
        raise ValueError('账号和学校编号可用 1–64 个文字、数字、点、下划线或短横线')
    return value


class SchoolAccess:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.failures = {}
        self.kiosk_key = secrets.token_bytes(32)
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS portal_users(
                  username TEXT PRIMARY KEY, school_id TEXT NOT NULL, role TEXT NOT NULL,
                  salt BLOB NOT NULL, hash BLOB NOT NULL, schools TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS portal_logins(
                  token TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES portal_users(username),
                  expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS portal_session_scope(
                  session_id TEXT PRIMARY KEY, school_id TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS portal_scope_school ON portal_session_scope(school_id);
                CREATE TABLE IF NOT EXISTS portal_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            if not db.execute("SELECT 1 FROM portal_settings WHERE key='legacy_scope'").fetchone():
                db.execute("INSERT OR IGNORE INTO portal_session_scope SELECT id,'local' FROM sessions")
                db.executemany("INSERT OR IGNORE INTO portal_session_scope VALUES(?,'local')",
                               [(p.parent.name,) for p in store.root.glob('*/answers.json')
                                if re.fullmatch(r'[A-Za-z0-9_-]{1,100}',p.parent.name)])
                db.execute("INSERT INTO portal_settings VALUES('legacy_scope','1')")

    @staticmethod
    def account_data(username, password, role, school, schools=()):
        username, school = identifier(username), identifier(school)
        if role not in ROLES or not isinstance(password,str) or not 12 <= len(password) <= 256:
            raise ValueError('请选择有效角色并设置 12–256 字的密码')
        if not isinstance(schools, (list,tuple)):
            raise ValueError('学校授权列表无效')
        scopes = sorted({identifier(s) for s in schools}) if role == 'district_admin' else []
        if role == 'district_admin' and not scopes:
            raise ValueError('教育局账号需要明确列出可访问的学校')
        salt = secrets.token_bytes(16)
        return (username, school, role, salt, hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1), json.dumps(scopes))

    def account(self, username, password, role, school, schools=(), replace=False):
        values = self.account_data(username,password,role,school,schools)
        with self.store._connect() as db:
            try:
                if replace:
                    db.execute('DELETE FROM portal_logins WHERE username=?',(username,))
                    db.execute('INSERT INTO portal_users VALUES(?,?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET '
                               'school_id=excluded.school_id,role=excluded.role,salt=excluded.salt,hash=excluded.hash,schools=excluded.schools',values)
                else:
                    db.execute('INSERT INTO portal_users VALUES(?,?,?,?,?,?)',values)
            except sqlite3.IntegrityError as exc:
                raise ValueError('账号已存在') from exc
        return {'username':username,'role':role,'school_id':school}

    def bootstrap(self):
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM portal_users LIMIT 1').fetchone():
                return
            path = self.store.root / '学校工作台-首次登录.txt'
            # Create secrets only on the signed-in OS user's disk, never in HTTP responses.
            passwords = {role:secrets.token_urlsafe(18) for role in ('teacher','school_admin')}
            fd = os.open(path,os.O_CREAT | os.O_EXCL | os.O_WRONLY,0o600)
            try:
                with os.fdopen(fd,'w',encoding='utf-8') as f:
                    f.write('本机学校编号 local。请分别交付老师与学校管理员。登录后可在账号页修改密码。\n')
                    for role,password in passwords.items():
                        db.execute('INSERT INTO portal_users VALUES(?,?,?,?,?,?)',self.account_data(role,password,role,'local'))
                        f.write(f'{role}　{password}\n')
                    f.flush()
                    os.fsync(f.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                raise

    def login(self, username, password):
        if not isinstance(username,str) or not isinstance(password,str) or len(password)>256:
            raise PermissionError('账号或密码错误')
        now=time.time()
        with self.lock:
            self.failures={k:v for k,v in self.failures.items() if v[1]>now-300}
            if self.failures.get(username,(0,0))[0]>=8:
                raise PermissionError('登录尝试过多，请稍后再试')
        with self.store._connect() as db:
            row=db.execute('SELECT * FROM portal_users WHERE username=?',(username,)).fetchone()
            proof=hashlib.scrypt(password.encode(),salt=row['salt'] if row else b'unknown-user-salt',n=16384,r=8,p=1)
            if not row or not hmac.compare_digest(proof,row['hash']):
                with self.lock:
                    self.failures[username]=(self.failures.get(username,(0,0))[0]+1,now)
                raise PermissionError('账号或密码错误')
            with self.lock: self.failures.pop(username,None)
            token=secrets.token_urlsafe(32)
            db.execute('DELETE FROM portal_logins WHERE expires<=?',(now,))
            db.execute('INSERT INTO portal_logins VALUES(?,?,?)',(self.digest(token),username,now+8*3600))
            return token

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def actor(self,token):
        if not token: return None
        with self.store._connect() as db:
            row=db.execute('SELECT u.username,u.role,u.school_id,u.schools FROM portal_logins l '
                           'JOIN portal_users u ON u.username=l.username WHERE l.token=? AND l.expires>?',
                           (self.digest(token),time.time())).fetchone()
        if row:
            return dict(username=row['username'],role=row['role'],school_id=row['school_id'],
                        allowed_school_ids=json.loads(row['schools']))

    def logout(self,token):
        with self.store._connect() as db:
            db.execute('DELETE FROM portal_logins WHERE token=?',(self.digest(token),))

    def set_scope(self,sid,school):
        with self.store._connect() as db:
            db.execute('INSERT INTO portal_session_scope VALUES(?,?)',(sid,school))

    def school_for(self,sid):
        with self.store._connect() as db:
            row=db.execute('SELECT school_id FROM portal_session_scope WHERE session_id=?',(sid,)).fetchone()
            return row[0] if row else None

    def pair_device(self,school):
        key=secrets.token_urlsafe(32)
        with self.store._connect() as db:
            db.executemany('INSERT OR REPLACE INTO portal_settings VALUES(?,?)',
                [('desk_key',self.digest(key)),('desk_school',school)])
        return key

    def station_school(self):
        with self.store._connect() as db:
            row=db.execute("SELECT value FROM portal_settings WHERE key='desk_school'").fetchone()
            return row[0] if row else 'local'

    def paired(self,key,school):
        if not key: return False
        with self.store._connect() as db:
            settings=dict(db.execute("SELECT key,value FROM portal_settings WHERE key IN ('desk_key','desk_school')"))
        return school==settings.get('desk_school') and hmac.compare_digest(self.digest(key),settings.get('desk_key',''))

    def kiosk(self,sid):
        return sid+'.'+hmac.new(self.kiosk_key,sid.encode(),'sha256').hexdigest()

    def allowed(self,actor,sid,kiosk='',raw=False):
        if actor and actor['role']=='teacher' and self.school_for(sid)==actor['school_id']:
            return True
        return not raw and bool(kiosk) and hmac.compare_digest(kiosk.encode(),self.kiosk(sid).encode())

    def school_sessions(self,school,limit=250):
        with self.store._connect() as db:
            return [dict(row) for row in db.execute("""SELECT s.id,s.id session_id,s.started_at,s.ended_at,s.status,s.mode,
                p.name,(SELECT COUNT(*) FROM media m WHERE m.session_id=s.id AND m.kind='audio') audio_count,
                (SELECT COUNT(*) FROM media m WHERE m.session_id=s.id AND m.kind='video') video_count
                FROM portal_session_scope x JOIN sessions s ON s.id=x.session_id JOIN participants p ON p.id=s.participant_id
                WHERE x.school_id=? ORDER BY s.started_at DESC LIMIT ?""",(school,limit))]


def main():
    parser=argparse.ArgumentParser(description='本机账号管理，密码从终端隐藏输入')
    parser.add_argument('username')
    parser.add_argument('--role',choices=ROLES,required=True)
    parser.add_argument('--school',required=True)
    parser.add_argument('--allow-school',action='append',default=[])
    parser.add_argument('--reset',action='store_true',help='重设该账号并注销原登录')
    parser.add_argument('--output',default=str(ROOT/'sessions'))
    args=parser.parse_args()
    password=getpass.getpass('新密码（至少 12 字）：')
    if password!=getpass.getpass('确认密码：'): parser.error('两次密码不同')
    SchoolAccess(ClinicalStore(args.output)).account(args.username,password,args.role,args.school,args.allow_school,args.reset)
    print('账号已保存。')


if __name__=='__main__':
    main()
