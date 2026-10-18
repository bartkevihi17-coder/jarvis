"""Fast local Portuguese recognition for Jarvis.

Uses VAD-like microphone energy segmentation followed by multilingual Whisper-tiny.
No audio is uploaded to a recognition service or stored permanently.
"""
import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from hybrid_stt import GroqSpeech, NoCloudService, SpeechSettings

LOG = logging.getLogger("jarvis")
RATE = 16000
FRAME = 1600
MODEL_NAME = "tiny"
CONTEXT = "Conversa em português brasileiro. Jarvis, Fortnite, The Walking Dead, League of Legends, Project Zomboid. Olá Jarvis. Jarvis ligar. Jarvis desligar. Quanto é dois mais dois?"


class Segmenter:
    """Adaptive background level, preroll and fast end-of-speech detection."""

    def __init__(self):
        self.preroll = deque(maxlen=4)
        self.calibration = []
        self.reset()

    def reset(self):
        self.frames = []
        self.active = False
        self.speech_frames = 0
        self.silent_frames = 0
        self.start_streak = 0
        self.noise = getattr(self, "noise", 0.0018)

    def push(self, block):
        power = np.frombuffer(block, dtype="<i2").astype(np.float32)
        if len(power) == 0:
            return None
        rms = float(np.sqrt(np.mean(np.square(power))) / 32768.0)
        if len(self.calibration) < 15:
            self.calibration.append(rms)
            if len(self.calibration) == 15:
                self.noise = max(0.0004, float(np.quantile(self.calibration, 0.35)))
                LOG.info("MIC ambient calibration rms=%.5f threshold=%.5f", self.noise, self.noise * 1.65)
            return None
        threshold = max(0.0020, self.noise * 1.65)
        loud = rms > threshold
        self.preroll.append(block)
        if not self.active:
            if not loud:
                self.noise = max(0.0003, self.noise * 0.95 + rms * 0.05)
                self.start_streak = 0
                return None
            self.start_streak += 1
            if self.start_streak < 2:
                return None
            self.active = True
            self.frames = list(self.preroll)
            self.speech_frames = self.start_streak
            self.silent_frames = 0
            return None
        self.frames.append(block)
        if loud:
            self.silent_frames = 0
            self.speech_frames += 1
        else:
            self.silent_frames += 1

        if self.silent_frames < 6 and len(self.frames) < 75:
            return None
        complete = (self.speech_frames >= 2 and len(self.frames) >= 5)
        chunks = self.frames
        self.preroll.clear()
        self.reset()
        if not complete:
            return None
        data = np.frombuffer(b"".join(chunks), dtype="<i2")
        return data.astype(np.float32) / 32768.0


class SpeechEngine:
    def __init__(self, model_root, audio, speaking, stop, restart, post, playback=None, stt_settings=None):
        self.model_root = Path(model_root)
        self.audio = audio
        self.speaking = speaking
        self.stop = stop
        self.restart = restart
        self.post = post
        self.model = None
        self.playback = playback
        self.stt_settings = stt_settings or SpeechSettings(Path.home() / ".jarvis-stt.json")
        self.cloud = GroqSpeech(self.stt_settings, self.stt_settings.path.with_name("stt-free-usage.json"))

    def load(self, download=False):
        t = time.perf_counter()
        self.model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8",
                                  cpu_threads=4, num_workers=1,
                                  download_root=str(self.model_root),
                                  local_files_only=not download)
        LOG.info("ASR model=whisper-%s ready_ms=%.0f",
                 MODEL_NAME, (time.perf_counter() - t) * 1000)

    def transcribe(self, audio):
        t = time.perf_counter()
        if self.cloud.enabled():
            try:
                cloud_text = self.cloud.transcribe(audio)
                elapsed = (time.perf_counter() - t) * 1000
                LOG.info("ASR method=groq-free ms=%.0f samples=%d chars=%d", elapsed, len(audio), len(cloud_text))
                self.post(("stt_method", "Groq Online: transcricao pela nuvem"))
                return cloud_text, elapsed
            except NoCloudService as exc:
                LOG.info("ASR groq-free not used: %s; fallback=local", str(exc))
        if self.model is None:
            self.load()
        segments, info = self.model.transcribe(
            audio, language="pt", beam_size=2, best_of=1,
            condition_on_previous_text=False, without_timestamps=True,
            initial_prompt=CONTEXT, vad_filter=False, no_speech_threshold=0.65,
            repetition_penalty=1.05)
        transcript = " ".join(segment.text.strip() for segment in segments).strip()
        elapsed = (time.perf_counter() - t) * 1000
        self.post(("stt_method", "Whisper local: transcricao no computador"))
        LOG.info("ASR method=whisper-tiny phrase_ms=%.0f samples=%d transcript_chars=%d", elapsed,
                 len(audio), len(transcript))
        return transcript, elapsed

    def run(self):
        try:
            self.post(("loading", "Carregando reconhecimento Whisper..."))
            self.load()
        except Exception as exc:
            LOG.exception("Whisper not available")
            self.post(("error", "Modelo Whisper indisponivel: " + str(exc)))
            return

        while not self.stop.is_set():
            frames = queue.Queue(maxsize=80)
            seg = Segmenter()
            try:
                device = self.audio.device("input")

                def callback(indata, count, timestamp, flags):
                    if self.stop.is_set():
                        return
                    try:
                        frames.put_nowait(bytes(indata))
                    except queue.Full:
                        try:
                            frames.get_nowait()
                            frames.put_nowait(bytes(indata))
                        except queue.Empty:
                            pass

                with sd.RawInputStream(samplerate=RATE, channels=1,
                                       blocksize=FRAME, device=device,
                                       dtype="int16", callback=callback):
                    self.restart.clear()
                    self.post(("ready",))
                    LOG.info("MIC started device=%s rate=%d", str(device), RATE)
                    interrupted = False
                    while not self.stop.is_set() and not self.restart.is_set():
                        try:
                            chunk = frames.get(timeout=0.2)
                        except queue.Empty:
                            continue
                        # The playback keeps a short local reference so Jarvis
                        # does not normally interrupt itself via microphone echo.
                        if self.speaking.is_set() and self.playback is not None:
                            if self.playback.is_echo(chunk):
                                continue
                        utterance = seg.push(chunk)
                        if (self.speaking.is_set() and self.playback is not None
                                and seg.active and seg.speech_frames >= 3
                                and not interrupted):
                            if self.playback.interrupt():
                                self.post(("barge_in",))
                                LOG.info("BARGE_IN detected - interrupting spoken answer")
                                interrupted = True
                        if utterance is not None:
                            text, latency = self.transcribe(utterance)
                            if text and not self.stop.is_set() and not self.restart.is_set():
                                self.post(("heard", text))
                            interrupted = False
            except Exception as exc:
                LOG.exception("Microphone processing failure")
                self.post(("error", "Microfone/reconhecimento: " + str(exc)))
                while not self.stop.is_set() and not self.restart.wait(0.3):
                    pass
            finally:
                self.restart.clear()
