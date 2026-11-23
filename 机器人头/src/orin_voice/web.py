"""Dependency-free local HTTP API and interview console."""
import argparse
import json
import mimetypes
import threading
import webbrowser
import os
import time
from http.cookies import SimpleCookie, CookieError
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote

from .service import InterviewService, Conflict
from .settings import ROOT, load_json

STATIC = Path(__file__).with_name('static')


def specification():
    action_schema = {'type': 'object', 'required': ['action'], 'properties': {
        'action': {'type': 'string', 'enum': ['answer', 'repeat', 'skip', 'previous', 'options', 'finish', 'stop']},
        'text': {'type': 'string', 'maxLength': 2000}, 'score': {'type': 'integer', 'minimum': 0, 'maximum': 3},
        'question_id': {'type': 'string', 'description': 'Expected current question; stale requests return 409'}}}
    paths = {}
    for path, method, summary in [('/api/status', 'get', '当前运行状态'),
                                  ('/api/questions', 'get', '简体题目与选项'),
                                  ('/api/robot/status', 'get', '机器人待机、视觉、硬件状态'),
                                  ('/api/robot/tracking', 'get', '最高置信度人脸眼球跟随状态'),
                                  ('/api/camera/status', 'get', '摄像头取帧状态与分辨率'),
                                  ('/api/camera/frame.jpg', 'get', '当前摄像头 JPEG 帧'),
                                  ('/api/camera/stream.mjpg', 'get', '实时 MJPEG 预览'),
                                  ('/api/teacher/tasks', 'get', '心理老师查看任务定义'),
                                  ('/api/teacher/sessions', 'get', '心理老师查看会话索引'),
                                  ('/api/teacher/sessions/{id}', 'get', '心理老师查看完整会话与媒体'),
                                  ('/api/sessions/{id}/session_package.json', 'get', '供下游的整场结构化结果'),
                                  ('/api/sessions/{id}/session_package.zip', 'get', '包含音频与对话的完整包'),
                                  ('/api/sessions', 'get', '会话列表'),
                                  ('/api/sessions/{id}', 'get', '会话、进度、转写与计分'),
                                  ('/api/sessions/{id}/answers.json', 'get', '下载 JSON'),
                                  ('/api/sessions/{id}/summary.txt', 'get', '下载总结'),
                                  ('/api/sessions/{id}/audio/{file}', 'get', '播放 WAV')]:
        operation = {'summary': summary, 'responses': {'200': {'description': '成功'}, '404': {'description': '不存在'}}}
        parameters = []
        for name in ('id', 'file'):
            if '{'+name+'}' in path:
                parameters.append({'in': 'path', 'name': name, 'required': True, 'schema': {'type': 'string'}})
        if parameters:
            operation['parameters'] = parameters
        paths.setdefault(path, {})[method] = operation
    for path, schema, summary in [('/api/sessions', {'type': 'object', 'properties': {
            'mode': {'type': 'string', 'enum': ['voice', 'text'], 'default': 'voice'},
            'workflow': {'type': 'boolean', 'default': False},
            'robot': {'type': 'boolean', 'default': False},
            'profile': {'type': 'object'} }}, '新建并开始访谈'),
            ('/api/sessions/{id}/actions', action_schema, '选分、文字回答或操作')]:
        operation = {'summary': summary, 'requestBody': {'required': True, 'content': {
            'application/json': {'schema': schema}}}, 'responses': {
                '200': {'description': '操作已接收；语音模式通过 GET 查询执行结果'},
                '400': {'description': '参数错误'}, '409': {'description': '状态冲突'}}}
        if '{id}' in path:
            operation['parameters'] = [{'in': 'path', 'name': 'id', 'required': True, 'schema': {'type': 'string'}}]
        paths.setdefault(path, {})['post'] = operation
    paths['/api/robot/tracking'] = {'get': {'summary':'查看眼球跟随状态','responses':{'200':{'description':'成功'}}},
        'post': {'summary':'启动或停止真实眼球跟随最高置信度人脸',
        'requestBody':{'required':True,'content':{'application/json':{'schema':{'type':'object','required':['action'],
        'properties':{'action':{'type':'string','enum':['start','stop']}}}}}},
        'responses':{'200':{'description':'已接收'},'400':{'description':'参数错误'}}}}
    paths['/api/robot/actions'] = {'post': {'summary': '启动待机、手动唤醒或停止自动唤醒',
        'requestBody': {'required': True, 'content': {'application/json': {'schema': {
            'type': 'object', 'required': ['action'], 'properties': {'action': {'type': 'string',
            'enum': ['arm','disarm','wake']}, 'mode': {'type': 'string', 'enum': ['voice','text']}}}}}},
        'responses': {'200': {'description': '操作已接收'}, '409': {'description': '已有会话'}}}}
    paths['/api/school/me']={'get':{'summary':'当前登录身份及学校范围','responses':{'200':{'description':'身份或未登录'}}}}
    for route,title in [('login','登录学校账号'),('logout','退出学校账号'),('ops/{action}','名册、面访、档案、预警与人工转介'),
                        ('reports/{action}','校内报告、量表审校与区域汇总'),('content/{action}','话术、课程和课堂记录'),('account/{action}','校内账号与密码管理')]:
        operation={'summary':title,'responses':{'200':{'description':'完成'},'403':{'description':'无权限'}}}
        if '{action}' in route:
            operation['parameters']=[{'name':'action','in':'path','required':True,'schema':{'type':'string'}}]
        paths['/api/school/'+route]={'post':operation}
    return {'openapi': '3.0.3', 'info': {'title': '小学感受访谈与老师复核 API', 'version': '1.1.0'}, 'paths': paths}


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'OrinInterview/1.0'

        def log_message(self, fmt, *args):
            # Avoid logging answer text or credentials; only route and status.
            if args and str(args[0]).startswith('GET /api/'):
                return
            super().log_message(fmt, *args)

        def send_bytes(self, data, content_type, status=200, download=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; media-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            for cookie in getattr(self,'outgoing_cookies',[]):
                self.send_header('Set-Cookie', cookie)
            if download:
                self.send_header('Content-Disposition', f'attachment; filename="{download}"')
            self.end_headers()
            self.wfile.write(data)

        def send_file(self, file, content_type, download=None):
            size = file.stat().st_size
            start, end, status = 0, size - 1, 200
            range_header = self.headers.get('Range', '')
            if range_header and content_type.startswith(('audio/', 'video/')):
                import re
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', range_header)
                if not match:
                    return self.respond({'error': '无效的音频范围'}, 416)
                start = int(match[1])
                end = min(size - 1, int(match[2])) if match[2] else size - 1
                if start > end or start >= size:
                    return self.respond({'error': '音频范围越界'}, 416)
                status = 206
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(max(0, end - start + 1)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Accept-Ranges', 'bytes')
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            if download:
                self.send_header('Content-Disposition', f'attachment; filename="{download}"')
            self.end_headers()
            with file.open('rb') as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)

        def respond(self, value, status=200):
            self.send_bytes(json.dumps(value, ensure_ascii=False).encode(), 'application/json; charset=utf-8', status)

        def cookie(self,name):
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get('Cookie',''))
            except CookieError:
                return ''
            return jar[name].value if name in jar else ''

        def set_cookie(self,name,value,seconds):
            self.outgoing_cookies.append(f'{name}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={seconds}')

        def access(self,path,method):
            """Guard old media/export routes as well as the new management screens."""
            from .school_portal import require
            portal=service.portal
            actor=self.actor
            parts=path.strip('/').split('/')
            if path.startswith('/api/teacher/'):
                require(actor,'teacher')
                if len(parts)>=4 and parts[2]=='sessions':
                    if not portal.access.allowed(actor,parts[3],raw=True):
                        raise PermissionError('无权读取此记录')
                if len(parts)==4 and parts[2]=='media':
                    with service.store._connect() as db:
                        row=db.execute('SELECT session_id FROM media WHERE id=?',(parts[3],)).fetchone()
                    if not row or not portal.access.allowed(actor,row[0],raw=True):
                        raise PermissionError('无权读取此媒体')
            if len(parts)>=3 and parts[:2]==['api','sessions']:
                sid=parts[2]
                raw=len(parts)>3 and parts[3] not in ('actions',)
                own_active=bool(service.active and service.active.interview.directory.name==sid)
                visit=self.cookie('visit') if own_active else ''
                if not portal.access.allowed(actor,sid,visit,raw=raw):
                    raise PermissionError('无权读取或办理此访谈')
            if method=='GET' and path in ('/api/camera/frame.jpg','/api/camera/stream.mjpg'):
                require(actor,'teacher')
            if method=='POST' and path in ('/api/robot/tracking','/api/camera/actions'):
                require(actor,'teacher')

        def do_GET(self):
            self.dispatch('GET')

        def do_POST(self):
            self.dispatch('POST')

        def dispatch(self, method):
            try:
                host = self.headers.get('Host', '')
                allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
                if host not in allowed:
                    return self.respond({'error': '仅允许本机访问'}, 403)
                origin = self.headers.get('Origin')
                if origin and origin not in {f'http://{item}' for item in allowed}:
                    return self.respond({'error': '跨站请求被拒绝'}, 403)
                path = unquote(urlsplit(self.path).path)
                portal=getattr(service,'portal',None)
                self.outgoing_cookies=[]
                self.actor=None
                if portal:
                    self.actor=portal.access.actor(self.cookie('staff'))
                    self.access(path,method)
                    if path=='/api/school/me' and method=='GET':
                        return self.respond({'actor':self.actor})
                if method == 'GET':
                    return self.get(path)
                if not self.headers.get('Content-Type', '').startswith('application/json'):
                    raise ValueError('请求必须使用 application/json')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= (1048576 if portal and self.actor and path.startswith('/api/school/') else 16384):
                    raise ValueError('JSON 请求大小不正确')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('请求体必须是 JSON 对象')
                if portal and path.startswith('/api/school/'):
                    if path=='/api/school/login':
                        token=portal.access.login(data.get('username'),data.get('password'))
                        self.set_cookie('staff',token,8*3600)
                        self.set_cookie('visit','',0)
                        return self.respond({'actor':portal.access.actor(token)})
                    if path=='/api/school/logout':
                        portal.access.logout(self.cookie('staff'))
                        self.set_cookie('staff','',0)
                        self.set_cookie('visit','',0)
                        return self.respond({'ok':True})
                    parts=path.strip('/').split('/')
                    if len(parts)!=4:
                        raise FileNotFoundError('未知学校接口')
                    result=portal.dispatch(parts[2],parts[3],data,self.actor,service)
                    if (parts[2],parts[3])==('ops','start'):
                        if data.get('handoff') is True:
                            portal.access.logout(self.cookie('staff'))
                            self.set_cookie('staff','',0)
                        self.set_cookie('visit',portal.access.kiosk(result['session_id']),3600*4)
                    return self.respond(result)
                if path == '/api/sessions':
                    kwargs={'school_id':self.actor['school_id']} if portal and self.actor and self.actor['role']=='teacher' else {}
                    result=service.start(data.get('mode', 'voice'),robot=data.get('robot',False),
                                         workflow=data.get('workflow',False),profile=data.get('profile') or {},**kwargs)
                    if portal:
                        self.set_cookie('visit',portal.access.kiosk(result['session_id']),3600*4)
                    return self.respond(result)
                if path == '/api/robot/tracking':
                    robot = getattr(service, 'robot', None)
                    if not robot:
                        raise ValueError('机器人运行时尚未配置')
                    return self.respond(robot.tracking_action(data.get('action')))
                if path == '/api/camera/actions':
                    robot = getattr(service, 'robot', None)
                    if not robot:
                        raise ValueError('摄像头运行时尚未配置')
                    return self.respond(robot.camera_action(data.get('action')))
                if path == '/api/robot/actions':
                    robot = getattr(service, 'robot', None)
                    if not robot:
                        raise ValueError('机器人运行时尚未配置')
                    action = data.get('action')
                    if portal and action in ('arm','disarm'):
                        from .school_portal import require
                        require(self.actor,'teacher')
                    if action == 'arm':
                        result=robot.arm()
                        if portal:
                            self.set_cookie('station',portal.access.pair_device(self.actor['school_id']),365*24*3600)
                        return self.respond(result)
                    if action == 'disarm':
                        return self.respond(robot.disarm())
                    if action == 'wake':
                        kwargs = {'school_id':self.actor['school_id']} if portal and self.actor and self.actor['role']=='teacher' else {}
                        result=robot.wake(mode=data.get('mode'),**kwargs)
                        if portal: self.set_cookie('visit',portal.access.kiosk(result['session_id']),3600*4)
                        return self.respond(result)
                    raise ValueError('未知机器人操作')
                parts = path.strip('/').split('/')
                if len(parts) == 4 and parts[:2] == ['api', 'sessions'] and parts[3] == 'actions':
                    return self.respond(service.action(parts[2], data.get('action'), text=data.get('text'),
                                                       score=data.get('score'), question_id=data.get('question_id')))
                raise FileNotFoundError('接口不存在')
            except PermissionError as error:
                self.respond({'error':str(error)},403)
            except Conflict as error:
                self.respond({'error': str(error)}, 409)
            except (ValueError, TypeError) as error:
                self.respond({'error': str(error)}, 400)
            except FileNotFoundError as error:
                self.respond({'error': str(error)}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:
                self.respond({'error': '服务操作失败：' + str(error)}, 500)

        def camera(self, stream=False):
            robot = getattr(service, 'robot', None)
            if not robot:
                return self.respond({'error': '摄像头运行时尚未配置'}, 503)
            robot.camera_action('start')
            hub = robot.camera
            frame = hub.wait_frame(after_sequence=-1, timeout=4)
            if frame is None:
                return self.respond({'error': hub.status().get('error') or '暂时没有摄像头画面'}, 503)
            if not stream:
                return self.send_bytes(frame.jpeg, 'image/jpeg')
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            sequence = -1
            try:
                while hub.is_alive():
                    if frame is not None and frame.sequence > sequence:
                        self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' +
                                         str(len(frame.jpeg)).encode() + b'\r\n\r\n' + frame.jpeg + b'\r\n')
                        self.wfile.flush()
                        sequence = frame.sequence
                    frame = hub.wait_frame(after_sequence=sequence, timeout=2)
                    if frame is None:
                        break
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            self.close_connection = True

        def get(self, path):
            portal=getattr(service,'portal',None)
            if portal and path in ('/school','/teacher','/admin','/school.js','/school.css','/school_ops_ui.js','/school_reports.js','/school_content.js'):
                name='school.html' if path in ('/school','/teacher','/admin') else path[1:]
                return self.send_bytes((STATIC / name).read_bytes(),(mimetypes.guess_type(name)[0] or 'text/plain')+'; charset=utf-8')
            if portal and path=='/teacher/records':
                from .school_portal import require
                require(self.actor,'teacher')
                return self.send_bytes((STATIC/'admin.html').read_bytes(),'text/html; charset=utf-8')
            clinical = getattr(service, 'clinical_store', None) or getattr(service, 'store', None)
            if path == '/api/teacher/tasks':
                if not clinical:
                    return self.respond({'error': '管理数据存储尚未配置'}, 503)
                return self.respond({'tasks': clinical.list_tasks()})
            if path == '/api/teacher/sessions':
                if not clinical:
                    return self.respond({'error': '管理数据存储尚未配置'}, 503)
                if portal:
                    return self.respond({'sessions':portal.access.school_sessions(self.actor['school_id'])})
                # Index the JSON exports created by older and current interview
                # workers.  sync_answers_json is idempotent and keeps the
                # teacher view useful even when no intake form was completed.
                for file in service.output.glob('*/answers.json'):
                    try:
                        clinical.sync_answers_json(file.parent)
                    except (OSError, ValueError, KeyError):
                        continue
                return self.respond({'sessions': clinical.list_sessions()})
            teacher_parts = path.strip('/').split('/')
            if len(teacher_parts) == 4 and teacher_parts[:3] == ['api', 'teacher', 'sessions']:
                if not clinical:
                    return self.respond({'error': '管理数据存储尚未配置'}, 503)
                return self.respond(clinical.get_session(teacher_parts[3]))
            if len(teacher_parts) == 5 and teacher_parts[:3] == ['api', 'teacher', 'sessions'] and teacher_parts[4] == 'export':
                if not clinical:
                    return self.respond({'error': '管理数据存储尚未配置'}, 503)
                return self.send_bytes(json.dumps(clinical.get_session(teacher_parts[3]), ensure_ascii=False, indent=2).encode('utf-8'),
                                       'application/json; charset=utf-8', download=f'{teacher_parts[3]}.json')
            if len(teacher_parts) == 4 and teacher_parts[:3] == ['api', 'teacher', 'media']:
                if not clinical:
                    return self.respond({'error': '管理数据存储尚未配置'}, 503)
                media = clinical.get_media(teacher_parts[3])
                content_type = media.get('mime_type') or ('video/mp4' if media.get('kind') == 'video' else 'audio/wav')
                return self.send_file(media['absolute_path'], content_type)
            if path == '/api/robot/tracking':
                robot = getattr(service, 'robot', None)
                return self.respond(robot.tracking_status() if robot else {'enabled':False,'phase':'unconfigured'})
            if path == '/api/camera/status':
                robot = getattr(service, 'robot', None)
                return self.respond(robot.camera_status() if robot else {'phase': 'unconfigured'})
            if path == '/api/camera/frame.jpg':
                return self.camera(stream=False)
            if path == '/api/camera/stream.mjpg':
                return self.camera(stream=True)
            if path == '/api/openapi.json':
                return self.respond(specification())
            if path == '/api/status':
                active = service.active.read() if service.active else None
                if (portal and active and active.get('status')=='in_progress' and not self.actor and
                    active.get('wake_event',{} ) and active['wake_event'].get('trigger')!='manual' and
                    not portal.access.allowed(None,active['session_id'],self.cookie('visit')) and
                    portal.access.paired(self.cookie('station'),service.active.school_id)):
                    self.set_cookie('visit',portal.access.kiosk(active['session_id']),4*3600)
                    return self.respond({'active_session_id':active['session_id'],'phase':active['phase']})
                if portal and active and not portal.access.allowed(self.actor,active['session_id'],self.cookie('visit')):
                    return self.respond({'active_session_id':None,'phase':'occupied' if active.get('status')=='in_progress' else 'idle'})
                return self.respond({'active_session_id': active['session_id'] if active else None,
                                     'phase': active['phase'] if active else 'idle'})
            if path == '/api/robot/status':
                robot = getattr(service, 'robot', None)
                return self.respond(robot.status() if robot else {'enabled': False, 'phase': 'unconfigured'})
            if path == '/api/questions':
                return self.respond(json.loads(service.questions.read_text(encoding='utf-8')))
            if path == '/api/sessions':
                if portal:
                    if self.actor and self.actor['role']=='teacher':
                        return self.respond({'sessions':portal.access.school_sessions(self.actor['school_id'])})
                    return self.respond({'sessions':[]})
                return self.respond({'sessions': service.list_sessions()})
            parts = path.strip('/').split('/')
            if len(parts) >= 3 and parts[:2] == ['api', 'sessions']:
                session_id = parts[2]
                if len(parts) == 3:
                    return self.respond(service.get(session_id))
                if len(parts) == 4 and parts[3] == 'teacher':
                    return self.respond(service.store.get_session(session_id))
                base = service.session_path(session_id)
                if len(parts) == 4 and parts[3] in {'answers.json', 'summary.txt', 'session_package.json', 'session_package.zip', 'transcript.json'}:
                    file = base / parts[3]
                    return self.send_file(file, 'application/zip' if file.suffix == '.zip' else
                                          ('application/json; charset=utf-8' if file.suffix == '.json' else 'text/plain; charset=utf-8'),
                                          download=parts[3])
                if len(parts) == 5 and parts[3] in {'audio','full_audio'} and parts[4].endswith('.wav'):
                    file = (base / parts[3] / parts[4]).resolve()
                    if not file.is_relative_to(base) or not file.is_relative_to((base / parts[3]).resolve()):
                        raise FileNotFoundError('录音不存在')
                    return self.send_file(file, 'audio/wav')
                if len(parts) == 5 and parts[3] == 'video' and parts[4].endswith(('.avi', '.mp4', '.webm')):
                    file = (base / parts[3] / parts[4]).resolve()
                    if not file.is_relative_to(base) or not file.is_relative_to((base / parts[3]).resolve()):
                        raise FileNotFoundError('录像不存在')
                    mime = {'avi': 'video/x-msvideo', 'mp4': 'video/mp4', 'webm': 'video/webm'}[file.suffix[1:]]
                    return self.send_file(file, mime)
                raise FileNotFoundError('文件不存在')
            static_names = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/api/docs': 'api.html', '/camera': 'camera.html',
                            '/camera.js': 'camera.js', '/camera.css': 'camera.css', '/teacher': 'admin.html', '/admin': 'admin.html',
                            '/admin.js': 'admin.js', '/admin.css': 'admin.css',
                            '/teacher.js': 'admin.js', '/teacher.css': 'admin.css'}
            if path in static_names:
                file = STATIC / static_names[path]
                mime = mimetypes.guess_type(file.name)[0] or 'text/plain'
                return self.send_bytes(file.read_bytes(), mime + '; charset=utf-8')
            raise FileNotFoundError('页面不存在')
    return Handler


def argument_parser():
    parser = argparse.ArgumentParser(description='小学感受访谈网页、3+1任务与老师复核 API')
    parser.add_argument('--port', type=int, default=18088)
    parser.add_argument('--open-browser', action='store_true')
    parser.add_argument('--open-school', action='store_true', help='在本机浏览器打开学校工作台')
    parser.add_argument('--camera', action='store_true', help='打开本地摄像头实时预览，不启动语音唤醒或运动')
    parser.add_argument('--track', action='store_true', help='启用YOLO最高置信度人脸的真实眼球跟随')
    parser.add_argument('--robot', action='store_true', help='启动后进入机器人待机，启用人物或语音唤醒')
    parser.add_argument('--output', default=str(ROOT / 'sessions'))
    parser.add_argument('--questions', default=str(ROOT / 'configs/primary21.zh.json'),
                        help='题单JSON，默认自编小学版；专业人员可显式选择原DASS题单')
    chat_profiles = list(load_json('configs/profiles.json'))
    parser.add_argument('--chat-profile', choices=chat_profiles, default=None,
                        help='机器人自由对话使用的本地模型配置；默认沿用configs/robot.json')
    parser.add_argument('--chat-port', type=int, default=None,
                        help='机器人自由对话本地推理端口；默认沿用configs/robot.json')
    parser.add_argument('--chat-max-tokens', type=int, default=None,
                        help='机器人自由对话每次最大输出token数；默认沿用configs/robot.json')
    return parser


def main():
    parser = argument_parser()
    args = parser.parse_args()
    service = InterviewService(output=args.output, questions=args.questions)
    from .school_portal import SchoolPortal
    service.portal = SchoolPortal(service.store)
    from .head_hardware import HeadHardware
    from .robot_runtime import RobotRuntime
    from .presence import load_robot_config
    robot_config = load_robot_config()
    chat_config = dict(robot_config.get('chat', {}))
    if args.chat_profile is not None:
        chat_config['profile'] = args.chat_profile
    if args.chat_port is not None:
        chat_config['port'] = args.chat_port
    if args.chat_max_tokens is not None:
        chat_config['max_tokens'] = args.chat_max_tokens
    robot_config['chat'] = chat_config
    service.robot = robot = RobotRuntime(service, config=robot_config, hardware=HeadHardware())
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(service))
    except OSError as error:
        parser.exit(1, f'无法启动本机网页服务：{error}。请确认端口未被占用，并在本机桌面终端运行。\n')
    print(f'访谈界面：http://127.0.0.1:{args.port}\nAPI 文档：http://127.0.0.1:{args.port}/api/docs', flush=True)
    if args.robot:
        robot.arm()
    if args.camera:
        robot.camera_action('start')
    if args.track:
        robot.tracking_action('start')
    if args.open_browser or args.open_school:
        page = '/school' if args.open_school else ('/camera' if args.camera else '/')
        opener = threading.Timer(.5, webbrowser.open, args=(f'http://127.0.0.1:{args.port}'+page,))
        opener.daemon = True
        opener.start()
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        robot.enabled = False
        robot.stop_event.set()
        service.shutdown()
        server.server_close()
        if service.worker:
            service.worker.join(timeout=20)
        robot.shutdown()


if __name__ == '__main__':
    main()
