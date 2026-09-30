"""Aggregate HTTP uses the same actual project-authenticated fixture as queries."""

import json
import unittest

import test_trace_query_api as fixture
from scripts.build_api_contract import synthetic_inputs


class TraceAggregateAPITests(unittest.TestCase):
    setUp = fixture.TraceQueryAPITests.setUp

    def test_totals_and_groups_expose_known_coverage_without_creating_work(self):
        trace, _, _ = synthetic_inputs()
        trace["run"]["id"] = "pending"
        self.store.revisions.append("one", json.dumps(trace).encode(), request_key="pending")
        response = self.remote.post(self.url.replace("/query", "/aggregate"),
                                    json={"group_by": "index_state"}, headers=self.read_auth)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["totals"]["matched_revisions"], 2)
        self.assertEqual(result["totals"]["indexed_revision_count"], 1)
        self.assertEqual(result["totals"]["counts"]["records"], len(trace["spans"]))
        pending = next(group for group in result["groups"] if group["value"] == "unindexed")
        self.assertIsNone(pending["metrics"]["counts"]["records"])
        self.assertEqual(result["watermark"]["kind"], "coverage")
        self.assertEqual(result["consistency"], "statement_snapshot")
        self.assertEqual(result["totals"]["error_rate"]["basis"], "indexed_records_with_known_outcome")
        self.assertEqual(self.store.repository.rows("SELECT count(*) AS n FROM analysis_results")[0]["n"], 0)

    def test_project_filter_and_trusted_peer_boundary_apply_before_data_access(self):
        url = self.url.replace("/query", "/aggregate")
        self.assertEqual(self.remote.post(url.replace("/one/", "/two/"), json={}, headers=self.read_auth).status_code, 200)
        self.assertEqual(self.untrusted.post(url, json={}).status_code, 403)
        empty = self.remote.post(url, json={"filters": {"run_id": ["absent"]}}, headers=self.read_auth).json()
        self.assertEqual(empty["totals"]["counts"]["records"], 0)
        self.assertIsNone(empty["totals"]["error_rate"]["value"])
        self.assertEqual(empty["groups"], [])

    def test_invalid_grouping_or_limits_are_not_silently_ignored(self):
        for body in ({"group_by": "payload"}, {"limit": 51}, {"limit": True}, {"sql": "SELECT 1"},
                     {"filters": {"model": None}}):
            with self.subTest(body=body):
                response = self.remote.post(self.url.replace("/query", "/aggregate"), json=body, headers=self.read_auth)
                self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
