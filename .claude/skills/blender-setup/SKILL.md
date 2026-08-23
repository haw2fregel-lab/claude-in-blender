---
description: claude-in-blender の初期セットアップ。Python 依存 → アドオンを Blender へ → 橋の登録 → 疎通確認まで一括で行う。「Blender のセットアップして」「セットアップして」で使う。
---

# claude-in-blender 初期セットアップ

このリポを初めて使う環境を、Blender のパネルから送信できる状態まで持っていく。
各ステップは失敗しても止まらず先へ進み、できなかったことは最後にまとめて報告する。

以下の `python` は、macOS / Linux では `python3` に読み替える（素の macOS に
`python` コマンドは無い）。

## 1. Python 依存

`python -m pip install -r requirements.txt`。

## 2. Extension zip のビルド

`python tools/build_extension.py` を実行し、出力された zip のパスを控える。

Blender が複数入っている環境では、候補を列挙して validate 前に止まる（zip は作られる）。
その時は列挙をそのまま利用者に見せ、どの Blender を使うか選んでもらってから
`python tools/build_extension.py --blender "<選ばれた path>"` で validate まで通す。
利用者に確認せず片方に決めない。

## 3. Blender へアドオンをインストール

インストール先は、手順2の validate に使った Blender と**同じ実行ファイル**。
`--blender` で選んだ場合はその path をそのまま使う。

手順2が単一候補で素通りした場合の探し先:

- Windows 標準: `C:\Program Files\Blender Foundation\Blender *\blender.exe`
- macOS 標準: `/Applications/Blender*.app/Contents/MacOS/Blender`
- PATH 上の `blender`

ここで複数見つかった時も、利用者に選んでもらう（validate と install の対象が割れたら、
選ばれた path で手順2からやり直す）。

見つかったら、決め打ちせず先に `"<blender>" --command extension --help` を実行して、
ローカル zip をインストールするサブコマンドの有無と正確な引数を確認する
（Blender のバージョンで異なりうる）。使えるなら repo `user_default` へ有効化込みで
インストールする。

CLI で入れられない場合（blender が見つからない・サブコマンドが無い・失敗した）は、
手動手順を案内して先へ進む:

> Blender の Edit > Preferences > Get Extensions → 右上の v メニュー →
> **Install from Disk** で `dist/claude_bridge-<version>.zip` を選ぶ

## 4. 橋の登録（repo + cwd + このセッション）

`python tools/bridge_register.py --cwd . --repo .` を実行する。スキル実行中は `CLAUDE_CODE_SESSION_ID`
でこのセッションを正確に登録する。出力に `(env)` が無い fallback 時のみ、session 末尾8文字を控える。

`--repo` はアドオンのソース（このリポ）。パネルはここから MCP サーバーを起動するので、
セットアップ時に一度書けば以後は変わらない——リポを移した時だけ、このスキルを打ち直す。

`--cwd` は Claude を起動する作業ディレクトリ。ここは `.mcp.json` が無くてよく、初回は
このリポで揃うが、以後は自分のプロジェクトへ移せる（そのリポで `/blender-bridge` を実行するか、
パネルのドロップダウンから選ぶ）。

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
  macOS / Linux で `python` コマンドが無い環境では、シェルの rc に
  `export CLAUDE_IN_BLENDER_PYTHON="$(command -v python3)"` を足してから
  開き直すよう案内する（パネル経由の fork は設定不要——パネルが自動で渡す）。
- Blender が未起動なら: 起動して 3D Viewport で N キー →「Claude」タブに
  「Work dir: <ディレクトリ名>」「Connected: ...<末尾8文字>」「Bridge: running」が
  出ることを確認してもらう。

## 報告

やったこと・できなかったこと（と手動での代替手順）を分けて報告する。
validate / install に使った Blender の path とバージョンを明記する（複数入っている
環境で、どれに入ったかを利用者があとから確かめられるように）。
パネルから一発送れば全経路のテストになることを添える。
