---
description: Blender パネルの接続先をこのセッションに繋ぎ直す。「Blender 繋いで」「橋かけて」「繋ぎ直して」で使う。
---

# 橋の繋ぎ直し

Blender パネル（claude-in-blender）の接続先を、いま話しているこのセッションに向ける。

1. `python tools/bridge_register.py --cwd .` を実行する。
   スクリプトは cwd のプロジェクトの transcript のうち最新のものを
   「このセッション」とみなして登録する。
2. 出力された session 末尾8文字をユーザーに見せ、Blender の N パネル
   「Claude」タブの「接続先: ...」表示と一致することを確認してもらう。
   並行セッションがあると別のセッションを掴むことがある——一致しなければ
   `python tools/bridge_register.py --cwd . --session-id <正しいID>` で指定し直す。
3. 以後、パネルからの依頼はこの会話の文脈に届く。
