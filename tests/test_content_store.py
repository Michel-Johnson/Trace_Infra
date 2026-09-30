"""Byte preservation, failure recovery and concurrent immutable publication."""

import hashlib
import io
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from trace_hunter.content import ContentCorruption, ContentLimitExceeded, ContentRef, LocalContentStore


class ContentStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = LocalContentStore(self.root)

    def test_original_bytes_roundtrip_without_normalization(self):
        original = '{ "说明" : "原文\\n", "n": 1.00 }\n'.encode()
        ref = self.store.put_bytes(original, media_type="application/json")
        self.assertEqual(ref.digest, "sha256:" + hashlib.sha256(original).hexdigest())
        self.assertEqual(ref.size_bytes, len(original))
        self.assertEqual(self.store.read_bytes(ref), original)

    def test_concurrent_uploads_publish_one_content_object(self):
        original = b"a" * 150_000
        with ThreadPoolExecutor(max_workers=8) as pool:
            refs = list(pool.map(lambda _: self.store.put_bytes(original), range(24)))
        self.assertEqual(len(set(refs)), 1)
        self.assertEqual(len([p for p in self.root.rglob("*") if p.is_file()]), 1)
        self.assertEqual(self.store.read_bytes(refs[0]), original)

    def test_empty_content_is_known_zero_not_missing(self):
        ref = self.store.put_bytes(b"")
        self.assertEqual(ref.size_bytes, 0)
        self.assertEqual(self.store.read_bytes(ref, max_bytes=0), b"")

    def test_interrupted_stream_does_not_publish_or_leave_upload(self):
        class Broken(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError("source disconnected")
                return super().read(3)
        with self.assertRaises(OSError):
            self.store.put(Broken(b"unfinished"))
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_upload_and_read_byte_limits(self):
        with self.assertRaises(ContentLimitExceeded):
            self.store.put(io.BytesIO(b"abcd"), max_bytes=3)
        self.assertEqual(list(self.root.rglob("*")), [])
        ref = self.store.put_bytes(b"abcd")
        with self.assertRaises(ContentLimitExceeded):
            self.store.read_bytes(ref, max_bytes=3)

    def test_corruption_is_detected_and_not_silently_overwritten(self):
        ref = self.store.put_bytes(b"correct")
        self.store._path(ref).write_bytes(b"changed")
        with self.assertRaises(ContentCorruption):
            self.store.read_bytes(ref)
        with self.assertRaises(ContentCorruption):
            self.store.put_bytes(b"correct")
        self.assertEqual(self.store._path(ref).read_bytes(), b"changed")

    def test_wrong_reference_size_and_missing_object_are_distinct(self):
        ref = self.store.put_bytes(b"correct")
        with self.assertRaises(ContentCorruption):
            self.store.read_bytes(replace(ref, size_bytes=1))
        missing = ContentRef("sha256:" + "a" * 64, 7)
        with self.assertRaises(FileNotFoundError):
            self.store.read_bytes(missing)

    def test_digest_cannot_address_an_arbitrary_path(self):
        for digest in ("../secret", "sha256:../secret", "md5:" + "a"*64):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                ContentRef(digest, 1)

    def test_separate_provider_instances_share_existing_content(self):
        ref = self.store.put_bytes(b"persisted")
        reopened = LocalContentStore(self.root)
        self.assertEqual(reopened.read_bytes(ref), b"persisted")
        self.assertEqual(reopened.put_bytes(b"persisted"), ref)


if __name__ == "__main__":
    unittest.main()
