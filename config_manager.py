"""Memeta の設定、OAuth 認証、一時プロセスを安全に管理する。"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "memeta_config.json"
TEMP_DIR = ROOT_DIR / "temp"

_process_lock = threading.Lock()
_current_process: subprocess.Popen[bytes] | None = None


def _as_type_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _normalise_org(alias: str, source: Any, defaults: dict[str, Any]) -> dict[str, Any]:
    source = source if isinstance(source, dict) else {}
    return {
        "org_alias": str(source.get("org_alias", alias)).strip() or alias,
        "is_sandbox": bool(source.get("is_sandbox", False)),
        "api_version": str(source.get("api_version", defaults.get("api_version", "60.0"))).strip() or "60.0",
        "include_types": _as_type_list(source.get("include_types", defaults.get("include_types", []))),
        "exclude_types": _as_type_list(source.get("exclude_types", defaults.get("exclude_types", []))),
    }


def load_config() -> dict[str, Any]:
    """旧形式も含め、組織ごとの設定形式へ正規化して読み込む。"""
    if not CONFIG_PATH.exists():
        return {"current_org": "", "orgs": {}}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8", errors="replace") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"current_org": "", "orgs": {}}
    if not isinstance(raw, dict):
        return {"current_org": "", "orgs": {}}

    defaults = {
        "api_version": raw.get("api_version", "60.0"),
        "include_types": raw.get("include_types", []),
        "exclude_types": raw.get("exclude_types", []),
    }
    raw_orgs = raw.get("orgs")
    if isinstance(raw_orgs, dict):
        orgs = {str(alias): _normalise_org(str(alias), value, defaults) for alias, value in raw_orgs.items()}
    else:
        # 初期バージョンの単一組織設定を移行する。
        alias = str(raw.get("org_alias", raw.get("current_org", ""))).strip()
        orgs = {alias: _normalise_org(alias, raw, defaults)} if alias else {}

    current = str(raw.get("current_org", "")).strip()
    if current not in orgs:
        current = next(iter(orgs), "")
    # git_url のような将来のトップレベル設定は失わない。
    config = {key: value for key, value in raw.items() if key not in {"current_org", "orgs"}}
    config.update({"current_org": current, "orgs": orgs})
    return config


def save_config(config: dict[str, Any]) -> bool:
    """AV の一時ロックにも耐えられる原子的な設定保存。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".json.tmp")
    for attempt in range(5):
        try:
            with temporary.open("w", encoding="utf-8", errors="replace") as handle:
                json.dump(config, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, CONFIG_PATH)
            return True
        except OSError:
            time.sleep(0.2 * (attempt + 1))
    return False


def upsert_org(alias: str, *, is_sandbox: bool, api_version: str,
               include_types: list[str] | None = None,
               exclude_types: list[str] | None = None) -> dict[str, Any]:
    alias = alias.strip()
    if not alias:
        raise ValueError("組織エイリアスを入力してください。")
    if alias in {".", ".."} or Path(alias).name != alias:
        raise ValueError("組織エイリアスにはパス区切り文字を使用できません。")
    config = load_config()
    existing = config["orgs"].get(alias, {})
    config["orgs"][alias] = {
        "org_alias": alias,
        "is_sandbox": is_sandbox,
        "api_version": api_version.strip() or "60.0",
        "include_types": _as_type_list(include_types if include_types is not None else existing.get("include_types", [])),
        "exclude_types": _as_type_list(exclude_types if exclude_types is not None else existing.get("exclude_types", [])),
    }
    config["current_org"] = alias
    if not save_config(config):
        raise OSError("設定ファイルを保存できませんでした。")
    return config


def update_org_settings(alias: str, *, api_version: str,
                        include_types: list[str], exclude_types: list[str]) -> dict[str, Any]:
    config = load_config()
    if alias not in config["orgs"]:
        raise ValueError(f"未登録の組織です: {alias}")
    org = config["orgs"][alias]
    org["api_version"] = api_version.strip() or "60.0"
    org["include_types"] = _as_type_list(include_types)
    org["exclude_types"] = _as_type_list(exclude_types)
    config["current_org"] = alias
    if not save_config(config):
        raise OSError("設定ファイルを保存できませんでした。")
    return config


def delete_org(alias: str) -> dict[str, Any]:
    """設定だけを削除する。Salesforce 認証キャッシュとバックアップは保持する。"""
    config = load_config()
    config["orgs"].pop(alias, None)
    if config.get("current_org") == alias:
        config["current_org"] = next(iter(config["orgs"]), "")
    if not save_config(config):
        raise OSError("設定ファイルを保存できませんでした。")
    return config


def set_current_org(alias: str) -> dict[str, Any]:
    config = load_config()
    if alias not in config["orgs"]:
        raise ValueError(f"未登録の組織です: {alias}")
    config["current_org"] = alias
    if not save_config(config):
        raise OSError("設定ファイルを保存できませんでした。")
    return config


def clean_temp_dir() -> bool:
    """Memeta が所有する temp だけを削除する。"""
    if not TEMP_DIR.exists():
        return True
    for attempt in range(5):
        try:
            shutil.rmtree(TEMP_DIR)
            return True
        except OSError:
            time.sleep(0.5 * (attempt + 1))
    return not TEMP_DIR.exists()


def set_current_process(process: subprocess.Popen[bytes] | None) -> None:
    global _current_process
    with _process_lock:
        _current_process = process


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def kill_process_on_port(port: int = 1717) -> None:
    """OAuth リダイレクトのポートを使う残留プロセスだけを終了する。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return
    except OSError:
        return
    if os.name != "nt":
        return
    try:
        result = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=False,
                                creationflags=_creation_flags())
    except FileNotFoundError:
        return
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or f":{port}" not in fields[1]:
            continue
        pid = fields[-1]
        if pid.isdigit() and pid != "0":
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", check=False,
                           creationflags=_creation_flags())


def cancel_active_process() -> None:
    """現在の Salesforce CLI とその子プロセスを停止し、temp を清掃する。"""
    global _current_process
    with _process_lock:
        process = _current_process
        _current_process = None
    if process and process.poll() is None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", check=False,
                               creationflags=_creation_flags())
            else:
                process.terminate()
        except OSError:
            pass
    clean_temp_dir()


def authenticate_org(alias: str, is_sandbox: bool = True,
                     instance_url: str | None = None) -> tuple[bool, str]:
    """Salesforce CLI の web-server OAuth フローを起動する。

    CLI が既定ブラウザを自動で開く。認証成功後の設定保存は呼び出し側が行う。
    """
    alias = alias.strip()
    if not alias:
        return False, "組織エイリアスを入力してください。"
    sf_path = shutil.which("sf")
    if not sf_path:
        return False, "Salesforce CLI (sf) が PATH 上に見つかりません。`sf --version` を確認してください。"
    url = (instance_url or ("https://test.salesforce.com" if is_sandbox else "https://login.salesforce.com")).strip()
    if not url.startswith(("https://", "http://")):
        return False, "ログイン URL は https:// で始まる URL を指定してください。"
    kill_process_on_port(1717)
    command = [os.path.abspath(sf_path), "org", "login", "web", "--instance-url", url, "--alias", alias]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                check=False, creationflags=_creation_flags())
    except OSError as error:
        return False, f"Salesforce CLI を起動できませんでした: {error}"
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return True, output or f"組織 '{alias}' の認証に成功しました。"
    return False, output or f"OAuth 認証に失敗しました (exit code: {result.returncode})。"
