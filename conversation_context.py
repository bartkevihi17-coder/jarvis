"""Private, bounded conversation context for the local Qwen Jarvis.

Stores only dialogue text in %LOCALAPPDATA%/Jarvis/context.json.
No sync, network or corporate dependencies.
"""
import json
import logging
import threading
from collections import deque
from pathlib import Path

LOG = logging.getLogger("jarvis")


class ConversationContext:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.messages = deque(maxlen=32)
        self._load()

    def _load(self):
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(doc, list):
                raise ValueError("Expected list")
            good = []
            for item in doc[-32:]:
                if (isinstance(item, dict) and
                        item.get("role") in ("user", "assistant") and
                        isinstance(item.get("content"), str)):
                    good.append({"role": item["role"], "content": item["content"][:1100]})
            self.messages.extend(good)
            LOG.info("CONTEXT loaded messages=%d", len(self.messages))
        except FileNotFoundError:
            pass
        except Exception:
            LOG.exception("CONTEXT could not load previous dialogue")

    def snapshot(self, max_messages=12, max_characters=2600):
        with self.lock:
            chosen = []
            chars = 0
            for msg in reversed(self.messages):
                length = len(msg["content"])
                if chosen and (len(chosen) >= max_messages or chars + length > max_characters):
                    break
                chosen.append(dict(msg))
                chars += length
            return list(reversed(chosen))

    def remember(self, question, answer):
        with self.lock:
            self.messages.append({"role": "user", "content": question[:700]})
            self.messages.append({"role": "assistant", "content": answer[:1100]})
            self._write()

    def clear(self):
        with self.lock:
            self.messages.clear()
            self._write()

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_suffix(".pending")
        try:
            staging.write_text(json.dumps(list(self.messages), ensure_ascii=False, indent=2),
                               encoding="utf-8")
            staging.replace(self.path)
        finally:
            staging.unlink(missing_ok=True)
