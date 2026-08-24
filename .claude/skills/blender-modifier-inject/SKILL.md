---
description: "Blender で既存メッシュへ変形を掛けて、値をユーザーが後から触り続けられる形で渡す。標準モディファイアを Python で流し込む。「膨らませて」「ねじって」「あとで強さを触りたい」「非破壊で変形かけて」で起動。要求→種類の対応、範囲の絞り方、Apply の印鑑を持つ。"
---

# モディファイアを流し込む

## 使う時

**既にあるメッシュ**に変形を掛けて、値をユーザーが触り続けたい時。構成はこちらが書き、値はユーザーが**レンチタブの標準 UI** で握り続ける——スライダー、一時 OFF、並べ替え、全部 Blender 標準のまま。**非破壊**——掛けた変形はいつでも外せて、`obj.data` は元のまま残る。

隣の形が2つ——一発で決めて終わる操作は Skill `blender-quick-edit`。ゼロから形を作るなら Skill `blender-param-panel`。

## 組み立て

- `obj.modifiers.new(表示名, 'TYPE')` で追加——data API なので context 不問。**表示名は日本語が通る**。ユーザーが見て分かる名前を付ける
- パラメータはプロパティへ直接代入。**角度はラジアン**（`math.radians` で入れる）
- 範囲を絞るのは頂点グループ——`obj.vertex_groups.new(name=)` → `vg.add(頂点index列, weight, 'REPLACE')` → modifier の `vertex_group` へ名前を刺す。weight は 0〜1 のグラデで境界を柔らかくできる
- スタックは追加順に効く。並べ替えは `obj.modifiers.move(from_index, to_index)`
- type の正確な列挙: `bpy.types.ObjectModifiers.bl_rna.functions["new"].parameters["type"].enum_items`

## 要求の言葉 → type

| 要求 | type |
| --- | --- |
| 膨らませて・凹ませて | `DISPLACE`——法線方向に一様。テクスチャを刺すと模様で変位。球に寄せるなら `CAST` |
| ねじって・曲げて・先細り・引き伸ばし | `SIMPLE_DEFORM`——`deform_method` = TWIST / BEND / TAPER / STRETCH |
| 厚みを付けて | `SOLIDIFY` |
| 丸めて・滑らかに | 角の面取りは `BEVEL`、凹凸ならしは `SMOOTH`、細分化は `SUBSURF` |

残りは名前から引ける（`MIRROR`・`ARRAY`・`SCREW`・`SHRINKWRAP`…）。迷ったら上の enum を列挙する。

## 検証と渡し方

効きの検算は evaluated mesh で——`evaluated_depsgraph_get()` → `obj.evaluated_get(deps)` → `to_mesh()` の頂点を測り、使い終えたら `to_mesh_clear()`。`obj.data` の頂点は元のままが正しい状態（それが非破壊の証拠）。

ユーザーへは**プロパティエディタのレンチタブに、付けた名前でモディファイアが並んでいて、そこの数値で調整できる**と伝える。

## 境界

- **Apply（適用）は印鑑を待つ**——メッシュへ焼き付いてモディファイアがリストから消える一方通行。実行は対象を active にして `bpy.ops.object.modifier_apply(modifier=表示名)`
- **ファイルの保存・書き出しは印鑑を待つ。** モディファイアを積んで調整できる状態にするまでがこの形の範囲
- 順序で結果が変わる（ねじってから膨らませる ≠ 膨らませてからねじる）——ユーザーの言葉の順に積み、迷ったら見せて確認する
- `'NODES'`（Geometry Nodes）はツリー構築の別世界——この紙の外
