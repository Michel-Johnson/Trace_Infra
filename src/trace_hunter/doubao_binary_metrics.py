"""Bounded, offline field audit of the observed Doubao AalG/AALG log format.

This is an observed-format reader, not an official or universal ALog decoder.
Only field-name counts and hashes leave the decoder. Headers, URLs, credentials,
and body text are never included in the report. A decoded stream without a
DEFLATE end marker is explicitly unverified, including a live file snapshot.
"""
import collections
import hashlib
import re
import struct
import zlib


FIELDS = (
    'prompt_tokens', 'completion_tokens', 'input_tokens', 'output_tokens',
    'total_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens',
    'reasoning_tokens', 'token_usage', 'context_window_usage', 'perf_mark',
    'query_duration', 'STREAM_CHUNK', 'FULL_MSG_NOTIFY', 'chat/completion',
    'text/event-stream',
)
PATTERN = re.compile(b'|'.join(re.escape(s.encode()) for s in FIELDS), re.I)
OVERLAP = max(map(len, FIELDS))


def audit(raw, *, max_decoded_bytes=512 * 1024 * 1024):
    if max_decoded_bytes < 1:
        raise ValueError('positive decoded byte limit required')
    if len(raw) < 24 or raw[:4] != b'AalG' or raw[8:12] != b'Atab':
        raise ValueError('not the observed AalG/Atab format')
    first = struct.unpack_from('<I', raw, 16)[0]
    # Only the exact block header observed in the current client is supported.
    header = re.compile(rb'AALG\x00\x00\x01\x00.{8}\x03\x00\x00\x00' + re.escape(b'0.0'), re.S)
    starts = [m.start() for m in header.finditer(raw)]
    if not starts or starts[0] != first:
        raise ValueError('unsupported or truncated block header')
    result = {'format': 'observed-AalG/AALG-0.0',
              'source_bytes': len(raw), 'source_sha256': hashlib.sha256(raw).hexdigest(),
              'decoded_bytes': 0, 'blocks': [], 'field_counts': {},
              'contains_actual_usage': None, 'historical_trace_complete': None,
              'note': 'Keyword counts are candidates, not model requests or measured usage.'}
    total = collections.Counter()
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(raw)
        offset = start + 23
        decoder = zlib.decompressobj(-15)
        digest = hashlib.sha256()
        counts = collections.Counter()
        tail = b''
        block = {'start': start, 'end': end, 'payload_start': offset,
                 'decoded_bytes': 0, 'deflate_eof': False, 'issues': []}
        for pos in range(offset, end, 16384):
            remaining = max_decoded_bytes - result['decoded_bytes']
            try:
                data = decoder.decompress(raw[pos:min(pos + 16384, end)], remaining + 1)
            except zlib.error:
                block['issues'].append('decompression_error')
                break
            if len(data) > remaining:
                block['issues'].append('decoded_byte_limit')
                break
            result['decoded_bytes'] += len(data)
            block['decoded_bytes'] += len(data)
            digest.update(data)
            combined = tail + data
            counts.update(m.group().decode().lower() for m in PATTERN.finditer(combined)
                          if m.end() > len(tail))
            tail = combined[-OVERLAP:]
            if decoder.eof:
                if decoder.unused_data or pos + 16384 < end:
                    block['issues'].append('unconsumed_block_bytes')
                break
        block['deflate_eof'] = decoder.eof
        if not decoder.eof:
            block['issues'].append('no_deflate_end_marker_completeness_unverified')
        block['decoded_sha256'] = digest.hexdigest()
        block['field_counts'] = dict(counts)
        total.update(counts)
        result['blocks'].append(block)
        if 'decoded_byte_limit' in block['issues']:
            result['remaining_blocks_unread'] = len(starts) - index - 1
            break
    result['field_counts'] = dict(total)
    return result
