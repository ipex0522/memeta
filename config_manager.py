"""Memeta の組織設定と Salesforce CLI 認証を扱うモジュール。"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "memeta_config.json"
DEFAULT_CONFIG: dict[str, Any] = {"current_org": "", "orgs": {}}


def _normalise_org(alias: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "org_alias": value.get("org_alias", alias),
        "is_sandbox": bool(value.get("is_sandbox", False)),
        "api_version": str(value.get("api_version", "60.0")),
        "exclude_types": list(value.get("exclude_types", [])),
        "include_types": list(value.get("include_types", [])),
    }


def load_config() -> dict[str, Any]:
    """設定を読み込み、旧来の単一組織形式も現在の形式へ移行する。"""
    if not CONFIG_PATH.exists():
        return {"current_org": "", "orgs": {}}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8", errors="replace") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"current_org": "", "orgs": {}}

    if isinstance(raw.get("orgs"), dict):
        orgs = {name: _normalise_org(name, item) for name, item in raw["orgs"].items() if isinstance(item, dict)}
        current = raw.get("current_org", "")
    else:
        # 旧形式: org_alias / api_version などが最上位にある設定。
        alias = str(raw.get("org_alias", raw.get("current_org", ""))).strip()
        orgs = {alias: _normalise_org(alias, raw)} if alias else {}
        current = alias
    config = {"current_org": current if current in orgs else next(iter(orgs), ""), "orgs": orgs}
    if config != raw:
        save_config(config)
    return config


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8", errors="replace") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)


def upsert_org(alias: str, *, is_sandbox: bool, api_version: str,
               include_types: list[str], exclude_types: list[str]) -> dict[str, Any]:
    alias = alias.strip()
    if not alias:
        raise ValueError("組織エイリアスを入力してください。")
    config = load_config()
    config["orgs"][alias] = {
        "org_alias": alias, "is_sandbox": is_sandbox, "api_version": api_version.strip() or "60.0",
        "include_types": include_types, "exclude_types": exclude_types,
    }
    config["current_org"] = alias
    save_config(config)
    return config


def delete_org(alias: str) -> dict[str, Any]:
    """設定エントリだけを消す。認証情報・取得済みデータには触れない。"""
    config = load_config()
    config["orgs"].pop(alias, None)
    if config.get("current_org") == alias:
        config["current_org"] = next(iter(config["orgs"]), "")
    save_config(config)
    return config


def set_current_org(alias: str) -> None:
    config = load_config()
    if alias not in config["orgs"]:
        raise ValueError(f"未登録の組織です: {alias}")
    config["current_org"] = alias
    save_config(config)


def kill_process_on_port(port: int = 1717) -> None:
    """OAuth リダイレクトの待受ポートが使われている場合に解放を試みる。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return
    except OSError:
        return
    if os.name == "nt":
        command = ["netstat", "-ano", "-p", "tcp"]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 5 and f":{port}" in fields[1]:
                subprocess.run(["taskkill", "/PID", fields[-1], "/T", "/F"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", check=False)
    else:
        subprocess.run(["pkill", "-f", f":{port}"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", check=False)


def authenticate_org(instance_url: str, alias: str) -> tuple[bool, str]:
    if not shutil.which("sf"):
        return False, "Salesforce CLI (sf) が PATH 上に見つかりません。"
    if not instance_url.strip() or not alias.strip():
        return False, "ログイン URL と組織エイリアスを入力してください。"
    kill_process_on_port(1717)
    result = subprocess.run(
        ["sf", "org", "login", "web", "--instance-url", instance_url.strip(), "--alias", alias.strip(), "--no-prompt"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output or ("認証を開始しました。" if result.returncode == 0 else "認証に失敗しました。")
