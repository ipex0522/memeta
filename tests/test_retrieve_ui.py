"""Tk のウィンドウを開かず、一括取得の集計と通知を検証する。"""
import threading
import unittest
from unittest.mock import Mock, patch

import main


class RetrieveUiTests(unittest.TestCase):
    def make_app(self):
        app = Mock()
        app.app_config = {"orgs": {}}
        app.cancel_event = threading.Event()
        app.after.side_effect = lambda _delay, callback, *args: callback(*args)
        return app

    def test_batch_separates_clean_warning_and_failure(self):
        app = self.make_app()
        streams = [
            [("status", "経過 1秒"), ("success", ("backup/clean", "{}"))],
            [("warnings", ["Dashboard / permission denied", "AddressCountryCode-nl / unsupported language nl"]),
             ("success", ("backup/warning", "{}"))],
            [("error", "取得に失敗")],
        ]
        with patch.object(main.Path, "exists", return_value=True), \
             patch.object(main.metadata_fetcher, "retrieve_metadata_stream", side_effect=streams):
            main.MemetaApp.run_retrieve_batch(app, ["clean", "warning", "failed"])
        args = app.finish_retrieve_batch.call_args.args
        self.assertEqual(args[:4], (1, 3, False, 1))
        self.assertIn("Dashboard / permission denied", args[4][0])
        self.assertIn("unsupported language nl", args[4][0])
        self.assertIn("取得に失敗", args[4][1])
        app.update_retrieve_status.assert_called_once()

    def test_missing_manifest_is_in_details(self):
        app = self.make_app()
        with patch.object(main.Path, "exists", return_value=False):
            main.MemetaApp.run_retrieve_batch(app, ["missing"])
        args = app.finish_retrieve_batch.call_args.args
        self.assertEqual(args[:4], (0, 1, False, 0))
        self.assertIn("package.xml", args[4][0])

    def test_empty_result_is_not_described_as_saved_metadata(self):
        for warnings in ([], [("warnings", ["permission denied"])]):
            with self.subTest(warnings=warnings):
                app = self.make_app()
                stream = [("empty", "資材ファイルは保存されていません。"), *warnings,
                          ("success", ("empty-generation", "{}"))]
                with patch.object(main.Path, "exists", return_value=True), \
                     patch.object(main.metadata_fetcher, "retrieve_metadata_stream", return_value=stream):
                    main.MemetaApp.run_retrieve_batch(app, ["org1"])
                args = app.finish_retrieve_batch.call_args.args
                self.assertEqual(args[:4], (0, 1, False, 0))
                self.assertEqual(args[5], 1)
                detail = args[4][0]
                self.assertIn("取得対象0件", detail)
                self.assertIn("空の世代フォルダ", detail)
                self.assertNotIn("取得できた資材は保存済み", detail)
                if warnings:
                    self.assertIn("permission denied", detail)

    def test_empty_result_is_not_counted_as_error(self):
        app = self.make_app()
        main.MemetaApp.finish_retrieve_batch(app, 1, 4, False, 1, ["詳細"], 1)
        summary = app.show_retrieve_details.call_args.args[1]
        self.assertIn("正常完了: 1", summary)
        self.assertIn("警告あり完了: 1", summary)
        self.assertIn("取得対象0件: 1", summary)
        self.assertIn("エラー: 1", summary)

    def test_diff_reports_are_grouped_by_org_without_changing_success_counts(self):
        app = self.make_app()
        streams = [[("diff", "追加: 1 / 削除: 2 / 変更: 3\nclasses/A.cls"), ("success", ("one", "{}"))],
                   [("diff", "比較対象なし"), ("success", ("two", "{}"))]]
        with patch.object(main.Path, "exists", return_value=True), \
             patch.object(main.metadata_fetcher, "retrieve_metadata_stream", side_effect=streams):
            main.MemetaApp.run_retrieve_batch(app, ["org1", "org2"])
        args = app.finish_retrieve_batch.call_args.args
        self.assertEqual(args[:4], (2, 2, False, 0))
        self.assertIn("バックアップ差分: org1", args[4][0])
        self.assertIn("classes/A.cls", args[4][0])
        self.assertIn("バックアップ差分: org2", args[4][1])

    def test_cancel_after_backup_still_counts_saved_result(self):
        app = self.make_app()
        def stream(*args, **kwargs):
            app.cancel_event.set()
            yield "warnings", ["差分比較を中止。保存済み"]
            yield "success", ("saved", "{}")
        with patch.object(main.Path, "exists", return_value=True), \
             patch.object(main.metadata_fetcher, "retrieve_metadata_stream", side_effect=stream) as retrieve:
            main.MemetaApp.run_retrieve_batch(app, ["org1", "org2"])
        self.assertEqual(app.finish_retrieve_batch.call_args.args[:4], (0, 2, True, 1))
        retrieve.assert_called_once()

    def test_final_summary_and_warning_details(self):
        app = self.make_app()
        with patch.object(main.messagebox, "showinfo") as info:
            main.MemetaApp.finish_retrieve_batch(app, 1, 3, False, 1, ["警告詳細", "エラー詳細"])
        info.assert_not_called()
        args = app.show_retrieve_details.call_args.args
        self.assertIn("正常完了: 1", args[1])
        self.assertIn("警告あり完了: 1", args[1])
        self.assertIn("エラー: 1", args[1])
        self.assertEqual(args[2], ["警告詳細", "エラー詳細"])
        self.assertFalse(app.retrieve_running)
        app.set_main_actions_enabled.assert_called_once_with(True)

    def test_clean_completion_uses_simple_dialog(self):
        app = self.make_app()
        with patch.object(main.messagebox, "showinfo") as info:
            main.MemetaApp.finish_retrieve_batch(app, 2, 2, False)
        self.assertIn("警告あり完了: 0", info.call_args.args[1])
        self.assertIn("エラー: 0", info.call_args.args[1])
        app.show_retrieve_details.assert_not_called()

    def test_details_widget_receives_full_text(self):
        app = self.make_app()
        with patch.object(main.tk, "Toplevel"), patch.object(main.ttk, "Label"), \
             patch.object(main.ttk, "Button"), patch.object(main, "ScrolledText") as widget:
            main.MemetaApp.show_retrieve_details(app, "完了", "概要", ["資材A / 原因A", "資材B / 原因B"])
        widget.return_value.insert.assert_called_once_with("1.0", "資材A / 原因A\n\n資材B / 原因B")
        widget.return_value.configure.assert_called_once_with(state="disabled")

    def test_cancel_does_not_label_unattempted_orgs_as_errors(self):
        app = self.make_app()
        with patch.object(main.messagebox, "showinfo") as info:
            main.MemetaApp.finish_retrieve_batch(app, 1, 3, True)
        self.assertNotIn("エラー: 2", info.call_args.args[1])
        self.assertIn("中止", info.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
