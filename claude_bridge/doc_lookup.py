# -*- coding: utf-8 -*-
"""実行中の Blender から bpy / mathutils / bmesh / bpy_extras の API を引く。

bridge_server の get_doc コマンドから呼ばれる。返すのは data の中身だけで、
封筒 ({"ok", ...}) は呼び出し側が付ける。
"""

import bmesh
import bpy
import bpy_extras
import mathutils

_ROOT_MODULES = {
    "bpy": bpy,
    "mathutils": mathutils,
    "bmesh": bmesh,
    "bpy_extras": bpy_extras,
}
_ALLOWED_ROOTS = frozenset(_ROOT_MODULES)

_MAX_MATCHES = 30
_MAX_MEMBERS = 100
_MAX_WILDCARD_MEMBERS = 200


class DocLookupError(Exception):
    """引数が不正で検索まで進めない場合。呼び出し側が error 封筒に詰める。"""


# ── Lookup ────────────────────────────────────────────────


def _public_members(obj):
    return [m for m in dir(obj) if not m.startswith("_")]


def _keyword_search(keyword):
    matches = []

    for cat_name in dir(bpy.ops):
        if cat_name.startswith("_"):
            continue
        if keyword in cat_name.lower():
            matches.append(f"bpy.ops.{cat_name}")
        for op_name in dir(getattr(bpy.ops, cat_name)):
            if op_name.startswith("_"):
                continue
            qualified = f"bpy.ops.{cat_name}.{op_name}"
            if keyword in op_name.lower() or keyword in qualified.lower():
                matches.append(qualified)

    for type_name in dir(bpy.types):
        if type_name.startswith("_"):
            continue
        qualified = f"bpy.types.{type_name}"
        if keyword in type_name.lower() or keyword in qualified.lower():
            matches.append(qualified)

    for mod_name in ("mathutils", "bmesh"):
        for member in dir(_ROOT_MODULES[mod_name]):
            if member.startswith("_"):
                continue
            qualified = f"{mod_name}.{member}"
            if keyword in member.lower() or keyword in qualified.lower():
                matches.append(qualified)

    total = len(matches)
    return {
        "query": keyword,
        "matches": matches[:_MAX_MATCHES],
        "total": total,
        "truncated": total > _MAX_MATCHES,
    }


def lookup_doc(identifier):
    """identifier を解決して get_doc の data を返す。解決できなければ語句検索に落とす。"""
    identifier = (identifier or "").strip()
    if not identifier:
        raise DocLookupError("identifier is required")

    wildcard = identifier.endswith("*")
    if wildcard:
        identifier = identifier.rstrip("*").rstrip(".")
        if not identifier:
            raise DocLookupError(
                "identifier is required"
                " (bare wildcard is not allowed)"
            )

    parts = identifier.split(".")
    root = parts[0]
    if root not in _ALLOWED_ROOTS:
        if "." in identifier:
            raise DocLookupError(
                f"'{root}' is not an allowed root"
                f" (allowed: {', '.join(sorted(_ALLOWED_ROOTS))})"
            )
        return _keyword_search(identifier.lower())

    obj = _ROOT_MODULES[root]
    for part in parts[1:]:
        try:
            obj = getattr(obj, part)
        except AttributeError:
            return _keyword_search(identifier.lower())

    members = _public_members(obj)

    if wildcard:
        return {
            "identifier": identifier,
            "members": members[:_MAX_WILDCARD_MEMBERS],
            "total": len(members),
            "truncated": len(members) > _MAX_WILDCARD_MEMBERS,
        }

    # doc 本文はここでは切らない。応答サイズの契約は封筒を組む handler 側
    # （bridge_server._cmd_get_doc）が持つ。
    raw_doc = getattr(obj, "__doc__", None) or "No documentation available"
    return {
        "identifier": identifier,
        "type": type(obj).__name__,
        "doc": raw_doc,
        "members": members[:_MAX_MEMBERS],
        "total_members": len(members),
        "truncated": len(members) > _MAX_MEMBERS,
    }
