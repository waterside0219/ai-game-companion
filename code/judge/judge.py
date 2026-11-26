import argparse
import io
import wave
import json
import hashlib
import hmac
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .prompts import BOARD_KEYS, DESCRIBE_PROMPT, JEV_PROMPT
from .providers import load_providers

EVENTS = {"死亡", "胜利", "boss", "菜单", "过场", "挂机", "普通"}
IMPORTANCE = {"高", "中", "低"}
TALK_LEVELS = {"quiet", "normal", "chatty"}
# the AI should actually see the game; 0.70 blocked every ordinary frame (scores ~0.35-0.46).
THRESHOLDS = {"quiet": 0.60, "normal": 0.33, "chatty": 0.20}
DHASH_RE = re.compile(r"[0-9a-fA-F]{16}")


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8833
    entry_url: str = "http://127.0.0.1:8798/desk/event"
    log_path: str = ".private/judge.jsonl"
    device_token: str = ""
    minimax_key_path: str = ""
    voice_id: str = ""
    direct_tmpfs: str = ""
    transcribe_enabled: bool = False
    competitive_games: list[str] = field(default_factory=list)


def load_config(path=None):
    if path is None:
        return Config()
    import tomllib

    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    allowed = set(Config.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError("unknown config fields")
    config = Config(**raw)
    if config.host != "127.0.0.1":
        raise ValueError("judge must bind 127.0.0.1")
    if not 1 <= config.port <= 65535:
        raise ValueError("invalid port")
    if not all(isinstance(game, str) for game in config.competitive_games):
        raise ValueError("invalid competitive_games")
    return config


class JsonlLogger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def log(self, record):
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._handle.write(line + "\n")

    def close(self):
        with self._lock:
            self._handle.close()


class HttpEventSender:
    def __init__(self, url, timeout=2.0):
        self.url = url
        self.timeout = timeout

    def send(self, event, timeout=None):
        request = urllib.request.Request(
            self.url,
            data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout if timeout is None else min(self.timeout, timeout)) as response:
                result = json.loads(response.read(65536))
        except urllib.error.HTTPError as error:
            try:
                result = json.loads(error.read(65536))
            except (ValueError, OSError):
                return {"ok": False}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return {"ok": False}
        return result if isinstance(result, dict) else {"ok": False}


class TmpfsUnavailable(RuntimeError):
    pass


def _unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _is_tmpfs(path):
    if sys.platform == "darwin" or not sys.platform.startswith("linux"):
        return False
    target = os.path.realpath(path)
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            mounts = []
            for line in handle:
                left, right = line.rstrip("\n").split(" - ", 1)
                mount_point = _unescape_mount(left.split()[4])
                fs_type = right.split()[0]
                mounts.append((mount_point, fs_type))
    except (OSError, ValueError, IndexError):
        return False
    matches = [
        mount for mount in mounts
        if target == mount[0] or target.startswith(mount[0].rstrip("/") + "/")
    ]
    return bool(matches) and max(matches, key=lambda item: len(item[0]))[1] == "tmpfs"


class TmpfsImageStore:
    def __init__(self, directory, ttl=600):
        if not directory or not os.path.isdir(directory) or not _is_tmpfs(directory):
            raise TmpfsUnavailable("direct requires an explicitly configured tmpfs directory")
        self.directory = os.path.realpath(directory)
        self.ttl = ttl
        self._lock = threading.Lock()
        self._timers = {}
        self._closed = False
        # A process restart must not leave earlier direct captures behind indefinitely.
        for entry in os.scandir(self.directory):
            if entry.is_file(follow_symlinks=False) and re.fullmatch(r"[0-9a-f]{32}\.jpg", entry.name):
                remaining = min(self.ttl, self.ttl - (time.time() - entry.stat().st_mtime))
                if remaining <= 0:
                    self.discard(entry.path)
                else:
                    self._schedule(entry.path, remaining)

    def put(self, image):
        name = uuid.uuid4().hex + ".jpg"
        path = os.path.join(self.directory, name)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(image)
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        self._schedule(path, self.ttl)
        return path

    def _schedule(self, path, delay):
        timer = threading.Timer(delay, self.discard, args=(path,))
        timer.daemon = True
        with self._lock:
            if self._closed:
                timer.cancel()
                os.unlink(path)
                raise TmpfsUnavailable("image store is closed")
            self._timers[path] = timer
        timer.start()

    def discard(self, path):
        with self._lock:
            timer = self._timers.pop(path, None)
        if timer is not None:
            timer.cancel()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def close(self):
        with self._lock:
            self._closed = True
            paths = list(self._timers)
        for path in paths:
            self.discard(path)


class Judge:
    def __init__(self, providers=None, sender=None, image_store=None, logger=None,
                 config=None, clock=None, idle_checker=None):
        self.config = config or Config()
        self.providers = providers or load_providers(self.config)
        self.sender = sender or HttpEventSender(self.config.entry_url)
        self.image_store = image_store
        if self.image_store is None and self.config.direct_tmpfs:
            self.image_store = TmpfsImageStore(self.config.direct_tmpfs)
        self.logger = logger or JsonlLogger(self.config.log_path)
        self.clock = clock or time
        self.idle_checker = idle_checker
        self._ai_status = None
        self.board = {key: "" for key in BOARD_KEYS}
        self._heartbeat = None
        self._auto_times = deque()
        self._last_auto_hash = None
        self._last_auto_time = 0.0
        self._direct_times = deque()
        self._spoken_times = deque()
        self._last_spoken = None
        self._voice_times = deque()
        self._voice_lock = threading.Lock()
        self._voice_upload_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._replies = deque()
        self._reply_id = 0
        self._reply_condition = threading.Condition()
        self._tts_lock = threading.Lock()
        self._closed = False
        self._expiry_thread = threading.Thread(target=self._expire_replies, daemon=True)
        self._expiry_thread.start()

    def heartbeat(self, data):
        required = {"ts", "paused", "talk_level", "fg_exe", "fg_path", "is_game"}
        if not isinstance(data, dict) or set(data) != required:
            raise ValueError("invalid heartbeat fields")
        if not _number(data["ts"]) or not isinstance(data["paused"], bool):
            raise ValueError("invalid heartbeat")
        if data["talk_level"] not in TALK_LEVELS or not isinstance(data["is_game"], bool):
            raise ValueError("invalid heartbeat")
        if not all(isinstance(data[key], str) for key in ("fg_exe", "fg_path")):
            raise ValueError("invalid heartbeat")
        with self._state_lock:
            previous = self._heartbeat
            self._heartbeat = dict(data, received=self.clock.time())
        # auto frames never came; log only when what Win sees changes, to tell paused/not-game apart.
        if previous is None or any(previous.get(k) != data[k] for k in ("fg_exe", "is_game", "paused")):
            print(f"heartbeat change: exe={data['fg_exe']!r} game={data['is_game']} paused={data['paused']} path={data['fg_path']!r}",
                  file=sys.stderr, flush=True)

    def status(self, data):
        if not isinstance(data, dict) or set(data) != {"idle"} or not isinstance(data["idle"], bool):
            raise ValueError("status requires a boolean idle")
        with self._state_lock:
            self._ai_status = {"idle": data["idle"], "received": self.clock.time()}

    def post_reply(self, data):
        if not isinstance(data, dict) or not {"text"} <= set(data) <= {"event_id", "text"}:
            raise ValueError("reply requires text and optional event_id")
        if data.get("event_id") is not None and not isinstance(data["event_id"], str):
            raise ValueError("invalid event_id")
        if not isinstance(data["text"], str) or not data["text"].strip() or len(data["text"]) > 4000:
            raise ValueError("invalid reply text")
        with self._tts_lock:
            try:
                audio = self.providers.tts(data["text"])
                # ponytail: 20 x 1 MiB keeps the small VPS bounded; larger speech is subtitle-only.
                if not isinstance(audio, bytes) or not 0 < len(audio) <= 1024 * 1024:
                    audio = None
            except Exception:
                audio = None
            with self._reply_condition:
                self._prune_replies()
                self._reply_id += 1
                reply = {"id": self._reply_id, "event_id": data.get("event_id"),
                         "text": data["text"], "audio": audio, "expires": self.clock.time() + 600}
                self._replies.append(reply)
                while len(self._replies) > 20:
                    self._replies.popleft()
                self._reply_condition.notify_all()
                return {key: reply[key] for key in ("id", "event_id", "text")}

    def _prune_replies(self):
        while self._replies and self._replies[0]["expires"] <= self.clock.time():
            self._replies.popleft()

    def _expire_replies(self):
        with self._reply_condition:
            while not self._closed:
                self._prune_replies()
                wait = max(0.01, self._replies[0]["expires"] - self.clock.time()) if self._replies else None
                self._reply_condition.wait(wait)

    def say_after(self, after, wait=25):
        return [{"id": reply["id"], "text": reply["text"],
                 "audio_url": f'/v1/audio/{reply["id"]}.mp3' if reply["audio"] else None}
                for reply in self._wait_replies(after, wait)]

    def _wait_replies(self, after, wait):
        deadline = time.monotonic() + wait
        with self._reply_condition:
            if after > self._reply_id:  # Client reconnects after a service restart.
                after = 0
            while not self._closed:
                self._prune_replies()
                replies = [dict(reply) for reply in self._replies if reply["id"] > after]
                if replies or wait <= 0:
                    return replies
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._reply_condition.wait(remaining)
            return []

    def audio(self, reply_id):
        with self._reply_condition:
            self._prune_replies()
            return next((reply["audio"] for reply in self._replies if reply["id"] == reply_id), None)

    def process_voice(self, audio, meta):
        _validate_voice(audio, meta)
        if not self._voice_lock.acquire(blocking=False):
            return {"sent": False, "gate": "busy"}
        try:
            now = self.clock.time()
            event_id = uuid.uuid4().hex
            # side-key words expired while the AI was mid-task (pane busy >20s); late beats lost.
            deadline = meta["ts"] + 180
            err = [None]

            def finish(gate):
                # Never persist microphone bytes or transcripts in the judge log.
                self.logger.log({"time": self.clock.time(), "event_id": event_id, "kind": "voice", "gate": gate, "sent": gate == "sent",
                                 "age_at_arrival": round(now - meta["ts"], 2), "ms": meta.get("ms"), "bytes": len(audio), "err": err[0]})
                return {"event_id": event_id, "sent": gate == "sent", "gate": gate}

            # a PC clock running a hair ahead (age -0.01s) got words thrown away; allow small skew.
            if not -5 <= now - meta["ts"] < 20:
                return finish("expired")
            while self._voice_times and now - self._voice_times[0] >= 3600:
                self._voice_times.popleft()
            if len(self._voice_times) >= 60:
                return finish("voice_hour")
            try:
                text = self.providers.transcribe(audio, deadline=deadline, clock=self.clock)
            except PermissionError:
                return finish("disabled")
            except Exception as exc:
                err[0] = type(exc).__name__ + ": " + str(exc)[:160]
                return finish("expired" if self.clock.time() >= deadline else "transcribe")
            if self.clock.time() >= deadline:
                return finish("expired")
            if not isinstance(text, str) or not any(char.isalpha() for char in text):
                return finish("noise")
            text = " ".join(text.split())[:2000]
            event = {"schema_version": 1, "kind": "desk.voice", "event_id": event_id,
                     "ts": meta["ts"], "text": text, "importance": "高"}
            while self.clock.time() < deadline:
                remaining = deadline - self.clock.time()
                response = (self.sender.send(event, timeout=remaining) if isinstance(self.sender, HttpEventSender)
                            else self.sender.send(event))
                if response.get("ok") is True:
                    self._voice_times.append(self.clock.time())
                    return finish("sent")
                if response.get("expired") is True:
                    return finish("expired")
                if response.get("busy") is not True:
                    return finish("send")
                # words were thrown away after 20s while the AI was mid-work. Queue them instead (text only,
                # no audio kept) and tell Win it's delivered, so nobody has to repeat themselves.
                self._voice_times.append(self.clock.time())
                threading.Thread(target=self._deliver_voice_later, args=(dict(event), finish), daemon=True).start()
                finish("queued")
                return {"event_id": event_id, "sent": True, "gate": "sent"}
            return finish("expired")
        finally:
            self._voice_lock.release()

    def _deliver_voice_later(self, event, finish, window=600):
        deadline = self.clock.time() + window
        while self.clock.time() < deadline:
            self.clock.sleep(2)
            # The entry only accepts fresh voice events, so each retry carries the current time.
            event["ts"] = self.clock.time()
            try:
                response = self.sender.send(event)
            except Exception:
                response = {}
            if response.get("ok") is True:
                finish("sent_late")
                return
            if response.get("busy") is not True and response.get("expired") is not True:
                break
        finish("busy_late")

    def process_frame(self, image, meta):
        meta = _validate_frame(image, meta)
        self._keep_latest(image)
        with self._frame_lock:
            return self._process_valid_frame(image, meta)

    def _keep_latest(self, image):
        # the AI may look at the screen whenever it likes, not only when a frame clears the gates.
        if not self.config.direct_tmpfs:
            return
        try:
            target = os.path.join(self.config.direct_tmpfs, "latest.jpg")
            tmp = target + ".tmp"
            with open(tmp, "wb") as handle:
                handle.write(image)
            os.replace(tmp, target)
        except OSError:
            pass

    def _process_valid_frame(self, image, meta):
        now = self.clock.time()
        event_id = uuid.uuid4().hex
        record = {"time": now, "event_id": event_id, "kind": meta["kind"],
                  "gate": "", "desc": None, "score": None, "sent": False}
        result = {"event_id": event_id, "sent": False}

        def finish(gate, **extra):
            record["gate"] = gate
            record.update(extra)
            self.logger.log(record)
            result.update(extra)
            result["gate"] = gate
            result["sent"] = bool(record["sent"])
            return result

        if meta["kind"] == "auto":
            with self._state_lock:
                heartbeat = dict(self._heartbeat) if self._heartbeat else None
            if (heartbeat is None or now - heartbeat["received"] >= 15
                    or heartbeat["paused"] or not heartbeat["is_game"] or not meta["is_game"]):
                return finish("heartbeat")

        with self._state_lock:
            if meta["kind"] == "auto":
                while self._auto_times and now - self._auto_times[0] >= 3600:
                    self._auto_times.popleft()
                # the 60/hour budget was gone in 15 min on menu/cursor wiggles, then 1500 frames
                # (incl. the Kasumi scene change) were dropped unseen. Spend describes only on real scene changes.
                distance = 64 if self._last_auto_hash is None else _hash_distance(meta["dhash"], self._last_auto_hash)
                if distance <= SAME_SCENE_BITS and now - self._last_auto_time < SAME_SCENE_REFRESH:
                    return finish("same_scene")
                minute = sum(now - stamp < 60 for stamp in self._auto_times)
                if minute >= 4:
                    return finish("rate_minute")
                if len(self._auto_times) >= 60:
                    return finish("rate_hour")
                # Keep the last quarter of the hour's budget for big changes only (new place, battle, cutscene).
                if len(self._auto_times) >= 45 and distance < BIG_SCENE_BITS:
                    return finish("rate_hour")
                self._auto_times.append(now)
                self._last_auto_hash = meta["dhash"]
                self._last_auto_time = now
            else:
                while self._direct_times and now - self._direct_times[0] >= 60:
                    self._direct_times.popleft()
                if len(self._direct_times) >= 12:
                    return finish("rate_minute")
                self._direct_times.append(now)

        if meta.get("capture_black", False):
            return finish("capture_black")

        try:
            raw = self.providers.describe.describe(image, DESCRIBE_PROMPT)
            description = _parse_description(raw)
        except Exception as exc:
            if meta["kind"] != "direct":
                return finish("describe", error=type(exc).__name__)
            # the player pressed to make the AI look; a blank from the describer must not eat that frame.
            record["error"] = type(exc).__name__
            description = {"game": meta["fg_exe"] or "unknown", "event": "普通",
                           "importance": "高", "desc": "识图没认出来，直接看原图"}

        try:
            if record.get("error"):
                with self._state_lock:
                    board = dict(self.board)
            else:
                board = self.providers.update_board.update_board(self.board, description)
            if not isinstance(board, dict) or set(board) != set(BOARD_KEYS):
                raise ValueError("invalid board")
            if not all(isinstance(board[key], str) for key in BOARD_KEYS):
                raise ValueError("invalid board")
            with self._state_lock:
                self.board = dict(board)
                board = dict(self.board)
        except Exception:
            return finish("board", desc=description["desc"])

        record["desc"] = description["desc"]
        result["desc"] = description["desc"]
        kind = meta["kind"]
        if kind == "auto":
            importance = description["importance"]
            if importance == "低":
                with self._state_lock:
                    talk_level = self._heartbeat["talk_level"]
                if talk_level != "chatty" or not self._idle():
                    return finish("low")
            if importance != "高":
                with self._state_lock:
                    talk_level = self._heartbeat["talk_level"]
                    last_spoken = self._last_spoken
                minutes = None if last_spoken is None else max(0, (now - last_spoken) / 60)
                context = {"question": JEV_PROMPT, "desc": description["desc"],
                           "board": board, "minutes_since_last_spoken": minutes,
                           "talk_level": talk_level}
                try:
                    score = float(self.providers.jev.score(context))
                except Exception:
                    return finish("jev", score=None)
                record["score"] = score
                result["score"] = score
                if not math.isfinite(score) or score < THRESHOLDS[talk_level]:
                    return finish("jev", score=score)

        now = self.clock.time()
        # a frame the player asked the AI to look at keeps 180s, not the auto 6s shelf life.
        shelf = 180 if meta["kind"] == "direct" else 6
        if not -5 <= now - meta["ts"] <= shelf:
            return finish("expired")

        if kind == "auto":
            with self._state_lock:
                talk_level = self._heartbeat["talk_level"]
                while self._spoken_times and now - self._spoken_times[0] >= 3600:
                    self._spoken_times.popleft()
                if description["importance"] != "高" and self._last_spoken is not None:
                    # 5 minutes of silence felt like the AI was always behind.
                    base = 20 if description["game"].casefold() in {
                        game.casefold() for game in self.config.competitive_games
                    } else 40
                    multiplier = {"quiet": 1.5, "normal": 1.0, "chatty": 0.6}[talk_level]
                    if now - self._last_spoken < base * multiplier:
                        return finish("cooldown")
                if len(self._spoken_times) >= 60:
                    return finish("active_hour")

        event = {"schema_version": 1,
                 "kind": "desk.direct" if kind == "direct" else "desk.event",
                 "event_id": event_id, "ts": meta["ts"], **description, "board": board}
        stored_path = None
        if kind == "direct":
            if self.image_store is None:
                return finish("direct_store")
            try:
                stored_path = self.image_store.put(image)
                event["image_path"] = stored_path
            except (OSError, TmpfsUnavailable):
                return finish("direct_store")
        elif self.image_store is not None:
            # show the AI the actual board on auto frames too, not just a one-line description.
            try:
                stored_path = self.image_store.put(image)
                event["image_path"] = stored_path
            except (OSError, TmpfsUnavailable):
                stored_path = None

        while True:
            if not -5 <= self.clock.time() - meta["ts"] <= shelf:
                if stored_path is not None:
                    self.image_store.discard(stored_path)
                return finish("expired", score=record["score"])
            # Consume the idle observation before dispatch; a later status can mark idle again.
            with self._state_lock:
                self._ai_status = None
            response = self.sender.send(event)
            if response.get("ok") is True:
                if kind == "auto":
                    with self._state_lock:
                        self._spoken_times.append(now)
                        self._last_spoken = now
                return finish("sent", sent=True, score=record["score"])
            if response.get("busy") is True and kind == "direct":
                # the player pressed the key to make the AI look; never drop it because the AI is mid-work. Queue it
                # in the background (image stays in tmpfs) and answer Win now so its upload doesn't time out.
                threading.Thread(target=self._deliver_direct_later, args=(event, stored_path, dict(record)),
                                 daemon=True).start()
                return finish("queued", score=record["score"])
            if not (response.get("busy") is True and description["importance"] == "高"):
                if stored_path is not None:
                    self.image_store.discard(stored_path)
                return finish("busy" if response.get("busy") is True else "send",
                              score=record["score"])
            self.clock.sleep(0.2)

    def _deliver_direct_later(self, event, stored_path, record, window=180):
        deadline = self.clock.time() + window
        while self.clock.time() < deadline:
            self.clock.sleep(2)
            try:
                response = self.sender.send(event)
            except Exception:
                response = {}
            if response.get("ok") is True:
                self.logger.log(dict(record, gate="sent_late", sent=True))
                return
            if response.get("busy") is not True:
                break
        if stored_path is not None:
            self.image_store.discard(stored_path)
        self.logger.log(dict(record, gate="busy_late", sent=False))

    def _idle(self):
        if self.idle_checker is not None:
            try:
                return bool(self.idle_checker())
            except Exception:
                return False
        with self._state_lock:
            status = self._ai_status
            return bool(status and status["idle"] and 0 <= self.clock.time() - status["received"] < 15)

    def close(self):
        with self._reply_condition:
            self._closed = True
            self._replies.clear()
            self._reply_condition.notify_all()
        self._expiry_thread.join(timeout=1)
        close = getattr(self.image_store, "close", None)
        if close:
            close()
        close = getattr(self.logger, "close", None)
        if close:
            close()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _parse_description(raw):
    if not isinstance(raw, str) or "\n" in raw or "\r" in raw:
        raise ValueError("description must be one line")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"game", "event", "importance", "desc"}:
        raise ValueError("invalid description fields")
    if value["event"] not in EVENTS or value["importance"] not in IMPORTANCE:
        raise ValueError("invalid description values")
    if not isinstance(value["game"], str) or not value["game"].strip() or len(value["game"]) > 80:
        raise ValueError("invalid game")
    if (not isinstance(value["desc"], str) or not value["desc"].strip()
            or "\n" in value["desc"] or "\r" in value["desc"]):
        raise ValueError("invalid desc")
    # Models often overrun the 40-char budget; trim instead of dropping the whole frame.
    value["desc"] = value["desc"].strip()[:40]
    return value


SAME_SCENE_BITS = 12
SAME_SCENE_REFRESH = 180
BIG_SCENE_BITS = 20


def _hash_distance(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _validate_frame(image, meta):
    if not isinstance(image, bytes) or not image.startswith(b"\xff\xd8") or b"\xff\xd9" not in image:
        raise ValueError("image must be JPEG")
    # a frame the player pressed for is sent full-size so the AI can read small numbers; auto stays small.
    if len(image) > (2 * 1024 * 1024 if isinstance(meta, dict) and meta.get("kind") == "direct" else 120 * 1024):
        raise ValueError("image too large")
    required = {"ts", "kind", "fg_exe", "is_game", "dhash"}
    if not isinstance(meta, dict) or not required <= set(meta) <= required | {"capture_black"}:
        raise ValueError("invalid frame fields")
    if not isinstance(meta.get("capture_black", False), bool):
        raise ValueError("invalid capture_black")
    if not _number(meta["ts"]) or meta["kind"] not in {"auto", "direct"}:
        raise ValueError("invalid frame")
    if not isinstance(meta["fg_exe"], str) or not isinstance(meta["is_game"], bool):
        raise ValueError("invalid frame")
    if not isinstance(meta["dhash"], str) or not DHASH_RE.fullmatch(meta["dhash"]):
        raise ValueError("invalid dhash")
    return dict(meta)


def _validate_voice(audio, meta):
    if not isinstance(audio, bytes) or not 44 < len(audio) <= 1024 * 1024:
        raise ValueError("invalid audio size")
    if (not isinstance(meta, dict) or set(meta) != {"ts", "ms"}
            or not _number(meta["ts"]) or type(meta["ms"]) is not int or not 0 < meta["ms"] <= 30000):
        raise ValueError("invalid voice meta")
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            frames = wav.getnframes()
            if (wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000
                    or wav.getcomptype() != "NONE" or not 0 < frames <= 480000
                    or abs(frames / 16 - meta["ms"]) > 2 or len(wav.readframes(frames)) != frames * 2):
                raise ValueError("expected <=30s PCM16 mono 16k WAV")
    except (wave.Error, EOFError) as error:
        raise ValueError("invalid WAV") from error


def _parse_multipart(content_type, body, field="image"):
    if field == "audio":
        # Parse only the small MIME headers; decoding a 1MB binary body as email text
        # leaves large transient Unicode allocations in the small VPS process.
        if len(content_type) > 200:
            raise ValueError("invalid multipart type")
        header = BytesParser(policy=policy.default).parsebytes(b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n")
        boundary = header.get_boundary()
        if not boundary or len(boundary) > 70:
            raise ValueError("invalid boundary")
        delimiter = b"--" + boundary.encode("ascii")
        parts = body.split(b"\r\n" + delimiter)
        if len(parts) != 3 or not parts[0].startswith(delimiter) or parts[-1] not in (b"--", b"--\r\n"):
            raise ValueError("voice requires exactly two fields")
        parts[0] = parts[0][len(delimiter):]
        fields = {}
        for part in parts[:2]:
            if not part.startswith(b"\r\n"):
                raise ValueError("invalid multipart separator")
            headers, data = part[2:].split(b"\r\n\r\n", 1)
            if len(headers) > 4096:
                raise ValueError("multipart headers too large")
            item = BytesParser(policy=policy.default).parsebytes(headers + b"\r\n\r\n")
            name = item.get_param("name", header="content-disposition")
            if item.get_content_disposition() != "form-data" or name not in {"audio", "meta"} or name in fields:
                raise ValueError("invalid voice fields")
            fields[name] = data
        if len(fields["meta"]) > 4096:
            raise ValueError("metadata too large")
        return fields["audio"], json.loads(fields["meta"])
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    if not message.is_multipart():
        raise ValueError("expected multipart")
    fields = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if field == "audio" and (name not in {"audio", "meta"} or name in fields):
            raise ValueError("invalid voice multipart fields")
        if name in {field, "meta"} and name not in fields:
            fields[name] = part.get_payload(decode=True)
    if set(fields) != {field, "meta"}:
        raise ValueError("multipart requires image and meta")
    try:
        meta = json.loads(fields["meta"])
    except (ValueError, UnicodeDecodeError, TypeError) as error:
        raise ValueError("invalid meta JSON") from error
    return fields[field], meta


SAVE_DIR = Path(__file__).resolve().parent.parent / "saves"


def store_save(data, meta, root=SAVE_DIR, keep=30):
    # read-only game save mirror (P5R first). Stored as-is; parsing happens on another machine (see saves/).
    game = str(meta.get("game", ""))
    if not re.fullmatch(r"[a-z0-9_-]{1,20}", game):
        raise ValueError("invalid game")
    slot = re.sub(r"[^A-Za-z0-9_-]", "", str(meta.get("slot", "")))[:20] or "slot"
    digest = hashlib.sha256(data).hexdigest()[:16]
    folder = root / game
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    same = [p.name for p in folder.glob(f"*-{digest}.DAT")]
    name = same[0] if same else f"{stamp}-{slot}-{digest}.DAT"
    if not same:
        (folder / name).write_bytes(data)
    tmp = folder / "latest.json.tmp"
    tmp.write_text(json.dumps({"file": name, "slot": slot, "mtime": meta.get("mtime"), "received": time.time()}), encoding="utf-8")
    os.replace(tmp, folder / "latest.json")
    old = sorted(folder.glob("*.DAT"))[:-keep]
    for p in old:
        p.unlink(missing_ok=True)
    return {"stored": True, "slot": slot}


def make_handler(judge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DeskJudge/1"

        def _json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            expected = judge.config.device_token
            supplied = self.headers.get("Authorization", "")
            valid = hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + expected).encode("utf-8"))
            if not expected or not valid:
                self._json(401, {"ok": False, "error": "unauthorized"})
                return False
            return True

        def _local_reply(self):
            # cloudflared connects from loopback too: reject its forwarded requests.
            forwarded = ("Forwarded", "X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP")
            return (self.client_address[0] == "127.0.0.1"
                    and self.headers.get("Host", "").split(":")[0] == "127.0.0.1"
                    and not any(name in self.headers for name in forwarded))

        def _body(self, limit):
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise ValueError("invalid content length")
            if length < 0 or length > limit:
                raise ValueError("invalid content length")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("incomplete body")
            return body

        def do_POST(self):
            if self.path in {"/v1/reply", "/v1/status"}:
                if not self._local_reply():
                    self._json(403, {"ok": False, "error": "local replies only"})
                    return
            elif not self._authorized():
                return
            try:
                if self.path == "/v1/heartbeat":
                    data = json.loads(self._body(65536))
                    judge.heartbeat(data)
                    self._json(200, {"ok": True})
                elif self.path == "/v1/status":
                    judge.status(json.loads(self._body(1024)))
                    self._json(200, {"ok": True})
                elif self.path == "/v1/reply":
                    data = json.loads(self._body(65536))
                    self._json(200, {"ok": True, "reply": judge.post_reply(data)})
                elif self.path == "/v1/voice":
                    content_type = self.headers.get("Content-Type", "")
                    if not content_type.lower().startswith("multipart/form-data"):
                        self._json(415, {"ok": False, "error": "expected multipart"})
                        return
                    if int(self.headers.get("Content-Length", "0")) > 1024 * 1024 + 8192:
                        self._json(413, {"ok": False, "error": "audio too large"})
                        return
                    # Before reading: at most one bounded voice body/ASR can occupy memory.
                    if not judge._voice_upload_lock.acquire(blocking=False):
                        self._json(200, {"ok": True, "sent": False, "gate": "busy"})
                        return
                    try:
                        self.connection.settimeout(5)
                        audio, meta = _parse_multipart(content_type, self._body(1024 * 1024 + 8192), "audio")
                        if len(audio) > 1024 * 1024:
                            self._json(413, {"ok": False, "error": "audio too large"})
                            return
                        self._json(200, {"ok": True, **judge.process_voice(audio, meta)})
                    finally:
                        judge._voice_upload_lock.release()
                elif self.path == "/v1/save":
                    content_type = self.headers.get("Content-Type", "")
                    if not content_type.lower().startswith("multipart/form-data"):
                        self._json(415, {"ok": False, "error": "expected multipart"})
                        return
                    data, meta = _parse_multipart(content_type, self._body(8 * 1024 * 1024 + 16384), "save")
                    if not data or len(data) > 8 * 1024 * 1024:
                        raise ValueError("invalid save size")
                    self._json(200, {"ok": True, **store_save(data, meta)})
                elif self.path == "/v1/frame":
                    content_type = self.headers.get("Content-Type", "")
                    if not content_type.lower().startswith("multipart/form-data"):
                        self._json(415, {"ok": False, "error": "expected multipart"})
                        return
                    image, meta = _parse_multipart(content_type, self._body(2 * 1024 * 1024 + 16384))
                    self._json(200, {"ok": True, **judge.process_frame(image, meta)})
                else:
                    self._json(404, {"ok": False, "error": "not found"})
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                # side-key looks vanished without a trace; log rejected posts (no payloads).
                print("POST", self.path, "400", type(exc).__name__, str(exc)[:160], file=sys.stderr, flush=True)
                self._json(400, {"ok": False, "error": "invalid request"})
            except Exception as exc:
                print("POST", self.path, "500", type(exc).__name__, str(exc)[:160], file=sys.stderr, flush=True)
                self._json(500, {"ok": False, "error": "internal error"})

        def do_GET(self):
            if not self._authorized():
                return
            parsed = urlparse(self.path)
            audio_match = re.fullmatch(r"/v1/audio/([1-9][0-9]*)\.mp3", parsed.path)
            if audio_match:
                audio = judge.audio(int(audio_match[1]))
                if audio is None:
                    self._json(404, {"ok": False, "error": "audio expired or unavailable"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(audio)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(audio)
                return
            # the Windows launcher and overlay pull the newest client files from here so nobody swaps files by hand.
            client_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client")
            if parsed.path == "/v1/client/manifest.json":
                files = {}
                for folder, _dirs, names in os.walk(client_dir):
                    for name in names:
                        if ".bak" in name or name.endswith(".prev"):
                            continue
                        full = os.path.join(folder, name)
                        with open(full, "rb") as handle:
                            files[os.path.relpath(full, client_dir).replace(os.sep, "/")] = hashlib.sha256(handle.read()).hexdigest()
                self._json(200, {"ok": True, "files": files})
                return
            client_match = re.fullmatch(r"/v1/client/(?:f/)?([A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.[A-Za-z0-9]+)", parsed.path)
            if client_match:
                try:
                    with open(os.path.join(client_dir, *client_match[1].split("/")), "rb") as handle:
                        payload = handle.read()
                except OSError:
                    self._json(404, {"ok": False, "error": "no such client file"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            if parsed.path != "/v1/say":
                self._json(404, {"ok": False, "error": "not found"})
                return
            query = parse_qs(parsed.query)
            try:
                if set(query) != {"after"} or len(query["after"]) != 1:
                    raise ValueError("invalid after")
                after = int(query["after"][0])
                if after < 0:
                    raise ValueError("invalid after")
                self._json(200, judge.say_after(after))
            except ValueError:
                self._json(400, {"ok": False, "error": "invalid after"})

        def log_message(self, format, *args):
            pass

    return Handler


def make_server(judge, host="127.0.0.1", port=8833):
    if host != "127.0.0.1":
        raise ValueError("judge must bind 127.0.0.1")
    return ThreadingHTTPServer((host, port), make_handler(judge))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    args = parser.parse_args()
    config = load_config(args.config)
    if not config.device_token:
        parser.error("device_token must be configured before starting the HTTP service")
    judge = Judge(config=config)
    server = make_server(judge, config.host, config.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        judge.close()


if __name__ == "__main__":
    main()
