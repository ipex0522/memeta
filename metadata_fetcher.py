"""隔離された temp 領域での Salesforce retrieve と世代バックアップ。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

import config_manager
import backup_diff
import package_generator

ROOT_DIR = Path(__file__).resolve().parent
TEMP_DIR = ROOT_DIR / "temp"
FORCE_APP_DIR = TEMP_DIR / "force-app"
ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OUTPUT_CHUNK_SIZE = 65536
OUTPUT_DRAIN_GRACE_SECONDS = 1.0
STATUS_INTERVAL_SECONDS = 1.0


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _read_pipe_chunk(stream: Any) -> bytes | None:
    """読み取り可能分だけ返す。None は未到着、b'' は EOF。待機スレッドを残さない。"""
    descriptor = stream.fileno()
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        available = wintypes.DWORD()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
        if not kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in (109, 232):  # ERROR_BROKEN_PIPE / ERROR_NO_DATA
                return b""
            raise ctypes.WinError(error)
        if not available.value:
            return None
        return os.read(descriptor, min(available.value, OUTPUT_CHUNK_SIZE))
    import select

    ready, _, _ = select.select([descriptor], [], [], 0)
    return os.read(descriptor, OUTPUT_CHUNK_SIZE) if ready else None


def _format_duration(seconds: float) -> str:
    """ログ表示用に短い経過時間へ整形する。"""
    if seconds < 60:
        return f"{seconds:.1f}秒"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}分{remainder:02d}秒"


class _RunDiagnostics:
    """取得1回分の時刻情報を、失敗しても本処理を止めずに保存する。"""

    def __init__(self, target_org: str) -> None:
        self.started_at = datetime.now()
        self.started_monotonic = time.monotonic()
        self.path: Path | None = self._create_path(target_org)
        self.write(f"取得計測を開始しました。対象組織: {target_org}")

    def _create_path(self, target_org: str) -> Path | None:
        safe_org = target_org.strip()
        if not safe_org or safe_org in {".", ".."} or Path(safe_org).name != safe_org:
            return None
        try:
            directory = ROOT_DIR / "data" / safe_org / "logs"
            directory.mkdir(parents=True, exist_ok=True)
            return directory / f"{self.started_at:%Y%m%d_%H%M%S_%f}_{safe_org}.log"
        except OSError:
            return None

    def write(self, message: str) -> None:
        """計測ログの書込み失敗は取得を妨げない。"""
        if not self.path:
            return
        elapsed = time.monotonic() - self.started_monotonic
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        try:
            with self.path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(f"[{timestamp}] [+{elapsed:8.3f}s] {message}\n")
        except OSError:
            self.path = None


def _manifest_summary(manifest: Path) -> str:
    """package.xml の取得規模を、取得処理に影響しない範囲で記録する。"""
    try:
        root = ET.parse(manifest).getroot()
    except (ET.ParseError, OSError) as error:
        return f"解析できません ({error})"
    type_count = 0
    member_count = 0
    wildcard_count = 0
    for element in root:
        if element.tag.rsplit("}", 1)[-1] != "types":
            continue
        type_count += 1
        for child in element:
            if child.tag.rsplit("}", 1)[-1] != "members" or not child.text:
                continue
            member_count += 1
            if child.text.strip() == "*":
                wildcard_count += 1
    return f"タイプ: {type_count} / members: {member_count} / ワイルドカード: {wildcard_count}"


def _directory_summary(directory: Path) -> str:
    """バックアップ前の出力規模を集計する。診断専用のため失敗を許容する。"""
    try:
        file_count = 0
        total_bytes = 0
        for item in directory.rglob("*"):
            if item.is_file():
                file_count += 1
                total_bytes += item.stat().st_size
        return f"ファイル: {file_count} / 容量: {total_bytes:,} bytes"
    except OSError as error:
        return f"集計できません ({error})"


def _copy_with_retry(source: Path, destination: Path, cancel_event: threading.Event | None = None) -> None:
    staging = destination.parent / f".{destination.name}.partial"
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("ユーザーによりコピーを中止しました。")
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(source, staging)
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("ユーザーによりコピーを中止しました。")
            os.replace(staging, destination)
            return
        except (OSError, InterruptedError) as error:
            last_error = error
            shutil.rmtree(staging, ignore_errors=True)
            if isinstance(error, InterruptedError):
                raise
            time.sleep(attempt + 1)
    raise last_error or OSError("バックアップのコピーに失敗しました。")


def _backup_path(target_org: str) -> Path:
    base = ROOT_DIR / "data" / target_org / "metadata"
    base.mkdir(parents=True, exist_ok=True)
    prefix = f"{datetime.now():%Y%m%d_%H%M%S}_{target_org}"
    destination = base / prefix
    suffix = 1
    while destination.exists():
        suffix += 1
        destination = base / f"{prefix}_{suffix}"
    return destination


def _message_text(value: Any) -> str:
    if isinstance(value, dict):
        # 型・資材名・理由を落とさず、未知の形式も原文で確認できるようにする。
        fields = ("type", "fullName", "fileName", "filePath", "problem", "error", "message")
        parts = [str(value[key]) for key in fields if value.get(key)]
        return " / ".join(parts) if parts else json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _parse_retrieve_result(transcript: str, stderr: str) -> list[str]:
    """JSON の成功状態を検証し、重複を除いた警告を返す。不明な結果は成功にしない。"""
    try:
        envelope = json.loads(transcript)
    except (ValueError, TypeError) as error:
        raise ValueError("Salesforce CLI の JSON 結果を解析できません。計測ログを確認してください。") from error
    if not isinstance(envelope, dict):
        raise ValueError("Salesforce CLI の JSON 結果が想定形式ではありません。")
    if envelope.get("status") != 0:
        raise ValueError(_message_text(envelope))
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise ValueError("Salesforce CLI の取得結果がありません。")
    if result.get("success") is not True or result.get("status") != "Succeeded" or result.get("done") is not True:
        raise ValueError("取得完了を確認できません: " + _message_text(result))
    files = result.get("files", [])
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        raise ValueError("Salesforce CLI の files が想定形式ではありません。")
    source_messages = result.get("messages")
    messages = source_messages if isinstance(source_messages, list) else [source_messages]
    # SDR RetrieveResult.getFileResponses() は messages を Failed / Error にも複製する。
    # 全体成功を確認した上で、その変換形と件数が一致するものだけ警告へ分類する。
    # 参照: forcedotcom/source-deploy-retrieve src/client/metadataApiRetrieve.ts
    message_failures: Counter[tuple[str, str, str, str]] = Counter()
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("problem"), str):
            continue
        problem = message["problem"]
        match = re.search(r".+'(.+)'.+'(.+)'", problem)
        type_name, full_name = match.groups() if match else ("", "")
        message_failures[(full_name, type_name, problem, "Error")] += 1
    unmatched_failures = []
    for item in files:
        if item.get("state") != "Failed":
            continue
        signature = tuple(item.get(key) for key in ("fullName", "type", "error", "problemType"))
        if (all(isinstance(value, str) for value in signature) and not item.get("filePath")
                and message_failures[signature] > 0):
            message_failures[signature] -= 1
        else:
            unmatched_failures.append(item)
    if unmatched_failures:
        raise ValueError("資材の取得・変換に失敗しました:\n" + "\n".join(_message_text(item) for item in unmatched_failures))
    warnings: list[str] = []
    unknown_message_count = 0
    for item in messages:
        if isinstance(item, dict) and isinstance(item.get("problem"), str) and not item["problem"].strip():
            unknown_message_count += 1
        elif item is not None and (message := _message_text(item)):
            warnings.append(message)
    if unknown_message_count:
        warnings.append(f"詳細不明の取得メッセージ: {unknown_message_count}件（CLI が理由を返していません。原文は計測ログを確認してください。）")
    for source in (result.get("warnings"), envelope.get("warnings")):
        for item in source if isinstance(source, list) else [source]:
            if item is not None and (message := _message_text(item)):
                warnings.append(message)
    if stderr.strip():
        warnings.append("CLI 標準エラー出力: " + ANSI_PATTERN.sub("", stderr).strip())
    return list(dict.fromkeys(warnings))


def retrieve_metadata_stream(target_org: str, manifest_path: str | Path | None = None,
                             cancel_event: threading.Event | None = None,
                             api_version: str = "60.0") -> Iterator[tuple[str, Any]]:
    """retrieve の実行状況を ``(event_type, payload)`` で逐次返す。

    event_type は log、status（最大毎秒）、progress、diff、empty、warnings、success、error。
    warnings はバックアップ成功時だけ返す文字列一覧。success の既存形式は維持する。
    """
    manifest = Path(manifest_path) if manifest_path else ROOT_DIR / "data" / target_org / "package.xml"
    manifest = manifest.resolve()
    diagnostics = _RunDiagnostics(target_org)
    diagnostics.write(f"マニフェスト: {manifest}")
    if diagnostics.path:
        yield "log", f"計測ログ: {diagnostics.path}"
    else:
        yield "log", "注意: 計測ログを保存できません。取得処理は継続します。"

    if not manifest.exists():
        diagnostics.write("エラー: package.xml が見つかりません。")
        diagnostics.write(f"全体終了: エラー / 所要 {_format_duration(time.monotonic() - diagnostics.started_monotonic)}")
        yield "error", f"package.xml が見つかりません: {manifest}\n先に Package.xml を作成してください。"
        return
    sf_path = shutil.which("sf")
    if not sf_path:
        diagnostics.write("エラー: Salesforce CLI が PATH 上に見つかりません。")
        diagnostics.write(f"全体終了: エラー / 所要 {_format_duration(time.monotonic() - diagnostics.started_monotonic)}")
        yield "error", "Salesforce CLI (sf) が PATH 上に見つかりません。`sf --version` を確認してください。"
        return

    manifest_info = _manifest_summary(manifest)
    diagnostics.write(f"マニフェスト集計: {manifest_info}")
    compatibility_started = time.monotonic()
    diagnostics.write("フェーズ: Package.xml の CLI 互換性検査を開始しました。")
    yield "log", "フェーズ: Package.xml の Salesforce CLI 互換性を確認しています。"
    compatible, compatibility_message = package_generator.validate_package_xml_compatibility(
        target_org, manifest, api_version
    )
    compatibility_duration = time.monotonic() - compatibility_started
    diagnostics.write(
        "フェーズ: Package.xml の CLI 互換性検査が完了しました。"
        f" 結果: {'成功' if compatible else '失敗'} / 所要 {_format_duration(compatibility_duration)}"
    )
    yield "log", f"フェーズ: Package.xml の Salesforce CLI 互換性検査が完了しました（{_format_duration(compatibility_duration)}）。"
    if not compatible:
        # 互換性検査の ``sf org list`` が作成した作業用プロジェクトを残さない。
        config_manager.clean_temp_dir()
        diagnostics.write("互換性検査後の一時作業領域を清掃しました。")
        diagnostics.write(f"全体終了: エラー / 所要 {_format_duration(time.monotonic() - diagnostics.started_monotonic)}")
        yield "error", compatibility_message
        return

    temp_cleanup_started = time.monotonic()
    if not config_manager.clean_temp_dir() or TEMP_DIR.exists():
        diagnostics.write(
            "エラー: 一時作業領域の清掃に失敗しました。"
            f" 所要 {_format_duration(time.monotonic() - temp_cleanup_started)}"
        )
        diagnostics.write(f"全体終了: エラー / 所要 {_format_duration(time.monotonic() - diagnostics.started_monotonic)}")
        yield "error", f"一時作業領域を清掃できませんでした: {TEMP_DIR}\n他の Memeta 処理が終了してから再試行してください。"
        return
    package_generator.create_default_project_files(api_version)
    diagnostics.write(
        "フェーズ: 一時プロジェクトの準備が完了しました。"
        f" 所要 {_format_duration(time.monotonic() - temp_cleanup_started)}"
    )
    environment = os.environ.copy()
    # Memeta は取得物を世代バックアップするだけで、temp プロジェクトの SourceMember 追跡を次回へ使わない。
    # 子プロセス限定で無効化し、PC 全体の Salesforce CLI 設定は変更しない。
    environment.update({
        "FORCE_COLOR": "0",
        "SF_COLOR": "0",
        "SF_DISABLE_SOURCE_MEMBER_POLLING": "true",
    })
    command = [os.path.abspath(sf_path), "project", "retrieve", "start", "--manifest", os.path.abspath(manifest),
               "--target-org", target_org, "--api-version", api_version or "60.0", "--ignore-conflicts", "--json"]
    yield "log", (f"{target_org} のメタデータ取得を開始します。\n"
                  f"{compatibility_message}\n"
                  f"マニフェスト: {manifest}\n"
                  f"取得範囲: {manifest_info}\n"
                  f"作業領域: {TEMP_DIR}\n"
                  "SourceMember ポーリング: この取得プロセスだけ無効")

    process: subprocess.Popen[bytes] | None = None
    outcome = "エラー"
    preserve_temp = False
    try:
        cli_started = time.monotonic()
        diagnostics.write("フェーズ: Salesforce CLI を起動します。SourceMember ポーリング: 子プロセス限定で無効")
        process = subprocess.Popen(command, cwd=TEMP_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=environment, creationflags=_creation_flags(), bufsize=0)
        config_manager.set_current_process(process)
        diagnostics.write("フェーズ: Salesforce CLI を起動しました。")
        assert process.stdout is not None and process.stderr is not None
        buffers = [bytearray(), bytearray()]
        streams = [process.stdout, process.stderr]
        closed = [False, False]
        first_output_received = False
        cli_return_code: int | None = None
        output_drain_deadline: float | None = None
        next_status = cli_started + STATUS_INTERVAL_SECONDS

        while True:
            if cancel_event and cancel_event.is_set():
                diagnostics.write("中止要求を受信しました。Salesforce CLI を停止します。")
                config_manager.cancel_active_process()
                outcome = "中止"
                yield "error", "ユーザーにより処理を中止しました。"
                return
            received = False
            # 両パイプを継続して回収し、stderr が大量でも子プロセスを詰まらせない。
            for index, stream in enumerate(streams):
                chunk = b"" if closed[index] else _read_pipe_chunk(stream)
                if chunk == b"":
                    closed[index] = True
                elif chunk:
                    buffers[index].extend(chunk)
                    received = True
            if received:
                if not first_output_received:
                    first_output_received = True
                    diagnostics.write(
                        "フェーズ: Salesforce CLI から最初の出力を受信しました。"
                        f" 起動から {_format_duration(time.monotonic() - cli_started)}"
                    )
                    yield "log", f"フェーズ: Salesforce CLI から最初の出力を受信しました（{_format_duration(time.monotonic() - cli_started)}）。"
            now = time.monotonic()
            if now >= next_status:
                yield "status", f"CLI 実行中（経過 {_format_duration(now - cli_started)}）"
                next_status = now + STATUS_INTERVAL_SECONDS

            return_code = process.poll()
            if return_code is None:
                if not received:
                    time.sleep(0.05)
                continue
            if cli_return_code is None:
                cli_return_code = return_code
                output_drain_deadline = time.monotonic() + OUTPUT_DRAIN_GRACE_SECONDS
                diagnostics.write(
                    "フェーズ: Salesforce CLI の親プロセスが終了しました。"
                    f" exit code: {return_code} / 所要 {_format_duration(time.monotonic() - cli_started)}"
                )
                yield "log", (
                    "フェーズ: Salesforce CLI の親プロセスが終了しました"
                    f"（exit code: {return_code}、{_format_duration(time.monotonic() - cli_started)}）。"
                )
            if all(closed):
                break
            # 到着済み出力は全て回収する。大量の出力を猶予時間だけで切り捨てない。
            if received:
                continue
            if output_drain_deadline is not None and time.monotonic() >= output_drain_deadline:
                diagnostics.write(
                    "警告: 親プロセス終了後も stdout/stderr の EOF を待機中のため、"
                    f"{OUTPUT_DRAIN_GRACE_SECONDS:.1f}秒で出力の待機を打ち切りました。"
                )
                preserve_temp = True
                yield "log", "注意: CLI の親プロセス終了後も標準出力が閉じない、または標準エラー出力が閉じないため、出力待機を打ち切ります。"
                break
            time.sleep(0.05)

        transcript = buffers[0].decode("utf-8-sig", errors="replace")
        stderr = buffers[1].decode("utf-8", errors="replace")
        # ファイルごとの UI 通知・ログ再オープンを避け、原文はまとめて保存する。
        diagnostics.write(f"CLI JSON:\n{transcript}")
        if stderr:
            diagnostics.write(f"CLI stderr:\n{stderr}")
        error_excerpt = (stderr + "\n" + transcript).strip()[:4000]
        return_code = cli_return_code if cli_return_code is not None else process.poll()
        if return_code is None:
            # ここへは親プロセス終了の検知後だけ到達するため、保険として扱う。
            raise OSError("Salesforce CLI の終了状態を確認できませんでした。")
        if return_code != 0:
            outcome = "エラー"
            diagnostics.write(f"エラー: Salesforce CLI が exit code {return_code} で終了しました。")
            yield "error", f"{target_org} のメタデータ取得に失敗しました (exit code: {return_code})。\n{error_excerpt}\n詳細は計測ログを確認してください。"
            return
        if not all(closed):
            # sf.cmd 等のラッパーだけが先に終了し、実際の retrieve/変換を行う子が残る可能性がある。
            # force-app は CLI 起動前から存在するため、親プロセスだけで成功と判断してコピーしない。
            outcome = "エラー"
            diagnostics.write(
                "エラー: Salesforce CLI の親プロセス終了後も stdout/stderr が閉じませんでした。"
                " 取得結果の完全性を保証できないため、バックアップを作成しません。"
            )
            yield "error", (
                "Salesforce CLI の親プロセスは終了しましたが、標準出力または標準エラー出力を保持する子プロセスが残っています。\n"
                "取得結果が途中の可能性があるため、バックアップは作成しませんでした。\n"
                f"作業領域は確認用に残しました: {TEMP_DIR}\n"
                "再実行前に残っている CLI プロセスの終了と計測ログを確認してください。\n"
                f"{error_excerpt}"
            )
            return
        try:
            warnings = _parse_retrieve_result(transcript, stderr)
        except ValueError as error:
            diagnostics.write(f"取得結果の検証エラー: {error}")
            yield "error", f"{str(error)[:4000]}\nバックアップは作成しませんでした。詳細は計測ログを確認してください。"
            return
        if not FORCE_APP_DIR.exists():
            outcome = "エラー"
            diagnostics.write("エラー: Salesforce CLI は成功しましたが、force-app が見つかりません。")
            yield "error", "CLI の処理は終了しましたが、temp/force-app に取得結果が見つかりません。"
            return
        source_summary_started = time.monotonic()
        yield "log", "フェーズ: 取得結果のファイル数・容量を確認しています。"
        source_summary = _directory_summary(FORCE_APP_DIR)
        has_files = any(path.is_file() for path in FORCE_APP_DIR.rglob("*"))
        reported_files = json.loads(transcript)["result"].get("files", [])
        if has_files and not reported_files:
            warnings.append("実ファイルは存在しますが、CLI の取得ファイル一覧が空です。計測ログを確認してください。")
        if not has_files and any(item.get("state") in {"Created", "Changed"} for item in reported_files):
            warnings.append("CLI はファイル取得を報告していますが、保存対象の実ファイルがありません。計測ログを確認してください。")
        diagnostics.write(
            "フェーズ: 取得結果を確認しました。"
            f" {source_summary} / 集計所要 {_format_duration(time.monotonic() - source_summary_started)}"
        )
        destination = _backup_path(target_org)
        backup_started = time.monotonic()
        diagnostics.write(f"フェーズ: バックアップを開始します。保存先: {destination}")
        yield "log", (f"取得完了。バックアップを作成しています: {destination}" if has_files else
                      f"取得対象0件。比較履歴用に空の世代フォルダを作成しています: {destination}")
        _copy_with_retry(FORCE_APP_DIR, destination, cancel_event)
        diagnostics.write(
            "フェーズ: バックアップが完了しました。"
            f" 所要 {_format_duration(time.monotonic() - backup_started)} / {source_summary}"
        )
        diff_started = time.monotonic()
        yield "log", "フェーズ: 同じ組織の前回バックアップとファイル差分を比較しています。"
        yield "status", "バックアップ保存済み・差分比較中..." if has_files else "取得対象0件・差分比較中..."
        try:
            diff = backup_diff.compare_with_previous(destination, target_org, cancel_event)
            report = diff.format_text()
            diagnostics.write("バックアップ差分:\n" + report)
            yield "log", "バックアップ差分: " + diff.summary()
            yield "diff", report
        except (OSError, ValueError) as error:
            # 比較は保存後の補助処理。失敗や中止で取得済みバックアップを失敗扱いにしない。
            warning = f"バックアップ差分を比較できませんでした: {error}\n世代フォルダは保持されています: {destination}"
            warnings.append(warning)
            diagnostics.write(warning)
        diagnostics.write(f"フェーズ: 差分比較終了。所要 {_format_duration(time.monotonic() - diff_started)}")
        outcome = "警告あり完了" if warnings else "成功"
        if not has_files:
            outcome = "取得対象0件（警告あり）" if warnings else "取得対象0件"
            notice = ("取得処理は完了しましたが、今回の条件で取得されたファイルは0件です。\n"
                      "対象が存在しない場合にも、この結果になります。存在するはずの場合は取得条件・権限を確認してください。\n"
                      "資材ファイルは保存されていません。比較履歴用の空の世代フォルダのみ作成しました。")
            diagnostics.write(notice)
            yield "log", notice
            yield "empty", notice
        if warnings:
            diagnostics.write("取得警告:\n" + "\n".join(warnings))
            yield "warnings", warnings
        diagnostics.write(f"全体完了: {outcome} / 所要 {_format_duration(time.monotonic() - diagnostics.started_monotonic)}")
        yield "progress", {"percent": 100, "current": 1, "total": 1,
                           "meta_type": "バックアップ完了" if has_files else "取得対象0件"}
        yield "success", (str(destination), transcript)
    except InterruptedError:
        outcome = "中止"
        diagnostics.write("中止: バックアップのコピーを中止しました。")
        yield "error", "ユーザーによりバックアップのコピーを中止しました。"
    except OSError as error:
        outcome = "エラー"
        if process and process.poll() is None:
            config_manager.cancel_active_process()
        diagnostics.write(f"エラー: メタデータ取得処理を開始または完了できませんでした: {error}")
        yield "error", f"メタデータ取得処理を完了できませんでした: {error}"
    finally:
        if process and process.stdout:
            process.stdout.close()
        if process and process.stderr:
            process.stderr.close()
        config_manager.set_current_process(None)
        cleanup_started = time.monotonic()
        if preserve_temp:
            diagnostics.write(f"フェーズ: CLI の完了を確認できないため、一時作業領域を保持しました: {TEMP_DIR}")
        else:
            cleaned = config_manager.clean_temp_dir()
            diagnostics.write(
                "フェーズ: 一時作業領域の清掃。"
                f" 結果: {'成功' if cleaned else '失敗'} / 所要 {_format_duration(time.monotonic() - cleanup_started)}"
            )
        diagnostics.write(f"全体終了: {outcome} / 所要 {_format_duration(time.monotonic() - diagnostics.started_monotonic)}")


def retrieve_metadata(target_org: str, manifest_path: str | Path | None = None,
                      log_callback: Any = None) -> tuple[bool, Any]:
    """既存コードから利用できる完了待ちラッパー。"""
    result: Any = None
    success = False
    for event_type, payload in retrieve_metadata_stream(target_org, manifest_path=manifest_path):
        if event_type == "log" and log_callback:
            log_callback(payload)
        elif event_type == "warnings" and log_callback:
            log_callback("警告あり完了:\n" + "\n".join(payload))
        elif event_type == "success":
            success, result = True, payload
        elif event_type == "error":
            success, result = False, payload
    return success, result
