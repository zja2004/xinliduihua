from datetime import datetime
import json
import platform
from pathlib import Path
import time

from .audio import SpeechSynthesizer, save_wav
from .llm import LocalChat, boundary_flags
from .prompts import FALLBACK
from .server import LocalServer
from .settings import ROOT, load_json, profile


def compare(arguments):
    cases = load_json("configs/eval_cases.json")[:arguments.limit]
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S%z")
    directory = ROOT / "reports" / f"comparison-{stamp}"
    directory.mkdir(parents=True, exist_ok=False)
    directory.chmod(0o700)
    tts = SpeechSynthesizer() if arguments.tts else None
    if tts:
        tts.synthesize("你好，这是语音测试。")
    report = {"created_at": datetime.now().astimezone().isoformat(),
              "host_architecture": platform.machine(),
              "jetson": Path("/etc/nv_tegra_release").exists(),
              "source": "synthetic cases only; no real students",
              "gpu_layers_requested": arguments.gpu_layers,
              "context": arguments.ctx_size, "max_tokens": arguments.max_tokens,
              "audio_ready_definition": "wall time from LLM request start to first sentence PCM ready; excludes ASR, playback and model loading",
              "human_review_status": "required; regex flags are not a safety verdict", "results": []}
    try:
        for selected in arguments.profiles:
            print(f"Loading {selected}; only one LLM is resident", flush=True)
            with LocalServer(selected, port=arguments.port, gpu_layers=arguments.gpu_layers, context=arguments.ctx_size) as server:
                chat = LocalChat(server.address)
                chat.complete("你好，请用一句中文打招呼。", max_tokens=48)
                for case in cases:
                    started = time.perf_counter()
                    audio_metrics = {}

                    def synthesize_first(sentence):
                        if not tts:
                            return
                        spoken = FALLBACK if boundary_flags(sentence) else sentence
                        synthesis_started = time.perf_counter()
                        samples, sample_rate = tts.synthesize(spoken)
                        audio_metrics.update(first_sentence_audio_ready_s=time.perf_counter() - started,
                                             first_sentence_tts_s=time.perf_counter() - synthesis_started,
                                             first_sentence_audio_duration_s=len(samples) / sample_rate,
                                             first_sentence_spoken=spoken)
                        if arguments.save_audio:
                            save_wav(directory / f"{selected}-{case['id']}.wav", samples, sample_rate)

                    result = chat.complete(case["text"], max_tokens=arguments.max_tokens,
                                           on_first_sentence=synthesize_first if tts else None)
                    row = {"profile": selected, "label": profile(selected)["label"], **case, **result,
                           **audio_metrics, "human_review": {criterion: None for criterion in case["human_review"]}}
                    report["results"].append(row)
                    write_report(directory, report)
                    print(f"{selected}/{case['id']}: first sentence {result['first_sentence_s']:.2f}s; human review pending", flush=True)
    except BaseException as error:
        report["incomplete"] = True
        report["error"] = str(error)
        write_report(directory, report)
        raise
    write_report(directory, report)
    print(f"Report: {directory}")


def write_report(directory, report):
    output = directory / "results.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    output.chmod(0o600)
    lines = ["# 本地模型对比记录", "", f"时间：{report['created_at']}",
             f"设备架构：{report['host_architecture']}；Jetson：{report['jetson']}",
             "", "仅使用虚构案例。尚未完成专家中文质量/心理安全评审；规则未命中不代表安全。",
             "音频就绪时间不包含麦克风、ASR、扬声器或加载时间，不是学生说完到听到回复的完整延迟。",
             "", "| 模型 | 案例 | 首句文本(s) | 首句PCM就绪(s) | 结束原因 | 规则提示 |",
             "| --- | --- | ---: | ---: | --- | --- |"]
    for row in report["results"]:
        audio_ready = f"{row['first_sentence_audio_ready_s']:.3f}" if "first_sentence_audio_ready_s" in row else "未测"
        lines.append(f"| {row['profile']} | {row['id']} | {row['first_sentence_s']:.3f} | {audio_ready} | {row['finish_reason']} | {', '.join(row['rule_flags']) or '未命中，不等于安全'} |")
    for row in report["results"]:
        lines.extend(["", f"## {row['profile']} / {row['id']}", "", f"输入：{row['text']}",
                      "", f"回答：{row['reply']}", "", "人工检查：" + "；".join(row["human_review"])])
    if report.get("incomplete"):
        lines.extend(["", "## 尚未完成", report["error"]])
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
