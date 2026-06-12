# P5c knowledge 层行为规范（py-final-baseline 提取，经对抗核验）

> TS 移植权威。2026-06-12 4 路提取+对抗核验。
> 复用：@ke/codegraph、@ke/store(Weaviate **读**侧)、@ke/llm、@ke/recall、@ke/pipeline(structure+semantic)。本阶段补 Weaviate **写**侧（add/add_many/delete + 建表）。
> 关键：① graphology 替 NetworkX MultiDiGraph（多重有向边）；② Weaviate v3 dataType 字符串非数组(P2f 坑)、UUID=sha256[:32] 重排（add 用 entity_id、add_many 用 eid+str(i)）；③ Neo4j 边用 CREATE 非 MERGE、batch 500、_rel_type 正则 [^A-Za-z0-9_]→_ +upper +fallback REL；④ clear 共 3 次尝试(2 重试)。



---

# 【KnowledgeGraph(graphology) + Neo4j 后端】

All confirmed. I now have everything needed to produce the corrected spec. Let me compile the corrections.

Key findings from verification:

1. **`build_from` step 7** — spec says "domain.service_domain_mappings → Service 节点 `service://<id>`... 边 `rel_type="BELONGS_TO_DOMAIN"`". The source iterates `for did in m.business_domain_ids`, creating one edge per domain id, and the domain node must already exist (`if self._g.has_node(domain_id)`). The Service node `name` = `m.service_or_module_id`. Spec is imprecise on the inner loop and the existence guard.

2. **`build_from` step 8 RELATED_TO** — spec says "其他 → 边 `rel_type="RELATED_TO"`（term/domain→entity）". The source checks `term_id` FIRST (`if has_node(term_id)`), and only `elif` falls to `domain_id`. Also both branches require `has_node(se.structure_entity_id)`. The "implemented_by" branch requires `has_node(cap_id)` but does NOT check the entity node exists. Worth noting the term-priority order.

3. **clear retry** — `range(3)` with `attempt < 2` = 3 total attempts / 2 retries, sleep 1s. Spec says "最多重试 3 次" in two places — imprecise. Also spec's TS note #8 says "最多 3 次" — should be 3 total attempts.

4. **`iter_edges` default** — KnowledgeGraph.iter_edges default rel_type = `"RELATED"` (line 447), spec section III omits this.

5. **Neo4j `successors_excluding` returns DISTINCT**, memory version does not dedupe — behavior difference worth flagging.

6. **`find_service_id` in build_from** uses a `break` after first matching `belongs_to` in-edge — only follows ONE belongs_to chain. Spec step 9 says "沿 belongs_to 入边回溯" without noting the break/single-path quirk.

Let me write the corrected spec.

## 修正清单

经回源逐条核验，发现以下需修正/补充项（其余抽查点准确，包括：所有 Cypher 语句逐字、`_rel_type` 正则 `[^A-Za-z0-9_]`→`_`+`.upper()`+fallback `"REL"`、节点 MERGE 键、边用 CREATE 非 MERGE、batch chunk_size=500、driver 4 个超时常量、`_neo4j_sanitize` 递归逻辑、impact_closure step-counter 语义、`_type_to_rel` 映射表 19 项逐字、subgraph_for_service 兜底逻辑、模型字段名 `service_or_module_id`/`business_domain_ids`/`weight`/`business_concept_id`/`link_type`）：

1. **build_from 步骤 7（BELONGS_TO_DOMAIN 边）**：原 spec 漏写内层循环与存在性守卫。实际是 `for did in m.business_domain_ids` 逐个建边，且要求 `domain://<did>` 节点**已存在**（`if has_node`）才建边。Service 节点 `name = m.service_or_module_id`。
2. **build_from 步骤 8（RELATED_TO 边）**：原 spec 未点明 **term 优先**。源码先查 `term://<id>`（`if has_node(term_id) and has_node(entity)`），仅当 term 不存在时才 `elif` 落到 `domain://<id>`。两分支都要求实体节点存在；而 `IMPLEMENTED_BY` 分支只校验 `capability://` 存在、**不**校验实体节点存在。
3. **build_from 步骤 9（IN_DOMAIN 回溯）**：`find_service_id` 沿 `belongs_to` 入边回溯时带 `break`——每个节点**只跟随第一条** `belongs_to` 入边（单路径 DFS，非全展开）。TS 须保持此 break 语义。
4. **clear 重试次数**：`range(3)` + `if attempt < 2` = **共 3 次尝试 / 最多 2 次重试**，间隔 `sleep(1)`。原 spec 多处写"最多重试 3 次"不精确（应为"最多 3 次尝试"）。
5. **KnowledgeGraph.iter_edges 默认 rel_type**：`ed.get("rel_type", "RELATED")`，缺省值 `"RELATED"`（原 spec 第三节遗漏此默认值）。
6. **Neo4j 排除遍历返回 DISTINCT**：`successors_excluding_rel_types` / `predecessors_excluding_rel_types` 的 Cypher 含 `RETURN DISTINCT`（去重）；而 MemoryGraphBackend 同名方法**不去重**（可能含重复 target）。这是两后端的行为差异，TS 移植须知悉。

---

# KnowledgeGraph + Neo4j 后端行为规范（修正版）

## 一、抽象接口层（abstractions.py）

### `GraphBackendProtocol`（`@runtime_checkable` Protocol）

图后端协议，内存/Neo4j 后端必须满足：

| 方法 | 入参 | 返回 |
|---|---|---|
| `add_node(nid, **attrs)` | nid: str，任意 kv 属性 | None |
| `add_edge(source_id, target_id, rel_type, **attrs)` | 三个 str + 任意 kv | None |
| `has_node(nid)` | str | bool |
| `get_node(nid)` | str | `Optional[dict]`，含 "id" 字段 |
| `successors(nid, rel_type=None)` | nid: str，rel_type 可选 | `list[str]` |
| `successors_excluding_rel_types(nid, exclude_rel_types)` | nid: str，Sequence[str] | `list[str]` |
| `predecessors(nid, rel_type=None)` | nid: str，rel_type 可选 | `list[str]` |
| `predecessors_excluding_rel_types(nid, exclude_rel_types)` | nid: str，Sequence[str] | `list[str]` |
| `node_count()` | — | int |
| `edge_count()` | — | int |
| `clear()` | — | None |
| `close()` | — | None |

排除遍历的 docstring 约定：**exclude 传小写名即可**（如 `implements`）；内部会同时排除 `r.rel_type` 属性与 Neo4j 关系类型。

### `ImpactClosureCapable`（可选 Protocol，关键字参数）

```python
impact_closure(start_id: str, *, direction: str = "down", max_depth: int = 50) -> set[str] | list[str]
```
注意 Protocol 声明里 `direction`/`max_depth` 是 keyword-only（`*`）；但具体实现（Memory/Neo4j/KnowledgeGraph）的 `impact_closure` 均为**位置可传**（无 `*`）。

### `TraversalWithExclusionsCapable`（可选 Protocol）

```python
successors_excluding_rel_types(nid, exclude_rel_types: Sequence[str]) -> list[str]
predecessors_excluding_rel_types(nid, exclude_rel_types: Sequence[str]) -> list[str]
```

### `VectorStoreProtocol`（额外存在，spec 未列，供参考）

`add(entity_id, vector, **kwargs)` / `add_many(items)` / `size()` / `search_by_vector(query_vector, top_k=10)→list[(id,score)]` / `search_by_text(query_text, top_k=10)→list[(id,score)]` / `get_by_entity_id(entity_id)→Optional[dict]` / `clear()`。

---

## 二、MemoryGraphBackend（memory_graph_backend.py）

**用途**：基于 NetworkX `MultiDiGraph` 的纯内存图后端，接口与 `Neo4jGraphBackend` 一致。

**底层数据结构**：`nx.MultiDiGraph`，同一对节点间允许多条边，边以 `(source, target, key)` 三元组唯一标识。

### 公开 API

```python
add_node(nid: str, **attrs) -> None
    # None 值属性被过滤（dict 推导 if v is not None），不写入图

add_edge(source_id: str, target_id: str, rel_type: str, **attrs) -> None
    # rel_type 作为关键字存入边属性：g.add_edge(s, t, rel_type=rel_type, **attrs)
    # 注意：add_edge 不过滤 None 属性（与 add_node 不同）

has_node(nid: str) -> bool

get_node(nid: str) -> Optional[dict]
    # 节点不存在返回 None；存在则 dict 拷贝并注入 "id" = nid

successors(nid: str, rel_type: Optional[str] = None) -> list[str]
    # 节点不存在返回 []
    # rel_type 比较：_rel_matches → str(edge_rel or "").lower() == str(want).lower()；None 不过滤

successors_excluding_rel_types(nid, exclude_rel_types: Sequence[str]) -> list[str]
    # exc = {strip().lower() for x if x.strip()}；空 exc → successors(None)
    # 不去重（可能返回重复 target）

predecessors(nid, rel_type=None) -> list[str]
predecessors_excluding_rel_types(nid, exclude_rel_types) -> list[str]   # 不去重

node_count() -> int   # number_of_nodes()
edge_count() -> int   # number_of_edges()

impact_closure(start_id, direction="down", max_depth=50) -> Set[str]   # 见第四节

clear() -> None
close() -> None  # 无操作（pass）
```

---

## 三、KnowledgeGraph（graph.py）

**用途**：图谱构建入口。内部维护 `nx.MultiDiGraph`（`_g`）+ 节点属性镜像字典（`_node_attrs`）+ 可选向量库（`_vector_store`）。`build_from` 是唯一写入路径；查询路径直接操作 `_g`。

### 构造

```python
__init__(self)
# _g: nx.MultiDiGraph
# _node_attrs: dict[str, dict]
# _vector_store: Optional[VectorStore] = None
# _version: Optional[str] = None
```

### clear()

清空 `_g`、`_node_attrs`；若有向量库则 duck-type 调用其 `clear()` 与 `close()`（close 异常吞掉）；`_version = None`。`build_from` 开头即调用 `self.clear()`。

### 主建图接口

```python
build_from(
    structure_facts: StructureFacts,
    semantic_facts: SemanticFacts,
    domain: DomainKnowledge,
    vector_enabled: bool = False,
    vector_dim: int = 64,
    graph_backend: str = "memory",       # "memory" | "neo4j"
    vector_backend: str = "memory",      # 形参存在但未在体内直接使用（实际后端来自 vector_config["backend"]）
    graph_config: Optional[dict] = None,
    vector_config: Optional[dict] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    force_full: bool = False,
) -> None
```

> 注：`vector_backend` 形参存在，但 vector_store 实际后端取自 `vector_config.get("backend", "memory")`，而非该形参。TS 移植需保留形参但以 vector_config 为准。

`graph_config` 字段（从 dict 读，带默认 fallback）：
- `neo4j_uri`（fallback `"bolt://localhost:7687"`）
- `neo4j_user`（fallback `"neo4j"`）
- `neo4j_password`（fallback `"password"`）
- `neo4j_database`（fallback `"neo4j"`）
- `project_id`（`gc.get("project_id") or None`）——多租户标识，注入节点/边属性 + 用作 Weaviate tenant + EmbeddingCheckpoint project_id

`vector_config` 字段：`backend`、`allow_fallback_to_memory`、`weaviate_url`、`weaviate_grpc_port`、`collection_name`、`weaviate_api_key`。

#### build_from 内部节点/边建图顺序

1. `structure_facts.entities` → 节点（`add_node`）。属性：`entity_type`（`e.type.value`）、`name`、`location`、`module_id` + `e.attributes` 中**除 `code_snippet` 外**全部字段。最终 `{k:v for ... if v is not None}` 过滤 None。同时镜像写入 `_node_attrs[e.id]`。
2. `structure_facts.relations` → 边：`add_edge(r.source_id, r.target_id, rel_type=r.type.value, **r.attributes)`。
3. `domain.business_domains` → 节点 `domain://<d.id>`，`entity_type="BusinessDomain"`，`name = d.name or d.id`。
4. `domain.capabilities`（list[dict]）→ `cid = c.get("id")`，**无 id 则 skip**；节点 `capability://<cid>`，`entity_type="BusinessCapability"`，`name = c.get("name", cid)`。
5. `domain.terms`（list[dict]）→ `tid = t.get("id")`，**无 id 则 skip**；节点 `term://<tid>`，`entity_type="BusinessTerm"`，`name = t.get("name", tid)`。
6. `domain.business_domains` 的 `capability_ids` → 边 `CONTAINS_CAPABILITY`（domain→capability）。要求 domain 节点存在 + capability 节点存在。
7. `domain.service_domain_mappings`（每条 `m`）：
   - Service 节点 `service://<m.service_or_module_id>`，**若不存在则创建**（`entity_type="Service"`，`name = m.service_or_module_id`）。
   - **遍历 `for did in m.business_domain_ids`**：若 `domain://<did>` 节点已存在，建边 `BELONGS_TO_DOMAIN`（service→domain），属性 `weight = m.weight`。
8. `semantic_facts.semantic_entities`（每个 `se`）的 `se.business_links`（每个 `link`）：
   - `link.link_type == "implemented_by"` → 若 `capability://<link.business_concept_id>` 存在，建边 `IMPLEMENTED_BY`（capability→`se.structure_entity_id`），属性 `confidence=link.confidence`、`source=link.source`。**此分支不校验实体节点是否存在**。
   - 否则（related_to 等）：**term 优先**——先查 `term://<concept_id>`，若 `term 节点存在 AND 实体节点存在` → 建边 `RELATED_TO`（term→entity），属性 `confidence`；**否则** `elif` 查 `domain://<concept_id>`，若 `domain 节点存在 AND 实体节点存在` → 建边 `RELATED_TO`（domain→entity），属性 `confidence`。（domain 分支无 `source` 属性。）
9. 对 `e.type.value in ("class","interface","method")` 的实体：`find_service_id(eid)` 沿 `belongs_to` 入边回溯找 Service 节点；找到后遍历该 Service 的出边，对 `rel_type=="BELONGS_TO_DOMAIN"` 且 domain 节点存在的，建边 `IN_DOMAIN`（entity→domain），属性 `derived=True`。
   - **怪癖**：`find_service_id` 内层对每个节点的 `belongs_to` 入边匹配到第一条后即 `break`——**只跟随单条 belongs_to 链**，不展开多条 belongs_to 入边。

#### build_from 中 Weaviate embedding 逻辑（仅 vector_enabled=True）

- 向量库经 `VectorStoreFactory.create(backend, True, vector_dim, allow_fallback_to_memory, weaviate_url, weaviate_grpc_port, collection_name, weaviate_api_key)` 创建。
- `_vs_tenant = graph_config.get("project_id")`；`_ckpt_pid = _vs_tenant or "default"`。
- `EmbeddingCheckpoint.load(_ckpt_pid, force_full=force_full, weaviate_store=None)` —— 当前 `weaviate_store=None`（向量库尚无 exists/exists_many API）。
- **批一（semantic embed_text）**：`sem_pairs = [(se.structure_entity_id, se.embed_text) for se if se.embed_text and se.structure_entity_id not in method_ids_with_snippet]`（`method_ids_with_snippet` = `EntityType.METHOD` 且有 code_snippet 的实体 id 集合）。用 `ckpt.has_many` 过滤已完成；`get_embeddings_batch([text...])`（内部按 `BATCH_MAX=25` 切片）；`vector_store.add(eid, vec, tenant=_vs_tenant)`；`ckpt.mark_done(eid)`；末尾 `ckpt.flush()`。
- **批二（method code_snippet）**：`snip_triples = [(e.id, e.name or "", e.attributes["code_snippet"]) for e if e.type==EntityType.METHOD and e.attributes and e.attributes.get("code_snippet")]`。同样 checkpoint 续跑；duck-type 取 `add` 方法；`add(eid, vec, entity_type="method", name=name, code_snippet=snip, tenant=_vs_tenant)`；`mark_done` + `flush`。
- `force_full=True`：EmbeddingCheckpoint 自动删本地 checkpoint；**Weaviate tenant 数据不自动清，需运维手工处理**（代码仅 log，未实现 auto-clear）。

#### build_from 末尾 Neo4j 同步

- 记 `self._neo4j_sync_status`：`graph_backend!="neo4j"` 时为 `"skipped"`；否则调用 `_sync_graph_to_neo4j(...)` 成功置 `"ok"`，异常置 `f"failed: {e!r}"`（**异常被吞，不上抛**）。

### 查询 API

```python
node_count() -> int
edge_count() -> int

iter_nodes() -> Iterator[(node_id: str, attrs: dict)]
iter_edges() -> Iterator[(source_id: str, target_id: str, rel_type: str, attrs: dict)]
    # rel_type = ed.get("rel_type", "RELATED")（缺省 "RELATED"）；attrs 为整条边 dict（含 rel_type）

get_node(nid: str) -> Optional[dict]   # 含 "id" 字段；不存在返回 None
get_entity_code(entity_id: str) -> Optional[dict]
    # entity_id 空 或 向量库为 None → None；duck-type 调 vector_store.get_by_entity_id

successors(nid, rel_type=None) -> list[str]
successors_excluding_rel_types(nid, exclude_rel_types: tuple[str,...] | list[str]) -> list[str]   # 不去重
predecessors(nid, rel_type=None) -> list[str]
predecessors_excluding_rel_types(nid, exclude_rel_types) -> list[str]   # 不去重

impact_closure(start_id, direction="down", max_depth=50) -> set[str]
    # KnowledgeGraph 版直接用 self._g.successors/predecessors（不经 rel_type 过滤层）

subgraph_for_service(service_id: str) -> dict
    # 返回 {"nodes": [{"id", ...attrs}], "edges": [{"source", "target", ...边属性dict}]}
    # service_id 不以 "service://" 开头则加前缀
    # 有 Service 节点 → impact_closure(direction="down", max_depth=10) 并把 sid 加入集合
    # 无 Service 节点 → 按 module_id（= sid 去前缀）扫所有节点兜底
    # 都为空 → {"nodes": [], "edges": []}
    # edges 直接展开整条边属性 dict（含 rel_type），非仅 rel_type

get_direct_callees(class_name, method_name) -> list[dict]
    # _find_method_node_ids 先按 entity_type=="method"(lower) + class_name 精确 + name 精确 找节点（含重载）
    # 沿 rel_type.lower()=="calls" 出边，target 的 (class_name.strip(), name.strip()) 去重
    # 返回 [{"class_name", "method_name"}, ...]
get_direct_callers(class_name, method_name) -> list[dict]   # 镜像（入边）

search_by_name(name_substring, entity_types: Optional[list[str]] = None) -> list[dict]
    # name 子串 case-insensitive；entity_types 过滤（lower 比较）
    # 返回 [{"id": nid, **node_attrs}, ...]

similarity_search(query_text, top_k=10) -> list[dict]
    # 向量库为 None 或 size()==0 → []
    # vector_store.search_by_text → 每个命中 get_node + node["similarity_score"]=round(score,4)

save_snapshot(output_dir, version="default") -> Path
    # node_link_data(self._g, edges="links") → graph.json；meta.json={version, nodes, edges}
    # 设 self._version = version
load_snapshot(snapshot_dir) -> None
    # clear() 后 node_link_graph(data, directed=True, multigraph=True, edges="links") 覆盖 _g
    # 重建 _node_attrs；meta.json 存在则读回 _version

version: property -> Optional[str]
```

---

## 四、影响闭包 BFS 算法

三处实现（Memory、Neo4j、KnowledgeGraph）逻辑一致（Memory/Neo4j 经各自 successors/predecessors，KnowledgeGraph 直接走 nx）：

```
impact_closure(start_id, direction, max_depth):
  seen = set(); stack = [start_id]; depth = 0
  while stack AND depth < max_depth:
    depth += 1
    nid = stack.pop()          # 栈 → DFS（不保证层序）
    if nid in seen: continue
    seen.add(nid)
    next_ids = successors(nid) if direction == "down" else predecessors(nid)
    for k in next_ids:
      if k not in seen: stack.append(k)
  return seen
```

**注意**：
- `depth` 是**迭代步数**计数器（每次 pop 计一次），非 BFS 层深度。TS 必须保留 step-counter 语义。
- 遍历**全部**边类型，不按 rel_type 过滤。
- 魔法数字：`max_depth` 默认 50；`subgraph_for_service` 固定传 `max_depth=10`。

---

## 五、Neo4jGraphBackend（graph_neo4j.py）

**用途**：Neo4j 图后端，同时被 `_sync_graph_to_neo4j` 与查询路径使用。

### 构造

```python
Neo4jGraphBackend(uri, user, password, database="neo4j", project_id: Optional[str] = None)
# self._project_id = project_id or None；构造时即 _ensure_driver()
```

驱动参数（硬编码）：`connection_timeout=15.0`、`connection_acquisition_timeout=30.0`、`max_connection_lifetime=300`、`keep_alive=True`。

### 节点/边存储模型

- 节点标签统一 `Entity`（`LABEL = "Entity"`）。
- 节点 MERGE 键：`id`。
- 关系类型 `_rel_type(rel)`：`re.sub(r"[^A-Za-z0-9_]", "_", rel or "")` 后 `.upper()`；空串 fallback `"REL"`。
- 边**同时**存 Neo4j relationship type（大写）和 `r.rel_type` 属性（原始值，如 `"calls"`）。
- `project_id` 非空时自动注入所有节点/边属性（`project_id`）。

### Cypher 语句（逐字核验，准确）

**add_node**：
```cypher
MERGE (n:Entity {id: $id}) SET n += $attrs
```
attrs 过滤 None；project_id 非空则注入。

**add_edge**（注意 CREATE，非 MERGE）：
```cypher
MERGE (a:Entity {id: $sid})
MERGE (b:Entity {id: $tid})
CREATE (a)-[r:<RTYPE>]->(b)
SET r += $attrs
```
edge_attrs 始终含 `rel_type`（原始值）+ 过滤 None 后的 attrs（+ project_id）。

**add_nodes_batch**（chunk_size=500）：
```cypher
UNWIND $items AS it
MERGE (n:Entity {id: it.id})
SET n += it.attrs
```
每行 attrs 过滤 None + 注入 project_id；单 session 内按 chunk 多次 `session.run`（每 chunk 一次 auto-commit）。

**add_edges_batch**（chunk_size=500）：先按 `_rel_type(rel)` 分组（`dict.setdefault`），每组一条：
```cypher
UNWIND $items AS e
MERGE (a:Entity {id: e.sid})
MERGE (b:Entity {id: e.tid})
CREATE (a)-[r:<RTYPE>]->(b)
SET r += e.attrs
```
e.attrs 含 `rel_type`（原始值）+ 过滤 None + project_id。

**clear**：
```cypher
MATCH (n:Entity) DETACH DELETE n
```
在 `_sync_graph_to_neo4j` 中包裹重试：`for attempt in range(3)`，仅当 `attempt < 2` 且错误消息 `.lower()` 含 `'defunct'` 或 `'unavailable'` 时 `time.sleep(1)` 重试——**共 3 次尝试 / 最多 2 次重试**。

### sync 写入算法（_sync_graph_to_neo4j，模块级函数）

```
入参：g, uri, user, password, database="neo4j", progress_callback=None, project_id=None

1. GraphBackendFactory.create("neo4j", neo4j_uri=, neo4j_user=, neo4j_password=, neo4j_database=, project_id=)
2. try:
   a. progress_callback(0, 1, "正在清空 Neo4j 旧数据…")（若有）
   b. clear() with 重试（range(3)/attempt<2/sleep 1/匹配 defunct|unavailable）
   c. nodes = list(g.nodes)；edges = list(g.edges(keys=True))；total_steps = n_total + e_total
   d. has_batch = hasattr(add_nodes_batch) and hasattr(add_edges_batch)
   e. has_batch:
      - node_items = [(nid, _neo4j_sanitize(dict(g.nodes[nid]))) for nid in nodes]
      - add_nodes_batch(node_items)
      - edge_items: for (u,v,k): ed=dict(g.edges[u,v,k]); rel_type=ed.pop("rel_type","RELATED"); ed=_neo4j_sanitize(ed); append((u,v,rel_type,ed))
      - add_edges_batch(edge_items)
      - progress_callback 分别报节点/边完成
   f. else (legacy fallback)：逐条 add_node / add_edge（rel_type 同样 pop 默认 "RELATED"）
   g. progress_callback(total, total, "Neo4j 同步完成")
3. finally: backend.close()
```

`_neo4j_sanitize(value)`：`None` 原样；`set`→`list`；`dict`→递归；`list`/`tuple`→`[递归...]`（tuple 也变 list）；其余原样。目的是消除 Neo4j 驱动不支持 `set`。

### 查询 API（Neo4j）

```python
has_node(nid) -> bool
get_node(nid) -> Optional[dict]   # 注入 "id"=nid

successors(nid, rel_type=None) -> list[str]
    # rel_type 真值时：WHERE r.rel_type = $rel_type OR type(r) = '<RLABEL>'（兼容旧数据）
    # 返回过滤掉 falsy 的 b.id
successors_excluding_rel_types(nid, exclude_rel_types) -> list[str]
    # raw=strip 非空；ex_lower=raw.lower()；空则 successors(None)
    # neo_types = dict.fromkeys(_rel_type(x))（去重保序）
    # WHERE NOT (toLower(trim(toString(coalesce(r.rel_type,'')))) IN $ex_lower)
    #   AND NOT (type(r) IN $neo_types)  RETURN DISTINCT b.id   ← 去重
predecessors / predecessors_excluding_rel_types   # 镜像；后者同样 RETURN DISTINCT

node_count() -> int    # count(n)，rec["c"] or 0
edge_count() -> int    # count(r)

all_node_ids() -> list[str]
impact_closure(start_id, direction="down", max_depth=50) -> set[str]
out_edges_with_rel(nid) -> list[tuple[str, dict]]
    # RETURN b.id, r.rel_type → [(bid, {"rel_type": rel_type or ""}), ...]

query_direct_callees(class_name, method_name) -> list[dict]
    # MATCH (a:Entity) WHERE toLower(coalesce(a.entity_type,''))='method'
    #   AND a.class_name=$class_name AND a.name=$method_name
    # MATCH (a)-[r:CALLS]->(b:Entity) RETURN DISTINCT b.class_name, b.name
    # Python 端再 strip + 去重 → [{"class_name","method_name"}, ...]
query_direct_callers(class_name, method_name) -> list[dict]   # 镜像（入边）

count_nodes_by_entity_type(entity_type) -> int
    # WHERE toLower(coalesce(n.entity_type,'')) = toLower($entity_type)

count_nodes_by_entity_type_and_prefix(entity_type, prefix, *, exclude_methods_on_interface=False) -> int
    # prefix.lower()=="other"：first = toLower(substring(coalesce(n.name,n.id),0,1))；
    #   WHERE size(coalesce(n.name,n.id))>=1 AND (first<'a' OR first>'z')
    # 否则 p=prefix.lower()，len(p)!=1 或越界 a-z → 直接 return 0；
    #   WHERE toLower(substring(coalesce(n.name,n.id),0,1)) = $prefix
    # exclude_methods_on_interface=True 追加：
    #   OPTIONAL MATCH (n)-[:BELONGS_TO]->(decl:Entity)
    #   WITH n, collect(decl.entity_type) AS declTypes
    #   WHERE none(t IN declTypes WHERE toLower(coalesce(t,''))='interface')

list_nodes_by_entity_type_and_prefix(entity_type, prefix, limit=500, skip=0, *, exclude_methods_on_interface=False) -> list[dict]
    # "other" 分支用 sortKey=coalesce(n.name,n.id)，ORDER BY sortKey，SKIP/LIMIT
    # a-z 分支 ORDER BY coalesce(n.name,n.id)；非法 prefix → return []
    # iface_tail 在 "other"/"az" 两分支 WITH 子句不同（other 带 sortKey）

list_nodes_by_entity_type_and_module(entity_type, module_id, limit=500, skip=0) -> list[dict]
    # WHERE entity_type 匹配 AND n.module_id = $module_id；ORDER BY coalesce(n.name,n.id)

list_nodes_by_entity_type(entity_type, limit=500, skip=0) -> list[dict]
list_distinct_module_ids_for_entity_type(entity_type, limit=200) -> list[str]
    # WHERE entity_type 匹配 AND module_id IS NOT NULL AND <> ''；RETURN DISTINCT，ORDER BY mid
list_distinct_module_ids(limit=200) -> list[str]

get_node_relations(nid) -> dict
    # {"outgoing": [{"rel_type","target_id","target_name","target_type"}, ...],
    #  "incoming": [{"rel_type","source_id","source_name","source_type"}, ...]}
    # rel_type 用 type(r)（Neo4j 关系类型，大写）；name 缺失回退 id；空值回退 ""

search_by_name(name_substring, entity_types=None, limit=100) -> list[dict]
    # q = name_substring.strip().lower()；空 → []
    # WHERE (toLower(coalesce(n.name,'')) CONTAINS $q OR toLower(coalesce(n.id,'')) CONTAINS $q)
    # entity_types 非空时 AND toLower(coalesce(n.entity_type,'')) IN $types_lower
    # ORDER BY n.name；LIMIT $limit

subgraph_for_service(service_id) -> dict
    # 同 KnowledgeGraph 语义：有 Service → impact_closure(max_depth=10)+加 sid；
    #   无 → WHERE n.module_id = $mid 取 id 集；都空 → {"nodes":[],"edges":[]}
    # 边查询 WHERE a.id IN $ids AND b.id IN $ids；
    #   返回 [{"source","target","rel_type"}]，rel_type 用 type(r)（大写）

iter_nodes() -> Iterator[(node_id, attrs)]   # 节点 attrs 含 id；nid=str(...)
iter_edges() -> Iterator[(source_id, target_id, rel_type, attrs)]
    # RETURN type(r) AS type_r, r.rel_type AS rel_type_prop
    # rel_type 优先取 rel_type_prop；为 None 或 空白串时查 _type_to_rel[type_r]
    #   命中映射 → 映射值；未命中 → type_r 原样（type_r 非空）；type_r 也空 → "RELATED"
    # attrs = {"rel_type": rel_type}（仅 rel_type，不含其他边属性）
```

### iter_edges 的 `_type_to_rel` 映射表（Neo4j type → 图 rel_type，逐字核验准确）

```python
{
  "CALLS": "calls", "EXTENDS": "extends", "IMPLEMENTS": "implements",
  "DEPENDS_ON": "depends_on", "BELONGS_TO": "belongs_to", "SERVICE_CALLS": "service_calls",
  "CONTAINS": "contains", "RELATES_TO": "relates_to", "ANNOTATED_BY": "annotated_by",
  "SERVICE_EXPOSES": "service_exposes", "BINDS_TO_SERVICE": "binds_to_service",
  "BELONGS_TO_DOMAIN": "BELONGS_TO_DOMAIN",   # 保留大写（知识层字面量）
  "CONTAINS_CAPABILITY": "CONTAINS_CAPABILITY",
  "IMPLEMENTED_BY": "IMPLEMENTED_BY",
  "RELATED_TO": "RELATED_TO",
  "IN_DOMAIN": "IN_DOMAIN",
}
```

---

## 六、TS 实现要点（graphology 替换 NetworkX）

1. **MultiDiGraph 语义**：用 graphology `MultiDirectedGraph`，同一对节点多条边；边属性保留 `rel_type`（小写枚举值或大写知识层字面量）。
2. **节点属性**：`get_node` 注入 `id`；`add_node` 过滤 None。`add_edge` **不过滤 None**（与 add_node 不一致，须如实复刻）。
3. **rel_type 比较**：所有 rel_type 过滤均 case-insensitive（`strip().toLowerCase()`）。
4. **影响闭包**：step-counter（非层计数）；max_depth 默认 50，subgraph 固定 10。
5. **`_neo4j_sanitize`**：递归 `Set`→`Array`、`Map`/对象→递归、tuple/array→array、其余原样。
6. **`add_edge` 默认 rel_type**：sync 中从 edge dict `pop("rel_type", "RELATED")`，缺失 fallback `"RELATED"`。`KnowledgeGraph.iter_edges` 同样默认 `"RELATED"`。
7. **batch chunk_size**：节点/边均 500（硬编码）。边 batch 须先按 `_rel_type` 分组（关系类型不能参数化），保序去重用 `dict.fromkeys` 等价。
8. **clear 重试**：**共 3 次尝试（最多 2 次重试）**，每次 1000ms，仅错误消息含 `defunct` 或 `unavailable` 时重试。
9. **`code_snippet` 不入图**：节点写入须过滤 `code_snippet`，仅存 Weaviate。
10. **`get_node_relations` / `subgraph_for_service` 边 / `iter_edges`(fallback) 的 rel_type 来源差异**：`get_node_relations` 与 `subgraph_for_service` 的边 rel_type 直接用 Neo4j `type(r)`（大写，如 CALLS、BELONGS_TO）；`iter_edges` 优先 `r.rel_type` 属性、仅缺失时才回退 `_type_to_rel[type(r)]`；`successors(rel_type=...)` 则同时匹配 `r.rel_type` 属性与 `type(r)`。三者需区分。
11. **去重差异**：Neo4j 的 `successors_excluding_rel_types`/`predecessors_excluding_rel_types`/`query_direct_callees`/`query_direct_callers` 在 Cypher 层 `RETURN DISTINCT`；而 Memory 后端的排除遍历**不去重**。如需跨后端一致，TS 须显式决定（建议保持各自原行为以兼容 golden）。
12. **prefix 边界**：`count/list_nodes_by_entity_type_and_prefix` 中 prefix 非 "other" 时，`len(p)!=1 or p<'a' or p>'z'` 直接返回空（count 返 0 / list 返 []）；"other" 表示首字母非 a-z。
13. **entity_type 匹配**：Neo4j 查询统一 `toLower(coalesce(n.entity_type,'')) = toLower($entity_type)`（大小写不敏感、null 安全）。

源码路径：`/Users/java/knowledge-engineering/src/knowledge/graph.py`、`/Users/java/knowledge-engineering/src/knowledge/graph_neo4j.py`、`/Users/java/knowledge-engineering/src/knowledge/backends/memory_graph_backend.py`、`/Users/java/knowledge-engineering/src/knowledge/abstractions.py`；模型 `/Users/java/knowledge-engineering/src/models/domain.py`、`/Users/java/knowledge-engineering/src/models/semantic.py`。

---

# 【Weaviate 写侧 + 3 collection 建表 schema】

I have all four files. Let me verify each claim in the spec against the source, focusing on the high-risk areas (UUID byte-exactness, dataType, defaults, truncation magic numbers, tenant injection).

## 修正清单

经逐字回源核验,spec 整体高度准确。发现 **2 处需修正**(均为 spec 自身错误/表述不严谨),其余为抽查确认点。

1. **【模块概览表 + 源文件标注】文件路径标错**:spec 表头与「五、外部依赖」未提及,但**任务描述与 spec 都把本批次叫 P2e,而 commit 历史中 P2e 已是 agent 引擎**——这是命名层面,不影响 TS 实现,不计入硬错误。**真正的硬错误**在「七、怪癖」第 7 条与「一、tenant 注入」对 CodeEntity 失败行为的描述需统一(见下)。

2. **【失败行为分类】CodeEntity 的 `add_with_created` 不存在**:spec「六/七」多处把 `class_name 最大长度 500` 等截断常量标注「位置 = `add_with_created()`」,这是对的(属 Topo store);但 spec 未明确这些 `add_with_created` 分属**两个不同 store**(Topo vs Pattern),且二者签名/默认值/截断常量不同。已在下方表格中按 store 分列,消除歧义。

3. **【`_parse_url` 默认端口】抽查确认**:spec「六」称「HTTP 默认端口 8080 / 443(HTTPS)」位置 `_parse_url()` —— 源码 line 42 `return rest, 443 if secure else 8080, secure`,**确认无误**。但注意这是 URL **无端口段时**的 fallback,不是 `__init__` 默认;spec 表述准确。

其余所有高危点(UUID 重排 8-4-4-4-12、sha256 前 32、三 store UUID 种子逐字符、维度 64/64/1024、tenant 三态、`related_entity_ids_json` 默认 `{}` vs `[]`、`add_many` 无截断/空字段、confidence 字符串、insert→422/already exists→replace upsert、best-effort 分级)**全部逐字节核对一致**。

---

# Weaviate 写侧 + Collection Schema 规范

## 模块概览

| 模块 | Collection | Python 源文件 |
|---|---|---|
| BaseWeaviateStore | 抽象基类,三 collection 共用 | `base_weaviate_store.py` |
| WeaviateVectorStore | CodeEntity | `vector_store_weaviate.py` |
| WeaviateTopologicalInterpretStore | TopologicalInterpretation | `weaviate_interpretation_store.py` |
| WeaviatePatternInterpretStore | PatternInterpretation | `weaviate_pattern_store.py` |

> collection 默认名来自 `src.core.weaviate_defaults`:`DEFAULT_COLLECTION_CODE_ENTITY` / `DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION` / `DEFAULT_COLLECTION_PATTERN_INTERPRETATION`,以及 `DEFAULT_WEAVIATE_HTTP_URL` / `DEFAULT_WEAVIATE_GRPC_PORT`。TS 侧应复用同名常量(本批次范围内不重定义默认值字面量,以 weaviate_defaults 为准)。

---

## 一、BaseWeaviateStore — 抽象基类

### 构造参数(keyword-only)

源码 line 13-21:`__init__` 的所有参数在 `*` 之后,**全部 keyword-only**。

```
url: string           — HTTP URL（默认值由各子类提供 = DEFAULT_WEAVIATE_HTTP_URL）
grpc_port: number     — gRPC 端口（默认 = DEFAULT_WEAVIATE_GRPC_PORT，weaviate_defaults 中 = 50051）
collection_name: string
dimension: number     — 向量维度（各 store 覆写默认值）
api_key?: string | null （默认 None）
```

内部状态:`_url / _grpc_port / _collection_name / _dim / _api_key / _client(初始 None)`。构造末尾**立即调用** `_ensure_client_and_schema()`(建表副作用在构造期发生,line 28)。

### `_to_uuid(s: string): string` — UUID 生成(全局唯一规范)

源码 line 30-33,逐字节核对一致:

1. `hashlib.sha256(s.encode("utf-8")).hexdigest()` → 64-char 十六进制
2. `[:32]` 取前 32 字符(**先取前 32,再切片**;源码是 `.hexdigest()[:32]`)
3. 重排:`f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"`,即 8-4-4-4-12

```
"foo" → sha256 = 2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae
        [:32]  = 2c26b46b68ffc68ff99b453c1d304134
        UUID   = 2c26b46b-68ff-c68f-f99b-453c1d304134
```

> TS 实现:`crypto.createHash("sha256").update(s, "utf-8").digest("hex").slice(0,32)` 后按 `[0:8]-[8:12]-[12:16]-[16:20]-[20:32]` 拼接。**注意分段是对 32-char 串切片**,不是对原始 64-char 串。

### 三个 store 的 UUID 种子

| Store | add() 种子 | add_many() 种子 | delete() 种子 |
|---|---|---|---|
| CodeEntity | `entity_id` | `eid + str(i)`(i 为 0-based 枚举索引) | `entity_id` |
| TopologicalInterpretation | `method_entity_id + "\|interpret"` | (无 add_many) | (无独立 delete,靠 `clear()`) |
| PatternInterpretation | `target_id + "\|" + scope_type + "\|" + pattern_type + "\|" + pattern_name + "\|pattern"`(均为 **normalize 后**的值) | (无 add_many) | (无独立 delete,靠 `clear()`) |

> Topo/Pattern 的种子用 f-string 拼接:Topo = `f"{method_entity_id}|interpret"`(line 132);Pattern = `f"{target_id}|{scope_type}|{pattern_type}|{pattern_name}|pattern"`(line 112)。

### `_parse_url(url): [host, port, secure]`(静态)

源码 line 35-42:
- `secure = url.startswith("https://")`
- 去掉 scheme 前缀 + 首尾 `/`
- 若含 `:` → `rsplit(":", 1)` 拆 host/port
- 否则 fallback:`secure ? 443 : 8080`

### `_ensure_client_and_schema()` — 连接 + 建表

源码 line 49-149。

**重连清理**(line 54-59):若 `_client` 非 None,先 `close()`(吞异常)再置 None。

**代理清理**(每次必做,line 62-64):
- 从环境删除 `HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / http_proxy / https_proxy / all_proxy`(`os.environ.pop(k, None)`)
- `NO_PROXY = 原值 + ",localhost,127.0.0.1"`(**追加**,line 64:`os.environ.get("NO_PROXY","") + ",localhost,127.0.0.1"`)

**连接**(line 66-85):`connect_to_custom(http_host/http_port/http_secure/grpc_host/grpc_port/grpc_secure)`,其中 host 对 HTTP 与 gRPC **同一个**(都来自 `_parse_url`),`grpc_port` 用构造传入的 `_grpc_port`。`secure` 同时作用于 http_secure 与 grpc_secure。
- 有 api_key 时:`auth_credentials = Auth.api_key(...)`(吞异常,line 75-81)
- **`skip_init_checks=True`**(line 84,v2.0 staging 稳定性,必须保留)

**建表条件**(line 87):`not collections.exists(collection_name)` 才建。

**Collection 创建三要素**:
1. **向量配置**(line 90-94):`Configure.VectorIndex.hnsw(distance_metric=VectorDistances.COSINE)`,失败 fallback 到字符串 `"cosine"`。外层包裹 `Configure.Vectors.self_provided(vector_index_config=vec_index)`(v4.10+ 路径,line 103-109)。
   - **TS v3 SDK**:`vectorizers.selfProvided()` + `vectorIndexConfig` 用 hnsw,距离用字符串 `"cosine"`。
2. **Multi-Tenancy**(line 96-100):`Configure.multi_tenancy(enabled=True, auto_tenant_creation=True, auto_tenant_activation=True)`。**三个 collection 全部启用**(在基类统一注入,子类无法关闭)。
3. **Properties**:子类 `_schema_properties()` 返回的 `Property[]`。

> **建表失败回滚**(line 144-149):任何异常 → `client.close()`(吞异常)→ `raise`。即建表失败**会抛到上层**(与写操作的 best-effort 不同)。`_client` 仅在成功后赋值(line 143)。

> **dataType 关键提醒(P2f 踩坑)**:Python SDK 用枚举 `DataType.TEXT`;TS v3 SDK 的 `dataType` 是**字符串** `"text"`(非数组)。本批次所有属性均为 `DataType.TEXT` → TS 全部 `"text"`。

### `_get_collection()`

源码 line 151-152:`self._client.collections.get(collection_name)`。返回**未绑定 tenant** 的 collection 引用;tenant 绑定由各子类在写/删时显式 `.with_tenant(tenant)` 完成。

### `clear()` / `close()` / `__del__`

- `clear()`(line 154-160):collection 存在则 `collections.delete(name)`,再 `_ensure_client_and_schema()` 重建空表。整体吞异常。这是 Topo/Pattern「删除」的唯一途径(无 per-object delete)。
- `close()`(line 162-168):`_client.close()` 吞异常后置 None。
- `__del__`(line 170-174):调 `close()`,吞异常。

### tenant 注入规范(三 store 不一致,关键)

- **WeaviateVectorStore (CodeEntity)**:`if tenant: coll = coll.with_tenant(tenant)` —— **tenant 非空才绑定**;为空走默认分区(向后兼容)。
- **WeaviateTopologicalInterpretStore**:统一经 `_resolve_collection(tenant)`(line 39-53)。tenant 非空 → `with_tenant`;**tenant 为 None → 打 `_log.warning`(deprecation)后返回未绑定 collection 并继续**(向后兼容,新代码必传)。
- **WeaviatePatternInterpretStore**:**完全不注入 tenant**。`add_with_created` 直接 `self._get_collection()`(line 111),无 `with_tenant`。pattern 是 system/module 级全局数据。

---

## 二、CodeEntity Collection (WeaviateVectorStore)

### 默认参数

```
collection_name: DEFAULT_COLLECTION_CODE_ENTITY ("CodeEntity")
dimension: 64
```

> 注意:`WeaviateVectorStore.__init__` 的参数**不是** keyword-only(无 `*`,line 20-27,位置参数),与基类不同。TS 侧若用对象参数则无影响。

### Schema Properties(顺序固定,line 43-48)

| 属性名 | dataType(TS) |
|---|---|
| `entity_id` | `"text"` |
| `name` | `"text"` |
| `entity_type` | `"text"` |
| `code_snippet` | `"text"` |

### `add(entity_id, vector, entity_type?, name?, code_snippet?, *, tenant?): void`

源码 line 53-83。`entity_type/name/code_snippet` 为位置可选(默认 None),`tenant` keyword-only。

**前置守卫**(line 64):`not vector or len(vector) < self._dim` → 直接 `return`(不写)。

**字段映射**(line 71-76):
```
entity_id    = entity_id
name         = (name or _name_from_id(entity_id))[:100]
entity_type  = entity_type or ""
code_snippet = code_snippet or ""
vector       = vector[:dimension]            // 截断到 dim
uuid         = _to_uuid(entity_id)
```

`_name_from_id(entity_id)`(line 50-51):`(entity_id.split("/")[-1] if "/" in entity_id else entity_id)[:100]` —— 含 `/` 取最后段,否则用整串,**均截 100**。

写入:`coll.data.insert(properties, vector, uuid)`。**失败 try/catch 静默忽略**(best-effort,line 82-83)。

### `add_many(items: Array<[eid, vec]>, *, tenant?): void`

源码 line 85-106。使用 `coll.batch.dynamic()` 上下文管理器(动态批量)。

**逐条字段映射**(line 95-104):
```
entity_id    = eid
name         = _name_from_id(eid)            // ⚠ 仍走 _name_from_id（内部已 [:100]），但 add_many 自身无额外 .slice(0,100)
entity_type  = ""
code_snippet = ""
vector       = vec[:dimension]
uuid         = _to_uuid(eid + str(i))        // i = 0-based 枚举索引
```

> **修正/澄清**:spec 原文「`name` 不截长度(与 add 不同,无 .slice(0,100))」**表述会误导**。实际 `_name_from_id` 内部已带 `[:100]`,所以 add_many 的 name **同样被截到 100**。区别仅在于:`add()` 是 `(name or _name_from_id(...))[:100]`(外层再截一次,对显式传入的 name 生效),而 `add_many()` 直接用 `_name_from_id(eid)`(没有外层显式 name 入参)。**TS 实现:add_many 的 name 也必须截 100**(经 `_name_from_id`)。

**逐条守卫**(line 93):`not vec or len(vec) < self._dim` → `continue`(跳过当前条,不终止批次)。

**失败**:整个批次外层 try/catch 静默忽略(line 105-106)。

### `delete(entity_id, *, tenant?): void`

源码 line 297-314:
```
coll = _get_collection()
if tenant: coll = coll.with_tenant(tenant)
coll.data.delete_by_id(_to_uuid(entity_id))   // 种子 = entity_id，与 add() 一致
```
best-effort 静默忽略。

---

## 三、TopologicalInterpretation Collection

### 默认参数

```
collection_name: DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION ("TopologicalInterpretation")
dimension: 64
```

### Schema Properties(顺序固定,line 58-68)

| 属性名 | dataType |
|---|---|
| `method_entity_id` | `"text"` |
| `class_entity_id` | `"text"` |
| `class_name` | `"text"` |
| `method_name` | `"text"` |
| `signature` | `"text"` |
| `interpretation_text` | `"text"` |
| `context_summary` | `"text"` |
| `language` | `"text"` |
| `related_entity_ids_json` | `"text"` |

### `add(vector, method_entity_id, interpretation_text, *, opts?): boolean`

源码 line 70-103。薄包装 → `add_with_created(...)`,返回元组第一个值 `ok`。

### `add_with_created(...): [boolean, boolean]`

源码 line 105-167。返回 `[success, created]`:created 仅首次 insert 为 true,replace 时 false。

**参数签名**:`vector / method_entity_id / interpretation_text` 为位置参数(前三个,**非 keyword-only**,line 106-109),其余在 `*` 后 keyword-only:
```
vector: number[]                  (位置)
method_entity_id: string          (位置)
interpretation_text: string       (位置)
tenant?: string | null = None     — null 走 legacy(打 warning 继续)
class_entity_id: string = ""
class_name: string = ""
method_name: string = ""
signature: string = ""
context_summary: string = ""
language: string = "zh"
related_entity_ids_json: string = "{}"   ← 默认空对象
```

> **澄清**:spec 写「所有 opts 均为 keyword-only」对 `class_entity_id` 等附加字段成立,但 `vector/method_entity_id/interpretation_text` 这三个核心参数是**位置参数**。TS 侧无所谓,记录以防签名误判。

**前置守卫**(line 128):`not vector or len(vector) < self._dim` → `[False, False]`。

**UUID 种子**:`method_entity_id + "|interpret"`。

**字段映射 + 截断**(line 133-143):
```
method_entity_id        = method_entity_id            (不截断)
class_entity_id         = class_entity_id or ""        (不截断)
class_name              = (class_name or "")[:500]
method_name             = (method_name or "")[:300]
signature               = (signature or "")[:2000]
interpretation_text     = (interpretation_text or "")[:48000]
context_summary         = (context_summary or "")[:12000]
language                = language or "zh"
related_entity_ids_json = related_entity_ids_json[:8000]   ← ⚠ 无 `or ""` 兜底
vector                  = vector[:dimension]
```

> **怪癖**:`related_entity_ids_json[:8000]` **没有 `or ""` 兜底**(line 142),若传入 None 会抛(但默认 `"{}"`)。TS 侧直接 `.slice(0,8000)`,调用方须保证非 null。

**Upsert 逻辑**(line 145-167):
1. `coll.data.insert(props, vector=vec, uuid=uid)` 成功 → `[true, true]`
2. 异常且 `"already exists" in str(e).lower()` **或** `"422" in str(e)` → `coll.data.replace(uuid=uid, properties=props, vector=vec)` 成功 → `[true, false]`
3. replace 再抛 → `_log.warning(...)` → `[false, false]`
4. 非 already-exists/422 异常 → `_log.warning(...)` → `[false, false]`

> warning 日志中 method 用 `method_entity_id[:50]`(line 156/163),并带 tenant。

---

## 四、PatternInterpretation Collection

### 默认参数

```
collection_name: DEFAULT_COLLECTION_PATTERN_INTERPRETATION ("PatternInterpretation")
dimension: 1024          ← 与前两个 store(64) 不同
```

> `__init__` 全部 keyword-only(有 `*`,line 27-35),与 CodeEntity/Topo 的位置参数不同。

### Schema Properties(顺序固定,line 48-58)

| 属性名 | dataType | 备注 |
|---|---|---|
| `scope_type` | `"text"` | `"system"` 或 `"module"` |
| `target_id` | `"text"` | system 级固定 `"system"`,module 级为 module_id |
| `pattern_type` | `"text"` | `"design"` 或 `"architecture"` |
| `pattern_name` | `"text"` | |
| `confidence` | `"text"` | float 转字符串存(注释:避免 DataType NUMBER 差异) |
| `summary_text` | `"text"` | |
| `evidence_json` | `"text"` | |
| `language` | `"text"` | |
| `related_entity_ids_json` | `"text"` | |

### `add(vector, *, opts): boolean`

源码 line 60-86。薄包装 → `add_with_created`,返回 `ok`。

### `add_with_created(vector, *, opts): [boolean, boolean]`

源码 line 88-140。

**参数签名**(`vector` 位置,其余 keyword-only,line 88-101):
```
vector: number[]                       (位置)
scope_type: string                     (必传，无默认；空值 normalize 后兜底 "system")
target_id: string                      (必传；空值兜底 "system")
pattern_type: string                   (必传；空值兜底 "design")
pattern_name: string                   (必传；空值兜底 "Unknown")
confidence: float                      (必传)
summary_text: string                   (必传)
evidence_json: string = ""
language: string = "zh"
related_entity_ids_json: string = "[]"  ← 默认空数组
```

> **修正**:spec「参数签名」把 `scope_type/target_id/pattern_type/pattern_name/confidence/summary_text` 标了「自动 .trim()...默认 ...」。实际这些参数**没有 Python 默认值**(是必传 keyword),只是函数体内做了 normalize 兜底。`evidence_json/language/related_entity_ids_json` 才是有默认值的。normalize 兜底逻辑本身 spec 描述正确,但「默认」二字用词不准——是**入参必传 + 体内空值兜底**。

**前置守卫**(line 103):`not vector or len(vector) < self._dim` → `[False, False]`。

**Normalize**(line 106-109,**在拼 UUID 与 props 之前**):
```
scope_type   = (scope_type or "").strip().lower() or "system"
pattern_type = (pattern_type or "").strip().lower() or "design"
target_id    = (target_id or "").strip() or "system"            ← 只 trim，不 lower
pattern_name = (pattern_name or "").strip() or "Unknown"         ← 只 trim，不 lower
```

> **怪癖**:`scope_type`/`pattern_type` 走 `.strip().lower()`;`target_id`/`pattern_name` **只 `.strip()` 不 `.lower()`**。spec 描述与此一致(target_id 标「.trim()」、pattern_name 标「.trim()」),确认无误。

**UUID 种子**:`f"{target_id}|{scope_type}|{pattern_type}|{pattern_name}|pattern"`(**normalize 后的值**,line 112)。

**字段映射 + 截断**(line 115-125):
```
scope_type              = scope_type                       (已 normalize，不再截)
target_id               = target_id                        (已 normalize，不再截)
pattern_type            = pattern_type                     (已 normalize，不再截)
pattern_name            = pattern_name[:200]
confidence              = str(float(confidence))[:20]      ← float→str→截 20
summary_text            = (summary_text or "")[:16000]
evidence_json           = (evidence_json or "")[:30000]
language                = language or "zh"
related_entity_ids_json = related_entity_ids_json[:8000]   ← 无 `or ""` 兜底
vector                  = vector[:dimension]
```

> `str(float(confidence))`:先 `float()` 再 `str()`,TS 等价 `String(Number(confidence)).slice(0,20)`。**注意 Python `str(float(1.0))` = `"1.0"`,JS `String(Number(1.0))` = `"1"`** —— 这是潜在不一致点,若需逐字节兼容旧数据,TS 需模拟 Python float repr(但 confidence 仅作展示/parseFloat 读回,差异通常无害,实现时留意)。

**Upsert 逻辑**:与 TopologicalInterpretation 完全相同(insert → `already exists`/`422` → replace → warning;line 128-140)。warning 用 `_LOG`。

**无 tenant 注入**:`_get_collection()` 直接用,无 `with_tenant`。

### `add_many_encoded_evidence(...)`(辅助,line 224-261)

不在核心写侧 add/delete 范围,但属本文件:将 `evidence` 与 `related_entity_ids` 用 `json.dumps(..., ensure_ascii=False)` 编码(失败 fallback `str(evidence)` / `"[]"`)后调 `add(...)`。TS 移植时若有对应调用方需保留 JSON 序列化语义(`ensure_ascii=False` → 不转义非 ASCII)。

---

## 五、外部依赖

- Python `weaviate-client`(v4.4-4.9 与 v4.10+ 双路径,见 `_ensure_client_and_schema` 的 `hasattr(Configure,"Vectors")` 分支)。TS 对应 v3 SDK。
- `weaviate.classes.config`:`Configure / Property / DataType / VectorDistances`
- `weaviate.auth.Auth`(api_key 路径)
- `weaviate.classes.query`:`Filter / MetadataQuery`(读侧,P2d 已覆盖)
- `src.core.weaviate_defaults`:默认 URL/grpc_port/collection 名
- `src.semantic.embedding.get_embedding`(读侧 search_by_text)
- `src.knowledge.method_entity_id_normalize.method_entity_id_variants`(读侧)
- **TS v3 等价(写侧 + 建表关键)**:`vectorizers.selfProvided({ vectorIndexConfig: configure.vectorIndex.hnsw({ distanceMetric: "cosine" }) })`、`multiTenancy({ enabled: true, autoTenantCreation: true, autoTenantActivation: true })`、property `dataType: "text"`(字符串)。

---

## 六、魔法数字汇总

| 常量 | 值 | 位置 |
|---|---|---|
| UUID sha256 前缀截取 | 32 chars | `_to_uuid` |
| UUID 重排分段 | 8-4-4-4-12(对 32-char 串切片) | `_to_uuid` |
| CodeEntity 默认维度 | 64 | `WeaviateVectorStore.__init__` |
| TopologicalInterpretation 默认维度 | 64 | `WeaviateTopologicalInterpretStore.__init__` |
| PatternInterpretation 默认维度 | **1024** | `WeaviatePatternInterpretStore.__init__` |
| name 最大长度(CodeEntity,`add` 外层 + `_name_from_id` 内层) | 100 | `add()` / `_name_from_id()` |
| class_name 最大长度 | 500 | Topo `add_with_created()` |
| method_name 最大长度 | 300 | Topo `add_with_created()` |
| signature 最大长度 | 2000 | Topo `add_with_created()` |
| interpretation_text 最大长度 | 48000 | Topo `add_with_created()` |
| context_summary 最大长度 | 12000 | Topo `add_with_created()` |
| related_entity_ids_json 最大长度(Topo) | 8000 | Topo `add_with_created()` |
| pattern_name 最大长度 | 200 | Pattern `add_with_created()` |
| confidence 字符串最大长度 | 20 | Pattern `add_with_created()` |
| summary_text 最大长度 | 16000 | Pattern `add_with_created()` |
| evidence_json 最大长度 | 30000 | Pattern `add_with_created()` |
| related_entity_ids_json 最大长度(Pattern) | 8000 | Pattern `add_with_created()` |
| gRPC 默认端口 | 50051 | `weaviate_defaults.py`(DEFAULT_WEAVIATE_GRPC_PORT) |
| HTTP/HTTPS fallback 端口(URL 无端口段时) | 8080 / 443 | `_parse_url()` |

---

## 七、怪癖与注意事项

1. **UUID 种子区分**:`add()` 种子 `entity_id`,`add_many()` 种子 `eid+str(i)` —— 同一 entity 经 add_many 与经 add 写入会得到**不同 UUID**(add_many 不与 add 互为 upsert,且 add_many 内同 eid 不同 i 也会产生多条)。`add()` 与 `delete()` 种子相同,可对应。

2. **confidence 是字符串非数值**:Pattern schema `confidence` 为 `"text"`,写入 `str(float(confidence))[:20]`,读取需 `parseFloat`。⚠ Python `str(float(...))` 与 JS `String(Number(...))` 对整数值的字符串形态不同(`"1.0"` vs `"1"`),逐字节兼容旧数据时需注意。

3. **related_entity_ids_json 默认值不对称**:Topo 默认 `"{}"`(对象),Pattern 默认 `"[]"`(数组),不可互换。两者写入时**都无 `or ""` 兜底**,直接 `[:8000]`,调用方须保证非 null。

4. **PatternInterpretation 无 tenant**:仅 CodeEntity、TopologicalInterpretation 注入 tenant(且方式不同——见 §一 tenant 注入);Pattern 全局共享,TS 不加 `withTenant`。

5. **TopologicalInterpretation legacy 路径**:tenant 为 None → `_resolve_collection` 打 `_log.warning`(deprecation)后用未绑定 collection 继续,不报错。新代码必传 tenant。

6. **add_many 不携带 entity_type/code_snippet 实值**:批量写入这两字段恒为 `""`,name 经 `_name_from_id`(截 100)。与 `add()` 不同(add 接受这些字段入参)。

7. **失败行为分级**:
   - **建表**(`_ensure_client_and_schema`):异常 **会抛到上层**(close 后 raise),非 best-effort。
   - **CodeEntity** add/add_many/delete:try/catch **静默忽略**(无日志)。
   - **Topo / Pattern** add_with_created:insert/replace 异常打 **`warning` 日志**后返回 `[false,false]`,不抛上层。

8. **截断的 `or ""` 兜底分布**:Topo/Pattern 大多数文本字段写 `(x or "")[:N]`(对 None 安全);但 `related_entity_ids_json`(两 store)、Topo 的 `class_entity_id`(只 `or ""` 不截)、Pattern 已 normalize 的 scope_type/target_id/pattern_type 处理方式各异 —— 见 §三/§四 字段映射逐行注明,实现时勿统一化。

9. **`distance_metric` 字符串 fallback**:Python 先试 `VectorDistances.COSINE` 枚举,失败回退字符串 `"cosine"`。TS v3 直接用字符串 `"cosine"`。

10. **建表三路径**:Python 因 SDK 版本兼容写了 v4.10+(`Configure.Vectors`)、v4.4-4.9(`vectorizer_config=Vectorizer.none()` + `vector_index_config`)、以及 `TypeError` 兜底(尝试给 hnsw 传 `vector_size`/`dimensions`)三条路径。**TS v3 只需实现 selfProvided + hnsw(cosine) + multiTenancy 一条等价路径**,无需复刻版本兜底。

---

**核验结论**:spec 在所有高危点(UUID 字节序、sha256 前 32、三 store 种子、维度、dataType 字符串、tenant 三态、upsert 422/already-exists、related_json 默认 `{}`/`[]`、confidence 字符串)上**逐字节正确**。修正项为:(2) `add_many` 的 name 实际**仍被 `_name_from_id` 截到 100**(spec 原文「不截长度」会误导);(3) Pattern `add_with_created` 的 6 个核心参数是**必传 keyword + 体内空值兜底**,非「带默认值」;并补充了建表失败抛异常、normalize 顺序、`or ""` 兜底分布、`str(float)` 跨语言差异等实现级注意点。源码路径:`/Users/java/knowledge-engineering/src/knowledge/{base_weaviate_store,vector_store_weaviate,weaviate_interpretation_store,weaviate_pattern_store}.py`。

---

# 【method-表访问 + Mapper SQL 解析 + DDL 解析】

I have all the source. Now let me verify each claim in the spec carefully against the source.

Let me check the key discrepancies I've already spotted:

1. **`_TABLE_PATTERN` is NOT global+IgnoreCase** — spec says `/gi`. Python uses `finditer` (so global-like) with `re.IGNORECASE`. Correct enough, but note no `\b` issue.

2. **`get_methods_for_table` op_filter "write"** — spec says `"write"` → 过滤 op == "select"（即排除 select）. Source: `if op_filter == "write" and op in ("select",): continue`. Correct.

3. **`reconstruct_path` failure** — returns `[start, end]`. Correct.

4. **`bfs_min_hops` secondary** — spec for service says `mb = merge_backend !== backend ? merge_backend : null`, then passes `mb`. But inside `bfs_min_hops`, there's ALSO a `secondary = merge_backend if merge_backend is not backend else None`. Double guard. Let me note this.

Let me verify the spec's claim about `_method_simple_name_from_graph_node` and `_canonical_method_id`/`resolve_bfs_start_id` more carefully, plus `_truncate_sql` truncation length.

The `_truncate_sql`: spec says "超过 1200 字符则截断并追加 `…`". Source: `s[: max_len - 1] + "…"` = `s[:1199] + "…"` → result length 1200. The spec section 三 says "最多 1200 字符" and "超过 1200 字符则截断并追加 `…`（省略号 U+2026）". This is slightly imprecise: it keeps 1199 chars + ellipsis. Let me flag.

Now the output.

## 修正清单

1. **`_truncate_sql` 截断细节不精确**（spec 三）：源码 `s[: max_len - 1] + "…"`，即保留前 **1199** 字符再拼省略号，结果总长正好 1200。spec 写"超过 1200 字符则截断并追加 `…`"会被误读为"前 1200 + 省略号 = 1201"。已更正为「保留前 (max_len-1)=1199 字符 + U+2026，总长 = max_len = 1200」。先做空白规范化（`" ".join(sql.split())`），`len(s) <= max_len` 时原样返回。

2. **`bfs_min_hops` 内部存在第二重 self-merge 防护**（spec 五、六）：spec 只在 service 层提到 `mb = merge_backend if merge_backend is not backend else None`，但 `bfs_min_hops` 内部**也**有 `secondary = merge_backend if merge_backend is not backend else None`。service 已先把 `mb` 算好再传入，故是双重保险。TS 移植两处都要保留 `===` 判等。

3. **`get_methods_for_table` 中 op_filter 过滤发生在「构建 candidates 阶段」**（spec 六）：spec 把 op_filter 列在流程注释里但位置含糊。实际：过滤在遍历 `table_to_methods` 构建 candidates 的循环内，**且在 `_is_method_node` 校验之后**。顺序为：取 graph_mid → 非空校验 → `_is_method_node` 校验 → op_filter 校验 → 入 candidates。

4. **`_is_method_node` 私有函数位于 `method_table_access_service`，非 graph_walker**（spec 五误归类）：spec 第五节把 `_is_method_node` 列在 "图遍历（method_table_graph_walker）" 标题下并注"（service 内私有函数）"。它确实定义在 `method_table_access_service.py`（L39）。已挪正归属。`graph_walker` 内的对应物是 `_backend_has_node`（不同语义：存在性，不判类型）。

5. **`resolve_bfs_start_id` 兜底返回的是 c1（primary 侧 canonical）**（spec 五基本对，补精确）：源码先 `c1 = _canonical_method_id(primary, method_id)`；若 primary 有 c1 返回 c1；否则若 secondary 存在且 `secondary is not primary`，算 `c2` 并在 secondary 有节点时返回 c2；最终兜底 `return c1`。spec "兜底返回 primary 侧的 canonical id" 正确。

6. **`templates_for_bfs_method` 路径3 的图节点获取用 variants 多形态**（spec 四补充）：源码经 `_get_node_with_variants`，即对 `method_entity_id_variants(mid)` 逐一 `get_node` 直到命中；不是单次 `get_node(mid)`。TS 移植须复用 `method_entity_id_variants` 枚举。

其余抽查点（DDL 正则、Mapper SQL 正则、列名提取分支、列数双截断 50→30、去重 by id()、500 回溯上限、glob、CDATA 处理、search_by_name limit 回退、location 正则双尝试）经逐字回源**全部与源码一致**，下方规范已并入上述修正。

---

# 方法-表访问 模块规范（TS 移植权威）

覆盖文件：`method_table_types.py` / `ddl_parser.py` / `mapper_sql_parser.py` / `mapper_access_index.py` / `method_table_graph_walker.py` / `method_table_access_service.py`

---

## 一、数据结构（method_table_types）

四个 `@dataclass`，无方法。

### `TableAccessDetail`
```
table:            string        // 表名（原始大小写）
op:               string        // "select" | "insert" | "update" | "delete"
columns:          string[]      // 解析到的列名，可为 []
source_method_id: string        // BFS 到达该条记录时的节点 id（可为 ""）
hop:              number        // BFS 步数
path_method_ids:  string[]      // 从 BFS 起点到 source_method_id 的完整路径
mapper_statement: string        // "ShortClassName.methodId"，如 "OrderMapper.selectById"
sql_snippet:      string = ""   // 截断后的原始 SQL，默认值 ""
```

### `TableAccessGrouped`
```
table:    string
op:       string
min_hop:  number
max_hop:  number
items:    TableAccessDetail[]    // 按 (hop, mapper_statement, source_method_id) 升序
```

### `MethodAccessResult`
```
read_groups:   TableAccessGrouped[] = []   // op == "select"，default_factory=list
write_groups:  TableAccessGrouped[] = []   // op != "select"
```

### `MethodForTable`
```
method_id:        string
op:               string
hop:              number
source_method_id: string
```

---

## 二、DDL 解析（ddl_parser）

### `parse_ddl_sql(content: string): TableInfo[]`
1. 建表正则 `re.compile(r"CREATE\s+TABLE\s+`?(\w+)`?\s*\(", re.IGNORECASE)`；`finditer` 全量扫描，顺序与文件出现顺序一致。
2. `table_name = group(1).strip()`，为空跳过。
3. 从 `match.end()`（即 `(` 之后）起，`depth=1` 括号深度计数；逐字符向右：`(` → depth++，`)` → depth--；循环条件 `pos < len(content) and depth > 0`，每步 `pos += 1`。
4. 退出后若 `depth == 0`：`body = content[start : pos - 1]`（`start = match.end()`，`pos-1` 即闭合 `)` 位置），传入 `_parse_columns`。**若到文件尾仍 depth>0（无闭合），该表整条丢弃**（不 append）。

### `load_ddl_from_file(path): TableInfo[]`
- `path.exists()` 为假返回 `[]`。
- `path.read_text(encoding="utf-8", errors="replace")`；任何异常 try/except 返回 `[]`。
- 委托 `parse_ddl_sql`。

### 数据结构
```
ColumnInfo { name: string; type_info: string }     // type_info = 原始类型串，如 "bigint(20)"
TableInfo  { name: string; columns: ColumnInfo[]; comment: string = "" }
```

### `_parse_columns(body): ColumnInfo[]`
- 深度 0 处逗号拆段：遍历字符，`(` depth++，`)` depth--，`,` 且 depth==0 时切段（`body[start:i].strip()`），`start=i+1`。
- 循环后若 `start < len(body)` 处理尾段。
- 每段调用 `_parse_column_line`，非 null 则收。

### `_parse_column_line(line): ColumnInfo | null`
- `line.strip()`；空 → null。
- `line.upper().startswith(("PRIMARY","KEY","UNIQUE","INDEX","CONSTRAINT","FOREIGN"))` → null（**前缀匹配，整段大写后比较**）。
- 列正则：`re.match(r"^`?(\w+)`?\s+(\w+(?:\([^)]*\))?)", line, re.IGNORECASE)`
  - group(1) = 列名；group(2) = 类型（可含一层括号参数，如 `varchar(100)`）。
- 不匹配 → null。

**怪癖**：`TableInfo.comment` 恒为 `""`（DDL 表注释未提取）；不解析主键/索引/外键/唯一约束行。

---

## 三、Mapper SQL 解析（mapper_sql_parser）

### `parse_mapper_xml(path): MapperMethodAccess[]`
1. `xml.etree.ElementTree.parse(path)`；任何异常返回 `[]`。
2. `namespace = (root.get("namespace") or "").strip()`；空则返回 `[]`。
3. 按顺序遍历四 tag：`("select","insert","update","delete")`，对每个用 `root.findall(f".//{tag}")`（递归后代查找）。
4. `mid = (elem.get("id") or "").strip()`；空跳过。
5. `sql = _get_sql_text(elem)`。
6. `tables = _extract_tables_from_sql(sql)`；**为空则整 elem 跳过**（不进 results）。
7. `op = tag.lower()`；`columns = _extract_columns_from_sql(sql, op)`；`sql_snip = _truncate_sql(sql)`。
8. **每个 table 生成一条 `TableAccess`，共用同一 `columns` 与 `sql_snippet`**，合并为一个 `MapperMethodAccess`。

### `load_mapper_accesses(repo_root, mapper_glob="**/mapper/*Mapper.xml"): MapperMethodAccess[]`
- `Path(repo_root).glob(mapper_glob)`；逐文件 `p.is_file() and p.suffix.lower() == ".xml"` 才解析；`extend` 聚合。

### 数据结构
```
TableAccess        { table; op; columns: string[]; sql_snippet: string = "" }
MapperMethodAccess { namespace; method_id; accesses: TableAccess[] }
```

### 正则与魔法数字（全集）
- **表名** `_TABLE_PATTERN = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+`?(\w+)`?", re.IGNORECASE)`，`finditer` 全量。
  - 提取后 `t.strip()`；丢弃条件：`t` 为空 或 `t.upper() in ("SELECT","WHERE","AND","OR","ON")`。
  - **结果用 `set` 去重后 `list(found)`**，故表名顺序不确定（TS 移植用 Set 收集，顺序与 Python set 不必逐一对齐，但需注意下游分组/去重不依赖顺序）。
  - 空/纯空白 SQL 直接返回 `[]`。
- **列名 `_extract_columns_from_sql(sql, op)`**：先 `sql_norm = " ".join(sql.split())`（空白规范化），再按 `op.lower()` 分支：

| op | 正则（均 `IGNORECASE|DOTALL`） | 行为 |
|----|------|------|
| select | `_COL_SELECT = re.compile(r"SELECT\s+(.+?)\s+FROM", I\|S)` | 取 group(1)；若 `"count(" in part.lower()` 或 `"*" in part` → 返回 `[]`；否则 `re.finditer(r"`?(\w+)`?", part)`，逐词，**排除 `c.upper() in ("AS","DISTINCT","FROM")`** |
| update | `_COL_SET = re.compile(r"SET\s+(.+?)(?:WHERE|$)", I\|S)` | 取 group(1)，`re.finditer(r"`?(\w+)`?\s*=", part)` 取 `=` 前的词（无额外过滤） |
| insert | `_COL_INSERT_COLS = re.compile(r"INSERT\s+INTO\s+\w+\s*\((.+?)\)", I\|S)` | 取括号内，`re.finditer(r"`?(\w+)`?", part)`，**排除 `c.upper() in ("VALUES",)`** |

  - 末尾统一 `return cols[:50]`（列数上限 **50**）。
- **`_truncate_sql(sql, max_len=1200)`**：`s = " ".join((sql or "").split())`；`len(s) <= max_len` 原样返回；否则 `return s[: max_len - 1] + "…"`（保留前 **1199** 字符 + U+2026，结果总长正好 1200）。
- **`_get_sql_text(elem)`**：
  - 若 `elem.text` 真值 → 返回 `elem.text.strip()`（CDATA/直接文本落此）。
  - 否则遍历子元素 `c`：依次 append `c.text`（若有）与 `c.tail`（若有），最后 `" ".join(parts).strip()`。
  - **怪癖**：不递归 `<include refid>` 展开；动态标签 `<if>` 仅取其 text+tail。TS 照抄此局限。

---

## 四、Mapper 访问索引（mapper_access_index）

### `MapperAccessIndex(repo_root, ddl_path, mapper_glob)`
内部状态（`defaultdict(list)` 标注）：
```
_tables:                TableInfo[]
_tables_by_name:        Map<string, TableInfo>
_mapper_accesses:       MapperMethodAccess[]
_ns_id_to_method:       Map<[namespace, method_id], graphNodeId:string>
_method_direct:         Map<normalizedMid, TableAccessDetail[]>       (defaultdict)
_method_direct_by_pair: Map<[shortClassLower, methodIdLower], TableAccessDetail[]>  (defaultdict)
_table_to_methods:      Map<tableName, [namespace, method_id, op][]>  (defaultdict)
_loaded:                boolean
```

#### `load(): void`（幂等：`_loaded` 真则 return）
1. `load_ddl_from_file(repo_root / ddl_path)` → `_tables`；`_tables_by_name = {t.name: t}`（同名后者覆盖前者）。
2. `load_mapper_accesses(repo_root, mapper_glob)` → `_mapper_accesses`。
3. 遍历 `ma.accesses`：`_table_to_methods[acc.table].append((ma.namespace, ma.method_id, acc.op))`。
4. `_loaded = True`。

#### `resolve_mapper_methods(backend): void`（先 `self.load()`，再全量重建）
- 先 `clear()` 三个缓存：`_ns_id_to_method` / `_method_direct` / `_method_direct_by_pair`。
- 对每个 `ma`：
  1. `mid = _resolve_mapper_to_method_id(ma.namespace, ma.method_id, backend)`（可 null）。
  2. `stmt = f"{_short_mapper_name(ns)}.{ma.method_id}"`；`_short_mapper_name` = `ns.rsplit(".",1)[-1]`（无 `.` 取整体）。
  3. `pair_key = (shortName.strip().lower(), (ma.method_id or "").strip().lower())`。
  4. 若 `mid`：`_ns_id_to_method[(ma.namespace, ma.method_id)] = mid`。
  5. `mid_norm = normalize_method_entity_id(mid) if mid else ""`。
  6. 对每个 `acc`：`cols = acc.columns or []`（若是 str 包成 `[cols]`）；构造 `TableAccessDetail`：`columns=list(cols)[:30]`（**二次截断到 30**），`source_method_id = mid or ""`，`hop=0`，`path_method_ids = [mid] if mid else []`，`mapper_statement = stmt`，`sql_snippet = str(acc.sql_snippet or "")`。
  7. `_method_direct_by_pair[pair_key].append(detail)`（**无条件**，不要求 mid 存在）。
  8. 若 `mid and mid_norm`：`_method_direct[mid_norm].append(detail)`。

#### `_resolve_mapper_to_method_id(namespace, method_id, backend): string | null`
1. backend 假 → null。
2. `class_simple = namespace.rsplit(".",1)[-1]`（无 `.` 取整体）。
3. `search_fn = getattr(backend, "search_by_name", None)`；无 → null。
4. `hits = search_fn(method_id, entity_types=["method"], limit=200)`；`TypeError` → 退回 `search_fn(method_id, entity_types=["method"])`（不传 limit），其内再异常 → null；外层其他异常 → null。
5. 遍历 `hits or []`：`cn = str(h.get("class_name") or "").strip()`；命中条件 `class_simple in cn or cn.endswith("." + class_simple)` → 返回 `str(h.get("id") or "")`。
6. 无命中 → null。

#### `templates_for_bfs_method(backend, merge_backend, mid): TableAccessDetail[]`
三路合并，**去重用 `id(t)`（对象身份）**，`seen: set[int]`：
1. **路径1**：`mid_key = normalize_method_entity_id(mid)`；收 `_method_direct.get(mid_key, [])`。
2. **路径2**：若 `mid and mid != mid_key`，再收 `_method_direct.get(mid, [])`（用**原始 mid** 当 key）。
3. **路径3（图节点匹配）**：
   - `n = _get_node_with_variants(b, mid)`，对 `(backend, merge_backend)` 依次取，命中即停。`_get_node_with_variants` 对 `method_entity_id_variants(mid) or [mid]` 逐一 `get_node` 直到非空（返回 `dict(n)` 若有 `.keys()` 否则 n）。
   - `is_methodish = mid.lower().startswith("method://") or "method//"`；`et = str((n or {}).get("entity_type")).lower()`。
   - 若 `n and (et == "method" or is_methodish)`：
     - `simple = cn.rsplit(".",1)[-1].strip().lower()`（cn = `n.class_name`）；若空，`simple = _mapper_simple_class_from_location(n.location)`。
     - `mname = _method_simple_name_from_graph_node(n)`。
     - 若 `simple and mname`：收 `_method_direct_by_pair.get((simple, mname), [])`。

#### `_mapper_simple_class_from_location(loc): string`
- `s = (loc or "").replace("\\","/")`。
- 优先 `re.search(r"/([\w]+Mapper)\.java:", s, re.IGNORECASE)`（**含冒号**，匹配 source-location 格式 `.java:行号`）→ `group(1).lower()`。
- 否则 `re.search(r"([\w]+Mapper)\.java", s, re.IGNORECASE)` → `group(1).lower()`；均无 → `""`。

#### `_method_simple_name_from_graph_node(n): string`
1. `raw = str(n.get("name") or "").strip()`；非空 → `raw.lower()`。
2. 否则 `sig = str(n.get("signature") or "").strip()`；空或不含 `(` → `""`。
3. `head = sig.split("(",1)[0].strip()`；空 → `""`。
4. `parts = head.replace("@"," ").split()`（按空白分，去注解）；`token = parts[-1] if parts else head`；`token = token.split(".")[-1]`（取最后一段）；返回 `token.strip().lower()`。

#### `table_schema_text(table_name, max_cols=40): string`（先 `load()`）
- 表未找到：返回 `（DDL 中未找到表 \`{table_name}\`）`。
- 首行 `表 \`{name}\`（{len(columns)} 列）`；逐列（前 max_cols）`  · \`{c.name}\` {c.type_info}`；若 `len(columns) > max_cols` 追加 `  … 共 {n} 列，仅展示前 {max_cols} 个`；`"\n".join`。

#### 其他公开
- `tables()`（先 load）→ `[t.name for t in _tables]`；`tables_sorted()` → `sorted(tables())`。
- property `table_to_methods`（触发 load）→ `_table_to_methods`；property `ns_id_to_method` → `_ns_id_to_method`（**不触发 load**）。

---

## 五、图遍历（method_table_graph_walker）

### `GraphWalkSuccessorConfig`（frozen, slots dataclass）
```
calls_only:                   boolean
excluded_edge_rel_types:      tuple<string>
excluded_target_id_prefixes:  tuple<string>
```
- `method_to_table_default()`: `calls_only=False`, rels=`("implements",)`, prefixes=`("term://","domain://","capability://")`。
- `calls_only_default()`: `calls_only=True`, 其余空 tuple。

### `GraphWalkPredecessorConfig`（frozen, slots）
```
excluded_edge_rel_types:      tuple<string>
excluded_target_id_prefixes:  tuple<string>
```
- `table_to_method_default()`: rels=`("implements",)`, prefixes=`("term://","domain://","capability://")`。

### `filter_ids_excluding_prefixes(ids, prefixes): string[]`
- 逐 id：空跳过；`s = str(x).strip().lower()`；`any(s.startswith(p) for p in prefixes)` 则丢；否则收**原始 x**（非小写化版本）。

### `safe_successors_for_walk(backend, mid, walk): string[]`
- backend 或 mid 假 → `[]`。
- `try`：`calls_only` → `backend.successors(mid, rel_type="calls")`；否则若 `isinstance(backend, TraversalWithExclusionsCapable)`（runtime_checkable Protocol，**duck-type 检查**：实现 `successors_excluding_rel_types`+`predecessors_excluding_rel_types`）→ `backend.successors_excluding_rel_types(mid, walk.excluded_edge_rel_types)`；否则 `backend.successors(mid, rel_type=None)`。`or []` 兜底。任何异常 → `[]`。
- `dict.fromkeys` 保序去重（先滤空 `[x for x in raw if x]`）。
- `calls_only` → **直接返回，跳过前缀过滤**；否则 `filter_ids_excluding_prefixes(raw, walk.excluded_target_id_prefixes)`。

### `safe_predecessors_for_walk(backend, mid, walk): string[]`
- 同上结构，无 calls_only 分支：`TraversalWithExclusionsCapable` → `predecessors_excluding_rel_types`；否则 `predecessors(mid, rel_type=None)`。去重 + **总是**前缀过滤。异常 → `[]`。

### `merged_successors_for_walk / merged_predecessors_for_walk(primary, secondary, mid, walk)`
- 依次 `(primary, secondary)`（secondary 为 None 跳过），各自 `safe_*`，跨后端 `set` 保序去重。

### `bfs_min_hops(start, backend, max_hops, merge_backend=None, *, successor_walk=None): [Map<id,hop>, Map<id,parent|null>]`
- `walk = successor_walk or method_to_table_default()`。
- 初始 `best={start:0}`，`parent={start:None}`，`q=deque([start])`。
- `secondary = merge_backend if merge_backend is not backend else None`（**内部 self-merge 防护，与 service 层重复，TS 用 `===`**）。
- 循环：`mid=q.popleft()`；`h=best[mid]`；`if h >= max_hops: continue`（不展开）。
- `succs = merged_successors_for_walk(backend, secondary, mid, walk)` 若 secondary 真，否则 `safe_successors_for_walk(backend, mid, walk)`。
- 每 succ：空跳过；`nh=h+1`；`if nh > max_hops: continue`；`if succ not in best`：记 `best[succ]=nh`、`parent[succ]=mid`、入队。
- **魔法数字**：无（max_hops 调用方给，默认 8）。

### `reconstruct_path(parent, start, end): string[]`
- `end == start` → `[start]`。
- 从 end 沿 parent 上溯，`for _ in range(500)`（**最大 500 次回溯，防环**）：cur None 中断；`path_rev.append(cur)`；`cur==start` 中断；`cur = parent.get(cur)`。
- 若 `path_rev` 空或末项 != start → 返回 `[start, end]`（重建失败兜底）；否则 `reversed(path_rev)`。
- **魔法数字**：500。

### `resolve_bfs_start_id(primary, secondary, method_id): string`
- `c1 = _canonical_method_id(primary, method_id)`；若 `_backend_has_node(primary, c1)` → 返回 c1。
- 否则若 `secondary is not None and secondary is not primary`：`c2 = _canonical_method_id(secondary, method_id)`；若 `_backend_has_node(secondary, c2)` → 返回 c2。
- 兜底 → c1。

### 私有辅助
- `_backend_has_node(backend, nid)`：优先 `has_node(nid)`（callable）→ `bool(...)`；否则 `get_node(nid) is not None`；异常吞掉，均无则 `False`。
- `_canonical_method_id(backend, method_id)`：枚举 `method_entity_id_variants(method_id) or [method_id]`，返回第一个 `_backend_has_node` 为真的变体；无 → 原 `method_id`。

---

## 六、方法-表访问服务（method_table_access_service）

### `MethodTableAccessService(repo_root, ddl_path, mapper_glob)`
- 持 `_index = MapperAccessIndex(...)`。
- 薄委托：`load()`/`resolve_mapper_methods(backend)`/`templates_for_bfs_method(...)`/`table_schema_text(name, max_cols=40)`/`tables()`/`tables_sorted()`。

#### `_is_method_node(backend, nid): boolean`（**本模块私有函数，非 graph_walker**）
- `nid` 空 → False。
- `s = nid.strip().lower()`；前缀 `method://` 或 `method//` → True。
- 否则若 backend 有 `get_node`：`get_node(nid)`（异常 → None）；`n and str(n.get("entity_type")).lower() == "method"` → True。
- 否则 False。

#### `get_tables_for_method(method_id, backend, max_hops=8, merge_backend=None): MethodAccessResult`
1. `self.load()` + `self.resolve_mapper_methods(backend)`。
2. **若 `not backend` → 立即返回空 `MethodAccessResult()`**（在 resolve 之后）。
3. `start = resolve_bfs_start_id(backend, merge_backend, method_id)`。
4. `mb = merge_backend if merge_backend is not backend else None`（`===` 自合并防护）。
5. `best_hop, parent = bfs_min_hops(start, backend, max_hops, merge_backend=mb, successor_walk=method_to_table_default())`。
6. 遍历 `best_hop.items()` 的 `(mid, hop)`：对 `_index.templates_for_bfs_method(backend, mb, mid)` 每个 tmpl：
   - `path = reconstruct_path(parent, start, mid)`。
   - 构造 `TableAccessDetail`：`columns=list(tmpl.columns)`（**原样复制**，无再截断），`source_method_id=mid`，`hop=hop`，`path_method_ids=path`，`mapper_statement/sql_snippet` 来自 tmpl。
   - `op=="select"` → read_list，否则 write_list。
7. 各自 `_group_by_table_op` 后装入 `MethodAccessResult`。

#### `get_methods_for_table(table_name, backend, op_filter=None, max_hops=8, merge_backend=None): MethodForTable[]`
1. `load()` + `resolve_mapper_methods(backend)`。
2. `mb0 = merge_backend if merge_backend is not backend else None`；`pred_walk = table_to_method_default()`。
3. **构建 candidates**：遍历 `_index.table_to_methods.get(table_name, [])` 的 `(ns, mid_str, op)`：
   - `graph_mid = _index.ns_id_to_method.get((ns, mid_str))`；为假跳过。
   - 校验 `_is_method_node(backend, graph_mid) or (mb0 and _is_method_node(mb0, graph_mid))`；不满足跳过。
   - **op_filter（在 method-node 校验之后）**：`"read"` 且 `op != "select"` → 跳过；`"write"` 且 `op in ("select",)` → 跳过；null/其他 → 不过滤。
   - `candidates.append((graph_mid, op, 0))`。
4. **若 `not backend`**：直接返回 `candidates` 映射为 `MethodForTable(method_id=m, op=o, hop=0, source_method_id=m)`。
5. result 先放各 candidate（hop=0，source=self），`seen` 收 m。
6. 对每个 candidate **独立手写 BFS 向上**（`mb = mb0`）：
   - `queue=[(m,0)]`，`v=set()`（每候选独立 visited）。
   - `while queue`：`cur, hop = queue.pop(0)`（**FIFO/BFS 顺序，list.pop(0)**）；`if cur in v or hop > max_hops: continue`；`v.add(cur)`。
   - `is_m = _is_method_node(backend, cur) or (mb and _is_method_node(mb, cur))`。
   - 若 `hop > 0 and cur not in seen and is_m`：加入 result（`op=o`，`hop=hop`，`source_method_id=m`），`seen.add(cur)`。
   - `preds = merged_predecessors_for_walk(backend, mb, cur, pred_walk)` 若 mb 真，否则 `safe_predecessors_for_walk(backend, cur, pred_walk)`；空则 continue。
   - 每 pred：`if pred in v: continue`；`queue.append((pred, hop+1))`。
- **不复用 `bfs_min_hops`**（手写）。

#### `_group_by_table_op(details): TableAccessGrouped[]`
- `defaultdict` 按 `(table, op)` 分组。
- 组内 items `sorted(key=(hop, mapper_statement, source_method_id))`；`min_hop/max_hop` 取组内极值。
- 组间 `sort(key=(min_hop, table, op))`。

#### `format_method_table_debug_report(...)`（仅调试报告，TS 移植可选；产出多行中文文本，含 BFS 模式/后端类型名/起点/后继数/可达节点数/Mapper 绑定数/已映射节点数/project.yaml backend）。

---

## 七、外部依赖接口（需 TS 侧实现）

### `GraphBackend`（被动鸭子类型适配，无强制 interface）
| 方法 | 签名 | 用途 |
|------|------|------|
| `get_node(id)` | `(id: string) => Record<string,any> \| null` | 取节点属性（读 entity_type/class_name/name/signature/location） |
| `has_node(id)` | `(id: string) => boolean` | 存在检查（可选；缺则降级 `get_node !== null`） |
| `successors(id, rel_type)` | `(id: string, rel_type: string \| null) => string[]` | 出边邻居；calls_only 时传 `"calls"`，否则 `null` |
| `predecessors(id, rel_type)` | `(id: string, rel_type: string \| null) => string[]` | 入边邻居 |
| `successors_excluding_rel_types(id, types)` | `(id: string, types: string[]) => string[]` | 仅 `TraversalWithExclusionsCapable` |
| `predecessors_excluding_rel_types(id, types)` | `(id: string, types: string[]) => string[]` | 同上 |
| `search_by_name(name, entity_types, limit?)` | `(name, entity_types: string[], limit?: number) => {id, class_name}[]` | mapper 方法 id 解析；不支持 limit 时 Python 靠 TypeError 回退 |

### `TraversalWithExclusionsCapable`（runtime_checkable Protocol → TS 用 `typeof obj.fn === "function"` 双方法探测）
实现了 `successors_excluding_rel_types` + `predecessors_excluding_rel_types` 则走排除优化路径，否则全边遍历后由 `filter_ids_excluding_prefixes` 手动过滤。

### 复用模块
- `normalize_method_entity_id(eid)`：`method://` 原样；`method//x` → `method://x`；其余原样（`@ke` 已有等效实现需复用）。
- `method_entity_id_variants(eid)`：空 → `[]`；`method://x` → `[原, "method//x"]`；`method//x` → `[原, "method://x"]`；其余 → `[原]`（去重）。

---

## 八、怪癖与注意事项

1. **列数双重截断 50→30**：`_extract_columns_from_sql` 截 50，`resolve_mapper_methods` 构造 detail 时再 `[:30]`，故 `TableAccessDetail.columns` 最终上限 **30**。`get_tables_for_method` 复制 tmpl.columns 时不再截断。
2. **三处 merge_backend self-merge 防护**：`bfs_min_hops` 内部 / `get_tables_for_method` / `get_methods_for_table` 各用 `merge_backend is not backend`，TS 一律 `merge_backend !== backend`。
3. **`templates_for_bfs_method` 去重用对象身份 `id(t)`**：同一 detail 实例只入一次。TS 用 `Set` 存对象引用（或唯一标识），不可用值相等去重（否则会错误合并不同来源的等值 detail）。
4. **`_method_direct_by_pair` 即使 mid 解析失败也填充**（路径3 图匹配兜底用）；`_method_direct` 仅在 `mid and mid_norm` 时填充。
5. **一方法多表共享 columns/sql_snippet**：JOIN 多表时每表一条 TableAccess，但 columns 与 sql_snippet 是同一份。
6. **空 SQL/无表名整 elem 跳过**（mapper 解析）。
7. **DDL 不解析主键/索引/外键/唯一**；`TableInfo.comment` 恒空；未闭合括号的 CREATE TABLE 整条丢弃。
8. **`reconstruct_path` 500 次回溯上限**，重建失败兜底 `[start, end]`。
9. **`_get_sql_text` 不递归 `<include refid>`**，动态标签仅取 text+tail —— 已知局限，TS 照抄。
10. **`search_by_name` limit 回退**：Python 靠 `TypeError` 二次调用不传 limit。TS 移植统一传 limit；若适配的 backend 不支持 limit，需在适配层吞掉/忽略而非抛错。
11. **`_mapper_simple_class_from_location` 双正则**：先 `/([\w]+Mapper)\.java:/i`（含冒号），失败才 `/([\w]+Mapper)\.java/i`；返回值 `.lower()`。
12. **`_is_method_node` 定义在 service 模块**（非 graph_walker）；与 graph_walker 的 `_backend_has_node` 语义不同（前者判类型，后者判存在）。
13. **`ns_id_to_method` property 不触发 load**（与 `table_to_methods` property 触发 load 不同）；调用方需保证已 `resolve_mapper_methods`。

源码绝对路径：`/Users/java/knowledge-engineering/src/knowledge/{method_table_types,ddl_parser,mapper_sql_parser,mapper_access_index,method_table_graph_walker,method_table_access_service}.py`，依赖 `method_entity_id_normalize.py` 与 `abstractions.py`。

---

# 【模式识别 pattern recognition】

All three files read. Now let me verify the specific claims in the spec against the source, paying attention to the high-risk areas flagged (regex, operator precedence, magic numbers, JSON structure).

The spec is largely accurate. Let me document my findings with precise corrections.

## 修正清单

1. **`_extract_json` markdown fence 正则不准确**（spec §2.3 描述含糊且 fence 正则不是简单"去掉 ```json ... ``` fences"）。实际源码（line 43）：`re.sub(r"^```(?:json)?\s*|```$", "", text.strip(), flags=re.IGNORECASE | re.MULTILINE)`。注意 flag 是 `re.IGNORECASE | re.MULTILINE`（**没有** `re.DOTALL`），且替换的是行首 ` ```json ` / 行首 ` ``` ` 和行尾 ` ``` `。spec 笼统写"忽略大小写，多行模式"基本对，但应逐字给出正则供 TS 移植。

2. **`_extract_json` 兜底正则的"轻微 bug"分析方向正确但措辞需精确**。源码 line 51：`re.search(r"[\\[{]", cleaned)`。在 Python 字符类 `[...]` 内，`\\` 是转义后的单个反斜杠字面量，`[`（已在字符类内，无需转义）是字面量 `[`，`{` 是字面量 `{`。所以字符类匹配集合 = `{反斜杠, '[', '{'}`。spec 说"`\\[` 实际匹配反斜杠或 `[`"——更准确的表述是整个字符类匹配三者之一（`\`、`[`、`{`），意图应是匹配 `[` 或 `{`。结论（轻微 bug、影响微小）成立。

3. **`_heuristic_fallback` 的 `summary` 文本 spec 完全未提**。源码 line 214：`summary = f"{language}-heuristic: 基于命名关键词的弱信号候选（置信度偏低）。模式：{name}"`。spec 只描述了 `evidence.notes` 固定文案，但兜底模式的 **summary 字段也是固定模板**（含 language 前缀 + pattern name），TS 移植必须照搬此格式。这是 spec 的遗漏，应补。

4. **`_heuristic_fallback` 写入向量文本时 summary 会被截断到 5000**——这点 spec §七表格写 `summary[:5000]` 仅归到 `_validate_and_normalize_patterns`，但兜底路径里 `add_if`（line 219）同样 `summary[:5000]`。两条路径都截断，spec 表格位置标注不全，补充。

5. **`_heuristic_fallback` 的 `add_if` 还有一道 `conf <= 0` 的过滤**（line 212：`if conf <= 0: return None`）。spec 完全没提这道闸。虽然所有硬编码 conf 都 > 0，但这是行为规范的一部分，应记录。

6. **`module_ids` 默认行为 / `skip_if_exists` 在 modules 路径的实际语义**：spec §2.2 modules 算法说"若 skip_if_exists=True 且已有记录 → 跳过识别，直接读 store"——正确。但需补充：modules 路径下"跳过"用的是 `continue`（line 441），即跳过后**仍读 store** 赋值；而 system 路径下"跳过识别"时 `patterns = []`（line 428）后**仍执行** `list_by_scope` 读回。两者最终都会读 store，spec 流程图正确，但 system 与 module 的控制流写法不同（system 用 if/else，module 用 continue），实现细节差异已澄清。

7. **`recognize_patterns_for_scope` 返回值**：spec 出参注释说"返回实际写入的模式列表（长度 <= top_n）"。实际源码 line 391 `return patterns`，返回的是 **`min_confidence` 过滤后的完整 `patterns` 列表**，**未** `[:top_n]` 截断（只有写入循环 line 371 `patterns[:top_n]` 用了 top_n）。所以返回长度可能 > top_n（若过滤后多于 top_n 条，写入只写前 top_n，但返回全部）。spec 的"长度 <= top_n"**不准确**，应改为"返回 min_confidence 过滤后的全部 patterns（未截断），与实际写入的前 top_n 条可能不一致"。§2.2 算法步骤 7 的"返回 patterns"正确，但出参注释要修正。

8. **`_validate_and_normalize_patterns` 的 `evidence` 非 dict 分支** spec 未覆盖。源码 line 103-105：若 `evidence` 不是 dict，则 `entity_ids=[]`、`notes=str(evidence)`。spec §2.3 只描述了"evidence 字段若为 dict"的情况，遗漏了非 dict 兜底（notes 取整个 evidence 的字符串化）。补充。

9. **`build_system_pattern_context` 的 `class_names` 取前 8 个**（line 179：`[e.name for e in hint_entities if e.name][:8]`），module 版同样 `[:8]`（line 256）。这个 `[:8]` 限制喂给 `_extract_method_excerpts` 的类名集合，spec §三魔法数字汇总**漏了 `[:8]`**。应补入魔法数字表。

10. **空节占位文案 spec 未列**。各节为空时有固定中文占位：跨模块依赖 `"- （无明显跨模块依赖证据）"`、entry `"- （无 path 属性方法）"`、hint `"- （无明显关键词命中）"`、method excerpts `"- （无 code_snippet 节选）"`。TS 移植需照搬，spec §三输出格式应补这些 fallback 行。

11. **`_collect_design_hint_entities` 的兜底 module 扫描条件**：spec §3.3 写"若自身无 score 且 `in_module` 非 None，额外扫描 in_module 字符串（score=1）"——正确（line 121-123）。准确。

12. **`format_allowed_patterns_for_prompt` 文案逐字**：spec §1.4 描述格式大体对，但应给逐字串供移植。实际为 `"Design patterns (GoF 23, pattern_name 必须从下面列表中严格选择):\n"` + 各行 `"- {n}"` + `"\n\nArchitecture patterns (common, pattern_name 必须从下面列表中严格选择):\n"` + 各行。spec 写的 `"Design patterns (GoF 23, ...):"` 省略号处实际含中文约束句，移植需逐字。

---

# Pattern Recognition 模块行为规范（修正版）

**源文件：**
- `/Users/java/knowledge-engineering/src/knowledge/pattern_recognition_catalog.py`（103 行）
- `/Users/java/knowledge-engineering/src/knowledge/pattern_recognition_runner.py`（461 行）
- `/Users/java/knowledge-engineering/src/knowledge/pattern_recognition_context_builders.py`（285 行）

---

## 一、模式 Catalog（pattern_recognition_catalog.py）

### 1.1 数据结构

```
PatternItem (frozen dataclass)
  pattern_type: str   // "design" | "architecture"
  name: str           // 模式官方名称（作为 ID 使用）
  hint: str           // 中文说明，仅用于 prompt 展示（实际 hint 字段当前未被任何函数读取）
```

### 1.2 设计模式列表（GoF 23 + 2 个工程补充信号）

`DESIGN_PATTERNS` 定义 **25 条**，但 `allowed_pattern_names()` 返回**严格硬编码的 GoF 23**（去掉 `Command (Redo/Undo)`、`Proxy (Lazy/Access)`）：

```
Singleton, Factory Method, Abstract Factory, Builder, Prototype,
Adapter, Decorator, Facade, Bridge, Composite, Flyweight, Proxy,
Chain of Responsibility, Command, Mediator, Iterator, Template Method,
Observer, State, Strategy, Visitor, Memento, Interpreter
```

**重要**：`allowed_pattern_names()` 的 design 列表是**独立硬编码字面量**（line 64-89），**不是**从 `DESIGN_PATTERNS` 派生过滤。TS 移植可直接内联此 23 项常量数组。

### 1.3 架构模式列表（10 条）

```
Layered Architecture
MVC (Model-View-Controller)
Hexagonal Architecture
Clean Architecture
Onion Architecture
Microservices Architecture
Event-Driven Architecture
CQRS
DDD - Layered
Plugin/Extension Architecture
```

arch 列表通过 `[p.name for p in ARCHITECTURE_PATTERNS]` 派生（line 90）。

### 1.4 公开函数

**`allowed_pattern_names() -> tuple[list[str], list[str]]`**
- 返回 `(design_names, arch_names)`：design 硬编码 23 项；arch 派生 10 项
- Runner 用此结果构建 `set` 做白名单过滤

**`format_allowed_patterns_for_prompt() -> str`**
逐字格式（移植照搬，含中文约束句）：
```
Design patterns (GoF 23, pattern_name 必须从下面列表中严格选择):
- {design_name}
...（23 行）

Architecture patterns (common, pattern_name 必须从下面列表中严格选择):
- {arch_name}
...（10 行）
```
- 段间分隔为 `\n\n`，列表行格式 `- {n}`

---

## 二、Runner 核心逻辑（pattern_recognition_runner.py）

### 2.1 顶层数据结构

```
Evidence (frozen dataclass)
  entity_ids: list[str]   // 写入时截断 [:32]
  notes: str              // 写入时截断 [:2000]

RecognizedPattern (frozen dataclass)
  pattern_type: str   // "design" | "architecture"
  pattern_name: str   // 必须在 allowed 白名单中
  confidence: float   // [0.0, 1.0]，_clamp_confidence 强制夹紧
  summary: str        // 写入时截断 [:5000]
  evidence: Evidence
```

### 2.2 公开函数

#### `recognize_patterns_for_scope`

```
入参（全部 keyword-only，函数签名用 *）：
  facts: StructureFacts
  llm: Any                       // 实现 .generate(prompt, **kwargs) -> str
  store: WeaviatePatternInterpretStore
  embedding_dim: int
  language: str                  // "zh" | "en"
  scope_type: str                // "system" | "module"（其他抛 ValueError）
  target_id: str
  top_n: int = 12
  min_confidence: float = 0.0
  llm_timeout_seconds: Optional[int] = None

出参：list[RecognizedPattern]
  // ★修正：返回 min_confidence 过滤后的【全部】 patterns，未按 top_n 截断。
  // 写入只写前 top_n 条（patterns[:top_n]），但返回值可能长于 top_n，
  // 与实际写入集合可能不一致。
```

**算法步骤：**
1. `allowed_pattern_names()` → 两个 set（`allowed_design_set`、`allowed_arch_set`）
2. 按 `scope_type` 选 context builder：
   - `"system"` → `build_system_pattern_context(facts)`
   - `"module"` → `build_module_pattern_context(facts, module_id=target_id)`
   - 其他 → `raise ValueError(f"Unsupported scope_type: {scope_type}")`
3. `_build_prompt(...)` 构造提示词
4. `llm.generate(prompt, **gen_kwargs)`，其中 `gen_kwargs["timeout"]=int(llm_timeout_seconds)` 仅当非 None：
   - **抛异常**：`_LOG.warning` 后 → `_heuristic_fallback(...)`
   - **正常返回**：`_extract_json(raw_text)` → `_validate_and_normalize_patterns(...)`；若结果**为空列表** → `_heuristic_fallback(...)`
5. `patterns = [p for p in patterns if p.confidence >= float(min_confidence)]`
6. `for p in patterns[:top_n]`：逐条写入：
   - 向量文本（逐字）：`f"[{p.pattern_type}] {p.pattern_name}\n置信度={p.confidence}\n{p.summary}\n证据说明={p.evidence.notes}"`
   - `vec = get_embedding(vec_text, embedding_dim)`
   - `evidence_json = json.dumps({"entity_ids": ..., "notes": ...}, ensure_ascii=False)`（失败兜底 `str(p.evidence)`）
   - `store.add(vec, scope_type=, target_id=, pattern_type=, pattern_name=, confidence=, summary_text=, evidence_json=, language=, related_entity_ids_json=json.dumps(entity_ids, ensure_ascii=False))`
7. `return patterns`（过滤后全量，非写入子集）

#### `recognize_patterns_system_and_modules`

```
入参（keyword-only）：
  facts, llm, store, embedding_dim, language
  recognize_system: bool
  recognize_modules: bool
  module_ids: list[str]
  top_n: int
  min_confidence: float = 0.0
  skip_if_exists: bool = True
  llm_timeout_seconds: Optional[int] = None

出参：dict[str, Any]
  { "system": list[dict] | None, "modules": { module_id: list[dict] } }
```

**system 路径（line 412-432）：**
1. `existing = store.list_by_scope(scope_type="system", target_id="system", limit=1)`
2. `if not (skip_if_exists and existing)` → 执行 `recognize_patterns_for_scope(scope="system", target="system", ...)`；否则 `patterns = []`（识别被跳过）
3. **无论是否跳过**，都 `sys_rows = store.list_by_scope(... limit=top_n*3)` → 按 `float(x.get("confidence") or 0.0)` 降序排序 → `out["system"] = sys_rows[:top_n]`

**modules 路径（line 434-457）：**
1. 遍历 `module_ids`，每个 `mid`：
   - `rows = store.list_by_scope(scope_type="module", target_id=mid, limit=1)`
   - `if skip_if_exists and rows`：读 `limit=top_n*3` → 排序 → `out["modules"][mid] = _rows[:top_n]` → `continue`（不识别）
   - 否则 `recognize_patterns_for_scope(scope="module", target=mid, ...)`，再读 `limit=top_n*3` → 排序 → `out["modules"][mid] = _rows[:top_n]`

**怪癖**：
- `recognize_system=False` 时 `out["system"]` 保持 `None`（初始值），需区分 `None`（未执行）vs `[]`（执行但无结果）。
- system 用 if/else 控制（跳过则 `patterns=[]`），module 用 `continue` 控制——两者最终都读 store，但代码结构不同。
- 排序 key 对缺失/None confidence 用 `or 0.0` 兜底。

### 2.3 内部函数

#### `_extract_json(text: str) -> Any`
1. `text` 为空/None → 返回 `None`
2. 去 fence（逐字）：`re.sub(r"^```(?:json)?\s*|```$", "", text.strip(), flags=re.IGNORECASE | re.MULTILINE)`
   - ★修正：flag 为 `IGNORECASE | MULTILINE`（无 DOTALL）；匹配行首 ` ```json `/行首 ` ``` `/行尾 ` ``` `
3. `json.loads(cleaned)`，成功即返回
4. 失败 → `m = re.search(r"[\\[{]", cleaned)`；无匹配返回 `None`；否则从 `m.start()` 截到末尾再 `json.loads`，仍失败返回 `None`

**怪癖（修正措辞）**：字符类 `[\\[{]` 中 `\\` 是单个反斜杠字面量、`[` 与 `{` 是字面量，故该字符类匹配 `{反斜杠, '[', '{'}` 三者之一；意图应为匹配 `[` 或 `{`。属轻微 bug，实际影响微小（LLM 极少以反斜杠开头）。TS 移植应用 `/[[{]/` 或 `/[\[{]/`（不含反斜杠）。

#### `_clamp_confidence(v: Any) -> float`
- `float(v)` 失败（含 None/非数字）→ `0.0`；`<0` → `0.0`；`>1` → `1.0`；否则原值

#### `_validate_and_normalize_patterns(raw, *, allowed_design, allowed_arch) -> list[RecognizedPattern]`
1. `raw` 非 dict → `[]`
2. `items = raw.get("top_patterns") or raw.get("patterns") or []`；`items` 非 list → `[]`
3. 逐项（非 dict 项 `continue`）：
   - `ptype = (it.get("pattern_type") or "").strip().lower()`
   - `pname = (it.get("pattern_name") or "").strip()`
   - `conf = _clamp_confidence(it.get("confidence", 0.0))`
   - `summary = (summary or summary_text or description or "").strip()`
   - `evidence = it.get("evidence") or {}`：
     - **若为 dict**：`entity_ids = evidence.get("entity_ids") or evidence.get("entities") or []`；若为 str 包成 `[str]`；非 list 置 `[]`；`entity_ids = [str(x) for x in entity_ids if x]`（过滤 falsy 并字符串化）；`notes = (notes or reason or "").strip()`
     - **★修正：若非 dict**：`entity_ids = []`，`notes = str(evidence)`（整个 evidence 字符串化作为 notes）
   - `ptype` 非 "design"/"architecture" → 丢弃
   - `pname` 不在对应白名单 → 丢弃
   - `summary` 为空 → 丢弃
   - 入列时截断：`summary[:5000]`、`entity_ids[:32]`、`notes[:2000]`

#### `_build_prompt(*, language, scope_type, target_id, top_n, context) -> str`
- `lang_hint = "简体中文" if (language or "zh").lower().startswith("zh") else "English"`
- 注入 `format_allowed_patterns_for_prompt()`
- 角色：资深软件架构师与重构顾问
- 输出严格 JSON，schema 内嵌 `top_patterns[]`，每项 `pattern_type`/`pattern_name`/`confidence`/`summary`（用 lang_hint）/`evidence.entity_ids`/`evidence.notes`；JSON 顶层还含 `scope_type`/`target_id` 回显
- 末尾附 `{context}`，提示"不要原样复述它，只用来推断"

#### `_heuristic_fallback(*, facts, scope_type, target_id, top_n, language) -> list[RecognizedPattern]`

**算法：**
1. 收集 name_tokens：遍历 `facts.entities`，`scope_type=="module"` 时跳过 `e.module_id != target_id`；加入 `e.name`（strip 非空）；若 `e.type.value == "method"`，再加入 `attrs.get("signature") or e.name`（须为非空 str）
2. `blob = " ".join(name_tokens).lower()`
3. `add_if(name, ptype, conf)` 内部门闸（★修正：spec 漏了第三道）：
   - design 且 name 不在 `allowed_design_set` → None
   - architecture 且 name 不在 `allowed_arch_set` → None
   - **`conf <= 0` → None**（当前硬编码 conf 均 >0，但移植须保留此判断）
   - 否则构造 RecognizedPattern，**summary 固定模板**（★修正：spec 缺）：
     `f"{language}-heuristic: 基于命名关键词的弱信号候选（置信度偏低）。模式：{name}"`，再 `[:5000]`
   - `evidence = Evidence(entity_ids=[], notes="LLM 输出解析失败时的兜底候选（仅用于可视化/快速提示）。")`

**设计模式关键词触发规则（含置信度，按源码顺序）：**

| 关键词（OR 关系，在 blob 中） | pattern_name | confidence |
|---|---|---|
| singleton / getinstance / get_instance | Singleton | 0.35 |
| factory / newfactory / create | Factory Method | 0.30 |
| abstractfactory | Abstract Factory | 0.25 |
| builder | Builder | 0.28 |
| adapter / convert / translate | Adapter | 0.26 |
| decorator / wrap | Decorator | 0.24 |
| facade | Facade | 0.26 |
| proxy | Proxy | 0.24 |
| observer / listener / event | Observer | 0.22 |
| strategy | Strategy | 0.22 |
| template | Template Method | 0.20 |
| iterator | Iterator | 0.20 |

**架构模式关键词触发规则（按源码顺序）：**

| 条件（在 blob 中） | pattern_name | confidence |
|---|---|---|
| controller AND service | MVC (Model-View-Controller) | 0.35 |
| controller AND service（同一 if 块内追加） | Layered Architecture | 0.32 |
| event / listener / subscriber | Event-Driven Architecture | 0.28 |
| `cqrs in blob or command in blob and query in blob` | CQRS | 0.22 |
| `hexagonal in blob or port in blob and adapter in blob` | Hexagonal Architecture | 0.20 |
| clean AND architecture | Clean Architecture | 0.20 |

**怪癖**：Python `and` 优先级高于 `or`，故 `"cqrs" in blob or "command" in blob and "query" in blob` 等价于 `("cqrs" in blob) or (("command" in blob) and ("query" in blob))`；Hexagonal 同理。TS 移植必须用括号显式表达 `(cqrs) || (command && query)`，否则 JS 同样优先级正确但需明确分组以防误改。

4. `out.sort(key=lambda x: x.confidence, reverse=True)`，`return out[:top_n]`

---

## 三、Context Builders（pattern_recognition_context_builders.py）

### 3.1 内部关键词表 `_DESIGN_KEYWORDS`（27 对）

```
Singleton → Singleton
getInstance → Singleton
instance → Singleton          // 兜底信号，注释"后续会通过上下文裁剪"
Factory → Factory Method
AbstractFactory → Abstract Factory
Builder → Builder
Prototype → Prototype
Adapter → Adapter
Decorator → Decorator
Facade → Facade
Bridge → Bridge
Composite → Composite
Flyweight → Flyweight
Proxy → Proxy
Chain → Chain of Responsibility
ChainOfResponsibility → Chain of Responsibility
Command → Command
Mediator → Mediator
Iterator → Iterator
Template → Template Method
Observer → Observer
Listener → Observer
State → State
Strategy → Strategy
Visitor → Visitor
Memento → Memento
Interpreter → Interpreter
```

匹配：`re.compile(re.escape(kw), re.IGNORECASE)` 的 `.search()`（子串、大小写不敏感、字面量转义）。注意映射的第二元素（pattern 名）在 `_collect_design_hint_entities` 中**当前未被使用**（仅用 score 计数），只是注释性映射。

### 3.2 公开函数

#### `build_system_pattern_context(facts, *, max_hint_entities=10) -> str`

- `entity_counts = _count_by_type(facts, [MODULE, PACKAGE, CLASS, INTERFACE, METHOD])`
- `module_ids = sorted({e.module_id for e in facts.entities if e.module_id})`
- `modules_line = ", ".join(module_ids[:12]) + ("…（共 N 个）" if len>12 else "")`
- `edges = _sample_module_edges(facts, max_edges=10)`
- `entry = _entry_points(facts, max_paths=12)`
- `hint_entities = _collect_design_hint_entities(facts, in_module=None, max_entities=max_hint_entities)`
- `hint_lines`：`f"- {e.id} | {e.name or ''} | module={e.module_id or ''}"`
- `class_names = [e.name for e in hint_entities if e.name][:8]`（★修正：`[:8]` 漏标，喂给 excerpt 提取的类名上限）
- `method_excerpts = _extract_method_excerpts(facts, class_names=, in_module=None, max_methods=3)`

输出节（`\n` 拼接，节间空行）；★修正：补各节空时的固定占位行：

```
=== System Evidence (structure_facts) ===
- modules: {modules_line}
- entity counts (selected types): {k}={v}, ...（按 entity_counts 项 sorted）

=== Cross-module dependency samples ===
{edges 或 "- （无明显跨模块依赖证据）"}

=== Entry points (HTTP-like paths) ===
{entry 或 "- （无 path 属性方法）"}

=== Design-pattern hint entities (name keywords) ===
{hint_lines 或 "- （无明显关键词命中）"}

=== Method code excerpts (from hint classes) ===
{method_excerpts 或 "- （无 code_snippet 节选）"}
```

#### `build_module_pattern_context(facts, *, module_id, max_hint_entities=10) -> str`

- `sub_entities = [e for e in facts.entities if e.module_id == module_id]`；若空返回：
  ```
  === Module Evidence ===
  - module_id={module_id}
  - （无该 module 的实体）
  ```
- ★注意：源码 line 212 先调了一次全局 `_count_by_type(...)` 但**丢弃不用**（dead code），随后用 `ts={CLASS,INTERFACE,METHOD}` 二次遍历 `sub_entities` 算 `mod_counts`（模块内统计）
- 边样本：内联实现（非调 `_sample_module_edges`），过滤跨模块 CALLS/DEPENDS_ON/EXTENDS/IMPLEMENTS 且 `s.module_id==module_id or t.module_id==module_id`（进出该 module），`most_common(10)`
- entry：内联，只取 `sub_entities` 中 METHOD 的 `attrs["path"]`（非空 str），key 为纯 path（**无** `（module）` 后缀，区别于 system 版），`most_common(12)`
- hint_entities：`in_module=module_id`；`hint_lines` 格式 `f"- {e.id} | {e.name or ''}"`（无 module= 字段）
- `class_names = [...][:8]`；`method_excerpts = _extract_method_excerpts(..., in_module=module_id, max_methods=3)`

输出节（占位行同 system 版）：
```
=== Module Evidence (structure_facts) ===
- module_id: {module_id}
- entity counts (selected types, module-local): {k}={v}, ...（sorted）

=== In/Out module dependency samples ===
{edges 或 "- （无明显跨模块依赖证据）"}

=== Entry points (module-local paths) ===
{entry 或 "- （无 path 属性方法）"}

=== Design-pattern hint entities (name keywords) ===
{hint_lines 或 "- （无明显关键词命中）"}

=== Method code excerpts (from hint classes) ===
{method_excerpts 或 "- （无 code_snippet 节选）"}
```

### 3.3 内部工具函数

#### `_index_entities(facts) -> dict[str, StructureEntity]`
- `{e.id: e for e in facts.entities}`（id→entity 映射）

#### `_safe_one_line(s, limit) -> str`
- strip + `\r`/`\n` 替换为空格；若 `len > limit` 截到 `s[:limit-1] + "…"`

#### `_count_by_type(facts, entity_types) -> dict[str, int]`
- 按 `e.type in set(entity_types)` 计数，key 为 `e.type.value`

#### `_sample_module_edges(facts, *, max_edges=10) -> list[str]`
- 只计跨模块（`s.module_id != t.module_id`，且双方 module_id 非空）的 CALLS/DEPENDS_ON/EXTENDS/IMPLEMENTS
- `Counter.most_common(max_edges)`，格式 `f"- {k}  （{v} 条关系证据）"`（k 为 `"A -> B"`）

#### `_entry_points(facts, *, max_paths=12) -> list[str]`
- 只取 METHOD，`attrs["path"]` 非空 str；key = `f"{path}（{mod}）"`（有 module 时）否则纯 path；`most_common(max_paths)`，格式 `f"- {k}"`

#### `_collect_design_hint_entities(facts, *, in_module, max_entities=10) -> list[StructureEntity]`
- 预编译 `_DESIGN_KEYWORDS` 为 `(re.compile(re.escape(kw), IGNORECASE), pattern)`
- 遍历实体：仅 CLASS/INTERFACE；`in_module` 非空时跳过 `e.module_id != in_module`
- `score` = 命中关键词正则的数量（对 `e.name` 计数，可累加多个 kw）
- 兜底：`if not score and in_module:` 且任一关键词命中 `in_module` 字符串 → `score=1`
- `score>0` 入列 `(score, e)`；按 score 降序，取前 `max_entities`

#### `_extract_method_excerpts(facts, *, class_names, in_module, max_methods=3, excerpt_chars=900) -> list[str]`
- `class_set = set(class_names)`
- 只取 METHOD；`in_module` 非空时跳过非本模块；`attrs["class_name"]` 须 ∈ class_set；`attrs["code_snippet"]` 须非空 str
- 输出格式：
  ```
  - {m.id} | {cls_name}.{m.name or ''}
    signature: {_safe_one_line(sig, 140)}
    excerpt: {snippet[:900].replace("\n"," ")}{… 若原文超 900}
  ```
  - `sig = attrs.get("signature") or m.name or ""`
  - `chr(10)`（换行）替换为空格
- 达 `max_methods` 即 break

---

## 四、Weaviate 写入结构（PatternInterpretation collection）

写入点：`recognize_patterns_for_scope` line 379 `store.add(vec, ...)`，参数：

| 字段名 | 类型 | 来源 | 备注 |
|---|---|---|---|
| `vec`（位置参数） | float[] | `get_embedding(vec_text, embedding_dim)` | 第一个位置实参，非 keyword |
| `scope_type` | str | 入参 | "system" \| "module" |
| `target_id` | str | 入参 | "system" 或 module_id |
| `pattern_type` | str | RecognizedPattern | "design" \| "architecture" |
| `pattern_name` | str | RecognizedPattern | 白名单内官方名 |
| `confidence` | float | RecognizedPattern | [0.0, 1.0] |
| `summary_text` | str | RecognizedPattern.summary | ≤5000 |
| `evidence_json` | str | `json.dumps({"entity_ids":[...],"notes":"..."}, ensure_ascii=False)` | 失败兜底 `str(p.evidence)` |
| `language` | str | 入参 | "zh" 等 |
| `related_entity_ids_json` | str | `json.dumps(entity_ids, ensure_ascii=False)` | ≤32 项 |

注：`store.add` 第一参数 `vec` 是**位置参数**，其余全 keyword。`json.dumps` 用 `ensure_ascii=False`（保留中文，移植 `JSON.stringify` 默认即非转义，行为一致）。

**向量文本（embedding 输入）逐字格式：**
```
[{pattern_type}] {pattern_name}
置信度={confidence}
{summary}
证据说明={notes}
```
（注意 `置信度={confidence}` 处 confidence 为 float 直接字符串化，如 `0.35`；Python `f"{0.35}"` → `"0.35"`，TS 须保证数值字符串化一致）

> 本阶段写 collection schema 未在这三个文件中定义——`WeaviatePatternInterpretStore.add()` 内部建表/dataType（属性名 `scope_type`/`target_id`/`pattern_type`/`pattern_name`/`confidence`/`summary_text`/`evidence_json`/`language`/`related_entity_ids_json` 及 vector 维度）需另查 `src/knowledge/weaviate_pattern_store.py`，**不在本次范围**。UUID 生成/dataType（text vs number）以该文件为权威。

---

## 五、外部依赖

| 依赖 | 调用位置 | 用途 |
|---|---|---|
| `src.knowledge.weaviate_pattern_store.WeaviatePatternInterpretStore` | runner | Weaviate 写入+查询（`.add()`/`.list_by_scope()`）|
| `src.semantic.embedding.get_embedding(text, dim)` | runner | 向量文本 → float[] |
| `src.models.structure.StructureFacts` | runner + context_builders | 实体+关系数据来源（`.entities`/`.relations`）|
| `src.models.structure.EntityType` | context_builders | 枚举（MODULE/PACKAGE/CLASS/INTERFACE/METHOD），比较用 `e.type`；`e.type.value` 取字符串 |
| `src.models.structure.RelationType` | context_builders | 枚举（CALLS/DEPENDS_ON/EXTENDS/IMPLEMENTS）|
| `src.models.structure.StructureEntity` | context_builders | 类型注解 + `.id`/`.name`/`.type`/`.module_id`/`.attributes` |
| `llm: Any` | runner | 须实现 `.generate(prompt: str, **kwargs) -> str`，kwargs 可含 `timeout: int` |

实体属性约定（`e.attributes` dict）：METHOD 用 `signature` / `code_snippet` / `class_name` / `path`。

---

## 六、整体执行流程图

```
recognize_patterns_system_and_modules(facts, llm, store, ...)
  ├─ [system] if recognize_system:
  │    store.list_by_scope("system","system", limit=1)
  │    ├─ skip_if_exists && existing → patterns=[]（跳过识别）
  │    └─ else → recognize_patterns_for_scope(scope="system", target="system")
  │              ├─ build_system_pattern_context(facts)
  │              ├─ _build_prompt → llm.generate
  │              │    ├─ 异常 → _heuristic_fallback
  │              │    └─ 成功 → _extract_json → _validate_and_normalize_patterns
  │              │              （结果为空 → _heuristic_fallback）
  │              ├─ 过滤 confidence >= min_confidence
  │              ├─ for p in patterns[:top_n]: get_embedding → store.add
  │              └─ return patterns（过滤后全量，未截 top_n）
  │    然后无条件：list_by_scope(limit=top_n*3) → 降序 → out["system"]=[:top_n]
  │
  └─ [modules] if recognize_modules: for mid in module_ids:
       list_by_scope("module", mid, limit=1)
       ├─ skip_if_exists && rows → 读 store(top_n*3) → [:top_n] → continue
       └─ else → recognize_patterns_for_scope(scope="module", target=mid)
                 然后 list_by_scope(top_n*3) → 降序 → out["modules"][mid]=[:top_n]

recognize_system=False → out["system"] 恒为 None（区别于 []）
```

---

## 七、关键魔法数字汇总

| 数字 | 位置 | 含义 |
|---|---|---|
| `top_n = 12` | `recognize_patterns_for_scope` 默认参数 | 每次最多写入模式数（写入用 `[:top_n]`）|
| `top_n * 3` | 读回逻辑（system & module） | store 查询 limit，预留排序余量 |
| `summary[:5000]` | `_validate_and_normalize_patterns` line 125 **及** `_heuristic_fallback` add_if line 219 | summary 最大长度（★两路径都截）|
| `entity_ids[:32]` | `_validate_and_normalize_patterns` line 126 | 证据 entity_ids 上限 |
| `notes[:2000]` | `_validate_and_normalize_patterns` line 126 | 证据 notes 最大长度 |
| `max_hint_entities = 10` | context builders 默认参数 | hint 实体数上限 |
| `[:8]` | build_system/module context `class_names` | ★喂给 excerpt 提取的类名上限（spec 原漏）|
| `max_edges = 10` | `_sample_module_edges` / module 内联 | 跨模块边样本数 |
| `max_paths = 12` | `_entry_points` / module 内联 | path 样本数 |
| `excerpt_chars = 900` | `_extract_method_excerpts` | 代码节选最大字符数 |
| `max_methods = 3` | `_extract_method_excerpts` | 方法节选条数上限 |
| `signature 140` | `_extract_method_excerpts`（`_safe_one_line(sig,140)`）| signature 截断 |
| `_safe_one_line` 截断 `s[:limit-1]+"…"` | `_safe_one_line` | 截断保留 limit-1 字符再加省略号 |
| `module_ids[:12]` | `build_system_pattern_context` | modules 展示上限 |
| design conf `0.20–0.35` | `_heuristic_fallback` | 兜底设计模式置信度范围 |
| arch conf `0.20–0.35` | `_heuristic_fallback` | 兜底架构模式置信度范围 |
| `conf <= 0` 闸 | `_heuristic_fallback.add_if` line 212 | ★兜底第三道过滤（spec 原漏）|

---

**核验结论**：spec 整体准确度高，规则表/魔法数字/怪癖分析基本无误。修正集中在 6 处遗漏（兜底 summary 模板、`conf<=0` 闸、evidence 非 dict 分支、`class_names[:8]`、空节占位文案、`recognize_patterns_for_scope` 返回值不截 top_n）和 2 处措辞精确化（fence 正则逐字 + 兜底正则字符类三元素分析）。最高风险项是**返回值语义（#7）**——若 TS 移植者据 spec 误以为返回 `[:top_n]` 截断列表，会与写入子集行为不一致；以及 **module 版 entry_points 无 `（module）`后缀**这一与 system 版的差异（已在 §3.2 标注）。Weaviate schema/dataType/UUID 的最终权威在 `weaviate_pattern_store.py`（本次范围外）。