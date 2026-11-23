"""Bounded endpoint state machine, independent of audio drivers."""
from collections import deque


class Endpoint:
    def __init__(self, silence_ms=1000, min_speech_ms=240, max_seconds=45, frame_ms=30, onset_tolerance_ms=0):
        if not 300 <= silence_ms <= 5000 or max_seconds < 2:
            raise ValueError('Invalid endpoint timing')
        self.frame_ms = frame_ms
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self.maximum = int(max_seconds * 1000 / frame_ms)
        self.onset_window = deque(maxlen=max(1, (90 + onset_tolerance_ms) // frame_ms)) if onset_tolerance_ms else None
        self.pre = deque(maxlen=max(1, 300 // frame_ms))
        self.frames = []
        self.started = False
        self.voiced_ms = self.quiet_ms = self.onset_ms = 0

    def push(self, frame, voiced):
        """Return completed frames or None; caller starts a new instance per turn."""
        if not self.started:
            self.pre.append(frame)
            if self.onset_window is not None:
                self.onset_window.append(bool(voiced))
                self.onset_ms = sum(self.onset_window) * self.frame_ms
            else:
                self.onset_ms = self.onset_ms + self.frame_ms if voiced else 0
            if self.onset_ms < 90:
                return None
            self.started = True
            self.frames = list(self.pre)
            self.voiced_ms = self.onset_ms
        else:
            self.frames.append(frame)
            if voiced:
                self.voiced_ms += self.frame_ms
        self.quiet_ms = 0 if voiced else self.quiet_ms + self.frame_ms
        if self.quiet_ms >= self.silence_ms or len(self.frames) >= self.maximum:
            if self.voiced_ms >= self.min_speech_ms:
                return self.frames
            self.frames.clear()
            if self.onset_window is not None:
                self.onset_window.clear()
            self.pre.clear()
            self.started = False
            self.voiced_ms = self.quiet_ms = self.onset_ms = 0
        return None


class AdaptiveEnergy:
    """Energy gate with a quiet-room floor, noise tracking and release hysteresis.

    This is not a neural VAD. The ceiling remains configurable for existing users.
    """
    def __init__(self, ceiling=.015):
        self.ceiling = ceiling
        self.floor = min(.0025, ceiling)
        self.noise = min(.0005, ceiling / 4)
        self.threshold = self.floor

    def voiced(self, rms, active=False):
        self.threshold = max(self.floor, min(self.ceiling, self.noise * 3.5))
        threshold = self.threshold * .6 if active else self.threshold
        result = rms >= threshold
        if not result and not active:
            self.noise = .98 * self.noise + .02 * rms
        return result
