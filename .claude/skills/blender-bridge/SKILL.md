---
description: Blender パネルの接続先をこのセッションに繋ぎ直す。「Blender 繋いで」「橋かけて」「繋ぎ直して」で使う。
---

# 橋の繋ぎ直し

Blender パネル（claude-in-blender）の接続先を、いま話しているこのセッションに向ける。

1. `python tools/bridge_register.py --cwd .` を実行する。
   スキル実行中のセッションでは `CLAUDE_CODE_SESSION_ID` を使い、正確に
   「このセッション」として登録する。環境変数が無い場合だけ、cwd のプロジェクトの
   transcript のうち最新のものを fallback として使う。
2. 出力に `(env)` が無い場合（fallback 時）のみ、session 末尾8文字をユーザーに見せ、
   Blender の N パネル「Claude」タブの「接続先: ...」表示と一致することを確認してもらう。
   並行セッションがあると別のセッションを掴むことがある——一致しなければ
   `python tools/bridge_register.py --cwd . --session-id <正しいID>` で指定し直す。
3. 以後、パネルからの依頼はこの会話の文脈に届く。

デスクトップ側で同じセッションを開いて会話中はパネルから送らない。パネル作業の後にデスクトップで続ける時は、セッションを開き直す。
