"""Memeta - Salesforce メタデータ取得・世代バックアップ GUI。"""
from __future__ import annotations

import datetime as dt
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

import config_manager
import metadata_fetcher
import package_generator

FONT_FAMILY = "BIZ UDPGothic"
BLUE = "#1589EE"
RED = "#D83A3A"
LOG_BG = "#2D2D2D"


def split_types(value: str) -> list[str]:
    """改行・カンマ区切り入力を、重複のないタイプ一覧にする。"""
    return list(dict.fromkeys(item.strip() for item in value.replace(",", "\n").splitlines() if item.strip()))


class MemetaApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Memeta - Salesforce 資材管理ツール")
        self.geometry("820x620")
        self.minsize(750, 500)
        self.app_config = config_manager.load_config()
        self.cancel_event = threading.Event()
        self.retrieve_running = False
        config_manager.clean_temp_dir()
        self._build_widgets()
        self.refresh_status()
        self.log("Memeta を起動しました。")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_widgets(self) -> None:
        style = ttk.Style(self)
        style.configure(".", font=(FONT_FAMILY, 10))
        style.configure("Action.TButton", font=(FONT_FAMILY, 10, "bold"), padding=8)
        style.configure("Danger.TButton", font=(FONT_FAMILY, 10, "bold"), padding=6)
        style.configure("TLabelframe.Label", font=(FONT_FAMILY, 10, "bold"))

        main = ttk.Frame(self, padding=15)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="Memeta", font=(FONT_FAMILY, 24, "bold"), foreground=BLUE).pack(pady=(0, 0))
        ttk.Label(main, text="Salesforce 資材管理ツール", font=(FONT_FAMILY, 10, "italic")).pack(pady=(0, 10))

        status = ttk.LabelFrame(main, text=" 現在の設定ステータス ", padding=12)
        status.pack(fill="x", pady=(0, 10))
        select_row = ttk.Frame(status)
        select_row.pack(fill="x")
        ttk.Label(select_row, text="接続環境 (Target Org):", font=(FONT_FAMILY, 10, "bold")).pack(side="left", padx=(0, 10))
        self.org_combo = ttk.Combobox(select_row, state="readonly", width=28)
        self.org_combo.pack(side="left", fill="x", expand=True)
        self.org_combo.bind("<<ComboboxSelected>>", self.on_org_selected)
        self.status_label = ttk.Label(status, text="API バージョン: 未設定")
        self.status_label.pack(anchor="w", pady=(6, 0))

        actions = ttk.Frame(main)
        actions.pack(fill="x", pady=(0, 10))
        for column in range(3):
            actions.columnconfigure(column, weight=1)
        self.config_button = ttk.Button(actions, text="⚙ 設定・接続環境の管理",
                                        command=self.open_config_window, style="Action.TButton")
        self.config_button.grid(row=0, column=0, padx=4, sticky="ew")
        self.package_button = ttk.Button(actions, text="📄 package.xml 作成",
                                         command=self.open_package_window, style="Action.TButton")
        self.package_button.grid(row=0, column=1, padx=4, sticky="ew")
        self.retrieve_button = ttk.Button(actions, text="📥 メタデータ取得",
                                          command=self.open_retrieve_window, style="Action.TButton")
        self.retrieve_button.grid(row=0, column=2, padx=4, sticky="ew")

        self.progress_frame = ttk.Frame(main)
        self.progress_label = ttk.Label(self.progress_frame, text="処理中...", foreground=BLUE,
                                        font=(FONT_FAMILY, 10, "bold"))
        self.progress_label.pack(fill="x", pady=(2, 2))
        self.progress = ttk.Progressbar(self.progress_frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 4))
        self.cancel_button = ttk.Button(self.progress_frame, text="🛑 処理の中止 (temp を清掃)",
                                        command=self.cancel_current_task, style="Danger.TButton")
        self.cancel_button.pack(pady=(2, 6))

        self.log_frame = ttk.LabelFrame(main, text=" 実行ログ（選択してコピー可能） ", padding=10)
        self.log_frame.pack(fill="both", expand=True)
        self.log_area = ScrolledText(self.log_frame, wrap="word", font=(FONT_FAMILY, 9), bg=LOG_BG,
                                     fg="#F1F1F1", insertbackground="white", state="disabled")
        self.log_area.pack(fill="both", expand=True)
        menu = tk.Menu(self.log_area, tearoff=0, font=(FONT_FAMILY, 10))
        menu.add_command(label="コピー", command=self.copy_log_selection)
        menu.add_command(label="すべて選択", command=self.select_all_log)
        self.log_context_menu = menu
        self.log_area.bind("<Button-3>", self.show_context_menu)
        self.log_area.bind("<Button-2>", self.show_context_menu)

    # ---- 共通 UI ----
    def log(self, message: str) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.after(0, self.log, message)
            return
        timestamp = dt.datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
        self.log_area.configure(state="normal")
        self.log_area.insert("end", f"{timestamp}{message}\n")
        self.log_area.configure(state="disabled")
        self.log_area.see("end")

    def show_context_menu(self, event: tk.Event) -> None:
        self.log_context_menu.tk_popup(event.x_root, event.y_root)

    def copy_log_selection(self) -> None:
        try:
            selected = self.log_area.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            return
        self.clipboard_clear()
        self.clipboard_append(selected)

    def select_all_log(self) -> None:
        self.log_area.tag_add(tk.SEL, "1.0", "end")
        self.log_area.focus_set()

    def refresh_status(self) -> None:
        self.app_config = config_manager.load_config()
        orgs = self.app_config.get("orgs", {})
        aliases = sorted(orgs)
        self.org_combo.configure(values=aliases)
        current = self.app_config.get("current_org", "")
        if current not in orgs:
            current = aliases[0] if aliases else ""
            if current:
                self.app_config = config_manager.set_current_org(current)
        self.org_combo.set(current if current else "未設定")
        org = orgs.get(current, {})
        if org:
            includes = org.get("include_types", [])
            excludes = org.get("exclude_types", [])
            include_text = f"対象: {len(includes)} 種" if includes else "対象: 全タイプ"
            self.status_label.configure(text=f"API バージョン: {org.get('api_version', '60.0')}    {include_text}    除外: {len(excludes)} 種")
        else:
            self.status_label.configure(text="API バージョン: 未設定")

    def on_org_selected(self, _event: tk.Event | None = None) -> None:
        alias = self.org_combo.get()
        if alias and alias != "未設定":
            try:
                self.app_config = config_manager.set_current_org(alias)
                self.refresh_status()
                self.log(f"アクティブな接続環境を '{alias}' に変更しました。")
            except (OSError, ValueError) as error:
                messagebox.showerror("設定エラー", str(error))

    def set_main_actions_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.config_button.configure(state=state)
        self.package_button.configure(state=state)
        self.retrieve_button.configure(state=state)
        self.org_combo.configure(state="readonly" if enabled else "disabled")

    def show_progress(self, text: str, *, indeterminate: bool = True) -> None:
        self.progress_label.configure(text=text)
        self.progress.configure(mode="indeterminate" if indeterminate else "determinate", maximum=100, value=0)
        self.progress_frame.pack(fill="x", pady=(2, 8), before=self.log_frame)
        if indeterminate:
            self.progress.start(10)

    def set_progress(self, percent: int, text: str) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=max(0, min(100, percent)))
        self.progress_label.configure(text=text)

    def hide_progress(self) -> None:
        self.progress.stop()
        self.progress_frame.pack_forget()

    # ---- 設定・OAuth ----
    def open_config_window(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("設定・接続環境の管理")
        dialog.geometry("660x710")
        dialog.minsize(600, 600)
        dialog.transient(self)
        dialog.grab_set()
        auth_in_progress = False

        def close_dialog() -> None:
            if auth_in_progress:
                messagebox.showinfo("OAuth 認証中", "OAuth 認証が完了または失敗するまで、この画面は閉じられません。", parent=dialog)
                return
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="接続環境の設定・登録", font=(FONT_FAMILY, 14, "bold")).pack(pady=(0, 10))

        existing = ttk.LabelFrame(frame, text=" 登録済み環境の設定変更 ", padding=12)
        existing.pack(fill="x", pady=(0, 12))
        ttk.Label(existing, text="対象環境").grid(row=0, column=0, sticky="w", pady=3)
        existing_alias = tk.StringVar(value=self.app_config.get("current_org", ""))
        existing_combo = ttk.Combobox(existing, textvariable=existing_alias, state="readonly", values=sorted(self.app_config.get("orgs", {})))
        existing_combo.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=3)
        existing.columnconfigure(1, weight=1)
        ttk.Label(existing, text="API バージョン").grid(row=1, column=0, sticky="w", pady=3)
        existing_api = ttk.Entry(existing)
        existing_api.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=3)
        ttk.Label(existing, text="対象タイプ（空欄=全タイプ）").grid(row=2, column=0, sticky="nw", pady=3)
        existing_include = ScrolledText(existing, height=4, font=(FONT_FAMILY, 9))
        existing_include.grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=3)
        ttk.Label(existing, text="除外タイプ").grid(row=3, column=0, sticky="nw", pady=3)
        existing_exclude = ScrolledText(existing, height=4, font=(FONT_FAMILY, 9))
        existing_exclude.grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=3)

        def load_existing(_event: tk.Event | None = None) -> None:
            alias = existing_alias.get()
            org = self.app_config.get("orgs", {}).get(alias)
            for widget in (existing_api, existing_include, existing_exclude):
                widget.configure(state="normal")
            existing_api.delete(0, "end")
            existing_include.delete("1.0", "end")
            existing_exclude.delete("1.0", "end")
            if not org:
                return
            existing_api.insert(0, org.get("api_version", "60.0"))
            existing_include.insert("1.0", "\n".join(org.get("include_types", [])))
            existing_exclude.insert("1.0", "\n".join(org.get("exclude_types", [])))

        def save_existing() -> None:
            alias = existing_alias.get()
            if not alias:
                return
            try:
                self.app_config = config_manager.update_org_settings(
                    alias, api_version=existing_api.get(),
                    include_types=split_types(existing_include.get("1.0", "end")),
                    exclude_types=split_types(existing_exclude.get("1.0", "end")),
                )
                self.refresh_status()
                self.log(f"環境 '{alias}' の設定を保存しました。")
                messagebox.showinfo("設定保存", f"環境 '{alias}' の設定を保存しました。", parent=dialog)
            except (OSError, ValueError) as error:
                messagebox.showerror("設定エラー", str(error), parent=dialog)

        def delete_existing() -> None:
            alias = existing_alias.get()
            if not alias:
                return
            if not messagebox.askyesno("環境の削除確認", f"設定から '{alias}' を削除しますか？\n認証情報とバックアップは残ります。", parent=dialog):
                return
            try:
                self.app_config = config_manager.delete_org(alias)
                self.refresh_status()
                self.log(f"環境 '{alias}' の設定を削除しました。")
                existing_combo.configure(values=sorted(self.app_config.get("orgs", {})))
                existing_alias.set(self.app_config.get("current_org", ""))
                load_existing()
            except OSError as error:
                messagebox.showerror("設定エラー", str(error), parent=dialog)

        existing_combo.bind("<<ComboboxSelected>>", load_existing)
        actions = ttk.Frame(existing)
        actions.grid(row=4, column=0, columnspan=2, pady=(8, 0), sticky="ew")
        existing_save_button = ttk.Button(actions, text="設定を保存", command=save_existing)
        existing_save_button.pack(side="left", padx=(0, 5))
        existing_delete_button = ttk.Button(actions, text="❌ 選択した環境の設定を削除", command=delete_existing)
        existing_delete_button.pack(side="left")
        load_existing()

        new = ttk.LabelFrame(frame, text=" ＋ 新規環境の追加・OAuth 認証 ", padding=12)
        new.pack(fill="x", pady=(0, 12))
        new.columnconfigure(1, weight=1)
        ttk.Label(new, text="組織エイリアス").grid(row=0, column=0, sticky="w", pady=3)
        new_alias = ttk.Entry(new)
        new_alias.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=3)
        ttk.Label(new, text="API バージョン").grid(row=1, column=0, sticky="w", pady=3)
        new_api = ttk.Entry(new)
        new_api.insert(0, "60.0")
        new_api.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=3)
        sandbox = tk.BooleanVar(value=True)
        sandbox_checkbox = ttk.Checkbutton(new, text="Sandbox 環境として接続する", variable=sandbox)
        sandbox_checkbox.grid(row=2, column=1, sticky="w", pady=3)
        ttk.Label(new, text="ログイン URL（任意）").grid(row=3, column=0, sticky="w", pady=3)
        new_url = ttk.Entry(new)
        new_url.grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=3)
        ttk.Label(new, text="空欄なら Sandbox は test.salesforce.com、本番は login.salesforce.com", foreground="#555555", font=(FONT_FAMILY, 8)).grid(row=4, column=1, sticky="w", pady=(0, 4))
        auth_status = ttk.Label(new, text="", foreground=BLUE)
        auth_progress = ttk.Progressbar(new, mode="indeterminate")

        def set_auth_controls(enabled: bool) -> None:
            state = "normal" if enabled else "disabled"
            for widget in (new_alias, new_api, new_url, existing_combo, existing_api, existing_include, existing_exclude,
                           sandbox_checkbox, existing_save_button, existing_delete_button):
                widget.configure(state=state if widget is not existing_combo else ("readonly" if enabled else "disabled"))
            login_button.configure(state=state)

        def start_auth() -> None:
            nonlocal auth_in_progress
            alias = new_alias.get().strip()
            api_version = new_api.get().strip() or "60.0"
            is_sandbox = bool(sandbox.get())
            url = new_url.get().strip() or None
            if not alias:
                messagebox.showerror("入力エラー", "新規組織エイリアスを入力してください。", parent=dialog)
                return
            if alias in self.app_config.get("orgs", {}) and not messagebox.askyesno(
                "再認証の確認", f"'{alias}' は既に登録されています。認証成功後に設定を更新しますか？", parent=dialog
            ):
                return
            # 設定は成功時まで保存しない。OAuth は CLI が既定ブラウザを開く。
            self.log(f"Salesforce 組織 '{alias}' の OAuth 認証を開始します。既定ブラウザが開きます。")
            auth_in_progress = True
            set_auth_controls(False)
            auth_status.configure(text="OAuth 認証を実行中です。ブラウザで Salesforce にログインしてください。")
            auth_status.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 2))
            auth_progress.grid(row=6, column=0, columnspan=2, sticky="ew")
            auth_progress.start(10)

            def worker() -> None:
                try:
                    result = config_manager.authenticate_org(alias, is_sandbox=is_sandbox, instance_url=url)
                except Exception as error:  # UI を必ず復帰させる。
                    result = (False, f"OAuth 認証の実行中に例外が発生しました: {error}")
                self.after(0, complete_auth, alias, api_version, is_sandbox, result)

            threading.Thread(target=worker, daemon=True).start()

        def complete_auth(alias: str, api_version: str, is_sandbox: bool, result: tuple[bool, str]) -> None:
            nonlocal auth_in_progress
            success, detail = result
            auth_in_progress = False
            auth_progress.stop()
            auth_progress.grid_remove()
            auth_status.grid_remove()
            set_auth_controls(True)
            if not success:
                self.log(f"【OAuth エラー】{detail}")
                messagebox.showerror("OAuth 認証に失敗しました", detail, parent=dialog)
                return
            try:
                existing_org = self.app_config.get("orgs", {}).get(alias, {})
                self.app_config = config_manager.upsert_org(
                    alias, is_sandbox=is_sandbox, api_version=api_version,
                    include_types=existing_org.get("include_types", []),
                    exclude_types=existing_org.get("exclude_types", []),
                )
            except (OSError, ValueError) as error:
                self.log(f"【設定保存エラー】{error}")
                messagebox.showerror("設定保存エラー", str(error), parent=dialog)
                return
            self.refresh_status()
            existing_combo.configure(values=sorted(self.app_config.get("orgs", {})))
            existing_alias.set(alias)
            load_existing()
            new_alias.delete(0, "end")
            self.log(f"【OAuth 成功】組織 '{alias}' を認証し、設定に追加しました。\n{detail}")
            messagebox.showinfo("OAuth 認証成功", f"組織 '{alias}' の認証に成功しました。", parent=dialog)

        login_button = ttk.Button(new, text="新規環境としてログイン認証を実行", command=start_auth, style="Action.TButton")
        login_button.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(frame, text="閉じる", command=close_dialog).pack(fill="x", pady=(2, 0))

    # ---- package.xml ----
    def open_package_window(self) -> None:
        orgs = self.app_config.get("orgs", {})
        if not orgs:
            messagebox.showerror("環境未設定", "先に設定画面から接続環境を OAuth 認証してください。")
            return
        dialog = tk.Toplevel(self)
        dialog.title("Package.xml の作成")
        dialog.geometry("780x720")
        dialog.minsize(680, 580)
        dialog.transient(self)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Package.xml の構成と作成", font=(FONT_FAMILY, 12, "bold"), foreground=BLUE).pack(anchor="w", pady=(0, 8))
        top = ttk.Frame(frame)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="対象接続環境:", font=(FONT_FAMILY, 10, "bold")).pack(side="left", padx=(0, 8))
        selected_org = tk.StringVar(value=self.app_config.get("current_org", ""))
        org_combo = ttk.Combobox(top, textvariable=selected_org, state="readonly", values=sorted(orgs), width=24)
        org_combo.pack(side="left")
        fetch_button = ttk.Button(top, text="🔍 メタデータタイプ一覧を取得")
        fetch_button.pack(side="left", padx=10)
        status = ttk.Label(frame, text="タイプを取得するか、直接編集して Package.xml を作成してください。",
                           foreground="#555555", wraplength=730)
        status.pack(anchor="w", pady=(0, 8))
        columns = ttk.Frame(frame)
        columns.pack(fill="both", expand=True, pady=(0, 8))
        columns.columnconfigure(0, weight=1)
        columns.columnconfigure(1, weight=1)
        wildcard_box = ttk.LabelFrame(columns, text=" ✅ ワイルドカード (*) で取得するタイプ ", padding=8)
        wildcard_box.grid(row=0, column=0, padx=(0, 5), sticky="nsew")
        wildcard_text = ScrolledText(wildcard_box, font=(FONT_FAMILY, 9))
        wildcard_text.pack(fill="both", expand=True)
        wildcard_text.insert("1.0", "\n".join(package_generator.DEFAULT_WILDCARD_TYPES))
        individual_box = ttk.LabelFrame(columns, text=" ⚠️ 固有名をスキャンするタイプ ", padding=8)
        individual_box.grid(row=0, column=1, padx=(5, 0), sticky="nsew")
        individual_text = ScrolledText(individual_box, font=(FONT_FAMILY, 9))
        individual_text.pack(fill="both", expand=True)
        individual_text.insert("1.0", "\n".join(package_generator.DEFAULT_INDIVIDUAL_TYPES))
        compatibility_box = ttk.LabelFrame(frame, text=" 🔒 Salesforce CLI 互換性検査 ", padding=8)
        compatibility_box.pack(fill="x", pady=(0, 8))
        compatibility_text = ScrolledText(compatibility_box, height=4, wrap="word", font=(FONT_FAMILY, 9))
        compatibility_text.pack(fill="x")
        compatibility_text.insert(
            "1.0", "タイプ一覧の取得時に、現在の Salesforce CLI で source 形式へ取得できない型を検出します。\n"
                   "Package.xml 作成時と Retrieve 直前にも同じ検査を実行します。"
        )
        compatibility_text.configure(state="disabled")
        map_holder: dict[str, Any] = {"official": {}}

        def show_compatibility(unsupported: list[str], message: str = "") -> None:
            compatibility_text.configure(state="normal")
            compatibility_text.delete("1.0", "end")
            if unsupported:
                compatibility_text.insert(
                    "1.0", "次の型は現在の Salesforce CLI で source 形式に取得できないため、"
                           "Package.xml から除外します。\n" + "\n".join(unsupported)
                )
            elif message:
                compatibility_text.insert("1.0", message)
            else:
                compatibility_text.insert("1.0", "CLI未対応のメタデータ型は検出されませんでした。")
            compatibility_text.configure(state="disabled")

        def filter_types(alias: str, wildcard: list[str], individual: list[str]) -> tuple[list[str], list[str]]:
            org = self.app_config.get("orgs", {}).get(alias, {})
            include = set(org.get("include_types", []))
            exclude = set(org.get("exclude_types", []))
            all_types = wildcard + individual
            if include:
                all_types = [item for item in all_types if item in include]
            all_types = [item for item in all_types if item not in exclude]
            return ([item for item in all_types if item not in package_generator.NON_WILDCARD_TYPES],
                    [item for item in all_types if item in package_generator.NON_WILDCARD_TYPES])

        def fetch_types() -> None:
            alias = selected_org.get()
            if not alias:
                messagebox.showerror("入力エラー", "対象環境を選択してください。", parent=dialog)
                return
            api_version = self.app_config["orgs"][alias].get("api_version", "60.0")
            fetch_button.configure(state="disabled")
            status.configure(text=f"【{alias}】からメタデータタイプ一覧を取得中...", foreground=BLUE)

            def worker() -> None:
                try:
                    success, data = package_generator.fetch_org_metadata_types(alias, api_version)
                except Exception as error:
                    success, data = False, {
                        "wildcard_types": package_generator.DEFAULT_WILDCARD_TYPES.copy(),
                        "individual_types": package_generator.DEFAULT_INDIVIDUAL_TYPES.copy(),
                        "official_map": {},
                        "unsupported_types": [],
                        "message": f"タイプ一覧の取得中に例外が発生しました: {error}",
                    }
                self.after(0, complete_fetch, alias, success, data)

            threading.Thread(target=worker, daemon=True).start()

        def complete_fetch(alias: str, success: bool, data: dict[str, Any]) -> None:
            fetch_button.configure(state="normal")
            wildcard, individual = filter_types(alias, data.get("wildcard_types", []), data.get("individual_types", []))
            wildcard_text.delete("1.0", "end")
            individual_text.delete("1.0", "end")
            wildcard_text.insert("1.0", "\n".join(wildcard))
            individual_text.insert("1.0", "\n".join(individual))
            map_holder["official"] = data.get("official_map", {})
            unsupported = list(data.get("unsupported_types", []))
            message = data.get("message", "")
            if success:
                discovered = data.get("discovered_count", len(wildcard) + len(individual))
                compatible = data.get("compatible_count", len(wildcard) + len(individual))
                status.configure(
                    text=f"【完了】{alias}: 組織検出 {discovered} 種 / CLI互換 {compatible} 種 / 除外 {len(unsupported)} 種",
                    foreground="#008800",
                )
                show_compatibility(unsupported)
                self.log(
                    f"【CLI互換性】{alias}: 組織検出 {discovered} 種 / "
                    f"CLI互換 {compatible} 種 / 除外 {len(unsupported)} 種"
                )
                if unsupported:
                    self.log(f"【CLI未対応のため除外】{', '.join(unsupported)}")
            else:
                status.configure(text="互換性を確認できなかったため、既定一覧を表示しています。", foreground=RED)
                show_compatibility([], message)
                self.log(f"【タイプ一覧の警告】{message}")

        def generate() -> None:
            alias = selected_org.get()
            wildcard = split_types(wildcard_text.get("1.0", "end"))
            individual = split_types(individual_text.get("1.0", "end"))
            if not alias or not (wildcard or individual):
                messagebox.showerror("入力エラー", "対象環境とメタデータタイプを指定してください。", parent=dialog)
                return
            api_version = self.app_config["orgs"][alias].get("api_version", "60.0")
            generate_button.configure(state="disabled")
            status.configure(text=f"【{alias}】の Package.xml を生成中（固有名スキャンを含む）...", foreground=BLUE)

            def worker() -> None:
                try:
                    result = package_generator.generate_custom_package_xml(
                        alias, wildcard, individual, api_version=api_version, official_map=map_holder["official"]
                    )
                except Exception as error:
                    result = False, f"Package.xml の生成中に例外が発生しました: {error}"
                self.after(0, complete_generate, result)

            threading.Thread(target=worker, daemon=True).start()

        def complete_generate(result: tuple[bool, str]) -> None:
            success, message = result
            generate_button.configure(state="normal")
            if success:
                status.configure(text="【成功】Package.xml を作成しました。", foreground="#008800")
                self.log(message)
                messagebox.showinfo("Package.xml 作成完了", message, parent=dialog)
            else:
                status.configure(text="【エラー】Package.xml の作成に失敗しました。", foreground=RED)
                self.log(f"【Package.xml エラー】{message}")
                messagebox.showerror("Package.xml 作成エラー", message, parent=dialog)

        fetch_button.configure(command=fetch_types)
        generate_button = ttk.Button(frame, text="📄 Package.xml を作成する", command=generate, style="Action.TButton")
        generate_button.pack(fill="x")

    # ---- Retrieve ----
    def open_retrieve_window(self) -> None:
        orgs = self.app_config.get("orgs", {})
        if not orgs:
            messagebox.showerror("環境未設定", "先に設定画面から接続環境を OAuth 認証してください。")
            return
        dialog = tk.Toplevel(self)
        dialog.title("メタデータ取得 (Retrieve)")
        dialog.geometry("540x430")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="メタデータ取得の一括実行", font=(FONT_FAMILY, 12, "bold"), foreground=BLUE).pack(pady=(0, 15))
        choices = ttk.LabelFrame(frame, text=" 取得対象環境（複数選択可） ", padding=12)
        choices.pack(fill="x", pady=(0, 15))
        variables: dict[str, tk.BooleanVar] = {}
        current = self.app_config.get("current_org", "")
        for index, alias in enumerate(sorted(orgs)):
            variable = tk.BooleanVar(value=alias == current)
            variables[alias] = variable
            ttk.Checkbutton(choices, text=alias, variable=variable).grid(row=index // 2, column=index % 2, sticky="w", padx=15, pady=5)
        ttk.Label(frame, text="各環境の data/<環境名>/package.xml を使い、取得結果を世代バックアップします。", foreground="#555555").pack(anchor="w", pady=(0, 15))

        def start() -> None:
            selected = [alias for alias, variable in variables.items() if variable.get()]
            if not selected:
                messagebox.showerror("入力エラー", "取得対象の環境を1つ以上選択してください。", parent=dialog)
                return
            dialog.destroy()
            self.start_retrieve_batch(selected)

        ttk.Button(frame, text="🚀 メタデータ取得を開始する", command=start, style="Action.TButton").pack(fill="x")

    def start_retrieve_batch(self, selected_orgs: list[str]) -> None:
        if self.retrieve_running:
            return
        self.retrieve_running = True
        self.cancel_event.clear()
        self.set_main_actions_enabled(False)
        self.show_progress(f"[0/{len(selected_orgs)}] メタデータ取得を準備中...")
        self.log(f"【一括メタデータ取得開始】対象環境: {', '.join(selected_orgs)}")
        threading.Thread(target=self.retrieve_worker_wrapper, args=(selected_orgs,), daemon=True).start()

    def retrieve_worker_wrapper(self, selected_orgs: list[str]) -> None:
        try:
            self.run_retrieve_batch(selected_orgs)
        except Exception as error:
            self.log(f"【予期しない Retrieve エラー】{error}")
            self.after(0, self.finish_retrieve_batch, 0, len(selected_orgs), self.cancel_event.is_set())

    def run_retrieve_batch(self, selected_orgs: list[str]) -> None:
        success_count = 0
        warning_count = 0
        empty_count = 0
        details: list[str] = []
        total = len(selected_orgs)
        for index, alias in enumerate(selected_orgs, start=1):
            if self.cancel_event.is_set():
                break
            org = self.app_config.get("orgs", {}).get(alias, {})
            api_version = org.get("api_version", "60.0")
            manifest = Path(__file__).resolve().parent / "data" / alias / "package.xml"
            self.log(f"--- 【環境 {index}/{total}: {alias}】メタデータ取得を開始します ---")
            if not manifest.exists():
                self.log(f"【エラー】{alias} の package.xml が見つかりません: {manifest}")
                details.append(f"【エラー: {alias}】package.xml が見つかりません: {manifest}")
                continue
            self.after(0, self.show_progress, f"[{index}/{total}] 【{alias}】メタデータを取得中...")
            result: tuple[str, Any] | None = None
            warnings: list[str] = []
            empty_notice: str | None = None
            for event_type, payload in metadata_fetcher.retrieve_metadata_stream(
                alias, manifest_path=manifest, cancel_event=self.cancel_event, api_version=api_version
            ):
                if event_type == "log":
                    self.log(f"[{alias}] {payload}")
                elif event_type == "status":
                    self.after(0, self.update_retrieve_status, f"[{index}/{total}] 【{alias}】{payload}")
                elif event_type == "warnings":
                    warnings.extend(payload)
                elif event_type == "diff":
                    details.append(f"【バックアップ差分: {alias}】\n{payload}")
                elif event_type == "empty":
                    empty_notice = payload
                elif event_type == "progress":
                    text = (f"[{index}/{total}] 【{alias}】取得中... {payload['percent']}% "
                            f"({payload['current']}/{payload['total']}) [{payload['meta_type']}]")
                    self.after(0, self.set_progress, int(payload["percent"]), text)
                elif event_type in {"success", "error"}:
                    result = (event_type, payload)
            if self.cancel_event.is_set() and (not result or result[0] != "success"):
                break
            if not result or result[0] != "success":
                detail = result[1] if result else "結果を取得できませんでした。"
                self.log(f"【エラー】{alias} のメタデータ取得に失敗しました。\n{detail}")
                details.append(f"【エラー: {alias}】\n{detail}")
                continue
            destination, _transcript = result[1]
            if empty_notice is not None:
                empty_count += 1
                label = "取得対象0件（警告あり）" if warnings else "取得対象0件"
                detail = f"【{label}: {alias}】\n{empty_notice}\n空の世代フォルダ: {destination}"
                if warnings:
                    detail += "\n\n【警告の詳細】\n" + "\n".join(warnings)
                details.append(detail)
                self.log(detail)
            elif warnings:
                warning_count += 1
                detail = (f"【警告あり完了: {alias}】\n保存先: {destination}\n"
                          "取得できた資材は保存済みです。以下の対象・理由を確認してください。\n"
                          + "\n".join(warnings))
                details.append(detail)
                self.log(detail)
            else:
                success_count += 1
                self.log(f"【成功】{alias} のメタデータ取得が完了しました。\n保存先: {destination}")
        cancelled = self.cancel_event.is_set()
        self.after(0, self.finish_retrieve_batch, success_count, total, cancelled, warning_count, details, empty_count)

    def update_retrieve_status(self, text: str) -> None:
        if self.retrieve_running and not self.cancel_event.is_set():
            self.progress_label.configure(text=text)

    def finish_retrieve_batch(self, success_count: int, total: int, cancelled: bool,
                              warning_count: int = 0, details: list[str] | None = None,
                              empty_count: int = 0) -> None:
        self.retrieve_running = False
        self.hide_progress()
        self.set_main_actions_enabled(True)
        if cancelled:
            summary = (f"メタデータ取得を中止しました。対象: {total} 環境\n"
                       f"中止までの正常完了: {success_count} / 警告あり完了: {warning_count}\n"
                       f"取得対象0件: {empty_count}\n"
                       "一時領域の清掃結果は計測ログを確認してください。")
        else:
            summary = (f"対象: {total} 環境\n正常完了: {success_count}\n"
                       f"警告あり完了: {warning_count}\n取得対象0件: {empty_count}\n"
                       f"エラー: {total - success_count - warning_count - empty_count}")
        title = "中止完了" if cancelled else "一括取得完了"
        self.log(f"【{title}】\n{summary}")
        if details:
            self.show_retrieve_details(title, summary, details)
        else:
            messagebox.showinfo(title, summary)

    def show_retrieve_details(self, title: str, summary: str, details: list[str]) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.geometry("850x550")
        dialog.transient(self)
        ttk.Label(dialog, text=summary, padding=12).pack(anchor="w")
        ttk.Label(dialog, text="バックアップ差分・警告・エラーの詳細（選択してコピーできます）", padding=8).pack(anchor="w")
        text = ScrolledText(dialog, wrap="word", padx=10, pady=10)
        text.pack(fill="both", expand=True, padx=12)
        text.insert("1.0", "\n\n".join(details))
        text.configure(state="disabled")
        ttk.Button(dialog, text="閉じる", command=dialog.destroy).pack(pady=10)

    def cancel_current_task(self) -> None:
        if not self.retrieve_running:
            return
        if not messagebox.askyesno("処理の中止確認", "実行中の処理を停止し、一時フォルダを清掃しますか？"):
            return
        self.cancel_event.set()
        config_manager.cancel_active_process()
        self.progress_label.configure(text="中止要求を送信しています。プロセス終了を待機中...")
        self.log("【中止要求】ユーザーによって処理の中止が要求されました。")

    def on_close(self) -> None:
        if self.retrieve_running:
            if not messagebox.askyesno("終了確認", "取得処理を中止してアプリケーションを終了しますか？"):
                return
            self.cancel_event.set()
            config_manager.cancel_active_process()
        else:
            config_manager.clean_temp_dir()
        self.destroy()


if __name__ == "__main__":
    MemetaApp().mainloop()
