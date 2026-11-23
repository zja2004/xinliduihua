"""Continuous capture with bounded buffering and interruption of stale replies."""
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import re
from contextlib import nullcontext


def cancellable(executor, cancel, function, *args, **kwargs):
    def invoke():
        if not cancel.is_set():
            return function(*args, **kwargs)
    future = executor.submit(invoke)
    while not cancel.is_set():
        try:
            return future.result(timeout=.03)
        except TimeoutError:
            pass
    future.cancel()
    return None

from .endpoint import Endpoint, AdaptiveEnergy


class Turns:
    def __init__(self, preserve=False):
        self.preserve = preserve
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.pending = queue.Queue(maxsize=8 if preserve else 2)

    def interrupt(self):
        with self.lock:
            self.cancel.set()
            self.cancel = threading.Event()

    def submit(self, samples, rate):
        with self.lock:
            item = (samples, rate, self.cancel)
            if self.pending.full():
                if self.preserve:
                    raise RuntimeError('回答处理积压，停止访谈以避免静默丢失录音。已有回答已保存。')
                self.pending.get_nowait()
            self.pending.put_nowait(item)


class IdlePrompt:
    """Bounded re-prompts after silence; never mark a question answered."""
    def __init__(self, interval=20, maximum=2):
        self.interval, self.maximum = interval, maximum
        self.last, self.count = 0, 0

    def reset(self, now):
        self.last, self.count = now, 0

    def due(self, now, quiet):
        if quiet and self.count < self.maximum and now - self.last >= self.interval:
            self.count += 1
            self.last = now
            return True
        return False


class VoiceStopped(Exception):
    pass


def run(args, control=None):
    import os
    import numpy as np
    from .audio import SpeechRecognizer, SpeechSynthesizer, sound_device
    from .llm import LocalChat, boundary_flags
    from .prompts import FALLBACK
    from .server import LocalServer

    source_name, sink_name = os.environ.get('PULSE_SOURCE', ''), os.environ.get('PULSE_SINK', '')
    if not (re.fullmatch(r'orin_aec_source(?:_[0-9]+_[0-9a-f]{8})?', source_name) and
            sink_name == source_name.replace('orin_aec_source', 'orin_aec_sink', 1)):
        raise RuntimeError('扬声器全双工需要回声消除，请使用 bash scripts/duplex.sh')
    if not 0 < args.speech_threshold < 1:
        raise ValueError('Speech threshold must be between 0 and 1')
    sd = sound_device()
    # Explicitly select PulseAudio so ALSA hardware defaults cannot bypass AEC.
    devices = sd.query_devices()
    inputs = [i for i, d in enumerate(devices) if d['name'] == 'pulse' and d['max_input_channels']]
    outputs = [i for i, d in enumerate(devices) if d['name'] == 'pulse' and d['max_output_channels']]
    if not inputs or not outputs:
        raise RuntimeError('PortAudio 没有 pulse 设备，不能保证回声消除路由')
    if args.input_device is not None:
        raise ValueError('全双工请通过 ORIN_MIC_SOURCE 选择物理麦克风，避免绕过回声消除')
    rate = int(devices[inputs[0]]['default_samplerate'])
    block = round(rate * .030)
    def new_endpoint():
        return Endpoint(silence_ms=args.silence_ms,
                        min_speech_ms=120 if getattr(args, 'questions', None) else 240,
                        onset_tolerance_ms=60 if getattr(args, 'questions', None) else 0)

    energy = AdaptiveEnergy(args.speech_threshold)
    telemetry = dict(input_rms=0., peak_rms=0., detected_utterances=0, speech_threshold=energy.threshold, speech_detected=False,
                     capture_frames=0, last_capture_at=0., started_at=0.)
    meter_updated = 0.
    endpoint = new_endpoint()
    turns = Turns(preserve=bool(getattr(args, 'questions', None)))
    if control:
        control.interrupt = turns.interrupt
    errors = queue.Queue(maxsize=1)

    def capture(samples, frames, timing, status):
        nonlocal endpoint
        try:
            if status:
                raise RuntimeError(f'音频采集异常：{status}')
            frame = samples[:, 0].copy()
            was_started = endpoint.started
            rms = float(np.sqrt(np.mean(frame * frame)))
            voiced = energy.voiced(rms, active=endpoint.started)
            complete = endpoint.push(frame, voiced)
            telemetry.update(input_rms=rms, peak_rms=max(rms, telemetry['peak_rms'] * .93), speech_threshold=energy.threshold,
                             speech_detected=endpoint.started,
                             capture_frames=telemetry['capture_frames'] + frames,
                             last_capture_at=time.monotonic())
            if endpoint.started and not was_started:
                turns.interrupt()
            if complete is not None:
                telemetry['detected_utterances'] += 1
                turns.submit(np.concatenate(complete), rate)
                endpoint = new_endpoint()
        except Exception as error:
            if errors.empty():
                errors.put_nowait(error)

    def check_capture():
        nonlocal meter_updated
        now = time.monotonic()
        if telemetry['started_at'] and now - (telemetry['last_capture_at'] or telemetry['started_at']) > 3:
            raise RuntimeError('麦克风已超过三秒没有传入数据，请检查耳机连接和系统输入设备。')
        if control and now - meter_updated >= .25:
            control.set_audio({key: value for key, value in telemetry.items() if key not in {'last_capture_at', 'started_at'}})
            meter_updated = now
        if control and control.stop.is_set():
            raise VoiceStopped()
        if not errors.empty():
            raise errors.get_nowait()

    print('正在加载语音识别与语音合成模型……', flush=True)
    recognizer, synthesizer = SpeechRecognizer(), SpeechSynthesizer(cache=bool(getattr(args, 'questions', None)))
    history = []
    interview = None
    if getattr(args, 'questions', None):
        from .interview import Interview
        interview = control.interview if control else Interview(args.questions, args.output)
        print(f'访谈保存目录：{interview.directory}', flush=True)
        if interview.data['example_questions']:
            print('注意：正在使用示例题单，非正式量表。', flush=True)
    inference = ThreadPoolExecutor(max_workers=1, thread_name_prefix='inference')
    synthesis = ThreadPoolExecutor(max_workers=1, thread_name_prefix='synthesis')
    idle = IdlePrompt()

    def speak(reply, cancel):
        if control:
            control.update(phase='speaking', role='assistant', text=reply)
        # Sentence-sized synthesis bounds obsolete work and time to first sound.
        parts = (interview.speech_parts(reply) if interview else
                 re.findall(r'[^。！？!?\n]+[。！？!?]?|[。！？!?]', reply))
        for sentence in parts:
            if cancel.is_set():
                return
            result = cancellable(synthesis, cancel, synthesizer.synthesize, interview.synthesis_text(sentence) if interview else sentence)
            if result is None or cancel.is_set():
                return
            audio, audio_rate = result
            with sd.OutputStream(device=outputs[0], samplerate=audio_rate,
                                 channels=1, dtype='float32', latency='low') as output:
                chunk = max(1, round(audio_rate * .02))
                for offset in range(0, len(audio), chunk):
                    check_capture()
                    if cancel.is_set():
                        output.abort()
                        return
                    output.write(audio[offset:offset + chunk].reshape(-1, 1))
    server_context = (nullcontext(None) if interview and interview.is_dass else
                      LocalServer(args.profile, port=args.port, gpu_layers=args.gpu_layers,
                                  context=args.ctx_size))
    try:
        with server_context as server:
            client = LocalChat(server.address) if server else None
            with sd.InputStream(device=inputs[0], samplerate=rate, channels=1,
                                dtype='float32', blocksize=block, callback=capture):
                telemetry['started_at'] = time.monotonic()
                print('全双工已启动：持续收音，讲话可打断回复；Ctrl+C 挂断。', flush=True)
                if interview:
                    print(f'助手：{interview.introduction}', flush=True)
                    speak(interview.introduction, turns.cancel)
                    idle.reset(time.monotonic())
                    if control:
                        control.update(phase='listening')
                while True:
                    check_capture()
                    command = None
                    if control:
                        try:
                            command = control.commands.get_nowait()
                        except queue.Empty:
                            pass
                    if command is not None:
                        text, expected = command
                        current_id = interview.current['id'] if interview.current else None
                        if current_id != expected:
                            control.update(role='system', text='题目已变化，上一操作未执行，请重新选择。')
                            continue
                        samples, sample_rate, cancel = None, None, turns.cancel
                    else:
                        try:
                            samples, sample_rate, cancel = turns.pending.get(timeout=.1)
                        except queue.Empty:
                            if interview and interview.current and idle.due(
                                    time.monotonic(), quiet=not endpoint.started and endpoint.onset_ms == 0):
                                reminder = '我在这里，不着急，我们还是当前这道题。' + interview.prompt
                                print(f'助手：{reminder}', flush=True)
                                speak(reminder, turns.cancel)
                                idle.last = time.monotonic()
                                if control:
                                    control.update(phase='listening')
                            continue
                        if cancel.is_set() and not interview:
                            continue
                        if control:
                            control.update(phase='transcribing')
                        asr_started = time.monotonic()
                        text = recognizer.transcribe(samples, sample_rate)
                        print(f'[语音识别 {time.monotonic() - asr_started:.2f}秒]', flush=True)
                        if not text:
                            if interview:
                                interview.record_unrecognized(samples, sample_rate)
                                print('听到声音，但没有识别出文字；录音已保留。', flush=True)
                                speak('没听清，请再说一次。', turns.cancel)
                            if control:
                                control.update(phase='listening')
                            idle.reset(time.monotonic())
                            continue
                        if cancel.is_set() and not interview:
                            if control:
                                control.update(phase='listening')
                            continue
                    print(f'你：{text}', flush=True)
                    if control:
                        control.update(role='user', text=text)
                    if interview:
                        # Once transcribed, preserve an answer even if more speech arrives.
                        reply = interview.accept(text, samples, sample_rate)
                        print(f"[已保存 {interview.data['completed_count']}/21 题；当前 {interview.current['id'] if interview.current else '完成'}]", flush=True)
                        if interview.data['status'] == 'completed':
                            interview.finish_summary(interview.basic_summary())
                            if interview.is_dass:
                                reply += interview.basic_summary()
                        if interview.data['status'] == 'completed' and not interview.is_dass:
                            result = cancellable(inference, cancel, client.complete,
                                                 interview.summary_prompt(), max_tokens=320, cancel=cancel)
                            if result and not cancel.is_set():
                                summary = FALLBACK if boundary_flags(result['reply']) else result['reply']
                                interview.finish_summary(summary)
                                reply += summary
                        print(f'助手：{reply}', flush=True)
                        speak(reply, cancel)
                        idle.reset(time.monotonic())
                        if control:
                            control.update(phase='listening' if interview.data['status'] == 'in_progress' else interview.data['status'])
                        if interview.data['status'] in {'stopped', 'completed'}:
                            return
                        continue
                    first_spoken = []
                    def first_sentence_ready(sentence, cancel=cancel, first_spoken=first_spoken):
                        if not cancel.is_set() and not boundary_flags(sentence):
                            first_spoken.append(sentence)
                            speak(sentence, cancel)
                    result = cancellable(inference, cancel, client.complete, text,
                                         history=list(history), max_tokens=args.max_tokens, cancel=cancel,
                                         on_first_sentence=first_sentence_ready)
                    if result is None or cancel.is_set():
                        continue
                    reply = FALLBACK if boundary_flags(result['reply']) else result['reply']
                    print(f'助手：{reply}', flush=True)
                    remainder = reply
                    if first_spoken and reply.startswith(first_spoken[0]):
                        remainder = reply[len(first_spoken[0]):]
                    if remainder.strip():
                        speak(remainder, cancel)
                    history.append({'role': 'user', 'content': text})
                    history.append({'role': 'assistant', 'content':
                                    reply if not cancel.is_set() else '[回复被用户打断，未完整播放]'})
                    history = history[-8:]
    except VoiceStopped:
        if interview and interview.data['status'] == 'in_progress':
            interview.accept('结束访谈')
        if control:
            control.update(phase='stopped')
    finally:
        turns.cancel.set()
        inference.shutdown(wait=False, cancel_futures=True)
        synthesis.shutdown(wait=False, cancel_futures=True)
