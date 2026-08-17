---
description: claude-in-blender の初期セットアップ。Python 依存 → アドオンを Blender へ → 橋の登録 → 疎通確認まで一括で行う。「Blender のセットアップして」「セットアップして」で使う。
---

# claude-in-blender 初期セットアップ

このリポを初めて使う環境を、Blender のパネルから送信できる状態まで持っていく。
各ステップは失敗しても止まらず先へ進み、できなかったことは最後にまとめて報告する。

## 1. Python 依存

`python -m pip install -r requirements.txt`。

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

`python tools/bridge_register.py --cwd .` を実行する。スキル実行中は `CLAUDE_CODE_SESSION_ID`
でこのセッションを正確に登録する。出力に `(env)` が無い fallback 時のみ、session 末尾8文字を控える。

## 5. MCP 承認ゲートの確認

`python tools/check_mcp_approval.py` を実行し、JSON の `status` を確認する。

- `missing`: `entries` に出た各 project entry について、実際に不足している値を
  `hasTrustDialogAccepted: false → true`、`enabledMcpjsonServers`:
  `"claude-in-blender"` なし → 既存値を残して追加、と before/after で示す。
  `.claude.json.bak-<timestamp>` が先に作られることと、修復後は既存 fork を
  `/blender-bridge` で作り直す必要があることも伝え、書き換えてよいか確認する。
  承認されたら `python tools/check_mcp_approval.py --fix` を実行する。最終報告には
  「Claude Code の信頼ダイアログをスキル経由で承認済みに設定した」と明記する。
  断られたら書き換えず先へ進み、最終報告に「未修復」と残す。
- `ok` / `no-entry` / `no-file`: 何も書き換えず先へ進む。Claude Code を使っていない
  可能性があるため、`no-entry` / `no-file` は触らない。
- `broken`: 書き換えず、`.claude.json` を確認できなかったことだけ報告する。

## 6. 疎通確認

- このセッションに `mcp__claude-in-blender__*` ツールが生えているなら
  `get_bridge_status` を叩く。Blender が起動していればバージョン情報が返る。
- ツールが無いなら: このリポで Claude Code を開き直すと `.mcp.json` から
  MCP サーバーが登録される（初回は承認が出る）ことを案内する。
- Blender が未起動なら: 起動して 3D Viewport で N キー →「Claude」タブに
  「Connected: ...<末尾8文字>」と「Bridge: running」が出ることを確認してもらう。

## 報告

やったこと・できなかったこと（と手動での代替手順）を分けて報告する。
パネルから一発送れば全経路のテストになることを添える。
