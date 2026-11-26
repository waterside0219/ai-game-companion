import base64
import time
import http.client
import socket
from urllib.parse import urlsplit
import math
import urllib.request
from types import SimpleNamespace

import json
import os
import sys
from pathlib import Path
import random
import threading

from .prompts import BOARD_KEYS, BOARD_PROMPT, JEV_PROMPT


# 你们常说的名字（人名、宠物、游戏里的角色）写在 DESK_KEYTERMS 里，逗号隔开，听写会更容易听对。
KEYTERMS = tuple(term for term in os.environ.get("DESK_KEYTERMS", "").split(",") if term.strip())

class MockDescribe:
    def __init__(self):
        self._events = (
            {"game": "示例游戏", "event": "死亡", "importance": "高", "desc": "角色倒在地上"},
            {"game": "示例游戏", "event": "普通", "importance": "中", "desc": "角色站在路口"},
            {"game": "示例游戏", "event": "挂机", "importance": "低", "desc": "画面停在营地"},
            {"game": "示例游戏", "event": "胜利", "importance": "高", "desc": "画面显示挑战完成"},
        )
        self._index = 0
        self._lock = threading.Lock()
        self.calls = 0

    def describe(self, image, prompt):
        with self._lock:
            event = self._events[self._index % len(self._events)]
            self._index += 1
            self.calls += 1
        return json.dumps(event, ensure_ascii=False)


class MockBoard:
    def __init__(self):
        self.calls = 0

    def update_board(self, board, description):
        self.calls += 1
        return {
            "在哪": description["game"],
            "在做什么": description["event"],
            "刚发生了什么": description["desc"],
            "手里有什么": "画面未显示",
            "还差什么": "等待后续画面",
        }


class MockJev:
    def __init__(self, seed=None):
        self._random = random.Random(seed)
        self._lock = threading.Lock()
        self.calls = 0

    def score(self, context):
        with self._lock:
            self.calls += 1
            return self._random.random()


class MockProviders:
    def __init__(self, seed=None):
        self.describe = MockDescribe()
        self.update_board = MockBoard()
        self.jev = MockJev(seed)

    def transcribe(self, audio, deadline=None, clock=time):
        return "mock：这是按住侧键说的话"

    def tts(self, text):
        return Path(__file__).with_name("mock_silence.mp3").read_bytes()


class RealProviders:
    """Credentials are read only when explicitly selected and called in real mode."""

    def __init__(self, config):
        self.config = config
        # At most a primary + fallback transport, including DNS that outlives cancellation.
        self._transcribe_slot = threading.BoundedSemaphore(2)
        # Preserve Kimi's existing provider injection seams.
        self.describe = SimpleNamespace(describe=self._describe)
        self.update_board = SimpleNamespace(update_board=self._update_board)
        self.jev = SimpleNamespace(score=self._judge)

    def _post(self, url, payload, key, timeout=3):
        if not key:
            raise ValueError("provider credential is not configured")
        request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2200001)
        if len(raw) > 2200000:
            raise ValueError("provider response too large")
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("error"):
            raise ValueError("provider rejected request")
        return result

    def _chat(self, prompt, content):
        # Haiku 又贵又认字差（Roboflow OCR 61.6% vs Flash 79%，同一帧把安全屋认成走廊）；换 Gemini 2.5 Flash，约 1/4 价。
        result = self._post("https://openrouter.ai/api/v1/chat/completions", {
            "model": "google/gemini-2.5-flash", "max_tokens": 400,
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": content}],
        }, os.environ.get("OPENROUTER_API_KEY", ""))
        content = result["choices"][0]["message"]["content"].strip()
        # Models sometimes wrap JSON in a ```json fence; the parsers expect the bare object.
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else ""
            if content.rstrip().endswith("```"):
                content = content.rstrip()[:-3]
        return content.strip()

    def _describe(self, image, prompt):
        return self._chat(prompt, [{"type": "text", "text": "描述这一帧游戏画面。"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," +
                base64.b64encode(image).decode("ascii")}}])

    def _update_board(self, board, description):
        return json.loads(self._chat(BOARD_PROMPT, json.dumps(
            {"old_board": board, "description": description}, ensure_ascii=False)))

    def _judge(self, context):
        result = self._post("https://openrouter.ai/api/alpha/decisions", {
            "model": "typesafe/jev-1.13", "state": context,
            "questions": {"speak": {"type": "noul", "instructions": JEV_PROMPT,
                "criteria": {"true": "此刻有新事件，主动回应有陪伴价值。", "false": "无新意、打扰或尚应保持安静。"}}},
        }, os.environ.get("OPENROUTER_API_KEY", ""))
        answer = result["answers"]["speak"]
        value = answer["noul"]
        if (answer.get("type") != "noul" or isinstance(value, bool)
                or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1):
            raise ValueError("invalid decision probability")
        return float(value)

    def _post_deadline(self, url, payload, key, timeout):
        if not key:
            raise ValueError("provider credential is not configured")
        if timeout <= 0 or not self._transcribe_slot.acquire(blocking=False):
            raise TimeoutError("audio transport unavailable before deadline")
        end = time.monotonic() + timeout
        parsed = urlsplit(url)
        connection = (http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection)(
            parsed.hostname, parsed.port, timeout=timeout)
        connection.auto_open = 0  # cancellation must never trigger an implicit reconnect
        done, cancelled = threading.Event(), threading.Event()
        result, active_socket = [], []

        def run():
            try:
                connection.connect()
                active_socket.append(connection.sock)
                # DNS itself is OS-owned. A late DNS/connect never sends a late provider request.
                if cancelled.is_set():
                    return
                connection.request("POST", parsed.path or "/", json.dumps(payload, ensure_ascii=False).encode(),
                                   {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
                with connection.getresponse() as response:
                    raw = response.read(2200001)
                    if len(raw) > 2200000 or response.status != 200:
                        raise ValueError("provider rejected request")
                value = json.loads(raw)
                if not isinstance(value, dict) or value.get("error"):
                    raise ValueError("provider rejected request")
                result.append(value)
            except Exception as error:
                result.append(error)
            finally:
                connection.close()
                self._transcribe_slot.release()
                done.set()

        threading.Thread(target=run, daemon=True).start()
        if not done.wait(max(0, end - time.monotonic())):
            cancelled.set()
            # shutdown interrupts a socket even while a peer keeps trickling response bytes.
            active = active_socket[0] if active_socket else connection.sock
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
            raise TimeoutError("audio request deadline")
        if not result:
            raise TimeoutError("audio request cancelled")
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

    def transcribe(self, audio, deadline=None, clock=time):
        if not self.config.transcribe_enabled:
            raise PermissionError("real transcription is disabled")
        deadline = deadline if deadline is not None else clock.time() + 16
        # ElevenLabs Scribe first (Gemini looped "我跟李忠"×200 on a real voice); Gemini stays as fallback.
        try:
            text = self._scribe(audio, timeout=min(6, deadline - clock.time()))
            # Scribe dropped a real 2s "厉害吧" as no-speech; an empty result now falls through to Gemini.
            if text and clock.time() < deadline:
                return text
        except Exception as exc:
            print(f"scribe failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        encoded = base64.b64encode(audio).decode("ascii")
        for model in ("google/gemini-2.5-flash-lite", "google/gemini-2.5-flash"):
            remaining = deadline - clock.time()
            if remaining <= 0:
                raise TimeoutError("voice expired")
            try:
                result = self._post_deadline("https://openrouter.ai/api/v1/chat/completions", {
                    "model": model, "max_tokens": 1000, "temperature": 0,
                    "messages": [{"role": "system", "content": "只转写音频中的人声原话，不回答、不解释。没有清晰人声就输出空字符串。不要输出时间戳。"},
                                 {"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": encoded, "format": "wav"}}]}],
                }, os.environ.get("OPENROUTER_API_KEY", ""), timeout=min(8, remaining))
                if clock.time() >= deadline:
                    raise TimeoutError("voice expired")
                text = result["choices"][0]["message"]["content"]
                if not isinstance(text, str):
                    raise ValueError("invalid transcription")
                return text.strip()
            except Exception:
                if model == "google/gemini-2.5-flash" or clock.time() >= deadline:
                    raise
        raise TimeoutError("voice expired")

    def _scribe(self, audio, timeout):
        if timeout <= 0:
            raise TimeoutError("voice expired")
        key = (Path(__file__).resolve().parents[1] / ".private" / "elevenlabs-stt-key").read_text(encoding="utf-8").strip()
        boundary = "desk" + os.urandom(8).hex()
        field = lambda name, value: (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
        body = (field("model_id", "scribe_v2") + field("tag_audio_events", "true")
                + b"".join(field("keyterms", term) for term in KEYTERMS)
                + f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
                + audio + f"\r\n--{boundary}--\r\n".encode())
        request = urllib.request.Request("https://api.elevenlabs.io/v1/speech-to-text", data=body, method="POST",
            headers={"xi-api-key": key, "Content-Type": "multipart/form-data; boundary=" + boundary})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read(65536))
        words = result.get("words") or []
        print(f"scribe: {len(words)} tokens, types={sorted({w.get('type') for w in words if isinstance(w, dict)})}", file=sys.stderr, flush=True)
        if not any(isinstance(w, dict) and w.get("type") == "word" for w in words):
            return ""
        text = result.get("text")
        if not isinstance(text, str):
            raise ValueError("invalid transcription")
        return text.strip()

    def tts(self, text):
        if not self.config.minimax_key_path or not self.config.voice_id:
            raise ValueError("MiniMax key path and voice_id must be configured")
        key = Path(self.config.minimax_key_path).read_text(encoding="utf-8").strip()
        result = self._post("https://api.minimaxi.com/v1/t2a_v2", {
            "model": "speech-02-hd", "text": text, "stream": False,
            "voice_setting": {"voice_id": self.config.voice_id},
            "audio_setting": {"format": "mp3", "sample_rate": 24000, "bitrate": 64000, "channel": 1},
            "output_format": "hex",
        }, key, timeout=15)
        if result.get("base_resp", {}).get("status_code") != 0:
            raise ValueError("speech synthesis failed")
        return bytes.fromhex(result["data"]["audio"])


def load_providers(config):
    mode = os.environ.get("DESK_PROVIDERS", "mock")
    if mode == "mock":
        return MockProviders(seed=7)
    if mode == "real":
        return RealProviders(config)
    raise ValueError("DESK_PROVIDERS must be mock or real")
