#!/usr/bin/env python3
"""Load one local model once and record three synthetic Chinese text checks."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / 'src'))

CASES = (
    ('peer_conflict', '今天课间和同学闹了点小矛盾，我有些难过。'),
    ('reading_worry', '明天要在全班面前朗读，我担心自己会读错。'),
    ('decline_answer', '这个问题我不想回答，可以跳过吗？'),
)
SCOPE = ('仅使用三个虚构中文场景，检查本地文本推理是否完成；'
         '不是儿童适用性、临床有效性、心理安全或真实录音录像验收。'
         '首句延迟不含ASR、TTS、播放或模型加载。')


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def proc_kib(path):
    try:
        return {key: int(value.split()[0]) for line in path.read_text().splitlines()
                if ':' in line for key, value in [line.split(':', 1)]
                if value.strip().endswith('kB')}
    except (OSError, ValueError):
        return {}


class MemorySampler:
    """Sample Linux-visible memory during loading and all three requests."""
    def __init__(self, server):
        self.server = server
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.data = {'sample_interval_s': 0.25, 'samples': 0,
                     'server_rss_peak_kib': None, 'server_vmhwm_peak_kib': None,
                     'system_mem_available_start_kib': None,
                     'system_mem_available_min_kib': None,
                     'scope': '进程RSS/HWM及整机MemAvailable采样；不等于完整GPU峰值内存，'
                              '整机变化也可能来自其他进程。'}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def sample(self):
        system = proc_kib(Path('/proc/meminfo'))
        process = self.server.process
        usage = proc_kib(Path(f'/proc/{process.pid}/status')) if process else {}
        with self.lock:
            self.data['samples'] += 1
            available = system.get('MemAvailable')
            if available is not None:
                if self.data['system_mem_available_start_kib'] is None:
                    self.data['system_mem_available_start_kib'] = available
                previous = self.data['system_mem_available_min_kib']
                self.data['system_mem_available_min_kib'] = min(previous, available) if previous is not None else available
            for source, target in [('VmRSS', 'server_rss_peak_kib'), ('VmHWM', 'server_vmhwm_peak_kib')]:
                value = usage.get(source)
                if value is not None:
                    self.data[target] = max(self.data[target] or 0, value)

    def _run(self):
        while not self.stop_event.wait(self.data['sample_interval_s']):
            self.sample()

    def start(self):
        self.sample()
        self.thread.start()

    def finish(self):
        self.stop_event.set()
        self.thread.join(timeout=2)

    def snapshot(self):
        with self.lock:
            return dict(self.data)


def write_report(path, report):
    temporary = path.with_suffix('.json.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=SCOPE)
    parser.add_argument('--profile', default='psyche', help='configs/profiles.json中的独立模型配置')
    parser.add_argument('--port', type=int, default=18082, help='仅绑定127.0.0.1，默认18082')
    parser.add_argument('--ctx-size', type=int, default=4096)
    parser.add_argument('--gpu-layers', type=int, default=99)
    parser.add_argument('--max-tokens', type=int, default=1024,
                        help='每例输出预算，包含可能产生的思考token，默认1024')
    parser.add_argument('--output-dir', type=Path, default=PROJECT_ROOT / '.runtime/psyche-deploy')
    args = parser.parse_args()
    if args.max_tokens < 1:
        parser.error('--max-tokens必须大于0')

    from orin_voice.llm import LocalChat
    from orin_voice.robot_dialogue import CHAT_SYSTEM
    from orin_voice.server import LocalServer
    from orin_voice.settings import ROOT, profile

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = args.output_dir / f'smoke-{stamp}-{secrets.token_hex(3)}.json'
    report = {'started_at': utc_now(), 'status': 'starting', 'scope': SCOPE,
              'profile': args.profile, 'port': args.port, 'context': args.ctx_size,
              'gpu_layers_requested': args.gpu_layers, 'max_tokens_per_case': args.max_tokens,
              'system_prompt': CHAT_SYSTEM, 'human_review_status': 'pending', 'results': []}
    write_report(output, report)
    sampler = None
    exit_code = 1
    try:
        selected = profile(args.profile)
        report.update(label=selected['label'], model_file=str(ROOT / selected['model']))
        server = LocalServer(args.profile, port=args.port, gpu_layers=args.gpu_layers,
                             context=args.ctx_size)
        sampler = MemorySampler(server)
        sampler.start()
        loading_started = time.perf_counter()
        print(f'加载模型：{args.profile}；只加载一次。', flush=True)
        with server:
            report.update(load_seconds=round(time.perf_counter() - loading_started, 4),
                          server_pid=server.process.pid, status='running')
            client = LocalChat(server.address)
            for case_id, text in CASES:
                row = {'id': case_id, 'text': text}
                try:
                    result = client.complete(text, history=[], max_tokens=args.max_tokens,
                                             system_prompt=CHAT_SYSTEM)
                    row.update(result, visible_reply=bool(result['reply'].strip()),
                               truncated=result['finish_reason'] == 'length')
                    print(f"{case_id}：首句{result['first_sentence_s']:.3f}s；"
                          f"结束原因={result['finish_reason']}；回答={result['reply']}", flush=True)
                except (OSError, RuntimeError, ValueError) as error:
                    row.update(visible_reply=False, truncated=None, error=str(error))
                    print(f'{case_id}：失败，{error}', flush=True)
                report['results'].append(row)
                sampler.sample()
                report['memory'] = sampler.snapshot()
                write_report(output, report)
            sampler.sample()
        report['status'] = 'smoke_complete'
        report['visible_reply_count'] = sum(row['visible_reply'] for row in report['results'])
        report['truncated_case_ids'] = [row['id'] for row in report['results'] if row['truncated']]
        report['all_cases_completed_without_truncation'] = all(
            row['visible_reply'] and row['truncated'] is False for row in report['results'])
        exit_code = 0 if report['all_cases_completed_without_truncation'] else 1
    except KeyboardInterrupt:
        report.update(status='interrupted', error='用户中断')
        exit_code = 130
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        report.update(status='failed', error=str(error))
        print(f'冒烟检查失败：{error}', file=sys.stderr)
    finally:
        if sampler:
            sampler.finish()
            report['memory'] = sampler.snapshot()
        report['finished_at'] = utc_now()
        write_report(output, report)
    print(f'记录：{output}\n{SCOPE}', flush=True)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
