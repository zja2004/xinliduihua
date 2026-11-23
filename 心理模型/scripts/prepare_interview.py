"""Generate fixed interview speech once, without opening microphone devices."""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from orin_voice.audio import SpeechSynthesizer
from orin_voice.interview import Interview
from orin_voice.settings import ROOT


def prepare(questions):
    synthesizer = SpeechSynthesizer(cache=True)
    with tempfile.TemporaryDirectory() as directory:
        interview = Interview(questions, directory)
        texts = interview.speech_parts(interview.introduction)
        texts += [f'已记录{number}分。' for number in range(4)]
        texts += [interview.options_prompt, '好的，我来提问。', '21 道题已经完成。',
                  '我在这里，不着急，我们还是当前这道题。',
                  '没听清，请再说一次。', '谢谢，你的补充已经记录。', '你更接近哪一个选项？也可以说跳过这题。']
        for index in range(21):
            interview.index = index
            texts.append(interview.prompt)
        for number, text in enumerate(dict.fromkeys(texts), 1):
            start = time.monotonic()
            synthesizer.synthesize(interview.synthesis_text(text))
            elapsed = time.monotonic() - start
            if elapsed >= .05:
                print(f'准备语音 {number}，{elapsed:.2f}秒：{text[:38]}', flush=True)
    print('固定题目语音已缓存，后续启动可复用。', flush=True)


if __name__ == '__main__':
    prepare(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'configs/dass21.zh.json')
