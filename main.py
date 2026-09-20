"""Memeta GUI entry point."""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import config_manager
import metadata_fetcher
import package_generator

BLUE, RED, LOG_BG = "#1589EE", "#D83A3A", "#2d2d2d"


def split_types(value: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in value.replace(",", "\n").splitlines() if item.strip()))


class MemetaApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        metadata_fetcher.cleanup_temp()
        self.title("Memeta - Salesforce 資材管理ツール")
        self.geometry("800x620")
        self.minsize(750, 500)
        self.config = config_manager.load_config()
        self.process_holder: dict = {"process": None}
        self.stop_event = threading.Event()
        self.running = False
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()

    def _build(self) -> None:
        header = ttk.Frame(self, padding=14)
        header.pack(fill="x")
        ttk.Label(header, text="Memeta", font=("Yu Gothic UI", 22, "bold"), foreground=BLUE).pack(side="left")
        self.status = ttk.Label(header, anchor="e")
        self.status.pack(side="right", fill="x", expand=True)
        buttons = ttk.Frame(self, padding=(14, 0))
        buttons.pack(fill="x")
        for col in range(3): buttons.columnconfigure(col, weight=1)
        ttk.Button(buttons, text="⚙ 設定・接続環境の管理", command=self.open_settings).grid(row=0, column=0, padx=4, sticky="ew")
        ttk.Button(buttons, text="📄 package.xml 作成", command=self.open_package_dialog).grid(row=0, column=1, padx=4, sticky="ew")
        ttk.Button(buttons, text="📥 メタデータ取得", command=self.open_retrieve_dialog).grid(row=0, column=2, padx=4, sticky="ew")
        self.progress_frame = ttk.Frame(self, padding=14)
        self.progress = ttk.Progressbar(self.progress_frame, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_text = ttk.Label(self.progress_frame, width=28)
        self.progress_text.pack(side="left", padx=8)
        self.stop_button = ttk.Button(self.progress_frame, text="🛑 処理の中止", command=self.stop_running)
        self.stop_button.pack(side="right")
        ttk.Label(self, text="実行ログ", padding=(14, 12, 14, 2)).pack(anchor="w")
        self.log = ScrolledText(self, height=22, bg=LOG_BG, fg="white", insertbackground="white", state="disabled")
        self.log.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.log.bind("<Button-3>", self.log_menu)
        self.refresh_status()

    def refresh_status(self) -> None:
        self.config = config_manager.load_config()
        alias = self.config.get("current_org", "")
        org = self.config.get("orgs", {}).get(alias, {})
        self.status.config(text=f"接続先: {alias or '未設定'}   API: {org.get('api_version', '-')}")

    def write_log(self, text: str) -> None:
        def update() -> None:
            self.log.config(state="normal"); self.log.insert("end", text + "\n"); self.log.see("end"); self.log.config(state="disabled")
        self.after(0, update)

    def log_menu(self, event: tk.Event) -> None:
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="コピー", command=lambda: self.clipboard_append(self.log.selection_get() if self.log.tag_ranges("sel") else ""))
        menu.add_command(label="すべて選択", command=lambda: (self.log.tag_add("sel", "1.0", "end"), self.log.focus_set()))
        menu.tk_popup(event.x_root, event.y_root)

    def set_progress(self, value: int, text: str) -> None:
        self.after(0, lambda: (self.progress.config(value=value), self.progress_text.config(text=text[:42])))

    def start_task(self, job, complete) -> None:
        if self.running: return
        self.running = True; self.stop_event.clear(); self.progress.config(value=0); self.progress_frame.pack(fill="x")
        def runner() -> None:
            try:
                result = job(); self.after(0, lambda: complete(result))
            except Exception as error:
                self.write_log(f"エラー: {error}"); self.after(0, lambda: messagebox.showerror("Memeta", str(error)))
            finally:
                self.running = False; self.after(0, self.progress_frame.pack_forget)
        threading.Thread(target=runner, daemon=True).start()

    def stop_running(self) -> None:
        self.stop_event.set()
        process = self.process_holder.get("process")
        if process: metadata_fetcher.kill_process_tree(process)
        metadata_fetcher.cleanup_temp(); self.write_log("中止要求を送信し、一時ファイルを清掃しました。")

    def open_settings(self) -> None:
        dialog = tk.Toplevel(self); dialog.title("設定・接続環境の管理"); dialog.transient(self); dialog.grab_set(); dialog.columnconfigure(1, weight=1)
        aliases = list(self.config.get("orgs", {})); selected = tk.StringVar(value=self.config.get("current_org", ""))
        fields = {}
        ttk.Label(dialog, text="登録済み環境").grid(row=0, column=0, padx=10, pady=8, sticky="w")
        combo = ttk.Combobox(dialog, textvariable=selected, values=aliases, state="readonly"); combo.grid(row=0, column=1, padx=10, pady=8, sticky="ew")
        for row, (label, key, default) in enumerate((("組織エイリアス", "alias", ""), ("ログイン URL", "url", "https://login.salesforce.com"), ("API バージョン", "api", "60.0")), 1):
            ttk.Label(dialog, text=label).grid(row=row, column=0, padx=10, pady=5, sticky="w"); fields[key] = ttk.Entry(dialog); fields[key].grid(row=row, column=1, padx=10, pady=5, sticky="ew"); fields[key].insert(0, default)
        sandbox = tk.BooleanVar(); ttk.Checkbutton(dialog, text="Sandbox", variable=sandbox).grid(row=4, column=1, padx=10, sticky="w")
        ttk.Label(dialog, text="対象タイプ（カンマ/改行）").grid(row=5, column=0, padx=10, pady=5, sticky="nw"); include = tk.Text(dialog, width=46, height=4); include.grid(row=5, column=1, padx=10, pady=5)
        ttk.Label(dialog, text="除外タイプ（カンマ/改行）").grid(row=6, column=0, padx=10, pady=5, sticky="nw"); exclude = tk.Text(dialog, width=46, height=4); exclude.grid(row=6, column=1, padx=10, pady=5)
        def load(*_) -> None:
            org = self.config.get("orgs", {}).get(selected.get(), {})
            for key, value in (("alias", selected.get()), ("api", org.get("api_version", "60.0"))): fields[key].delete(0, "end"); fields[key].insert(0, value)
            sandbox.set(org.get("is_sandbox", False)); include.delete("1.0", "end"); include.insert("1.0", "\n".join(org.get("include_types", []))); exclude.delete("1.0", "end"); exclude.insert("1.0", "\n".join(org.get("exclude_types", [])))
        combo.bind("<<ComboboxSelected>>", load)
        if selected.get(): load()
        def save() -> None:
            config_manager.upsert_org(fields["alias"].get(), is_sandbox=sandbox.get(), api_version=fields["api"].get(), include_types=split_types(include.get("1.0", "end")), exclude_types=split_types(exclude.get("1.0", "end"))); self.refresh_status(); messagebox.showinfo("Memeta", "設定を保存しました。")
        def login() -> None:
            save(); self.start_task(lambda: config_manager.authenticate_org(fields["url"].get(), fields["alias"].get()), lambda result: (self.write_log(result[1]), messagebox.showinfo("認証", "認証に成功しました。" if result[0] else "認証に失敗しました。")))
        def delete() -> None:
            alias = selected.get()
            if alias and messagebox.askyesno("確認", f"設定から {alias} を削除しますか？\n認証情報とバックアップは残ります。"):
                config_manager.delete_org(alias); self.refresh_status(); dialog.destroy()
        actions = ttk.Frame(dialog); actions.grid(row=7, column=0, columnspan=2, pady=10); ttk.Button(actions, text="保存", command=save).pack(side="left", padx=4); ttk.Button(actions, text="OAuth ログイン", command=login).pack(side="left", padx=4); ttk.Button(actions, text="❌ 選択した環境の設定を削除", command=delete).pack(side="left", padx=4)

    def open_package_dialog(self) -> None:
        alias = self.config.get("current_org", "")
        if not alias: return messagebox.showwarning("Memeta", "先に組織を設定してください。")
        dialog = tk.Toplevel(self); dialog.title("package.xml 作成"); dialog.grab_set()
        ttk.Label(dialog, text=f"対象組織: {alias}").pack(anchor="w", padx=12, pady=8)
        wildcard, named = tk.Text(dialog, width=54, height=9), tk.Text(dialog, width=54, height=7)
        ttk.Label(dialog, text="✅ ワイルドカード(*)可能タイプ").pack(anchor="w", padx=12); wildcard.pack(padx=12)
        ttk.Label(dialog, text="⚠️ 固有資材名スキャンが必要").pack(anchor="w", padx=12, pady=(8, 0)); named.pack(padx=12)
        def fetch_types() -> None:
            def job(): return package_generator.get_org_metadata_types(alias)
            def done(result):
                types, message = result
                include = self.config["orgs"][alias].get("include_types", []); exclude = set(self.config["orgs"][alias].get("exclude_types", [])); types = [t for t in (include or types) if t not in exclude]
                wildcard.delete("1.0", "end"); named.delete("1.0", "end"); wildcard.insert("1.0", "\n".join(t for t in types if t not in package_generator.FOLDERED_TYPES)); named.insert("1.0", "\n".join(t for t in types if t in package_generator.FOLDERED_TYPES)); self.write_log(message or f"{len(types)} タイプを取得しました。")
            self.start_task(job, done)
        def generate() -> None:
            api = self.config["orgs"][alias].get("api_version", "60.0")
            self.start_task(lambda: package_generator.generate_hybrid_package_xml(alias, split_types(wildcard.get("1.0", "end")), split_types(named.get("1.0", "end")), api), lambda result: (self.write_log(f"package.xml を保存しました: {result[0]}"), [self.write_log(w) for w in result[1]], messagebox.showinfo("Memeta", f"保存しました。\n{result[0]}")))
        area = ttk.Frame(dialog); area.pack(pady=10); ttk.Button(area, text="🔍 メタデータタイプ一覧を取得", command=fetch_types).pack(side="left", padx=5); ttk.Button(area, text="📄 Package.xml を作成する", command=generate).pack(side="left", padx=5)

    def open_retrieve_dialog(self) -> None:
        dialog = tk.Toplevel(self); dialog.title("メタデータ取得"); dialog.grab_set(); ttk.Label(dialog, text="取得する環境を選択").pack(anchor="w", padx=12, pady=8)
        choices = []
        for alias in self.config.get("orgs", {}):
            value = tk.BooleanVar(value=alias == self.config.get("current_org")); ttk.Checkbutton(dialog, text=alias, variable=value).pack(anchor="w", padx=16); choices.append((alias, value))
        def run() -> None:
            targets = [alias for alias, value in choices if value.get()]
            if not targets: return messagebox.showwarning("Memeta", "組織を1つ以上選択してください。")
            def job():
                results = []
                for alias in targets:
                    self.write_log(f"{alias} の取得を開始します。"); results.append(metadata_fetcher.retrieve_metadata_stream(alias, self.set_progress, self.write_log, self.process_holder, self.stop_event))
                return results
            self.start_task(job, lambda results: messagebox.showinfo("Memeta", f"{len(results)} 組織のバックアップが完了しました。")); dialog.destroy()
        ttk.Button(dialog, text="📥 選択した環境を取得", command=run).pack(pady=12)

    def on_close(self) -> None:
        if self.running: self.stop_running()
        metadata_fetcher.cleanup_temp(); self.destroy()


if __name__ == "__main__":
    MemetaApp().mainloop()
