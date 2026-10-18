"""Smoke test of Jarvis GUI, mute/wake transcript and routed natural TTS."""
import os
import sys
import time
import tkinter as tk
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)
from jarvis import Jarvis

app=Jarvis()
app.root.withdraw()
app.root.update()
assert app.root.winfo_exists()
assert len(app.input_combo["values"])>1
assert len(app.output_combo["values"])>1
app.handle("Jarvis desligar")
app.root.update()
assert not app.gate.enabled
app.handle("OlÃ¡ Javes")
app.root.update()
assert app.gate.enabled
# Wait until neural acknowledgement finishes. Real selected audio device is exercised.
deadline=time.monotonic()+20
while app.working and time.monotonic()<deadline:
    time.sleep(.2)
    app.root.update()
app.root.update()
transcript=app.chat.get("1.0","end")
assert "Jarvis desligar" in transcript
assert "OlÃ¡ Javes" in transcript
assert "JARVIS VAI FALAR" in transcript,transcript[-500:]
assert "Estou ouvindo." in transcript
assert not app.working
print("WINDOW_CREATED_OK",flush=True)
print("AUDIO_SELECTORS_OK",flush=True)
print("MUTE_AND_WAKE_OK",flush=True)
print("TRANSCRIPT_CHAT_OK",flush=True)
print("WAKE_VOICE_PLAYED_OK",flush=True)
app.exit()
