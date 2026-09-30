import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trace_hunter.local_doubao import capture, list_sessions, parse_messages, SESSIONS, LOGS, session_fingerprint
from trace_hunter.storage import Store


def call(identifier, name, args):
    return {'id': identifier, 'function': {'name': name, 'arguments': json.dumps(args)}}


class LocalDoubaoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'client'
        self.session = self.root / SESSIONS / '123'
        self.native = self.session / 'agents/main/system/trajectory.jsonl'
        self.native.parent.mkdir(parents=True)
        self.command = 'echo "采集证据，只记录不执行"'
        self.rows = [
            {'role':'user', 'content':'超市进货系统'},
            {'role':'assistant', 'content':'开始', 'tool_calls':[
                call('r', 'Read', {'file_path':'/skills/inventory/SKILL.md'}),
                call('s', 'Skill', {'skill':'inventory'}),
                call('u', 'FutureTool', {'something':1}),
                call('b', 'Bash', {'command':self.command})]},
            {'role':'tool','tool_call_id':'b','content':'tool result'},
            {'role':'assistant','content':'已完成，但这不是采集完整性证据'},
        ]
        self.write_native()

    def write_native(self):
        self.raw = b''.join((json.dumps(r, ensure_ascii=False)+'\n').encode() for r in self.rows)
        self.native.write_bytes(self.raw)

    def export(self, **kwargs):
        out = Path(self.temp.name) / 'out'
        result = capture(self.root, '123', out, query_id='same-query', env_id='test-env', **kwargs)
        return result, json.loads((out/'run.trace.json').read_text()), out

    def log(self, *lines):
        folder = self.root / LOGS
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'saman_2026.0907.0.log').write_text(''.join(lines))

    def start(self, rid='run1', command=None, at='120000.000000'):
        command = self.command if command is None else command
        return f'[1:2:0907/{at}:INFO:test] [sandbox] ContinueRunBash [bash] run begin, instance_id=i, bash_run_id={rid}, command_len={len(command.encode())}, command={json.dumps(command,ensure_ascii=False)}, env_len=0\n'

    def end(self, rid='run1', duration=500, at='120000.500000'):
        return f'[1:2:0907/{at}:INFO:test] [sandbox] OnRunBashFinished [bash] run end, instance_id=i, bash_run_id={rid}, duration_ms={duration}, exit_code=0\n'

    def test_original_bytes_notes_unknown_tools_and_no_model_invention(self):
        note = b'# Session note\nOriginal bytes\n'
        (self.session/'board.md').write_bytes(note)
        result, trace, out = self.export()
        self.assertEqual((out/'raw/agents/main/system/trajectory.jsonl').read_bytes(), self.raw)
        self.assertEqual(self.native.read_bytes(), self.raw)
        self.assertEqual((out/'raw/board.md').read_bytes(), note)
        self.assertEqual([s['name'] for s in trace['spans']], ['Read','Skill','FutureTool','Bash'])
        self.assertEqual(trace['spans'][0]['skill']['action'], 'load')
        self.assertEqual(trace['spans'][1]['skill']['action'], 'invoke')
        self.assertEqual(trace['spans'][2]['operation'], 'other')
        self.assertTrue(all(s['usage'] is None and s['duration_ms'] is None for s in trace['spans']))
        self.assertFalse(result['complete_trajectory'])
        self.assertEqual(trace['run']['status'], 'partial')
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        for file in out.rglob('*'):
            if file.is_file():self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        # Import/read remain deterministic and do not trigger any analysis.
        with patch('trace_hunter.storage.analyze', side_effect=AssertionError('analysis invoked')):
            store = Store(out/'test.sqlite')
            store.import_trace(trace)
            self.assertEqual(len(store.get(trace['run']['id'])['view']['rows']), 4)

    def test_torn_tail_and_corrupt_records_preserved_without_fabrication(self):
        self.native.write_bytes(self.raw + b'not-json\n{"role":"assistant","content":')
        result, trace, out = self.export()
        self.assertEqual(len(trace['spans']), 4)
        self.assertEqual(len(result['agents'][0]['parse_issues']), 2)
        self.assertEqual((out/'raw/agents/main/system/trajectory.jsonl').read_bytes(), self.native.read_bytes())

    def test_native_does_not_follow_symlink_notes_or_agent_directories(self):
        secret = Path(self.temp.name)/'secret'
        secret.write_text('PRIVATE_NOT_A_SESSION_FILE')
        (self.session/'board.md').symlink_to(secret)
        (self.session/'agents/escape').symlink_to(secret.parent, target_is_directory=True)
        result, _, out = self.export()
        self.assertEqual(len(result['agents']),1)
        self.assertTrue(any(o['path']=='board.md' for o in result['omissions']))
        self.assertNotIn('PRIVATE_NOT_A_SESSION_FILE',(out/'session.local.json').read_text())

    def test_output_cannot_write_into_live_profile_through_alias(self):
        alias = Path(self.temp.name)/'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            capture(self.root, '123', alias/'capture', query_id='q',env_id='e')
        self.assertFalse((self.root/'capture').exists())

    def test_unique_complete_sdk_command_gets_time_with_source(self):
        self.log(self.start(), self.end())
        result, trace, out = self.export(log_days=['20260907'],log_timezone='Asia/Singapore')
        bash = trace['spans'][-1]
        self.assertEqual(bash['duration_ms'],500)
        self.assertEqual((bash['start_ms'],bash['end_ms']),(0,500))
        self.assertIsNone(trace['phases'][0]['start_ms'])
        self.assertEqual(result['tools_with_sdk_duration'],1)
        captured = json.loads((out/'session.local.json').read_text())
        self.assertIn('raw_line',captured['runtime_calls'][0]['start'])

    def test_missing_timezone_does_not_invent_absolute_time(self):
        self.log(self.start(),self.end())
        _, trace, _ = self.export(log_days=['20260907'])
        self.assertEqual(trace['spans'][-1]['duration_ms'],500)
        self.assertIsNone(trace['spans'][-1]['start_ms'])

    def test_duplicate_runtime_command_with_missing_end_is_ambiguous(self):
        self.log(self.start(),self.end(),self.start(rid='run2',at='120001.000000'))
        result,trace,_ = self.export(log_days=['20260907'])
        self.assertIsNone(trace['spans'][-1]['duration_ms'])
        self.assertTrue(any('missing_bash_end' in x for x in result['log_issues']))

    def test_duplicate_native_command_never_matches_one_runtime_event_twice(self):
        self.rows[1]['tool_calls'].append(call('b2','Bash',{'command':self.command}))
        self.write_native()
        self.log(self.start(),self.end())
        result,trace,_ = self.export(log_days=['20260907'])
        self.assertEqual(result['tools_with_sdk_duration'],0)
        self.assertTrue(all(s['duration_ms'] is None for s in trace['spans']))

    def test_conflicting_clocks_and_wrong_day_remain_unknown(self):
        self.log(self.start(),self.end(duration=700))
        result,trace,_ = self.export(log_days=['20260907'],log_timezone='Asia/Singapore')
        self.assertEqual(result['tools_with_sdk_duration'],0)
        self.assertTrue(any('clock_duration_conflict' in x for x in result['log_issues']))

    def test_missing_log_directory_still_exports_native(self):
        result,trace,_ = self.export(log_days=['20260907'])
        self.assertIn('sdk_log_directory_unavailable',result['log_issues'])
        self.assertEqual(len(trace['spans']),4)

    def test_snapshot_exclusive_and_session_identity_validated(self):
        self.export()
        with self.assertRaises(FileExistsError):self.export()
        with self.assertRaises(ValueError):capture(self.root,'../../escape',Path(self.temp.name)/'bad',query_id='q',env_id='e')
        self.assertEqual(list_sessions(self.root)[0]['conversation_id'],'123')

    def test_change_detection_sees_rewrite_and_noop(self):
        a=session_fingerprint(self.root,'123')
        self.assertEqual(a,session_fingerprint(self.root,'123'))
        self.rows.append({'role':'user','content':'第二轮'})
        self.write_native()
        self.assertNotEqual(a,session_fingerprint(self.root,'123'))

    def test_nonfinite_json_is_raw_only(self):
        rows,issues=parse_messages(b'{"role":"assistant","value":NaN}\n')
        self.assertEqual(rows,[])
        self.assertEqual(len(issues),1)

    def test_missing_call_id_does_not_join_an_unidentified_result(self):
        self.rows.append({'role':'assistant','tool_calls':[{'function':None}]})
        self.rows.append({'role':'tool','content':'Cannot identify this result'})
        self.write_native()
        _,trace,_=self.export()
        self.assertEqual(trace['spans'][-1]['name'],'未知工具')
        self.assertIsNone(trace['spans'][-1]['output'])


if __name__ == '__main__':unittest.main()
