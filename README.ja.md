# claude-in-blender

[English](README.md) | **日本語**

**Blender の中から、あなたの Claude Code に頼む。**

Blender の N パネルに小さな窓を開き、いま使っている Claude Code のセッションにそのまま依頼を送る逆方向連携。頼んだ作業は、Claude が同梱の MCP サーバー経由で Blender を直接操作して実行します——デスクトップでの会話の続きのまま、Blender から出ずに。

構成はこのリポジトリで完結します：Blender アドオン + 同梱 MCP サーバー + あなたの Claude Code。それだけ。

> 開発中（試作 v0.13.0）。1.0.0 は初回公開時。動作確認: Blender 5.1.2 / Claude Code 2.1.228 / Windows

## できること

- N パネル（サイドバー「Claude」タブ）から、普段の言葉で依頼を送る
- コンテキストトグル——「選択中を対象に」「シーン情報を見てから」「ドキュメントを調べてから」「スクショを確認してから」を、チェック一つで依頼に載せる
- 応答するのはデスクトップの Claude Code と**同じセッション**——会話の文脈・あなたの設定・CLAUDE.md がそのまま生きる
- Claude が実行したコードは Text エディタの **`claude_bridge_log`** に全文残る——何をされたか、後から全部追える

## 安全の話

- **追加のキー登録はありません**——認証も利用量も、ログイン済みの Claude Code のアカウントをそのまま使います。サブスクリプションログインならサブスクの枠で、API アカウントなら API の課金で動きます
- アドオンが認証情報（API キー・トークン）を読む・保存する・送信することはありません
- パネルから起動する Claude に渡るツールは同梱 MCP のものだけ——Claude Code のファイル操作は渡しません
- 受け口は 127.0.0.1 のみ bind。接続にはセッショントークンが必要です
- `execute_code` は Blender 内で Python を実行するツールです——Blender のプロセスにできることは原理上すべてできる、という前提で使ってください。環境変数 `CLAUDE_BRIDGE_EXECUTE=0` で無効化できます

## 必要なもの

- [Claude Code](https://claude.com/claude-code)（ログイン済み——サブスクリプションでも API アカウントでも。どちらで入っているかは事前に確認を）
- Python 3.10+（MCP サーバー用）
- Blender 4.2+（Extension 形式の対応下限。動作確認済みは 5.1.2 / Windows）

## 使い始める

1. このリポジトリを clone して Claude Code で開く——同梱 `.mcp.json` が MCP サーバーを登録します（初回に承認あり）
2. Claude Code に「**Blender のセットアップして**」と頼む（`/blender-setup`）——Python 依存・アドオン・橋の登録・疎通確認まで一括
3. 3D Viewport で N キー →「Claude」タブ → 依頼を書いて送信

別の会話の文脈をパネルに持たせたい時は「**Blender 繋いで**」（`/blender-bridge`）の一言。パネルはその会話の写し（fork）を専用セッションとして育てます——元の会話には何も追記されません。

## ライセンス

[GPL-3.0-or-later](LICENSE)。Blender アドオン（bpy 利用）は Blender 本体の GPL に従います。
