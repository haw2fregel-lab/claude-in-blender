# claude-in-blender

**Blender の中から、あなたの Claude Code に頼む。**

Blender の N パネルに小さな窓を開き、いま使っている Claude Code のセッションにそのまま依頼を送る逆方向連携です。頼んだ作業は Claude が同梱の MCP サーバー経由で Blender を直接操作して実行します——デスクトップでの会話の続きのまま、Blender から出ずに。

> 開発中（試作 v1.5）。動作確認: Blender 5.1.2 / Claude Code 2.1.228 / Windows

## 何ができる

- Blender の N パネル（サイドバー「Claude」タブ）から日本語で依頼を送る
- コンテキストトグル——「選択中を対象に」「シーン情報を見てから」「ドキュメントを調べてから」「スクショを確認してから」を、チェック一つで依頼に載せられる
- 応答側はデスクトップの Claude Code と**同じセッション**——会話の文脈・あなたの設定・CLAUDE.md がそのまま生きる
- 依頼された作業は同梱 MCP サーバーのツール経由で Blender を直接操作（シーンが目の前で変わる）

## 仕組みとセキュリティ

このリポジトリだけで完結する構成です。

1. **Blender アドオン（`claude_bridge/`）** — N パネルの UI と、MCP からの操作を受ける TCP 受け口
2. **MCP サーバー（`mcp_server/`）** — Claude Code に Blender 操作ツール 7 個（`execute_code`・`get_scene_info`・`get_doc` など）を提供
3. **あなたの Claude Code** — パネルからの依頼を受け、MCP ツールで Blender を操作

- Claude Code にログイン済みなら、そのまま動きます。**追加の API キーは不要**
- アドオンが認証情報（API キー・トークン）を読む・保存する・送信することはありません
- パネルから起動する Claude が使えるツールは同梱 MCP に限定しています（`--allowedTools` 指定）。PC のファイルを直接読み書きするツール（Read / Write / Bash）は渡していません
- ただし `execute_code` は Blender 内で Python を実行するツールです（シーン操作はこれで実現しています）。Blender のプロセスができることは原理上すべてできる、という前提で使ってください。環境変数 `CLAUDE_BRIDGE_EXECUTE=0` で無効化できます
- 受け口は 127.0.0.1:9877 のみ bind。接続には一時ディレクトリ経由のセッショントークンが必要です

## 必要なもの

- [Claude Code](https://claude.com/claude-code)（ログイン済み）
- Python 3.10+（MCP サーバー用。`pip install -r requirements.txt`）
- Blender 4.2+

## セットアップ（試作段階）

1. このリポジトリを clone して `pip install -r requirements.txt`
2. Claude Code をこのリポジトリで開く——同梱 `.mcp.json` が MCP サーバーを登録します（初回に承認を求められます）
3. アドオン zip をビルドして（下記）Blender にインストール: Edit > Preferences > Get Extensions → 右上の v メニュー → **Install from Disk**
4. Claude Code 側でセッション情報を `~/.claude/blender-bridge-session.json` に登録（現状は手書き。自動化予定）
5. 3D Viewport で N キー →「Claude」タブ → 依頼を書いて送信

### zip のビルド（開発者向け）

`claude_bridge/` の中身がルートに来る形で zip に固める。PowerShell なら:

```powershell
Compress-Archive -Path claude_bridge\* -DestinationPath dist\claude_bridge-1.5.0.zip
```

## ロードマップ

- [x] アドオン化（インストール式・Run Script 卒業）— v1.4 で Blender Extension 形式に
- [x] MCP サーバーの同梱 — v1.5 で自己完結に
- [x] コンテキストボタン（選択・ビューポートを見てもらう）— v1.5 でトグルに
- [ ] セッション登録の自動化
- [ ] 送信ロックと同時実行の安全策

## ライセンス

[GPL-3.0-or-later](LICENSE)。Blender アドオン（bpy 利用）は Blender 本体の GPL に従います。
