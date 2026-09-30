"""Compare regex candidate indexes on deterministic Trace Hunter documents.

The prototypes follow the designs discussed in Cursor's "Fast regex search"
article.  Every index is only a candidate generator: the final result always
comes from Python's regex engine over the immutable document text.
"""

from __future__ import annotations

import argparse
import copy
import functools
import json
import math
import re
import statistics
import time
import warnings
import zlib
from array import array
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATHS = (
    ROOT / "examples/minimal.trace.json",
    ROOT / "examples/base-building-demo/plan.trace.json",
    ROOT / "examples/base-building-demo/spec.trace.json",
    ROOT / "examples/doubao-orange-web.trace.json",
    ROOT / "examples/claude-orange.trace.json",
)


@dataclass(frozen=True)
class Document:
    doc_id: int
    run_id: str
    text: str


@dataclass(frozen=True)
class Query:
    name: str
    pattern: str


QUERIES = (
    Query("skill_literal", r"lark-cli"),
    Query("skill_command", r"lark-cli\s+(?:base|task|doc)"),
    Query("tool_error", r"(?:ERROR|failed|timeout)"),
    Query("json_field", r'"operation":"(?:bash|skill)"'),
    Query("path_fragment", r"/api/v1/[a-z-]+"),
    Query("short_unindexable", r"id"),
)


def _sample_text() -> str:
    chunks = []
    for path in SAMPLE_PATHS:
        value = path.read_text(errors="replace")
        chunks.append(value.replace("lark-cli", "lark_cli_controlled"))
    return "\n".join(chunks)


def _tool_span(index: int, document_index: int, payload: str, *, lark_cli: bool) -> dict:
    start = index * 100
    duration = (20 + (document_index * 17 + index * 11) % 180)
    if lark_cli and document_index in (17, 45):
        duration = 12_000 + document_index * 100
    command = (
        f"lark-cli base search --trace bench-{document_index:02d}"
        if lark_cli else f"python worker.py --trace bench-{document_index:02d} --step {index}"
    )
    status = "error" if (document_index + index) % 19 == 0 else "ok"
    output = (
        "ERROR timeout while reading remote trace"
        if status == "error" else f"completed step {index}"
    )
    span = {
        "id": f"tool-{index}", "kind": "tool", "name": "Skill" if lark_cli else "Bash",
        "operation": "skill" if lark_cli else "bash", "agent_id": "main", "parent_id": None,
        "request_id": None, "attempt": 1, "phase_id": "task", "start_ms": start,
        "end_ms": start + duration, "duration_ms": None, "status": status,
        "input": {"command": command, "payload": payload}, "output": output, "usage": None,
        "source": {"source_id": "original", "pointer": f"/synthetic/{index}"},
        "sequence": index + 1, "order_basis": "source_sequence",
    }
    if lark_cli:
        span["skill"] = {"name": "lark-cli", "action": "invoke"}
    return span


def generate_corpus(count: int = 50, min_bytes: int = 4_096,
                    max_bytes: int = 24_576) -> list[Document]:
    """Generate valid v1.1 traces with distinct, steadily increasing lengths."""
    if count < 1 or min_bytes < 1 or max_bytes < min_bytes:
        raise ValueError("invalid corpus bounds")
    template = json.loads((ROOT / "examples/minimal.trace.json").read_text())
    source = _sample_text()
    documents = []
    used_lengths = set()
    for document_index in range(count):
        target = min_bytes if count == 1 else round(
            min_bytes + (max_bytes - min_bytes) * document_index / (count - 1))
        trace = copy.deepcopy(template)
        trace["run"].update(
            id=f"regex-bench-{document_index:02d}", query_id=f"regex-query-{document_index:02d}",
            title=f"Regex benchmark trace {document_index:02d}", query="Search trace text",
            harness="Regex benchmark", model="Synthetic deterministic",
        )
        trace["collector"] = {"name": "regex-benchmark", "version": "1.0"}
        trace["spans"], trace["links"], trace["evidence"] = [], [], []
        cursor = document_index * 997 % len(source)
        span_index = 0
        while True:
            current = json.dumps(trace, ensure_ascii=True, separators=(",", ":"))
            remaining = target - len(current.encode())
            if remaining <= 700 and trace["spans"]:
                break
            payload_size = max(128, min(900, remaining - 450))
            payload = (source[cursor:] + source[:cursor])[:payload_size]
            cursor = (cursor + payload_size + 131) % len(source)
            lark_cli = document_index % 4 == 1 and span_index == 0
            trace["spans"].append(_tool_span(
                span_index, document_index, payload, lark_cli=lark_cli))
            span_index += 1
        trace["phases"][0]["end_ms"] = max(
            (span["end_ms"] for span in trace["spans"]), default=0)
        text = json.dumps(trace, ensure_ascii=True, separators=(",", ":"))
        while len(text.encode()) in used_lengths:
            trace["environment"]["notes"] += " "
            text = json.dumps(trace, ensure_ascii=True, separators=(",", ":"))
        used_lengths.add(len(text.encode()))
        documents.append(Document(document_index, trace["run"]["id"], text))
    lengths = [len(document.text.encode()) for document in documents]
    if len(set(lengths)) != len(lengths):
        raise AssertionError("benchmark trace lengths must be distinct")
    return documents


@functools.lru_cache(maxsize=512)
def mandatory_literals(pattern: str) -> tuple[str, ...]:
    """Return conservative literals that every regex match must contain."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import sre_parse  # Python still ships this parser; benchmark only.

    def walk(sequence) -> list[str]:
        result, literal = [], []

        def flush():
            if literal:
                result.append("".join(literal))
                literal.clear()

        for operation, argument in sequence:
            if operation is sre_parse.LITERAL:
                literal.append(chr(argument))
                continue
            flush()
            if operation is sre_parse.SUBPATTERN:
                result.extend(walk(argument[-1]))
            elif operation is sre_parse.BRANCH:
                branches = [walk(branch) for branch in argument[1]]
                common = set(branches[0]) if branches else set()
                for branch in branches[1:]:
                    common.intersection_update(branch)
                result.extend(sorted(common))
                if branches and all(len(branch) == 1 for branch in branches):
                    prefix = _common_prefix([branch[0] for branch in branches])
                    if prefix:
                        result.append(prefix)
            elif operation in (sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT) and argument[0] >= 1:
                result.extend(walk(argument[2]))
        flush()
        return result

    return tuple(sorted({literal for literal in walk(sre_parse.parse(pattern)) if literal},
                        key=lambda value: (-len(value), value)))


def _common_prefix(values: list[str]) -> str:
    if not values:
        return ""
    prefix = values[0]
    for value in values[1:]:
        while prefix and not value.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


def _trigrams(value: str):
    return (value[index:index + 3] for index in range(max(0, len(value) - 2)))


def _ngrams(value: str, size: int):
    return (value[index:index + size] for index in range(max(0, len(value) - size + 1)))


class SearchIndex:
    name = "base"

    def __init__(self, documents: list[Document]):
        self.documents = documents
        self.all_ids = set(range(len(documents)))

    def candidates(self, pattern: str) -> set[int]:
        return set(self.all_ids)

    def search(self, pattern: str) -> tuple[set[int], int]:
        candidate_ids = self.candidates(pattern)
        expression = re.compile(pattern)
        return ({doc_id for doc_id in candidate_ids
                 if expression.search(self.documents[doc_id].text)}, len(candidate_ids))

    def logical_bytes(self) -> int:
        return 0


class ScanIndex(SearchIndex):
    name = "scan"


class TrigramIndex(SearchIndex):
    name = "trigram"

    def __init__(self, documents: list[Document]):
        super().__init__(documents)
        self.postings: dict[str, set[int]] = defaultdict(set)
        for document in documents:
            for gram in set(_trigrams(document.text)):
                self.postings[gram].add(document.doc_id)

    def _literal_candidates(self, literals: tuple[str, ...]) -> set[int]:
        grams = [gram for literal in literals if len(literal) >= 3
                 for gram in _trigrams(literal)]
        if not grams:
            return set(self.all_ids)
        result = set(self.all_ids)
        for gram in sorted(set(grams), key=lambda item: len(self.postings.get(item, ()))):
            result.intersection_update(self.postings.get(gram, ()))
            if not result:
                break
        return result

    def candidates(self, pattern: str) -> set[int]:
        return self._literal_candidates(mandatory_literals(pattern))

    def logical_bytes(self) -> int:
        return sum(len(gram.encode()) + 4 * len(ids) for gram, ids in self.postings.items())


class AdaptiveNGramIndex(SearchIndex):
    """Use the rarest available 2-5 gram for each mandatory literal.

    This trades index space for fewer posting-list intersections.  Exact regex
    verification remains the authority, so the selected gram may only add false
    positives and can never remove a real match.
    """

    name = "adaptive_ngram"
    sizes = (2, 3, 4, 5)

    def __init__(self, documents: list[Document]):
        super().__init__(documents)
        self.postings: dict[int, dict[str, set[int]]] = {
            size: defaultdict(set) for size in self.sizes}
        for document in documents:
            for size in self.sizes:
                for gram in set(_ngrams(document.text, size)):
                    self.postings[size][gram].add(document.doc_id)

    def _best_gram(self, literal: str) -> tuple[int, str] | None:
        options = (
            (len(self.postings[size].get(gram, ())), -size, gram, size)
            for size in self.sizes if len(literal) >= size
            for gram in _ngrams(literal, size)
        )
        try:
            _, _, gram, size = min(options)
        except ValueError:
            return None
        return size, gram

    def candidates(self, pattern: str) -> set[int]:
        selected = [self._best_gram(literal) for literal in mandatory_literals(pattern)]
        selected = [item for item in selected if item is not None]
        if not selected:
            return set(self.all_ids)
        result = set(self.all_ids)
        for size, gram in sorted(
                selected, key=lambda item: len(self.postings[item[0]].get(item[1], ()))):
            result.intersection_update(self.postings[size].get(gram, ()))
            if not result:
                break
        return result

    def logical_bytes(self) -> int:
        return sum(
            len(gram.encode()) + 4 * len(ids)
            for postings in self.postings.values() for gram, ids in postings.items())


_TOKEN = re.compile(r"[\w./:-]+", re.UNICODE)


class TokenTrigramIndex(TrigramIndex):
    """Combine exact token postings with trigram fallback for arbitrary regex."""

    name = "token_trigram"

    def __init__(self, documents: list[Document]):
        super().__init__(documents)
        self.tokens: dict[str, set[int]] = defaultdict(set)
        for document in documents:
            for token in set(_TOKEN.findall(document.text)):
                self.tokens[token].add(document.doc_id)

    def candidates(self, pattern: str) -> set[int]:
        literals = mandatory_literals(pattern)
        token_postings = [self.tokens[literal] for literal in literals
                          if _TOKEN.fullmatch(literal) and literal in self.tokens]
        if token_postings:
            result = set(min(token_postings, key=len))
            for posting in token_postings:
                result.intersection_update(posting)
            return result
        return self._literal_candidates(literals)

    def logical_bytes(self) -> int:
        token_bytes = sum(len(token.encode()) + 4 * len(ids)
                          for token, ids in self.tokens.items())
        return super().logical_bytes() + token_bytes


class SelectiveTokenTrigramIndex(TrigramIndex):
    """Index only path/command-like tokens, then fall back to trigrams.

    Plain JSON keys are already handled well by trigrams.  Restricting the token
    sidecar to punctuation-bearing values keeps the hybrid's storage overhead
    focused on skill names, commands, URLs and paths.
    """

    name = "selective_token_trigram"
    punctuation = frozenset("./:-")

    def __init__(self, documents: list[Document]):
        super().__init__(documents)
        self.tokens: dict[str, set[int]] = defaultdict(set)
        for document in documents:
            for token in set(_TOKEN.findall(document.text)):
                if 4 <= len(token) <= 160 and self.punctuation.intersection(token):
                    self.tokens[token].add(document.doc_id)

    def candidates(self, pattern: str) -> set[int]:
        literals = mandatory_literals(pattern)
        postings = [self.tokens[literal] for literal in literals if literal in self.tokens]
        if postings:
            result = set(min(postings, key=len))
            for posting in postings:
                result.intersection_update(posting)
            return result
        return self._literal_candidates(literals)

    def logical_bytes(self) -> int:
        return super().logical_bytes() + sum(
            len(token.encode()) + 4 * len(ids) for token, ids in self.tokens.items())


def _rotate_right(mask: int, shift: int) -> int:
    shift %= 8
    return ((mask >> shift) | (mask << (8 - shift))) & 0xFF if shift else mask


def _next_bit(character: str) -> int:
    return 1 << (zlib.crc32(character.encode()) & 7)


class MaskedTrigramIndex(SearchIndex):
    name = "masked_trigram"

    def __init__(self, documents: list[Document]):
        super().__init__(documents)
        self.postings: dict[str, dict[int, tuple[int, int]]] = defaultdict(dict)
        for document in documents:
            masks: dict[str, list[int]] = defaultdict(lambda: [0, 0])
            for position, gram in enumerate(_trigrams(document.text)):
                masks[gram][0] |= 1 << (position & 7)
                if position + 3 < len(document.text):
                    masks[gram][1] |= _next_bit(document.text[position + 3])
            for gram, (location, following) in masks.items():
                self.postings[gram][document.doc_id] = (location, following)

    def candidates(self, pattern: str) -> set[int]:
        literals = [literal for literal in mandatory_literals(pattern) if len(literal) >= 3]
        grams = [gram for literal in literals for gram in _trigrams(literal)]
        if not grams:
            return set(self.all_ids)
        result = set(self.all_ids)
        for gram in sorted(set(grams), key=lambda item: len(self.postings.get(item, ()))):
            result.intersection_update(self.postings.get(gram, ()))
        for document_id in tuple(result):
            for literal in literals:
                aligned = 0xFF
                literal_grams = list(_trigrams(literal))
                for offset, gram in enumerate(literal_grams):
                    location, following = self.postings[gram][document_id]
                    aligned &= _rotate_right(location, offset)
                    if offset + 3 < len(literal) and not following & _next_bit(literal[offset + 3]):
                        aligned = 0
                    if not aligned:
                        break
                if not aligned:
                    result.remove(document_id)
                    break
        return result

    def logical_bytes(self) -> int:
        return sum(len(gram.encode()) + 6 * len(rows) for gram, rows in self.postings.items())


def _pair_weight(value: str) -> int:
    return zlib.crc32(value.encode())


def sparse_ngrams(value: str, max_chars: int = 12) -> list[tuple[str, int, int]]:
    """Extract bounded sparse n-grams using deterministic endpoint weights."""
    if len(value) < 3:
        return []
    weights = [_pair_weight(value[index:index + 2]) for index in range(len(value) - 1)]
    result = []
    for left in range(len(weights) - 1):
        internal_max = -1
        for right in range(left + 1, min(len(weights), left + max_chars - 1)):
            if right > left + 1:
                internal_max = max(internal_max, weights[right - 1])
            if weights[left] > internal_max and weights[right] > internal_max:
                result.append((value[left:right + 2], left, right + 2))
    return result


class SparseNGramIndex(SearchIndex):
    name = "sparse_ngram"

    def __init__(self, documents: list[Document]):
        super().__init__(documents)
        self.postings: dict[str, set[int]] = defaultdict(set)
        for document in documents:
            for gram, _, _ in set(sparse_ngrams(document.text)):
                self.postings[gram].add(document.doc_id)

    def _covering(self, literal: str) -> list[str]:
        options = sparse_ngrams(literal)
        uncovered = set(range(len(literal)))
        selected = []
        while uncovered and options:
            best = min(options, key=lambda item: (
                -len(uncovered.intersection(range(item[1], item[2]))),
                len(self.postings.get(item[0], self.all_ids)), -len(item[0]), item[0]))
            covered = uncovered.intersection(range(best[1], best[2]))
            if not covered:
                break
            selected.append(best[0])
            uncovered.difference_update(covered)
            options.remove(best)
        return selected

    def candidates(self, pattern: str) -> set[int]:
        grams = [gram for literal in mandatory_literals(pattern) if len(literal) >= 3
                 for gram in self._covering(literal)]
        if not grams:
            return set(self.all_ids)
        result = set(self.all_ids)
        for gram in sorted(set(grams), key=lambda item: len(self.postings.get(item, ()))):
            result.intersection_update(self.postings.get(gram, ()))
        return result

    def logical_bytes(self) -> int:
        return sum(len(gram.encode()) + 4 * len(ids) for gram, ids in self.postings.items())


def _suffix_array(text: bytes) -> array:
    size = len(text)
    if not size:
        return array("I")
    suffixes = list(range(size))
    ranks = list(text)
    step = 1
    while step < size:
        suffixes.sort(key=lambda index: (
            ranks[index], ranks[index + step] if index + step < size else -1))
        updated = [0] * size
        for position in range(1, size):
            previous, current = suffixes[position - 1], suffixes[position]
            previous_key = (ranks[previous], ranks[previous + step] if previous + step < size else -1)
            current_key = (ranks[current], ranks[current + step] if current + step < size else -1)
            updated[current] = updated[previous] + (current_key != previous_key)
        ranks = updated
        if ranks[suffixes[-1]] == size - 1:
            break
        step *= 2
    return array("I", suffixes)


def _suffix_contains(text: bytes, suffixes: array, literal: bytes) -> bool:
    low, high = 0, len(suffixes)
    while low < high:
        middle = (low + high) // 2
        fragment = text[suffixes[middle]:suffixes[middle] + len(literal)]
        if fragment < literal:
            low = middle + 1
        else:
            high = middle
    return low < len(suffixes) and text[suffixes[low]:suffixes[low] + len(literal)] == literal


class SuffixArrayIndex(SearchIndex):
    name = "suffix_array"

    def __init__(self, documents: list[Document]):
        super().__init__(documents)
        self.encoded = [document.text.encode() for document in documents]
        self.suffixes = [_suffix_array(text) for text in self.encoded]

    def candidates(self, pattern: str) -> set[int]:
        literals = [literal.encode() for literal in mandatory_literals(pattern) if literal]
        if not literals:
            return set(self.all_ids)
        return {document_id for document_id, (text, suffixes) in enumerate(
            zip(self.encoded, self.suffixes, strict=True))
            if all(_suffix_contains(text, suffixes, literal) for literal in literals)}

    def logical_bytes(self) -> int:
        return sum(item.itemsize * len(item) for item in self.suffixes)


INDEXES = (
    ScanIndex, TrigramIndex, AdaptiveNGramIndex, TokenTrigramIndex,
    SelectiveTokenTrigramIndex,
    MaskedTrigramIndex, SparseNGramIndex, SuffixArrayIndex,
)


def lark_cli_anomalies(documents: list[Document], matched: set[int]) -> dict:
    rows = []
    for document_id in sorted(matched):
        trace = json.loads(documents[document_id].text)
        for span in trace["spans"]:
            if span.get("skill", {}).get("name") == "lark-cli":
                duration = span["end_ms"] - span["start_ms"]
                rows.append((document_id, span["id"], duration))
    durations = [row[2] for row in rows]
    if not durations:
        return {"threshold_ms": None, "matches": []}
    median = statistics.median(durations)
    deviations = [abs(value - median) for value in durations]
    mad = statistics.median(deviations)
    threshold = median + max(1, 6 * mad)
    return {"threshold_ms": threshold, "matches": [
        {"doc_id": doc_id, "span_id": span_id, "duration_ms": duration}
        for doc_id, span_id, duration in rows if duration > threshold]}


def percentile(values: list[int], ratio: float) -> float:
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return ordered[position] / 1_000


def score_algorithms(results: list[dict]) -> None:
    """Attach a 0-100 relative score; exactness is a hard gate.

    Latency dominates, while candidate count, build cost and logical index size
    prevent a query-only micro-optimization from winning the experiment.
    """
    indexed = [row for row in results if row["algorithm"] != "scan"]
    scalar_metrics = {
        "build": lambda row: max(row["build_ms"], 0.001),
        "memory": lambda row: max(row["logical_index_bytes"], 1),
    }
    weights = {"p50": 0.35, "p95": 0.20, "candidates": 0.20,
               "build": 0.10, "memory": 0.15}
    best_scalar = {name: min(measure(row) for row in indexed)
                   for name, measure in scalar_metrics.items()}
    best_query = {
        metric: [min(max(row["queries"][position][metric], 0.001) for row in indexed)
                 for position in range(len(indexed[0]["queries"]))]
        for metric in ("p50_us", "p95_us", "candidates")
    }
    for row in results:
        row["accuracy"] = "exact"
        if row["algorithm"] == "scan":
            row["score"] = None
            row["score_components"] = None
            continue
        components = {
            "p50": statistics.mean(
                best / max(query["p50_us"], 0.001)
                for best, query in zip(best_query["p50_us"], row["queries"], strict=True)),
            "p95": statistics.mean(
                best / max(query["p95_us"], 0.001)
                for best, query in zip(best_query["p95_us"], row["queries"], strict=True)),
            "candidates": statistics.mean(
                1.0 if best == query["candidates"] == 0
                else best / max(query["candidates"], 0.001)
                for best, query in zip(
                    best_query["candidates"], row["queries"], strict=True)),
            **{name: best_scalar[name] / measure(row)
               for name, measure in scalar_metrics.items()},
        }
        row["score"] = round(100 * sum(weights[name] * components[name]
                                        for name in weights), 2)
        row["score_components"] = {
            name: round(100 * value, 2) for name, value in components.items()}


def benchmark(documents: list[Document], iterations: int = 100) -> dict:
    if iterations < 1:
        raise ValueError("iterations must be positive")
    results, baseline = [], None
    for index_type in INDEXES:
        started = time.perf_counter_ns()
        index = index_type(documents)
        build_ms = (time.perf_counter_ns() - started) / 1_000_000
        queries = []
        for query in QUERIES:
            timings, answer, candidate_count = [], set(), 0
            for _ in range(iterations):
                started = time.perf_counter_ns()
                answer, candidate_count = index.search(query.pattern)
                timings.append(time.perf_counter_ns() - started)
            if baseline is None:
                pass
            elif answer != baseline[query.name]:
                raise AssertionError(f"{index.name} lost exact results for {query.name}")
            queries.append({
                "name": query.name, "pattern": query.pattern, "matches": len(answer),
                "candidates": candidate_count, "p50_us": percentile(timings, 0.50),
                "p95_us": percentile(timings, 0.95),
            })
        if baseline is None:
            baseline = {query["name"]: index.search(query["pattern"])[0] for query in queries}
        results.append({"algorithm": index.name, "build_ms": round(build_ms, 3),
                        "logical_index_bytes": index.logical_bytes(), "queries": queries})
    score_algorithms(results)
    ranking = [row["algorithm"] for row in sorted(
        (row for row in results if row["score"] is not None),
        key=lambda row: (-row["score"], row["algorithm"]))]
    lark_matches, _ = ScanIndex(documents).search(r"lark-cli")
    lengths = [len(document.text.encode()) for document in documents]
    return {
        "corpus": {"documents": len(documents), "bytes": sum(lengths),
                   "min_bytes": min(lengths), "max_bytes": max(lengths),
                   "distinct_lengths": len(set(lengths))},
        "iterations": iterations, "algorithms": results, "ranking": ranking,
        "lark_cli_anomalies": lark_cli_anomalies(documents, lark_matches),
    }


def repeated_benchmark(documents: list[Document], iterations: int = 100,
                       rounds: int = 3) -> dict:
    if rounds < 1:
        raise ValueError("rounds must be positive")
    runs = [benchmark(documents, iterations) for _ in range(rounds)]
    if rounds == 1:
        result = runs[0]
        result["rounds"] = 1
        return result
    result = copy.deepcopy(runs[0])
    result["rounds"] = rounds
    for position, row in enumerate(result["algorithms"]):
        samples = [run["algorithms"][position] for run in runs]
        row["build_ms"] = round(statistics.median(
            sample["build_ms"] for sample in samples), 3)
        for query_position, query in enumerate(row["queries"]):
            query_samples = [sample["queries"][query_position] for sample in samples]
            query["p50_us"] = round(statistics.median(
                sample["p50_us"] for sample in query_samples), 3)
            query["p95_us"] = round(statistics.median(
                sample["p95_us"] for sample in query_samples), 3)
        row.pop("score", None)
        row.pop("score_components", None)
    score_algorithms(result["algorithms"])
    result["ranking"] = [row["algorithm"] for row in sorted(
        (row for row in result["algorithms"] if row["score"] is not None),
        key=lambda row: (-row["score"], row["algorithm"]))]
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--traces", type=int, default=50)
    parser.add_argument("--min-bytes", type=int, default=4_096)
    parser.add_argument("--max-bytes", type=int, default=24_576)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = repeated_benchmark(
        generate_corpus(args.traces, args.min_bytes, args.max_bytes),
        args.iterations, args.rounds)
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
