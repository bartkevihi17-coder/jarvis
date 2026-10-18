"""Jarvis V0.1 - assistente pessoal Windows, sem servidor da empresa."""
import argparse
import asyncio
import ctypes
import json
import hashlib
import logging
import os
import queue
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import unicodedata
from conversation_context import ConversationContext
from audio_control import PlaybackControl
from hybrid_stt import SpeechSettings
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Jarvis"
DATA.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=DATA / "jarvis.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("jarvis")
MODEL = ROOT / "models" / "vosk-model-small-pt-0.3"
LLM = "qwen2.5:1.5b"
VOICE = "pt-BR-AntonioNeural"
CONTEXT_FILE = DATA / "context.json"
DEFAULT_AUDIO = "Padrao do Windows"
AUDIO_CONFIG = DATA / "audio-settings.json"
STOP = (
    r"\b(?:jarvis|javis|jarves|javes|jarwis|jarvi)\s+(?:desliga|desligar|desligue|dorme|durma|descansa|descansar|descanse|silencio|cale a boca|fica quieto)\b",
    r"\b(?:desliga|desligar|desligue|dorme|durma|descansa|descansar|descanse|silencio)\s+(?:o )?(?:jarvis|javis|jarves|javes|jarwis|jarvi)\b",
    r"\b(?:hora de dormir|vai dormir|pode dormir|pode descansar)\b",
)
WAKE = (
    r"\b(?:jarvis|javis|jarves|javes|jarwis|jarvi)\s+(?:liga|ligar|acorda|acordar|volta|retorna|ativar|ativa)\b",
    r"\b(?:liga|ligar|acorda|acordar|ativa|ativar)\s+(?:o )?(?:jarvis|javis|jarves|javes|jarwis|jarvi)\b",
)
SYSTEM = ("Voce e Jarvis, um assistente pessoal por voz em portugues brasileiro. "
          "Converse naturalmente, com memoria do dialogo, de forma objetiva e inteligente. Considere as mensagens anteriores ao responder perguntas de continuacao, inclusive pronomes como ele, ela e isso. Se nao houver contexto suficiente, pergunte em vez de inventar. "
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
        has_name = bool(re.search(r"\b(?:jarvis|javis|jarves|javes|jarwis|jarvi)\b", n))
        if not self.enabled and not has_name:
            return "ignore", ""
        explicit = any(re.search(p, n) for p in WAKE)
        self.enabled = True
        q = re.sub(r"\b(?:jarvis|javis|jarves|javes|jarwis|jarvi)\b", " ", phrase, flags=re.I).strip(" ,.!?")
        if explicit:
            q = re.sub(r"\b(?:liga|ligar|acorda|acordar|volta|retorna|ativar|ativa)\b",
                       " ", q, flags=re.I).strip(" ,.!?")
        if not q or normalize(q) in ("ola", "oi", "e ai", "bom dia", "boa tarde", "boa noite"):
            return "wake", ""
        return "ask", q


class Brain:
    def __init__(self, store=None):
        self.context = store or ConversationContext(CONTEXT_FILE)

    def ask(self, q, still_current=lambda: True):
        start = time.perf_counter()
        previous = self.context.snapshot()
        payload = json.dumps({"model": LLM, "messages": [
            {"role": "system", "content": SYSTEM}, *previous,
            {"role": "user", "content": q}
        ], "stream": False, "options": {
            "temperature": 0.18, "num_predict": 128, "num_ctx": 2048, "num_thread": 2
        }}, ensure_ascii=False).encode("utf-8")
        req = Request("http://127.0.0.1:11434/api/chat", payload,
                      {"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=120) as resp:
                answer = json.load(resp)["message"]["content"].strip()
        except (URLError, TimeoutError, OSError) as exc:
            LOG.error("LLM connection failed after %.2fs: %s", time.perf_counter() - start, exc)
            raise RuntimeError("IA local indisponivel: Ollama/Qwen nao esta pronto na porta 11434.") from exc
        if not answer:
            raise RuntimeError("O modelo local retornou uma resposta vazia.")
        if not still_current():
            LOG.info("LLM response discarded because user interrupted")
            return ""
        self.context.remember(q, answer)
        LOG.info("LLM request_ms=%.0f output_chars=%d previous_messages=%d",
                 (time.perf_counter()-start)*1000, len(answer), len(previous))
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


WAKE_VOICE = DATA / "wake-antonio.mp3"


def prepare_wake_voice():
    """Preload only the activation phrase, never cache private conversations."""
    if WAKE_VOICE.is_file() and WAKE_VOICE.stat().st_size > 2000:
        return
    import edge_tts
    tmp = DATA / ("wake-" + uuid4().hex + ".mp3")
    try:
        start = time.perf_counter()
        asyncio.run(edge_tts.Communicate("Estou ouvindo.", VOICE, rate="+5%").save(str(tmp)))
        tmp.replace(WAKE_VOICE)
        LOG.info("TTS wake cache_ms=%.0f", (time.perf_counter()-start)*1000)
    except Exception:
        LOG.exception("Could not prewarm voice")
    finally:
        tmp.unlink(missing_ok=True)


def synthesize_and_play(text, playback, output_device, still_current=lambda: True):
    """Synthesise natural speech and let the listener interrupt playback."""
    import edge_tts
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
    temp = None
    t = time.perf_counter()
    try:
        if not still_current():
            return False
        if text.strip() == "Estou ouvindo." and WAKE_VOICE.is_file():
            file = WAKE_VOICE
        else:
            temp = Path(tempfile.gettempdir()) / ("jarvis-" + uuid4().hex + ".mp3")
            asyncio.run(edge_tts.Communicate(text, VOICE, rate="+5%").save(str(temp)))
            file = temp
        if not still_current():
            return False
        generated = (time.perf_counter() - t) * 1000
        audio, sample_rate = sf.read(str(file), dtype="float32", always_2d=False)
        d = sd.query_devices(output_device, "output") if output_device is not None else sd.query_devices(kind="output")
        target_rate = int(round(d["default_samplerate"]))
        if target_rate != sample_rate:
            x = np.arange(len(audio), dtype=np.float32)
            output_indices = np.arange(0, len(audio), sample_rate / target_rate, dtype=np.float32)
            if getattr(audio, "ndim", 1) == 2:
                audio = np.stack([np.interp(output_indices, x, audio[:, channel])
                                  for channel in range(audio.shape[1])], axis=1)
            else:
                audio = np.interp(output_indices, x, audio)
            sample_rate = target_rate
        finished = playback.play(audio.astype(np.float32), sample_rate,
                                 output_device, still_current)
        LOG.info("TTS generation_ms=%.0f playback_ms=%.0f completed=%s text_chars=%d",
                 generated, (time.perf_counter()-t)*1000-generated, finished, len(text))
        return finished
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def check():
    import importlib
    issues = []
    for name in ("faster_whisper", "sounddevice", "soundfile", "edge_tts", "pystray", "PIL"):
        try:
            importlib.import_module(name)
        except Exception as exc:
            issues.append(name + ": " + str(exc))
    if not (ROOT / "models" / "whisper").exists():
        issues.append("Modelo Whisper portugues nao instalado")
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
        self.root.title("JARVIS | Assistente pessoal - Beta STT")
        self.root.geometry("675x760")
        self.root.minsize(530, 590)
        self.root.configure(bg="#0b111b")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Escape>", lambda _: self.hide())
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.restart_mic = threading.Event()
        self.playback = PlaybackControl()
        self.speaking = self.playback.speaking
        self.turn_id = 0
        self.audio = AudioPreferences()
        self.stt_settings = SpeechSettings(DATA / "stt-settings.json")
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
        tk.Label(self.root, text="ASSISTENTE PESSOAL  /  V0.5 | VOZ HIBRIDA GRATIS", bg=bg, fg=muted,
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
        tk.Button(controls, text="Transcricao IA", command=self.open_stt_settings,
                  bg="#30445b", fg=fg, bd=0, padx=12, pady=8).pack(side="left", padx=(8, 0))
        self.stt_status = tk.StringVar(value="Transcricao local (Groq opcional nas configuracoes)")
        tk.Label(settings, textvariable=self.stt_status, bg=card, fg=muted,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(8, 0))
        self.refresh_devices()

        actions = tk.Frame(self.root, bg=bg)
        actions.pack(fill="x", padx=22)
        self.toggle_button = tk.Button(actions, command=self.toggle, bg="#62e2bb", fg=bg,
                                       font=("Segoe UI Semibold", 10), bd=0, pady=9)
        self.toggle_button.pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Button(actions, text="Ocultar painel", command=self.hide, bg=card, fg=fg,
                  bd=0, pady=9).pack(side="left", fill="x", expand=True)
        tk.Button(actions, text="Limpar contexto", command=self.clear_context, bg=card, fg=fg,
                  bd=0, pady=9).pack(side="left", fill="x", expand=True, padx=(6, 0))

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

    def open_stt_settings(self):
        """Configure optional free cloud ASR without ever showing the stored secret."""
        win = tk.Toplevel(self.root)
        win.title("Jarvis | Reconhecimento de voz")
        win.configure(bg="#0b111b")
        win.geometry("540x350")
        win.resizable(False, False)
        win.transient(self.root)
        bg, card, fg, muted, accent = "#0b111b", "#172335", "#eff7ff", "#9aaec5", "#5ac8fa"
        tk.Label(win, text="RECONHECIMENTO DE VOZ", font=("Segoe UI Semibold", 14),
                 fg=accent, bg=bg).pack(pady=(18, 8))
        options = {
            "Automático — Groq grátis + Whisper local": "auto",
            "Somente Whisper local": "local",
            "Groq grátis — volta ao local se falhar": "groq",
        }
        selected = tk.StringVar(value=next((k for k, v in options.items()
                                            if v == self.stt_settings.mode), list(options)[0]))
        picker = ttk.Combobox(win, values=list(options), state="readonly",
                              textvariable=selected, width=48)
        picker.pack(padx=22, fill="x")
        tk.Label(win, text="A nuvem só recebe trechos de áudio quando a chave estiver configurada.",
                 bg=bg, fg=muted, wraplength=490).pack(padx=16, pady=(9, 10))
        key = tk.Entry(win, show="*", bg=card, fg=fg, insertbackground=fg,
                       font=("Segoe UI", 10), bd=0)
        key.pack(padx=22, fill="x", ipady=8)
        key.insert(0, "")
        status = tk.StringVar()
        status_text = tk.Label(win, textvariable=status, bg=bg, fg=muted,
                               wraplength=490)
        status_text.pack(pady=9)
        def update_status(message=""):
            if message:
                status.set(message)
            else:
                status.set("Chave Groq configurada no Windows." if self.stt_settings.get_key()
                           else "Sem chave: funciona só com Whisper local, sem enviar áudio.")
        update_status()
        def save():
            try:
                self.stt_settings.set_mode(options[selected.get()])
                if key.get().strip():
                    self.stt_settings.set_key(key.get())
                    key.delete(0, "end")
                update_status("Preferências salvas. " + (
                    "Groq ativada para próximas falas." if self.stt_settings.get_key() and
                    self.stt_settings.mode != "local" else "Reconhecimento local ativo."))
            except Exception as exc:
                status.set("Não foi possível salvar: " + str(exc))
        def remove():
            try:
                self.stt_settings.set_key("")
                update_status("Chave removida do Windows. A voz continuará local.")
            except Exception as exc:
                status.set("Não foi possível remover a chave: " + str(exc))
        row = tk.Frame(win, bg=bg)
        row.pack(pady=4)
        tk.Button(row, text="Salvar preferências", command=save, bg=accent, fg="#0b111b",
                  bd=0, padx=14, pady=7).pack(side="left", padx=4)
        tk.Button(row, text="Remover chave", command=remove, bg=card, fg=fg,
                  bd=0, padx=10, pady=7).pack(side="left", padx=4)
        tk.Button(row, text="Fechar", command=win.destroy, bg=card, fg=fg,
                  bd=0, padx=10, pady=7).pack(side="left", padx=4)
        tk.Label(win, text="Chaves gratuitas: https://console.groq.com/keys",
                 bg=bg, fg=accent).pack(pady=9)

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

    def current(self, turn):
        return turn == self.turn_id and not self.stop.is_set()

    def interrupt_current(self, reason="Interrompido para ouvir voce."):
        self.turn_id += 1
        self.playback.interrupt()
        self.working = False
        self.add("Sistema", reason, "system")

    def clear_context(self):
        if messagebox.askyesno("Jarvis", "Apagar a memoria de conversa salva neste PC?"):
            self.brain.context.clear()
            self.add("Sistema", "Contexto apagado apenas deste computador.", "system")

    def handle(self, text):
        now = time.monotonic()
        if normalize(text) == self.last[0] and now - self.last[1] < 2:
            return
        self.last = (normalize(text), now)
        self.add("OUVIDO / TRANSCRICAO", text, "heard")
        action, q = self.gate.decide(text)
        self.refresh_state()
        if action == "ignore":
            return
        if action == "sleep":
            if self.working:
                self.interrupt_current("Jarvis silenciado.")
            else:
                self.add("Sistema", "Jarvis silenciado. Continuo ouvindo.", "system")
            return
        if self.working:
            if self.speaking.is_set():
                self.interrupt_current()
            else:
                self.add("Sistema", "Aguarde a resposta atual.", "system")
                return
        self.turn_id += 1
        turn = self.turn_id
        self.working = True
        if action == "wake":
            threading.Thread(target=self.answer_wake, args=(turn,), daemon=True).start()
        elif action == "ask":
            self.status.set("Pensando...")
            threading.Thread(target=self.answer, args=(q, turn), daemon=True).start()

    def test_voice(self):
        if self.working:
            self.interrupt_current("Teste de audio interrompido.")
        self.turn_id += 1
        turn = self.turn_id
        self.working = True
        sample = "Ola! Eu sou o Jarvis e esta e a minha saida de voz selecionada."
        self.queue.put(("reply", turn, sample))
        threading.Thread(target=self.speak_test, args=(sample, turn), daemon=True).start()

    def speak_test(self, message, turn):
        try:
            synthesize_and_play(message, self.playback, self.audio.device("output"),
                                lambda: self.current(turn))
        except Exception as exc:
            LOG.exception("Voice test failed")
            if self.current(turn):
                self.queue.put(("error", str(exc)))
        finally:
            self.queue.put(("done", turn))

    def answer_wake(self, turn):
        try:
            if self.current(turn):
                self.queue.put(("reply", turn, "Estou ouvindo."))
                synthesize_and_play("Estou ouvindo.", self.playback,
                                    self.audio.device("output"),
                                    lambda: self.current(turn))
        except Exception as exc:
            if self.current(turn):
                self.queue.put(("error", str(exc)))
        finally:
            self.queue.put(("done", turn))

    def answer(self, q, turn):
        try:
            a = self.brain.ask(q, lambda: self.current(turn))
            if a and self.current(turn) and self.gate.enabled:
                speech = re.sub(r"[#*_]+", "", a)[:850]
                self.queue.put(("reply", turn, speech))
                synthesize_and_play(speech, self.playback, self.audio.device("output"),
                                    lambda: self.current(turn))
        except Exception as exc:
            LOG.exception("Answer failed")
            if self.current(turn):
                self.queue.put(("error", str(exc)))
        finally:
            self.queue.put(("done", turn))

    def listen(self):
        from speech_engine import SpeechEngine
        SpeechEngine(ROOT / "models" / "whisper", self.audio, self.speaking,
                     self.stop, self.restart_mic, self.queue.put,
                     playback=self.playback, stt_settings=self.stt_settings).run()

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
                if self.current(data[0]):
                    self.add("JARVIS VAI FALAR", data[1], "reply")
            elif tag == "barge_in":
                if self.working:
                    self.interrupt_current()
            elif tag == "error":
                self.add("ERRO", data[0], "error")
                self.status.set("Erro: verifique o painel")
            elif tag == "stt_method":
                self.stt_status.set(data[0])
            elif tag == "ready":
                self.refresh_state()
            elif tag == "loading":
                self.status.set(data[0])
            elif tag == "done":
                if self.current(data[0]):
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
        self.turn_id += 1
        self.playback.interrupt()
        self.stop.set()
        self.restart_mic.set()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def run(self):
        threading.Thread(target=prepare_wake_voice, daemon=True, name="JarvisWakeVoice").start()
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
