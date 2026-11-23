import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from orin_voice.assets import download_ranges, safe_extract, sha256_file, verify
from orin_voice.llm import LocalChat, boundary_flags, first_sentence, local_url, visible_text
from orin_voice.server import LocalServer, binary_path


class LocalOnlyTests(unittest.TestCase):
    def test_chat_history_clear_and_exit(self):
        from argparse import Namespace
        from orin_voice.cli import chat
        calls = []
        def complete(text, history, max_tokens):
            calls.append((text, list(history)))
            return {'reply': '你好。', 'first_visible_token_s': 0.1,
                    'completion_s': 0.2, 'finish_reason': 'stop'}
        args = Namespace(profile='distill9b', port=18080, gpu_layers=99,
                         ctx_size=2048, max_tokens=512)
        with patch('orin_voice.server.LocalServer') as server, \
             patch('orin_voice.llm.LocalChat') as client, \
             patch('builtins.input', side_effect=['第一轮', '第二轮', '/clear', '新会话', '/exit']), \
             patch('sys.stdout', new_callable=io.StringIO):
            client.return_value.complete.side_effect = complete
            chat(args)
            server.return_value.__exit__.assert_called_once()
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][1], [])
        self.assertEqual(calls[1][1][0]['content'], '第一轮')
        self.assertEqual(calls[2][1], [])

    def test_copied_desktop_binary_rejected_on_arm(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'llama-server'
            header = bytearray(20)
            header[:4] = b'\x7fELF'
            header[5] = 1
            header[18:20] = (62).to_bytes(2, 'little')
            executable.write_bytes(header)
            with patch.dict('os.environ', {'LLAMA_SERVER': str(executable)}), \
                 patch('orin_voice.server.platform.machine', return_value='aarch64'):
                with self.assertRaisesRegex(RuntimeError, 'architecture'):
                    binary_path()
            header[18:20] = (183).to_bytes(2, 'little')
            executable.write_bytes(header)
            with patch.dict('os.environ', {'LLAMA_SERVER': str(executable)}), \
                 patch('orin_voice.server.platform.machine', return_value='aarch64'):
                self.assertEqual(binary_path(), executable)

    def test_context_bounds(self):
        self.assertEqual(LocalServer("baseline", context=2048).context, 2048)
        with self.assertRaises(ValueError):
            LocalServer("baseline", context=262144)

    def test_local_addresses(self):
        self.assertEqual(local_url("http://127.0.0.1:18080/"), "http://127.0.0.1:18080")

    def test_external_addresses_rejected(self):
        for address in ["https://example.com", "http://192.168.1.2", "http://127.0.0.1.evil.test", "http://user:password@localhost"]:
            with self.subTest(address=address), self.assertRaises(ValueError):
                local_url(address)

    def test_thinking_hidden(self):
        self.assertEqual(visible_text("<think>internal。</think>你好。"), "你好。")
        self.assertEqual(visible_text("<think>unfinished"), "")
        self.assertEqual(visible_text("<thi"), "")

    def test_sentence_and_flags(self):
        self.assertEqual(first_sentence("你好。你想聊什么？"), "你好。")
        self.assertIsNone(first_sentence("没有完整句子"))
        self.assertIn("possible_diagnosis", boundary_flags("你患有抑郁症"))
        self.assertEqual(boundary_flags("我不能替代专业评估。"), [])

    def test_stream_metrics_and_callback(self):
        events = [{"choices": [{"delta": {"reasoning_content": "hidden"}}]},
                  {"choices": [{"delta": {"content": "你好"}}]},
                  {"choices": [{"delta": {"content": "。"}}]},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        payload = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"
        sentences = []
        with patch("orin_voice.llm.open_local", return_value=io.BytesIO(payload)):
            result = LocalChat().complete("你好", on_first_sentence=sentences.append)
        self.assertEqual(result["reply"], "你好。")
        self.assertEqual(sentences, ["你好。"])
        self.assertEqual(result["finish_reason"], "stop")

    def test_empty_response_fails(self):
        with patch("orin_voice.llm.open_local", return_value=io.BytesIO(b"data: [DONE]\n")), self.assertRaises(RuntimeError):
            LocalChat().complete("你好")


class AssetTests(unittest.TestCase):
    def test_range_resume_preserves_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            partial = Path(directory) / "model.part"
            partial.write_bytes(b"ab")
            response = io.BytesIO(b"cdef")
            response.status = 206
            response.headers = {"Content-Range": "bytes 2-5/6"}
            with patch("orin_voice.assets.urllib.request.urlopen", return_value=response):
                download_ranges("test", {"bytes": 6, "url": "https://example.test/model"}, partial)
            self.assertEqual(partial.read_bytes(), b"abcdef")

    def test_ignored_range_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            partial = Path(directory) / "model.part"
            partial.write_bytes(b"ab")
            response = io.BytesIO(b"abcdef")
            response.status = 200
            response.headers = {}
            with patch("orin_voice.assets.urllib.request.urlopen", return_value=response), self.assertRaises(ValueError):
                download_ranges("test", {"bytes": 6, "url": "https://example.test/model"}, partial)
            self.assertEqual(partial.read_bytes(), b"ab")

    def test_hash_and_size_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "asset"
            filename.write_bytes(b"abc")
            self.assertEqual(verify(filename, {"bytes": 3, "sha256": sha256_file(filename)}), sha256_file(filename))
            with self.assertRaises(ValueError):
                verify(filename, {"bytes": 4})
            with self.assertRaises(ValueError):
                verify(filename, {"sha256": "invalid"})

    def test_archive_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad.tar"
            with tarfile.open(archive, "w") as output:
                member = tarfile.TarInfo("../outside")
                member.size = 1
                output.addfile(member, io.BytesIO(b"x"))
            with self.assertRaises(ValueError):
                safe_extract(archive, Path(directory) / "extracted")

    def test_archive_link_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "link.tar"
            with tarfile.open(archive, "w") as output:
                member = tarfile.TarInfo("link")
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                output.addfile(member)
            with self.assertRaises(ValueError):
                safe_extract(archive, Path(directory) / "extracted")


if __name__ == "__main__":
    unittest.main()
