---
description: "Blender modeling know-how (written in Japanese) — API pitfalls in 5.x, data names under a translated UI, Geometry Nodes, camera framing. Read once per session when a request starts with `[Sent from Blender]`."
---

# Blender モデリングの作法

実測環境: Blender 5.1.2 / 2026-08。バージョンが違えば §1 の確かめ方だけ守れば足りる。

> セッションで一度読めば十分。二回目以降の Blender 依頼で開き直さなくていい。

## A. 実機に聞いてから書く

学習知識の中心は Blender 4.x で、しかも英語 UI 前提。5.x の実機に対して「知ってるつもりの名前」を書くと静かに外れる。

### A-1. まずバージョンを確認する

`get_bridge_status` で Blender バージョンを見る。4.x の記憶で 5.x を書くなら、確認の回数を増やす。

### A-2. ノードは bl_idname で引く

```python
b = next(n for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
```

ノードの `name` は UI 言語で変わる。日本語 UI では `プリンシプルBSDF` になり、英語名を鍵にした参照は `KeyError` で落ちる。World の背景ノードも同じで、`ShaderNodeBackground` で引く。詳しくは §D。

### A-3. enum は推測せず列挙する

```python
[i.identifier for i in scene.render.bl_rna.properties["engine"].enum_items]
```

実例: Blender 5.1 の render engine は `('BLENDER_EEVEE', 'BLENDER_WORKBENCH', 'CYCLES')`。4.2〜4.5 にあった `BLENDER_EEVEE_NEXT` は存在しない（EEVEE Next が `BLENDER_EEVEE` を名乗るようになった）。

### A-4. 新しいノードはソケット一覧をダンプしてから使う

```python
[(s.name, s.type) for s in node.inputs]
```

実例: 5.1 の Curve to Mesh は radius 属性の自動適用が廃止され、`"Scale"` 入力ソケットに変わった。4.x の知識で Set Curve Radius を使うと、**エラーゼロで静かに無視される**。ソケット構成まで見て初めて、この種の仕様変更が見える。

### A-5. `get_doc` に出てこない名前は `hasattr` で実物に聞く

**`get_doc` は RNA プロパティを解決できない**——`Mesh.uv_layers` は引けないが存在する。引けなかった名前は、実行時に `hasattr` で確かめてから判断する。

### A-6. エラーの分岐は型と状態で書く

UI 言語設定によって例外メッセージまで翻訳されることがある（実測: 「ノードタイプ〜が未定義です」）。分岐は例外の型と、読み直した実際の状態で決める。

## B. 形の設計

### B-1. 形はプロファイル関数として設計する

「この形は何の関数か」を最初に決める（例: ティーポットの胴体 = 高さ t に対する半径 r(t) の回転体）。

プリミティブを置いて潰して埋め込む積み上げ方は、データが汚れ、後からの調整で壊れ、ポリゴンモデリングとの品質乖離が大きくなる。関数を先に決めておけば、調整は係数の変更で済む。

### B-2. 部品の位置・寸法は他部品のパラメータから導出する

例: フタのつまみの高さ = フタの上面位置から計算。取っ手の付け根 = 胴体プロファイル r(t) から計算。

絶対座標のベタ書きは「パラメータを触ったらフタが浮いた」型の破損を生む。**依存関係を先に設計してから組む。** 独立変数（露出パラメータ）と従属値（導出）を区別する。

### B-3. パラメータ露出は「人間が最終調整で触るもの」だけ

露出するのは人間が仕上げで触ると明言したものだけ。内部係数は昇格要求が出るまでコード定数でよい。露出したら用途別にグルーピングする（胴体／フタ／取っ手…）。追加のたびに末尾へ足すと散らかって使われなくなる。

### B-4. シルエット確認は軸ビュー、扁平確認は 3/4 ビュー

プロポーション（膨らみの重心・肩の張り）は FRONT/SIDE の平行投影で確認する。断面の扁平・奥行き方向の形は正面から見えない——3/4 ビューを 1 枚足す。

### B-5. パラメータで寄るか試してから、構造を直す

参照と形が違う時、まず現行パラメータで寄せられるか試す。寄らない部分だけが構造（カーブの種類・トポロジ）の問題であり、コード修正の対象。この切り分けをせずにコードから直すと、人間の調整領域を壊す。

### B-6. 印象は形状勾配の設計で作る

- smoothstep 補間は区間端で傾き 0 → 「肩が張る」「頂点が平らになる」
- 円弧（`sqrt(1-u**2)`）は端で急峻 → 「肩が丸く落ちる」ドーム
- 「重み・垂れ」は先端に向かって太らせる（テーパー逆転）。付け根を細くすると対比で強調される
- 「かわいい」系の丸みは、複合カーブより単純な凸弧の組み合わせが安定する

## C. オブジェクトの建て方

### C-1. オブジェクトは `bpy.data` で建てる

```python
mesh = bpy.data.meshes.new("Teapot_Base")
obj = bpy.data.objects.new("Teapot", mesh)
bpy.context.scene.collection.objects.link(obj)
```

生成系の operator より `bpy.data` が強い理由が二つある。

**理由1: operator は context を要求する。** 選択・追加・削除の operator は呼ばれた場所の context を見る。Scripting ワークスペースから送ると `poll() failed, context is incorrect` で止まり、`temp_override(window=..., area=..., region=...)` を渡しても通らないことがある（実測）。`bpy.data` は context を見ない。

**理由2: operator は翻訳名を付ける。** `primitive_*_add()` 系が作るデータには、UI 言語設定（新規データ名の翻訳）によって「立方体」等の名前が入る。`bpy.data.meshes.new("Teapot_Base")` なら名前は自分の手にある。

operator でしか届かない操作（modifier の適用、bake、UV 展開など）はそのまま使う。その時は `temp_override` で window / area / region を明示し、直後に `bpy.context.active_object` で参照を掴む——名前で引き直すと翻訳名に当たる。

### C-2. 同じ mesh を共有するなら material は object リンクへ

mesh を共有したまま個体ごとに色を変えるには、slot を mesh 側に 1 つ作ってから object 側へ切り替える。

```python
me.materials.append(None)
...
ob.material_slots[0].link = 'OBJECT'
ob.material_slots[0].material = mat
```

`ob.data.materials.append(mat)` は mesh 側に効くので、共有中の全 object が同じ色になる。

### C-3. 冪等構築（掃除 → 再構築）

部品単位で名前に接頭辞を付け、再実行時は接頭辞で掃除してから作り直す。

```python
for ob in [o for o in bpy.data.objects if o.name.startswith("HANDLE_")]:
    bpy.data.objects.remove(ob, do_unlink=True)
```

構築が途中で失敗しても残骸が残らず、定数を変えた再投下が安全になる。マテリアル・メッシュも同じ接頭辞で掃除する（object を消しても data ブロックは残る）。

## D. 日本語 UI

**API 識別子は UI 言語に依存しない。** `bl_idname`・ソケット名（`"Base Color"`, `"Emission Strength"`）・enum の identifier は、日本語 UI でも英語のまま通る。実測済み。

**翻訳されるのは name。** データブロックやノードの `name` は「新規データ名の翻訳」設定の影響を受ける。`use_nodes = True` で自動生成されるノードもここに入る——ops を使わなくても翻訳名が生える。

だから §A-2 の「name ではなく bl_idname で引く」が要る。

現在の設定はこう読む。

```python
v = bpy.context.preferences.view
{"language": v.language, "new_dataname": v.use_translate_new_dataname}
```

`use_translate_new_dataname` を切ると、**UI は日本語のまま name だけ英語**になる。人間の作業言語を変えずに翻訳名の事故だけ消せる。これはユーザーの環境設定なので、切りたいか聞いてから触る。

切らない前提で書くなら、§A-2 と §C-1 と §A-6 の三つで日本語環境は足りる。

## E. Geometry Nodes

### E-1. ソケットを後付けしたら既存モディファイアに手動注入

`interface.new_socket()` の `default_value` はツリー側の定義であり、**既存モディファイアには 0 が入る**。

```python
s = ng.interface.new_socket(name="Lid Sink", in_out='INPUT', socket_type='NodeSocketFloat')
s.default_value = 0.03
ng.interface_update(bpy.context)
mod[s.identifier] = 0.03   # ← これを忘れると後付け分が 0 初期化され、形が潰れる
```

### E-2. `links.new` したら `ng.update_tag()`

`default_value` の変更は通知が飛ぶが、**Python からのリンク追加は通知が発火しないことがある**。ツリーをスクリプトで変えたら `ng.update_tag()` を打ってから評価する。

それでも効かない時はリンクの問題ではない——§A-4 の仕様変更を疑い、バイパステストへ。

### E-3. 検証は evaluated mesh で

```python
deps = bpy.context.evaluated_depsgraph_get()
obj_eval = obj.evaluated_get(deps)
em = obj_eval.to_mesh()
try:
    ...  # len(em.vertices), max(v.co.z for v in em.vertices) 等を expect と比較
finally:
    obj_eval.to_mesh_clear()  # 一時メッシュは object 所有。明示解放する（公式仕様）
```

パラメータ変更テストは `mod[socket.identifier] = 値` → `obj.update_tag()` → `bpy.context.view_layer.update()` → 再評価。

### E-4. パラメータは interface パネルで用途別に

```python
panel = ng.interface.new_panel("Ears")
ng.interface.move_to_parent(socket_item, panel, index)
```

モディファイア UI で折りたたみグループになる。パネル分けはソケット追加のたびに行う（あとでまとめては散らかる）。

### E-5. ノードは人間が読める配置にする

人間がノードエディタを開いて直接調整・指示できることが、パラメータ露出と同じくらい重要。

- トポロジカル深さで左→右の列に並べる
- 部品ごとに NodeFrame（日本語ラベル可）で囲う
- 整形は形を変えない変更なので、検算は不変性で行う

## F. 見え方を確かめる

### F-1. 検証は `result`、スクリーンショットは見た目だけ

作れたか・数は合っているかは `result` に詰めて返す。`get_viewport_screenshot` / `capture_after` は、ライティング・マテリアル・構図といった**目でしか分からないもの**に限る。

### F-2. Material Preview はシーンのワールドを見ない

`shading.type = 'MATERIAL'` はスタジオ HDRI で照らす。シーンのワールド色やライトを反映した絵が見たいなら、こう切り替える。

```python
space.shading.type = 'RENDERED'
space.shading.use_scene_world = True
space.shading.use_scene_lights = True
```

### F-3. 撮る前にシェーダーコンパイルを待つ

マテリアルを大量に作った直後のスクリーンショットは、コンパイル中で真っ暗に写る（左上に進捗が出る）。実行直後に撮らず、read-only の確認を一つ挟んでから撮る。

### F-4. 画角は撮る前に計算で確かめる

「撮ってみたら見切れていた」を減らせる。

```python
from bpy_extras.object_utils import world_to_camera_view
p = world_to_camera_view(scene, cam, world_coord)   # 0..1 に収まっていれば画面内
```

被写体の `bound_box` の 8 点を投影して x/y の範囲を見る。はみ出していたらカメラ距離を変えて再計算する——スクリーンショットを撮り直すより安い。

### F-5. スクリーンショットの前にオーバーレイを畳む

サイドバー・ツールバー・グリッドが画面の面積を食う。

```python
space.show_region_ui = False
space.show_region_toolbar = False
space.overlay.show_overlays = False
```

## G. この MCP の道具

### G-1. 数行を超えるなら `write_scratch` → `execute_file`

長いスクリプトを `execute_code` で投げ直すのは高い。scratch にファイルとして置き、`edit_scratch` で部分だけ直して再実行する。構文エラーはファイル行番号付きでローカルに捕まる。

scratch は MCP server プロセスごとの名前空間にあり、別プロセスが書いた同名ファイルとは交わらない。

### G-2. `outcome_unknown` は `get_request_status` で決着させる

`get_request_status(request_id)` を呼んで、その依頼が `succeeded` か `failed` に落ちるのを観測する。観測されるまで次の execute はブロックされる。観測前に投げ直すと二重適用になる。

### G-3. 変更は非トランザクション

スクリプトが途中で落ちたら、**その手前までは適用済み**とみなす。状態を読んでから、未適用の分だけを流す。特に mesh・material を作るループの途中で落ちた時は、接頭辞での掃除（§C-3）から入り直すのが安全。

### G-4. 状態が不確かなら、読んでから書く

選択・寸法・既存の名前が分からない時は、read-only のクエリを先に投げて状態を確定する。確定した条件に対して、分岐のない実行スクリプトを書く。場合分けを内蔵したスクリプトより、二回に分けた方が短く済む。
