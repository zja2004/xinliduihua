"""Dependency-free local HTTP API and interview console."""
import argparse
import json
import mimetypes
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote

from .service import InterviewService, Conflict
from .settings import ROOT

STATIC = Path(__file__).with_name('static')


def specification():
    action_schema = {'type': 'object', 'required': ['action'], 'properties': {
        'action': {'type': 'string', 'enum': ['answer', 'repeat', 'skip', 'previous', 'options', 'stop']},
        'text': {'type': 'string', 'maxLength': 2000}, 'score': {'type': 'integer', 'minimum': 0, 'maximum': 3},
        'question_id': {'type': 'string', 'description': 'Expected current question; stale requests return 409'}}}
    paths = {}
    for path, method, summary in [('/api/status', 'get', '当前运行状态'),
                                  ('/api/questions', 'get', '简体题目与选项'),
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
            'mode': {'type': 'string', 'enum': ['voice', 'text'], 'default': 'voice'}}}, '新建并开始访谈'),
            ('/api/sessions/{id}/actions', action_schema, '选分、文字回答或操作')]:
        operation = {'summary': summary, 'requestBody': {'required': True, 'content': {
            'application/json': {'schema': schema}}}, 'responses': {
                '200': {'description': '操作已接收；语音模式通过 GET 查询执行结果'},
                '400': {'description': '参数错误'}, '409': {'description': '状态冲突'}}}
        if '{id}' in path:
            operation['parameters'] = [{'in': 'path', 'name': 'id', 'required': True, 'schema': {'type': 'string'}}]
        paths.setdefault(path, {})['post'] = operation
    return {'openapi': '3.0.3', 'info': {'title': '本地 DASS-21 语音访谈 API', 'version': '1.0.0'}, 'paths': paths}


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
            if download:
                self.send_header('Content-Disposition', f'attachment; filename="{download}"')
            self.end_headers()
            self.wfile.write(data)

        def respond(self, value, status=200):
            self.send_bytes(json.dumps(value, ensure_ascii=False).encode(), 'application/json; charset=utf-8', status)

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
                if method == 'GET':
                    return self.get(path)
                if not self.headers.get('Content-Type', '').startswith('application/json'):
                    raise ValueError('请求必须使用 application/json')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    raise ValueError('JSON 请求大小不正确')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('请求体必须是 JSON 对象')
                if path == '/api/sessions':
                    return self.respond(service.start(data.get('mode', 'voice')))
                parts = path.strip('/').split('/')
                if len(parts) == 4 and parts[:2] == ['api', 'sessions'] and parts[3] == 'actions':
                    return self.respond(service.action(parts[2], data.get('action'), text=data.get('text'),
                                                       score=data.get('score'), question_id=data.get('question_id')))
                raise FileNotFoundError('接口不存在')
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

        def get(self, path):
            if path == '/api/openapi.json':
                return self.respond(specification())
            if path == '/api/status':
                active = service.active.read() if service.active else None
                return self.respond({'active_session_id': active['session_id'] if active else None,
                                     'phase': active['phase'] if active else 'idle'})
            if path == '/api/questions':
                return self.respond(json.loads(service.questions.read_text(encoding='utf-8')))
            if path == '/api/sessions':
                return self.respond({'sessions': service.list_sessions()})
            parts = path.strip('/').split('/')
            if len(parts) >= 3 and parts[:2] == ['api', 'sessions']:
                session_id = parts[2]
                if len(parts) == 3:
                    return self.respond(service.get(session_id))
                base = service.session_path(session_id)
                if len(parts) == 4 and parts[3] in {'answers.json', 'summary.txt'}:
                    file = base / parts[3]
                    return self.send_bytes(file.read_bytes(), 'application/json; charset=utf-8' if file.suffix == '.json'
                                           else 'text/plain; charset=utf-8', download=parts[3])
                if len(parts) == 5 and parts[3] == 'audio' and parts[4].endswith('.wav'):
                    file = (base / 'audio' / parts[4]).resolve()
                    if not file.is_relative_to(base) or not file.is_relative_to((base / 'audio').resolve()):
                        raise FileNotFoundError('录音不存在')
                    return self.send_bytes(file.read_bytes(), 'audio/wav')
                raise FileNotFoundError('文件不存在')
            static_names = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/api/docs': 'api.html'}
            if path in static_names:
                file = STATIC / static_names[path]
                mime = mimetypes.guess_type(file.name)[0] or 'text/plain'
                return self.send_bytes(file.read_bytes(), mime + '; charset=utf-8')
            raise FileNotFoundError('页面不存在')
    return Handler


def main():
    parser = argparse.ArgumentParser(description='本机 DASS-21 语音访谈网页与 API')
    parser.add_argument('--port', type=int, default=18088)
    parser.add_argument('--open-browser', action='store_true')
    parser.add_argument('--output', default=str(ROOT / 'sessions'))
    args = parser.parse_args()
    service = InterviewService(output=args.output)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(service))
    except OSError as error:
        parser.exit(1, f'无法启动本机网页服务：{error}。请确认端口未被占用，并在本机桌面终端运行。\n')
    print(f'访谈界面：http://127.0.0.1:{args.port}\nAPI 文档：http://127.0.0.1:{args.port}/api/docs', flush=True)
    if args.open_browser:
        opener = threading.Timer(.5, webbrowser.open, args=(f'http://127.0.0.1:{args.port}',))
        opener.daemon = True
        opener.start()
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        service.shutdown()
        server.server_close()
        if service.worker:
            service.worker.join(timeout=10)


if __name__ == '__main__':
    main()
