#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

tools_python="$PWD/.runtime/psyche-tools/bin/python"
hf_cli="$PWD/.runtime/psyche-tools/bin/hf"
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != '--verify-only' ) ]]; then
  printf '用法：bash scripts/download_psyche.sh [--verify-only]\n' >&2
  exit 2
fi
if [[ ! -x "$tools_python" || ! -x "$hf_cli" ]]; then
  printf '未找到已经准备的 Psyche-R1 下载环境。\n' >&2
  exit 2
fi

if [[ "${1:-}" != '--verify-only' ]]; then
  export HF_ENDPOINT=https://hf-mirror.com
  export HF_HUB_DISABLE_XET=1
  export HF_HUB_ETAG_TIMEOUT=20
  export HF_HUB_DOWNLOAD_TIMEOUT=120
  printf '下载目标：MindIntLab/Psyche-R1，经由 https://hf-mirror.com\n'
  if ! "$tools_python" -c 'import sys; from huggingface_hub import get_token; sys.exit(0 if get_token() else 1)'; then
    printf '此模型需要先在官网取得访问权限。\n'
    printf '下面仅在本机隐藏输入 Token；请使用只读凭证，不要发到聊天里。\n'
    printf '认证请求会把凭证发送至第三方镜像 hf-mirror.com。\n'
    if [[ ! -t 0 ]]; then
      printf '需要在可交互的本机终端运行本脚本。\n' >&2
      exit 2
    fi
    "$hf_cli" auth login
  fi
  "$tools_python" - <<'PY'
import os
from huggingface_hub import HfApi
from huggingface_hub.utils import GatedRepoError, HfHubHTTPError
from requests.exceptions import RequestException

api = HfApi(endpoint=os.environ['HF_ENDPOINT'], headers={'Cache-Control': 'no-cache'})
try:
    api.whoami(token=True)
    print('Token身份校验通过。', flush=True)
    api.auth_check('MindIntLab/Psyche-R1', token=True)
except GatedRepoError:
    raise SystemExit(
        'Token已登录，但该账号尚不能读取Psyche-R1。\n'
        '请用创建此Token的同一账号登录官方模型页，确认已同意访问条件并获准访问：\n'
        'https://huggingface.co/MindIntLab/Psyche-R1\n'
        '创建Token与取得模型访问权是两步；获准后重跑此脚本，无需再次输入有效Token。'
    )
except HfHubHTTPError as error:
    status = error.response.status_code if error.response is not None else 'unknown'
    raise SystemExit(f'访问预检返回HTTP {status}，尚未开始下载。请先检查登录或镜像服务状态。')
except RequestException:
    raise SystemExit('无法连接镜像的访问预检接口，尚未开始下载；请检查网络后重试。')
print('Psyche-R1访问检查通过，开始下载固定版本。', flush=True)
PY
  "$hf_cli" download MindIntLab/Psyche-R1 \
    --revision 2c2dc49d0f5027931b152ab776a90c6bfcd0eecb \
    --max-workers 2 --local-dir models/Psyche-R1/source
fi

"$tools_python" - <<'PY'
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

root = Path('models/Psyche-R1/source')
metadata = json.loads(Path('.runtime/psyche-deploy/source-metadata.json').read_text())
if metadata['repo_id'] != 'MindIntLab/Psyche-R1' or metadata['revision'] != '2c2dc49d0f5027931b152ab776a90c6bfcd0eecb':
    raise SystemExit('来源记录与固定下载版本不符，未进行校验。')
problems = []
for item in metadata['files']:
    name = item['rfilename']
    path = root / name
    if not path.is_file():
        problems.append(f'缺少：{name}')
        continue
    if path.stat().st_size != item['size']:
        problems.append(f'大小不符：{name}')
        continue
    lfs = item.get('lfs')
    digest = hashlib.sha256() if lfs else hashlib.sha1()
    if not lfs:
        digest.update(f'blob {item["size"]}\0'.encode())
    print(f'正在校验：{name}', flush=True)
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != (lfs['sha256'] if lfs else item['blobId']):
        problems.append(f'哈希不符：{name}')
if problems:
    print('\n'.join(problems))
    raise SystemExit('模型文件尚不完整，未标记下载成功。')
status_path = Path('.runtime/psyche-deploy/status.json')
status = json.loads(status_path.read_text())
status.update(state='source_download_verified', weights_downloaded=True,
              source_files_verified=True, verified_at=datetime.now(timezone.utc).isoformat())
status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n')
print('17个源文件大小及哈希校验通过。下一步可以转换和量化；尚未进行模型推理。')
PY
