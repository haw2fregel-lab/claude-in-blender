# -*- coding: utf-8 -*-
"""Claude Bridge — Blender から Claude Code の継続セッションに依頼を送る。

インストール:
  1. Edit > Preferences > Get Extensions → 右上メニュー → Install from Disk で zip を選ぶ
  2. 3D Viewport で N キー → 「Claude」タブ

構成:
  panel.py         — N パネル（依頼欄・コンテキストトグル・送信・応答表示）
  bridge_server.py — 同梱 MCP サーバー (mcp_server/) からの操作を受ける TCP 受け口
  doc_lookup.py    — get_doc コマンドの Blender API ドキュメント検索
"""
from . import bridge_server
from . import panel


def register():
    panel.register()
    bridge_server.start_server()


def unregister():
    bridge_server.stop_server()
    panel.unregister()
