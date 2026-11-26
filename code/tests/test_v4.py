import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from judge.judge import Config, Judge, make_server
from judge import providers
from test_judge import FakeClock, Logger, JPEG

TOKEN = 'mock-device-token'


class V4Tests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.judge = Judge(logger=Logger(), clock=self.clock)
        self.addCleanup(self.judge.close)

    def test_default_port_and_provider_selection(self):
        self.assertEqual(Config().port, 8833)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(providers.load_providers(Config()), providers.MockProviders)
        with mock.patch.dict(os.environ, {'DESK_PROVIDERS': 'invalid'}, clear=True):
            with self.assertRaises(ValueError):
                providers.load_providers(Config())
        with mock.patch.dict(os.environ, {'DESK_PROVIDERS': 'real'}, clear=True):
            # Selecting the real implementation must not read credentials or connect.
            with mock.patch('pathlib.Path.read_text', side_effect=AssertionError('key read')):
                with mock.patch('urllib.request.urlopen', side_effect=AssertionError('network')):
                    self.assertIsInstance(providers.load_providers(Config()), providers.RealProviders)

    def test_say_audio_and_subtitle_fallback(self):
        self.assertTrue(hasattr(self.judge, 'say_after'), 'v4 say queue missing')
        reply = self.judge.post_reply({'text': '收到：画面'})
        self.assertEqual(reply['id'], 1)
        self.assertEqual(self.judge.say_after(0, wait=0), [
            {'id': 1, 'text': '收到：画面', 'audio_url': '/v1/audio/1.mp3'}])
        audio = self.judge.audio(1)
        self.assertIsInstance(audio, bytes)
        self.assertGreater(len(audio), 100)
        self.judge.providers.tts = mock.Mock(side_effect=OSError('tts down'))
        self.judge.post_reply({'event_id': None, 'text': '只有字幕'})
        self.assertIsNone(self.judge.say_after(1, wait=0)[0]['audio_url'])
        self.assertIsNone(self.judge.audio(2))

    def test_twenty_audio_limit_ttl_and_monotonic_ids(self):
        self.assertTrue(hasattr(self.judge, 'audio'), 'v4 audio store missing')
        for i in range(21):
            self.judge.post_reply({'text': str(i)})
        self.assertIsNone(self.judge.audio(1))
        self.assertEqual(len(self.judge.say_after(0, wait=0)), 20)
        self.clock.advance(600)
        self.assertIsNone(self.judge.audio(21))
        self.assertEqual(self.judge.say_after(0, wait=0), [])
        self.assertEqual(self.judge.post_reply({'text': 'new'})['id'], 22)

    def test_say_wakes_when_reply_arrives(self):
        self.assertTrue(hasattr(self.judge, 'say_after'), 'v4 long poll missing')
        results = []
        thread = threading.Thread(target=lambda: results.extend(self.judge.say_after(0, wait=1)))
        thread.start()
        self.judge.post_reply({'text': 'wake'})
        thread.join(2)
        self.assertEqual(results[0]['text'], 'wake')

    def test_capture_black_and_size_contract(self):
        meta = {'ts': self.clock.time(), 'kind': 'direct', 'fg_exe': 'game.exe',
                'is_game': True, 'dhash': '0000000000000000', 'capture_black': True}
        with mock.patch.object(self.judge.providers.describe, 'describe') as describe:
            self.assertEqual(self.judge.process_frame(JPEG, meta)['gate'], 'capture_black')
            describe.assert_not_called()
        meta['capture_black'] = False
        with self.assertRaises(ValueError):
            self.judge.process_frame(JPEG + b'0' * (2 * 1024 * 1024), meta)
        meta['capture_black'] = 'false'
        with self.assertRaises(ValueError):
            self.judge.process_frame(JPEG, meta)


class V4HttpTests(unittest.TestCase):
    def setUp(self):
        self.assertIn('device_token', Config.__dataclass_fields__, 'v4 bearer config missing')
        self.judge = Judge(config=Config(device_token=TOKEN), logger=Logger())
        self.server = make_server(self.judge, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.judge.close()

    def request(self, path, data=None, token=TOKEN, headers=None):
        all_headers = {'Content-Type': 'application/json', **(headers or {})}
        if token is not None:
            all_headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request(self.url + path,
            data=None if data is None else json.dumps(data).encode(), headers=all_headers)
        return urllib.request.urlopen(request, timeout=3)

    def test_all_win_routes_require_bearer_and_constant_time_comparison(self):
        for path, data in [('/v1/heartbeat', {}), ('/v1/frame', {}),
                           ('/v1/say?after=0', None), ('/v1/audio/1.mp3', None)]:
            for token in (None, '', 'wrong'):
                with self.subTest(path=path, token=token):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        self.request(path, data, token=token)
                    self.assertEqual(caught.exception.code, 401)
                    caught.exception.close()
        import hmac
        with mock.patch('judge.judge.hmac.compare_digest', wraps=hmac.compare_digest) as compare:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request('/v1/audio/1.mp3')
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()
            compare.assert_called()

    def test_reply_say_audio_and_forwarded_reply_rejection(self):
        with self.request('/v1/reply', {'text': '收到：测试'}, token=None) as response:
            self.assertEqual(response.status, 200)
        with self.request('/v1/say?after=0') as response:
            value = json.load(response)
        self.assertEqual(set(value[0]), {'id', 'text', 'audio_url'})
        with self.request(value[0]['audio_url']) as response:
            self.assertEqual(response.headers['Content-Type'], 'audio/mpeg')
            self.assertGreater(len(response.read()), 100)
        for headers in ({'CF-Connecting-IP': '203.0.113.1'},
                        {'X-Forwarded-For': '127.0.0.1'}, {'Forwarded': 'for=127.0.0.1'}):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request('/v1/reply', {'text': 'remote'}, headers=headers)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()

    def test_status_is_local_only(self):
        try:
            with self.request('/v1/status', {'idle': True}, token=None) as response:
                self.assertEqual(response.status, 200)
        except urllib.error.HTTPError as error:
            error.close()
            self.fail(f'status endpoint rejected local request: {error.code}')
        self.assertTrue(self.judge._idle())
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/v1/status', {'idle': True}, headers={'CF-Connecting-IP': '203.0.113.1'})
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_missing_device_token_fails_closed(self):
        self.judge.config.device_token = ''
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/v1/audio/1.mp3', token='')
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()


class DeadlineTests(unittest.TestCase):
    def test_high_busy_retry_never_sends_after_deadline(self):
        from test_judge import Describe, Board, Jev, desc, Sender, MemoryStore
        from types import SimpleNamespace
        clock = FakeClock()
        attempts = []

        def first(event):
            attempts.append(clock.time() - event['ts'])
            clock.advance(5.9)
            return {'ok': False, 'busy': True}

        def late(event):
            attempts.append(clock.time() - event['ts'])
            return {'ok': True}

        judge = Judge(providers=SimpleNamespace(describe=Describe([desc()]), update_board=Board(), jev=Jev()),
                      sender=Sender([first, late]), image_store=MemoryStore(clock), logger=Logger(), clock=clock)
        self.addCleanup(judge.close)
        result = judge.process_frame(JPEG, {'ts': clock.time(), 'kind': 'direct', 'fg_exe': '',
            'is_game': False, 'dhash': '0000000000000000'})
        self.assertEqual(result['gate'], 'queued')  # direct waits for the AI instead of expiring
        self.assertGreaterEqual(len(attempts), 1)  # queued delivery may retry in the background


class IdleStatusTests(unittest.TestCase):
    def test_executable_default_requires_fresh_idle_status(self):
        from test_judge import Describe, Board, Jev, Sender, desc
        from types import SimpleNamespace
        clock = FakeClock()
        jev = Jev([1.0])
        judge = Judge(providers=SimpleNamespace(describe=Describe([desc('低')]), update_board=Board(), jev=jev),
                      sender=Sender(), logger=Logger(), clock=clock)
        self.addCleanup(judge.close)
        self.assertFalse(judge._idle(), 'production must not assume the AI is idle')
        self.assertTrue(hasattr(judge, 'status'))
        judge.status({'idle': True})
        self.assertTrue(judge._idle())
        clock.advance(15)
        self.assertFalse(judge._idle())
        judge.status({'idle': False})
        self.assertFalse(judge._idle())
        with self.assertRaises(ValueError):
            judge.status({'idle': 'true'})
        judge.heartbeat({'ts': clock.time(), 'paused': False, 'talk_level': 'chatty',
            'fg_exe': 'game.exe', 'fg_path': 'C:/Games/game.exe', 'is_game': True})
        frame = {'ts': clock.time(), 'kind': 'auto', 'fg_exe': 'game.exe',
            'is_game': True, 'dhash': '0000000000000000'}
        self.assertEqual(judge.process_frame(JPEG, frame)['gate'], 'low')
        self.assertEqual(len(jev.calls), 0)
        judge.status({'idle': True})
        self.assertEqual(judge.process_frame(JPEG, dict(frame, dhash='ffffffffffffffff'))['gate'], 'sent')
        self.assertEqual(len(jev.calls), 1)
        self.assertFalse(judge._idle(), 'sending an event consumes the last idle status')


class TmpfsRestartTests(unittest.TestCase):
    def test_restart_removes_expired_images_and_restores_remaining_timer(self):
        import tempfile
        from judge.judge import TmpfsImageStore
        # Only synthetic fixture bytes, in this project; never reads a real tmpfs or screenshot.
        with tempfile.TemporaryDirectory(dir='.private') as directory:
            old = __import__('pathlib').Path(directory) / ('a' * 32 + '.jpg')
            recent = old.with_name('b' * 32 + '.jpg')
            unrelated = old.with_name('keep.txt')
            for path in (old, recent, unrelated):
                path.write_bytes(JPEG)
            os.utime(old, (time.time() - 601, time.time() - 601))
            os.utime(recent, (time.time() - 300, time.time() - 300))
            with mock.patch('judge.judge._is_tmpfs', return_value=True):
                store = TmpfsImageStore(directory)
            try:
                self.assertFalse(old.exists(), 'expired pre-restart image must be deleted')
                self.assertIn(str(recent.resolve()), store._timers)
                self.assertLessEqual(store._timers[str(recent.resolve())].interval, 300)
                self.assertTrue(unrelated.exists())
            finally:
                store.close()
