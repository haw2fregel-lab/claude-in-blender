# claude-in-blender

[English](README.md) | **日本語**

**Blender の中から、Claude Code に頼む。**会話の文脈も CLAUDE.md も、そのまま写しに引き継ぐ——元の会話には一切書き込まない。

Blender の N パネルに小さな窓を開き、いま使っている Claude Code セッションの**写し（fork）**に依頼を送る逆方向連携。頼んだ作業は、Claude が同梱の MCP サーバー経由で Blender を直接操作して実行します——デスクトップでの会話の文脈を引き継いで、Blender から出ずに。

足すものはこのリポジトリで完結します：Blender アドオン + 同梱 MCP サーバー。あとは、すでに使っている Claude Code。それだけ。

> **v1.0.0** — 動作確認: Blender 5.1.2 / Claude Code 2.1.228 / Windows（macOS/Linux は未確認） · [変更履歴](CHANGELOG.md) · [セキュリティ](SECURITY.md)

## できること

- N パネル（サイドバー「Claude」タブ）から、普段の言葉で依頼を送る
- コンテキストトグル——「選択中を対象に」「シーン情報を見てから」「ドキュメントを調べてから」「スクショを確認してから」を、チェック一つで依頼に載せる。トグルが指すのは live な状態——「選択中」は Claude が確認した時点の選択で、送信の瞬間を固定するものではない
- 応答するのはデスクトップの Claude Code の**写し（fork）**——会話の文脈と CLAUDE.md を引き継ぎ、元の会話には一切書き込まれない。パネルは独自のモデル選択と、制限したツール/MCP 構成を使う
- `.blend` が切り替わると、次の送信前にパネルが警告し、ツール応答の `file_switched` が切替を報告する。進行中の操作は止めない
- Text エディタの **`claude_bridge_log`** には、送信したコード・result の先頭200文字・エラーメッセージが残る。応答に収まらない大きなエラー（2KB 超）は、message と traceback の全文がログ側に保存される。stdout/stderr は取り込まない。5,000行を超えると古い側を切り詰め、直近約2,500行を残す

## 安全の話

- **追加のキー登録はありません**——認証も利用量も、ログイン済みの Claude Code のアカウントをそのまま使います。サブスクリプションログインならサブスクの枠で、API アカウントなら API の課金で動きます。アドオン自身の処理として API キーを要求・使用することはありません。`execute_code` 経由の Python は Blender の全ファイル・ネットワークアクセスを持ちます
- **送信ボタンが承認です。**Claude Code 自身のファイル操作・シェルは無効化していますが、同梱 MCP のツールはその依頼について全て事前承認されます——送信後に確認は出ません
- **`execute_code` は sandbox ではありません**——Blender 内で Python を実行するので、Blender のプロセスにできることは原理上すべてできる前提で使ってください。`CLAUDE_BRIDGE_EXECUTE=0` で無効化できます
- **長い処理の間、Blender の画面は固まって見えます**——コードは Blender のメインスレッドで実行され、強制中断もロールバックもありません。重い作業を頼む前に保存してください
- **Claude はこのプロジェクトの過去セッションを検索できます**（`search_session_history`）。パネルの履歴一覧も同じ transcript を読んでいます
- **ログは `.blend` の中に保存されます**——`claude_bridge_log` は送信コード・result の抜粋・エラーメッセージ——大きなエラーでは traceback 全文——を含んだまま、ファイルと一緒に付いていきます。人に渡す前に消してください
- 同じユーザーで同時に有効化できる bridge は一つです（既定 port は 9877）。受け口は 127.0.0.1 のみ bind、接続にはセッショントークンが必要です——ただしこれは他のマシンに対する境界で、あなたの権限で動く他プロセスに対する境界ではありません

信頼の境界・データの置き場・止め方の全体は **[SECURITY.md](SECURITY.md)**（英語）にまとめています。

## 必要なもの

- [Claude Code](https://claude.com/claude-code)（ログイン済み——サブスクリプションでも API アカウントでも。どちらで入っているかは事前に確認を）
- Python 3.10+（MCP サーバー用）
- Blender 4.2+（Extension 形式の対応下限。動作確認済みは 5.1.2 / Windows）

## 使い始める

1. このリポジトリを clone して Claude Code で開く——同梱 `.mcp.json` が MCP サーバーを登録します（初回に承認あり）
2. Claude Code に「**Blender のセットアップして**」と頼む（`/blender-setup`）——Python 依存・アドオン・橋の登録・疎通確認まで一括
3. 3D Viewport で N キー →「Claude」タブ → 依頼を書いて送信

**Windows で日本語入力するときは、変換のたびに Enter で確定してください。**変換中に確定せず次の文字を打つと、確定済みの文字列が消えます。Blender 本体（GHOST）の IME 処理による制限で、アドオン側からは直せません（上流の修正 PR は未マージ）。

別の会話の文脈をパネルに持たせたい時は「**Blender 繋いで**」（`/blender-bridge`）の一言。パネルはその会話の写し（fork）を専用セッションとして育てます——元の会話には何も追記されません。

モデリングの作法は Skill として同梱しています（[`.claude/skills/blender-modeling/`](.claude/skills/blender-modeling/SKILL.md)）。パネルから送った依頼では、そのセッションの最初に一度だけ読まれます——Blender 5.x で名前や enum が変わった場所、日本語 UI でのデータ名の扱い、Geometry Nodes の検証手順など。**他のリポジトリでの Blender 作業にも、よかったらそのままコピーして使ってください。**

## ライセンス

[GPL-3.0-or-later](LICENSE)。Blender アドオン（bpy 利用）は Blender 本体の GPL に従います。
