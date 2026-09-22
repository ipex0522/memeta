"""Retrieve 実行時の待機・計測ログに対する回帰テスト。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import metadata_fetcher


MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
  <types><members>*</members><name>ApexClass</name></types>
  <version>60.0</version>
</Package>"""


def success_json(**overrides: object) -> bytes:
    result = {"success": True, "status": "Succeeded", "done": True,
              "files": [{"fullName": "Example", "type": "ApexClass", "state": "Created"}]}
    result.update(overrides)
    return json.dumps({"status": 0, "result": result, "warnings": []}, ensure_ascii=False).encode("utf-8")


class DelayedEofStdout:
    """親プロセス終了後も stdout が閉じない状態を再現する。"""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.first_read = True
        self.read_sizes: list[int] = []

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if self.first_read:
            self.first_read = False
            return b"Retrieving 1/2 ApexClass\r\n"
        return b"" if self.release.is_set() else None

    def close(self) -> None:
        self.release.set()


class ChunkStdout:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.exhausted = False
        self.read_sizes: list[int] = []

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if self.chunks:
            return self.chunks.pop(0)
        self.exhausted = True
        return b""

    def close(self) -> None:
        pass


class FakeProcess:
    def __init__(self, stdout: object, *, return_when: object, exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ChunkStdout([])
        self._return_when = return_when
        self.poll_calls = 0
        self.exit_code = exit_code

    def poll(self) -> int | None:
        self.poll_calls += 1
        if callable(self._return_when):
            return self.exit_code if self._return_when() else None
        return self.exit_code if self.poll_calls >= int(self._return_when) else None


class RetrieveRuntimeTests(unittest.TestCase):
    def _run_in_temporary_project(self, stdout: object, *, return_when: object,
                                  drain_grace_seconds: float = 0.02, exit_code: int = 0,
                                  log_failure: bool = False, stderr: object = None,
                                  cancel_event: threading.Event | None = None,
                                  create_output: bool = True,
                                  real_cli: bool = False,
                                  previous_files: dict[str, bytes] | None = None) -> tuple[list[tuple[str, object]], str, object]:
        real_popen = subprocess.Popen
        real_read = metadata_fetcher._read_pipe_chunk
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = root / "data" / "mydev" / "metadata" / "20000101_000000_mydev"
            if previous_files is not None:
                previous.mkdir(parents=True)
                for name, content in previous_files.items():
                    path = previous / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
            manifest = root / "package.xml"
            manifest.write_text(MANIFEST, encoding="utf-8")
            temp_dir = root / "temp"
            force_app = temp_dir / "force-app"
            process = FakeProcess(stdout, return_when=return_when, exit_code=exit_code)
            if stderr is not None:
                process.stderr = stderr

            def create_project(_api_version: str) -> None:
                force_app.mkdir(parents=True)

            def launch(*args: object, **kwargs: object) -> FakeProcess:
                if create_output:
                    result = force_app / "classes" / "Example.cls"
                    result.parent.mkdir(parents=True, exist_ok=True)
                    result.write_text("public class Example {}", encoding="utf-8")
                if real_cli:
                    # パイプ容量を超える stderr を先に書く。両パイプを回収しない実装は停止する。
                    script = ("import sys; sys.stderr.buffer.write(b'notice ' * 20000); sys.stderr.flush(); "
                              f"sys.stdout.buffer.write({success_json()!r}); sys.stdout.flush()")
                    return real_popen([sys.executable, "-c", script], **kwargs)
                return process

            popen = Mock(side_effect=launch)
            if log_failure:
                log_dir = root / "data" / "mydev" / "logs"
                log_dir.parent.mkdir(parents=True)
                log_dir.write_text("block log directory", encoding="utf-8")

            with (
                patch.object(metadata_fetcher, "ROOT_DIR", root),
                patch.object(metadata_fetcher, "TEMP_DIR", temp_dir),
                patch.object(metadata_fetcher, "FORCE_APP_DIR", force_app),
                patch.object(metadata_fetcher.shutil, "which", return_value="C:/sf/bin/sf.exe"),
                patch.object(metadata_fetcher.package_generator, "validate_package_xml_compatibility", return_value=(True, "互換")),
                patch.object(metadata_fetcher.package_generator, "create_default_project_files", side_effect=create_project),
                patch.object(metadata_fetcher.config_manager, "clean_temp_dir", return_value=True),
                patch.object(metadata_fetcher.config_manager, "set_current_process"),
                patch.object(metadata_fetcher.config_manager, "cancel_active_process"),
                patch.object(metadata_fetcher.subprocess, "Popen", popen),
                patch.object(metadata_fetcher, "_read_pipe_chunk", side_effect=real_read if real_cli else
                             lambda stream: stream.read(metadata_fetcher.OUTPUT_CHUNK_SIZE)),
                patch.object(metadata_fetcher, "OUTPUT_DRAIN_GRACE_SECONDS", drain_grace_seconds),
            ):
                events = list(metadata_fetcher.retrieve_metadata_stream("mydev", manifest, cancel_event))
                logs = list((root / "data" / "mydev" / "logs").glob("*.log"))
                self.assertEqual(len(logs), 0 if log_failure else 1)
                log_text = logs[0].read_text(encoding="utf-8") if logs else ""
                backups = [path for path in (root / "data" / "mydev" / "metadata").glob("*") if path != previous]
                successful = any(kind == "success" for kind, _ in events)
                self.assertEqual(len(backups), 1 if successful else 0)
                if successful and create_output:
                    self.assertEqual((backups[0] / "classes" / "Example.cls").read_text(), "public class Example {}")
                call = popen.call_args
            return events, log_text, call

    def test_diff_is_emitted_and_logged_after_backup(self) -> None:
        stdout = ChunkStdout([success_json()])
        events, log_text, _ = self._run_in_temporary_project(stdout, return_when=lambda: stdout.exhausted,
            previous_files={"old.txt": b"old", "classes/Example.cls": b"previous version"})
        report = next(value for kind, value in events if kind == "diff")
        self.assertIn("追加: 0 / 削除: 1 / 変更: 1", report)
        self.assertIn("old.txt", report)
        self.assertIn("classes/Example.cls", report)
        self.assertIn(report, log_text)
        self.assertTrue(any(kind == "success" for kind, _ in events))

    def test_first_backup_emits_no_baseline_report(self) -> None:
        stdout = ChunkStdout([success_json()])
        events, _, _ = self._run_in_temporary_project(stdout, return_when=lambda: stdout.exhausted)
        self.assertTrue(any(kind == "diff" and "比較対象なし" in value for kind, value in events))
        self.assertFalse(any(kind == "warnings" for kind, _ in events))

    def test_diff_failure_or_cancel_keeps_successful_backup(self) -> None:
        for error in (PermissionError("denied"), InterruptedError("cancelled")):
            with self.subTest(error=error), patch.object(metadata_fetcher.backup_diff, "compare_with_previous", side_effect=error):
                stdout = ChunkStdout([success_json()])
                events, log_text, _ = self._run_in_temporary_project(stdout, return_when=lambda: stdout.exhausted)
                self.assertTrue(any(kind == "success" for kind, _ in events))
                self.assertFalse(any(kind == "error" for kind, _ in events))
                warnings = next(value for kind, value in events if kind == "warnings")
                self.assertIn("バックアップ差分を比較できませんでした", warnings[0])
                self.assertIn("全体終了: 警告あり完了", log_text)

    def test_parent_process_exit_does_not_wait_for_stdout_eof_and_keeps_diagnostics(self) -> None:
        stdout = DelayedEofStdout()
        started = time.monotonic()
        try:
            with patch.dict(os.environ, {"SF_DISABLE_SOURCE_MEMBER_POLLING": "false"}, clear=False):
                events, log_text, popen_call = self._run_in_temporary_project(stdout, return_when=2)
                elapsed = time.monotonic() - started
                self.assertEqual(os.environ["SF_DISABLE_SOURCE_MEMBER_POLLING"], "false")
        finally:
            stdout.release.set()

        self.assertLess(elapsed, 1.0)
        self.assertFalse(any(kind == "progress" for kind, _ in events))
        self.assertFalse(any(event_type == "success" for event_type, _payload in events))
        error = next(payload for event_type, payload in events if event_type == "error")
        self.assertIn("バックアップは作成しませんでした", error)
        self.assertIn("Retrieving 1/2 ApexClass", error)
        self.assertTrue(any("標準出力が閉じない" in str(payload) for event_type, payload in events if event_type == "log"))
        self.assertIn(metadata_fetcher.OUTPUT_CHUNK_SIZE, stdout.read_sizes)

        environment = popen_call.kwargs["env"]
        self.assertEqual(environment["SF_DISABLE_SOURCE_MEMBER_POLLING"], "true")
        for label in (
            "マニフェスト集計: タイプ: 1 / members: 1 / ワイルドカード: 1",
            "CLI 互換性検査を開始しました",
            "CLI 互換性検査が完了しました",
            "Salesforce CLI を起動しました",
            "最初の出力を受信しました",
            "親プロセスが終了しました",
            "取得結果の完全性を保証できないため、バックアップを作成しません",
            "全体終了: エラー",
        ):
            self.assertIn(label, log_text)

    def test_json_chunk_boundaries_preserve_utf8_and_warnings_without_line_events(self) -> None:
        raw = success_json(messages={"fileName": "layouts/顧客.layout", "problem": "権限がありません"})
        stdout = ChunkStdout([raw[index:index + 1] for index in range(len(raw))])
        events, log_text, popen_call = self._run_in_temporary_project(
            stdout, return_when=lambda: stdout.exhausted
        )

        progress = [payload for event_type, payload in events if event_type == "progress"]
        logs = [str(payload) for event_type, payload in events if event_type == "log"]
        success = next(payload for event_type, payload in events if event_type == "success")
        self.assertEqual([item["percent"] for item in progress], [100])
        self.assertLess(len(logs), 12)
        self.assertEqual(success[1], raw.decode("utf-8"))
        self.assertIn(("warnings", ["layouts/顧客.layout / 権限がありません"]), events)
        self.assertIn("--json", popen_call.args[0])
        self.assertEqual(popen_call.kwargs["stderr"], subprocess.PIPE)
        self.assertIn(metadata_fetcher.OUTPUT_CHUNK_SIZE, stdout.read_sizes)
        self.assertIn("バックアップが完了しました", log_text)
        self.assertIn("全体完了: 警告あり完了", log_text)
        self.assertIn("CLI JSON:", log_text)

    def test_nonzero_exit_never_creates_backup(self) -> None:
        stdout = ChunkStdout([b"failed without newline"])
        events, log_text, _ = self._run_in_temporary_project(
            stdout, return_when=lambda: stdout.exhausted, exit_code=1
        )
        self.assertFalse(any(kind == "success" for kind, _ in events))
        self.assertIn("failed without newline", events[-1][1])
        self.assertIn("exit code 1", log_text)

    def test_log_failure_does_not_prevent_backup(self) -> None:
        stdout = ChunkStdout([success_json()])
        events, _, _ = self._run_in_temporary_project(
            stdout, return_when=lambda: stdout.exhausted, log_failure=True
        )
        self.assertTrue(any(kind == "success" for kind, _ in events))
        self.assertTrue(any("計測ログを保存できません" in str(value) for _, value in events))

    def test_large_file_list_does_not_generate_per_file_ui_events(self) -> None:
        files = [{"fullName": f"Example{index}", "type": "ApexClass", "state": "Created"} for index in range(10000)]
        raw = success_json(files=files)
        stdout = ChunkStdout([raw[index:index + 4096] for index in range(0, len(raw), 4096)])
        events, log_text, _ = self._run_in_temporary_project(stdout, return_when=1)
        self.assertTrue(any(kind == "success" for kind, _ in events))
        self.assertFalse(any(kind == "warnings" for kind, _ in events))
        self.assertLess(len([event for event in events if event[0] != "status"]), 15)
        self.assertIn("Example9999", log_text)

    def test_stderr_is_separate_and_visible_as_warning(self) -> None:
        stdout = ChunkStdout([success_json()])
        stderr = ChunkStdout([b"CLI notice\n" for _ in range(100)])
        events, log_text, _ = self._run_in_temporary_project(
            stdout, stderr=stderr, return_when=lambda: stdout.exhausted and stderr.exhausted)
        self.assertTrue(any(kind == "success" for kind, _ in events))
        warnings = next(value for kind, value in events if kind == "warnings")
        self.assertIn("CLI notice", warnings[0])
        self.assertIn("CLI stderr:", log_text)

    def test_real_process_drains_large_stderr_before_json_stdout(self) -> None:
        events, log_text, _ = self._run_in_temporary_project(None, return_when=1, real_cli=True,
                                                            drain_grace_seconds=1.0)
        self.assertTrue(any(kind == "success" for kind, _ in events))
        self.assertTrue(any(kind == "warnings" for kind, _ in events))
        self.assertIn("CLI JSON:", log_text)
        self.assertIn("CLI stderr:", log_text)

    def test_unclosed_stderr_prevents_backup(self) -> None:
        stdout = ChunkStdout([success_json()])
        stderr = DelayedEofStdout()
        events, log_text, _ = self._run_in_temporary_project(stdout, stderr=stderr, return_when=1)
        self.assertFalse(any(kind == "success" for kind, _ in events))
        self.assertIn("一時作業領域を保持", log_text)

    def test_invalid_or_failed_json_prevents_backup_even_when_exit_zero(self) -> None:
        for raw in (b"not json", b"{}", b"[]", success_json(success=False, status="Failed"),
                    success_json(done=False), success_json(status="InProgress"), success_json(files=["invalid"]),
                    success_json(files=[{"state": "Failed", "fullName": "Bad", "error": "conversion failed"}]),
                    b'{"status":1,"message":"authentication failed"}'):
            with self.subTest(raw=raw):
                stdout = ChunkStdout([raw])
                events, _, _ = self._run_in_temporary_project(stdout, return_when=lambda: stdout.exhausted)
                self.assertTrue(any(kind == "error" for kind, _ in events))
                self.assertFalse(any(kind == "success" for kind, _ in events))

    def test_empty_result_is_not_clean_success(self) -> None:
        stdout = ChunkStdout([success_json(files=[])])
        events, _, _ = self._run_in_temporary_project(stdout, return_when=lambda: stdout.exhausted,
                                                    create_output=False)
        notice = next(value for kind, value in events if kind == "empty")
        self.assertIn("資材ファイルは保存されていません", notice)
        self.assertIn("空の世代フォルダ", notice)
        self.assertFalse(any(kind == "warnings" for kind, _ in events))
        self.assertTrue(any(kind == "success" for kind, _ in events))

    def test_empty_result_keeps_real_warnings_visible(self) -> None:
        stdout = ChunkStdout([success_json(files=[], messages=[{"problem": "permission denied"}])])
        events, log_text, _ = self._run_in_temporary_project(stdout, return_when=lambda: stdout.exhausted,
                                                            create_output=False)
        self.assertTrue(any(kind == "empty" for kind, _ in events))
        self.assertIn(("warnings", ["permission denied"]), events)
        self.assertIn("全体終了: 取得対象0件（警告あり）", log_text)

    def test_empty_result_is_logged_without_warning(self) -> None:
        stdout = ChunkStdout([success_json(files=[])])
        events, log_text, _ = self._run_in_temporary_project(stdout, return_when=lambda: stdout.exhausted,
                                                            create_output=False)
        self.assertIn("全体終了: 取得対象0件 /", log_text)
        self.assertTrue(any(kind == "progress" and value["meta_type"] == "取得対象0件" for kind, value in events))

    def test_reported_file_without_actual_file_is_still_a_warning(self) -> None:
        stdout = ChunkStdout([success_json()])
        events, _, _ = self._run_in_temporary_project(stdout, return_when=lambda: stdout.exhausted,
                                                    create_output=False)
        warnings = next(value for kind, value in events if kind == "warnings")
        self.assertIn("保存対象の実ファイルがありません", warnings[0])
        self.assertTrue(any(kind == "empty" for kind, _ in events))

    def test_actual_file_without_cli_file_list_is_still_a_warning(self) -> None:
        stdout = ChunkStdout([success_json(files=[])])
        events, _, _ = self._run_in_temporary_project(stdout, return_when=lambda: stdout.exhausted)
        warnings = next(value for kind, value in events if kind == "warnings")
        self.assertIn("実ファイルは存在します", warnings[0])
        self.assertFalse(any(kind == "empty" for kind, _ in events))

    def test_success_with_30_message_derived_failures_creates_warning_backup(self) -> None:
        # 実際の障害ログと同じ構造。組織の資材名・パスはテストへ取り込まない。
        problems = ([""] * 24
                    + ["You do not have the proper permissions to access Dashboard."] * 2
                    + ["You do not have the proper permissions to access Layout."] * 2
                    + [f"Unable to retrieve file for id {name}-nl of type StandardValueSetTranslation. 言語 nl はサポートされていません。"
                       for name in ("AddressCountryCode", "AddressStateCode")])
        messages = [{"fileName": "unpackaged/package.xml", "problem": problem} for problem in problems]
        failures = [{"fullName": "", "type": "", "problemType": "Error", "state": "Failed", "error": problem}
                    for problem in problems]
        created = [{"fullName": f"Example{index}", "type": "ApexClass", "state": "Created"} for index in range(869)]
        raw = success_json(files=failures + created, messages=messages)
        stdout = ChunkStdout([raw])
        events, log_text, _ = self._run_in_temporary_project(stdout, return_when=lambda: stdout.exhausted)
        self.assertTrue(any(kind == "success" for kind, _ in events))
        self.assertFalse(any(kind == "error" for kind, _ in events))
        warnings = next(value for kind, value in events if kind == "warnings")
        self.assertEqual(len(warnings), 5)  # 重複は除外し、空24件を1行に集約
        for expected in ("Dashboard", "Layout", "AddressCountryCode-nl", "AddressStateCode-nl",
                         "詳細不明の取得メッセージ: 24件"):
            self.assertTrue(any(expected in warning for warning in warnings))
        self.assertIn("全体完了: 警告あり完了", log_text)
        self.assertIn(raw.decode("utf-8"), log_text)

    def test_single_named_message_matches_sdr_failure_conversion(self) -> None:
        problem = "Entity of type 'ApexClass' named 'Unavailable' cannot be found"
        raw = success_json(messages={"fileName": "unpackaged/package.xml", "problem": problem},
                           files=[{"fullName": "Unavailable", "type": "ApexClass", "state": "Failed",
                                   "problemType": "Error", "error": problem},
                                  {"fullName": "Example", "type": "ApexClass", "state": "Created"}])
        warnings = metadata_fetcher._parse_retrieve_result(raw.decode(), "")
        self.assertEqual(warnings, ["unpackaged/package.xml / " + problem])

    def test_unmatched_failures_still_prevent_backup(self) -> None:
        failure = {"fullName": "", "type": "", "problemType": "Error", "state": "Failed", "error": "notice"}
        cases = [
            ([failure], []),  # message の裏付けがない
            ([failure], [{"problem": "different"}]),
            ([failure, failure], [{"problem": "notice"}]),  # 同じ文面でも対応件数を超える
            ([{**failure, "fullName": "Unknown"}], [{"problem": "notice"}]),
            ([{**failure, "filePath": "classes/Example.cls"}], [{"problem": "notice"}]),
            ([{**failure, "problemType": "Unexpected"}], [{"problem": "notice"}]),
            ([{**failure, "error": ""}], []),  # 空の Failed を無条件に無視しない
            ([{**failure, "error": "conversion failed"}], [{"problem": "notice"}]),
        ]
        for failures, messages in cases:
            with self.subTest(failures=failures, messages=messages):
                stdout = ChunkStdout([success_json(files=failures, messages=messages)])
                events, _, _ = self._run_in_temporary_project(stdout, return_when=lambda: stdout.exhausted)
                self.assertTrue(any(kind == "error" for kind, _ in events))
                self.assertFalse(any(kind == "success" for kind, _ in events))

    def test_message_match_never_overrides_failed_or_unfinished_result(self) -> None:
        failure = {"fullName": "", "type": "", "problemType": "Error", "state": "Failed", "error": "notice"}
        for overrides in ({"success": False}, {"status": "Failed"}, {"done": False}, {"done": None}):
            with self.subTest(overrides=overrides):
                stdout = ChunkStdout([success_json(files=[failure], messages=[{"problem": "notice"}], **overrides)])
                events, _, _ = self._run_in_temporary_project(stdout, return_when=lambda: stdout.exhausted)
                self.assertTrue(any(kind == "error" for kind, _ in events))
                self.assertFalse(any(kind == "success" for kind, _ in events))

    def test_cancellation_never_creates_backup(self) -> None:
        event = threading.Event()
        event.set()
        events, log_text, _ = self._run_in_temporary_project(ChunkStdout([]), return_when=1, cancel_event=event)
        self.assertFalse(any(kind == "success" for kind, _ in events))
        self.assertIn("全体終了: 中止", log_text)

    def test_warning_sources_deduplicate_and_preserve_unknown_fields(self) -> None:
        data = json.loads(success_json(messages=[{"fileName": "Dashboard", "problem": "permission denied"},
                                                {"unexpected": "reason"}], warnings="duplicate"))
        data["warnings"] = ["duplicate", "unsupported language nl"]
        warnings = metadata_fetcher._parse_retrieve_result(json.dumps(data), "")
        self.assertEqual(len(warnings), 4)
        self.assertIn("Dashboard / permission denied", warnings)
        self.assertIn('{"unexpected": "reason"}', warnings)

    def test_status_events_are_throttled(self) -> None:
        stdout = ChunkStdout([success_json(), *([None] * 100)])
        ticks = iter(index / 100 for index in range(2000))
        with patch.object(metadata_fetcher.time, "monotonic", side_effect=lambda: next(ticks)), \
             patch.object(metadata_fetcher.time, "sleep"):
            events, _, _ = self._run_in_temporary_project(stdout, return_when=lambda: stdout.exhausted)
        statuses = [value for kind, value in events if kind == "status"]
        self.assertGreaterEqual(len(statuses), 1)
        self.assertLess(len(statuses), 10)

    def test_real_os_pipe_reads_small_utf8_output_before_process_exit(self) -> None:
        # 実OSのパイプで確認。Salesforce接続やユーザーデータを使わない。
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([227,129,130])); sys.stdout.flush(); sys.stdin.buffer.read(1)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0,
            creationflags=metadata_fetcher._creation_flags(),
        )
        try:
            deadline = time.monotonic() + 5
            output = bytearray()
            while len(output) < 3 and time.monotonic() < deadline:
                chunk = metadata_fetcher._read_pipe_chunk(process.stdout)
                if chunk:
                    output.extend(chunk)
                else:
                    time.sleep(0.01)
            self.assertEqual(output.decode("utf-8"), "あ")
            self.assertIsNone(process.poll())
            self.assertIsNone(metadata_fetcher._read_pipe_chunk(process.stdout))
            process.stdin.close()
            process.wait(timeout=5)
            self.assertEqual(metadata_fetcher._read_pipe_chunk(process.stdout), b"")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stdin.close()


if __name__ == "__main__":
    unittest.main()
