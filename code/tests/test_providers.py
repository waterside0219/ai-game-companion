import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from judge.judge import Config
from judge.providers import RealProviders, MockProviders
from judge.prompts import BOARD_KEYS, DESCRIBE_PROMPT, JEV_PROMPT
from test_judge import JPEG, desc


class ProviderContractTests(unittest.TestCase):
    def setUp(self):
        self.real = RealProviders(Config(minimax_key_path='mock-key-file', voice_id='mock-voice'))

    def response(self, value):
        return io.BytesIO(json.dumps(value).encode())

    def test_real_describe_board_and_decision_using_fake_transport(self):
        self.assertTrue(hasattr(self.real, 'describe'), 'real describe missing')
        board = {key: '测试' for key in BOARD_KEYS}
        responses = [self.response({'choices': [{'message': {'content': json.dumps(desc())}}]}),
                     self.response({'choices': [{'message': {'content': json.dumps(board)}}]}),
                     self.response({'answers': {'speak': {'type': 'noul', 'noul': 0.73}}})]
        with mock.patch.dict(os.environ, {'OPENROUTER_API_KEY': 'mock-provider-token'}, clear=True):
            with mock.patch('urllib.request.urlopen', side_effect=responses) as send:
                self.assertEqual(json.loads(self.real.describe.describe(JPEG, DESCRIBE_PROMPT)), desc())
                self.assertEqual(self.real.update_board.update_board({}, desc()), board)
                self.assertEqual(self.real.jev.score({'question': JEV_PROMPT, 'desc': '路口'}), 0.73)
        requests = [call.args[0] for call in send.call_args_list]
        vision = json.loads(requests[0].data)
        self.assertEqual(vision['model'], 'google/gemini-2.5-flash')
        self.assertIn('data:image/jpeg;base64,', json.dumps(vision))
        self.assertEqual(requests[2].full_url, 'https://openrouter.ai/api/alpha/decisions')
        decision = json.loads(requests[2].data)
        self.assertEqual(decision['model'], 'typesafe/jev-1.13')
        self.assertEqual(decision['questions']['speak']['instructions'], JEV_PROMPT)
        self.assertEqual(decision['questions']['speak']['type'], 'noul')

    def test_real_tts_mp3_contract_and_provider_failure(self):
        self.assertTrue(hasattr(self.real, 'tts'), 'real tts missing')
        audio = MockProviders().tts('mock')
        with mock.patch.object(Path, 'read_text', return_value='mock-provider-token') as read:
            with mock.patch('urllib.request.urlopen', return_value=self.response(
                    {'base_resp': {'status_code': 0}, 'data': {'audio': audio.hex()}})) as send:
                self.assertEqual(self.real.tts('hello'), audio)
        read.assert_called_once()
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.minimaxi.com/v1/t2a_v2')
        body = json.loads(request.data)
        self.assertEqual(body['model'], 'speech-02-hd')
        self.assertEqual(body['audio_setting']['format'], 'mp3')
        self.assertEqual(body['output_format'], 'hex')
        self.assertEqual(body['voice_setting']['voice_id'], 'mock-voice')
        with mock.patch.object(Path, 'read_text', return_value='mock-provider-token'):
            with mock.patch('urllib.request.urlopen', return_value=self.response(
                    {'base_resp': {'status_code': 1001}, 'data': None})):
                with self.assertRaises(ValueError):
                    self.real.tts('hello')

    def test_invalid_probability_is_rejected(self):
        self.assertTrue(hasattr(self.real, 'jev'), 'real judge missing')
        for value in (float('nan'), 1.01, -0.01, True):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {'OPENROUTER_API_KEY': 'mock-token'}, clear=True):
                    with mock.patch('urllib.request.urlopen', return_value=self.response(
                            {'answers': {'speak': {'type': 'noul', 'noul': value}}})):
                        with self.assertRaises(ValueError):
                            self.real.jev.score({'question': JEV_PROMPT})
