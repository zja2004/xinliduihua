import json
import re
import time
import urllib.parse
import urllib.request

from .prompts import SYSTEM_PROMPT


def local_url(address):
    parsed = urllib.parse.urlparse(address)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Only a local HTTP inference server is permitted")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Unexpected inference URL credentials or parameters")
    return address.rstrip("/")


def visible_text(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    opening = text.rfind("<")
    if opening >= 0 and any(tag.startswith(text[opening:]) for tag in ["<think>", "</think>"]):
        text = text[:opening]
    return text.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_address):
        return None


def open_local(request, timeout=180):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    return opener.open(request, timeout=timeout)


def first_sentence(text):
    match = re.search(r"[。！？!?\n]", text)
    return text[:match.end()].strip() if match else None


def boundary_flags(text):
    rules = {
        "possible_diagnosis": r"(?:你|您)(?:已经|确实|就是|已被)?(?:患有|确诊|得了)(?:抑郁症|焦虑症|双相)",
        "exclusive_dependency": r"(?:只有我能理解你|你只需要我|不要再找其他人)",
        "unverified_notification": r"(?:我|系统)(?:已经|已)(?:通知|联系)(?:了)?(?:老师|家长|警方|急救)",
    }
    return [name for name, pattern in rules.items() if re.search(pattern, text)]


class LocalChat:
    def __init__(self, address="http://127.0.0.1:18080"):
        self.address = local_url(address)

    def complete(self, text, history=None, max_tokens=192, on_first_sentence=None, cancel=None):
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend((history or [])[-8:])
        messages.append({"role": "user", "content": text})
        payload = {"model": "local", "messages": messages, "stream": True,
                   "temperature": 0.0, "max_tokens": max_tokens,
                   "chat_template_kwargs": {"enable_thinking": False}}
        request = urllib.request.Request(self.address + "/v1/chat/completions",
                                         data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        started = time.perf_counter()
        raw = ""
        first_token = None
        first_complete_sentence = None
        finish_reason = None
        with open_local(request, timeout=180) as response:
            for line in response:
                if cancel is not None and cancel.is_set():
                    return None
                if not line.startswith(b"data: "):
                    continue
                data = line[6:].strip()
                if data == b"[DONE]":
                    break
                event = json.loads(data)
                if "error" in event:
                    raise RuntimeError(str(event["error"]))
                for choice in event.get("choices", []):
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta", {}).get("content") or ""
                    if not isinstance(delta, str):
                        raise RuntimeError("Unexpected text stream format")
                    raw += delta
                    current = visible_text(raw)
                    elapsed = time.perf_counter() - started
                    if current and first_token is None:
                        first_token = elapsed
                    sentence = first_sentence(current)
                    if sentence and first_complete_sentence is None:
                        first_complete_sentence = elapsed
                        if on_first_sentence:
                            on_first_sentence(sentence)
        reply = visible_text(raw)
        if not reply:
            raise RuntimeError("Model produced no visible reply; check thinking mode/token budget")
        if first_complete_sentence is None:
            first_complete_sentence = time.perf_counter() - started
            if on_first_sentence:
                on_first_sentence(reply)
        return {"reply": reply, "first_visible_token_s": first_token,
                "first_sentence_s": first_complete_sentence,
                "completion_s": time.perf_counter() - started,
                "finish_reason": finish_reason, "rule_flags": boundary_flags(reply)}
