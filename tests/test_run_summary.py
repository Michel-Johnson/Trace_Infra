"""Header totals use elapsed boundaries and real user messages, not tool counts."""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from trace_hunter.ordering import source_projection
from trace_hunter.run_summary import conversation_projection, summarize, span_turn_projection
from trace_hunter.timeline import view


class RunSummaryTests(unittest.TestCase):
    def setUp(self):
        self.trace = json.loads((ROOT / 'examples/minimal.trace.json').read_text())
        self.phases = [
            {'id': '1', 'purpose': 'task', 'start_ms': 0, 'end_ms': 10000},
            {'id': '2', 'purpose': 'task', 'start_ms': 15000, 'end_ms': 30000},
            {'id': '3', 'purpose': 'export', 'start_ms': 40000, 'end_ms': 50000},
        ]
        self.source = {'phases': self.phases, 'timeline': [
            {'row': 1, 'kind': 'user_message', 'phase': 1, 'is_meta': False},
            {'row': 2, 'kind': 'user_message', 'phase': 1, 'is_meta': True},
            {'row': 3, 'kind': 'tool_result', 'phase': 1, 'is_meta': False},
            {'row': 4, 'kind': 'assistant_message', 'phase': 1, 'is_meta': False},
            {'row': 5, 'kind': 'user_message', 'phase': 2, 'is_meta': False},
            {'row': 5, 'kind': 'user_message', 'phase': 2, 'is_meta': False},
            {'row': 6, 'kind': 'user_message', 'phase': 3, 'is_meta': False},
        ]}

    def test_scoped_elapsed_time_includes_gaps_and_not_parallel_sums(self):
        summary = summarize(self.trace, self.phases[:2])
        self.assertEqual(summary['wall_ms'], 30000)
        self.assertIsNone(summary['user_turn_count'])
        self.assertEqual(summary['user_turn_coverage'], 'missing')
        self.assertEqual(summarize(self.trace, self.phases)['wall_ms'], 50000)

    def test_unknown_boundary_is_not_filled_from_tool_intervals(self):
        phases = copy.deepcopy(self.phases)
        phases[1]['end_ms'] = None
        self.assertIsNone(summarize(self.trace, phases)['wall_ms'])
        self.assertEqual(summarize(self.trace, [{'id':'1','start_ms':0,'end_ms':0}])['wall_ms'], 0)

    def test_user_rounds_exclude_skill_injection_tools_and_duplicate_snapshots(self):
        self.trace['phases'] = self.phases
        self.trace['_conversation_projection'] = conversation_projection(self.trace, self.source, 'claude-export')
        summary = summarize(self.trace, self.phases[:2])
        self.assertEqual(summary['user_turn_count'], 2)
        self.assertEqual(summary['user_turn_coverage'], 'complete')
        full = summarize(self.trace, self.phases)
        self.assertEqual(full['user_turn_count'], 3)
        self.assertEqual(full['user_turn_coverage'], 'partial')

    def test_partial_native_capture_is_a_lower_bound(self):
        raw = {'native_messages': [{'role':'user','content':'query'}, {'role':'user','is_meta':True}, {'role':'tool'}, {'role':'user','content':[{'type':'tool_result'}]}, {'role':'user','isCompactSummary':True}]}
        self.trace['_conversation_projection'] = conversation_projection(self.trace, raw, 'doubao-export')
        summary = view(self.trace)['summary']
        self.assertEqual(summary['user_turn_count'], 1)
        self.assertEqual(summary['user_turn_coverage'], 'partial')
        raw['native_messages'] = []
        self.trace['_conversation_projection'] = conversation_projection(self.trace, raw, 'doubao-export')
        self.assertIsNone(view(self.trace)['summary']['user_turn_count'])

    def test_complete_zero_rounds_can_be_reported_for_a_phase(self):
        self.trace['phases'] = self.phases
        self.source['timeline'] = self.source['timeline'][:1]
        self.trace['_conversation_projection'] = conversation_projection(self.trace, self.source, 'claude-export')
        result = summarize(self.trace, self.phases[1:2])
        self.assertEqual(result['user_turn_count'], 0)
        self.assertEqual(result['user_turn_coverage'], 'complete')

    def test_source_is_hash_checked_and_never_modifies_imported_trace(self):
        self.trace['phases'] = self.phases
        self.trace['spans'] = []
        raw = json.dumps(self.source).encode()
        self.trace['sources'] = [{'id':'original','name':'claude-orange-5.execution-trace.json','sha256':hashlib.sha256(raw).hexdigest()}]
        original = copy.deepcopy(self.trace)
        with tempfile.TemporaryDirectory() as directory, patch('trace_hunter.protocol.ROOT', Path(directory)):
            path = Path(directory) / 'apps/trace-lab/claude-orange-5.execution-trace.json'
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
            projected = source_projection(self.trace)
            self.assertEqual(summarize(projected,self.phases[:2])['user_turn_count'],2)
            self.assertEqual(self.trace, original)
            path.write_bytes(b'{}')
            self.assertIsNone(summarize(source_projection(self.trace),self.phases)['user_turn_count'])

    def test_missing_source_does_not_infer_from_case_query(self):
        self.trace['run']['query'] = 'One planned query is not evidence of all user turns.'
        self.assertEqual(view(self.trace)['summary']['user_turn_coverage'],'missing')
        self.assertIsNone(view(self.trace)['summary']['user_turn_count'])

    def test_span_rounds_follow_source_messages_not_tool_counts(self):
        self.trace['phases'] = self.phases
        first, second = copy.deepcopy(self.trace['spans'][1]), copy.deepcopy(self.trace['spans'][3])
        first.update(phase_id='1', source={'source_id':'original','pointer':'/tool_calls/0'})
        second.update(phase_id='2', source={'source_id':'original','pointer':'/timeline/5'})
        self.trace['spans'] = [first, second]
        self.source['tool_calls'] = [{'row':4}]
        result = span_turn_projection(self.trace,self.source,'claude-export','original')
        self.assertEqual(result[first['id']]['ordinal'],1)
        self.assertEqual(result[second['id']]['ordinal'],2)
        self.assertEqual(span_turn_projection(self.trace,self.source,'claude-export','another-source'),{})
        self.trace['_span_turns']=result
        self.assertEqual(set(view(self.trace,['1'])['span_turns']),{first['id']})

    def test_native_span_rounds_skip_compaction_and_result_messages(self):
        raw={'native_messages':[{'role':'user','content':'first'}, {'role':'user','isCompactSummary':True}, {'role':'assistant'}, {'role':'user','content':[{'type':'tool_result'}]}, {'role':'user','content':'next'}, {'role':'assistant'}],
             'tool_calls':[{'tool_call_id':'native','native_message_index':5}], 'runtime_tool_calls':[{'native_tool_call_id':'native'}]}
        self.trace['spans'][1]['source']['pointer']='/runtime_tool_calls/0'
        result=span_turn_projection(self.trace,raw,'doubao-export','original')
        self.assertEqual(result['read']['ordinal'],2)
        self.assertEqual(result['read']['coverage'],'partial')


if __name__ == '__main__':
    unittest.main()
