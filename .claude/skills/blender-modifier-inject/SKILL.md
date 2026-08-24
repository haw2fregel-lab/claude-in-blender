---
description: "Blender で既存メッシュへ変形や散布を掛けて、値をユーザーが後から触り続けられる形で渡す。「モディファイアで」「ジオメトリノードで」「GNで」「非破壊で」で起動。標準モディファイアで足りれば一行、無ければ Geometry Nodes を Python で組んで流し込む。"
---

# モディファイアを流し込む

## 使う時

**既にあるメッシュ**に変形や散布を掛けて、値をユーザーが触り続けたい時。書くのは Python、触るのは**レンチタブの標準 UI**——スライダー、一時 OFF、並べ替え、全部 Blender 標準のまま。**非破壊**——いつでも外せて、`obj.data` は元のまま残る。

隣の形が2つ——一発で決めて終わる操作は Skill `blender-quick-edit`。ゼロから形を作って数値で詰めるのは Skill `blender-param-panel`。

道は二段。**要求が既製品の表に載るなら標準モディファイア**が最短。載らない動き（生やす・散らす・独自の法則で変形）は **Geometry Nodes ツリーを Python で組む**——モディファイア UI に並ぶソケットをユーザーが握る。

## 既製品で足りる時

- `obj.modifiers.new(表示名, 'TYPE')`——data API なので context 不問。**表示名は日本語が通る**。ユーザーが見て分かる名前を付ける
- パラメータはプロパティへ直接代入。**角度はラジアン**（`math.radians` で入れる）
- 範囲を絞るのは頂点グループ——`obj.vertex_groups.new(name=)` → `vg.add(頂点index列, weight, 'REPLACE')` → modifier の `vertex_group` へ名前を刺す。weight 0〜1 のグラデで境界が柔らかくなる
- スタックは追加順に効く。並べ替えは `obj.modifiers.move(from_index, to_index)`

| 要求 | type |
| --- | --- |
| 膨らませて・凹ませて | `DISPLACE`——法線方向に一様。テクスチャを刺すと模様で変位。球に寄せるなら `CAST` |
| ねじって・曲げて・先細り・引き伸ばし | `SIMPLE_DEFORM`——`deform_method` = TWIST / BEND / TAPER / STRETCH |
| 厚みを付けて | `SOLIDIFY` |
| 丸めて・滑らかに | 角の面取りは `BEVEL`、凹凸ならしは `SMOOTH`、細分化は `SUBSURF` |

残りは名前から引ける（`MIRROR`・`ARRAY`・`SCREW`・`SHRINKWRAP`…）。正確な列挙: `bpy.types.ObjectModifiers.bl_rna.functions["new"].parameters["type"].enum_items`

## GN を組む時

### 書く前に実機へ聞く

GN の型は「ジオメトリ」しかない——**繋がりさえすれば通り、中身がズレていても通る**。だから書く前にソケットを確かめる:

```python
[(s.name, s.type) for s in node.inputs]
[e.identifier for e in node.bl_rna.properties["distribute_method"].enum_items]
```

バージョンで入力は増減する——Distribute Points on Faces は `Density Max`・`Density`・`Density Factor` が並存し、効く1本は `distribute_method` で変わる。プリミティブは初回に bounds を実測する——Mesh Cone の原点は底面（z = 0〜depth）。

### 組み立て

- `bpy.data.node_groups.new(名前, "GeometryNodeTree")` → `interface.new_socket` で Geometry の INPUT/OUTPUT → `NodeGroupInput`/`NodeGroupOutput` を繋ぐ → `obj.modifiers.new(表示名, 'NODES')` → `mod.node_group = ng`
- 露出するのは**人が仕上げで触るものだけ**——`interface.new_panel(名前)` を作り `new_socket(..., parent=panel)`。ソケット追加のたびにパネルへ入れる。内部係数はコードの定数でよい
- **ソケットを後付けしたら `mod[s.identifier] = s.default_value` で注入する**——`default_value` はツリー側の定義で、既存モディファイアには 0 が入っている。キーは `"Socket_3"` 型の identifier であって名前とは別物
- `links.new` の後は `ng.update_tag()`——リンク追加は変更通知が飛ばないことがある
- 計算ノードに無い式が主役になったら Skill `blender-param-panel`（`bpy.props`＋再生成）へ——スクリプトを書けるノードは GN に無い

### 運用中のツリーは差分で patch

**ユーザーがスライダーを振った瞬間から、その値は資産。** 作り直し（`node_groups.remove` → 新規）は振った値を既定へ戻すので、運用中はノードとリンクの差分だけ足し引きする。ツリーを消すと既存モディファイアは `node_group` が `None` の空殻になる——掃除ループは `is None` も拾う。

## 検算と渡し方

効きの検算は evaluated mesh で——`evaluated_depsgraph_get()` → `obj.evaluated_get(deps)` → `to_mesh()` の頂点を測り、使い終えたら `to_mesh_clear()`。`obj.data` の頂点は元のままが正しい状態（それが非破壊の証拠）。

パラメータを振るテストは `mod[s.identifier] = 値` → `obj.update_tag()` → `bpy.context.view_layer.update()` → 再評価。枝ごとの寄与は `node.mute = True` で切って差分を測る。

ユーザーへは**プロパティエディタのレンチタブに、付けた名前でモディファイアが並んでいて、そこの数値で調整できる**と伝える。GN はパネル名ごとソケットがその下に出る。

## GN の中を見る——Store Named Attribute

GN の中間値は**属性に焼いて Python から読む**——`GeometryNodeStoreNamedAttribute`（`data_type`・`domain` を設定、`inputs["Name"]` へ `_log_` 接頭辞の名前）を測りたい所へ挟み、evaluated mesh の `em.attributes["_log_..."].data` から読む。

- **真偽を1個出す**——`abs(実測 - expect) < 1e-4` の形。一番の価値は期待値を先に言葉にさせられること——「コーンの底は表面に座る」と書いた瞬間、その仮定を確かめたか自問することになる。真偽1個は 0.01 の浮きを拾う——目視の分解能を超える
- **選別は意味で**——「天面のトゲ」は法線で選ぶ（`normal_z > 0.99`）。位置での選別は側面インスタンスの張り出しを拾う
- **足場として使う**——組んでいる間だけ挿し、真偽が通ったら `mute`、壊れたらミュートを外して測り直す
- Join Geometry で合流すると相手側のジオメトリは 0 埋め——識別のマーカー属性を一緒に焼く
- Viewer とスプレッドシートは**人間の画面専用**。こちらが読むのは属性、人間に分布を見てもらう局面では Viewer を勧める

## 罠

| 罠 | 症状 | 対処 |
| --- | --- | --- |
| ソケット後付け | 既存モディファイアに 0 が入り形が潰れる | `mod[s.identifier] = s.default_value` |
| `links.new` だけで終える | 反映されないことがある | `ng.update_tag()` |
| 運用中ツリーの作り直し | ユーザーの値が既定へ戻る・空殻モディファイアが残る | 差分 patch。掃除は `node_group is None` も拾う |
| プリミティブの原点を仮定 | エラーゼロのまま浮く・めり込む | 初回に bounds を実測（Cone は底面原点） |
| `dimensions` で検算 | 変形したのに値が変わらない | depsgraph 待ち。evaluated mesh の頂点から計算する |

## 境界

- **Apply（適用）は印鑑を待つ**——メッシュへ焼き付いてモディファイアがリストから消える一方通行。実行は対象を active にして `bpy.ops.object.modifier_apply(modifier=表示名)`
- **ファイルの保存・書き出しは印鑑を待つ。** モディファイアを積んで調整できる状態にするまでがこの形の範囲
- 順序で結果が変わる（ねじってから膨らませる ≠ 膨らませてからねじる）——ユーザーの言葉の順に積み、迷ったら見せて確認する
