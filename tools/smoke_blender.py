#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""実機 Blender でしか確かめられない前提を検査する（CI から headless で呼ぶ）。

ファイル切替の検知は「WindowManager のカスタムプロパティは、ファイルを開くと
default に戻り、保存では戻らない」という挙動に乗っている。これは Blender が
文書で約束した契約ではなく、こちらが実測しただけの振る舞い。だから毎回確かめる。

失敗は例外で落とす。`--python-exit-code 1` を付けて呼ぶと exit code になる。
"""
import os
import sys
import tempfile

import bpy


def _check(label, got, want):
    if got != want:
        raise AssertionError(f"{label}: got {got!r}, want {want!r}")
    print(f"  ok  {label}")


def main():
    print(f"Blender {bpy.app.version_string}")
    work = tempfile.mkdtemp(prefix="claude-bridge-smoke-")
    blend = os.path.join(work, "smoke.blend")

    bpy.types.WindowManager.smoke_generation = bpy.props.IntProperty(default=0)
    bpy.types.WindowManager.smoke_flag = bpy.props.BoolProperty(default=False)

    bpy.context.window_manager.smoke_generation = 7
    bpy.context.window_manager.smoke_flag = True

    # 保存では戻らない。ここが戻ると、保存のたびに「切り替わった」と誤報する。
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    _check("a value written before saving survives the save",
           bpy.context.window_manager.smoke_generation, 7)
    _check("a bool written before saving survives the save",
           bpy.context.window_manager.smoke_flag, True)

    # ファイルを開くと default に戻る。この「戻り」が切替の合図そのもの。
    bpy.ops.wm.open_mainfile(filepath=blend)
    _check("opening a file resets the int to its default",
           bpy.context.window_manager.smoke_generation, 0)
    _check("opening a file resets the bool to its default",
           bpy.context.window_manager.smoke_flag, False)

    # 値だけが戻る。登録そのものが消えると getattr が落ちて検知が止まる。
    _check("the property registration itself survives the load",
           hasattr(bpy.types.WindowManager, "smoke_generation"), True)

    # 他のロード経路でも同じこと。パネルの確認はどの経路でも出したい。
    bpy.context.window_manager.smoke_generation = 3
    bpy.ops.wm.read_homefile(use_empty=True)
    _check("read_homefile resets it too",
           bpy.context.window_manager.smoke_generation, 0)

    print("all smoke checks passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - CI へ非ゼロで返すのが目的
        print(f"SMOKE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
