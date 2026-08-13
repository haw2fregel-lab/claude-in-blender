# claude-in-blender

**Blender の中から、あなたの Claude Code に頼む。**

Blender の N パネルに小さな窓を開き、いま使っている Claude Code のセッションにそのまま依頼を送る逆方向連携です。頼んだ作業は Claude が同梱の MCP サーバー経由で Blender を直接操作して実行します——デスクトップでの会話の続きのまま、Blender から出ずに。

> 開発中（試作 v1.8.0）。動作確認: Blender 5.1.2 / Claude Code 2.1.228 / Windows

## 何ができる

- Blender の N パネル（サイドバー「Claude」タブ）から日本語で依頼を送る
- コンテキストトグル——「選択中を対象に」「シーン情報を見てから」「ドキュメントを調べてから」「スクショを確認してから」を、チェック一つで依頼に載せられる
- 応答側はデスクトップの Claude Code と**同じセッション**——会話の文脈・あなたの設定・CLAUDE.md がそのまま生きる
- セッション未接続でもパネルから復帰できる——直近セッション（5件）から選ぶか、新規セッションでそのまま送る
- 依頼された作業は同梱 MCP サーバーのツール経由で Blender を直接操作（シーンが目の前で変わる）
- Claude が実行したコードは Text エディタの **`claude_bridge_log`** に全文残る（時刻・結果付き。システムコンソールにも出力）——何をされたか後から追えるし、コピペで再利用もできる。約5000行で古い分から切り捨て、パネルからクリアも可能

## 仕組みとセキュリティ

このリポジトリだけで完結する構成です。

1. **Blender アドオン（`claude_bridge/`）** — N パネルの UI と、MCP からの操作を受ける TCP 受け口
2. **MCP サーバー（`mcp_server/`）** — Claude Code に Blender 操作ツール 10 個（`execute_code`・`get_scene_info`・`get_doc` など）を提供
3. **あなたの Claude Code** — パネルからの依頼を受け、MCP ツールで Blender を操作

- Claude Code にログイン済みなら、そのまま動きます。**追加の API キーは不要**
- アドオンが認証情報（API キー・トークン）を読む・保存する・送信することはありません
- パネルから起動する Claude は `--tools ""` で組み込みツールを無効化しており、使えるのは同梱 MCP のツールだけです。Claude Code のファイル操作は渡さず、scratch 専用領域への書き込み・差分編集は MCP の `write_scratch` / `edit_scratch` 経由に限定しています
- ただし `execute_code` は Blender 内で Python を実行するツールです（シーン操作はこれで実現しています）。Blender のプロセスができることは原理上すべてできる、という前提で使ってください。環境変数 `CLAUDE_BRIDGE_EXECUTE=0` で無効化できます
- Python 実行には30秒の上限がありますが、Blender の重い処理（レンダ・大規模 modifier など）に入ると途中で止められず、その間 UI も応答しなくなります。固まった場合は処理完了を待つか Blender を再起動してください
- 受け口は 127.0.0.1:9877 のみ bind。接続には一時ディレクトリ経由のセッショントークンが必要です

## 必要なもの

- [Claude Code](https://claude.com/claude-code)（ログイン済み）
- Python 3.10+（MCP サーバー用。`python -m pip install -r requirements.txt`）
- Blender 4.2+

## セットアップ

1. このリポジトリを clone して Claude Code で開く——同梱 `.mcp.json` が MCP サーバーを登録します（初回に承認を求められます）
2. Claude Code に「**Blender のセットアップして**」と頼む（`/blender-setup`）——Python 依存・アドオンのインストール・橋の登録・疎通確認まで一括でやってくれます
3. 3D Viewport で N キー →「Claude」タブ → 依頼を書いて送信

次から別の会話に繋ぎ直す時は「**Blender 繋いで**」（`/blender-bridge`）の一言で。パネル側からも、未接続なら直近セッションの選択・新規セッションでの送信ができます。

> 💡 長い会話のセッションに繋ぐと、送信のたびに履歴の読み込みで初動が重くなります。Blender 作業は軽い専用セッション（パネルの「新規セッションで送る」で作れる）に分けると速い。
>
> 💡 デスクトップ側で同じセッションを開いて会話している最中にパネルから送ると、履歴が混在します。パネルで作業した後にデスクトップで続きをやる時は、そのセッションを `claude -r <ID>` や `/resume` で開き直してください——パネルでのやり取りも履歴に入った状態で続きになります。

<details>
<summary>手動セットアップ（スキルを使わない場合）</summary>

1. `python -m pip install -r requirements.txt`
2. `python tools/build_extension.py` で zip をビルド（`dist/claude_bridge-<version>.zip`）
3. Blender の Edit > Preferences > Get Extensions → 右上の v メニュー → **Install from Disk** で zip を選ぶ
4. `python tools/bridge_register.py --cwd .` で橋（cwd + 今のセッション）を登録

</details>

開発中にアドオンを直したら「**アドオン更新して**」（`/blender-update`）——ビルドから実機へのホットリロードまで一括でやります。

## ロードマップ

- [x] アドオン化（インストール式・Run Script 卒業）— v1.4 で Blender Extension 形式に
- [x] MCP サーバーの同梱 — v1.5 で自己完結に
- [x] コンテキストボタン（選択・ビューポートを見てもらう）— v1.5 でトグルに
- [x] セッション登録の自動化 — v1.6 でスキル同梱（`/blender-setup`・`/blender-bridge`）+ パネルから選択/新規
- [x] 送信ロックと同時実行の安全策
- [x] 実行の安全策（busy ガード・タイムアウト整合・テスト整備）— v1.8

## ライセンス

[GPL-3.0-or-later](LICENSE)。Blender アドオン（bpy 利用）は Blender 本体の GPL に従います。
