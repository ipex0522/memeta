"""隔離ディレクトリで Salesforce CLI retrieve を実行し、バックアップを作る。"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

ROOT_DIR = Path(__file__).resolve().parent
TEMP_DIR = ROOT_DIR / "temp"
FORCE_APP_DIR = TEMP_DIR / "force-app"
ProgressCallback = Callable[[int, str], None]
LogCallback = Callable[[str], None]


def prepare_temp() -> None:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    (TEMP_DIR / "sfdx-project.json").write_text('{"packageDirectories":[{"path":"force-app","default":true}],"namespace":"","sourceApiVersion":"60.0"}', encoding="utf-8", errors="replace")


def cleanup_temp() -> None:
    if FORCE_APP_DIR.exists():
        shutil.rmtree(FORCE_APP_DIR, ignore_errors=True)


def kill_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", check=False)
    else:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)


def _copy_with_retry(source: Path, destination: Path) -> None:
    last_error: OSError | None = None
    for _ in range(5):
        try:
            shutil.copytree(source, destination)
            return
        except OSError as error:
            last_error = error
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            time.sleep(1)
    raise last_error or OSError("バックアップのコピーに失敗しました。")


def retrieve_metadata_stream(target_org: str, on_progress: ProgressCallback, on_log: LogCallback,
                             process_holder: dict[str, subprocess.Popen[str] | None], stop_event: threading.Event) -> Path:
    manifest = (ROOT_DIR / "data" / target_org / "package.xml").resolve()
    if not manifest.exists():
        raise FileNotFoundError(f"package.xml が見つかりません: {manifest}")
    if not shutil.which("sf"):
        raise RuntimeError("Salesforce CLI (sf) が PATH 上に見つかりません。")
    cleanup_temp()
    prepare_temp()
    env = os.environ.copy()
    env.update({"FORCE_COLOR": "0", "SF_COLOR": "0"})
    command = ["sf", "project", "retrieve", "start", "--manifest", os.path.abspath(manifest), "--target-org", target_org]
    kwargs: dict[str, object] = {"cwd": str(TEMP_DIR), "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                                  "text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 0, "env": env}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]
    process_holder["process"] = process
    buffer = ""
    try:
        assert process.stdout is not None
        while True:
            character = process.stdout.read(1)
            if not character:
                break
            if stop_event.is_set():
                kill_process_tree(process)
                raise RuntimeError("ユーザーにより中止しました。")
            buffer += character
            if character in "\r\n":
                line = buffer.strip()
                buffer = ""
                if line:
                    match = re.search(r"(\d+)\s*/\s*(\d+)", line)
                    if match and int(match.group(2)):
                        percent = int(int(match.group(1)) * 100 / int(match.group(2)))
                        on_progress(percent, line)
                    elif "Retriev" in line or "処理" in line:
                        on_progress(0, line)
                    on_log(line)
        exit_code = process.wait()
        if exit_code != 0:
            raise RuntimeError(f"sf retrieve が失敗しました (exit code: {exit_code})")
        if not FORCE_APP_DIR.exists():
            raise RuntimeError("取得結果 (temp/force-app) が見つかりません。")
        backup = ROOT_DIR / "data" / target_org / "metadata" / f"{datetime.now():%Y%m%d_%H%M}_{target_org}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        _copy_with_retry(FORCE_APP_DIR, backup)
        on_progress(100, "バックアップ完了")
        return backup
    finally:
        process_holder["process"] = None
        cleanup_temp()
