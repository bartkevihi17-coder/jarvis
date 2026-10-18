"""Jarvis V0.5 tests for privacy, free tier quotas and cloud/local fallback."""
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from hybrid_stt import (GroqSpeech, NoCloudService, SpeechSettings, UsageLimiter,
                        MAX_REQUESTS_PER_DAY)
from speech_engine import SpeechEngine

class StubResponse(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self,*args):
        self.close()

class HybridTests(unittest.TestCase):
    def test_local_default_never_sends_network_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf=SpeechSettings(Path(tmp)/"settings.json")
            self.assertEqual(conf.mode,"auto")
            with patch.object(SpeechSettings,"get_key",return_value=""):
                cloud=GroqSpeech(conf,Path(tmp)/"usage.json")
                self.assertFalse(cloud.enabled())
                with patch("hybrid_stt.urlopen",side_effect=AssertionError("Audio leaked")):
                    with self.assertRaises(NoCloudService):
                        cloud.transcribe(np.zeros(16000,dtype=np.float32))

    def test_mode_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"settings.json"
            a=SpeechSettings(p)
            a.set_mode("local")
            self.assertEqual(SpeechSettings(p).mode,"local")
            self.assertRaises(ValueError,a.set_mode,"paid")

    def test_fake_online_transcription_and_safe_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf=SpeechSettings(Path(tmp)/"settings.json")
            config=GroqSpeech(conf,Path(tmp)/"usage.json")
            recorded=[]
            def fake_http(req,timeout):
                recorded.append((req,timeout))
                return StubResponse(json.dumps({"text":"Jarvis, abre o Fortnite."}).encode())
            with patch.object(SpeechSettings,"get_key",return_value="gsk_TEST_ONLY_NOT_REAL"), \
                    patch("hybrid_stt.urlopen",side_effect=fake_http):
                actual=config.transcribe(np.zeros(16000,dtype=np.float32))
            self.assertEqual(actual,"Jarvis, abre o Fortnite.")
            self.assertEqual(recorded[0][1],8)
            request=recorded[0][0]
            self.assertEqual(request.full_url,"https://api.groq.com/openai/v1/audio/transcriptions")
            self.assertIn(b'whisper-large-v3-turbo',request.data)
            self.assertIn(b'Content-Type: audio/wav',request.data)
            self.assertIn(b'name="language"',request.data)
            self.assertIn(b'pt\r\n',request.data)
            self.assertEqual(config.usage.requests,1)
            self.assertLess(config.usage.seconds,2)

    def test_budget_cannot_exceed_free_user_guardrails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"usage.json"
            meter=UsageLimiter(path)
            meter.requests=MAX_REQUESTS_PER_DAY
            meter.today=__import__("datetime").date.today().isoformat()
            with self.assertRaises(NoCloudService):
                meter.reserve(1.0)
            self.assertEqual(meter.requests,MAX_REQUESTS_PER_DAY)

    def test_online_failure_falls_back_to_whisper(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf=SpeechSettings(Path(tmp)/"settings.json")
            class Segment: text="Jarvis, você me escuta?"
            class LocalModel:
                def transcribe(self,*args,**kwargs):
                    return [Segment()],None
            engine=SpeechEngine(Path(tmp),None,None,None,None,lambda e:None,
                                stt_settings=conf)
            engine.model=LocalModel()
            with patch.object(GroqSpeech,"enabled",return_value=True), \
                 patch.object(GroqSpeech,"transcribe",side_effect=NoCloudService("Offline")):
                transcript,ms=engine.transcribe(np.zeros(16000,dtype=np.float32))
            self.assertEqual(transcript,"Jarvis, você me escuta?")

    def test_cloud_success_skips_local_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf=SpeechSettings(Path(tmp)/"settings.json")
            engine=SpeechEngine(Path(tmp),None,None,None,None,lambda e:None,
                                stt_settings=conf)
            with patch.object(GroqSpeech,"enabled",return_value=True), \
                 patch.object(GroqSpeech,"transcribe",return_value="Olá, Jarvis"), \
                 patch.object(engine,"load",side_effect=AssertionError("Did not use cloud")):
                transcript,ms=engine.transcribe(np.zeros(16000,dtype=np.float32))
            self.assertEqual(transcript,"Olá, Jarvis")

if __name__ == "__main__":
    unittest.main(verbosity=2)
