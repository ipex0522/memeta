# Memeta

Salesforce 組織のメタデータを `sf` CLI 経由で取得し、組織ごとに世代バックアップする Windows/macOS/Linux 向けの Python 3 + Tkinter GUI ツールです。

## 前提条件

- Python 3.10 以降（Tkinter を含む標準配布）
- Salesforce CLI (`sf`) が PATH 上にあり、対象組織へログインできること

追加の Python パッケージは不要です。

## 起動

```bash
python main.py
```

1. **設定・接続環境の管理**でエイリアス、ログイン URL、API バージョン、対象／除外タイプを登録します。
2. **package.xml 作成**で対象組織のメタデータタイプを取得し、必要なタイプを調整して manifest を保存します。
3. **メタデータ取得**で package.xml を持つ組織を選び、取得結果を `data/<org>/metadata/<日時>_<org>/` にバックアップします。

## 保存先と安全性

- 設定は `memeta_config.json`、manifest は `data/<org>/package.xml` に保存されます。
- Salesforce CLI の作業物は `temp/` のみを使用し、終了・中止・次回起動時にクリーンアップします。
- このツールが削除するのは `temp/` と設定から明示的に削除した組織エントリだけです。取得済みバックアップと Salesforce CLI の認証情報は削除しません。

## 注意

`sf org login web` はブラウザを開きます。OAuth のリダイレクトで使う 1717 番ポートに残ったプロセスがあれば、ログイン直前に終了を試みます。

