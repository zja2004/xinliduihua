"""One shared interview state for local HTTP controls and the audio worker."""
import copy
import fcntl
import uuid
import json
import os
import queue
import re
import subprocess
import threading
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from .interview import Interview
from .settings import ROOT


class Conflict(ValueError):
    pass


class SessionControl:
    def __init__(self, interview, mode):
        self.interview = interview
        self.mode = mode
        self.lock = threading.RLock()
        self.commands = queue.Queue(maxsize=16)
        self.stop = threading.Event()
        self.interrupt = lambda: None
        self.phase = 'preparing' if mode == 'voice' else 'ready'
        self.error = None
        self.events = deque(maxlen=150)
        self.snapshot = {}
        self.audio_state = {}
        self.update()

    def update(self, phase=None, role=None, text=None):
        with self.lock:
            if phase:
                self.phase = phase
            if role and text:
                self.events.append({'id': len(self.events) + getattr(self, '_discarded', 0),
                                    'at': datetime.now(timezone.utc).isoformat(), 'role': role, 'text': text})
                if len(self.events) == self.events.maxlen:
                    self._discarded = getattr(self, '_discarded', 0) + 1
            self.snapshot = copy.deepcopy(self.interview.data)
            self.snapshot.update(mode=self.mode, phase=self.phase, error=self.error,
                                 current_index=self.interview.index,
                                 current_question=copy.deepcopy(self.interview.current),
                                 events=list(self.events), audio=dict(self.audio_state))

    def set_audio(self, values):
        with self.lock:
            self.audio_state = dict(values)
            self.snapshot['audio'] = dict(values)

    def read(self):
        with self.lock:
            return copy.deepcopy(self.snapshot)

    def finish(self):
        if self.interview.data['status'] == 'completed':
            self.interview.finish_summary(self.interview.basic_summary())
            return self.interview.basic_summary()
        return ''


@contextmanager
def audio_route():
    lock_path = ROOT / '.runtime/audio-route.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('另一个访谈进程正在使用音频，请先结束旧访谈程序。') from error
        try:
            with _audio_route() as route:
                yield route
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def _audio_route():
    """Use the same PulseAudio AEC route as duplex.sh, with guaranteed cleanup."""
    def pactl(*args):
        try:
            return subprocess.run(['pactl', *args], check=True, capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError('无法连接或配置桌面音频服务，请在开发板已登录的桌面终端启动网页服务。') from error
    pactl('info')
    sources = [line.split()[1] for line in pactl('list', 'short', 'sources').splitlines()]
    sinks = [line.split()[1] for line in pactl('list', 'short', 'sinks').splitlines()]
    def select(items, default, override):
        if override:
            return override
        physical = [name for name in items if not name.endswith('.monitor') and not name.startswith('orin_aec_')]
        for pattern in ('me01tc', 'tw16_pro'):
            match = next((name for name in physical if pattern in name.lower()), None)
            if match:
                return match
        chosen = pactl(default)
        if chosen not in physical:
            raise RuntimeError('默认音频设备不是物理设备，请通过 ORIN_MIC_SOURCE / ORIN_SPEAKER_SINK 指定。')
        return chosen
    mic = select(sources, 'get-default-source', os.environ.get('ORIN_MIC_SOURCE'))
    speaker = select(sinks, 'get-default-sink', os.environ.get('ORIN_SPEAKER_SINK'))
    if mic.endswith('.monitor') or mic.startswith('orin_aec_') or speaker.startswith('orin_aec_'):
        raise ValueError('请选择物理麦克风和输出设备')
    suffix = f'_{os.getpid()}_{uuid.uuid4().hex[:8]}'
    source_name, sink_name = 'orin_aec_source' + suffix, 'orin_aec_sink' + suffix
    module_id = pactl('load-module', 'module-echo-cancel', f'source_master={mic}',
                      f'sink_master={speaker}', f'source_name={source_name}',
                      f'sink_name={sink_name}', 'aec_method=webrtc', 'rate=48000', 'channels=1')
    previous = {key: os.environ.get(key) for key in ('PULSE_SOURCE', 'PULSE_SINK')}
    os.environ.update(PULSE_SOURCE=source_name, PULSE_SINK=sink_name)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            pactl('unload-module', module_id)
        except RuntimeError:
            pass


class InterviewService:
    def __init__(self, output=None, questions=None, voice_runner=None):
        self.output = Path(output or ROOT / 'sessions')
        self.questions = Path(questions or ROOT / 'configs/dass21.zh.json')
        self.lock = threading.RLock()
        self.active = None
        self.worker = None
        self.voice_runner = voice_runner or self._voice

    def start(self, mode='voice'):
        if mode not in {'voice', 'text'}:
            raise ValueError('mode 必须是 voice 或 text')
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise Conflict('语音任务仍在运行或释放设备，请稍后再开始')
            if self.active and self.active.read()['status'] == 'in_progress':
                raise Conflict('已有访谈进行中，请先结束当前访谈')
            interview = Interview(self.questions, self.output)
            self.active = control = SessionControl(interview, mode)
            control.update(role='assistant', text=interview.introduction)
            if mode == 'voice':
                self.worker = threading.Thread(target=self._run_voice, args=(control,), daemon=True)
                self.worker.start()
            return control.read()

    def _voice(self, control):
        from .duplex import run
        with audio_route():
            args = SimpleNamespace(questions=str(self.questions), output=str(self.output),
                                   speech_threshold=.015, silence_ms=600, input_device=None,
                                   profile='baseline', port=18080, gpu_layers=99, ctx_size=4096, max_tokens=160)
            run(args, control=control)

    def _run_voice(self, control):
        try:
            self.voice_runner(control)
            if control.interview.data['status'] == 'in_progress':
                control.interview.accept('结束访谈')
            control.update(phase=control.interview.data['status'])
        except Exception as error:
            control.error = str(error)
            if control.interview.data['status'] == 'in_progress':
                control.interview.data['status'] = 'stopped'
                control.interview.save()
            control.update(phase='error', role='system', text=str(error))

    def action(self, session_id, action, text=None, score=None, question_id=None):
        commands = {'repeat': '重复', 'skip': '跳过这题', 'previous': '上一题',
                    'options': '选项', 'stop': '结束访谈'}
        if action == 'answer':
            if score is not None:
                if type(score) is not int or not 0 <= score <= 3:
                    raise ValueError('score 必须是 0–3 的整数')
                text = f'{score}分'
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                raise ValueError('回答必须是 1–2000 字的文字或有效分值')
        elif action in commands:
            text = commands[action]
        else:
            raise ValueError('未知操作')
        with self.lock:
            control = self.active
            if not control or control.interview.directory.name != session_id:
                raise Conflict('只能操作当前访谈')
            snapshot = control.read()
            if snapshot['status'] != 'in_progress' or snapshot['phase'] == 'error':
                raise Conflict('当前访谈已结束')
            current_id = (snapshot['current_question'] or {}).get('id')
            if question_id is not None and question_id != current_id and action != 'stop':
                raise Conflict('题目已经变化，请刷新后重试')
            if control.mode == 'voice':
                if action == 'stop':
                    control.stop.set()
                    control.interrupt()
                    control.update(phase='stopping')
                else:
                    if snapshot['phase'] in {'preparing', 'stopping'}:
                        raise Conflict('语音设备正在准备或停止，请稍后操作')
                    try:
                        control.commands.put_nowait((text, current_id))
                    except queue.Full as error:
                        raise Conflict('操作积压，请稍后重试') from error
                    control.interrupt()
                return control.read()
            reply = control.interview.accept(text)
            reply += control.finish()
            control.update(phase='completed' if control.interview.data['status'] == 'completed' else
                           ('stopped' if action == 'stop' else 'ready'), role='user', text=text)
            control.update(role='assistant', text=reply)
            return control.read()

    def list_sessions(self):
        result = []
        for file in sorted(self.output.glob('*/answers.json'), reverse=True)[:100]:
            try:
                data = json.loads(file.read_text(encoding='utf-8'))
                result.append({key: data.get(key) for key in
                               ('session_id', 'created_at', 'status', 'completed_count', 'total_questions')})
            except (OSError, ValueError):
                continue
        return result

    def session_path(self, session_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', session_id):
            raise FileNotFoundError('会话不存在')
        path = (self.output / session_id).resolve()
        if not path.is_relative_to(self.output.resolve()) or not (path / 'answers.json').is_file():
            raise FileNotFoundError('会话不存在')
        return path

    def get(self, session_id):
        with self.lock:
            if self.active and self.active.interview.directory.name == session_id:
                return self.active.read()
        data = json.loads((self.session_path(session_id) / 'answers.json').read_text(encoding='utf-8'))
        # Display old saved sessions in simplified script without rewriting original files.
        config = json.loads(self.questions.read_text(encoding='utf-8'))
        mapping = {q.get('source_text', q['text']): q['text'] for q in config['questions']}
        for q in data['questions']:
            q['question'] = mapping.get(q['question'], q['question'])
        data.update(phase='archived', mode='archived', current_question=None, events=[])
        return data

    def shutdown(self):
        with self.lock:
            if self.active and self.active.read()['status'] == 'in_progress':
                if self.active.mode == 'voice':
                    self.active.stop.set()
                    self.active.interrupt()
                else:
                    self.active.interview.accept('结束访谈')
                    self.active.update(phase='stopped')
