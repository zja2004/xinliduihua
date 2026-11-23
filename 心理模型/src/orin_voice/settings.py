import json
import os
from pathlib import Path


ROOT = Path(os.environ.get("ORIN_VOICE_HOME", Path(__file__).resolve().parents[2]))


def load_json(relative_path):
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def profile(name):
    profiles = load_json("configs/profiles.json")
    if name not in profiles:
        raise ValueError(f"Unknown profile: {name}")
    return profiles[name]
