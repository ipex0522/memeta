"""外部接続なしで世代選択とバイナリを含むファイル差分を検証する。"""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import backup_diff


class BackupDiffTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)

    def generation(self, name, files=None):
        directory = self.root / name
        directory.mkdir()
        for relative, content in (files or {}).items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return directory

    def test_first_backup_has_no_baseline(self):
        current = self.generation("20260921_120000_mydev", {"a": b"a"})
        diff = backup_diff.compare_with_previous(current, "mydev")
        self.assertIsNone(diff.previous)
        self.assertEqual(diff.added, [])
        self.assertIn("比較対象なし", diff.format_text())

    def test_selects_same_org_previous_numeric_suffix_and_ignores_partial(self):
        for name in ("20260920_120000_mydev", "20260921_120000_mydev_2", "20260921_120000_mydev_9",
                     "20260921_120000_other", ".20260921_120000_mydev_11.partial", "arbitrary",
                     "20261321_120000_mydev", "20260922_120000_mydev"):
            self.generation(name)
        expected = self.generation("20260921_120000_mydev_10")
        current = self.generation("20260921_120000_mydev_11")
        self.assertEqual(backup_diff.find_previous_backup(current, "mydev"), expected)

    def test_directory_mtime_does_not_select_old_generation(self):
        older = self.generation("20260920_120000_mydev")
        previous = self.generation("20260921_110000_mydev")
        current = self.generation("20260921_120000_mydev")
        os.utime(older, (2000000000, 2000000000))
        self.assertEqual(backup_diff.find_previous_backup(current, "mydev"), previous)

    def test_added_removed_changed_and_unchanged(self):
        previous = self.generation("20260921_110000_mydev", {
            "classes/同じ.cls": b"same", "removed.txt": b"old", "changed.bin": b"\x00\x01",
            "length.txt": b"long", "renamed_old.txt": b"rename", "empty": b""})
        current = self.generation("20260921_120000_mydev", {
            "classes/同じ.cls": b"same", "added.txt": b"new", "changed.bin": b"\x00\x02",
            "length.txt": b"shorter", "renamed_new.txt": b"rename", "empty": b""})
        # 同じサイズ・mtime でも変更を検出する。
        for base in (previous, current):
            os.utime(base / "changed.bin", (1000000000, 1000000000))
        diff = backup_diff.compare_with_previous(current, "mydev")
        self.assertEqual(diff.added, ["added.txt", "renamed_new.txt"])
        self.assertEqual(diff.removed, ["removed.txt", "renamed_old.txt"])
        self.assertEqual(diff.changed, ["changed.bin", "length.txt"])
        self.assertEqual(diff.unchanged, 2)
        self.assertIn("削除は Salesforce 上の削除を断定", diff.format_text())
        self.assertEqual((previous / "removed.txt").read_bytes(), b"old")

    def test_identical_bytes_ignore_mtime_but_line_endings_are_changes(self):
        previous = self.generation("before", {"same": b"x", "line": b"a\r\n"})
        current = self.generation("after", {"same": b"x", "line": b"a\n"})
        os.utime(previous / "same", (1000000000, 1000000000))
        diff = backup_diff.compare_backups(previous, current)
        self.assertEqual(diff.changed, ["line"])
        self.assertEqual(diff.unchanged, 1)

    def test_content_difference_after_first_chunk(self):
        previous = self.generation("before", {"large": b"a" * 8 + b"x"})
        current = self.generation("after", {"large": b"a" * 8 + b"y"})
        with patch.object(backup_diff, "CHUNK_SIZE", 4):
            diff = backup_diff.compare_backups(previous, current)
        self.assertEqual(diff.changed, ["large"])

    def test_empty_directories_are_not_file_changes(self):
        previous = self.generation("before")
        current = self.generation("after")
        (current / "empty").mkdir()
        diff = backup_diff.compare_backups(previous, current)
        self.assertEqual(diff.summary(), "追加: 0 / 削除: 0 / 変更: 0 / 変更なし: 0")
        self.assertIn("前回・今回ともに取得ファイルが0件", diff.format_text())
        self.assertIn("比較ファイル数: 前回 0件 / 今回 0件", diff.format_text())

    def test_empty_current_explains_deletions_without_hiding_them(self):
        previous = self.generation("before", {"Example.cls": b"class"})
        current = self.generation("after")
        diff = backup_diff.compare_backups(previous, current)
        self.assertEqual(diff.removed, ["Example.cls"])
        self.assertIn("今回は取得ファイルが0件", diff.format_text())
        self.assertIn("比較ファイル数: 前回 1件 / 今回 0件", diff.format_text())

    def test_empty_previous_explains_additions(self):
        previous = self.generation("before")
        current = self.generation("after", {"Example.cls": b"class"})
        diff = backup_diff.compare_backups(previous, current)
        self.assertEqual(diff.added, ["Example.cls"])
        self.assertIn("前回は取得ファイルが0件", diff.format_text())

    def test_file_to_directory_transition(self):
        previous = self.generation("before", {"path": b"file"})
        current = self.generation("after", {"path/file": b"nested"})
        diff = backup_diff.compare_backups(previous, current)
        self.assertEqual(diff.added, ["path/file"])
        self.assertEqual(diff.removed, ["path"])

    def test_unreadable_directory_raises_instead_of_reporting_deletions(self):
        previous = self.generation("before")
        current = self.generation("after")
        with patch.object(backup_diff.os, "scandir", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                backup_diff.compare_backups(previous, current)

    def test_link_is_not_followed(self):
        previous = self.generation("before")
        current = self.generation("after")
        with patch.object(backup_diff, "_is_link", return_value=True):
            with self.assertRaises(OSError):
                backup_diff.compare_backups(previous, current)

    def test_cancellation_raises_without_modifying_files(self):
        previous = self.generation("before", {"keep": b"data"})
        current = self.generation("after", {"keep": b"data"})
        event = threading.Event()
        event.set()
        with self.assertRaises(InterruptedError):
            backup_diff.compare_backups(previous, current, event)
        self.assertEqual((current / "keep").read_bytes(), b"data")


if __name__ == "__main__":
    unittest.main()
