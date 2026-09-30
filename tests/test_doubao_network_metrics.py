import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_hunter.doubao_network_metrics import audit, summarize


def fixture():
    return {'base': {'origin_url': 'https://www.doubao.com/chat/completion?secret=NEVER_EXPORT'},
            'header': {'set_cookie': 'NEVER_EXPORT', 'authorization': 'NEVER_EXPORT',
                       'server-timing': 'inner;dur=9;desc="NEVER_EXPORT"', 'x-output-tokens': '18'},
            'timing': {'request': {'start_time': 10, 'log_time': 30, 'duration': 20000},
                       'detailed_duration': {'ttfb': 100, 'body_recv': 19900, 'dns': -1}},
            'response': {'code': 200, 'recv_body_bytes': 30}}


class NetworkMetricsTests(unittest.TestCase):
    def test_timing_is_not_inference_and_secrets_are_not_exported(self):
        result = summarize(fixture(), 'test.log', 1)
        self.assertNotIn('NEVER_EXPORT', json.dumps(result))
        self.assertIsNone(result['actual_token_usage'])
        self.assertIsNone(result['model_duration_ms'])
        self.assertIsNone(result['phases_ms']['dns'])
        self.assertEqual(result['metric_header_candidates'], {'x-output-tokens': '18'})
        self.assertEqual(result['stream_body_window_ms'], 19900)
        data = fixture();data['base']['origin_url'] = 'https://www.doubao.com.evil.invalid/chat/completion'
        self.assertIsNone(summarize(data, 'test.log', 1))

    def test_overlap_selection_and_decode_failure_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.log'
            path.write_text('[rlog] request_log=' + json.dumps(fixture()) + '\n[rlog] request_log={broken\n')
            result = audit([path], start=20, end=40)
            self.assertEqual(len(result['requests']), 1, 'include streams that began before the window')
            self.assertEqual(result['decode_errors'], 1)
            self.assertTrue(result['sources'][0]['stable_during_read'])
            self.assertEqual(audit([path], start=31)['requests'], [])
