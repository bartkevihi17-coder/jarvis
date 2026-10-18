"""Low-latency interruptible Jarvis playback and acoustic echo screening.

Everything stays on the user's personal PC. This is a best-effort filter,
not a substitute for hardware acoustic echo cancellation.
"""
import logging
import threading
import time

import numpy as np
import sounddevice as sd

LOG = logging.getLogger("jarvis")


class PlaybackControl:
    def __init__(self):
        self.speaking = threading.Event()
        self.cancelled = threading.Event()
        self._play_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._reference = None
        self._started = 0.0

    def interrupt(self):
        """Request a stop that takes effect within one playback polling interval."""
        if not self.speaking.is_set():
            return False
        self.cancelled.set()
        return True

    def is_echo(self, pcm_bytes):
        """Detect strongly correlated loudspeaker-to-mic bleed, accounting for delay."""
        with self._state_lock:
            reference = self._reference
            elapsed = time.monotonic() - self._started
        if reference is None or not self.speaking.is_set():
            return False
        a = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32)[::4]
        if len(a) < 100:
            return False
        a -= a.mean()
        scale = np.linalg.norm(a)
        if scale < 800:
            return False
        # Playback and mic callbacks can be separated by a few hundred milliseconds.
        # Check temporal offsets without recording or persisting either signal.
        center = int(elapsed * 4000)
        best = 0.0
        for delay in range(-2600, 501, 100):
            begin = center + delay - len(a)
            if begin < 0 or begin + len(a) > len(reference):
                continue
            b = reference[begin:begin + len(a)]
            b = b - b.mean()
            denom = scale * np.linalg.norm(b)
            if denom > 1:
                best = max(best, abs(float(np.dot(a, b) / denom)))
        return best >= 0.66

    def play(self, data, rate, output_device=None, still_current=lambda: True):
        """Return True only when the whole utterance played, False if interrupted."""
        with self._play_lock:
            if not still_current():
                return False
            # The same speaker object serializes playback, preventing sd.stop races.
            with self._state_lock:
                self.cancelled.clear()
                mono = data.mean(axis=1) if getattr(data, "ndim", 1) == 2 else data
                indices = np.arange(0, len(mono), rate / 4000, dtype=np.float32)
                sampled = np.interp(indices, np.arange(len(mono)), mono)
                self._reference = (sampled * 32767).astype(np.float32)
                self._started = time.monotonic()
                self.speaking.set()
            try:
                sd.play(data, rate, device=output_device, blocking=False)
                while True:
                    if self.cancelled.is_set() or not still_current():
                        sd.stop()
                        LOG.info("VOICE interrupted during playback")
                        return False
                    if not sd.get_stream().active:
                        return True
                    self.cancelled.wait(0.03)
            finally:
                sd.stop()
                with self._state_lock:
                    self.speaking.clear()
                    self._reference = None
                    self._started = 0.0
