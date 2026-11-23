import math
import re
import hashlib
import uuid
import ctypes
import os
from pathlib import Path
import platform

import numpy as np
import sherpa_onnx
import soundfile as sf
from scipy.signal import resample_poly

from .settings import ROOT


def asset_directory(kind):
    candidates = list((ROOT / "models" / kind).glob("*/model.int8.onnx"))
    if len(candidates) != 1:
        raise FileNotFoundError(f"Download the {kind} asset first: python -m orin_voice.assets {kind}")
    return candidates[0].parent


class SpeechRecognizer:
    def __init__(self, threads=2):
        directory = asset_directory("asr")
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(
            tokens=str(directory / "tokens.txt"), model=str(directory / "model.int8.onnx"),
            num_threads=threads, sample_rate=16000, provider="cpu",
        )

    def transcribe(self, samples, sample_rate):
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim == 2:
            samples = samples.mean(axis=1)
        if sample_rate != 16000:
            divisor = math.gcd(sample_rate, 16000)
            samples = resample_poly(samples, 16000 // divisor, sample_rate // divisor).astype(np.float32)
        stream = self.recognizer.create_stream()
        for offset in range(0, len(samples), 3200):
            stream.accept_waveform(16000, samples[offset:offset + 3200])
            while self.recognizer.is_ready(stream):
                self.recognizer.decode_stream(stream)
        stream.accept_waveform(16000, np.zeros(8000, dtype=np.float32))
        stream.input_finished()
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)
        return self.recognizer.get_result(stream).strip()

    def transcribe_file(self, filename):
        samples, sample_rate = sf.read(filename, dtype="float32")
        return self.transcribe(samples, sample_rate)


class SpeechSynthesizer:
    def __init__(self, speaker=3, threads=2, cache=False):
        directory = asset_directory("tts")
        kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
            model=str(directory / "model.int8.onnx"), voices=str(directory / "voices.bin"),
            tokens=str(directory / "tokens.txt"), data_dir=str(directory / "espeak-ng-data"),
            dict_dir=str(directory / "dict"),
            lexicon=",".join(str(directory / name) for name in ["lexicon-us-en.txt", "lexicon-zh.txt"]),
            lang="zh",
        )
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(kokoro=kokoro, num_threads=threads, provider="cpu"),
            rule_fsts=",".join(str(directory / name) for name in ["date-zh.fst", "number-zh.fst", "phone-zh.fst"]),
            max_num_sentences=1,
        )
        self.engine = sherpa_onnx.OfflineTts(config)
        if not 0 <= speaker < self.engine.num_speakers:
            raise ValueError(f"Speaker must be in [0, {self.engine.num_speakers - 1}]")
        self.speaker = speaker
        self.cache_directory = ROOT / '.runtime/interview-tts' if cache else None
        self.cache_identity = repr((speaker, threads, str(directory),
                                    (directory / 'model.int8.onnx').stat().st_mtime_ns,
                                    (directory / 'voices.bin').stat().st_mtime_ns))

    def synthesize(self, text):
        if not text.strip():
            raise ValueError("Cannot synthesize empty text")
        text = re.sub(r'(?i)\bDASS(?:[- ]?21)?\b', '抑郁焦虑压力自评', text)
        cached = None
        if self.cache_directory:
            key = hashlib.sha256((self.cache_identity + text).encode()).hexdigest()
            cached = self.cache_directory / (key + '.wav')
            if cached.is_file():
                samples, rate = sf.read(cached, dtype='float32')
                return samples, rate
        spoken_text = re.sub(r'(?<=[\u3400-\u9fff0-9])\s+(?=[\u3400-\u9fff0-9])', '', text)
        generated = self.engine.generate(spoken_text, sid=self.speaker, speed=1.0)
        if not len(generated.samples):
            raise RuntimeError("TTS produced no audio")
        samples = np.asarray(generated.samples, dtype=np.float32)
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            temporary = cached.with_suffix(f'.{uuid.uuid4().hex}.tmp.wav')
            save_wav(temporary, samples, generated.sample_rate)
            temporary.replace(cached)
        return samples, generated.sample_rate


def save_wav(path, samples, sample_rate):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, samples, sample_rate, subtype="PCM_16")
    path.chmod(0o600)


def sound_device():
    local_library = ROOT / f".runtime/portaudio/usr/lib/{platform.machine()}-linux-gnu/libportaudio.so.2"
    try:
        if local_library.exists():
            linker_name = local_library.with_name("libportaudio.so")
            if not linker_name.exists():
                linker_name.symlink_to(local_library.name)
            os.environ["LD_LIBRARY_PATH"] = str(local_library.parent) + ":" + os.environ.get("LD_LIBRARY_PATH", "")
            ctypes.CDLL(str(local_library), mode=ctypes.RTLD_GLOBAL)
        import sounddevice
    except (ImportError, OSError) as error:
        raise RuntimeError("Microphone/playback requires sounddevice and libportaudio2; WAV commands still work") from error
    return sounddevice


def record_push_to_talk(max_seconds=60):
    sounddevice = sound_device()
    device = sounddevice.query_devices(kind="input")
    sample_rate = int(device["default_samplerate"])
    chunks = []
    overflow = []
    maximum = sample_rate * max_seconds
    captured = 0

    def capture(samples, frames, timing, status):
        nonlocal captured
        if status:
            overflow.append(str(status))
        if captured < maximum:
            chunks.append(samples.copy())
            captured += frames

    input("按 Enter 开始录音（仅此时访问麦克风）；最长 60 秒。")
    with sounddevice.InputStream(samplerate=sample_rate, channels=1, dtype="float32", callback=capture):
        input("正在录音，再按 Enter 停止：")
    if overflow:
        raise RuntimeError("Audio capture error; please record again: " + "; ".join(overflow))
    if not chunks:
        raise RuntimeError("No audio captured")
    return np.concatenate(chunks, axis=0).reshape(-1)[:maximum], sample_rate


def play_audio(samples, sample_rate):
    sounddevice = sound_device()
    try:
        sounddevice.play(samples, sample_rate)
        sounddevice.wait()
    finally:
        sounddevice.stop()


def record_utterance(silence_ms=1000, threshold=0.015, device=None, max_seconds=45):
    """Capture until a speech-like energy burst is followed by silence.

    Input is closed before inference/playback to prevent speaker feedback.
    This energy detector is intended for quiet-room MVP use, not robust VAD.
    """
    from .endpoint import Endpoint
    if not 0 < threshold < 1:
        raise ValueError('Speech threshold must be between 0 and 1')
    sd = sound_device()
    info = sd.query_devices(device, kind='input')
    rate = int(info['default_samplerate'])
    block = round(rate * 0.030)
    endpoint = Endpoint(silence_ms=silence_ms, max_seconds=max_seconds)
    with sd.InputStream(device=device, samplerate=rate, channels=1,
                        dtype='float32', blocksize=block) as stream:
        while True:
            samples, overflow = stream.read(block)
            if overflow:
                raise RuntimeError('麦克风缓冲溢出，请检查设备或降低系统负载后重试')
            frame = samples[:, 0].copy()
            rms = float(np.sqrt(np.mean(frame * frame)))
            complete = endpoint.push(frame, rms >= threshold)
            if complete is not None:
                return np.concatenate(complete), rate
