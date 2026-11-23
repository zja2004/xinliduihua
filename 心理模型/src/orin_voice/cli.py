import argparse
import json
import platform
import time
from pathlib import Path

from .prompts import DEMO_NOTICE, FALLBACK
from .settings import ROOT, load_json


def doctor():
    assets = load_json("configs/assets.json")
    report = {"architecture": platform.machine(), "jetson": Path("/etc/nv_tegra_release").exists(),
              "root": str(ROOT), "assets": {name: {"downloaded": (ROOT / asset["file"]).is_file(),
              "partial_bytes": (ROOT / asset["file"]).with_suffix(Path(asset["file"]).suffix + ".part").stat().st_size
              if (ROOT / asset["file"]).with_suffix(Path(asset["file"]).suffix + ".part").exists() else 0}
              for name, asset in assets.items()}}
    from .server import binary_path
    try:
        report["llama_server"] = str(binary_path())
    except (OSError, RuntimeError) as error:
        report["llama_server"] = str(error)
    try:
        from .audio import sound_device
        report["audio_devices"] = str(sound_device().query_devices())
    except (ImportError, OSError, RuntimeError) as error:
        report["audio_devices"] = str(error)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def voice_smoke(output):
    from .audio import SpeechRecognizer, SpeechSynthesizer, save_wav
    output = Path(output)
    phrase = "最近考试让我有些紧张，晚上睡得不太好。"
    tts = SpeechSynthesizer()
    started = time.perf_counter()
    samples, sample_rate = tts.synthesize(phrase)
    synthesis_s = time.perf_counter() - started
    save_wav(output, samples, sample_rate)
    asr = SpeechRecognizer()
    started = time.perf_counter()
    recognized = asr.transcribe(samples, sample_rate)
    record = {"input": phrase, "recognized": recognized, "tts_s": synthesis_s,
              "asr_s": time.perf_counter() - started, "audio_duration_s": len(samples) / sample_rate,
              "sample_rate": sample_rate, "speaker_id": 3, "speaker_name": "zf_001",
              "scope": "synthetic TTS-to-ASR round trip, not microphone/campus/clinical validation"}
    output.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=2))


def ask(arguments):
    from .llm import LocalChat, boundary_flags
    from .server import LocalServer
    tts = None
    if arguments.wav:
        from .audio import SpeechSynthesizer, save_wav
        tts = SpeechSynthesizer()
    with LocalServer(arguments.profile, port=arguments.port, gpu_layers=arguments.gpu_layers, context=arguments.ctx_size) as server:
        result = LocalChat(server.address).complete(arguments.text, max_tokens=arguments.max_tokens)
    if tts:
        spoken = FALLBACK if boundary_flags(result["reply"]) else result["reply"]
        samples, sample_rate = tts.synthesize(spoken)
        save_wav(arguments.wav, samples, sample_rate)
        result["spoken"] = spoken
    print(json.dumps(result, ensure_ascii=False, indent=2))


def chat(arguments):
    from .llm import LocalChat, boundary_flags
    from .server import LocalServer
    print(DEMO_NOTICE)
    print('正在加载模型；/exit 退出，/clear 清除本轮历史。对话不写入文件。', flush=True)
    history = []
    with LocalServer(arguments.profile, port=arguments.port, gpu_layers=arguments.gpu_layers,
                     context=arguments.ctx_size) as server:
        client = LocalChat(server.address)
        while True:
            try:
                text = input('你：').strip()
            except EOFError:
                break
            if text == '/exit':
                break
            if text == '/clear':
                history.clear()
                print('历史已清除。')
                continue
            if not text:
                continue
            result = client.complete(text, history=history, max_tokens=arguments.max_tokens)
            reply = FALLBACK if boundary_flags(result['reply']) else result['reply']
            print(f'助手：{reply}')
            print(f"[首字 {result['first_visible_token_s']:.2f}s / 总耗时 {result['completion_s']:.2f}s]")
            if result['finish_reason'] == 'length':
                print('[回复达到输出上限，可用 --max-tokens 增大预算]')
            history.extend([{'role': 'user', 'content': text}, {'role': 'assistant', 'content': reply}])
            history = history[-8:]


def talk(arguments):
    from .audio import SpeechRecognizer, SpeechSynthesizer, play_audio, record_push_to_talk, sound_device
    from .llm import LocalChat, boundary_flags
    from .server import LocalServer
    sound_device()
    print(DEMO_NOTICE)
    print("按键式原型，尚不支持自动发言结束检测和语音打断。Ctrl+C 退出。")
    recognizer, synthesizer = SpeechRecognizer(), SpeechSynthesizer()
    history = []
    with LocalServer(arguments.profile, port=arguments.port, gpu_layers=arguments.gpu_layers, context=arguments.ctx_size) as server:
        chat = LocalChat(server.address)
        while True:
            samples, sample_rate = record_push_to_talk()
            text = recognizer.transcribe(samples, sample_rate)
            if not text:
                print("没有识别清楚，请重新录音。")
                continue
            print(f"识别：{text}")
            if input("Enter 确认；输入其他内容取消本轮：").strip():
                continue
            result = chat.complete(text, history=history, max_tokens=arguments.max_tokens)
            spoken = FALLBACK if boundary_flags(result["reply"]) else result["reply"]
            print(f"回答：{spoken}")
            audio, audio_rate = synthesizer.synthesize(spoken)
            play_audio(audio, audio_rate)
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": spoken}])
            history = history[-8:]


def call(arguments):
    if arguments.questions and not arguments.duplex:
        raise ValueError('访谈请使用 bash scripts/interview.sh')
    if arguments.duplex:
        from .duplex import run
        return run(arguments)
    from .audio import SpeechRecognizer, SpeechSynthesizer, play_audio, record_utterance, sound_device
    from .llm import LocalChat, boundary_flags
    from .server import LocalServer
    sd = sound_device()
    sd.query_devices(arguments.input_device, kind='input')
    sd.query_devices(kind='output')
    print(DEMO_NOTICE)
    print('正在加载语音与对话模型……', flush=True)
    recognizer, synthesizer = SpeechRecognizer(), SpeechSynthesizer()
    history = []
    with LocalServer(arguments.profile, port=arguments.port, gpu_layers=arguments.gpu_layers,
                     context=arguments.ctx_size) as server:
        client = LocalChat(server.address)
        print('通话已启动。自动收音、停顿后发送；Ctrl+C 挂断。', flush=True)
        while True:
            print('正在听……', flush=True)
            samples, rate = record_utterance(silence_ms=arguments.silence_ms,
                                            threshold=arguments.speech_threshold,
                                            device=arguments.input_device)
            text = recognizer.transcribe(samples, rate)
            if not text:
                print('未识别出文字，继续听。', flush=True)
                continue
            print(f'你：{text}', flush=True)
            print('正在思考……', flush=True)
            result = client.complete(text, history=history, max_tokens=arguments.max_tokens)
            reply = FALLBACK if boundary_flags(result['reply']) else result['reply']
            print(f'助手：{reply}', flush=True)
            audio, audio_rate = synthesizer.synthesize(reply)
            play_audio(audio, audio_rate)
            # Let speaker reverberation decay before opening the microphone again.
            time.sleep(0.25)
            history.extend([{'role': 'user', 'content': text}, {'role': 'assistant', 'content': reply}])
            history = history[-8:]


def main():
    parser = argparse.ArgumentParser(description=DEMO_NOTICE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    download = commands.add_parser("download")
    download.add_argument("names", nargs="*", default=["asr", "tts", "baseline", "experimental"])
    smoke = commands.add_parser("voice-smoke")
    smoke.add_argument("--output", default=str(ROOT / "reports/voice-smoke.wav"))
    transcribe = commands.add_parser("transcribe")
    transcribe.add_argument("wav")
    speak = commands.add_parser("speak")
    speak.add_argument("text")
    speak.add_argument("--output", required=True)
    profiles = list(load_json('configs/profiles.json'))
    for name in ["serve", "ask", "talk", "compare", "chat", "call"]:
        command = commands.add_parser(name)
        command.add_argument("--port", type=int, default=18080)
        command.add_argument("--gpu-layers", type=int, default=99)
        command.add_argument("--max-tokens", type=int, default=160 if name == 'call' else (512 if name == 'chat' else 192))
        command.add_argument("--ctx-size", type=int, default=2048 if name in {'chat', 'call'} else 4096)
        if name == "compare":
            command.add_argument("--profiles", nargs="+", choices=profiles, default=["baseline", "experimental"])
            command.add_argument("--limit", type=int, default=8)
            command.add_argument("--tts", action="store_true")
            command.add_argument("--save-audio", action="store_true")
        else:
            command.add_argument("--profile", choices=profiles, default='distill9b' if name == 'chat' else 'baseline')
        if name == 'call':
            command.add_argument('--questions', help='21 题访谈 JSON；须通过 scripts/interview.sh 启动')
            command.add_argument('--output', default=str(ROOT / 'sessions'))
            command.add_argument('--duplex', action='store_true', help='持续监听并支持插话；使用 scripts/duplex.sh 配置回声消除')
            command.add_argument('--silence-ms', type=int, default=1000)
            command.add_argument('--speech-threshold', type=float, default=0.015)
            command.add_argument('--input-device', type=int)
        if name == "ask":
            command.add_argument("text")
            command.add_argument("--wav")
    arguments = parser.parse_args()
    try:
        if arguments.command == "doctor":
            doctor()
        elif arguments.command == "download":
            from .assets import download_asset
            for name in arguments.names:
                download_asset(name)
        elif arguments.command == "voice-smoke":
            voice_smoke(arguments.output)
        elif arguments.command == "transcribe":
            from .audio import SpeechRecognizer
            print(SpeechRecognizer().transcribe_file(arguments.wav))
        elif arguments.command == "speak":
            from .audio import SpeechSynthesizer, save_wav
            samples, sample_rate = SpeechSynthesizer().synthesize(arguments.text)
            save_wav(arguments.output, samples, sample_rate)
        elif arguments.command == "ask":
            ask(arguments)
        elif arguments.command == "talk":
            talk(arguments)
        elif arguments.command == 'chat':
            chat(arguments)
        elif arguments.command == 'call':
            call(arguments)
        elif arguments.command == "compare":
            from .evaluation import compare
            compare(arguments)
        elif arguments.command == "serve":
            from .server import LocalServer
            with LocalServer(arguments.profile, port=arguments.port, gpu_layers=arguments.gpu_layers, context=arguments.ctx_size) as server:
                print(f"Local only: {server.address}. Ctrl+C to stop.", flush=True)
                server.process.wait()
    except KeyboardInterrupt:
        print("\nStopped; microphone and model server closed.")
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
