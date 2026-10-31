import os
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace
import stat

from agentforge.errors import ScanError
from agentforge.safeio import open_source
from agentforge.scanner import (
    ScanCandidate, ScanLimits, ScanReadError, read_candidate, scan_repository,
)
from tests.support import LocalTestCase, make_junction


class ScannerTests(LocalTestCase):
    def test_reparse_and_symlink_metadata_reject_without_recursion(self):
        for mode, attributes in ((stat.S_IFDIR, 0x400), (stat.S_IFLNK, 0)):
            entry = Mock()
            entry.name = "link"
            entry.path = str(self.root / "link")
            entry.stat.return_value = SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
            listing = Mock()
            listing.__enter__ = Mock(return_value=iter([entry]))
            listing.__exit__ = Mock(return_value=False)
            with patch("agentforge.scanner.os.scandir", return_value=listing) as scanned:
                report = scan_repository(self.root)
            self.assertEqual(report.candidates, [])
            self.assertEqual(report.issues[0].reason, "reparse_point")
            scanned.assert_called_once_with(self.root)

    def test_explicit_root_and_relative_paths(self):
        self.write("src/example.py", "def example():\n    return 1\n")
        outside = self.sandbox / "outside.py"
        outside.write_text("outside", encoding="utf-8")
        result = scan_repository(self.root)
        self.assertEqual([item.relative_path for item in result.candidates], ["src/example.py"])
        self.assertIn("def example", read_candidate(result.candidates[0], result.root))
        with self.assertRaises(ScanError):
            scan_repository(self.root / "missing")
        with self.assertRaises(ScanError):
            scan_repository(outside)

    def test_sensitive_build_database_and_model_names_excluded_before_read(self):
        paths = [
            ".env", ".env.local", "production.env", "credentials.json", "private.pem",
            "certificate.crt", "id_ed25519", "model.gguf", "weights.safetensors", "index.sqlite3",
            "index.sqlite3-wal", ".git/config", ".venv/lib.py", "node_modules/index.js",
            "build/result.py", "__pycache__/cache.pyc", ".agentforge/data/value.txt",
            "secrets/private.txt",
        ]
        for path in paths:
            self.write(path, "FAKE SYNTHETIC TEST DATA\n")
        self.write("src/allowed.py", "allowed = True\n")
        with patch("agentforge.scanner.open_source", side_effect=AssertionError("Scan must not read file bytes")):
            report = scan_repository(self.root)
        self.assertEqual([item.relative_path for item in report.candidates], ["src/allowed.py"])
        self.assertTrue(report.complete)
        self.assertGreaterEqual(len(report.issues), 15)

    def test_binary_bad_encoding_and_suspected_inline_secret(self):
        self.write("binary.dat", b"text\x00payload")
        self.write("bad.txt", b"\xff\xfe\xff")
        self.write("config.txt", 'api_key = "FAKE_SYNTHETIC_VALUE_123456"\n')
        self.write("utf8.txt", b"\xef\xbb\xbfvalid utf8\n")
        report = scan_repository(self.root)
        reasons = {}
        for candidate in report.candidates:
            try:
                text = read_candidate(candidate, report.root)
                self.assertEqual(text, "valid utf8\n")
            except ScanReadError as exc:
                reasons[candidate.relative_path] = exc.reason
        self.assertEqual(reasons, {
            "binary.dat": "binary_content", "bad.txt": "unsupported_encoding",
            "config.txt": "secret_like_content",
        })

    def test_file_total_count_and_entry_limits(self):
        self.write("a.txt", "aaaa")
        self.write("b.txt", "bbbb")
        self.write("huge.txt", "x" * 50)
        report = scan_repository(self.root, ScanLimits(max_file_bytes=10, max_total_bytes=6))
        self.assertEqual([item.relative_path for item in report.candidates], ["a.txt"])
        self.assertEqual({item.reason for item in report.issues}, {"file_size_limit", "total_size_limit"})
        self.assertFalse(scan_repository(self.root, ScanLimits(max_files=1)).complete)
        self.assertFalse(scan_repository(self.root, ScanLimits(max_entries=1)).complete)
        for kwargs in ({"max_files": 0}, {"max_total_bytes": -1}, {"max_entries": 0}):
            with self.assertRaises(ValueError):
                ScanLimits(**kwargs)

    def test_path_traversal_and_forged_candidate_rejected(self):
        outside = self.sandbox / "outside.py"
        outside.write_text("outside", encoding="utf-8")
        for relative in ("../outside.py", "outside.py"):
            candidate = ScanCandidate(relative, outside, 7, 0)
            with self.assertRaises(ScanReadError):
                read_candidate(candidate, self.root)

    def test_unreadable_file_has_safe_reason(self):
        self.write("ordinary.py", "example\n")
        report = scan_repository(self.root)
        with patch("agentforge.scanner.open_source", side_effect=PermissionError("hidden detail")):
            with self.assertRaisesRegex(ScanReadError, "^unreadable_file$"):
                read_candidate(report.candidates[0], report.root)

    def test_file_symbolic_link_escape(self):
        outside = self.sandbox / "outside.py"
        outside.write_text("outside", encoding="utf-8")
        link = self.root / "linked.py"
        try:
            os.symlink(outside, link)
        except OSError as exc:
            self.skipTest(f"Cannot create file symlink: {exc}")
        self.addCleanup(link.unlink)
        report = scan_repository(self.root)
        self.assertEqual(report.candidates, [])
        self.assertEqual(report.issues[0].reason, "reparse_point")

    def test_directory_symbolic_link_escape_and_link_root(self):
        outside = self.sandbox / "outside"
        outside.mkdir()
        (outside / "escape.py").write_text("outside", encoding="utf-8")
        link = self.root / "linked"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Cannot create directory symlink: {exc}")
        self.addCleanup(link.unlink)
        report = scan_repository(self.root)
        self.assertEqual(report.candidates, [])
        self.assertEqual(report.issues[0].reason, "reparse_point")
        with self.assertRaises(ScanError):
            scan_repository(link)

    def test_windows_junction_escape_and_opened_handle_boundary(self):
        outside = self.sandbox / "outside"
        outside.mkdir()
        (outside / "escape.py").write_text("outside", encoding="utf-8")
        link = self.root / "junction"
        try:
            make_junction(link, outside)
        except OSError as exc:
            self.skipTest(f"Cannot construct a Windows junction: {exc}")
        self.addCleanup(os.rmdir, link)
        report = scan_repository(self.root)
        self.assertEqual(report.candidates, [])
        self.assertEqual(report.issues[0].reason, "reparse_point")
        with self.assertRaises(ScanError):
            scan_repository(link)
        with self.assertRaises(OSError):
            open_source(self.root, link / "escape.py")
        self.assertEqual((outside / "escape.py").read_text(encoding="utf-8"), "outside")

    def test_hard_link_is_excluded(self):
        outside = self.sandbox / "outside.py"
        outside.write_text("outside", encoding="utf-8")
        link = self.root / "linked.py"
        try:
            os.link(outside, link)
        except OSError as exc:
            self.skipTest(f"Cannot create a hard link: {exc}")
        report = scan_repository(self.root)
        self.assertEqual(report.candidates, [])
        self.assertEqual(report.issues[0].reason, "hard_link")

    def test_scan_rejects_missing_link_metadata_from_enumeration(self):
        source = self.write("ordinary.py", "ordinary = True\n")
        actual_scandir = os.scandir

        class EntryWithIncompleteMetadata:
            def __init__(self, entry):
                self.name, self.path = entry.name, entry.path

            def stat(self, *, follow_symlinks=True):
                real = source.stat()
                return SimpleNamespace(st_mode=real.st_mode, st_file_attributes=0,
                                       st_nlink=0, st_dev=0, st_ino=0,
                                       st_size=real.st_size, st_mtime_ns=real.st_mtime_ns)

        class Listing:
            def __enter__(self):
                with actual_scandir(self_root) as entries:
                    return iter([EntryWithIncompleteMetadata(entry) for entry in entries])

            def __exit__(self, *_):
                return False

        self_root = self.root
        with patch("agentforge.scanner.os.scandir", return_value=Listing()):
            report = scan_repository(self.root)
        self.assertEqual([item.relative_path for item in report.candidates], ["ordinary.py"])
        self.assertEqual((report.candidates[0].device, report.candidates[0].inode),
                         (source.stat().st_dev, source.stat().st_ino))
        self.assertEqual(read_candidate(report.candidates[0], report.root).splitlines(), ["ordinary = True"])

    def test_regular_file_replacement_after_scan_is_rejected(self):
        original = self.write("source.py", "original\n")
        report = scan_repository(self.root)
        replacement = self.sandbox / "replacement.py"
        replacement.write_text("changed!\n", encoding="utf-8")
        os.replace(replacement, original)
        with self.assertRaises(ScanReadError):
            read_candidate(report.candidates[0], report.root)

    def test_unverifiable_file_identity_is_excluded(self):
        source = self.write("ordinary.py", "ordinary = True\n")
        original_lstat = Path.lstat
        actual = source.stat()

        def lstat_with_missing_identity(path):
            if path == source:
                return SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=0,
                                       st_nlink=0, st_dev=0, st_ino=0,
                                       st_size=actual.st_size, st_mtime_ns=actual.st_mtime_ns)
            return original_lstat(path)

        with patch("pathlib.Path.lstat", lstat_with_missing_identity):
            report = scan_repository(self.root)
        self.assertEqual(report.candidates, [])
        self.assertEqual(report.issues[0].reason, "unverifiable_file_identity")

    def test_hard_link_added_after_scan_is_rejected_before_read(self):
        source = self.write("source.py", "ordinary\n")
        report = scan_repository(self.root)
        external_link = self.sandbox / "external_link.py"
        try:
            os.link(source, external_link)
        except OSError as exc:
            self.skipTest(f"Cannot create a hard link: {exc}")
        with self.assertRaises(ScanReadError) as caught:
            read_candidate(report.candidates[0], report.root)
        self.assertEqual(caught.exception.reason, "file_changed_or_hard_link")

    def test_link_substitution_after_scan_is_rejected(self):
        original = self.write("source.py", "original\n")
        report = scan_repository(self.root)
        outside = self.sandbox / "outside.py"
        outside.write_text("outside", encoding="utf-8")
        original.unlink()
        try:
            os.symlink(outside, original)
        except OSError as exc:
            self.skipTest(f"Cannot create substitution symlink: {exc}")
        self.addCleanup(original.unlink)
        with self.assertRaises(ScanReadError):
            read_candidate(report.candidates[0], report.root)

