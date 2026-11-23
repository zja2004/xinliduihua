import argparse
import concurrent.futures
import hashlib
import http.client
import json
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .settings import ROOT, load_json


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path, asset):
    if asset.get("bytes") and path.stat().st_size != asset["bytes"]:
        raise ValueError(f"Unexpected file size: {path}")
    checksum = sha256_file(path)
    if asset.get("sha256") and checksum != asset["sha256"]:
        raise ValueError(f"SHA256 mismatch: {path}; do not load this file")
    return checksum


def safe_extract(archive, destination):
    destination = Path(destination).resolve()
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination):
                raise ValueError(f"Unsafe archive path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"Unsupported archive member: {member.name}")
        options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        bundle.extractall(destination, **options)


def download_ranges(name, asset, partial):
    offset = partial.stat().st_size if partial.exists() else 0
    total = asset["bytes"]
    if offset > total:
        raise ValueError(f"Partial download is larger than expected: {partial}")
    directory = partial.with_suffix(partial.suffix + ".chunks")
    directory.mkdir(exist_ok=True)
    chunk_size = 32 * 1024 * 1024
    spans = [(start, min(start + chunk_size, total) - 1) for start in range(offset, total, chunk_size)]

    def fetch_span(span):
        start, end = span
        output = directory / f"{start}-{end}"
        if output.exists() and output.stat().st_size == end - start + 1:
            return output
        temporary = output.with_suffix(".tmp")
        for attempt in range(8):
            try:
                request = urllib.request.Request(asset["url"], headers={"Range": f"bytes={start}-{end}", "User-Agent": "orin-voice-lab/0.1"})
                with urllib.request.urlopen(request, timeout=45) as response:
                    if response.status != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {start}-{end}/"):
                        raise ValueError("Server did not honor bounded range request")
                    with temporary.open("wb") as handle:
                        shutil.copyfileobj(response, handle, length=1024 * 1024)
                if temporary.stat().st_size != end - start + 1:
                    raise OSError("Range download was truncated")
                temporary.replace(output)
                return output
            except (OSError, urllib.error.URLError, http.client.IncompleteRead):
                if attempt == 7:
                    raise
                time.sleep(min(2 ** attempt, 8))

    print(f"[{name}] resuming {offset / 1e9:.2f} GB with four bounded range connections", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        pending = [executor.submit(fetch_span, span) for span in spans]
        for completed, future in enumerate(concurrent.futures.as_completed(pending), 1):
            future.result()
            if completed % 4 == 0 or completed == len(spans):
                print(f"[{name}] downloaded {completed}/{len(spans)} remaining chunks", flush=True)
    with partial.open("ab") as handle:
        for start, end in spans:
            with (directory / f"{start}-{end}").open("rb") as chunk:
                shutil.copyfileobj(chunk, handle, length=4 * 1024 * 1024)
    verify(partial, asset)
    shutil.rmtree(directory)


def download_asset(name):
    asset = load_json("configs/assets.json")[name]
    destination = ROOT / asset["file"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    if not destination.exists() and asset.get("bytes", 0) > 512 * 1024 * 1024:
        download_ranges(name, asset, partial)
        partial.replace(destination)
    if not destination.exists():
        for attempt in range(12):
            offset = partial.stat().st_size if partial.exists() else 0
            if asset.get("bytes") and offset == asset["bytes"]:
                verify(partial, asset)
                partial.replace(destination)
                break
            headers = {"User-Agent": "orin-voice-lab/0.1"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            print(f"[{name}] downloading from byte {offset}", flush=True)
            request = urllib.request.Request(asset["url"], headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    append = offset > 0 and response.status == 206
                    if append and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                        raise ValueError("Unexpected resume range")
                    mode = "ab" if append else "wb"
                    last_report = time.monotonic()
                    with partial.open(mode) as output:
                        while chunk := response.read(4 * 1024 * 1024):
                            output.write(chunk)
                            if time.monotonic() - last_report > 20:
                                print(f"[{name}] {output.tell() / 1e9:.2f} GB", flush=True)
                                last_report = time.monotonic()
                if asset.get("bytes") and partial.stat().st_size < asset["bytes"]:
                    raise OSError("Connection ended early; retaining partial download for resume")
                verify(partial, asset)
                partial.replace(destination)
                break
            except (OSError, urllib.error.URLError, http.client.IncompleteRead) as error:
                print(f"[{name}] retry after {error}", flush=True)
                if attempt == 11:
                    raise RuntimeError(f"Download failed for {name}: {error}") from error
                time.sleep(min(2 ** attempt, 16))
    checksum = verify(destination, asset)
    if asset.get("extract_to"):
        extraction = ROOT / asset["extract_to"]
        receipt = extraction / ".asset.json"
        if not receipt.exists():
            if extraction.exists():
                raise ValueError(f"Refusing to overwrite unmanaged directory: {extraction}")
            extraction.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=extraction.parent) as temporary:
                safe_extract(destination, temporary)
                Path(temporary, ".asset.json").write_text(
                    json.dumps({"url": asset["url"], "sha256": checksum}, indent=2), encoding="utf-8"
                )
                shutil.move(temporary, extraction)
        elif json.loads(receipt.read_text())["sha256"] != checksum:
            raise ValueError(f"Extracted asset checksum changed: {extraction}")
    receipts = ROOT / "models/receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / f"{name}.json").write_text(
        json.dumps({"name": name, "file": asset["file"], "bytes": destination.stat().st_size,
                    "sha256": checksum, "expected_sha256_provided": bool(asset.get("sha256")),
                    "url": asset["url"]}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[{name}] ready; sha256={checksum}", flush=True)
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="*", default=["baseline", "experimental", "asr", "tts"])
    parser.add_argument("--workers", type=int, default=2)
    arguments = parser.parse_args()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(arguments.workers, 4))) as executor:
        list(executor.map(download_asset, arguments.names))


if __name__ == "__main__":
    main()
