---
description: Blender パネルにこのセッションの写しを持たせ、いるリポを作業ディレクトリにする。「Blender 繋いで」「橋かけて」「繋ぎ直して」で使う。
---

# 橋の繋ぎ直し

Blender パネル（claude-in-blender）に、いま話しているこのセッションの写しを持たせる。
同時に、**いま開いているリポがパネルの作業ディレクトリになる**。

このスキルと `tools/bridge_register.py` は claude-in-blender リポの中にあるので、
別のリポで Claude Code を開いてもここからは見えない。パネルの作業ディレクトリを
別のリポにしたい時は、claude-in-blender で
`python tools/bridge_register.py --cwd <path>` を実行するか、パネルのドロップダウンから
選ぶ（一度登録すれば履歴に残る）。ドロップダウン隣の＋なら、その場でファイルブラウザ
から選べる（一度きり——履歴には積まれない）。

アドオンのソース（`--repo`）は `/blender-setup` が登録した値をそのまま使うので、
ここでは触らない。まだ一度も setup していない環境では、先に `/blender-setup` へ。

1. `python tools/bridge_register.py --cwd .` を実行する。
   `--cwd` は Claude を起動する作業ディレクトリ。ここに `.mcp.json` は要らない。
   実行のたびにパネルのドロップダウン（`Work dir:`）へ履歴として積まれ、最新5件まで残る。
   スキル実行中のセッションでは `CLAUDE_CODE_SESSION_ID` を使い、正確に
   「このセッション」の fork 元として登録する。環境変数が無い場合だけ、cwd のプロジェクトの
   transcript のうち最新のものを fallback として使う。
2. 出力に `(env)` が無い場合（fallback 時）のみ、fork 元の末尾8文字をユーザーに見せ、
   Blender の N パネル「Claude」タブの「Connected: ... (fork pending)」表示と一致することを確認してもらう。
   並行セッションがあると別のセッションの写しを作ることがある——一致しなければ
   `python tools/bridge_register.py --cwd . --session-id <正しいID>` で指定し直す。
3. パネルの初回送信で fork 元から新しいセッションが作られる。以後パネルは自分の専用線を育て、
   元のセッションは掴まれず、追記もされない。fork の初回送信はキャッシュ割引なしの満額が一回かかる。

デスクトップ側は元のセッションをそのまま続けられる。パネル側の会話は fork 後の専用セッションで続く。
