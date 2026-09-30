import importlib.util
import sys
import unittest
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'scripts/capture_doubao_cdp.py'
spec = importlib.util.spec_from_file_location('doubao_cdp_probe', path)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class DoubaoCDPProbeTests(unittest.TestCase):
    def test_background_capture_is_explicit_and_scoped(self):
        self.assertTrue(probe.is_doubao_capture_page('doubaowork://doubaowork-background/'))
        self.assertFalse(probe.is_doubao_chat_page('doubaowork://doubaowork-background/'))
        for url in ('https://doubaowork-background/', 'doubaowork://doubaowork-background.evil/',
                    'doubaowork://doubaowork-background/account', 'doubaowork://user@doubaowork-background/'):
            self.assertFalse(probe.is_doubao_capture_page(url))

    def test_only_chat_pages_on_observed_client_schemes(self):
        for url in ('chrome://doubaowork-chat/chat/123', 'doubaowork://doubaowork-chat/chat'):
            self.assertTrue(probe.is_doubao_chat_page(url))
        for url in ('https://doubaowork-chat/chat', 'doubaowork://doubaowork-chat/chat-evil',
                    'doubaowork://doubaowork-background/chat', 'doubaowork://user@doubaowork-chat/chat'):
            self.assertFalse(probe.is_doubao_chat_page(url))

    def test_only_exact_endpoint_matches(self):
        host = 'api5-normal-lq.doubao.com'
        self.assertTrue(probe.matches('https://' + host + '/chat/completion?x=1', host))
        for url in ['https://' + host + '.example.com/chat/completion',
                    'https://' + host + '/other', 'http://' + host + '/chat/completion',
                    'https://user:password@' + host + '/chat/completion', None]:
            self.assertFalse(probe.matches(url, host))

    def test_request_metadata_excludes_credentials_and_body(self):
        result = probe.safe_metadata('Network.requestWillBeSent', {
            'requestId': '1', 'timestamp': 12.5, 'request': {
                'url': 'https://api5-normal-lq.doubao.com/chat/completion?secret=query-secret',
                'method': 'POST', 'headers': {'Authorization': 'secret-header'}, 'postData': 'secret-body'},
            'redirectResponse': {'headers': {'Set-Cookie': 'secret-cookie'}}})
        self.assertNotIn('secret', str(result))
        self.assertTrue(result['redirect'])
        self.assertEqual(result['timestamp'], 12.5)

    def test_response_preserves_status_but_not_headers(self):
        result = probe.safe_metadata('Network.responseReceived', {
            'requestId': '2', 'timestamp': 13, 'response': {
                'status': 200, 'mimeType': 'text/event-stream', 'protocol': 'h2',
                'headers': {'Set-Cookie': 'private'}, 'url': 'https://example.com?secret=1'}})
        self.assertEqual(result['status'], 200)
        self.assertNotIn('private', str(result))
        self.assertNotIn('url', result)
