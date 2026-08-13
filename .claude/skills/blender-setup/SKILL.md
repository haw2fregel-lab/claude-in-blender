---
description: claude-in-blender の初期セットアップ。Python 依存 → アドオンを Blender へ → 橋の登録 → 疎通確認まで一括で行う。「Blender のセットアップして」「セットアップして」で使う。
---

# claude-in-blender 初期セットアップ

このリポを初めて使う環境を、Blender のパネルから送信できる状態まで持っていく。
各ステップは失敗しても止まらず先へ進み、できなかったことは最後にまとめて報告する。

## 1. Python 依存

`pip install -r requirements.txt`（`pip` が無ければ `python -m pip install -r requirements.txt`）。

## 2. Extension zip のビルド

`python tools/build_extension.py` を実行し、出力された zip のパスを控える。

## 3. Blender へアドオンをインストール

blender.exe を探す（複数あれば最新バージョン）:

- Windows 標準: `C:\Program Files\Blender Foundation\Blender *\blender.exe`
- PATH 上の `blender`

見つかったら、決め打ちせず先に `"<blender>" --command extension --help` を実行して、
ローカル zip をインストールするサブコマンドの有無と正確な引数を確認する
（Blender のバージョンで異なりうる）。使えるなら repo `user_default` へ有効化込みで
インストールする。

CLI で入れられない場合（blender が見つからない・サブコマンドが無い・失敗した）は、
手動手順を案内して先へ進む:

> Blender の Edit > Preferences > Get Extensions → 右上の v メニュー →
> **Install from Disk** で `dist/claude_bridge-<version>.zip` を選ぶ

## 4. 橋の登録（cwd + このセッション）

`python tools/bridge_register.py --cwd .` を実行し、出力された session 末尾8文字を控える。

## 5. 疎通確認

- このセッションに `mcp__claude-in-blender__*` ツールが生えているなら
  `get_bridge_status` を叩く。Blender が起動していればバージョン情報が返る。
- ツールが無いなら: このリポで Claude Code を開き直すと `.mcp.json` から
  MCP サーバーが登録される（初回は承認が出る）ことを案内する。
- Blender が未起動なら: 起動して 3D Viewport で N キー →「Claude」タブに
  「接続先: ...<末尾8文字>」と「受け口: 稼働中」が出ることを確認してもらう。

## 報告

やったこと・できなかったこと（と手動での代替手順）を分けて報告する。
パネルから一発送れば全経路のテストになることを添える。
