import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from trace_hunter.analysis import analyze
from trace_hunter.ordering import decorate, merge_order, source_projection
from trace_hunter.protocol import validate, InvalidTrace
from trace_hunter.reports import compose, validate_prices
from trace_hunter.storage import Store
from trace_hunter.timeline import view


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.trace = json.loads((ROOT / 'examples/minimal.trace.json').read_text())
        self.prices = json.loads((ROOT / 'config/prices.example.json').read_text())

    def test_cost_does_not_add_cache_or_thinking_twice(self):
        report = compose(self.trace, analyze(self.trace), self.prices)
        self.assertEqual(report['cost']['amount'], '0.00182')
        self.assertEqual(report['cost']['pricing_status'], 'estimated')

    def test_unknown_price_never_becomes_zero_cost(self):
        report = compose(self.trace, analyze(self.trace))
        self.assertIsNone(report['cost']['amount'])
        self.assertEqual(report['cost']['tokens']['total_tokens'], 3200)

    def test_missing_cache_usage_cannot_be_priced_as_zero(self):
        self.trace['spans'][0]['usage']['cache_read_tokens'] = None
        report = compose(self.trace, analyze(self.trace), self.prices)
        self.assertIsNone(report['cost']['amount'])
        self.assertEqual(report['cost']['pricing_status'], 'missing_usage')

    def test_partial_coverage_only_returns_observed_cost(self):
        self.trace['coverage']['model_requests'] = 'partial'
        cost = compose(self.trace, analyze(self.trace), self.prices)['cost']
        self.assertIsNone(cost['amount'])
        self.assertEqual(cost['observed_amount'], '0.00182')

    def test_invalid_and_duplicate_prices_rejected(self):
        self.prices['entries'][0]['input_per_million'] = float('nan')
        with self.assertRaises(ValueError):
            validate_prices(self.prices)
        self.prices['entries'][0]['input_per_million'] = 1
        self.prices['entries'].append(copy.deepcopy(self.prices['entries'][0]))
        with self.assertRaises(ValueError):
            validate_prices(self.prices)

    def test_price_changes_do_not_reuse_old_analysis_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.sqlite'
            first = Store(path, self.prices)
            first.import_trace(self.trace)
            a = first.analyze_run(self.trace['run']['id'])
            self.prices['entries'][0]['output_per_million'] = 3
            second = Store(path, self.prices)
            b = second.analyze_run(self.trace['run']['id'])
            self.assertNotEqual(a['analysis']['cost']['amount'], b['analysis']['cost']['amount'])

    def test_untimed_skill_between_timed_tools_stays_in_place(self):
        unknown = copy.deepcopy(self.trace['spans'][1])
        unknown.update(id='skill', name='Skill', start_ms=None, end_ms=None,
                       duration_ms=None, input={'skill':'inventory'}, operation='skill')
        self.trace['spans'].insert(2, unknown)
        decorate(self.trace, ['read', 'skill', 'write'])
        rows = view(validate(self.trace))['rows']
        self.assertEqual([r['span_id'] for r in rows], ['read', 'skill', 'write'])
        self.assertIsNone(rows[1]['tool_ms'])
        self.assertEqual(rows[1]['order_basis'], 'source_sequence')
        self.assertEqual(rows[1]['skill'], {'name':'inventory', 'action':'invoke'})

    def test_no_timestamp_or_sequence_preserves_input_not_uuid_sort(self):
        first, last = self.trace['spans'][1], self.trace['spans'][3]
        first.update(id='z', start_ms=None, end_ms=None, duration_ms=None)
        last.update(id='a', start_ms=None, end_ms=None, duration_ms=None)
        self.assertEqual([r['span_id'] for r in view(self.trace)['rows']], ['z','a'])

    def test_skill_read_is_load_not_invocation_or_extra_tool(self):
        self.trace['spans'][1]['input'] = {'file_path':'/skills/inventory/SKILL.md'}
        decorate(self.trace)
        result = compose(self.trace, analyze(self.trace))
        self.assertEqual(result['trajectory']['skills'][0]['action'], 'load')
        self.assertEqual(result['time']['tool_count'], 2)

    def test_hash_verified_source_recovers_historical_order_without_mutation(self):
        # Exercise legacy recovery with a self-contained source, not private captures.
        from trace_hunter.adapters import normalize
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'apps/trace-lab/doubao-2.1-pro.trajectory.json'
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({
                'manifest': {'query':'synthetic order test', 'conversation_id':'order-fixture',
                             'selected_model_label':'fixture',
                             'actual_submission_observed_at':'2026-01-01T00:00:00Z',
                             'completed_observed_at':'2026-01-01T00:00:12Z'},
                'tool_calls': [
                    {'tool_call_id':'skill-first','name':'Skill','arguments':{'skill':'inventory'}},
                    {'tool_call_id':'bash-second','name':'Bash','arguments':{'command':'true'}}],
                'runtime_tool_calls': [
                    {'runtime_id':'runtime-bash','name':'Bash','native_tool_call_id':'bash-second',
                     'timing':{'started_at':'2026-01-01T00:00:10Z','ended_at':'2026-01-01T00:00:11Z','duration_ms':1000}}]
            }))
            trace = normalize(source, 'doubao-export')
            for span in trace['spans']:
                for key in ('sequence','order_basis','skill'):
                    span.pop(key, None)
            original = copy.deepcopy(trace)
            with patch('trace_hunter.protocol.ROOT', root):
                projected = source_projection(trace)
                self.assertEqual([r['span_id'] for r in view(projected)['rows']], ['skill-first','runtime-bash'])
                self.assertIsNone(view(projected)['rows'][0]['tool_ms'])
                self.assertEqual(trace, original)
                trace['sources'][0]['sha256'] = '0' * 64
                unmatched = source_projection(trace)
                self.assertFalse(any('sequence' in s for s in unmatched['spans']))

    def test_redacted_skill_filename_does_not_invent_skill_name(self):
        self.trace['spans'][1]['input'] = {'tail':'SKILL.md','hash':'opaque'}
        decorate(self.trace)
        self.assertEqual(self.trace['spans'][1]['skill'], {'name':'未记录技能名','action':'load'})

    def test_request_elapsed_time_is_unknown_without_endpoints(self):
        for s in self.trace['spans']:
            if s['kind'] == 'model':
                s.update(start_ms=None, end_ms=None, duration_ms=None)
        time = compose(self.trace, analyze(self.trace))['time']
        self.assertIsNone(time['model_time_union_ms'])
        self.assertEqual(time['tool_time_union_ms'], 3000)

    def test_duplicate_tool_sequence_rejected(self):
        for span in self.trace['spans']:
            if span['kind'] == 'tool':
                span['sequence'] = 0
        with self.assertRaises(InvalidTrace):
            validate(self.trace)

    def test_interaction_stays_in_wall_time_but_is_separate_from_automatic_tools(self):
        self.trace['spans'][1].update(name='AskUserQuestion', operation='human')
        report = compose(self.trace, analyze(self.trace))['time']
        self.assertEqual(report['wall_ms'], 12000)
        self.assertEqual(report['tool_time_union_ms'], 3000)
        self.assertEqual(report['automatic_tool_time_union_ms'], 2000)
        self.assertEqual(report['interaction_time_union_ms'], 1000)

    def test_unknown_interaction_time_is_not_zero_or_inferred_from_task_gap(self):
        self.trace['spans'][1].update(operation='human', start_ms=None, end_ms=None, duration_ms=None)
        report = compose(self.trace, analyze(self.trace))['time']
        self.assertEqual(report['interaction_count'], 1)
        self.assertEqual(report['interaction_time_known'], 0)
        self.assertIsNone(report['interaction_time_union_ms'])
        self.assertEqual(report['automatic_tool_time_union_ms'], 2000)

    def test_overlapping_interaction_and_wait_are_counted_once_and_clipped(self):
        self.trace['spans'][1].update(operation='human', start_ms=6000, end_ms=11000)
        wait = copy.deepcopy(self.trace['spans'][1])
        wait.update(id='human-wait', kind='wait', start_ms=10000, end_ms=15000)
        self.trace['spans'].append(wait)
        report = compose(self.trace, analyze(self.trace))['time']
        self.assertEqual(report['interaction_time_union_ms'], 6000)
        self.assertEqual(report['automatic_tool_time_union_ms'], 2000)
        self.assertEqual(report['wall_ms'], 12000)


if __name__ == '__main__':
    unittest.main()
