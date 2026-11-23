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
from .clinical_store import ClinicalStore
from .video_recorder import CameraVideoRecorder


class Conflict(ValueError):
    pass


class SessionControl:
    def __init__(self, interview, mode, robot_runtime=None, video_recorder=None):
        self.robot_runtime = robot_runtime
        self.hardware = robot_runtime.hardware if robot_runtime else None
        self.video_recorder = video_recorder
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
        self.portal = None
        self.school_id = None
        self.update()

    @property
    def introduction(self):
        if self.robot_runtime and self.interview.data.get('stage') == 'intake':
            return '开始心理对话前，我先确认几项基本资料。' + self.interview.prompt
        if self.robot_runtime:
            from .robot_dialogue import ROBOT_GREETING
            return ROBOT_GREETING
        return self.interview.introduction

    def respond(self, text, samples=None, rate=None, cancel=None, reply_executor=None):
        from .interview import intent
        from .robot_dialogue import requests_assessment
        stage = self.interview.data.get('stage', 'assessment')
        if self.portal and intent(text) != 'stop':
            if self.interview.data.get('safety_hold'):
                return '我们先停在这里。请找身边信任的大人陪你，也可以说结束访谈。'
            from .school_ops import risk_signal
            if risk_signal(text):
                metadata = {'stage': stage, 'kind': 'safety_disclosure'}
                if samples is not None:
                    from .audio import save_wav
                    relative = 'audio/safety-' + uuid.uuid4().hex + '.wav'
                    save_wav(self.interview.directory / relative, samples, rate)
                    metadata['audio_path'] = relative
                event = self.interview.record_event('user', text, **metadata)
                self.portal.ops.capture_risk(self.school_id, self.interview.directory.name, text, stage, event_id=event)
                self.interview.data['safety_hold'] = True
                self.interview.save()
                self.interview.record_event('system','访谈暂停，安全线索待老师查看。',kind='safety_hold')
                return '谢谢你告诉我。我们先停下来。请现在找身边信任的大人陪着你，一起去安全的地方。如果眼前有危险，请马上向身边的大人或当地紧急服务求助。'
        if self.robot_runtime and self.interview.data.get('stage') == 'intake' and intent(text) != 'stop':
            return self.interview.accept(text, samples, rate)
        if self.robot_runtime and self.interview.data.get('stage') == 'greeting' and intent(text) != 'stop':
            metadata = {}
            if samples is not None:
                from .audio import save_wav
                import time
                relative = f'audio/prelude-{time.monotonic_ns()}.wav'
                save_wav(self.interview.directory / relative, samples, rate)
                metadata['audio_path'] = relative
            self.interview.record_event('user', text, question_id=None, stage='greeting', **metadata)
            if requests_assessment(text):
                self.interview.data['stage'] = 'assessment'
                self.interview.save()
                return '好的，我们开始，请根据过去一周的情况作答。' + self.interview.prompt
            if self.portal:
                reply = self.portal.content.approved_response(self.school_id, text)
                if reply: return reply
            history = [{'role': event['role'], 'content': event['text']} for event in
                       self.interview.journal.snapshot() if event['role'] in {'user','assistant'}][-8:]
            if reply_executor is not None:
                from .duplex import cancellable
                return cancellable(reply_executor, cancel, self.robot_runtime.dialogue.reply,
                                   text, history=history[:-1], cancel=cancel)
            return self.robot_runtime.dialogue.reply(text, history=history[:-1], cancel=cancel)
        return self.interview.accept(text, samples, rate)

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
                                 events=list(self.events), audio=dict(self.audio_state),
                                 export_ready=(self.interview.directory / 'session_package.zip').is_file())
            if self.video_recorder:
                self.snapshot['video'] = self.video_recorder.status()
            manifest = self.interview.directory / 'full_audio/manifest.json'
            if manifest.is_file():
                self.snapshot['full_audio'] = json.loads(manifest.read_text())

    def set_audio(self, values):
        with self.lock:
            self.audio_state = dict(values)
            self.snapshot['audio'] = dict(values)

    def read(self):
        with self.lock:
            result = copy.deepcopy(self.snapshot)
            if self.video_recorder:
                result['video'] = self.video_recorder.status()
            return result

    def finish(self):
        if self.interview.data['status'] == 'completed':
            self.interview.finish_summary(self.interview.basic_summary())
            return self.interview.basic_summary()
        return ''

    def close_media(self):
        if self.video_recorder:
            self.video_recorder.stop()
            self.update()


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
    # The dedicated remote mic carries only the controller's voice.
    mic_override = os.environ.get('ORIN_MIC_SOURCE')
    if not mic_override and 'xinyu_todesk_mic' in sources:
        mic_override = 'xinyu_todesk_mic'
    mic = select(sources, 'get-default-source', mic_override)
    speaker = select(sinks, 'get-default-sink', os.environ.get('ORIN_SPEAKER_SINK'))
    if speaker == 'xinyu_todesk_voice':
        raise ValueError('请选择用于播报的扬声器。ToDesk 远端话音接收设备仅用于麦克风输入。')
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
    def __init__(self, output=None, questions=None, voice_runner=None, store=None):
        self.output = Path(output or ROOT / 'sessions')
        self.questions = Path(questions or ROOT / 'configs/primary21.zh.json')
        self.lock = threading.RLock()
        self.active = None
        self.worker = None
        self.voice_runner = voice_runner or self._voice
        self.store = store or ClinicalStore(self.output)
        self.portal = None

    def start(self, mode='voice', robot=False, wake_event=None, workflow=False, profile=None, school_id='local', enrollment=None):
        if mode not in {'voice', 'text'}:
            raise ValueError('mode 必须是 voice 或 text')
        if profile is not None and not isinstance(profile, dict):
            raise ValueError('profile 必须是基本资料对象')
        profile = dict(profile or {})
        for field, minimum, maximum in (('height_cm', 30, 250), ('weight_kg', 2, 400), ('age', 1, 130)):
            if profile.get(field) not in (None, ''):
                try:
                    value = float(profile[field])
                except (ValueError, TypeError) as error:
                    raise ValueError(field + ' 必须是数字') from error
                if not minimum <= value <= maximum or (field == 'age' and value % 1):
                    raise ValueError(field + ' 超出可填写范围')
        if len(str(profile.get('name', ''))) > 120:
            raise ValueError('姓名不能超过120字')
        runtime = getattr(self, 'robot', None)
        if runtime and (mode == 'voice' or robot):
            runtime.pause_voice()
        try:
            with self.lock:
                if self.worker and self.worker.is_alive():
                    raise Conflict('语音任务仍在运行或释放设备，请稍后再开始')
                if self.active and self.active.read()['status'] == 'in_progress':
                    raise Conflict('已有访谈进行中，请先结束当前访谈')
                workflow = bool(workflow)
                interview = Interview(self.questions, self.output, workflow=workflow, participant=profile)
                interview.data['mode'] = mode
                interview.data['robot_mode'] = bool(robot)
                interview.save()
                if self.portal:
                    sid = interview.directory.name
                    try:
                        self.portal.access.set_scope(sid, school_id)
                        self.store.sync_answers_json(interview.directory)
                        if enrollment:
                            student, term, appointment = enrollment
                            self.portal.ops.link_session(school_id, student, sid, term, appointment or None)
                    except Exception:
                        interview.data['status'] = 'stopped'
                        interview.save()
                        self._sync_store(interview)
                        raise
                if robot:
                    interview.data.update(stage=('intake' if interview.data.get('stage') == 'intake' else 'greeting'),
                                          wake_event=wake_event)
                    interview.save()
                    dialogue = getattr(runtime, 'dialogue', None)
                    if dialogue and callable(getattr(dialogue, 'start', None)):
                        dialogue.start()
                video = None
                if runtime and (robot or workflow) and getattr(runtime, 'camera', None) is not None:
                    camera_config = getattr(runtime, 'config', {}).get('vision', {})
                    video = CameraVideoRecorder(runtime.camera, interview.directory,
                                                fps=camera_config.get('fps', 12),
                                                width=camera_config.get('width', 640),
                                                height=camera_config.get('height', 480),
                                                origin=interview.journal.origin)
                self.active = control = SessionControl(interview, mode, runtime if robot else None, video)
                control.portal = self.portal
                control.school_id = school_id
                if video:
                    video.start()
                self._sync_store(interview)
                if mode == 'text':
                    interview.record_event('assistant', control.introduction, delivery='text')
                control.update(role='assistant', text=control.introduction)
                if mode == 'voice':
                    self.worker = threading.Thread(target=self._run_voice, args=(control,), daemon=True)
                    self.worker.start()
                return control.read()
        finally:
            if runtime:
                runtime.release_voice()

    def _sync_store(self, interview):
        try:
            self.store.sync_answers_json(interview.directory)
            if self.portal:
                sid = interview.directory.name
                scope = self.portal.access.school_for(sid)
                if scope:
                    self.portal.ops.sync_session(scope, sid, interview.data)
        except (OSError, ValueError, KeyError, TypeError) as error:
            # SQLite is an index for review; a database outage must not lose the
            # portable answers.json session record.
            interview.data['store_warning'] = str(error)

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
                control.interview.data['status'] = 'stopped'
                control.interview.record_event('system', '语音任务结束', kind='session_stop', source='worker_exit')
                control.interview.save()
            control.close_media()
            self._sync_store(control.interview)
            control.interview.export()
            control.update(phase=control.interview.data['status'])
        except Exception as error:
            control.error = str(error)
            if control.interview.data['status'] == 'in_progress':
                control.interview.data['status'] = 'stopped'
                control.interview.save()
            control.interview.record_event('system', str(error), kind='worker_error')
            control.close_media()
            control.interview.export()
            self._sync_store(control.interview)
            control.update(phase='error', role='system', text=str(error))

    def action(self, session_id, action, text=None, score=None, question_id=None):
        commands = {'repeat': '重复', 'skip': '跳过这题', 'previous': '上一题',
                    'options': '选项', 'finish': '回答完了', 'stop': '结束访谈'}
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
            reply = control.respond(text)
            reply += control.finish()
            control.update(phase='completed' if control.interview.data['status'] == 'completed' else
                           ('stopped' if action == 'stop' else 'ready'), role='user', text=text)
            control.interview.record_event('assistant', reply, delivery='text')
            control.update(role='assistant', text=reply)
            if control.interview.data['status'] != 'in_progress':
                control.close_media()
                control.interview.export()
                self._sync_store(control.interview)
                control.update()
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
        instrument = data.get('instrument')
        archive_questions = (ROOT / 'configs/dass21.zh.json' if instrument == 'DASS-21' else
                             ROOT / 'configs/primary21.zh.json' if instrument == 'PRIMARY-21' else self.questions)
        config = json.loads(archive_questions.read_text(encoding='utf-8'))
        mapping = {q.get('source_text', q['text']): q['text'] for q in config['questions']}
        for q in data['questions']:
            q['question'] = mapping.get(q['question'], q['question'])
        from .session_export import EventJournal
        events = EventJournal(self.session_path(session_id)).snapshot() if (self.session_path(session_id) / 'events.jsonl').is_file() else []
        manifest = self.session_path(session_id) / 'full_audio/manifest.json'
        data.update(phase='archived', mode='archived', current_question=None, events=events,
                    export_ready=(self.session_path(session_id) / 'session_package.zip').is_file(),
                    full_audio=json.loads(manifest.read_text()) if manifest.is_file() else None)
        return data

    def shutdown(self):
        with self.lock:
            if self.active and self.active.read()['status'] == 'in_progress':
                if self.active.mode == 'voice':
                    self.active.stop.set()
                    self.active.interrupt()
                else:
                    self.active.interview.data['status'] = 'stopped'
                    self.active.interview.record_event('system', '服务关闭', kind='session_stop', source='shutdown')
                    self.active.interview.save()
                    self.active.close_media()
                    self.active.interview.export()
                    self._sync_store(self.active.interview)
                    self.active.update(phase='stopped')
