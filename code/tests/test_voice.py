import io
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import wave
from types import SimpleNamespace
from unittest import mock

from judge.judge import Config, Judge, make_server
from judge.providers import MockProviders, RealProviders
from entry.desk_entry import DeskEntry, validate_event, format_message
from test_judge import FakeClock, Sender
from test_entry import FakeTmux, event as frame_event


def wav(ms=1000, rate=16000):
    b=io.BytesIO()
    with wave.open(b,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate);w.writeframes(b'\0\0'*int(rate*ms/1000))
    return b.getvalue()


def multipart(audio,meta):
    return (b'--test\r\nContent-Disposition: form-data; name="audio"; filename="voice.wav"\r\nContent-Type: audio/wav\r\n\r\n'+audio+
        b'\r\n--test\r\nContent-Disposition: form-data; name="meta"\r\nContent-Type: application/json\r\n\r\n'+json.dumps(meta).encode()+b'\r\n--test--\r\n')


def voice_event(ts=None,**changes):
    e={'schema_version':1,'kind':'desk.voice','event_id':'voice-test-1','ts':time.time() if ts is None else ts,'text':'测试\n玩家说的话','importance':'高'}
    e.update(changes);return e


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.clock=FakeClock();self.sender=Sender();self.records=[]
        self.provider=MockProviders();self.provider.transcribe=mock.Mock(return_value='mock：我在电脑前说话')
        self.judge=Judge(config=Config(device_token='mock-device-token'),providers=self.provider,sender=self.sender,clock=self.clock,logger=SimpleNamespace(log=self.records.append))
        self.addCleanup(self.judge.close)
        self.assertTrue(callable(getattr(self.judge,'process_voice',None)),'voice pipeline missing')

    def send(self,audio=None,ts=None,ms=1000):
        return self.judge.process_voice(wav(ms) if audio is None else audio,{'ts':self.clock.time() if ts is None else ts,'ms':ms})

    def test_voice_event_high_no_cooldown_and_no_frame_state_change(self):
        self.judge._spoken_times.extend([self.clock.time()]*10);self.judge._last_spoken=self.clock.time()
        result=self.send();self.assertTrue(result['sent']);e=self.sender.events[0]
        self.assertEqual(set(e),{'schema_version','kind','event_id','ts','text','importance'})
        self.assertEqual(e['kind'],'desk.voice');self.assertEqual(e['importance'],'高');self.assertEqual(e['text'],'mock：我在电脑前说话')
        self.assertEqual(len(self.judge._spoken_times),10);self.assertEqual(len(self.judge._voice_times),1)
        self.assertNotIn('text',json.dumps(self.records));self.assertNotIn('audio',json.dumps(self.records))

    def test_noise_empty_digits_timestamps_dropped(self):
        for text in ('','  ','12345','00:00 00:01','１２３，４５６','...','[00:00] 00:01.000'):
            self.provider.transcribe.return_value=text
            self.assertEqual(self.send()['gate'],'noise',text)
        self.assertFalse(self.sender.events)
        self.provider.transcribe.return_value='12点见'
        self.assertTrue(self.send()['sent'])

    def test_rate_is_60_successes_before_asr_and_independent(self):
        for _ in range(60):self.assertTrue(self.send()['sent'])
        self.assertEqual(self.send()['gate'],'voice_hour');self.assertEqual(self.provider.transcribe.call_count,60)
        self.clock.advance(3600);self.assertTrue(self.send()['sent'])

    def test_no_quota_for_noise_send_failure_or_busy_expiry(self):
        self.sender.responses=[{'ok':False}];self.assertEqual(self.send()['gate'],'send');self.assertEqual(len(self.judge._voice_times),0)
        # busy no longer drops the player's words; they are queued for later delivery and Win is told it's sent.
        self.sender.responses=[{'ok':False,'busy':True}]*200
        self.assertEqual(self.send()['gate'],'sent');self.assertEqual(len(self.judge._voice_times),1)

    def test_deadline_includes_upload_asr_and_retries(self):
        self.assertEqual(self.send(ts=self.clock.time()-20)['gate'],'expired');self.provider.transcribe.assert_not_called()
        # up to 5s of PC clock skew is tolerated; further in the future still expires.
        self.assertEqual(self.send(ts=self.clock.time()+6)['gate'],'expired')
        def slow(audio,**kw):self.assertEqual(kw['deadline'],1000170);self.clock.advance(181);return '超时的话'
        self.provider.transcribe.side_effect=slow
        self.assertEqual(self.send(ts=999990)['gate'],'expired');self.assertFalse(self.sender.events)

    def test_invalid_wav_duration_size_and_metadata(self):
        for audio,meta in [(b'x'*1048577,{'ts':1,'ms':1000}),(b'ID3bad',{'ts':1,'ms':1000}),
            (wav(1000,48000),{'ts':1,'ms':1000}),(wav(30001),{'ts':1,'ms':30001}),
            (wav(),{'ts':1,'ms':30000}),(wav(),{'ts':float('nan'),'ms':1000}),(wav(),{'ts':1,'ms':True}),
            (wav(),{'ts':1,'ms':1000,'extra':1})]:
            with self.subTest(meta=meta):
                with self.assertRaises(ValueError):self.judge.process_voice(audio,meta)
        self.assertTrue(self.send(audio=wav(30000),ms=30000)['sent'])

    def test_http_auth_size_and_frame_limit_are_separate(self):
        server=make_server(self.judge,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def post(route,body,token='mock-device-token'):
            r=urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+route,data=body,headers={'Authorization':'Bearer '+token,'Content-Type':'multipart/form-data; boundary=test'})
            try:
                with urllib.request.urlopen(r,timeout=3) as x:return x.status,json.load(x)
            except urllib.error.HTTPError as x:
                with x:return x.code,json.load(x)
        body=multipart(wav(),{'ts':self.clock.time(),'ms':1000})
        self.assertEqual(post('/v1/voice',body,'bad')[0],401)
        self.assertTrue(post('/v1/voice',body)[1]['sent'])
        # Oversize must be rejected from headers before receiving/allocating the payload.
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        conn.putrequest('POST','/v1/voice');conn.putheader('Authorization','Bearer mock-device-token')
        conn.putheader('Content-Type','multipart/form-data; boundary=test');conn.putheader('Content-Length','1060000');conn.endheaders()
        response=conn.getresponse();self.assertEqual(response.status,413);response.read();conn.close()
        self.assertEqual(post('/v1/frame',b'x'*200000)[0],400)
        self.assertEqual(post('/v1/voice',body.replace(b'name="meta"',b'name="audio"'))[0],400)


class TranscribeTests(unittest.TestCase):
    def test_real_is_separately_disabled_without_reading_credentials(self):
        provider=RealProviders(Config())
        self.assertTrue(callable(getattr(provider,'transcribe',None)),'transcribe provider missing')
        with mock.patch.dict(os.environ,{},clear=True),mock.patch('urllib.request.urlopen') as http:
            with self.assertRaises(PermissionError):provider.transcribe(wav())
            http.assert_not_called()

    def test_slow_trickle_has_a_wall_deadline_using_loopback_only(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        stopped=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                body=json.dumps({'choices':[{'message':{'content':'mock'}}]}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers()
                try:
                    for i in range(0,len(body),5):self.wfile.write(body[i:i+5]);self.wfile.flush();time.sleep(.04)
                except (BrokenPipeError,ConnectionResetError):pass
                finally:stopped.set()
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        provider=RealProviders(Config());self.assertTrue(hasattr(provider,'_post_deadline'),'wall deadline transport missing')
        began=time.monotonic()
        with self.assertRaises(TimeoutError):provider._post_deadline(f'http://127.0.0.1:{server.server_port}/',{},'mock',timeout=.08)
        self.assertLess(time.monotonic()-began,.25)
        self.assertTrue(stopped.wait(.15),'transport socket must stop, not just its caller')

    def test_cancelled_transport_cannot_auto_reconnect(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        encoded,release,received=threading.Event(),threading.Event(),threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.set();self.rfile.read(int(self.headers['Content-Length']))
                try:self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'{}')
                except (BrokenPipeError,ConnectionResetError):pass
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown);self.addCleanup(release.set)
        original=json.dumps
        def delayed(value,**kw):
            if value=={'cancel_test':True}:encoded.set();release.wait(1)
            return original(value,**kw)
        provider=RealProviders(Config())
        with mock.patch('judge.providers.json.dumps',side_effect=delayed):
            with self.assertRaises(TimeoutError):provider._post_deadline(f'http://127.0.0.1:{server.server_port}/',{'cancel_test':True},'mock',timeout=.04)
            self.assertTrue(encoded.is_set());release.set()
            self.assertFalse(received.wait(.12),'expired POST must not reconnect')

    def test_primary_and_fallback_use_remaining_deadline_only_fake_transport(self):
        self.assertTrue(hasattr(Config(),'transcribe_enabled'),'separate real transcribe switch missing')
        provider=RealProviders(Config(transcribe_enabled=True));clock=FakeClock();calls=[]
        def post(url,payload,key,timeout):
            calls.append((payload,timeout));clock.advance(8 if len(calls)==1 else 1)
            if len(calls)==1:raise TimeoutError()
            return {'choices':[{'message':{'content':'测试转写'}}]}
        with mock.patch.dict(os.environ,{'OPENROUTER_API_KEY':'mock-provider-token'},clear=True),mock.patch.object(provider,'_post_deadline',side_effect=post):
            self.assertEqual(provider.transcribe(wav(),deadline=clock.time()+12,clock=clock),'测试转写')
        self.assertEqual([c[0]['model'] for c in calls],['google/gemini-2.5-flash-lite','google/gemini-2.5-flash'])
        self.assertEqual([c[1] for c in calls],[8,4]);self.assertEqual(calls[0][0]['messages'][1]['content'][0]['input_audio']['format'],'wav')


class VoiceEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='.private');self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)/'state.json';self.state.write_text('{"state":"idle"}')
        self.runner=FakeTmux();self.entry=DeskEntry(state_file=self.state,run=self.runner,database=Path(self.temp.name)/'db',sleep=lambda _:None);self.addCleanup(self.entry.close)

    def test_voice_message_is_user_speech_once_and_strict_schema(self):
        e=voice_event();self.assertEqual(validate_event(e),e)
        self.assertEqual(self.entry.accept(e),{'ok':True});self.assertEqual(self.entry.accept(e),{'ok':True})
        self.assertEqual(len(self.runner.typed()),1);message=self.runner.typed()[0]
        self.assertIn('这是玩家在电脑前按住侧键对你说的话',message);self.assertNotIn('不是玩家在聊天里发的消息',message);self.assertNotIn('\n',message)
        for changes in ({'importance':'中'},{'text':''},{'image_path':'/dev/shm/desk/test.jpg'},{'board':{}}):
            with self.assertRaises(ValueError):validate_event(voice_event(**changes))
        with self.assertRaises(ValueError):validate_event(frame_event(text='sneak'))

    def test_expired_voice_never_types_or_presses_enter(self):
        self.assertEqual(self.entry.accept(voice_event(ts=time.time()-181)).get('expired'),True);self.assertFalse(self.runner.calls)

if __name__=='__main__':unittest.main()
