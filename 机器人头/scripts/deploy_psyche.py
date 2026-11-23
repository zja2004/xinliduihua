#!/usr/bin/env python3
"""Resume Psyche-R1 download, convert, quantize and run an isolated text smoke check."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import struct
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
REVISION = '2c2dc49d0f5027931b152ab776a90c6bfcd0eecb'
SCOPE = '仅完成独立文本冒烟检查；不代表儿童适用性、心理安全、临床有效性或完整机器人验收。'


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_text(value):
    value = re.sub(r'https?://[^\s<>\"\']+', '[URL REDACTED]', str(value))
    return re.sub(r'hf_[A-Za-z0-9_-]+', '[TOKEN REDACTED]', value)


def load_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def gguf_header(path):
    with path.open('rb') as handle:
        header = handle.read(24)
    if len(header) != 24:
        raise ValueError(f'GGUF头不完整：{path.name}')
    magic, version, tensors, metadata = struct.unpack('<4sIQQ', header)
    if magic != b'GGUF' or version not in (2, 3) or tensors < 1:
        raise ValueError(f'GGUF魔数、版本或tensor数量无效：{path.name}')
    if path.stat().st_size <= 24 + tensors * 24:
        raise ValueError(f'GGUF文件明显不完整：{path.name}')
    return {'version': version, 'tensor_count': tensors, 'metadata_count': metadata}


class Interrupted(BaseException):
    def __init__(self, signum):
        self.signum = signum


def interrupt(signum, frame):
    raise Interrupted(signum)


def descendants(pid):
    parents = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            tail = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            parents[int(entry.name)] = int(tail[1])
        except (OSError, ValueError, IndexError):
            continue
    found = {pid}
    while True:
        more = {child for child, parent in parents.items() if parent in found}
        if more <= found:
            return found
        found.update(more)


def stop_child(process):
    """Give the downloader time to save aria2 resume state, then reap survivors."""
    if process is None:
        return
    owned = descendants(process.pid)
    def send(pid, sig):
        try:
            if pid != os.getpid() and os.getpgid(pid) == os.getpgrp():
                os.kill(pid, sig)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        send(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
    for pid in owned:
        send(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not any(Path(f'/proc/{pid}').exists() for pid in owned):
            break
        time.sleep(0.2)
    for pid in owned:
        send(pid, signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class Job:
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / 'job-status.json'
        previous = load_json(self.path)
        self.data = {'pid': os.getpid(), 'pgid': os.getpgrp(), 'child_pid': None,
                     'started_at': now(), 'updated_at': now(), 'state': 'running',
                     'phase': 'starting', 'source_revision': REVISION, 'scope': SCOPE,
                     'artifacts': previous.get('artifacts', {}), 'steps': [], 'smoke_attempts': []}
        self.child = None
        self.save()

    def save(self, **changes):
        self.data.update(changes, updated_at=now())
        temporary = self.path.with_suffix('.json.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        temporary.replace(self.path)

    def run(self, phase, command, env=None):
        log_path = self.directory / f'job-{os.getpid()}-{phase}.log'
        step = {'phase': phase, 'started_at': now(), 'log': str(log_path)}
        self.data['steps'].append(step)
        self.save(phase=phase, latest_output=None)
        print(f'[{now()}] {phase}', flush=True)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(fd, 'a', encoding='utf-8') as log:
                self.child = subprocess.Popen(command, cwd=ROOT, env=env,
                                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                              text=True, errors='replace', bufsize=1)
                step['pid'] = self.child.pid
                self.save(child_pid=self.child.pid)
                last_saved = 0.0
                for line in self.child.stdout:
                    cleaned = safe_text(line)
                    log.write(cleaned)
                    log.flush()
                    if time.monotonic() - last_saved >= 15:
                        self.save(latest_output=cleaned.strip()[:1500])
                        last_saved = time.monotonic()
                code = self.child.wait()
        except BaseException:
            stop_child(self.child)
            raise
        finally:
            if self.child and self.child.stdout:
                self.child.stdout.close()
        if code:
            stop_child(self.child)
        self.child = None
        step.update(returncode=code, finished_at=now())
        self.save(child_pid=None)
        return code

    def reusable(self, name, path, kind):
        record = self.data['artifacts'].get(name, {})
        try:
            return (record.get('producer_returncode') == 0 and record.get('source_revision') == REVISION
                    and record.get('path') == str(path) and record.get('kind') == kind
                    and record.get('bytes') == path.stat().st_size
                    and record.get('header') == gguf_header(path))
        except (OSError, ValueError):
            return False

    def artifact(self, name, path, kind):
        self.data['artifacts'][name] = {
            'path': str(path), 'kind': kind, 'bytes': path.stat().st_size,
            'header': gguf_header(path), 'producer_returncode': 0,
            'source_revision': REVISION, 'completed_at': now()}
        self.save()


def deploy(job, args):
    tools = ROOT / '.runtime/psyche-tools/bin/python'
    app = ROOT / '.venv/bin/python'
    source = ROOT / 'models/Psyche-R1/source'
    model_dir = source.parent
    for executable in (tools, app):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError(f'Python环境不可执行：{executable}')
    environment = dict(os.environ)
    environment.pop('HF_DEBUG', None)
    environment['ORIN_VOICE_HOME'] = str(ROOT)
    for attempt in range(1, 13):
        job.save(download_attempt=attempt)
        if job.run(f'download-{attempt:02d}',
                   [str(tools), str(ROOT / 'scripts/download_psyche_parallel.py')], environment) == 0:
            job.save(sources_verified=True)
            break
        if attempt == 12:
            raise RuntimeError('下载及源文件校验连续12次未完成；续传文件已保留。')
        job.save(phase='download_retry_wait', retry_after_seconds=30)
        time.sleep(30)

    model_dir.mkdir(parents=True, exist_ok=True)
    f16 = model_dir / 'Psyche-R1-F16.gguf'
    f16_temp = model_dir / 'Psyche-R1-F16.building.gguf'
    if not job.reusable('f16', f16, 'F16'):
        f16_temp.unlink(missing_ok=True)
        conversion_env = dict(environment, OMP_NUM_THREADS='2')
        command = [str(tools), str(ROOT / '.runtime/llama.cpp/convert_hf_to_gguf.py'), str(source),
                   '--outfile', str(f16_temp), '--outtype', 'f16', '--use-temp-file']
        if job.run('convert-f16', command, conversion_env):
            raise RuntimeError('F16转换失败，未发布临时GGUF；请查看阶段日志。')
        gguf_header(f16_temp)
        f16_temp.replace(f16)
        job.artifact('f16', f16, 'F16')
    else:
        job.save(phase='reuse_verified_f16')

    quantizer = Path((ROOT / '.runtime/llama-quantize.path').read_text().strip())
    if not quantizer.is_absolute():
        quantizer = ROOT / quantizer
    if not quantizer.is_file() or not os.access(quantizer, os.X_OK):
        raise RuntimeError('原生llama-quantize路径无效。')
    with quantizer.open('rb') as handle:
        elf = handle.read(20)
    expected_machine = {'aarch64': 183, 'x86_64': 62}.get(platform.machine())
    if len(elf) < 20 or elf[:4] != b'\x7fELF' or expected_machine is None:
        raise RuntimeError('无法确认量化器为本机原生ELF。')
    order = 'little' if elf[5] == 1 else 'big'
    if int.from_bytes(elf[18:20], order) != expected_machine:
        raise RuntimeError('量化器架构与本机不符。')
    q4 = model_dir / 'Psyche-R1-Q4_K_M.gguf'
    q4_temp = model_dir / 'Psyche-R1-Q4_K_M.building.gguf'
    if not job.reusable('q4', q4, 'Q4_K_M'):
        q4_temp.unlink(missing_ok=True)
        quant_env = dict(environment)
        quant_env['LD_LIBRARY_PATH'] = str(quantizer.parent) + ':' + environment.get('LD_LIBRARY_PATH', '')
        command = [str(quantizer), '--max-buffer-size', '512', str(f16), str(q4_temp), 'Q4_K_M', '2']
        if job.run('quantize-q4', command, quant_env):
            raise RuntimeError('Q4_K_M量化失败，未发布临时GGUF；请查看阶段日志。')
        gguf_header(q4_temp)
        q4_temp.replace(q4)
        job.artifact('q4', q4, 'Q4_K_M')
    else:
        job.save(phase='reuse_verified_q4')

    job.save(phase='hash_q4')
    digest = hashlib.sha256()
    with q4.open('rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(block)
    recorded_hash = job.data['artifacts']['q4'].get('sha256')
    if recorded_hash and recorded_hash != digest.hexdigest():
        job.data['artifacts']['q4']['producer_returncode'] = None
        job.save()
        raise RuntimeError('先前发布的Q4文件SHA256已改变，未进行推理。')
    job.data['artifacts']['q4']['sha256'] = digest.hexdigest()
    job.save()

    for budget in (1024, 2048):
        before = set(job.directory.glob('smoke-*.json'))
        command = [str(app), str(ROOT / 'scripts/smoke_psyche.py'), '--profile', 'psyche',
                   '--port', str(args.port), '--ctx-size', str(args.ctx_size),
                   '--gpu-layers', str(args.gpu_layers), '--max-tokens', str(budget),
                   '--output-dir', str(job.directory)]
        code = job.run(f'smoke-{budget}', command, environment)
        created = set(job.directory.glob('smoke-*.json')) - before
        latest = max(created, key=lambda path: path.stat().st_mtime) if created else None
        result = load_json(latest) if latest else {}
        passed = (code == 0 and result.get('status') == 'smoke_complete'
                  and len(result.get('results', [])) == 3
                  and result.get('all_cases_completed_without_truncation') is True)
        job.data['smoke_attempts'].append({
            'max_tokens': budget, 'returncode': code, 'report': str(latest) if latest else None,
            'passed': passed, 'visible_reply_count': result.get('visible_reply_count'),
            'truncated_case_ids': result.get('truncated_case_ids')})
        job.save()
        if passed:
            job.save(state='isolated_text_smoke_passed', phase='complete', finished_at=now())
            return 0
    raise RuntimeError('1024和2048输出预算的独立文本冒烟检查均未通过；未切换默认模型。')


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=SCOPE)
    parser.add_argument('--port', type=int, default=18082)
    parser.add_argument('--ctx-size', type=int, default=4096)
    parser.add_argument('--gpu-layers', type=int, default=99)
    args = parser.parse_args()
    directory = ROOT / '.runtime/psyche-deploy'
    directory.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(directory / 'job.lock', os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('已有Psyche部署作业持有job.lock；未启动重复作业。', flush=True)
            return 2
        job = Job(directory)
        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGTERM, interrupt)
        try:
            return deploy(job, args)
        except Interrupted as error:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            stop_child(job.child)
            job.save(state='interrupted', interrupted_phase=job.data['phase'], child_pid=None,
                     signal=error.signum, finished_at=now())
            return 128 + error.signum
        except Exception as error:
            stop_child(job.child)
            job.save(state='failed', failed_phase=job.data['phase'], child_pid=None,
                     error=safe_text(f'{type(error).__name__}: {error}'), finished_at=now())
            print(job.data['error'], flush=True)
            return 1
        finally:
            if job.child:
                stop_child(job.child)


if __name__ == '__main__':
    raise SystemExit(main())
