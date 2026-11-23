"""Warm fixed robot greetings without opening microphone or moving motors."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from orin_voice.audio import SpeechSynthesizer
from orin_voice.robot_dialogue import ROBOT_GREETING
from orin_voice.interview import Interview
from orin_voice.settings import ROOT
import tempfile

if __name__ == '__main__':
    tts = SpeechSynthesizer(cache=True)
    with tempfile.TemporaryDirectory() as output:
        interview = Interview(ROOT / 'configs/primary21.zh.json', output)
        texts = interview.speech_parts(ROBOT_GREETING)
    for text in texts + [ '好的，我们开始，请根据过去一周的情况作答。',
                 '我在听。准备好后，可以说开始测评，我们一起完成二十一道自评题。']:
        print('准备机器人语音：' + text[:30], flush=True)
        tts.synthesize(text)
    print('机器人开场语音准备完成。', flush=True)
