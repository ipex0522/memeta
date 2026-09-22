"""同じ組織の保存済みバックアップを、ファイル単位・バイト単位で読み取り比較する。"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


CHUNK_SIZE = 1024 * 1024
COMPARISON_NOTE = ("注意: バックアップ間の比較です。取得対象・権限・取得警告の違いでも増減します。"
                   "削除は Salesforce 上の削除を断定するものではありません。")


@dataclass
class BackupDiff:
    current: Path
    previous: Path | None
    added: list[str]
    removed: list[str]
    changed: list[str]
    unchanged: int = 0

    def summary(self) -> str:
        if self.previous is None:
            return "比較対象なし（同じ組織の前回バックアップがありません）。"
        return (f"追加: {len(self.added)} / 削除: {len(self.removed)} / "
                f"変更: {len(self.changed)} / 変更なし: {self.unchanged}")

    def format_text(self) -> str:
        lines = [f"今回: {self.current}", f"前回: {self.previous or 'なし'}", self.summary()]
        if self.previous is not None:
            previous_count = len(self.removed) + len(self.changed) + self.unchanged
            current_count = len(self.added) + len(self.changed) + self.unchanged
            lines.append(f"比較ファイル数: 前回 {previous_count}件 / 今回 {current_count}件")
            if previous_count == 0 and current_count == 0:
                lines.append("前回・今回ともに取得ファイルが0件のため、比較するファイルがありません。")
            elif current_count == 0:
                lines.append("今回は取得ファイルが0件です。前回のファイルがすべて差分上の削除として表示されます。")
            elif previous_count == 0:
                lines.append("前回は取得ファイルが0件のため、今回のファイルがすべて追加として表示されます。")
            lines.append(COMPARISON_NOTE)
            for label, paths in (("追加", self.added), ("削除", self.removed), ("変更", self.changed)):
                lines.append(f"\n【{label}: {len(paths)}件】")
                lines.extend(paths or ["なし"])
        return "\n".join(lines)


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise InterruptedError("差分比較を中止しました。取得済みバックアップは保存されています。")


def _is_link(path: Path) -> bool:
    # Windows の junction も追跡しない。3.10/3.11 では reparse point 属性で検出する。
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def find_previous_backup(current: Path, target_org: str) -> Path | None:
    """同組織・同保存先の命名規則に合う直前世代。partial や任意フォルダは対象外。"""
    pattern = re.compile(rf"^(\d{{8}}_\d{{6}})_{re.escape(target_org)}(?:_(\d+))?$")

    def generation(path: Path) -> tuple[datetime, int] | None:
        match = pattern.fullmatch(path.name)
        if not match:
            return None
        try:
            return datetime.strptime(match[1], "%Y%m%d_%H%M%S"), int(match[2] or 1)
        except ValueError:
            return None

    current_key = generation(current)
    if current_key is None:
        raise ValueError("今回バックアップの世代名を判定できません。")
    candidates = []
    for path in current.parent.iterdir():
        key = generation(path)
        if key is not None and key < current_key and path.is_dir() and not _is_link(path):
            candidates.append((key, path))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _files(directory: Path, cancel_event: threading.Event | None) -> dict[str, Path]:
    if _is_link(directory) or not directory.is_dir():
        raise OSError(f"比較対象が通常のディレクトリではありません: {directory}")
    result = {}
    pending = [directory]
    while pending:
        _check_cancel(cancel_event)
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                _check_cancel(cancel_event)
                path = Path(entry.path)
                if _is_link(path):
                    raise OSError(f"リンクを含むため比較できません: {path}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    result[path.relative_to(directory).as_posix()] = path
                else:
                    raise OSError(f"通常ファイルではないため比較できません: {path}")
    return result


def _same_content(before: Path, after: Path, cancel_event: threading.Event | None) -> bool:
    # mtime やキャッシュには頼らず、同サイズでも全バイトを確認する。バイナリも対象。
    if before.stat().st_size != after.stat().st_size:
        return False
    with before.open("rb") as left, after.open("rb") as right:
        while True:
            _check_cancel(cancel_event)
            chunk = left.read(CHUNK_SIZE)
            other = right.read(CHUNK_SIZE)
            if chunk != other:
                return False
            if not chunk:
                return True


def compare_backups(previous: Path, current: Path,
                    cancel_event: threading.Event | None = None) -> BackupDiff:
    before = _files(previous, cancel_event)
    after = _files(current, cancel_event)
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    changed = []
    unchanged = 0
    for name in sorted(before.keys() & after.keys()):
        _check_cancel(cancel_event)
        if _same_content(before[name], after[name], cancel_event):
            unchanged += 1
        else:
            changed.append(name)
    return BackupDiff(current, previous, added, removed, changed, unchanged)


def compare_with_previous(current: Path, target_org: str,
                          cancel_event: threading.Event | None = None) -> BackupDiff:
    _check_cancel(cancel_event)
    previous = find_previous_backup(current, target_org)
    if previous is None:
        return BackupDiff(current, None, [], [], [])
    return compare_backups(previous, current, cancel_event)
