import argparse
import hashlib
from pathlib import Path
import platform
import subprocess
import tarfile
import urllib.request


root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--cuda", action="store_true")
arguments = parser.parse_args()
if platform.machine() != "x86_64":
    raise SystemExit("On Jetson, use bash scripts/build_jetson.sh; do not install desktop CUDA binaries")
tag = "b11669"
variants = [f"llama-{tag}-bin-ubuntu-x64.tar.gz"]
if arguments.cuda:
    variants = [f"llama-{tag}-bin-ubuntu-cuda-12.8-x64.tar.gz", f"cudart-llama-{tag}-bin-ubuntu-cuda-12.8-x64.tar.gz"]
runtime = root / ".runtime"
runtime.mkdir(exist_ok=True)
for filename in variants:
    archive = runtime / filename
    if not archive.exists():
        partial = archive.with_suffix(".part")
        print(f"Downloading {filename}", flush=True)
        subprocess.run(["curl", "--fail", "--location", "--retry", "12", "--retry-all-errors",
                        "--connect-timeout", "30", "--continue-at", "-", "--output", str(partial),
                        f"https://github.com/ggml-org/llama.cpp/releases/download/{tag}/{filename}"], check=True)
        partial.replace(archive)
    destination = runtime / ("llama-cudart" if filename.startswith("cudart") else "llama-bin")
    destination.mkdir(exist_ok=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")
    print(f"{filename}: sha256={hashlib.sha256(archive.read_bytes()).hexdigest()}", flush=True)
server = next((runtime / "llama-bin").rglob("llama-server"))
(runtime / "llama-server.path").write_text(str(server.relative_to(root)) + "\n")
print(f"Ready: {server}")
