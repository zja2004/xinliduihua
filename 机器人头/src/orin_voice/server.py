import fcntl
import os
import platform
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .settings import ROOT, profile
from .llm import open_local


def binary_path():
    configured = os.environ.get("LLAMA_SERVER")
    receipt = ROOT / ".runtime/llama-server.path"
    if not configured and receipt.exists():
        configured = receipt.read_text().strip()
    if not configured:
        raise FileNotFoundError("Install llama.cpp: python scripts/install_llama.py (desktop), or bash scripts/build_jetson.sh")
    result = Path(configured)
    if not result.is_absolute():
        result = ROOT / result
    if not result.is_file():
        raise FileNotFoundError(result)
    # Reject copied desktop executables before spawning them on Jetson.
    with result.open('rb') as handle:
        header = handle.read(20)
    machines = {'x86_64': 62, 'aarch64': 183}
    expected = machines.get(platform.machine())
    if header[:4] == b'\x7fELF' and expected is not None:
        order = 'little' if header[5] == 1 else 'big'
        if int.from_bytes(header[18:20], order) != expected:
            raise RuntimeError(f'{result}: executable architecture does not match {platform.machine()}; '
                               'on Jetson run bash scripts/build_jetson.sh')
    return result


class LocalServer:
    def __init__(self, selected, port=18080, gpu_layers=99, threads=4, context=4096):
        if not 512 <= context <= 8192:
            raise ValueError("Prototype context must be between 512 and 8192")
        self.selected = selected
        self.port = port
        self.gpu_layers = gpu_layers
        self.threads = threads
        self.context = context
        self.process = None
        self.lock = None
        self.log = None
        self.address = f"http://127.0.0.1:{port}"

    def __enter__(self):
        model = ROOT / profile(self.selected)["model"]
        if not model.is_file():
            raise FileNotFoundError(f"Model is not downloaded: {model}")
        executable = binary_path()
        runtime = ROOT / ".runtime"
        runtime.mkdir(exist_ok=True)
        self.lock = (runtime / "llm.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", self.port))
            command = [str(executable), "-m", str(model), "--host", "127.0.0.1", "--port", str(self.port),
                       "-c", str(self.context), "-np", "1", "-b", "256", "-ub", "128", "-t", str(self.threads),
                       "-ngl", str(self.gpu_layers), "--jinja", "--chat-template-kwargs", '{"enable_thinking":false}',
                       "--log-disable"]
            environment = dict(os.environ)
            libraries = {str(executable.parent.resolve())}
            # Desktop CUDA archives must never enter a Jetson loader search path.
            if platform.machine() == 'x86_64':
                libraries.update(str(path.parent) for path in (runtime / 'llama-cudart').rglob('lib*.so*'))
            environment["LD_LIBRARY_PATH"] = ":".join(sorted(libraries) + [environment.get("LD_LIBRARY_PATH", "")])
            self.log = (runtime / f"server-{self.selected}.log").open("w")
            os.chmod(self.log.name, 0o600)
            self.process = subprocess.Popen(command, stdout=self.log, stderr=self.log, env=environment)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"llama-server exited; see {self.log.name}")
                try:
                    with open_local(self.address + "/health", timeout=2) as response:
                        if response.status == 200:
                            return self
                except (OSError, urllib.error.URLError):
                    time.sleep(0.3)
            raise TimeoutError("Model did not become ready within 300 seconds")
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, error_type, error, traceback):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log:
            self.log.close()
        if self.lock:
            self.lock.close()
