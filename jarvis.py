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
from tkinter import ttk
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
DEFAULT_AUDIO = "Padrao do Windows"
AUDIO_CONFIG = DATA / "audio-settings.json"
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


class AudioPreferences:
    """Device settings stored only on the personal computer, never in Git."""

    def __init__(self):
        self._lock = threading.RLock()
        self.settings = {"input": DEFAULT_AUDIO, "output": DEFAULT_AUDIO}
        try:
            loaded = json.loads(AUDIO_CONFIG.read_text(encoding="utf-8"))
            for kind in self.settings:
                if isinstance(loaded.get(kind), str):
                    self.settings[kind] = loaded[kind]
        except FileNotFoundError:
            pass
        except Exception:
            LOG.exception("Failed loading audio preferences")

    @staticmethod
    def enumerate_devices(kind):
        import sounddevice as sd
        direction = "max_input_channels" if kind == "input" else "max_output_channels"
        names = [(DEFAULT_AUDIO, None)]
        for i, device in enumerate(sd.query_devices()):
            if device[direction] > 0:
                api = sd.query_hostapis(device["hostapi"])["name"]
                label = f"{device['name']}  [{api} | #{i}]"
                names.append((label, i))
        return names

    def current(self, kind):
        with self._lock:
            return self.settings[kind]

    def save(self, kind, label):
        if kind not in self.settings:
            raise ValueError("Invalid audio direction")
        valid = dict(self.enumerate_devices(kind))
        if label not in valid:
            raise ValueError("Dispositivo de audio indisponivel: " + label)
        with self._lock:
            self.settings[kind] = label
            temporary = AUDIO_CONFIG.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            temporary.replace(AUDIO_CONFIG)

    def device(self, kind):
        label = self.current(kind)
        choices = dict(self.enumerate_devices(kind))
        if label not in choices:
            raise RuntimeError("Dispositivo salvo desconectado: " + label)
        return choices[label]


def synthesize_and_play(text, busy, output_device):
    """Generate natural Edge voice and route decoded MP3 to selected speaker."""
    import edge_tts
    import sounddevice as sd
    import soundfile as sf
    tmp = Path(tempfile.gettempdir()) / ("jarvis-" + uuid4().hex + ".mp3")
    busy.set()
    try:
        asyncio.run(edge_tts.Communicate(text, VOICE, rate="+5%").save(str(tmp)))
        audio, sample_rate = sf.read(str(tmp), dtype="float32", always_2d=False)
        # sounddevice uses PortAudio, so the output device is selected explicitly.
        sd.play(audio, sample_rate, device=output_device, blocking=True)
    finally:
        sd.stop()
        busy.clear()
        tmp.unlink(missing_ok=True)


def check():
    import importlib
    issues = []
    for name in ("vosk", "sounddevice", "soundfile", "edge_tts", "pystray", "PIL"):
        try:
            importlib.import_module(name)
        except Exception as exc:
            issues.append(name + ": " + str(exc))
    if not ((MODEL / "conf" / "model.conf").exists() or (MODEL / "final.mdl").exists()):
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
        self.root.geometry("675x760")
        self.root.minsize(530, 590)
        self.root.configure(bg="#0b111b")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Escape>", lambda _: self.hide())
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.restart_mic = threading.Event()
        self.speaking = threading.Event()
        self.audio = AudioPreferences()
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
                 font=("Segoe UI Semibold", 23)).pack(pady=(17, 2))
        tk.Label(self.root, text="ASSISTENTE PESSOAL  /  V0.2", bg=bg, fg=muted,
                 font=("Segoe UI", 9)).pack()
        self.status = tk.StringVar(value="Preparando microfone...")
        tk.Label(self.root, textvariable=self.status, bg=bg, fg=fg,
                 font=("Segoe UI Semibold", 11)).pack(pady=(12, 10))

        settings = tk.Frame(self.root, bg=card, padx=15, pady=12)
        settings.pack(fill="x", padx=22, pady=(0, 10))
        tk.Label(settings, text="DISPOSITIVOS DE AUDIO", bg=card, fg=accent,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(0, 8))
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
            style.configure("Jarvis.TCombobox", fieldbackground="#263951",
                            background="#263951", foreground="#ffffff",
                            arrowcolor="#5ac8fa", padding=5)
            style.map("Jarvis.TCombobox", fieldbackground=[("readonly", "#263951")],
                      foreground=[("readonly", "#ffffff")])
        except tk.TclError:
            LOG.exception("Combo visual setup failed")
        self.input_var = tk.StringVar(value=self.audio.current("input"))
        self.output_var = tk.StringVar(value=self.audio.current("output"))
        for label, var, key in [
            ("MICROFONE  |  O que Jarvis escuta", self.input_var, "input"),
            ("SAIDA DE VOZ  |  Onde Jarvis fala", self.output_var, "output"),
        ]:
            tk.Label(settings, text=label, bg=card, fg=muted, anchor="w",
                     font=("Segoe UI", 9)).pack(fill="x", pady=(4, 3))
            combobox = ttk.Combobox(settings, state="readonly",
                                    textvariable=var, style="Jarvis.TCombobox")
            combobox.pack(fill="x")
            combobox.bind("<<ComboboxSelected>>",
                          lambda _event, k=key, v=var: self.select_device(k, v.get()))
            if key == "input":
                self.input_combo = combobox
            else:
                self.output_combo = combobox

        controls = tk.Frame(settings, bg=card)
        controls.pack(fill="x", pady=(9, 0))
        tk.Button(controls, text="Atualizar dispositivos", command=self.refresh_devices,
                  bg="#30445b", fg=fg, bd=0, padx=12, pady=8).pack(side="left")
        tk.Button(controls, text="Testar saida de voz", command=self.test_voice,
                  bg="#30445b", fg=fg, bd=0, padx=12, pady=8).pack(side="left", padx=(8, 0))
        self.refresh_devices()

        actions = tk.Frame(self.root, bg=bg)
        actions.pack(fill="x", padx=22)
        self.toggle_button = tk.Button(actions, command=self.toggle, bg="#62e2bb", fg=bg,
                                       font=("Segoe UI Semibold", 10), bd=0, pady=9)
        self.toggle_button.pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Button(actions, text="Ocultar painel", command=self.hide, bg=card, fg=fg,
                  bd=0, pady=9).pack(side="left", fill="x", expand=True)

        tk.Label(self.root, text="TRANSCRICOES  /  OUVIDO E RESPOSTAS",
                 bg=bg, fg=muted, font=("Segoe UI Semibold", 9),
                 anchor="w").pack(fill="x", padx=24, pady=(15, 5))
        box = tk.Frame(self.root, bg=card)
        box.pack(fill="both", expand=True, padx=22)
        self.chat = tk.Text(box, bg=card, fg=fg, font=("Segoe UI", 10),
                            bd=0, padx=14, pady=10, wrap="word", state="disabled")
        scroll = tk.Scrollbar(box, command=self.chat.yview)
        scroll.pack(side="right", fill="y")
        self.chat.configure(yscrollcommand=scroll.set)
        self.chat.pack(fill="both", expand=True)
        self.chat.tag_configure("heard", foreground="#72d7fb")
        self.chat.tag_configure("reply", foreground="#67e1ba")
        self.chat.tag_configure("system", foreground=muted)
        self.chat.tag_configure("error", foreground="#ff8c8c")

        form = tk.Frame(self.root, bg=bg)
        form.pack(fill="x", padx=22, pady=(10, 15))
        self.entry = tk.Entry(form, bg=card, fg=fg, insertbackground=fg, bd=0,
                              font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, ipady=11, ipadx=10)
        self.entry.bind("<Return>", lambda _: self.submit())
        tk.Button(form, text="Enviar", command=self.submit, bg=accent, fg=bg,
                  bd=0, padx=16, pady=11).pack(side="right", padx=(8, 0))
        self.refresh_state()

    def refresh_devices(self):
        try:
            inputs = [label for label, _ in self.audio.enumerate_devices("input")]
            outputs = [label for label, _ in self.audio.enumerate_devices("output")]
            self.input_combo.configure(values=inputs)
            self.output_combo.configure(values=outputs)
            self.input_var.set(self.audio.current("input"))
            self.output_var.set(self.audio.current("output"))
            if self.input_var.get() not in inputs or self.output_var.get() not in outputs:
                self.add("Sistema", "Um dispositivo configurado nao esta mais conectado.",
                         "error")
        except Exception as exc:
            LOG.exception("Could not list audio devices")
            self.add("Erro", "Nao consegui listar dispositivos: " + str(exc), "error")

    def select_device(self, kind, value):
        try:
            self.audio.save(kind, value)
            if kind == "input":
                self.restart_mic.set()
            self.add("Dispositivo", ("Microfone: " if kind == "input" else
                                    "Saida de voz: ") + value, "system")
        except Exception as exc:
            self.add("Erro", str(exc), "error")
            self.refresh_devices()

    def add(self, who, text, tag="system"):
        stamp = time.strftime("%H:%M:%S")
        self.chat.configure(state="normal")
        self.chat.insert("end", f"[{stamp}] {who}\n", tag)
        self.chat.insert("end", str(text) + "\n\n")
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
        # Keep an explicit transcript even when muted and ignoring the phrase.
        self.add("OUVIDO / TRANSCRICAO", text, "heard")
        action, q = self.gate.decide(text)
        self.refresh_state()
        if action == "ignore":
            return
        if action == "sleep":
            self.add("Sistema", "Jarvis silenciado. Continuo ouvindo.", "system")
            return
        if self.working:
            self.add("Sistema", "Aguarde a resposta atual.", "system")
            return
        self.working = True
        if action == "wake":
            threading.Thread(target=self.answer_wake, daemon=True).start()
        elif action == "ask":
            self.status.set("Pensando...")
            threading.Thread(target=self.answer, args=(q,), daemon=True).start()

    def test_voice(self):
        if self.working:
            self.add("Sistema", "Aguarde a fala atual terminar.", "system")
            return
        self.working = True
        sample = "Ola! Eu sou o Jarvis e esta e a minha saida de voz selecionada."
        self.queue.put(("reply", sample))
        threading.Thread(target=self.speak_test, args=(sample,), daemon=True).start()

    def speak_test(self, message):
        try:
            synthesize_and_play(message, self.speaking,
                                self.audio.device("output"))
        except Exception as exc:
            LOG.exception("Voice test failed")
            self.queue.put(("error", "Saida de voz: " + str(exc)))
        finally:
            self.queue.put(("done",))

    def answer_wake(self):
        try:
            self.queue.put(("reply", "Estou ouvindo."))
            synthesize_and_play("Estou ouvindo.", self.speaking,
                                self.audio.device("output"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
        finally:
            self.queue.put(("done",))

    def answer(self, q):
        try:
            a = self.brain.ask(q)
            if self.gate.enabled and not self.stop.is_set():
                speech = re.sub(r"[#*_]+", "", a)[:850]
                self.queue.put(("reply", speech))
                synthesize_and_play(speech, self.speaking,
                                    self.audio.device("output"))
        except Exception as exc:
            LOG.exception("Answer failed")
            self.queue.put(("error", str(exc)))
        finally:
            self.queue.put(("done",))

    def listen(self):
        import sounddevice as sd
        from vosk import Model, KaldiRecognizer, SetLogLevel
        try:
            SetLogLevel(-1)
            model = Model(str(MODEL))
        except Exception as exc:
            LOG.exception("Speech recognition model unavailable")
            self.queue.put(("error", "Reconhecimento: " + str(exc)))
            return
        while not self.stop.is_set():
            try:
                rec = KaldiRecognizer(model, 16000)
                audio_q = queue.Queue(maxsize=24)
                device_id = self.audio.device("input")

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

                with sd.RawInputStream(samplerate=16000, channels=1, device=device_id,
                                       blocksize=4000, dtype="int16", callback=callback):
                    self.restart_mic.clear()
                    self.queue.put(("ready",))
                    while not self.stop.is_set() and not self.restart_mic.is_set():
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
                LOG.exception("Microphone failed")
                self.queue.put(("error", "Microfone: " + str(exc)))
                # Wait for a different device selection rather than retry in a tight loop.
                while not self.stop.is_set() and not self.restart_mic.wait(0.3):
                    pass
            finally:
                self.restart_mic.clear()

    def poll(self):
        if self.stop.is_set():
            return
        for _ in range(24):
            try:
                event = self.queue.get_nowait()
            except queue.Empty:
                break
            tag, *data = event
            if tag == "heard":
                self.handle(data[0])
            elif tag == "reply":
                self.add("JARVIS VAI FALAR", data[0], "reply")
            elif tag == "error":
                self.add("ERRO", data[0], "error")
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
        self.refresh_devices()
        self.root.deiconify()
        self.root.lift()

    def exit(self):
        self.stop.set()
        self.restart_mic.set()
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
    mutex = None
    if os.name == "nt":
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\JarvisPessoalV01")
        if ctypes.windll.kernel32.GetLastError() == 183:
            return 0  # Keep only one listening assistant.
    try:
        Jarvis(minimized=args.minimized).run()
    finally:
        if mutex:
            ctypes.windll.kernel32.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    sys.exit(main())
