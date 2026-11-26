import base64
import hashlib
import json
from pathlib import Path
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from types import SimpleNamespace
from unittest import mock

from judge import fake_ai
from judge.judge import (Config, HttpEventSender, Judge, TmpfsImageStore,
                         TmpfsUnavailable, make_server)
from judge.prompts import BOARD_KEYS, DESCRIBE_PROMPT

JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAP//////////////////////////////////"
    "////////////////////////////////////////////////2wBDAf//////////////"
    "////////////////////////////////////////////////////////wAARCAABAAED"
    "ASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAA"
    "AAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ/"
    "/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAA"
    "AAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QA"
    "FBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABAf/8QAFBEB"
    "AAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPxB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/a"
    "AAgBAgEBPxB//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxB//9k="
)


def jpeg(index):
    return JPEG + ("tail-" + str(index)).encode()


def dhash(index):
    return hashlib.sha256(str(index).encode()).hexdigest()[:16]


def desc(importance="高", game="休闲游戏", event="普通", text="角色站在路口"):
    return {"game": game, "event": event, "importance": importance, "desc": text}


class FakeClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class Describe:
    def __init__(self, descriptions):
        self.descriptions = list(descriptions)
        self.calls = []

    def describe(self, image, prompt):
        self.calls.append((image, prompt))
        value = self.descriptions[min(len(self.calls) - 1, len(self.descriptions) - 1)]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class Board:
    def __init__(self):
        self.calls = []

    def update_board(self, board, description):
        self.calls.append((dict(board), dict(description)))
        return {
            "在哪": description["game"],
            "在做什么": description["event"],
            "刚发生了什么": description["desc"],
            "手里有什么": "物品",
            "还差什么": "目标",
        }


class Jev:
    def __init__(self, scores=()):
        self.scores = list(scores) or [1.0]
        self.calls = []

    def score(self, context):
        self.calls.append(context)
        return self.scores[min(len(self.calls) - 1, len(self.scores) - 1)]


class Sender:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.events = []

    def send(self, event):
        self.events.append(event)
        if not self.responses:
            return {"ok": True}
        response = self.responses[min(len(self.events) - 1, len(self.responses) - 1)]
        return response(event) if callable(response) else response


class Logger:
    def __init__(self):
        self.records = []

    def log(self, record):
        self.records.append(dict(record))


class MemoryStore:
    def __init__(self, clock):
        self.clock = clock
        self.files = {}
        self.expiry = {}

    def put(self, image):
        path = "memory://" + uuid.uuid4().hex
        self.files[path] = image
        self.expiry[path] = self.clock.time() + 600
        return path

    def discard(self, path):
        self.files.pop(path, None)
        self.expiry.pop(path, None)

    def cleanup(self):
        for path, expiry in list(self.expiry.items()):
            if self.clock.time() >= expiry:
                self.discard(path)


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.counter = 0

    def build(self, descriptions=None, scores=(), responses=(), competitive=None, idle=True):
        self.describer = Describe(descriptions or [desc()])
        self.board = Board()
        self.jev = Jev(scores)
        self.sender = Sender(responses)
        self.logger = Logger()
        self.store = MemoryStore(self.clock)
        providers = SimpleNamespace(describe=self.describer, update_board=self.board, jev=self.jev)
        config = Config(competitive_games=competitive or ["对抗游戏"])
        judge = Judge(providers=providers, sender=self.sender, image_store=self.store,
                      logger=self.logger, config=config, clock=self.clock,
                      idle_checker=lambda: idle)
        return judge

    def heartbeat(self, judge, paused=False, game=True, level="normal"):
        judge.heartbeat({"ts": self.clock.time(), "paused": paused, "talk_level": level,
                         "fg_exe": "game.exe", "fg_path": "C:/Games/game.exe", "is_game": game})

    def frame(self, judge, kind="auto", game=True, hash_value=None, ts=None):
        self.counter += 1
        image = jpeg(self.counter)
        meta = {"ts": self.clock.time() if ts is None else ts, "kind": kind,
                "fg_exe": "game.exe", "is_game": game,
                "dhash": hash_value or dhash(self.counter)}
        return judge.process_frame(image, meta)

    def test_twenty_distinct_memory_jpegs_batch_and_logging(self):
        judge = self.build([desc(text="第20批画面")])
        images = [path.read_bytes() for path in sorted((Path(__file__).parent / "frames").glob("*.jpg"))]
        self.assertEqual(len(images), 20)
        self.assertEqual(len(set(images)), 20)
        results = []
        for index, image in enumerate(images):
            self.heartbeat(judge)
            result = judge.process_frame(image, {
                "ts": self.clock.time(), "kind": "auto", "fg_exe": "game.exe",
                "is_game": True, "dhash": dhash(index + 100),
            })
            results.append(result)
            self.clock.advance(15)
        self.assertEqual(len(self.describer.calls), 20)
        self.assertEqual(len(self.board.calls), 20)
        self.assertEqual(len(self.logger.records), 20)
        self.assertEqual(sum(result["sent"] for result in results), 20)  # 60/hour, 20 frames all speak
        self.assertEqual(results[-1]["gate"], "sent")
        self.assertTrue(all(call[1] == DESCRIBE_PROMPT for call in self.describer.calls))
        self.assertNotIn("image", self.logger.records[0])
        self.assertNotIn(JPEG.decode("latin1"), json.dumps(self.logger.records))

    def test_heartbeat_fresh_pause_and_game_gates(self):
        judge = self.build()
        self.assertEqual(self.frame(judge)["gate"], "heartbeat")
        self.heartbeat(judge)
        self.clock.advance(15)
        self.assertEqual(self.frame(judge)["gate"], "heartbeat")
        for paused, heartbeat_game, frame_game in ((True, True, True), (False, False, True),
                                                   (False, True, False)):
            with self.subTest(paused=paused, heartbeat_game=heartbeat_game, frame_game=frame_game):
                judge = self.build()
                self.heartbeat(judge, paused=paused, game=heartbeat_game)
                self.assertEqual(self.frame(judge, game=frame_game)["gate"], "heartbeat")
        direct = self.build()
        self.assertEqual(self.frame(direct, kind="direct", game=False)["gate"], "sent")

    def test_auto_minute_limit(self):
        judge = self.build()
        for index in range(4):
            self.heartbeat(judge)
            self.assertEqual(self.frame(judge)["gate"], "sent")
        self.heartbeat(judge)
        self.assertEqual(self.frame(judge)["gate"], "rate_minute")
        self.assertEqual(len(self.describer.calls), 4)

    def test_auto_hour_limit(self):
        judge = self.build()
        for index in range(60):
            self.heartbeat(judge)
            self.assertNotEqual(self.frame(judge)["gate"], "rate_hour")
            self.clock.advance(59)
        self.heartbeat(judge)
        self.assertEqual(self.frame(judge)["gate"], "rate_hour")
        self.assertEqual(len(self.describer.calls), 60)

    def test_direct_minute_limit(self):
        judge = self.build()
        for index in range(12):
            self.assertEqual(self.frame(judge, kind="direct")["gate"], "sent")
        self.assertEqual(self.frame(judge, kind="direct")["gate"], "rate_minute")
        self.assertEqual(len(self.describer.calls), 12)

    def test_server_skips_same_scene_until_refresh(self):
        # describes are spent on real scene changes; small wiggles don't burn the hourly budget.
        judge = self.build()
        base = "0000000000000000"
        near = "000000000000003f"
        far = "00000000ffffffff"
        self.heartbeat(judge)
        self.assertEqual(self.frame(judge, hash_value=base)["gate"], "sent")
        self.clock.advance(179)
        self.heartbeat(judge)
        self.assertEqual(self.frame(judge, hash_value=near)["gate"], "same_scene")
        self.heartbeat(judge)
        self.assertEqual(self.frame(judge, hash_value=far)["gate"], "sent")
        self.clock.advance(180)
        self.heartbeat(judge)
        self.assertEqual(self.frame(judge, hash_value="00000000fffffff0")["gate"], "sent")

    def test_describe_requires_legal_single_line_json(self):
        invalid = ["not json", json.dumps(desc()) + "\n",
                   json.dumps(desc(event="未知")), json.dumps(dict(desc(), extra=1))]
        for raw in invalid:
            with self.subTest(raw=raw[:30]):
                judge = self.build([raw])
                self.heartbeat(judge)
                self.assertEqual(self.frame(judge)["gate"], "describe")
                self.assertEqual(len(self.board.calls), 0)

    def test_long_desc_is_trimmed_not_dropped(self):
        # 11-04 real Haiku output overran 40 chars; the frame should still go through.
        judge = self.build([desc(text="长" * 41)])
        self.heartbeat(judge)
        self.assertEqual(self.frame(judge)["gate"], "sent")
        self.assertEqual(judge.board["刚发生了什么"], "长" * 40)

    def test_board_updates_all_five_columns_after_every_description(self):
        judge = self.build([desc(text="一"), desc(text="二")])
        judge.board = {key: "旧" for key in BOARD_KEYS}
        for expected in ("一", "二"):
            self.clock.advance(301)
            self.heartbeat(judge)
            self.assertEqual(self.frame(judge)["gate"], "sent")
            self.assertEqual(set(judge.board), set(BOARD_KEYS))
            self.assertEqual(judge.board["刚发生了什么"], expected)
        self.assertEqual(len(self.board.calls), 2)
        self.assertEqual(self.board.calls[1][0]["刚发生了什么"], "一")

    def test_high_medium_low_and_jev_thresholds(self):
        high = self.build([desc("高")], scores=[0.0])
        self.heartbeat(high)
        self.assertEqual(self.frame(high)["gate"], "sent")
        self.assertEqual(len(self.jev.calls), 0)

        for level, passing, failing in (("quiet", 0.60, 0.599), ("normal", 0.33, 0.329),
                                        ("chatty", 0.20, 0.199)):
            for score, expected in ((passing, "sent"), (failing, "jev")):
                with self.subTest(level=level, score=score):
                    judge = self.build([desc("中")], scores=[score])
                    self.heartbeat(judge, level=level)
                    self.assertEqual(self.frame(judge)["gate"], expected)
                    self.assertEqual(self.jev.calls[0]["question"], "现在值得 AI 开口跟玩家说话吗？")

        low = self.build([desc("低")])
        self.heartbeat(low, level="normal")
        self.assertEqual(self.frame(low)["gate"], "low")
        busy = self.build([desc("低")], idle=False)
        self.heartbeat(busy, level="chatty")
        self.assertEqual(self.frame(busy)["gate"], "low")
        chatty = self.build([desc("低")], scores=[0.55])
        self.heartbeat(chatty, level="chatty")
        self.assertEqual(self.frame(chatty)["gate"], "sent")

    def test_six_second_shelf_life_includes_provider_time(self):
        def slow_description(image, prompt):
            self.clock.advance(6.01)
            return json.dumps(desc(), ensure_ascii=False)

        judge = self.build()
        self.describer.describe = slow_description
        self.heartbeat(judge)
        self.assertEqual(self.frame(judge)["gate"], "expired")
        self.assertEqual(len(self.sender.events), 0)

    def test_competitive_and_other_cooldowns_with_talk_multipliers(self):
        cases = (("对抗游戏", "normal", 19, 20), ("休闲游戏", "normal", 39, 40),
                 ("对抗游戏", "quiet", 29, 30), ("对抗游戏", "chatty", 11, 12))
        for game, level, blocked, allowed in cases:
            for elapsed, expected in ((blocked, "cooldown"), (allowed, "sent")):
                with self.subTest(game=game, level=level, elapsed=elapsed):
                    judge = self.build([desc("高", game=game), desc("中", game=game)], scores=[1.0])
                    self.heartbeat(judge, level=level)
                    self.assertEqual(self.frame(judge)["gate"], "sent")
                    self.clock.advance(elapsed)
                    self.heartbeat(judge, level=level)
                    self.assertEqual(self.frame(judge)["gate"], expected)

    def test_high_bypasses_cooldown_but_not_sixty_per_hour(self):
        judge = self.build([desc("高")])
        for index in range(60):
            self.heartbeat(judge)
            self.assertEqual(self.frame(judge)["gate"], "sent")
            self.clock.advance(15)
        self.heartbeat(judge)
        self.assertIn(self.frame(judge)["gate"], {"active_hour", "rate_hour"})  # both caps are 60/hour now
        self.assertEqual(len(self.sender.events), 60)

    def test_busy_high_retries_within_shelf_life_and_others_drop(self):
        judge = self.build(responses=[{"ok": False, "busy": True},
                                     {"ok": False, "busy": True}, {"ok": True}])
        self.heartbeat(judge)
        self.assertEqual(self.frame(judge)["gate"], "sent")
        self.assertEqual(len(self.sender.events), 3)
        self.assertAlmostEqual(self.clock.now, 1_000_000.4)

        medium = self.build([desc("中")], scores=[1.0], responses=[{"ok": False, "busy": True}])
        self.heartbeat(medium)
        self.assertEqual(self.frame(medium)["gate"], "busy")
        self.assertEqual(len(self.sender.events), 1)

        expired = self.build(responses=[{"ok": False, "busy": True}])
        self.heartbeat(expired)
        self.assertEqual(self.frame(expired)["gate"], "expired")
        self.assertGreater(len(self.sender.events), 1)

    def test_direct_pressed_while_busy_is_queued_not_dropped(self):
        judge = self.build([desc("低")], responses=[{"ok": False, "busy": True},
                                                   {"ok": False, "busy": True}, {"ok": True}])
        self.assertEqual(self.frame(judge, kind="direct", game=False)["gate"], "queued")
        for _ in range(200):
            if any(r.get("gate") == "sent_late" for r in self.logger.records):
                break
            time.sleep(0.01)
        self.assertTrue(any(r.get("gate") == "sent_late" and r["sent"] for r in self.logger.records))
        self.assertEqual(len(self.sender.events), 3)

    def test_direct_uses_memory_store_skips_judgment_and_expires_after_ten_minutes(self):
        judge = self.build([desc("低")], scores=[0.0])
        result = self.frame(judge, kind="direct", game=False)
        self.assertEqual(result["gate"], "sent")
        self.assertEqual(len(self.jev.calls), 0)
        event = self.sender.events[0]
        self.assertEqual(event["kind"], "desk.direct")
        path = event["image_path"]
        self.assertIn(path, self.store.files)
        self.clock.advance(599)
        self.store.cleanup()
        self.assertIn(path, self.store.files)
        self.clock.advance(1)
        self.store.cleanup()
        self.assertNotIn(path, self.store.files)

        no_store = Judge(providers=SimpleNamespace(describe=self.describer, update_board=self.board,
                                                   jev=self.jev), sender=self.sender,
                         image_store=None, logger=self.logger, clock=self.clock)
        self.assertEqual(self.frame(no_store, kind="direct")["gate"], "direct_store")
        with mock.patch("judge.judge.sys.platform", "darwin"):
            with self.assertRaises(TmpfsUnavailable):
                TmpfsImageStore("/tmp")

    def test_input_validation(self):
        judge = self.build()
        with self.assertRaises(ValueError):
            judge.heartbeat({"paused": False})
        with self.assertRaises(ValueError):
            judge.post_reply({"event_id": None, "text": ""})
        with self.assertRaises(ValueError):
            judge.process_frame(b"not jpeg", {})
        with self.assertRaises(ValueError):
            self.frame(judge, hash_value="xyz")

    def test_all_three_mock_providers(self):
        from judge.providers import MockProviders

        providers = MockProviders(seed=3)
        first = providers.describe.describe(JPEG, DESCRIBE_PROMPT)
        second = providers.describe.describe(JPEG, DESCRIBE_PROMPT)
        self.assertNotEqual(first, second)
        parsed = json.loads(first)
        board = providers.update_board.update_board({}, parsed)
        self.assertEqual(set(board), set(BOARD_KEYS))
        scores = [providers.jev.score({}) for _ in range(5)]
        self.assertTrue(all(0 <= score < 1 for score in scores))
        self.assertGreater(len(set(scores)), 1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.logger = Logger()
        self.store = MemoryStore(self.clock)
        self.describer = Describe([desc(text="HTTP画面")])
        self.board = Board()
        providers = SimpleNamespace(describe=self.describer, update_board=self.board, jev=Jev())
        self.judge = Judge(providers=providers, image_store=self.store, logger=self.logger,
                           config=Config(device_token="mock-device-token"), clock=self.clock)
        self.judge_server = make_server(self.judge, port=0)
        self.judge_url = "http://127.0.0.1:" + str(self.judge_server.server_address[1])
        self.fake_server = fake_ai.make_server(self.judge_url, port=0)
        fake_url = "http://127.0.0.1:" + str(self.fake_server.server_address[1]) + "/desk/event"
        self.judge.sender = HttpEventSender(fake_url)
        self.threads = [threading.Thread(target=server.serve_forever, daemon=True)
                        for server in (self.judge_server, self.fake_server)]
        for thread in self.threads:
            thread.start()

    def tearDown(self):
        for server in (self.judge_server, self.fake_server):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join()
        self.judge.close()

    def request(self, path, data=None, content_type="application/json"):
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        request = urllib.request.Request(self.judge_url + path, data=data,
                                         headers={"Content-Type": content_type, "Authorization": "Bearer mock-device-token"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def frame_body(self, kind, index):
        boundary = "----desk-test-boundary"
        image = jpeg(index)
        meta = json.dumps({"ts": self.clock.time(), "kind": kind, "fg_exe": "game.exe",
                           "is_game": kind == "auto", "dhash": dhash(index + 500)})
        body = (("--" + boundary + "\r\nContent-Disposition: form-data; name=\"image\"; "
                 "filename=\"frame.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n").encode()
                + image + ("\r\n--" + boundary + "\r\nContent-Disposition: form-data; "
                           "name=\"meta\"\r\nContent-Type: application/json\r\n\r\n").encode()
                + meta.encode() + ("\r\n--" + boundary + "--\r\n").encode())
        return body, "multipart/form-data; boundary=" + boundary

    def test_http_heartbeat_auto_direct_fake_reply_and_null_event_reply(self):
        heartbeat = {"ts": self.clock.time(), "paused": False, "talk_level": "normal",
                     "fg_exe": "game.exe", "fg_path": "C:/Games/game.exe", "is_game": True}
        status, result = self.request("/v1/heartbeat", heartbeat)
        self.assertEqual((status, result), (200, {"ok": True}))
        body, content_type = self.frame_body("auto", 1)
        status, result = self.request("/v1/frame", body, content_type)
        self.assertEqual(status, 200)
        self.assertTrue(result["sent"], result)
        body, content_type = self.frame_body("direct", 2)
        status, result = self.request("/v1/frame", body, content_type)
        self.assertEqual(status, 200)
        self.assertTrue(result["sent"])
        status, replies = self.request("/v1/say?after=0")
        self.assertEqual(status, 200)
        self.assertEqual([reply["text"] for reply in replies], ["收到：HTTP画面", "收到：HTTP画面"])
        self.assertEqual(replies[1]["id"], 2)
        self.assertTrue(self.judge._idle(), "fake AI must publish idle after replying")
        status, result = self.request("/v1/reply", {"event_id": None, "text": "独立回话"})
        self.assertEqual(status, 200)
        _, replies = self.request("/v1/say?after=2")
        self.assertEqual(replies, [{"id": 3, "text": "独立回话", "audio_url": None}])

    def test_http_rejects_bad_input_and_empty_long_poll_uses_condition(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/v1/heartbeat", {"paused": False})
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/v1/say?after=nope")
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        started = time.monotonic()
        self.assertEqual(self.judge.say_after(0, wait=0.02), [])
        self.assertGreaterEqual(time.monotonic() - started, 0.015)


if __name__ == "__main__":
    unittest.main(verbosity=2)
