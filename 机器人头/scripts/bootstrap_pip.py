import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request


root = Path(__file__).resolve().parents[1]
runtime = root / ".runtime"
runtime.mkdir(exist_ok=True)
with urllib.request.urlopen("https://pypi.org/pypi/pip/25.3/json", timeout=30) as response:
    metadata = json.load(response)
wheel = next(entry for entry in metadata["urls"] if entry["filename"].endswith(".whl"))
destination = runtime / wheel["filename"]
with urllib.request.urlopen(wheel["url"], timeout=60) as response:
    payload = response.read()
if hashlib.sha256(payload).hexdigest() != wheel["digests"]["sha256"]:
    raise RuntimeError("pip wheel checksum mismatch")
destination.write_bytes(payload)
environment = dict(os.environ, PYTHONPATH=str(destination))
subprocess.run([str(root / ".venv/bin/python"), "-m", "pip", "install", "--no-index", str(destination)],
               env=environment, check=True)
