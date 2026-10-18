"""Jarvis V0.3 pre-release spoken integration tests with synthetic speech."""
import asyncio
import json
import math
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import sounddevice as sd
import edge_tts

from jarvis import Gate, normalize, prepare_wake_voice, WAKE_VOICE
from speech_engine import Segmenter, SpeechEngine

ROOT=Path(__file__).resolve().parent
phrases=[
    ("OlÃ¡, Jarvis", "wake"),
    ("Jarvis, quanto Ã© dois mais dois?", "ask"),
    ("Jarvis, desligar", "sleep"),
    ("Qual Ã© a previsÃ£o do tempo, Jarvis?", "ask"),
]
gate=Gate()
assert gate.decide("Jarvis desligar")[0]=="sleep"
assert gate.decide("VocÃª tÃ¡ me ouvindo?")[0]=="ignore"
assert gate.decide("OlÃ¡ Javes")[0]=="wake"
assert gate.decide("desliga Javes")[0]=="sleep"
assert gate.decide("Jarvis, ligar")[0]=="wake"
print("GATE_TEST_OK",flush=True)

engine=SpeechEngine(ROOT/"models"/"whisper",None,None,None,None,lambda e:None)
engine.load()
print("WHISPER_MODEL_LOAD_OK",flush=True)

for text,expected in phrases:
    path=ROOT/"models"/"synthetic-test.mp3"
    asyncio.run(edge_tts.Communicate(text, "pt-BR-AntonioNeural").save(str(path)))
    a,sr=sf.read(path,dtype="float32")
    if a.ndim==2:a=a.mean(axis=1)
    a=np.interp(np.arange(0,len(a),sr/16000),np.arange(len(a)),a).astype("float32")
    frame_size=1600
    pack=np.pad((a*32767).astype("<i2"),(0,(-len(a))%frame_size))
    silence=np.zeros(frame_size,dtype="<i2").tobytes()
    segmenter=Segmenter()
    got=[]
    frames=[silence]*17+[pack[i:i+frame_size].tobytes() for i in range(0,len(pack),frame_size)]+[silence]*11
    for chunk in frames:
        res=segmenter.push(chunk)
        if res is not None:
            got.append(res)
    assert got, f"NO_UTTERANCE for {text}"
    speech=got[0]
    actual,ms=engine.transcribe(speech)
    action,q=gate.decide(actual)
    print(json.dumps({"expected":text,"recognized":actual,"action":action,
                      "ASR_ms":round(ms),"speech_seconds":round(len(speech)/16000,2)},
                     ensure_ascii=False),flush=True)
    if expected=="sleep":
        assert action=="sleep", f"Sleep failed: {actual}"
    else:
        assert "jarv" in normalize(actual), f"Jarvis name lost: {actual}"
        assert action==expected, f"action expected {expected} but {action} for {actual}"
    path.unlink(missing_ok=True)

t=time.perf_counter()
prepare_wake_voice()
assert WAKE_VOICE.is_file() and WAKE_VOICE.stat().st_size>5000
print("TTS_WAKE_PREPARED_MS",round((time.perf_counter()-t)*1000),flush=True)
for key in ("input","output"):
    device=sd.query_devices(kind=key)
    print("DEFAULT_DEVICE",key,device["name"],flush=True)
print("INTEGRATION_TEST_OK",flush=True)

