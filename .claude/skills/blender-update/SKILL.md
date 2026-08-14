---
description: 開発中のアドオンを実機 Blender に反映するホットリロード。zip ビルド → disable → 上書き → モジュール purge → enable を一括で行う。「アドオン更新して」「Blender に反映して」で使う。
---

# アドオンのホットリロード（開発者向け）

`claude_bridge/` を編集した後、Blender を再起動せずに実機へ反映する。
新規インストールは `blender-setup` へ。これは**上書き更新専用**。

> 送信処理が走っている最中（パネルが「処理中」表示）の更新は避ける。

## 1. ビルド

`python tools/build_extension.py` を実行。出力された zip のパスとバージョンを控える。

## 2. ホットリロード

Blender 操作の MCP ツール（`mcp__claude-in-blender__execute_code` 等）が使えるなら、
以下を実行する。**必ずこの形のまま使う**——処理を timer に逃がすのが本体で、
同期実行すると実行元の橋の timer を巻き込んで死なせる（実証済みの事故）。
enable 中の拡張の上書きは reload が掛からないことがあるため、
`sys.modules` の purge と明示 enable まで含めて一手（これも実証済み）。

```python
import bpy

def _do_update():
    import sys, traceback
    try:
        bpy.ops.preferences.addon_disable(module="bl_ext.user_default.claude_bridge")
    except Exception as e:
        print("[claude-bridge-update] disable:", e)
    try:
        bpy.ops.extensions.package_install_files(
            filepath=r"<zip の絶対パス>",
            repo="user_default", enable_on_install=False, overwrite=True)
    except Exception:
        traceback.print_exc()
        try:
            bpy.ops.preferences.addon_enable(module="bl_ext.user_default.claude_bridge")
        except Exception:
            traceback.print_exc()
        print("[claude-bridge-update] install failed — previous versionへの復帰は保証されない")
        return None
    for name in list(sys.modules):
        if name.startswith("bl_ext.user_default.claude_bridge"):
            del sys.modules[name]
    try:
        bpy.ops.preferences.addon_enable(module="bl_ext.user_default.claude_bridge")
        print("[claude-bridge-update] done")
    except Exception:
        traceback.print_exc()
    return None

bpy.app.timers.register(_do_update, first_interval=0.2)
result = "update scheduled"
```

## 3. 確認

数秒おいて `get_bridge_status` を叩き、`addon_version` が
manifest のバージョンと一致することを確認する。
上がっていなければ、もう一度 2 を実行（それでもダメなら Blender 再起動が確実）。
更新後に橋が死んで戻したい場合は、`git stash` 等で旧版の作業樹に戻して `python tools/build_extension.py` で旧 zip を作り、Blender の Install from Disk で入れ直す（このリポは git 管理なので旧版 zip はいつでも再現できる）。

## MCP が使えない場合

Blender の Edit > Preferences > Get Extensions → 右上の v メニュー →
**Install from Disk** で zip を選ぶ（enable 中でも上書きされるが、
反映には Blender 再起動が要ることがある）。
