import struct
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_hunter.doubao_binary_metrics import audit


def fixture(parts, complete=True, level=-1):
    raw = bytearray(152)
    raw[:4], raw[8:12] = b'AalG', b'Atab'
    struct.pack_into('<I', raw, 16, 152)
    for part in parts:
        compressor = zlib.compressobj(level=level, wbits=-15)
        body = compressor.compress(part) + compressor.flush(zlib.Z_FINISH if complete else zlib.Z_SYNC_FLUSH)
        raw.extend(b'AALG\x00\x00\x01\x00' + bytes(8) + struct.pack('<I', 3) + b'0.0' + body)
    return bytes(raw)


class BinaryMetricsTests(unittest.TestCase):
    def test_multiple_blocks_and_private_values_not_in_report(self):
        result = audit(fixture([b'input_tokens=123 secret=private-fixture-password', b'output_tokens=45']))
        self.assertEqual(result['field_counts'], {'input_tokens': 1, 'output_tokens': 1})
        self.assertEqual(len(result['blocks']), 2)
        self.assertTrue(all(b['deflate_eof'] for b in result['blocks']))
        self.assertNotIn('private-fixture-password', str(result))
        self.assertIsNone(result['contains_actual_usage'])

    def test_live_sync_flush_cannot_claim_complete(self):
        result = audit(fixture([b'perf_mark'], complete=False))
        self.assertEqual(result['field_counts'], {'perf_mark': 1})
        self.assertIn('no_deflate_end_marker_completeness_unverified', result['blocks'][0]['issues'])

    def test_expansion_limit_and_bad_header(self):
        result = audit(fixture([b'x' * 10000]), max_decoded_bytes=100)
        self.assertLessEqual(result['decoded_bytes'], 100)
        self.assertIn('decoded_byte_limit', result['blocks'][0]['issues'])
        with self.assertRaises(ValueError):
            audit(b'not a log')

    def test_corruption_is_explicit(self):
        raw = bytearray(fixture([b'input_tokens=123']))
        raw[175] = 0xff
        result = audit(bytes(raw))
        self.assertIn('decompression_error', result['blocks'][0]['issues'])

    def test_keyword_at_chunk_boundary_counted_once(self):
        # Store uncompressed DEFLATE blocks to place text over the read boundary.
        prefix = b'x' * 16370
        content = prefix + b'cache_creation_input_tokens' + b' perf_mark'
        result = audit(fixture([content], level=0))
        self.assertEqual(result['field_counts']['cache_creation_input_tokens'], 1)
        self.assertEqual(result['field_counts']['perf_mark'], 1)
