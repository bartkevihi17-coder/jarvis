"""Jarvis hybrid speech recognition: local Whisper + optional Groq free plan.

The microphone sends audio to Groq ONLY after the user saves an API key
and enables Auto/Groq. There is no paid-tier upgrade and no silent retry.
The configured key is stored in Windows Credential Manager (via keyring).
"""
import io
import json
import logging
import threading
import time
import wave
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import numpy as np

LOG = logging.getLogger("jarvis")
ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3-turbo"
CREDENTIAL_SERVICE = "JarvisPersonalSpeech"
CREDENTIAL_USER = "GroqCloud"
MODE_CHOICES = ("auto", "local", "groq")
CONTEXT = "Conversa em portugues brasileiro. Jarvis, Fortnite, Project Zomboid, The Walking Dead, League of Legends."

# Intentionally much lower than Groq's published Free tier limits.
# No token billing is ever enabled by Jarvis; account tier is controlled by Groq.
MAX_REQUESTS_PER_DAY = 250
MAX_AUDIO_SECONDS_PER_DAY = 3600
MAX_REQUESTS_PER_MINUTE = 8
MAX_AUDIO_SECONDS = 12.0


class NoCloudService(Exception):
    """Cloud not configured, temporarily unavailable or at a safety limit."""


class SpeechSettings:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.mode = "auto"
        self._read()

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("mode") in MODE_CHOICES:
                self.mode = data["mode"]
        except (OSError, ValueError, TypeError):
            pass

    def set_mode(self, mode):
        if mode not in MODE_CHOICES:
            raise ValueError("Invalid recognition mode")
        with self.lock:
            self.mode = mode
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"mode": self.mode}, indent=2), encoding="utf-8")

    @staticmethod
    def get_key():
        try:
            import keyring
            return (keyring.get_password(CREDENTIAL_SERVICE, CREDENTIAL_USER) or "").strip()
        except Exception:
            LOG.warning("Cloud key store unavailable")
            return ""

    @staticmethod
    def set_key(value):
        value = value.strip()
        if value and not value.startswith("gsk_"):
            raise ValueError("A chave da Groq normalmente comeca com gsk_.")
        import keyring
        if value:
            keyring.set_password(CREDENTIAL_SERVICE, CREDENTIAL_USER, value)
        else:
            try:
                keyring.delete_password(CREDENTIAL_SERVICE, CREDENTIAL_USER)
            except keyring.errors.PasswordDeleteError:
                pass


class UsageLimiter:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.recent = []
        self.today = ""
        self.requests = 0
        self.seconds = 0.0
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("day") == date.today().isoformat():
                self.today = data["day"]
                self.requests = max(0, int(data.get("requests", 0)))
                self.seconds = max(0.0, float(data.get("audio_seconds", 0.0)))
        except (OSError, ValueError, TypeError):
            pass

    def reserve(self, audio_seconds):
        with self.lock:
            day = date.today().isoformat()
            if self.today != day:
                self.today, self.requests, self.seconds = day, 0, 0.0
            now = time.monotonic()
            self.recent = [x for x in self.recent if now - x < 60]
            if (len(self.recent) >= MAX_REQUESTS_PER_MINUTE or
                    self.requests >= MAX_REQUESTS_PER_DAY or
                    self.seconds + audio_seconds > MAX_AUDIO_SECONDS_PER_DAY):
                raise NoCloudService("Limite gratuito de seguranca do Jarvis atingido")
            self.recent.append(now)
            self.requests += 1
            self.seconds += audio_seconds
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"day": day, "requests": self.requests,
                                             "audio_seconds": round(self.seconds, 1)}),
                                 encoding="utf-8")


class GroqSpeech:
    def __init__(self, settings, usage_path):
        self.settings = settings
        self.usage = UsageLimiter(usage_path)
        self.cooldown_until = 0

    def enabled(self):
        return self.settings.mode != "local" and bool(self.settings.get_key())

    def transcribe(self, audio):
        if not self.enabled():
            raise NoCloudService("Cloud disabled or no key")
        if time.monotonic() < self.cooldown_until:
            raise NoCloudService("Cloud cooling down")
        seconds = len(audio) / 16000.0
        if seconds < 0.15 or seconds > MAX_AUDIO_SECONDS:
            raise NoCloudService("Audio outside safe limits")
        self.usage.reserve(seconds)
        key = self.settings.get_key()
        # In-memory mono PCM WAV; no raw audio saved to disk.
        samples = (np.clip(audio, -1, 1) * 32767).astype("<i2")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(samples.tobytes())
        boundary = uuid4().hex
        def field(name, value):
            return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                    f"{value}\r\n").encode("utf-8")
        payload = (field("model", MODEL) + field("language", "pt") +
                   field("response_format", "json") + field("prompt", CONTEXT) +
                   f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                   f"filename=\"speech.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode("utf-8") +
                   buffer.getvalue() + b"\r\n" +
                   f"--{boundary}--\r\n".encode("utf-8"))
        req = Request(ENDPOINT, data=payload, headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "User-Agent": "JarvisPersonal/0.5",
        }, method="POST")
        try:
            with urlopen(req, timeout=8) as response:
                obj = json.load(response)
            transcript = str(obj.get("text") or "").strip()
            if not transcript:
                raise NoCloudService("Empty cloud transcript")
            return transcript
        except HTTPError as exc:
            self.cooldown_until = time.monotonic() + (300 if exc.code in (429, 401, 403) else 45)
            LOG.warning("GROQ STT fallback: HTTP %s", exc.code)
            raise NoCloudService(f"Groq HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
            self.cooldown_until = time.monotonic() + 45
            LOG.warning("GROQ STT fallback: network or response error (%s)", type(exc).__name__)
            raise NoCloudService("Cloud temporary unavailable") from None
