---
description: 開発中のアドオンを実機 Blender に反映するホットリロード。zip ビルド → disable → 上書き → モジュール purge → enable を一括で行う。「アドオン更新して」「Blender に反映して」で使う。
---

# アドオンのホットリロード（開発者向け）

`claude_bridge/` を編集した後、Blender を再起動せずに実機へ反映する。
新規インストールは `blender-setup` へ。これは**上書き更新専用**。

> 送信処理が走っている最中（パネルが「処理中」表示）の更新は避ける。

## 1. ビルド

`python tools/build_extension.py` を実行。出力された zip の絶対パスを控える。
（バージョン番号は控えても確認には使えない——手順 3 を見て。）

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

数秒おいて `get_bridge_status` を叩く。返ってくれば橋は生きている。
ただし **`addon_version` の一致だけでは、ホットリロードが現作業ツリーを拾った証拠にならない**——
開発中の変更は release version を変えずに入るから、中身だけ変えて
バージョン据え置きの更新が普通に起きる。
番号が一致していても、それは最初から一致していただけかもしれない。

証拠は、**今回入れた変更にしかない名前**で取る。反映したい diff から
新しく増えた関数・定数を一つ選び、実機のモジュールが持っているか見る。

```python
import sys
m = sys.modules.get("bl_ext.user_default.claude_bridge.bridge_server")
result = {"loaded": m is not None, "has_mark": hasattr(m, "<新版だけが持つ名前>")}
```

新版の名前を選ぶには `git show <commit> -- claude_bridge/bridge_server.py`
を `^\+(def |class |[A-Z_]+ =)` で絞ると早い（未コミットなら `git diff` で同じ）。
`panel.py` を変えたなら、そちらのモジュールを見る。

印が付いていなければ、もう一度 2 を実行（それでもダメなら Blender 再起動が確実）。
更新後に橋が死んで戻したい場合は、`git stash` 等で旧版の作業樹に戻して `python tools/build_extension.py` で旧 zip を作り、Blender の Install from Disk で入れ直す（このリポは git 管理なので旧版 zip はいつでも再現できる）。

## MCP が使えない場合

Blender の Edit > Preferences > Get Extensions → 右上の v メニュー →
**Install from Disk** で zip を選ぶ（enable 中でも上書きされるが、
反映には Blender 再起動が要ることがある）。
