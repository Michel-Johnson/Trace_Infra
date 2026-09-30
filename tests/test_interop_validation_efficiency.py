import json
import unittest

from trace_hunter.interop import service


class CountingValidator:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = 0

    def iter_errors(self, document):
        self.calls += 1
        return self.delegate.iter_errors(document)


class InteropValidationEfficiencyTests(unittest.TestCase):
    def test_adapter_conversion_runs_full_schema_validation_once(self):
        raw = json.dumps({
            "case_id": "validation-count",
            "domain": "trace-analysis",
            "source": "test",
            "raw_turn": {
                "turn_index": 0,
                "user_prompt": "检查一次校验",
                "calls": [{
                    "tool_name": "Read",
                    "tool_call_id": "call-1",
                    "start_time": "2026-09-20T10:00:00+08:00",
                    "duration_ms": 12,
                    "call_content": {"file_path": "/tmp/SKILL.md"},
                    "result": "Process exited with code 0",
                }],
            },
        }, ensure_ascii=False).encode()
        version = "trace-hunter/2.0-draft.2"
        original = service.STORAGE_V2_VALIDATORS[version]
        validator = CountingValidator(original)
        service.STORAGE_V2_VALIDATORS[version] = validator
        try:
            service.convert(
                raw,
                binding={"run_id": "run", "query_id": "query", "env_id": "env"},
                source_format="doubao-turn-export",
            )
        finally:
            service.STORAGE_V2_VALIDATORS[version] = original
        self.assertEqual(validator.calls, 1)


if __name__ == "__main__":
    unittest.main()
