"""Real end-to-end Jarvis test with local Qwen and the selected voice output."""
import sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from jarvis import Jarvis
app=Jarvis()
app.root.withdraw()
app.root.update()
try:
    app.handle("Jarvis, quanto e 2 mais 2?")
    deadline=time.monotonic()+95
    while app.working and time.monotonic()<deadline:
        app.root.update()
        time.sleep(0.12)
    app.root.update()
    transcript=app.chat.get("1.0","end")
    if app.working:
        raise AssertionError("Timed out waiting for conversational answer and speech")
    if "ERRO" in transcript:
        raise AssertionError("Jarvis UI contains an error: "+transcript[-600:])
    if "JARVIS VAI FALAR" not in transcript:
        raise AssertionError("Jarvis did not render a spoken answer")
    response=transcript.rsplit("JARVIS VAI FALAR",1)[1].strip()
    if "4" not in response and "quatro" not in response.lower():
        raise AssertionError("Jarvis gave an incorrect 2+2 answer: "+response)
    print("BRAIN_CONVERSATION_OK",flush=True)
    print("VOICE_PLAYBACK_COMPLETED",flush=True)
    print("RESPONSE",repr(response[:180]),flush=True)
except Exception:
    import traceback
    traceback.print_exc()
    raise
finally:
    app.exit()
