#!/usr/bin/env python3
"""Resume the pinned Psyche-R1 source using authenticated mirror metadata and aria2."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
os.environ['HF_HUB_DISABLE_XET'] = '1'
os.environ['HF_HUB_DOWNLOAD_TIMEOUT'] = '120'

import requests
from huggingface_hub import get_token, hf_hub_download

ACTIVE_TRANSFER = None


def stop_transfer(signum, frame):
    if ACTIVE_TRANSFER is not None and ACTIVE_TRANSFER.poll() is None:
        ACTIVE_TRANSFER.send_signal(signal.SIGINT)
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, stop_transfer)
signal.signal(signal.SIGINT, stop_transfer)


def main():
    global ACTIVE_TRANSFER
    metadata = json.loads((ROOT / '.runtime/psyche-deploy/source-metadata.json').read_text())
    repo = 'MindIntLab/Psyche-R1'
    revision = '2c2dc49d0f5027931b152ab776a90c6bfcd0eecb'
    if metadata['repo_id'] != repo or metadata['revision'] != revision:
        raise SystemExit('Source metadata does not match the pinned model revision.')
    token = get_token()
    if not token:
        raise SystemExit('No local Hugging Face credential is available.')
    aria_root = ROOT / '.runtime/psyche-aria2'
    aria = aria_root / 'usr/bin/aria2c'
    if not aria.is_file():
        raise SystemExit('The native aria2 runtime is missing.')
    source = ROOT / 'models/Psyche-R1/source'
    cache = source / '.cache/huggingface/download'
    source.mkdir(parents=True, exist_ok=True)

    # Keep the mirror credential off subprocess arguments and CDN requests.
    session = requests.Session()
    entries = []
    for item in metadata['files']:
        name = item['rfilename']
        if not name.endswith('.safetensors'):
            continue
        target = source / name
        digest = item['lfs']['sha256']
        if not target.exists():
            partials = sorted(cache.glob(f'*.{digest}.incomplete'),
                              key=lambda path: path.stat().st_size, reverse=True)
            if partials and partials[0].stat().st_size <= item['size']:
                length = partials[0].stat().st_size
                partials[0].replace(target)
                print(f'保留已有顺序下载：{name}，{length / 1024**2:.1f} MiB', flush=True)
        url = f'https://hf-mirror.com/{repo}/resolve/{revision}/{name}'
        with session.get(url, headers={'Authorization': f'Bearer {token}'},
                         stream=True, timeout=(10, 30)) as response:
            response.raise_for_status()
            direct_url = response.url
        host = urlsplit(direct_url).hostname or ''
        if urlsplit(direct_url).scheme != 'https' or any(c.isspace() for c in direct_url):
            raise RuntimeError('Unexpected download URL format.')
        if not host.endswith(('.hf.co', '.huggingface.co', '.hf-mirror.com', '.amazonaws.com')):
            raise RuntimeError('Unexpected model CDN domain.')
        if host == 'hf-mirror.com':
            raise RuntimeError('A separate signed CDN URL was not returned.')
        print(f'分段源已就绪：{name}，CDN={host}', flush=True)
        entries.extend([direct_url, f'  dir={source}', f'  out={name}',
                        f'  checksum=sha-256={digest}', ''])

    control = ROOT / '.runtime/psyche-deploy/aria2-input.private'
    descriptor = os.open(control, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(control, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write('\n'.join(entries))
    environment = dict(os.environ)
    libraries = str(aria_root / 'usr/lib/aarch64-linux-gnu')
    environment['LD_LIBRARY_PATH'] = libraries + ':' + environment.get('LD_LIBRARY_PATH', '')
    command = [str(aria), '--input-file', str(control), '--continue=true',
               '--check-integrity=true', '--file-allocation=none', '--auto-file-renaming=false',
               '--max-connection-per-server=8', '--split=8', '--min-split-size=8M',
               '--max-concurrent-downloads=3', '--max-tries=5', '--retry-wait=3',
               '--connect-timeout=10', '--timeout=45', '--summary-interval=15',
               '--console-log-level=warn', '--enable-color=false', '--download-result=hide']
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1, env=environment)
        ACTIVE_TRANSFER = process
        last_progress_print = 0
        for line in process.stdout:
            safe = line.replace(token, '[REDACTED]')
            safe = re.sub(r'hf_[A-Za-z0-9_-]+', '[REDACTED]', safe)
            safe = re.sub(r'(https?://[^\s?]+)\?[^\s]+', r'\1?[REDACTED]', safe)
            if safe.startswith('[DL:'):
                progress_path = ROOT / '.runtime/psyche-deploy/transfer-progress.json'
                temporary = progress_path.with_suffix('.json.tmp')
                temporary.write_text(json.dumps({
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                    'status_line': safe.strip(), 'source_verified': False,
                    'note': 'aria2 active-transfer progress; file sizes alone do not prove completion.',
                }, ensure_ascii=False, indent=2) + '\n')
                temporary.replace(progress_path)
                if time.monotonic() - last_progress_print < 15:
                    continue
                last_progress_print = time.monotonic()
            if safe.strip():
                print(safe.rstrip(), flush=True)
        result = process.wait()
        if result:
            raise SystemExit(f'Parallel transfer stopped with exit code {result}; resume files were kept.')
    finally:
        if ACTIVE_TRANSFER is not None and ACTIVE_TRANSFER.poll() is None:
            ACTIVE_TRANSFER.send_signal(signal.SIGINT)
            try:
                ACTIVE_TRANSFER.wait(timeout=20)
            except subprocess.TimeoutExpired:
                ACTIVE_TRANSFER.kill()
                ACTIVE_TRANSFER.wait()
        control.unlink(missing_ok=True)

    for item in metadata['files']:
        if item['rfilename'].endswith('.safetensors'):
            continue
        hf_hub_download(repo, item['rfilename'], revision=revision, local_dir=source, token=True)
    result = subprocess.run(['bash', 'scripts/download_psyche.sh', '--verify-only'])
    return result.returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except requests.RequestException as error:
        # HTTP exceptions can contain signed URLs; report only a status and type.
        status = error.response.status_code if error.response is not None else 'network'
        raise SystemExit(f'Model transfer failed: {type(error).__name__}, status={status}.')
    except KeyboardInterrupt:
        raise SystemExit('Transfer stopped; resume data was preserved.')
