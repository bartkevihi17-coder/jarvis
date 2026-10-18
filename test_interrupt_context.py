"""Jarvis V0.4: interrupt safety, echo filtering, persistence and context tests."""
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from audio_control import PlaybackControl
from conversation_context import ConversationContext
from jarvis import Brain, Gate, Jarvis


class FakeStream:
    active = True


class InterruptTests(unittest.TestCase):
    def test_audio_stops_quickly_and_old_turn_cannot_restart(self):
        ctl = PlaybackControl()
        stream = FakeStream()
        stopped = []
        def fake_stop():
            stopped.append(time.perf_counter())
            stream.active = False
        audio = np.sin(2 * np.pi * 140 * np.arange(48000) / 24000).astype(np.float32)
        results = []
        with patch("audio_control.sd.play", return_value=None), \
             patch("audio_control.sd.get_stream", return_value=stream), \
             patch("audio_control.sd.stop", side_effect=fake_stop):
            worker = threading.Thread(target=lambda: results.append(ctl.play(audio,24000)),daemon=True)
            worker.start()
            self.assertTrue(ctl.speaking.wait(3))
            start=time.perf_counter()
            self.assertTrue(ctl.interrupt())
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results,[False])
        self.assertTrue(stopped)
        self.assertLess(stopped[0]-start,0.2)
        self.assertFalse(ctl.speaking.is_set())

    def test_echo_rejected_without_muting_distinct_voice(self):
        ctl=PlaybackControl()
        wave=np.sin(2*np.pi*163*np.arange(32000)/16000).astype(np.float32)
        ref=wave[::4]*32767
        with ctl._state_lock:
            ctl._reference=ref
            ctl._started=time.monotonic()-0.5
            ctl.speaking.set()
        center=int(.5*4000)
        clip=np.repeat(ref[center-400:center].astype("<i2"),4).tobytes()
        self.assertTrue(ctl.is_echo(clip))
        rng=np.random.default_rng(22)
        voice=rng.integers(-7000,7000,1600,dtype=np.int16).tobytes()
        self.assertFalse(ctl.is_echo(voice))

    def test_stale_generation_ui_does_not_reset_new_turn(self):
        app=Jarvis()
        app.root.withdraw()
        try:
            app.turn_id=4
            app.working=True
            app.queue.put(("done",3))
            app.poll()
            self.assertTrue(app.working)
            app.queue.put(("reply",3,"OLD REPLY MUST NOT SHOW"))
            app.poll()
            self.assertNotIn("OLD REPLY MUST NOT SHOW",app.chat.get("1.0","end"))
            app.queue.put(("done",4))
            app.poll()
            self.assertFalse(app.working)
        finally:
            app.exit()

    def test_sleep_and_followup_interaction(self):
        gate=Gate()
        self.assertEqual(gate.decide("desliga Jarvis")[0],"sleep")
        self.assertEqual(gate.decide("qual a idade dele?")[0],"ignore")
        self.assertEqual(gate.decide("Qual a idade dele, Jarvis?")[0],"ask")
        self.assertEqual(gate.decide("E onde ele nasceu?")[0],"ask")


class ContextTests(unittest.TestCase):
    def test_memory_survives_restart_and_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"context.json"
            ctx=ConversationContext(path)
            ctx.remember("Meu cachorro se chama Amigo.","Entendi, seu cachorro se chama Amigo.")
            self.assertTrue(path.exists())
            ctx2=ConversationContext(path)
            self.assertEqual(ctx2.snapshot()[-2]["role"],"user")
            self.assertIn("Amigo",ctx2.snapshot()[-1]["content"])
            ctx2.clear()
            self.assertEqual(ConversationContext(path).snapshot(),[])

    def test_brain_uses_followup_context_and_ignores_cancelled_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx=ConversationContext(Path(tmp)/"context.json")
            ctx.remember("Eu estou falando sobre o personagem Pedro.","Entendi, Pedro.")
            brain=Brain(store=ctx)
            captured=[]
            def mock_urlopen(req, timeout):
                captured.append(json.loads(req.data))
                return io.BytesIO(json.dumps({"message":{"content":"Ele tem vinte anos."}}).encode())
            with patch("jarvis.urlopen",side_effect=mock_urlopen):
                result=brain.ask("Quantos anos ele tem?")
                self.assertEqual(result,"Ele tem vinte anos.")
                self.assertIn("Pedro",repr(captured[0]["messages"]))
                before=len(ctx.snapshot(max_messages=32))
                self.assertEqual(brain.ask("Nova pergunta",lambda:False),"")
                self.assertEqual(len(ctx.snapshot(max_messages=32)),before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
