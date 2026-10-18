"""Jarvis V0.1 - assistente pessoal Windows, sem servidor da empresa."""
import argparse
import asyncio
import ctypes
import json
import logging
import os
import queue
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
import unicodedata
from collections import deque
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Jarvis"
DATA.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=DATA / "jarvis.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("jarvis")
MODEL = ROOT / "models" / "vosk-model-small-pt-0.3"
LLM = "qwen2.5:1.5b"
VOICE = "pt-BR-AntonioNeural"
STOP = (
    r"\bjarvis\s+(?:desliga|desligar|desligue|dorme|durma|descansa|descansar|descanse|silencio|cale a boca|fica quieto)\b",
    r"\b(?:desliga|desligar|desligue|dorme|durma|descansa|descansar|descanse|silencio)\s+(?:o )?jarvis\b",
    r"\b(?:hora de dormir|vai dormir|pode dormir|pode descansar)\b",
)
WAKE = (
    r"\bjarvis\s+(?:liga|ligar|acorda|acordar|volta|retorna|ativar|ativa)\b",
    r"\b(?:liga|ligar|acorda|acordar|ativa|ativar)\s+(?:o )?jarvis\b",
)
SYSTEM = ("Voce e Jarvis, um assistente pessoal por voz em portugues brasileiro. "
          "Converse naturalmente, com memoria do dialogo, de forma objetiva e inteligente. "
          "Nao invente fatos nem afirme ter controlado o PC: esta versao so conversa. "
          "Para perguntas simples, responda preferencialmente em ate tres frases. "
          "Voce e independente de qualquer empresa, servidor corporativo ou credencial de trabalho.")


def normalize(text):
    text = unicodedata.normalize("NFKD", text.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ",
             "".join(c for c in text if not unicodedata.combining(c)))).strip()


class Gate:
    def __init__(self):
        self.enabled = True

    def decide(self, phrase):
        n = normalize(phrase)
        if not n:
            return "ignore", ""
        if any(re.search(p, n) for p in STOP):
            self.enabled = False
            return "sleep", ""
        has_name = bool(re.search(r"\b(?:jarvis|javis|jarves)\b", n))
        if not self.enabled and not has_name:
            return "ignore", ""
        explicit = any(re.search(p, n) for p in WAKE)
        self.enabled = True
        q = re.sub(r"\b(?:jarvis|javis|jarves)\b", " ", phrase, flags=re.I).strip(" ,.!?")
        if explicit:
            q = re.sub(r"\b(?:liga|ligar|acorda|acordar|volta|retorna|ativar|ativa)\b",
                       " ", q, flags=re.I).strip(" ,.!?")
        if not q or len(q) < 3:
            return "wake", ""
        return "ask", q


class Brain:
    def __init__(self):
        self.memory = deque(maxlen=12)

    def ask(self, q):
        payload = json.dumps({"model": LLM, "messages": [
            {"role": "system", "content": SYSTEM}, *list(self.memory),
            {"role": "user", "content": q}
        ], "stream": False, "options": {
            "temperature": 0.65, "num_predict": 180, "num_ctx": 2048, "num_thread": 2
        }}, ensure_ascii=False).encode("utf-8")
        req = Request("http://127.0.0.1:11434/api/chat", payload,
                      {"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=180) as resp:
            answer = json.load(resp)["message"]["content"].strip()
        if not answer:
            raise RuntimeError("O modelo nao retornou nenhuma resposta.")
        self.memory.extend(({"role": "user", "content": q},
                            {"role": "assistant", "content": answer}))
        return answer


def play_mp3(filename):
    alias = "jarvis" + uuid4().hex[:10]
    mci = ctypes.windll.winmm.mciSendStringW

    def send(cmd):
        buf = ctypes.create_unicode_buffer(512)
        code = mci(cmd, buf, 512, 0)
        if code:
            raise OSError("Reproducao de audio Windows: erro " + str(code))
    send('open "' + filename + '" type mpegvideo alias ' + alias)
    try:
        send("play " + alias + " wait")
    finally:
        send("close " + alias)


def synthesize_and_play(text, busy):
    import edge_tts
    tmp = Path(tempfile.gettempdir()) / ("jarvis-" + uuid4().hex + ".mp3")
    busy.set()
    try:
        asyncio.run(edge_tts.Communicate(text, VOICE, rate="+5%").save(str(tmp)))
        play_mp3(str(tmp))
    finally:
        busy.clear()
        tmp.unlink(missing_ok=True)


def check():
    import importlib
    issues = []
    for name in ("vosk", "sounddevice", "edge_tts", "pystray", "PIL"):
        try:
            importlib.import_module(name)
        except Exception as exc:
            issues.append(name + ": " + str(exc))
    if not (MODEL / "conf" / "model.conf").exists():
        issues.append("Modelo Vosk portugues nao instalado")
    try:
        with urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
            d = json.load(r)
        if not any(m.get("name") == LLM or m.get("model") == LLM for m in d.get("models", [])):
            issues.append("Qwen2.5 ainda nao instalado no Ollama")
    except Exception as exc:
        issues.append("Ollama local indisponivel: " + str(exc))
    for issue in issues:
        print("FAIL", issue)
    if not issues:
        print("CHECK_OK: dependencias, voz e modelo local presentes")
    return 1 if issues else 0


class Jarvis:
    def __init__(self, minimized=False):
        self.root = tk.Tk()
        self.root.title("JARVIS | Assistente pessoal")
        self.root.geometry("560x650")
        self.root.minsize(440, 500)
        self.root.configure(bg="#0b111b")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Escape>", lambda _: self.hide())
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.speaking = threading.Event()
        self.gate = Gate()
        self.brain = Brain()
        self.working = False
        self.tray = None
        self.last = ("", 0)
        self.make_ui()
        self.root.after(100, self.poll)
        self.root.after(900, lambda: self.hide() if minimized else None)

    def make_ui(self):
        bg, card, fg, muted, accent = "#0b111b", "#172335", "#eff7ff", "#9aaec5", "#5ac8fa"
        tk.Label(self.root, text="J A R V I S", bg=bg, fg=accent,
                 font=("Segoe UI Semibold", 24)).pack(pady=(23, 4))
        tk.Label(self.root, text="ASSISTENTE PESSOAL  /  V0.1", bg=bg, fg=muted,
                 font=("Segoe UI", 9)).pack()
        self.status = tk.StringVar(value="Preparando microfone...")
        tk.Label(self.root, textvariable=self.status, bg=bg, fg=fg,
                 font=("Segoe UI Semibold", 12)).pack(pady=(20, 15))
        bar = tk.Frame(self.root, bg=bg)
        bar.pack(fill="x", padx=24)
        self.toggle_button = tk.Button(bar, command=self.toggle, bg="#62e2bb", fg=bg,
                                       font=("Segoe UI Semibold", 10), bd=0, pady=12)
        self.toggle_button.pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Button(bar, text="Ocultar painel", command=self.hide, bg=card, fg=fg,
                  bd=0, pady=12).pack(side="left", fill="x", expand=True)
        self.chat = tk.Text(self.root, bg=card, fg=fg, font=("Segoe UI", 10),
                            bd=0, padx=16, pady=15, wrap="word", state="disabled")
        self.chat.pack(fill="both", expand=True, padx=24, pady=15)
        inp = tk.Frame(self.root, bg=bg)
        inp.pack(fill="x", padx=24, pady=(0, 18))
        self.entry = tk.Entry(inp, bg=card, fg=fg, insertbackground=fg, bd=0,
                              font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, ipady=12, ipadx=10)
        self.entry.bind("<Return>", lambda _: self.submit())
        tk.Button(inp, text="Enviar", command=self.submit, bg=accent, fg=bg,
                  bd=0, padx=16, pady=12).pack(side="right", padx=(8, 0))
        self.refresh_state()

    def add(self, who, text):
        self.chat.configure(state="normal")
        self.chat.insert("end", who + ": " + text + "\n\n")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def refresh_state(self):
        active = self.gate.enabled
        self.toggle_button.configure(text="Silenciar Jarvis" if active else "Ativar Jarvis",
                                     bg="#62e2bb" if active else "#30445b")
        self.status.set("Escutando o ambiente" if active else
                        'Silencioso: diga "Jarvis" para voltar')

    def toggle(self):
        self.gate.enabled = not self.gate.enabled
        self.refresh_state()

    def submit(self):
        text = self.entry.get().strip()
        self.entry.delete(0, "end")
        if text:
            self.queue.put(("heard", text))

    def heard(self, phrase):
        self.queue.put(("heard", phrase))

    def handle(self, text):
        now = time.monotonic()
        if normalize(text) == self.last[0] and now - self.last[1] < 2:
            return
        self.last = (normalize(text), now)
        action, q = self.gate.decide(text)
        if action == "ignore":
            return
        self.add("Voce", text)
        self.refresh_state()
        if action == "sleep":
            return
        if self.working:
            self.add("Sistema", "Aguarde a resposta atual.")
            return
        if action == "wake":
            self.working = True
            threading.Thread(target=self.answer_wake, daemon=True).start()
        elif action == "ask":
            self.working = True
            self.status.set("Pensando...")
            threading.Thread(target=self.answer, args=(q,), daemon=True).start()

    def answer_wake(self):
        try:
            synthesize_and_play("Estou ouvindo.", self.speaking)
        except Exception as exc:
            self.queue.put(("error", str(exc)))
        finally:
            self.queue.put(("done",))

    def answer(self, q):
        try:
            a = self.brain.ask(q)
            if self.gate.enabled and not self.stop.is_set():
                self.queue.put(("reply", a))
                synthesize_and_play(re.sub(r"[#*_]+", "", a)[:850], self.speaking)
        except Exception as exc:
            LOG.exception("Resposta falhou")
            self.queue.put(("error", str(exc)))
        finally:
            self.queue.put(("done",))

    def listen(self):
        import sounddevice as sd
        from vosk import Model, KaldiRecognizer, SetLogLevel
        try:
            SetLogLevel(-1)
            model = Model(str(MODEL))
            rec = KaldiRecognizer(model, 16000)
            audio_q = queue.Queue(maxsize=20)

            def callback(data, frames, t, status):
                if not self.speaking.is_set() and not self.stop.is_set():
                    try:
                        audio_q.put_nowait(bytes(data))
                    except queue.Full:
                        try:
                            audio_q.get_nowait()
                            audio_q.put_nowait(bytes(data))
                        except queue.Empty:
                            pass

            with sd.RawInputStream(samplerate=16000, channels=1,
                                   blocksize=4000, dtype="int16", callback=callback):
                self.queue.put(("ready",))
                while not self.stop.is_set():
                    if self.speaking.is_set():
                        rec.Reset()
                        while not audio_q.empty():
                            try: audio_q.get_nowait()
                            except queue.Empty: break
                        time.sleep(0.1)
                        continue
                    try:
                        data = audio_q.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if rec.AcceptWaveform(data):
                        phrase = json.loads(rec.Result()).get("text", "").strip()
                        if phrase:
                            self.heard(phrase)
        except Exception as exc:
            LOG.exception("Microfone falhou")
            self.queue.put(("error", "Microfone: " + str(exc)))

    def poll(self):
        if self.stop.is_set():
            return
        for _ in range(20):
            try:
                event = self.queue.get_nowait()
            except queue.Empty:
                break
            tag, *data = event
            if tag == "heard":
                self.handle(data[0])
            elif tag == "reply":
                self.add("Jarvis", data[0])
            elif tag == "error":
                self.add("Erro", data[0])
                self.status.set("Erro: verifique o painel")
            elif tag == "ready":
                self.refresh_state()
            elif tag == "done":
                self.working = False
                self.refresh_state()
            elif tag == "open":
                self.show()
            elif tag == "toggle":
                self.toggle()
            elif tag == "exit":
                self.exit()
                return
        self.root.after(100, self.poll)

    def tray_run(self):
        import pystray
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (64, 64), "#0b111b")
        draw = ImageDraw.Draw(im)
        draw.ellipse((9, 9, 55, 55), outline="#5ac8fa", width=5)
        draw.ellipse((23, 23, 41, 41), fill="#62e2bb")
        self.tray = pystray.Icon("JarvisPessoal", im, "Jarvis",
                                menu=pystray.Menu(
                                    pystray.MenuItem("Abrir painel", lambda *_: self.queue.put(("open",)), default=True),
                                    pystray.MenuItem("Ativar/silenciar", lambda *_: self.queue.put(("toggle",))),
                                    pystray.MenuItem("Sair do Jarvis", lambda *_: self.queue.put(("exit",)))))
        self.tray.run()

    def hide(self):
        self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def exit(self):
        self.stop.set()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def run(self):
        threading.Thread(target=self.tray_run, daemon=True, name="JarvisTray").start()
        threading.Thread(target=self.listen, daemon=True, name="JarvisMic").start()
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--minimized", action="store_true")
    args = parser.parse_args()
    if args.check:
        return check()
    Jarvis(minimized=args.minimized).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
