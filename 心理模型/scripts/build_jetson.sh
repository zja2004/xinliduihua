#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(uname -m)" != aarch64 ] || [ ! -f /etc/nv_tegra_release ]; then
  printf 'This script requires the Jetson itself. No build was started.\n' >&2
  exit 1
fi
if ! command -v nvcc >/dev/null && [ -x /usr/local/cuda/bin/nvcc ]; then
  export PATH="/usr/local/cuda/bin:$PATH"
fi
command -v nvcc >/dev/null || { printf 'CUDA compiler missing. Check JetPack installation.\n' >&2; exit 1; }
mkdir -p .runtime
if [ ! -d .runtime/llama.cpp ]; then
  git clone --depth 1 --branch b11669 https://github.com/ggml-org/llama.cpp.git .runtime/llama.cpp
fi
expected_revision=620d2225cc436ab0b001e3200e590e3b286ef9ab
# Multiple release tags can identify one commit; describe may choose another tag.
if [ "$(git -C .runtime/llama.cpp rev-parse HEAD)" != "$expected_revision" ]; then
  printf 'Unexpected existing llama.cpp revision; refusing to reset it.\n' >&2
  exit 1
fi
cmake -S .runtime/llama.cpp -B .runtime/llama.cpp/build \
  -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 -DCMAKE_BUILD_TYPE=Release \
  -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF
cmake --build .runtime/llama.cpp/build --target llama-server -j2
printf '.runtime/llama.cpp/build/bin/llama-server\n' > .runtime/llama-server.path.tmp
mv .runtime/llama-server.path.tmp .runtime/llama-server.path
printf 'Jetson runtime built; run .venv/bin/orin-voice doctor next.\n'
