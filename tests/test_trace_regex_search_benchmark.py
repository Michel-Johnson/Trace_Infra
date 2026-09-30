import json
import unittest

from benchmarks.trace_regex_search import (
    INDEXES, QUERIES, AdaptiveNGramIndex, Document, ScanIndex, benchmark,
    generate_corpus, lark_cli_anomalies, mandatory_literals, repeated_benchmark,
    sparse_ngrams,
)
from trace_hunter.protocol import validate


class TraceRegexSearchBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = generate_corpus(8, 2_000, 4_800)

    def test_corpus_is_valid_and_has_distinct_lengths(self):
        lengths = []
        for document in self.documents:
            value = json.loads(document.text)
            validate(value)
            lengths.append(len(document.text.encode()))
        self.assertEqual(len(self.documents), 8)
        self.assertEqual(len(set(lengths)), 8)
        self.assertLess(min(lengths), max(lengths))

    def test_candidate_indexes_preserve_exact_regex_results(self):
        baseline = ScanIndex(self.documents)
        expected = {query.name: baseline.search(query.pattern)[0] for query in QUERIES}
        for index_type in INDEXES[1:]:
            index = index_type(self.documents)
            for query in QUERIES:
                with self.subTest(index=index.name, query=query.name):
                    matches, candidates = index.search(query.pattern)
                    self.assertEqual(matches, expected[query.name])
                    self.assertGreaterEqual(candidates, len(matches))

    def test_regex_literal_extraction_is_conservative(self):
        self.assertEqual(mandatory_literals(r"error.*timeout"), ("timeout", "error"))
        self.assertIn("lark-", mandatory_literals(r"lark-(?:cli|task)"))
        self.assertEqual(mandatory_literals(r"[a-z]+"), ())

    def test_sparse_ngrams_are_deterministic(self):
        first = sparse_ngrams("lark-cli timeout")
        self.assertEqual(first, sparse_ngrams("lark-cli timeout"))
        self.assertTrue(all(3 <= len(value) <= 12 for value, _, _ in first))

    def test_anomaly_stage_uses_structured_duration(self):
        documents = generate_corpus(50, 1_500, 3_000)
        matches, _ = ScanIndex(documents).search(r"lark-cli")
        result = lark_cli_anomalies(documents, matches)
        self.assertEqual([row["doc_id"] for row in result["matches"]], [17, 45])

    def test_adaptive_ngram_can_filter_two_character_literals(self):
        documents = [Document(0, "a", "alpha xy"), Document(1, "b", "beta zz")]
        index = AdaptiveNGramIndex(documents)
        self.assertEqual(index.search("xy"), ({0}, 1))

    def test_benchmark_scores_only_exact_candidate_indexes(self):
        result = benchmark(self.documents, iterations=2)
        scored = [row for row in result["algorithms"] if row["score"] is not None]
        self.assertEqual(len(result["ranking"]), len(scored))
        self.assertEqual({row["accuracy"] for row in scored}, {"exact"})
        self.assertTrue(all(0 < row["score"] <= 100 for row in scored))
        self.assertEqual(
            result["ranking"],
            [row["algorithm"] for row in sorted(
                scored, key=lambda row: (-row["score"], row["algorithm"]))],
        )

    def test_repeated_benchmark_uses_requested_round_count(self):
        result = repeated_benchmark(self.documents, iterations=1, rounds=2)
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(len(result["ranking"]), len(INDEXES) - 1)


if __name__ == "__main__":
    unittest.main()
