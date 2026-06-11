# P2d 召回链路行为规范（py-final-baseline 提取，经对抗核验）

> TS 移植权威。2026-06-12 4 路并行提取+对抗核验工作流产出。
> 关键提醒：① 实际只有 3 个 Weaviate collection（CodeEntity/TopologicalInterpretation/PatternInterpretation，无独立 business）；② recall_threshold=0.45；③ 门控处 score 缺省 1.0、should_rerank 缺省 0.0（有意不同，勿统一）；④ recall_score = rerank 前原始 cosine top1。



---

# 【Weaviate 读取层 + Collection Schema + Adapters】

I have enough to finalize. Key finding: there is no separate `business` Weaviate collection/store — only 3 stores exist (CodeEntity, TopologicalInterpretation, PatternInterpretation). The task description's "4 collection (CodeEntity/TopologicalInterpretation/business/pattern)" is the orchestrator's loose framing; the spec correctly documented 3. Let me note that. Let me also verify one subtle claim: spec §2.1 says CodeEntity `name` truncation logic is `entity_id.split("/")[-1]` — confirmed in `_name_from_id`. And the `add_many` uuid uses `eid + str(i)` which the spec omits. These are write-path details; the task scope is the read layer, so I'll note write-path UUID quirks as a minor addendum.

Now I'll produce the corrected complete spec.

---

## 修正清单

逐条回源核验后，原 spec 整体高度准确（魔法数字、Protocol 签名、collection schema、查询构造均逐字命中）。发现以下问题：

1. **【范围澄清，非错误】** 任务描述提到"4 个 collection（CodeEntity/TopologicalInterpretation/business/pattern）"，但回源确认主仓**只有 3 个 Weaviate collection/store**：`CodeEntity`、`TopologicalInterpretation`、`PatternInterpretation`。**不存在独立的 `business` Weaviate collection**。代码中出现的 `BusinessInterpretationStoreAdapter`（`src/knowledge/interpretation_store_adapter.py`）是**写入侧适配器**，落库目标仍是 `TopologicalInterpretation`，不是独立 collection。原 spec 正确地只写了 3 个 collection——予以确认，不修正。

2. **【遗漏补充】** `WeaviateVectorStore.add_many` 的 UUID 生成与 `add` 不同：用 `self._to_uuid(eid + str(i))`（带 batch 序号 `i`），而非 `add` 的 `self._to_uuid(entity_id)`。原 spec §2.1 只写了 `add` 路径的 UUID 公式。已补入怪癖区。

3. **【遗漏补充】** `WeaviateTopologicalInterpretStore` 另有 `get_by_method_id`（无 tenant 版，走默认分区）和 `search_by_text`（无 tenant，`top_k=10` 默认，`return_properties` 为 `[method_entity_id/method_name/interpretation_text/signature]`）两个公开方法，原 spec 未列。已补入 §3.4 附近。

4. **【精度修正】** 原 spec §2.2 称 `TopologicalInterpretation` 的 `interpretation_text` 截断上限 **48000**——回源确认 `[:48000]`，正确。但 `related_entity_ids_json` 写入用 `related_entity_ids_json[:8000]`（**直接下标，未做 `or ""` 兜底**，若传 None 会抛 → 由上层 try/except 吞），属潜在边界，已注明。

5. **【精度修正】** 原 spec §2.1 称 CodeEntity `dimension` 代码层 default 为 `64`——回源确认 `WeaviateVectorStore.__init__` 与 `WeaviateTopologicalInterpretStore.__init__` 默认 `dimension: int = 64`；但 `WeaviatePatternInterpretStore.__init__` 默认 `dimension: int = 1024`（**三个 store 代码层 default 不一致**）。原 spec 未点出 Pattern store 的 default 是 1024。已修正。

6. **【精度修正】** `WeaviatePatternInterpretStore.__init__` 的参数是 **keyword-only**（签名首位为 `*`），而 CodeEntity/Topological 的 `__init__` 是位置参数。已注明。

7. 其余抽查点（连接配置、MT 创建参数、near_vector 构造、return_properties 精确列表、adapters 的 Cypher、CompositeKnowledgeStore 三段逻辑、env 默认值 4/0.05/0.05、retriever 全部魔法数字、Protocol 签名）**全部逐字命中，无修正**。

---

# Weaviate 读取层 + Collection Schema + Adapters 规范（修正版）

## 1. 连接配置

### 代码层默认值（`src/core/weaviate_defaults.py`）

| 常量 | 值 |
|---|---|
| `DEFAULT_WEAVIATE_HTTP_URL` | `"http://localhost:8080"` |
| `DEFAULT_WEAVIATE_GRPC_PORT` | `50051` |
| `DEFAULT_COLLECTION_CODE_ENTITY` | `"CodeEntity"` |
| `DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION` | `"TopologicalInterpretation"` |
| `DEFAULT_COLLECTION_PATTERN_INTERPRETATION` | `"PatternInterpretation"` |

### project.yaml 真实运行时配置（`config/project.yaml` → `knowledge:` 段，行 152-182）

```yaml
knowledge:
  semantic_embedding:
    backend: dashscope
    model: text-embedding-v4          # 1024 维；API key 走 env DASHSCOPE_API_KEY，不入 git
  graph:
    backend: neo4j
    neo4j_uri: "bolt://localhost:7687"
    neo4j_user: "neo4j"
    neo4j_password: "12345678"
    neo4j_database: "neo4j"
  vectordb-code:
    enabled: true
    backend: weaviate                 # 注释标注 Weaviate 1.24 单机 + client 4.6.7
    dimension: 1024
    weaviate_url: "http://localhost:8080"
    weaviate_grpc_port: 50051
    weaviate_api_key: "user-a-key"
    collection_name: "CodeEntity"
  vectordb-interpret:
    enabled: true
    backend: weaviate
    dimension: 1024
    weaviate_url: "http://localhost:8080"
    weaviate_grpc_port: 50051
    weaviate_api_key: "user-a-key"
    collection_name: "TopologicalInterpretation"
```

> 注：YAML 中无 `vectordb-pattern` 段。`PatternInterpretation` 仅由代码默认值驱动（dimension 1024，见 §2.3）。

### `VectorDBConfig` Pydantic 字段（`src/config/models.py:143-153`，逐字段）

```python
enabled: bool = True
backend: str = "weaviate"
dimension: int = 1024
weaviate_url: str = DEFAULT_WEAVIATE_HTTP_URL          # "http://localhost:8080"
weaviate_grpc_port: int = DEFAULT_WEAVIATE_GRPC_PORT   # 50051
weaviate_api_key: Optional[str] = None
collection_name: str = DEFAULT_COLLECTION_CODE_ENTITY  # "CodeEntity"
allow_fallback_to_memory: bool = False
```

ProjectConfig 中两个 VectorDBConfig 实例由 `default_factory` 构造，分别注入 `collection_name=DEFAULT_COLLECTION_CODE_ENTITY` 与 `=DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION`。

### Weaviate 客户端初始化（`BaseWeaviateStore._ensure_client_and_schema`，`src/knowledge/base_weaviate_store.py`）

- 调用 `weaviate.connect_to_custom(**conn_kw)`。
- `conn_kw` 字段：`http_host` / `http_port` / `http_secure` 由 `_parse_url(url)` 解析；`grpc_host` **= http_host**（同主机）；`grpc_port` 独立配置；`grpc_secure` = `http_secure`（同 secure 标志）。
- `_parse_url`：`https://` 前缀 → secure=True；无端口时默认 `443`（secure）/ `8080`（非 secure）。
- `skip_init_checks=True`（v2.0 staging：跳过 gRPC ping init check，启动期不稳定）。
- `api_key` 存在时 `conn_kw["auth_credentials"] = Auth.api_key(api_key)`（import 失败静默跳过）。
- 代理环境变量 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/http_proxy/https_proxy/all_proxy` 全部 `os.environ.pop(..., None)` 清除；`NO_PROXY` 追加 `,localhost,127.0.0.1`。
- collection 不存在时才 create（`client.collections.exists(...)` 守卫）；建表用 `_schema_properties()` 子类提供的属性清单。

### 向量索引 & Multi-Tenancy 创建参数（建表时，所有 collection 统一）

```python
# 向量索引：HNSW + COSINE 距离；self-provided vector（不用内置 vectorizer）
Configure.VectorIndex.hnsw(distance_metric=VectorDistances.COSINE)  # 异常回退 distance_metric="cosine"

# Multi-Tenancy（所有 collection 建表默认开启）
Configure.multi_tenancy(
    enabled=True,
    auto_tenant_creation=True,
    auto_tenant_activation=True,
)
```

- 向量配置走 SDK 版本分支：v4.10+ 用 `Configure.Vectors.self_provided(vector_index_config=vec_index)`；v4.4-4.9 用 `vectorizer_config=Configure.Vectorizer.none() + vector_index_config=vec_index`；再失败用 inspect 探测 `vector_size`/`dimensions` 参数兜底（此兜底分支会把 `self._dim` 注入向量维度）。

---

## 2. Collection Schema

### 2.1 `CodeEntity`

**Store 类**：`WeaviateVectorStore`（`src/knowledge/vector_store_weaviate.py`）
**默认 collection_name**：`"CodeEntity"`
**向量维度**：`1024`（project.yaml 运行时）；**代码层 `__init__` default `dimension=64`**（仅单测用）。`__init__` 参数为位置参数 `(url, grpc_port, collection_name, dimension, api_key)`。

| 属性名 | DataType | 说明 |
|---|---|---|
| `entity_id` | `TEXT` | 方法实体 ID，与 Neo4j 方法节点一一对应（主键语义） |
| `name` | `TEXT` | 实体简名，写入时截断 100 字符 |
| `entity_type` | `TEXT` | 实体类型（如 `"method"`，可为空串） |
| `code_snippet` | `TEXT` | 真实方法源码片段 |

**`name` 生成（`_name_from_id`）**：`(entity_id.split("/")[-1] if "/" in entity_id else entity_id)[:100]`（含 `/` 才取末段，否则用全 id，再截 100）。`add` 时优先用传入 `name`，再走 `(name or _name_from_id(entity_id))[:100]`。

**UUID 生成（`BaseWeaviateStore._to_uuid`）**：`sha256(s.encode("utf-8")).hexdigest()[:32]`，拼成 `8-4-4-4-12` UUID 格式。
- `add` 用 `s = entity_id`。
- **`add_many` 用 `s = eid + str(i)`（带 batch 序号 i）—— 与 `add` 不同**，批量写时每条 UUID 含循环下标。
- `delete` 用 `s = entity_id`（与 add 镜像）。

**Tenant 用法**：`add`/`add_many`/`search_by_vector`/`get_by_entity_id`/`delete` 均在 `tenant` 非空时 `coll.with_tenant(tenant)`（= project_id）；`tenant=None` 走无分区路径（向后兼容旧调用，对 MT collection 会取不到，Weaviate 报 "multi-tenancy enabled, but request was without tenant"）。

---

### 2.2 `TopologicalInterpretation`

**Store 类**：`WeaviateTopologicalInterpretStore`（`src/knowledge/weaviate_interpretation_store.py`）
**默认 collection_name**：`"TopologicalInterpretation"`
**向量维度**：`1024`（运行时）；代码层 `__init__` default `dimension=64`（位置参数）。

| 属性名 | DataType | 写入截断 | 说明 |
|---|---|---|---|
| `method_entity_id` | `TEXT` | 无截断 | 主键，方法持久 ID（`qualified_name#params` 格式）；召回 `return_properties` 主字段 |
| `class_entity_id` | `TEXT` | 无截断（`or ""`） | 所属类实体 ID |
| `class_name` | `TEXT` | `[:500]` | 所属类简名 |
| `method_name` | `TEXT` | `[:300]` | 方法简名 |
| `signature` | `TEXT` | `[:2000]` | 方法完整签名 |
| `interpretation_text` | `TEXT` | `[:48000]` | 业务解读正文（LLM 生成中文）；召回主文本字段 |
| `context_summary` | `TEXT` | `[:12000]` | 上下文摘要；`interpretation_text` 空时回退 |
| `language` | `TEXT` | 无截断（`or "zh"`） | 语言代码（默认 `"zh"`） |
| `related_entity_ids_json` | `TEXT` | `[:8000]` | 关联实体 ID JSON 字符串；写入用裸下标 `related_entity_ids_json[:8000]`（**无 `or ""` 兜底**，None 会抛、被上层 try 吞），默认 `"{}"` |

**UUID 生成**：`_to_uuid(method_entity_id + "|interpret")`。

**写已存在的 upsert 逻辑**：`coll.data.insert(...)` 抛异常且 `"already exists" in str(e).lower() or "422" in str(e)` → `coll.data.replace(uuid=uid, ...)`；`add_with_created` 返回 `(success, created)`，replace 成功时 created=False。

**Tenant 用法（`_resolve_collection(tenant)`）**：
- `tenant` 非空 → `coll.with_tenant(tenant)`（v2.0 路径）。
- `tenant=None` → 普通 collection + **WARNING 日志**（deprecated，未来必填）。
- 写操作 `add` / `add_with_created` 通过 `_resolve_collection` 选分区。
- `get_by_entity_with_tenant`：v2.0 精确查询，`_get_collection().with_tenant(tenant)` + `Filter.by_property("method_entity_id").equal(mid_try)`，对 `method_entity_id_variants(entity_id)` 逐一试匹配，`limit=1`。`level` 参数预留（无层级过滤）。
- `get_by_method_id`（**无 tenant 版**）：走 `_get_collection()` 默认分区（不带 tenant，取不到 tenant 数据），同样对 variants 逐一试。

---

### 2.3 `PatternInterpretation`

**Store 类**：`WeaviatePatternInterpretStore`（`src/knowledge/weaviate_pattern_store.py`）
**默认 collection_name**：`"PatternInterpretation"`
**向量维度**：`1024`（**代码层 `__init__` default 即 `dimension=1024`，与另两个 store 的 64 不同**）。`__init__` 参数为 **keyword-only**（签名首位 `*`）。

| 属性名 | DataType | 写入截断 | 值域/说明 |
|---|---|---|---|
| `scope_type` | `TEXT` | 无截断 | `"system"` 或 `"module"`；写入前 `.strip().lower()`，空 → `"system"` |
| `target_id` | `TEXT` | 无截断 | scope=system 固定 `"system"`；scope=module 为 module_id；空 → `"system"` |
| `pattern_type` | `TEXT` | 无截断 | `"design"` 或 `"architecture"`；空 → `"design"` |
| `pattern_name` | `TEXT` | `[:200]` | 模式名称；空 → `"Unknown"` |
| `confidence` | `TEXT` | `[:20]` | `str(float(confidence))[:20]`（float 转字符串存，避免 DataType.NUMBER 版本差异） |
| `summary_text` | `TEXT` | `[:16000]` | 模式描述文本 |
| `evidence_json` | `TEXT` | `[:30000]` | 证据 JSON 字符串 |
| `language` | `TEXT` | 无截断（`or "zh"`） | 默认 `"zh"` |
| `related_entity_ids_json` | `TEXT` | `[:8000]` | 关联实体 ID JSON 数组字符串，默认 `"[]"` |

**UUID 生成**：`_to_uuid(f"{target_id}|{scope_type}|{pattern_type}|{pattern_name}|pattern")`（标准化后的字段值参与拼接）。

**Tenant 用法**：写操作（`add`/`add_with_created`）和读操作（`list_by_scope`/`list_existing_target_ids`）**全部直接 `_get_collection()`，不调 `with_tenant`**。PatternInterpretation 在 v2.0 中尚未启用 per-project tenant 分区（虽建表默认开了 MT config，但代码读写未绑 tenant）。

> 读取层补充：本 store 无 near_vector 检索方法，只有 `list_by_scope`（`Filter.by_property("scope_type").equal(...) & Filter.by_property("target_id").equal(...)`，`limit=200` 默认）和 `list_existing_target_ids`（分页 page_size=2000）。

---

## 3. `near_vector` 查询构造

### 3.1 通用辅助：`near_vector_property_hits`（`src/knowledge/weaviate_near_vector.py`）

**签名**：
```python
def near_vector_property_hits(
    coll: Any,                      # 已 with_tenant 或未绑定的 Collection
    *,
    vector: list[float],
    dim: int,
    limit: int,
    collection_name: str,           # 仅用于 GraphQL 兜底解析时的 key 查找
    return_properties: list[str],   # 必须显式指定，否则某些 SDK 返回空 objects
    filters: Any | None = None,     # weaviate.classes.query.Filter 或 None
) -> list[tuple[dict[str, Any], float]]:
    # 返回 [(properties_dict, score), ...]；score = 1.0 - float(distance)，缺失 → 0.0
```

**入口校验**：`if not vector or len(vector) < dim: return []`。
**向量裁剪**：`vec = vector[:dim]`。
**base_kw**：`dict(near_vector=vec, limit=int(limit), return_properties=return_properties, return_metadata=MetadataQuery(distance=True))`。

**调用与降级**：
```python
if filters is not None:
    try:
        q = coll.query.near_vector(**base_kw, filters=filters)
        result = q.do() if hasattr(q, "do") else q
    except TypeError as e:
        # WARNING：不再回退无 filter（避免 class/module 占满结果）
        return []
    except Exception as e:
        # WARNING：同样不回退
        return []
else:
    try:
        q = coll.query.near_vector(**base_kw)
        result = q.do() if hasattr(q, "do") else q
    except Exception as e:
        return []          # WARNING
if result is None:
    return []
```

**结果解析（多格式兼容，`_extract_objects` 顺序）**：
1. `result.objects`（属性）
2. `result` 本身是 list
3. `result["objects"]`（dict.objects）
4. `result["data"]["Get"][collection_name]`（或 `Data`/`get` 大小写变体；再不行取第一个 list 值，GraphQL 兜底）

`_extract_props`：`obj.properties` → `obj["properties"]`（dict）→ `obj`（扁平 dict）。
`_extract_distance`：`obj.metadata.distance` → `obj["metadata"]["distance"]` → `obj["distance"]`，均 `float(...)`。

**怪癖**：
- 旧版 SDK 返回"可执行查询对象"，需 `.do()`，靠 `hasattr(q, "do")` 检测。
- `return_properties` 不显式指定时某些 SDK 版本 `result.objects` 为空。
- `filters` 存在时 TypeError/任意异常都**不回退无 filter**。

---

### 3.2 `CodeEntity` 近邻查询（`WeaviateVectorStore.search_by_vector`）

> 注意：CodeEntity 的 near_vector **不走 `near_vector_property_hits`**，而是 store 内部自带的内联实现（含 `_last_search_error`/`_last_search_detail` 诊断字段）。

```python
def search_by_vector(query_vector, top_k=10, *, tenant=None) -> list[tuple[str, float]]:
```
调用链：
1. 复位 `_last_search_error = None`、`_last_search_detail = None`。
2. `if not query_vector or len(query_vector) < self._dim: return []`。
3. `coll = self._get_collection()`；`tenant` 非空 → `coll.with_tenant(tenant)`。
4. `coll.query.near_vector(near_vector=query_vector[:dim], limit=top_k, return_properties=["entity_id","name","entity_type","code_snippet"], return_metadata=MetadataQuery(distance=True))`。
5. `result = query.do() if hasattr(query, "do") else query`，内联 `_extract_objects/_extract_props/_extract_distance`（逻辑同 §3.1）。
6. 取 `props["entity_id"]`，`score = 1.0 - float(dist)`（dist 缺失 → 0.0），仅 `str(eid)` 非空才入列。
7. 异常 → `self._last_search_error = traceback.format_exc()`，返 `[]`。

**`search_by_text(query_text, top_k=10, *, tenant=None)`**：`vec = get_embedding(query_text, self._dim)` → `search_by_vector(vec, top_k, tenant=tenant)`。

**`get_by_entity_id(entity_id, *, tenant=None)`**（精确，非 near_vector）：
- `eid` strip，空 → None。
- `method://` / `method//` 前缀 → `method_entity_id_variants(eid)`（候选变体）；否则 `[eid]`。
- `coll.query.fetch_objects(filters=Filter.by_property("entity_id").equal(cand), limit=1)`，`tenant` 非空时先 `with_tenant`。
- 命中返回 `{entity_id, name, entity_type, code_snippet}`，否则 None；异常 → None。

---

### 3.3 `TopologicalInterpretation` 近邻查询（`WeaviateTopologicalAdapter.search_method_hits_by_text`，`adapters.py`）

```python
def search_method_hits_by_text(*, text: str, project_id: str, limit: int = 5) -> list[dict[str, Any]]:
    # 返回 [{"entity_id","summary_text","level","language","score"}, ...]
```

查询构造：
1. 边界：`if not (text or "").strip() or not project_id: return []`。
2. `vec = get_embedding(text.strip(), self._store._dim)`（DashScope text-embedding-v4，1024 维）。
3. `coll = self._store._get_collection().with_tenant(project_id)`（tenant-scoped 视图）。
4. `fetch_limit = max(int(limit) * 3, 10)`（过取系数 3×，下限 10）。
5. `near_vector_property_hits(coll, vector=vec, dim=self._store._dim, limit=fetch_limit, collection_name=self._store._collection_name, return_properties=[...], filters=None)`。
6. `return_properties` **精确 4 列**（2026-06-02 修，删幻影列）：
   - `"method_entity_id"`、`"interpretation_text"`、`"context_summary"`、`"language"`
   - 旧代码曾请求 `entity_type/level/business_domain/business_capabilities/summary_text/entity_id`，这些列**不存在** → Weaviate `no such prop` 异常 → 每次召回降级。
7. 任意异常（含 tenant 不存在）→ WARNING + 返 `[]`（不上抛）。

后处理：
- `rows.sort(key=lambda r: r[1], reverse=True)`（score 降序）。
- 截 `rows[:int(limit)]`。
- 字段映射（写死）：
  ```
  "entity_id"    ← props.get("method_entity_id", "")
  "summary_text" ← props.get("interpretation_text") or props.get("context_summary") or ""
  "level"        ← "method"（固定字符串）
  "language"     ← props.get("language", "")
  "score"        ← float(score)
  ```

**关键约定**：`level` 固定 `"method"`。下游 `build_user_prompt` 以 `level == "code_entity"` 判"拓扑解读缺失"，故 `"method"` 表示有效解读；误写 `"code_entity"` 会触发 LLM 降级路径。

> 另：`WeaviateTopologicalInterpretStore.search_by_text(query_text, top_k=10)`（store 自带、**无 tenant**）也走 `near_vector_property_hits`，但 `return_properties=["method_entity_id","method_name","interpretation_text","signature"]`，返回 `list[(method_entity_id, score)]`。adapter 路径**不使用**它。

---

### 3.4 `TopologicalInterpretation` 精确查询（`get_by_entity_with_tenant`）

```python
def get_by_entity_with_tenant(entity_id, *, tenant, level=None) -> Optional[dict]:
```
1. `if not entity_id or not self._client: return None`。
2. `coll = self._get_collection().with_tenant(tenant)`。
3. `for mid_try in method_entity_id_variants(entity_id)`：`fetch_objects(filters=Filter.by_property("method_entity_id").equal(mid_try), limit=1)`。
4. 命中返回 dict（含全部 9 字段）；无命中/异常 → None（静默）。

返回结构（字段顺序）：
```
{ "method_entity_id", "method_name", "signature", "interpretation_text",
  "class_entity_id", "class_name", "language", "context_summary",
  "related_entity_ids_json" }
```

---

## 4. Adapters（`src/service/qa_engine/adapters.py`）

### 4.1 `WeaviateTopologicalAdapter`

**实现 Protocol**：`InterpretationStoreProto`（`retriever.py`）。
**构造**：`__init__(self, store: WeaviateTopologicalInterpretStore) -> None`，存 `self._store`。

**`get_by_entity(entity_id, *, project_id, level=None) -> dict | None`**：
1. 优先 `self._store.get_by_entity_with_tenant(entity_id, tenant=project_id, level=level)`。
2. `except AttributeError`（主仓未实现）→ DEBUG + fallback `self._store.get_by_entity(entity_id, level=level)`（注：主仓 store 实际**无 `get_by_entity` 方法**，故此 fallback 会再抛 AttributeError → 被内层 `except Exception` → WARNING + None）。
3. `except Exception` → WARNING + None（不上抛）。

**`search_method_hits_by_text`**：见 §3.3。

### 4.2 `Neo4jGraphAdapter`

**实现 Protocol**：`GraphProto`（`retriever.py`）。
**构造**：`__init__(self, backend: Neo4jGraphBackend, project_id: str)`；`if not project_id: raise TypeError("project_id is required for Neo4jGraphAdapter (v2.0)")`。

**未实现 `module_of`**：`GraphProto` 要求 `module_of(entity_id) -> str | None`，但此 adapter 未实现（由其他适配器/上层覆盖）。

**`successors(entity_id, rel_type=None) -> list[str]`** Cypher：
```cypher
MATCH (a:Entity {id: $eid, project_id: $pid})-[r]->(b:Entity)
WHERE b.project_id = $pid
  AND ($rel = '' OR type(r) = $rel)
RETURN b.id AS nid
```
- 参数 `eid=entity_id, pid=self._project_id, rel=rel_type or ""`（None → `""` 对应 `$rel = ''` 不过滤）。
- 异常 → `[]`（DEBUG，不上抛）。

**`predecessors`**：方向 `<-[r]-`（找上游调用方），其余完全对称。

**`close`**：`self._backend.close()`，失败静默。

---

## 5. `InterpretationStoreProto` 与 `GraphProto`（`src/service/qa_engine/retriever.py:59-82`）

```python
class InterpretationStoreProto(Protocol):
    def search_method_hits_by_text(
        self, *, text: str, project_id: str, limit: int = 5
    ) -> list[dict[str, Any]]: ...
    def get_by_entity(
        self, entity_id: str, level: str | None = None
    ) -> dict[str, Any] | None: ...

class GraphProto(Protocol):
    def successors(self, entity_id: str, rel_type: str | None = None) -> list[str]: ...
    def predecessors(self, entity_id: str, rel_type: str | None = None) -> list[str]: ...
    def module_of(self, entity_id: str) -> str | None: ...
```

> 注：`InterpretationStoreProto.get_by_entity` 签名**不含** `project_id`（只有 `entity_id, level`）。而 `WeaviateTopologicalAdapter.get_by_entity` 与 `CompositeKnowledgeStore.get_by_entity` 的实现签名**带** `project_id`（前者 keyword-only 必填，后者内部用 `self._project_id`）。Composite 用 `try TypeError` 兼容两种 signature（先试带 `project_id=` 的现代签名，TypeError 回退到无 project_id）。这是已知的 Protocol-与-实现签名分歧，TS 侧需保留这种"按需注入 project_id"的桥接。

---

## 6. `CompositeKnowledgeStore`（`src/knowledge/composite_knowledge_store.py`）

双源兜底，实现 `InterpretationStoreProto`，被 QARetriever 直接消费。

**构造**：
```python
CompositeKnowledgeStore(*, interpretation_store, code_store: Optional[...], project_id: str)
# 全 keyword-only
```

**`search_method_hits_by_text(*, text, project_id, limit=5)` 三段逻辑**：
1. 调 `self._interpretation_store.search_method_hits_by_text(text=text, project_id=project_id, limit=limit)`；非空 → 直接返。
2. 异常分流：`_is_tenant_missing(exc)`（substring 匹配 `"tenant not found"` / `"TenantNotFoundError"`，**大小写无关**，`str(exc).lower()` 比对各 marker 的 `.lower()`）→ INFO；其他异常 → WARNING（含 `type(exc).__name__`）。两者都走 `_code_fallback`。
3. 解读库返空（非异常）→ DEBUG + `_code_fallback`。

**CodeEntity 兜底（`_code_fallback(*, text, limit)`）**：
- `code_store is None` → DEBUG + `[]`。
- env 读取（`_num_env`，坏值/缺失回退默认）：
  - `overfetch = max(1, int(_num_env("KE_QA_RECALL_OVERFETCH", 4)))` → 默认 **4**，下限 1。
  - `boost = _num_env("KE_QA_RECALL_BOOST", 0.05)` → 默认 **0.05**。
  - `demote = _num_env("KE_QA_RECALL_DEMOTE", 0.05)` → 默认 **0.05**。
- `hits = self._code_store.search_by_text(text, top_k=limit * overfetch, tenant=self._project_id)`；异常 → WARNING + `[]`。
- `hits = rerank_and_filter(hits, limit, boost=boost, demote=demote)`（降噪重排 + 截 limit；返回的 score 仍是原始 cosine，门控诚实）。
- 归一化 + dedup（`seen` set 保首次出现序），每项：
  ```
  {"entity_id": eid, "summary_text": "", "level": "code_entity", "score": score}
  ```

**`get_by_entity(entity_id, level=None)`**：仅代理解读库，无 CodeEntity 兜底；先试带 `project_id=self._project_id` 的现代签名，`TypeError` → 回退无 project_id；异常分流 `_log_get_by_entity_exc`（tenant missing → DEBUG，否则 WARNING），返 None。

**`get_code_snippet(entity_id)`（source-first grounding P1）**：
- `_code_store is None` → None。
- `getter = getattr(store, "get_by_entity_id", None)`，`not callable(getter)` → None（鸭子类型探测，proto 未声明）。
- `record = getter(entity_id, tenant=self._project_id)`；异常（含旧签名 TypeError）→ None。
- 返回 `record.get("code_snippet")`（空串/None 归一为 None）。

---

## 7. QARetriever 召回魔法数字（`src/service/qa_engine/retriever.py`，逐字核验全部命中）

| 常量名 | 值 | 语义 |
|---|---|---|
| `TOP_N_FOR_CHAIN_EXPANSION` | `3` | 对 top-3 候选取调用链 |
| `MAX_CALLEES` | `5` | 每候选向下 1 跳最多 5 个 callee（`direction="down", max_depth=1`） |
| `MAX_CALLERS` | `5` | 每候选向上 1 跳最多 5 个 caller（`direction="up", max_depth=1`） |
| `CHAIN_DEPTH` | `2` | 调用链多跳展开深度 |
| `MAX_CHAIN_EDGES` | `25` | 调用边总数上限 |
| `_TOP_K_FOR_SNIPPET` | `3` | 源码预读候选数（P1 grounding） |
| `recall_threshold` | `0.45`（`__init__` 默认，可覆盖） | top1 score < 阈值 → chit-chat（空 ctx，不查图）；≥ → architecture（1 跳上下游） |
| `_SNIPPET_MAX_LINES` | `300` | 方法源码行数截断上限 |
| `_SNIPPET_MAX_CHARS` | `8000` | 方法源码字符截断上限（兜底防超长单行） |
| callchain summary token cap | `text[:120]` 字符/条 | `ctx.callchain_node_summaries[mid] = text[:120]` |
| fetch_limit（§3.3） | `max(limit*3, 10)` | TopologicalInterpretation over-fetch |
| `KE_QA_RECALL_OVERFETCH` | `4`（下限 1） | CodeEntity 兜底 over-fetch 倍数 |
| `KE_QA_RECALL_BOOST` | `0.05` | 业务实体加权 |
| `KE_QA_RECALL_DEMOTE` | `0.05` | 低价值实体降权 |

**`#` 剥参回退（retriever.py:268-269）**：富集调用链节点解读时，`if not rec and "#" in mid: rec = self.interpretation_store.get_by_entity(mid.split("#", 1)[0])`（带参查不到 → 剥 `#` 后再查无参形态）。

---

## 8. 嵌入模型配置

| 配置项 | project.yaml 值 |
|---|---|
| `knowledge.semantic_embedding.backend` | `dashscope` |
| `knowledge.semantic_embedding.model` | `text-embedding-v4` |
| 向量维度 | `1024`（与 `vectordb.dimension` 一致） |
| API Key | env `DASHSCOPE_API_KEY`（不入 git） |

`get_embedding(text, dim)`（`src/semantic/embedding.py`）是全局入口，`dim` 控制输出维度，调用方一律传 `self._dim`。

---

## 9. 降级与边界行为

| 场景 | 行为 |
|---|---|
| `query_vector` 空或 `len < dim` | `search_by_vector` / `near_vector_property_hits` 返 `[]` |
| `text` 或 `project_id` 为空 | `search_method_hits_by_text`（adapter）返 `[]` |
| near_vector + filters TypeError/任意异常 | **不回退无 filter**，直接 `[]`（WARNING） |
| `do()` 兼容层 | `hasattr(q, "do")` 检测旧版 SDK"查询对象" |
| `return_properties` 缺失 | 某些 SDK `objects` 为空 → 强制显式指定 |
| tenant 不存在（substring `"tenant not found"`/`"TenantNotFoundError"`，大小写无关） | Composite → CodeEntity 兜底（search INFO / get_by_entity DEBUG） |
| `get_by_entity` 主仓无 `get_by_entity_with_tenant` | AttributeError → fallback `get_by_entity(entity_id, level=...)`（主仓亦无 → 再 except → None） |
| `code_store=None` | `_code_fallback` 返 `[]` → 召回门控 < threshold → chit-chat |
| Neo4j Cypher 失败 | `successors`/`predecessors` 返 `[]`（DEBUG，不上抛） |
| entity_id 含 `#` 且解读查不到 | `mid.split("#", 1)[0]` 剥参再试 |
| `PatternInterpretation` tenant | 读写均不绑 tenant（v2.0 未迁移；建表 MT config 已开但代码未用） |
| CodeEntity `add_many` UUID | 用 `eid + str(i)`（含 batch 序号），与单条 `add` 的 `entity_id` 不同 |
| 三个 store 代码层 default dimension | CodeEntity/Topological = `64`；Pattern = `1024`（运行时全部 1024，64 仅单测） |
| 写 store 的 upsert | insert 抛 `"already exists"`/`"422"` → `data.replace(uuid, ...)`（Topological/Pattern；CodeEntity 的 `add` 直接 except pass 不 replace） |

---

**相关文件（绝对路径）**：
- `/Users/java/knowledge-engineering/src/knowledge/vector_store_weaviate.py`
- `/Users/java/knowledge-engineering/src/knowledge/weaviate_near_vector.py`
- `/Users/java/knowledge-engineering/src/knowledge/weaviate_interpretation_store.py`
- `/Users/java/knowledge-engineering/src/knowledge/weaviate_pattern_store.py`
- `/Users/java/knowledge-engineering/src/knowledge/base_weaviate_store.py`
- `/Users/java/knowledge-engineering/src/knowledge/composite_knowledge_store.py`
- `/Users/java/knowledge-engineering/src/service/qa_engine/adapters.py`
- `/Users/java/knowledge-engineering/src/service/qa_engine/retriever.py`
- `/Users/java/knowledge-engineering/src/core/weaviate_defaults.py`
- `/Users/java/knowledge-engineering/src/config/models.py`
- `/Users/java/knowledge-engineering/config/project.yaml`（knowledge 段，行 152-182）

---

# 【query 预处理 + retriever 主流程 + 门控路由】

I have everything I need. The source matches the spec on nearly all points. Let me verify the few items where the spec's wording could diverge from the code.

Key cross-checks done:
- `_bfs_edges` noise filter: code calls `is_callchain_noise(child)` and `continue` **before** appending edge or counting toward `max_edges` — spec correct.
- `_extract_table_access` exists (spec routes `table_access_by_entry` through `_extract_table_access`, which internally calls `graph.successors(entity_id, rel_type="accesses_table")`) — spec describes the underlying call but skips the wrapper name (minor).
- `route_async` docstring at line 134-137 says "5 skill" / "兜底到 chit-chat" but the actual code (lines 156, 165) bottoms out to `architecture`. The spec correctly captured the **code behavior**, not the stale docstring.

Below is the corrected full spec.

---

## 修正清单

经逐条回源核验，本规范**整体高度准确**，绝大多数魔法数字、查询构造、降级条件、字段集均逐字命中源码。发现的偏差/需补强项：

1. **【中等】`table_access_by_entry` 漏掉 `_extract_table_access` 包装方法**：spec 在 retrieve 步骤 d 写 `table_access_by_entry：graph.successors(entity_id, rel_type="accesses_table")`，但实际 retrieve 调的是 `self._extract_table_access(entity_id)`（retriever.py:245），该私有方法内部才调 `successors(..., rel_type="accesses_table")` 并把每条边组装成 `{"table_id": table_id, "operation": "unknown"}`。TS 移植需注意有一层包装方法（含 try/except 兜底 `[]`）。已在下文修正。

2. **【低/澄清】`router.route_async` 的 docstring 与代码行为不一致（spec 取对了行为）**：router.py:134-137 的 docstring 仍写「在 **5** 个 skill 里选 1 个」「异常 / 不合法返回都兜底到 **chit-chat**」，但实际代码（router.py:156 `llm-error`、165 `llm-fallback`）兜底均为 `architecture`。spec 正确地以**代码行为**为权威（兜底 architecture），docstring 是 v1.2.1 回退后未清理的陈旧注释。TS 实现以 `architecture` 为准，**勿照搬 docstring**。已在「怪癖」补注。

3. **【低/补强】`_VALID_SKILL_IDS` 含 5 个值，LLM system prompt 也列 5 个 skill**：spec 已正确写出 frozenset 5 元素，但需强调 LLM 候选集（含 `chit-chat`）与关键词词典是两套——LLM 可返回 `chit-chat`（合法），而关键词路径下 `chit-chat` 也能命中。spec 无误，仅补一句澄清。

4. **【抽查确认无误】** 以下逐条回源命中，无修正：
   - `recall_threshold=0.45`、`top_k=5`、`TOP_N_FOR_CHAIN_EXPANSION=3`、`MAX_CALLEES=5`、`MAX_CALLERS=5`、`CHAIN_DEPTH=2`、`MAX_CHAIN_EDGES=25`、`_TOP_K_FOR_SNIPPET=3`、`_SNIPPET_MAX_LINES=300`、`_SNIPPET_MAX_CHARS=8000`、`_MIN_CLEANED_LEN=2`、`120` 字截断 —— 全部逐字命中。
   - `top1 = max((c.get("score", 1.0) for c in candidates), default=0.0)` —— score 缺省 1.0、空候选 0.0，逐字命中（retriever.py:190）。
   - `is_callchain_noise(child)` 在 append 边 + 计 `max_edges` **之前** `continue`（retriever.py:456-459）—— spec「不消耗 edge 预算」正确。
   - `is_callchain_noise(entity_id: str)` 单参（recall_rerank.py:116）、`should_rerank(candidates)`、`rerank_candidates(question, candidates)`（semantic_rerank.py:32/97）—— 签名命中。
   - `#` 剥参重试用 `mid.split("#", 1)[0]`（retriever.py:269）、解读字段优先级 `summary_text → interpretation_text → context_summary`（retriever.py:276-281）—— 命中。
   - `_RESIDUE_ONLY` 11 元素集合、`_PUNCT_WS` 正则、回退判定 `len(cleaned) < 2 or cleaned in _RESIDUE_ONLY` —— 逐字命中。
   - `_RENDER_PATTERNS` 10 条、`_FILLER_PATTERNS` 9 条 —— 数量命中（注：spec 表述「10 条」「9 条」准确）。
   - 关键词词典顺序 `dependency → data-flow → business → chit-chat` + architecture 兜底、子串 `kw in question` 非正则 —— 命中。

---

# Query Preprocess + Retriever + Router — 行为规范提取（已核验修正）

---

## 一、`query_preprocess.py` — 召回 Query 预处理

### 组件用途

仅用于召回时的向量化 query 净化。将用户原始问题中与"找哪段代码"无关的渲染指令和口水填充词剥离，提纯代码语义信噪比。**门控判定、展示给用户、喂给 LLM 作答均使用原始 question，不经过此函数。**

### 公开函数签名

```
clean_recall_query(text: str) -> str
```

- **入参** `text`：用户原始问题字符串
- **出参**：提纯后 query；若清理后为空或过短，回退原始问题（去首尾空白）

### 核心算法步骤

1. 空/纯空白输入守卫：`if not text or not text.strip(): return text or ""`（保证返回必为 `str`，不为 `None`）
2. 按顺序逐一将 `_COMPILED`（`_RENDER_PATTERNS` + `_FILLER_PATTERNS` 合并列表，模块加载时预编译）中的每个 Pattern 匹配到的片段 `re.sub(" ", ...)` 替换为单空格（用空格而非空串，避免相邻词粘连）
3. 将 `_PUNCT_WS`（`[，。、！？；：,.!?;:\s]+`）折叠为单空格，`.strip()` 去首尾空白
4. 回退判定：若 `len(cleaned) < 2`（`_MIN_CLEANED_LEN = 2`）或 `cleaned in _RESIDUE_ONLY`，则回退 `text.strip()`
5. 返回 `cleaned`

### 所有魔法数字与常量

| 常量 | 值 | 含义 |
|------|-----|------|
| `_MIN_CLEANED_LEN` | `2` | 提纯结果最短可接受长度（字符数），小于此值回退原文 |
| `_RESIDUE_ONLY` | `frozenset({"画","画个","画出","画出来","画一下","画一张","绘制","展示","图","看","看看"})`（11 元素） | 提纯后只剩这些孤立渲染残词时，判定原文几乎没有代码语义，回退原文；用 `frozenset` 做 O(1) 查表 |
| `_PUNCT_WS` | `re.compile(r"[，。、！？；：,.!?;:\s]+")` | 中英文标点 + 连续空白折叠为单空格 |

### 正则模式组

**渲染指令模式（`_RENDER_PATTERNS`，10 条）**：逐字如下（TS 移植需逐条等价）
```
r"用?流程图(来)?(展示|表示|画出来?|画一下)?"
r"用?时序图(来)?(展示|表示)?"
r"用?架构图(来)?(展示|表示)?"
r"用?类图"
r"用\s*(UML|PlantUML|Mermaid|ReactFlow)\s*图?(来)?(展示|画)?"
r"画(一下|一张|个|出)?(流程图|时序图|架构图|类图|图)"
r"可视化(地)?(展示|表示)?"
r"图形化(地)?(展示)?"
r"展示(一下|出来)?"
r"给我(看一?看|画|展示)"
```

**口水填充模式（`_FILLER_PATTERNS`，9 条）**：逐字如下
```
r"帮我"  r"请"  r"麻烦"
r"我想(知道|了解|看看?)"  r"想知道"
r"是怎么(实现|做)的"  r"怎么(实现|做)的"  r"是如何(实现|工作)的"
r"一下"  r"呢"  r"吧"  r"啊"  r"嘛"
```
> 注：`_FILLER_PATTERNS` 列表内含 13 个正则字符串，但语义上分 9 类（"语气助词"那行 `一下/呢/吧/啊/嘛` 是 5 个独立 pattern）。`_COMPILED` 总编译数 = 10 + 13 = 23 个 Pattern。TS 实现应按**全部 23 个正则**逐一替换，而非按"9 条"。

预编译：`_COMPILED = [re.compile(p) for p in (_RENDER_PATTERNS + _FILLER_PATTERNS)]`（模块加载时一次）。

---

## 二、`retriever.py` — 召回主流程

### 组件用途

从 Weaviate（语义召回）+ CodeGraph（拓扑）取候选实体和调用链，组装 `RetrievedContext` 交给 synthesizer。本仓只用 `Protocol` 定义接口，运行时由 `api.py` 注入主仓实例。

### `RetrievedContext` 所有字段（`@dataclass`）

| 字段名 | 类型 | 默认值 | 含义 |
|--------|------|--------|------|
| `question` | `str` | 必填 | 用户原始问题（不经过预处理） |
| `project_id` | `str` | 必填 | 项目租户 ID |
| `entry_candidates` | `list[dict[str, Any]]` | `field(default_factory=list)` | 语义召回命中列表（BusinessInterpretation 优先，空/异常兜底 CodeEntity），每条 dict 含 `entity_id / summary_text / level / score`；`module` 由 retriever 就地注入 |
| `callees_by_entry` | `dict[str, list[str]]` | `{}` | `{entity_id: [下游 method id, ...]}` —— 仅 top-3 候选，1 跳 BFS，最多 5 个 |
| `callers_by_entry` | `dict[str, list[str]]` | `{}` | `{entity_id: [上游 caller id, ...]}` —— 仅 top-3 候选，1 跳 BFS，最多 5 个 |
| `call_edges_by_entry` | `dict[str, list[tuple[str, str]]]` | `{}` | `{entity_id: [(from_id, to_id), ...]}` —— 入口向下多跳（depth=2）BFS 调用边，保留层级关系，供 LLM 画调用图；经 `is_callchain_noise` 过滤噪声 |
| `table_access_by_entry` | `dict[str, list[dict]]` | `{}` | `{entity_id: [{table_id, operation}, ...]}` —— 经 `_extract_table_access` 取图上 `accesses_table` 边；`operation` 固定为 `"unknown"` |
| `skill_id` | `str` | `"architecture"` | 召回门控结果：`"architecture"`（过线）或 `"chit-chat"`（未过线） |
| `recall_score` | `float` | `0.0` | top1 cosine 相似度（门控裁决前记录，不受 rerank 影响） |
| `callchain_node_summaries` | `dict[str, str]` | `{}` | `{entity_id: 2b中文业务解读(截断至120字)}` —— call_edges_by_entry 所有边端点（去重）的中文解读，供 LLM 写准确标签；entity_id 带参数签名时剥参（`#` 前缀剥离）再查 |
| `candidate_code_snippets` | `dict[str, str]` | `{}` | `{entity_id: 真实方法源码}` —— top-3 候选预读全文，超限截断（source-first grounding P1） |
| `candidate_tree` | `CandidateTree \| None` | `None` | 候选按调用顺序组装的树形结构；异常或 chit-chat 时为 `None`，prompt 端见 `None` 走旧扁平分支（字符串注解 `"CandidateTree | None"` 防循环导入） |

### 公开函数签名

#### `QARetriever.__init__`

```
QARetriever(
    *,
    interpretation_store: InterpretationStoreProto,
    graph: GraphProto,
    recall_threshold: float = 0.45,
)
```

- `interpretation_store`：Weaviate 语义检索 + 精确查询接口（`search_method_hits_by_text` / `get_by_entity` / 可选 `get_code_snippet`）
- `graph`：CodeGraph 图导航（`successors` / `predecessors` / `module_of`）
- `recall_threshold`：召回门控阈值，**默认 `0.45`**

#### `QARetriever.retrieve`（异步主入口）

```
async retrieve(
    *,
    question: str,
    project_id: str,
    top_k: int = 5,
) -> RetrievedContext
```

- `top_k` 默认 `5`，传入 `search_method_hits_by_text(limit=top_k)`

### 核心算法步骤（`retrieve` 内）

1. **query 预处理**：`recall_text = clean_recall_query(question)`，向量化用提纯 query，门控/ctx.question/作答用原始 question
2. **语义召回**：`interpretation_store.search_method_hits_by_text(text=recall_text, project_id=project_id, limit=top_k)`
3. **门控判定**：`top1 = max((c.get("score", 1.0) for c in candidates), default=0.0)`；解读库命中无 `score` 字段时视为 `1.0`（强信号），空候选列表时 `top1=0.0`
4. **未过线**（`top1 < recall_threshold`）：返回 `RetrievedContext(question=question, project_id=project_id, skill_id="chit-chat", recall_score=top1)`，不查图
5. **过线**：构建 `RetrievedContext(skill_id="architecture", recall_score=top1)`，继续以下步骤：
   a. **二次语义重排**：`should_rerank(candidates)` 为真时调 `rerank_candidates(question, candidates)`（用原始 question，非 recall_text；内部 best-effort，失败回退原序）；`recall_score` 已用 cosine top1 固定，不受 rerank 影响。重排后赋 `ctx.entry_candidates = candidates`
   b. **module 标注**：遍历 `ctx.entry_candidates`，就地写入 `c["module"] = graph.module_of(eid) if eid else None`，try/except 包裹，异常或 eid 为空/None 时写 `None`
   c. **源码预读**（P1 grounding）：`_enrich_candidate_snippets(ctx)` —— 见下方专述
   d. **调用链展开**：遍历 `candidates[:TOP_N_FOR_CHAIN_EXPANSION]`（=3），跳过无 `entity_id` 的候选：
      - `callees_by_entry[eid]`：`_bfs_chain(eid, direction="down", max_depth=1, max_nodes=MAX_CALLEES=5)`
      - `callers_by_entry[eid]`：`_bfs_chain(eid, direction="up", max_depth=1, max_nodes=MAX_CALLERS=5)`
      - `table_access_by_entry[eid]`：**`_extract_table_access(eid)`**（内部 `graph.successors(eid, rel_type="accesses_table")`，每条边 → `{"table_id": table_id, "operation": "unknown"}`，try/except 兜底 `[]`）
      - `call_edges_by_entry[eid]`：`_bfs_edges(eid, max_depth=CHAIN_DEPTH=2, max_edges=MAX_CHAIN_EDGES=25)`，内部过滤 `is_callchain_noise`
   e. **调用链节点中文解读富集**：收集 `call_edges_by_entry` 所有边的去重 `from/to` 端点集 `recalled_ids`；逐个 `interpretation_store.get_by_entity(mid)`（不带 level）；若 `not rec and "#" in mid` → `get_by_entity(mid.split("#", 1)[0])` 剥参重试；try/except 吞异常写 `None`；取 `rec.get("summary_text") or rec.get("interpretation_text") or rec.get("context_summary") or ""` 后 `.strip()`，非空则 `ctx.callchain_node_summaries[mid] = text[:120]`（截断 **120 字**）
   f. **候选树组装**：`build_candidate_tree(ctx.entry_candidates, code_snippets=ctx.candidate_code_snippets or {}, graph=self.graph)`；try/except 异常时 `logging.warning` + 降级 `ctx.candidate_tree = None`
6. 返回 `ctx`

### `_enrich_candidate_snippets(ctx)` 专述

- `getter = getattr(self.interpretation_store, "get_code_snippet", None)`；`if not callable(getter): return`（旧实例无此方法 → 整体跳过）
- 遍历 `ctx.entry_candidates[:_TOP_K_FOR_SNIPPET]`（=3），跳过无 `eid`
- `snippet = getter(eid)`，try/except 双保险吞异常写 `None`；`if not snippet: continue`
- `ctx.candidate_code_snippets[eid] = _truncate_snippet(snippet)`

### `_truncate_snippet(snippet, max_lines=300, max_chars=8000)` 专述

- `lines = snippet.splitlines()`；若 `len(lines) <= 300 and len(snippet) <= 8000` → 原样返回
- 否则 `kept = "\n".join(lines[:300])[:8000]` + 标注 `f"\n…（已截断，原方法共 {len(lines)} 行，调 ke_read_entity 取全文）"`

### `_bfs_chain` / `_bfs_edges` 专述

- `_bfs_chain(start_id, *, direction, max_depth, max_nodes)`：`direction=="down"`→`successors`，`=="up"`→`predecessors`，**其他→返回 `[]`**；`visited={start_id}`（起点不入 result）；逐层 BFS，邻居去重入 result，`len(result) >= max_nodes` 即 `return`；每节点 `step_fn` 异常→该节点 neighbors 视为 `[]`
- `_bfs_edges(start_id, *, max_depth, max_edges)`：`visited={start_id}`；逐层对 `successors(node)` 的每个 child，**先 `if is_callchain_noise(child): continue`**（不记边、不消耗预算、不展开），否则 `edges.append((node, child))`；`len(edges) >= max_edges` 即 `return`；`child not in visited` 才入 `visited` + `next_frontier`（已访问节点仍记为横向边，保留图结构）；`successors` 异常→该节点 children 视为 `[]`

### 所有魔法数字

| 常量 | 值 | 所在 | 含义 |
|------|-----|------|------|
| `recall_threshold` | `0.45`（默认） | `__init__` 参数 | 召回门控阈值；top1 cosine < 0.45 → chit-chat |
| `top_k` | `5`（默认） | `retrieve` 参数 | 语义召回 limit |
| `TOP_N_FOR_CHAIN_EXPANSION` | `3` | 类常量 | 只对 top-3 候选展开调用链（控成本） |
| `MAX_CALLEES` | `5` | 类常量 | 向下 1 跳最多 5 个节点 |
| `MAX_CALLERS` | `5` | 类常量 | 向上 1 跳最多 5 个节点 |
| `CHAIN_DEPTH` | `2` | 类常量 | `_bfs_edges` 多跳深度（入口→callee→再下一跳） |
| `MAX_CHAIN_EDGES` | `25` | 类常量 | `_bfs_edges` 边数上限，防 BFS 爆炸 |
| `_TOP_K_FOR_SNIPPET` | `3` | 类常量 | 源码预读 top-K 候选数 |
| `_SNIPPET_MAX_LINES` | `300` | 模块常量 | 源码截断行数阈值 |
| `_SNIPPET_MAX_CHARS` | `8000` | 模块常量 | 源码截断字符数阈值 |
| `120` | 字面量 | `retrieve` 内 | `callchain_node_summaries` 每条中文解读截断上限（字符） |
| `_TABLE_MENTION_RE` | `re.compile(r"([A-Za-z_]\w*)\s*表")` | 类常量 | `_extract_tables_from_text` 用（注：此方法 retrieve 主流程未调用，是另一处文本抽表名工具，TS 可低优先级移植） |

### 外部依赖

**Weaviate（通过 `InterpretationStoreProto`）**

- `search_method_hits_by_text(*, text, project_id, limit=5)` → `list[dict]`：每条 dict 字段 `entity_id / summary_text / level / score`（CodeEntity 兜底有真实 cosine score；解读库命中 score 可能缺失）
- `get_by_entity(entity_id, level=None)` → `dict | None`：精确查一条解读；返回字段兼容 `summary_text` / `interpretation_text` / `context_summary`（多版本 schema），永不抛、取不到返 `None`
- `get_code_snippet(entity_id)` → `str | None`：源码预读，**接口可选**（旧实例无此方法时 `_enrich_candidate_snippets` 整体跳过）

**CodeGraph（通过 `GraphProto`）**

- `successors(entity_id, rel_type=None)` → `list[str]`：下游 callee 或指定关系边（`accesses_table`）
- `predecessors(entity_id, rel_type=None)` → `list[str]`：上游 caller
- `module_of(entity_id)` → `str | None`：顶层目录（`mall-portal` / `mall-admin` 等）

**噪声过滤**：`src.knowledge.recall_rerank.is_callchain_noise(entity_id: str) -> bool` —— 单参；过滤 getter/setter、MyBatis Example/CRUD、结果包装类，不计入 `call_edges_by_entry`，也不消耗 `MAX_CHAIN_EDGES` 预算

**重排**：`src.service.qa_engine.semantic_rerank.should_rerank(candidates: list[dict]) -> bool` + `rerank_candidates(question: str, candidates: list[dict]) -> list[dict]`

**候选树**：`src.service.qa_engine.candidate_assembly.build_candidate_tree`（+ `CandidateTree` 类型）

### 降级与边界

- 候选为空 → `top1=0.0` → chit-chat 路径
- 解读库命中无 `score` → 视为 `1.0`（强信号，必过门控）
- `get_code_snippet` 不存在（旧实例）→ 整体跳过预读，不崩
- 单节点 `module_of` / `get_by_entity` / `_bfs_chain` / `_bfs_edges` / `_extract_table_access` 异常 → best-effort，写 `None` / 跳过 / 返 `[]`，不阻断主流程
- `build_candidate_tree` 异常 → `logging.warning` + `candidate_tree = None`，prompt 端走旧扁平分支
- `_bfs_chain`：`direction` 非 `"down"/"up"` → 返回 `[]`
- `_bfs_edges` 中 `is_callchain_noise` 命中的噪声 child：不写边、不展开、不消耗 edge 预算

### 怪癖

- **entity_id 参数签名剥离**：`callchain_node_summaries` 富集时，带 `#` 的签名 id（如 `com.example.OrderService::createOrder#(OrderParam)`）查不到时自动 `mid.split("#", 1)[0]` 剥参重试，防调用边 id 与 2b 解读 id 格式不一致导致大量漏匹配（实测中文覆盖 3/11 根因）
- **`score` 缺省为 `1.0`**：解读库（BusinessInterpretation）命中为强信号，不应被门控拦截；CodeEntity 兜底命中的 score 才是真实 cosine 距离
- **`recall_score` 与 rerank 解耦**：门控 `recall_score` 在 rerank 前记录（原始 cosine top1），rerank 只改候选顺序，不影响门控判定
- **`call_edges_by_entry` 含横向边**：`_bfs_edges` 的 `visited` 只防止重复展开节点，不阻止已访问节点作为 child 被记录为边（保留图结构完整性）
- **`table_access` 经包装方法**：调用链不是直接 `successors`，而是 `_extract_table_access`（含 try/except 兜底 + dict 组装）

---

## 三、`router.py` — 召回门控路由

### 组件用途

将用户自然语言问题映射到 `skill_id`（5 选 1），控制 synthesizer 走哪条作答路径。`SkillRouter.skill_id` 决定 synthesizer 路径；`QARetriever.RetrievedContext.skill_id`（`architecture`/`chit-chat`）是召回门控结果，层次不同但 `chit-chat` 语义共用。无状态，可放 `app.state` 共享。

### 公开函数签名

```
SkillRouter.__init__(llm_provider: Optional[LLMProviderProto] = None) -> None
SkillRouter.route(question: str) -> RouteDecision            # 同步，永不抛错，纯关键词，兜底 architecture
SkillRouter.classify(question: str) -> str                   # 同步壳，= route(question).skill_id
async SkillRouter.route_async(question: str) -> RouteDecision  # 关键词命中直接返回，否则 LLM fallback，永不抛错
```

### `RouteDecision` 数据结构

```
@dataclass(frozen=True, slots=True)
RouteDecision:
    skill_id: str                                       # 'business'|'dependency'|'data-flow'|'architecture'|'chit-chat'
    matched_keywords: list[str] = field(default_factory=list)
    source: str = "keyword"                             # 'keyword'|'llm'|'llm-fallback'|'llm-error'
```

### 核心算法步骤

**同步 `route`**：
1. 按词典插入顺序（优先级）遍历 `_keywords`：`dependency` → `data-flow` → `business` → `chit-chat`
2. 对每个 skill：`hits = [kw for kw in kws if kw in question]`（子串包含，**非正则**，区分大小写）
3. 首个 `hits` 非空 → 返回 `RouteDecision(skill_id, matched_keywords=hits, source="keyword")`
4. 全部未命中 → 兜底 `RouteDecision(skill_id="architecture")`（`matched_keywords=[]`，`source="keyword"`）

**异步 `route_async`**：
1. `keyword_decision = self.route(question)`；`matched_keywords` 非空 → 直接返回
2. `self._llm is None` → 返回 `keyword_decision`（同步兜底）
3. `raw = await llm.complete(system=_LLM_ROUTE_SYSTEM, user=question)`；异常 → `RouteDecision(skill_id="architecture", source="llm-error")`
4. `candidate = (raw or "").strip().split()[0] if raw else ""`（只取第一个 token）
5. `candidate in _VALID_SKILL_IDS` → `RouteDecision(skill_id=candidate, source="llm")`；否则 → `RouteDecision(skill_id="architecture", source="llm-fallback")`

### 关键词词典（完整，顺序即优先级）

| skill_id | 关键词列表 |
|----------|-----------|
| `dependency` | `["调用", "依赖", "调了"]` |
| `data-flow` | `["写表", "写到哪些表", "数据流", "怎么流", "数据库表"]` |
| `business` | `["业务规则", "约束", "限制", "校验"]` |
| `chit-chat` | `["你好","您好","嗨","hi","hello","hey","在吗","在么","在不在","早上好","晚上好","下午好","早安","晚安","谢谢","感谢","辛苦","thank","再见","拜拜","bye","goodbye","抱歉","对不起","sorry","你是谁","你叫什么","你能做什么","你是干嘛的","KE 是什么","怎么用","有什么用"]` |
| `architecture`（兜底） | 无关键词，全 miss 时默认 |

### 合法 skill 集合

`_VALID_SKILL_IDS = frozenset({"business", "dependency", "data-flow", "architecture", "chit-chat"})`（**仅** LLM 返回值校验用；含 `chit-chat`，故 LLM 路径可合法返回 `chit-chat`）。

### LLM System Prompt（`_LLM_ROUTE_SYSTEM`）

模型被要求：从 5 个 skill（`business` / `dependency` / `data-flow` / `chit-chat` / `architecture`）中选 1 个，**只输出 skill 名**，不附加任何其他文字，附 `dependency` 输出示例。

### 降级与边界

| 场景 | 结果 |
|------|------|
| 关键词未命中，无 LLM | `architecture`，`source="keyword"`（`matched_keywords=[]`） |
| LLM 调用异常 | `architecture`，`source="llm-error"` |
| LLM 返回非法值 / 空 | `architecture`，`source="llm-fallback"` |
| 关键词命中 | 对应 skill，`source="keyword"`，不调 LLM |

### 怪癖

- **兜底曾是 `chit-chat`（v1.2.1），已回退**：历史教训——"X怎么实现/X流程"等不含魔法词的真实问题被判成闲聊导致空检索，现在兜底固定为 `architecture`（做检索）
- **`route_async` 兜底（`llm-error` / `llm-fallback`）均为 `architecture`，与同步路径一致**。⚠️ **注意**：`route_async` 的 docstring（router.py:134-137）仍残留 v1.2.1 旧文案「在 5 个 skill 里选 1 个」「兜底到 chit-chat」，与实际代码（兜底 `architecture`）**矛盾**——以代码行为为准，TS 实现兜底 `architecture`，勿照搬 docstring。
- **`matched_keywords` 空 = 兜底分支信号**：`route_async` 用 `keyword_decision.matched_keywords` 是否为空判断是否需问 LLM（空 → 继续走 LLM）
- **子串匹配非正则**：`kw in question` 是 Python 字符串包含，区分大小写（如 `hi` / `hello` 仅匹配小写），中文天然无分词歧义。TS 移植用 `question.includes(kw)`，**勿做大小写归一化**（保持区分大小写行为一致）

---

涉及文件（绝对路径）：
- `/Users/java/knowledge-engineering/src/service/qa_engine/query_preprocess.py`
- `/Users/java/knowledge-engineering/src/service/qa_engine/retriever.py`
- `/Users/java/knowledge-engineering/src/service/qa_engine/router.py`
- `/Users/java/knowledge-engineering/src/knowledge/recall_rerank.py`（`is_callchain_noise(entity_id: str) -> bool`，line 116）
- `/Users/java/knowledge-engineering/src/service/qa_engine/semantic_rerank.py`（`should_rerank` line 32 / `rerank_candidates` line 97）

---

# 【semantic_rerank + recall_rerank】

All claims verified against source. Both files read in full, all callers checked. Here is the corrected complete spec.

---

## 修正清单

抽查 + 回源逐条核验结果。**绝大部分规范精确无误**。发现 **3 处需修正**（均为元信息/位置类，行为逻辑全部正确）：

1. **文件名错误**：spec 标题与正文写作 `src/knowledge/recall_rerank.py`，但任务范围标题写 `business_question_lexical_rerank.py`——该文件**不存在**。真实文件是 **`src/knowledge/recall_rerank.py`（204 行）**。spec 正文路径正确，此处标题/任务命名修正备案。
2. **composite store 路径错误**：spec 写 `composite_knowledge_store.py: CodeEntity 兜底路径` 未给目录，实际位于 **`src/knowledge/composite_knowledge_store.py`**（不在 `src/service/qa_engine/`）。
3. **`_num_env` 行为补全 + max 下限保护**：spec 给的 `overfetch = max(1, int(_num_env(...)))` 正确，但漏了一个不变量——`max(1, ...)` 是防 0/负值导致 `top_k<=0` 静默零召回的下限保护（源码注释 line 327 明示）。已在下文补入。

`sse_emitter.py:246`、`retriever.py:113` 等行号未逐一回源比对（不在范围内），但 `recall_score` 字段语义、`round(recall_score, 4)` 透传逻辑与 retriever 内的赋值逻辑一致，标注为「待 SSE 侧二次核验」。其余所有魔法数字、打分公式、降级条件、字段集、HTTP 参数、frozenset 内容 **逐字命中源码**。

---

## semantic_rerank — 行为规范提取

### 组件用途

`src/service/qa_engine/semantic_rerank.py`（127 行）：召回二次语义重排模块。设计原则："门控式护栏"——cosine 不自信时才调 DashScope gte-rerank（cross-encoder）重排候选顺序，提升自然提问 recall@1；cosine 已高且拉开时跳过，避免 cross-encoder 翻车并省调用。**本模块只改候选顺序，绝不修改 score 字段，不参与召回门控。**

---

### 公开函数签名

#### `should_rerank(candidates: list[dict]) -> bool`

**入参：**
- `candidates`: 候选列表，每项为 dict，含 `"score"` 键（float，cosine 相似度），**已按 cosine score 降序排列**。

**返回：** `True` 表示需要 rerank，`False` 表示跳过。

**算法（门控式护栏，设计 §4）：**

```
if len(candidates) < 2:
    return False                          # 无可重排
confident = _env_float("KE_RERANK_CONFIDENT_TOP1", 0.6)
margin    = _env_float("KE_RERANK_MARGIN", 0.05)
top1 = candidates[0].get("score", 0.0)
top2 = candidates[1].get("score", 0.0)
if top1 < confident:                      # top1 本身不高
    return True
if (top1 - top2) < margin:                # top1/top2 过于接近
    return True
return False                              # top1 高且拉开，信 cosine
```

> 修正注：阈值在 `should_rerank` 内部**每次调用都重读 env**（通过 `_env_float`），非模块加载时缓存。TS 实现需保持「每次调用读 env」语义。

**魔法数字（可被 env 覆盖）：**

| 常量 | 默认值 | env 变量 | 含义 |
|---|---|---|---|
| `_DEFAULT_CONFIDENT_TOP1` | `0.6` | `KE_RERANK_CONFIDENT_TOP1` | top1 cosine 低于此值视为"不自信" |
| `_DEFAULT_MARGIN` | `0.05` | `KE_RERANK_MARGIN` | top1 - top2 小于此值视为"排名不明确" |

**`_env_float(key, default) -> float`（私有，阈值读取器）：** `os.environ.get(key)` → 空（`if not raw`，**空串也算缺失**）返回 default；`float(raw)` 失败（`ValueError`）返回 default。**注意：捕获的是 `ValueError`，不是宽泛 `Exception`。** TS 实现 `parseFloat`/`Number` 失败（NaN）须等价回退 default。

防御：`candidates[i].get("score", 0.0)`，缺 score 字段时视为 0.0（偏保守，倾向触发 rerank）。

---

#### `rerank_candidates(question: str, candidates: list[dict]) -> list[dict]`

**入参：**
- `question`: 用户原始问题（原始 question，非 recall_text；cross-encoder 吃完整意图）
- `candidates`: 候选 dict 列表（含 `score`/`entity_id`/`summary_text` 等字段）

**返回：** 重排后的候选 `list[dict]`，顺序变化，**所有 dict 字段（含 score）原样不变**。

**降级条件（任一满足 → 原样返回 cosine 序，绝不抛异常）：**
1. `_rerank_enabled()` 为 False，即 env `KE_RECALL_RERANK` 经 `.strip().lower()` 后 ∈ `{"0","false","off","no"}` → 关闭
2. `len(candidates) < 2`
3. `DASHSCOPE_API_KEY` 未设置（`os.environ.get` 返回 None/空 → falsy）
4. `_gte_rerank` 任何异常（网络/超时/`raise_for_status`/JSON 解析）→ `_log.warning("gte-rerank 失败，回退 cosine 序: %s", e)` + 回退原序

> 修正注：`_rerank_enabled()` 的判定是 `(os.environ.get("KE_RECALL_RERANK") or "").strip().lower() not in (...)`。**未设置 env → 空串 → 不在关闭集合 → 默认 on（启用）**。即「默认开」。

**文档截断：** 每候选喂给 gte-rerank 的文本 = `(c.get("summary_text") or c.get("entity_id") or "")[:500]`（优先 summary_text，空/None 则 entity_id，二者皆空则空串，硬截 500 字符控 payload）。

**防御性重排拼接：**
```python
seen: set[int] = set()
reranked = []
for i in order:                          # order = gte-rerank 返回的原始 index 降序列表
    if isinstance(i, int) and 0 <= i < len(candidates) and i not in seen:
        seen.add(i); reranked.append(candidates[i])
for i, c in enumerate(candidates):       # 漏掉的 index 按原序补末尾
    if i not in seen:
        reranked.append(c)
return reranked
```
保证：越界/重复/非 int index 全部跳过不崩溃，缺失 index 按原序补末尾，最终长度 == 原 candidates 长度。TS 须保留 `isinstance(i, int)` 这道类型守卫（防响应里混入非整数）。

---

#### `_gte_rerank(query: str, documents: list[str], api_key: str) -> list[int]`（私有）

**HTTP 调用参数（POST）：**

| 参数 | 值 |
|---|---|
| URL | `https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank` |
| Headers | `Authorization: Bearer {api_key}`、`Content-Type: application/json` |
| Body.model | `gte-rerank-v2` |
| Body.input | `{"query": query, "documents": documents}` |
| Body.parameters | `{"return_documents": False, "top_n": len(documents)}` |
| Timeout | `5` 秒 |

`resp.raise_for_status()` 非 2xx 抛异常 → 上层降级。

**响应解析：** `resp.json()["output"]["results"]`，每项 `{"index": int, "relevance_score": float}`，已按 relevance_score 降序。返回值为 `[r["index"] for r in results]`（仅 index 列表，relevance_score 不传出）。

---

### 调用位置与解耦设计

`src/service/qa_engine/retriever.py` 中的 `QARetriever.retrieve()`（已回源核对 line 188-211）：

```python
# recall_score 用 rerank 前的原始 cosine top1（门控与重排解耦，recall_score 不受重排影响）
top1 = max((c.get("score", 1.0) for c in candidates), default=0.0)
if top1 < self.recall_threshold:
    return RetrievedContext(..., skill_id="chit-chat", recall_score=top1)

ctx = RetrievedContext(..., skill_id="architecture", recall_score=top1)
if should_rerank(candidates):
    candidates = rerank_candidates(question, candidates)
ctx.entry_candidates = candidates
```

**不变量（设计明文保证，已回源确认）：**
- `recall_score` = rerank **前**的原始 cosine top1，永远不受重排影响
- `score` 字段 = 原始 cosine，rerank 只改顺序，不写 score
- rerank 在门控通过（`top1 >= recall_threshold`）之后才执行，不参与门控决策

---

### `recall_score` 字段规范

定义于 `RetrievedContext.recall_score: float = 0.0`（`composite_knowledge_store.py` 内的 `RetrievedContext` dataclass，retriever.py 从此处 import；spec 原标注 `retriever.py:113` 为 dataclass 字段定义行，实际 `RetrievedContext` 定义在 `src/service/qa_engine/retriever.py` 内，line 88 class / line 113 字段 — 已回源，行号正确）。

**赋值逻辑（retriever.py 内，回源确认）：**
```python
top1 = max((c.get("score", 1.0) for c in candidates), default=0.0)
```
- 候选 `score` 缺失时默认 **`1.0`**（解读库命中视为强信号，设计 §7）—— **注意与 `should_rerank` 内的 `.get("score", 0.0)` 默认值不同**。两处默认值不一致是有意为之：门控处缺分给 1.0（放行），重排自信判定处缺分给 0.0（保守触发重排）。TS 实现切勿统一这两个默认值。
- 候选列表为空时 `default=0.0`（必然 < 阈值 → chit-chat）

**用途：** 透传到 SSE `meta/route` 事件（`sse_emitter.py:246`，**待 SSE 侧二次核验**），前端显示匹配度/调阈值。格式：`round(recall_score, 4)`。

---

## recall_rerank（lexical/structural rerank）— 行为规范提取

### 组件用途

`src/knowledge/recall_rerank.py`（**204 行**，文件名修正）：CodeEntity 召回降噪 + 加权（query-time 纯函数）。解决问题：CodeEntity 召回中 ~96% 为 MyBatis 样板/getter/Mapper-CRUD，业务实体仅 ~4%，本模块按 entity_id(qualified_name) 把候选分四类并在 composite 兜底路径过滤+重排。

---

### 公开函数签名

#### `classify_entity(entity_id: str) -> str`

**入参：** `entity_id`，格式 `com.pkg.ClassName::methodName#(ParamTypeParamName,...)`
**返回：** `"drop"` | `"boost"` | `"demote"` | `"neutral"`

**解析步骤（回源确认）：**
1. `(entity_id or "").split("#", 1)[0]` → 去参数签名，得 `ClassName::methodName`（**注意 `entity_id or ""` 防 None**）
2. `if "::" not in head: return "neutral"` → 无 `::` 直接保守返回（在 partition 之前）
3. `head.partition("::")` → `(qualified_class, "::", method)`
4. `qualified_class.rsplit(".", 1)[-1]` → `simple_class`

**分类规则（优先级从高到低，回源逐字确认）：**

| 分类 | 条件 | 含义 |
|---|---|---|
| `drop` | `method == "Base_Column_List"` 或 `simple_class.endswith("Example")` | MyBatis 纯样板，硬删除 |
| `boost` | `simple_class.endswith(("Controller", "ServiceImpl", "Service"))` | 业务实体，优先浮顶 |
| `demote` | `_is_accessor(method)` 或 `method in _GENERATED_BY_EXAMPLE` | 低价值，降权保留 |
| `neutral` | 其余（含 Mapper 的真实 CRUD 操作） | 保持原始分数 |

> 注：`endswith(("Controller","ServiceImpl","Service"))` 用元组（任一后缀命中即 True）。`endswith("Example")` 命中也含 `XxxExample` 条件类全部方法。判定顺序严格为 drop → boost → demote → neutral，TS 须保持此优先级（不可重排 if 顺序）。

格式异常（无 `::`）/ 空串 → 返回 `"neutral"`（保守）。

**`_is_accessor(method: str) -> bool`（私有，回源确认）：**
遍历前缀 `("get", "set", "is")`，条件为 `method.startswith(pre) and len(method) > len(pre) and method[len(pre)].isupper()`。即前缀后**还有字符**且**首字符为大写**（Java camelCase 访问器）。例：`getStatus`→True，`isDeleted`→True，`issueRefund`→False（`is`+小写`s`），裸 `get`→False（无后续字符）。

**`_GENERATED_BY_EXAMPLE` frozenset（私有，回源逐字确认）：**
`{"selectByExample", "countByExample", "deleteByExample", "updateByExample", "updateByExampleSelective"}`（5 个）

---

#### `rerank_and_filter(hits: list[tuple[str, float]], limit: int, *, boost: float = 0.05, demote: float = 0.05) -> list[tuple[str, float]]`

**入参：**
- `hits`: `[(entity_id, cosine_score), ...]`，通常是 over-fetch 后的候选集
- `limit`: 最终返回条数上限
- `boost`: 业务实体 adj 加量（默认 `0.05`；调用方 composite 传入由 env `KE_QA_RECALL_BOOST` 覆盖的值）—— **注意：函数签名默认是裸 `0.05`，env 覆盖发生在调用方 composite_knowledge_store，非本函数内部**
- `demote`: 低价值 adj 减量（默认 `0.05`；同上由调用方 env `KE_QA_RECALL_DEMOTE` 覆盖）
- **`*` 强制 boost/demote 为关键字参数**（keyword-only）

**返回：** `[(entity_id, 原始cosine_score), ...]`，长度 `<= limit`。**返回值始终是原始 cosine score，不含 adj 调整分。**

**核心算法（打分公式，回源逐字确认）：**

```
adj = score + boost    if cat == "boost"
    = score - demote   if cat == "demote"
    = score            if cat == "neutral"
# drop → continue 直接丢弃，不进候选集
```

**流程（回源确认）：**
1. 遍历 hits：`cat == "drop"` → `continue` 丢弃；其余计算 `adj`，追加三元组 `(eid, score, adj)` 到 `scored`
2. 空兜底：`if not scored:` → `return list(hits[:limit])`（所有候选均被 drop → 原样返回前 limit，`list()` 复制切片，绝不返回空、绝不比原始差）
3. `scored.sort(key=lambda t: t[2], reverse=True)`：按 `adj`（三元组第 3 元素）降序，**Python list.sort 稳定**（adj 相同时保留原始 hits 顺序）—— TS 须用稳定排序
4. `return [(eid, score) for (eid, score, _adj) in scored[:limit]]`：取前 `limit`，丢弃 adj

**不变量：** adj 仅用于内部排序，**不写回返回值**，保证调用方（门控）拿到的 top1 score 诚实反映 cosine 相似度。

---

#### `is_callchain_noise(entity_id: str) -> bool`

**用途：** 调用图降噪（区别于召回降噪——召回里 demote 是降权保留，调用图里 demote 也视为噪声直接剔除）。

**规则（回源确认）：**
- `classify_entity(entity_id) in ("drop", "demote")` → `True`
- 否则取短类名：`head = (entity_id or "").split("#", 1)[0]`；`simple_class = head.partition("::")[0].rsplit(".", 1)[-1]`；`simple_class in {"CommonResult", "IErrorCode"}` → `True`（结果包装类/错误码类）
- 否则 → `False`（含 boost/neutral）

`_RESULT_WRAPPER_CLASSES` frozenset（私有）：`{"CommonResult", "IErrorCode"}`（2 个）。

空串/格式异常 → 经 `classify_entity` 返回 neutral，且短类名不命中包装类 → `False`（保守保留，不误删业务节点）。

**调用位置（回源确认）：** `retriever.py._bfs_edges()`（line 426/456-459）内，BFS 展开调用图时 `if is_callchain_noise(child): continue` 过滤噪声节点，**且过滤发生在 `len(edges) >= max_edges` 检查之前**——即噪声节点不消耗 `max_edges` 预算（把预算留给业务调用）。

---

### 外部依赖与调用链

#### composite_knowledge_store 调用 rerank_and_filter

`src/knowledge/composite_knowledge_store.py`（路径修正，line 316-347，回源确认）：

```python
def _num_env(key, default): ...                              # line 316，读 env 数值，坏值兜底 default
# overfetch 倍数：默认 4；max(1, ...) 下限保护，防误设 0/负值导致 top_k<=0 静默零召回
overfetch = max(1, int(_num_env("KE_QA_RECALL_OVERFETCH", 4)))   # line 328
boost  = _num_env("KE_QA_RECALL_BOOST",  0.05)                   # line 330
demote = _num_env("KE_QA_RECALL_DEMOTE", 0.05)                   # line 332
hits = self._code_store.search_by_text(text, top_k=limit * overfetch, tenant=self._project_id)  # line 335-336
hits = rerank_and_filter(hits, limit, boost=boost, demote=demote)  # line 347
```

> 修正注：`top_k = limit * overfetch`（overfetch 是 `max(1, int(...))` 后的值）。`tenant=self._project_id`。`search_by_text` 经 `getattr` 鸭子类型探测（line 246 注释），非静态接口。

---

### 魔法数字汇总（全部回源确认）

| 模块 | 常量/参数 | 值 | env 覆盖 | 含义 |
|---|---|---|---|---|
| semantic_rerank | `_DEFAULT_CONFIDENT_TOP1` | `0.6` | `KE_RERANK_CONFIDENT_TOP1` | top1 cosine 自信门槛 |
| semantic_rerank | `_DEFAULT_MARGIN` | `0.05` | `KE_RERANK_MARGIN` | top1-top2 拉开最小差 |
| semantic_rerank | `_RERANK_TIMEOUT` | `5` 秒 | 无 | HTTP 超时，超时即降级 |
| semantic_rerank | `_RERANK_MODEL` | `"gte-rerank-v2"` | 无 | DashScope cross-encoder 模型 |
| semantic_rerank | doc 截断 | `500` 字符 | 无 | 每候选喂 gte-rerank 的文本长度上限 |
| recall_rerank | `boost` default | `0.05` | （由调用方 `KE_QA_RECALL_BOOST` 注入） | 业务实体 adj 加量 |
| recall_rerank | `demote` default | `0.05` | （由调用方 `KE_QA_RECALL_DEMOTE` 注入） | 低价值 adj 减量 |
| composite_store | `overfetch` default | `4` | `KE_QA_RECALL_OVERFETCH` | over-fetch 倍数；`max(1, int(...))` 下限保护 |
| retriever | `recall_threshold` | `0.45`（构造参数默认） | 无（构造时传入） | 召回门控阈值（top1 >= 此值走 KE） |
| retriever | `TOP_N_FOR_CHAIN_EXPANSION` | `3` | 无 | 只对 top-3 候选取调用链 |
| retriever | `MAX_CALLEES / MAX_CALLERS` | `5` / `5` | 无 | 每方向 1 跳邻居上限 |
| retriever | `CHAIN_DEPTH` | `2` | 无 | BFS 多跳深度（画调用图） |
| retriever | `MAX_CHAIN_EDGES` | `25` | 无 | 调用图边数上限 |
| retriever | `_TOP_K_FOR_SNIPPET` | `3` | 无 | 源码预读 top-K 候选 |
| retriever | callchain_node_summaries 截断 | `120` 字符 | 无 | 每条 2b 解读截断（`text[:120]`，line 284） |
| retriever | `_SNIPPET_MAX_LINES` | `300` | 无 | 源码截断行数上限（line 30） |
| retriever | `_SNIPPET_MAX_CHARS` | `8000` | 无 | 源码截断字符上限（line 31，兜底防超长单行） |

> 修正注：spec 把 `_DEFAULT_CONFIDENT_TOP1` 等 semantic_rerank 常量列为「可被 env 覆盖」——准确，但覆盖发生在 `should_rerank` 内 `_env_float` 每次调用读取。而 recall_rerank 的 `boost/demote` 默认值是**函数签名裸默认**，env 覆盖在 composite 调用方完成（两层结构不同，TS 移植须区分）。

---

### env 开关汇总（回源确认）

| env 变量 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `KE_RECALL_RERANK` | 字符串开关 | on（未设/空串→on） | `.strip().lower()` ∈ `{"0","false","off","no"}` 时关闭 gte-rerank |
| `KE_RERANK_CONFIDENT_TOP1` | float | `0.6` | 门控阈值：top1 自信分（`_env_float`，非法/空→default） |
| `KE_RERANK_MARGIN` | float | `0.05` | 门控阈值：top1-top2 最小差 |
| `DASHSCOPE_API_KEY` | string | 无 | DashScope API key（缺失/空→降级） |
| `KE_QA_RECALL_BOOST` | float | `0.05` | recall_rerank boost 幅度（`_num_env`，坏值→default） |
| `KE_QA_RECALL_DEMOTE` | float | `0.05` | recall_rerank demote 幅度 |
| `KE_QA_RECALL_OVERFETCH` | int | `4` | over-fetch 倍数（`max(1, int(_num_env(...)))`） |

---

### 降级与边界条件

**semantic_rerank 降级（任一 → 原样返回 cosine 序）：**
- `KE_RECALL_RERANK` 经 `.strip().lower()` ∈ `{"0","false","off","no"}`
- `len(candidates) < 2`
- `DASHSCOPE_API_KEY` falsy（None/空串）
- HTTP 超时（5秒）
- HTTP 非 2xx（`raise_for_status`）
- `resp.json()["output"]["results"]` 解析失败（KeyError 等）
- `_gte_rerank` 任何其他异常（`except Exception`，`_log.warning` 记录，绝不抛）

**recall_rerank 降级（rerank_and_filter）：**
- 所有 candidates 均被 drop（`scored` 为空）→ `return list(hits[:limit])`（绝不返回空）

**recall_score 不动原则（不变量，回源确认）：**
- `recall_score` 在 `should_rerank` 调用**之前**已从原始候选 `max((c.get("score", 1.0) ...), default=0.0)` 计算完毕并写入 ctx
- rerank 只改顺序，score 字段只读不写
- 门控（chit-chat/architecture 判定）与重排完全解耦

---

### 数据结构字段（回源确认）

**candidates 单项（dict）关键字段：**

| 字段 | 类型 | 来源 | 用途 |
|---|---|---|---|
| `entity_id` | str | composite_store | 实体唯一标识（qualified_name） |
| `summary_text` | str | 解读库 | 业务解读文本；gte-rerank 文本优先用此字段 |
| `score` | float | Weaviate cosine | 相似度；门控（缺省 1.0）/ should_rerank（缺省 0.0）/recall_score 来源 |
| `level` | str | composite_store | `"code_entity"` 兜底标记 |
| `module` | str\|None | graph.module_of | retriever 就地写入（line 216），标注所属模块 |

**RetrievedContext 关键字段（`retriever.py` line 88+ dataclass，回源确认）：**

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `entry_candidates` | `list[dict[str, Any]]` | `field(default_factory=list)`（line 93） | rerank 后的候选（顺序已调整，score 原样） |
| `call_edges_by_entry` | `dict[str, list[tuple[str, str]]]` | `field(default_factory=dict)`（line 102） | top-3 入口向下 2 跳调用边 |
| `skill_id` | str | `"architecture"`（line 110） | `"architecture"` 或 `"chit-chat"` |
| `recall_score` | float | `0.0`（line 113） | rerank 前原始 cosine top1，不受重排影响 |
| `callchain_node_summaries` | `dict[str, str]` | `field(default_factory=dict)`（line 116） | entity_id → 中文业务解读（`text[:120]`，line 284 截断） |

相关源码文件（绝对路径）：
- `/Users/java/knowledge-engineering/src/service/qa_engine/semantic_rerank.py`（127 行）
- `/Users/java/knowledge-engineering/src/knowledge/recall_rerank.py`（204 行）
- `/Users/java/knowledge-engineering/src/service/qa_engine/retriever.py`（调用方 + RetrievedContext + recall_score）
- `/Users/java/knowledge-engineering/src/knowledge/composite_knowledge_store.py`（rerank_and_filter 调用 + overfetch/boost/demote env）

---

# 【candidate_assembly 候选树】

The spec's §7 says retriever uses `getattr(ctx, 'candidate_code_snippets', {}) or {}` but the actual code uses `ctx.candidate_code_snippets or {}` (no getattr — it's a real dataclass field, line 120). Minor but worth correcting since the spec must be the TS authority. The behavior is identical (empty-dict fallback) but the getattr framing is inaccurate. This is a "wiring layer, not in candidate_assembly" detail, so it doesn't affect the TS port of candidate_assembly itself, but I'll correct it.

Everything else verified accurate. Here's the corrected complete spec.

---

## 修正清单

逐条回源核验 `candidate_assembly.py`（443 行）+ TDD 计划。**核心模块（§1–§6）全部精确，无行为偏差**。仅 §7 怪癖区有 2 处描述与现网源码不符（不影响 candidate_assembly 本体 TS 移植，但既为权威参考须订正）：

1. **【修正】retriever wiring 实参写法**：spec §7 写 `getattr(ctx, 'candidate_code_snippets', {}) or {}`；现网 `retriever.py:294` 实为 `ctx.candidate_code_snippets or {}`（`candidate_code_snippets` 是 `RetrievedContext` 的真实 dataclass 字段，`retriever.py:120` `field(default_factory=dict)`，无需 getattr）。空 dict 兜底行为一致，但 getattr 框架不准确，已订正。
2. **【修正】异常日志参数**：spec §7 暗示 `logging...warning("build_candidate_tree 失败: %s", e)`；现网 `retriever.py:301-304` 实为 `"build_candidate_tree 异常，降级为 None：%s: %s", type(exc).__name__, exc`。属 wiring 层细节，已对齐。

**抽查确认无误的关键点**：① `_bfs_reachable` depth≥max_depth `continue`（line 100-101）+ depth=max_depth 节点已在上轮 `reachable.add` 故可达不扩展（line 113-115）；② `build_subtree_for_entry` 严格候选过滤 `if succ_qn not in candidate_qns_set: continue`（line 253-254）；③ token 公式 `max(1, int(chars/3.5))`（line 314），code_snippet 仅在 truthy 时计入（line 303-304）；④ 7 个魔法数字逐一核对默认值；⑤ 同 qn 去重保首个（line 364-365）；⑥ notes `>= 2`（line 419）；⑦ prompt 分支 `candidate_tree is not None and len(subtrees) >= 2 and not fallback_to_flat`（`prompts.py:327-329`）现网逐字一致。

---

## candidate_assembly — 行为规范（TS 移植权威参考）

源文件：`/Users/java/knowledge-engineering/src/service/qa_engine/candidate_assembly.py`（443 行）
TDD 计划：`/Users/java/knowledge-engineering/docs/superpowers/plans/2026-06-08-candidate-tree-assembly.md`

---

## 1. 数据结构

### TreeNode

```
@dataclass（非 frozen，构造期 append children，构造后只读）
```

| 字段 | 类型 | 语义 |
|---|---|---|
| `entity_id` | `string` | qualified_name（已剥 `#` 签名），等同 CodeGraph qn |
| `summary` | `string` | 业务说明（已由 build_candidate_tree 按 max_summary_chars 截断） |
| `module` | `string \| null` | 模块名（如 `mall-portal` / `mall-admin`）；未知时 null |
| `code_snippet` | `string \| null` | source-first grounding P1 命中的真实源码；无时 null |
| `children` | `TreeNode[]` | BFS 下游候选成员子节点；默认 `[]`（`field(default_factory=list)`，不共享引用） |

### CandidateTree

```
@dataclass（普通 mutable）
```

| 字段 | 类型 | 语义 |
|---|---|---|
| `subtrees` | `TreeNode[]` | 独立入口对应子树，最多 `max_entries` 棵，按 recall 原序 |
| `orphans` | `TreeNode[]` | 孤儿节点（无 children），按 recall 原序取前 `max_orphans` 个 |
| `fallback_to_flat` | `boolean` | token 估算超阈值时为 `true`；调用方（prompt builder）应降级走扁平分支 |
| `notes` | `string[]` | 元信息字符串列表；多入口时含桥接提示（≥2 棵子树时追加） |

---

## 2. 公开函数签名与算法

### 2.1 内部辅助：`_strip_signature(durable_key: str) -> str`

- 剥 `#` 签名：`'Cls::m#(Long)'` → `'Cls::m'`；不含 `#` 原样返回
- 实现：`durable_key.split('#', 1)[0]`
- 调用方必须在比较 qn 前统一归一

### 2.2 内部辅助：`_bfs_reachable(start: str, graph, max_depth: int) -> Set[str]`

**用途**：BFS 收集 `start` 的所有下游可达 qn 集合（不含 `start` 自身）。

**入参**：
- `start`：已剥签名的 qn
- `graph`：实现 `successors(qn) -> list[durable_key]` 的对象（GraphProto-like）
- `max_depth`：BFS 最大深度（`depth >= max_depth` 的节点不再扩展；depth 0 = 起点，1 = 直接下游）

**算法步骤**：
1. `queue = deque([(start, 0)])`，`reachable = set()`
2. `popleft` 取 `(node, depth)`；`depth >= max_depth` 时 `continue`（不扩展）
3. `succs = graph.successors(node) or []`（`None` → `[]`）；try/except 包裹，异常时 `succs = []`（fail-soft，与 ke_impact 同范式）
4. 每个 successor 剥签名为 `qn`；条件 `if qn != start and qn not in reachable` 才 `reachable.add(qn)` 并以 `(qn, depth+1)` 入队（跳过自环 + 已访问去重）

**边界**：fail-soft——图后端单节点查询失败不阻断整体 BFS，返回已收集的部分结果。

---

### 2.3 `compute_independent_entries(candidate_qns, graph, *, max_depth=3) -> list[str]`

**入参**：
- `candidate_qns: list[str]` — 候选 qn 列表（调用方应预先剥签名；本函数也容忍带签名输入，内部做一次 strip）
- `graph` — GraphProto-like，`successors(qn) -> list[durable_key]`
- `max_depth: int = 3` — BFS 深度上限（关键魔法数字）

**出参**：`list[str]` — 独立入口 qn 列表，保 `candidate_qns` 原序的子集

**算法**（双向 reach 集合互查）：
1. 防御归一：`normalized = [_strip_signature(c) for c in candidate_qns]`
2. 预算每个候选的下游 reach：`reach[c] = _bfs_reachable(c, graph, max_depth)`（字典推导式，key 为 normalized 元素）
3. 独立判定：对每个 `c`（遍历 `normalized`），遍历所有 `d ≠ c`，若 `c in reach[d]` 则 `is_descendant=True` 并短路 `break`
4. 未被任何 `d` 的 reach 集包含的 `c` → 加入 `independent`，保原序

**不变量**：`c ∈ independent ⟺ ∀d ∈ candidates, d≠c → c ∉ reach[d]`

**边界**：空输入 → 空列表。

---

### 2.4 `build_subtree_for_entry(entry_qn, candidate_meta, graph, *, max_depth=3, max_nodes_per_subtree=6) -> TreeNode`

**入参**：
- `entry_qn: str` — 入口 qn（已剥签名）
- `candidate_meta: dict[str, dict]` — `{qn: {summary, module?, code_snippet?}}`；只有这些 qn 的节点会被挂进子树
- `graph` — GraphProto-like
- `max_depth: int = 3` — BFS 深度上限（魔法数字）
- `max_nodes_per_subtree: int = 6` — 整棵子树节点上限（含根；魔法数字）

**出参**：`TreeNode`（根节点，children 可能为空，但根必定存在）

**算法（BFS 候选优先）**：
1. `candidate_qns_set = set(candidate_meta.keys())`（O(1) 成员判断）
2. `_make_node(qn)` 闭包：从 `candidate_meta.get(qn, {})` 取 `summary`（`meta.get("summary") or ""`）/ `module`（`meta.get("module")`，None 兜底）/ `code_snippet`（`meta.get("code_snippet")`，None 兜底）；`children=[]`
3. 构造 `root = _make_node(entry_qn)`
4. BFS 状态：`visited = {entry_qn}`，`node_lookup = {entry_qn: root}`，`queue = deque([(entry_qn, 0)])`，`nodes_added = 1`
5. 主循环条件：`queue 非空 AND nodes_added < max_nodes_per_subtree`
6. 每轮：`popleft (current_qn, depth)`；`depth >= max_depth` → `continue`（不扩展）
7. `succs = graph.successors(current_qn) or []`，try/except 异常 → `succs = []`（fail-soft）
8. **候选优先排序**：`succ_qns = [_strip_signature(s) for s in succs]`，再 `sorted(key=lambda q: (0 if q in candidate_qns_set else 1))`（候选排前，稳定排序保原顺序）
9. 遍历 `prioritized`：
   - `nodes_added >= max_nodes_per_subtree` → `break`（剩余 successors 跳过）
   - `succ_qn in visited` → `continue`（去重）
   - `succ_qn not in candidate_qns_set` → `continue`（**严格候选过滤，非候选一律不挂**）
   - 否则：`visited.add(succ_qn)`，`child = _make_node(succ_qn)`，`node_lookup[current_qn].children.append(child)`，`node_lookup[succ_qn] = child`，`nodes_added += 1`，`queue.append((succ_qn, depth+1))`
10. 返回 `root`

**边界**：图完全不可用时，只返根节点（无 children）。

---

### 2.5 `build_candidate_tree(candidates, code_snippets, graph, *, max_entries=3, max_depth=3, max_nodes_per_subtree=6, max_orphans=3, max_summary_chars=300, token_safety_cap=8000) -> CandidateTree`

**入参完整签名**：

| 参数 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `candidates` | `list[dict]` | — | recall 候选列表，每项含 `entity_id` / `summary_text` / `module?` / `score?` |
| `code_snippets` | `dict[str, str]` | — | `{entity_id: source}`，P1 grounding 结果，可为空 dict |
| `graph` | GraphProto-like | — | 提供 `successors` |
| `max_entries` | `int` | `3` | 子树数量上限（魔法数字） |
| `max_depth` | `int` | `3` | BFS 深度（传递给下游函数，魔法数字） |
| `max_nodes_per_subtree` | `int` | `6` | 单子树节点上限（含根，魔法数字） |
| `max_orphans` | `int` | `3` | 孤儿上限（魔法数字） |
| `max_summary_chars` | `int` | `300` | summary 截断字符数（超出截断 + 追加 `"…"`，魔法数字） |
| `token_safety_cap` | `int` | `8000` | token 总量上限；超过则 `fallback_to_flat=true`（魔法数字） |

**出参**：`CandidateTree`

**六步编排算法**：

**步骤 1：提取 qn + meta**
- 遍历 `candidates`，取 `eid = c.get("entity_id")`，剥签名得 `qn`
- 无 `entity_id`（falsy）的候选跳过：`if not eid: continue`（防御）
- 同 qn 重复（带/不带签名两种形态）→ 保首个，跳后续（`if qn in candidate_meta: continue`，此判断在 `candidate_qns.append(qn)` 之前）
- summary 取 `c.get("summary_text") or ""`；截断：`len(summary) > max_summary_chars` 时 `summary = summary[:max_summary_chars] + "…"`
- `candidate_meta[qn] = {"summary": summary, "module": c.get("module"), "code_snippet": ...}`
- code_snippet 查找顺序：`code_snippets.get(eid) or code_snippets.get(qn)`（P1 grounding 多用完整 eid 作 key；先带签名 eid 再裸 qn）

**步骤 2：独立入口**
- `independent = compute_independent_entries(candidate_qns, graph, max_depth=max_depth)`
- `subtree_roots = independent[:max_entries]`（切片保原序，限制至多 `max_entries` 棵）

**步骤 3：构造子树**
- 列表推导：每个 `entry in subtree_roots` 调 `build_subtree_for_entry(entry, candidate_meta, graph, max_depth=max_depth, max_nodes_per_subtree=max_nodes_per_subtree)`

**步骤 4：孤儿**
- `in_subtree_qns = set()`；`for st in subtrees: in_subtree_qns.update(_collect_all_tree_qns(st))` 递归收集所有子树节点（含根）
- `orphan_qns = [q for q in candidate_qns if q not in in_subtree_qns]`（保 recall 原序）
- `orphans = [TreeNode(entity_id=q, summary=candidate_meta[q]["summary"], module=candidate_meta[q]["module"], code_snippet=candidate_meta[q]["code_snippet"], children=[]) for q in orphan_qns[:max_orphans]]`（叶子节点，无 children）

**步骤 5：notes**
- `if len(subtrees) >= 2`：追加固定字符串（`%d` = `len(subtrees)`）：
  ```
  "多入口检测：识别到 %d 个独立业务路径；路径间可能通过 MQ / Spring 配置 / AOP 异步桥接，CodeGraph 静态分析无法连线，作答时用文字描述跨边界关系，不要编 calls 边。" % len(subtrees)
  ```

**步骤 6：token 估算 + fallback**
- 先构造 `tree = CandidateTree(subtrees, orphans, fallback_to_flat=False, notes)`
- `if _estimate_tokens_in_tree(tree) > token_safety_cap`：重建 `CandidateTree(subtrees=tree.subtrees, orphans=tree.orphans, fallback_to_flat=True, notes=tree.notes)`
- 保留 subtrees/orphans 内容供调试；仅置 flag，prompt 端据此降级

**边界**：`candidates` 为空 → 立即返 `CandidateTree(subtrees=[], orphans=[], fallback_to_flat=False, notes=[])`（在步骤 1 之前 `if not candidates: return ...`）

---

## 3. 内部辅助（build_candidate_tree 私有）

### `_collect_all_tree_qns(node: TreeNode) -> Set[str]`

递归收集子树所有 `entity_id`（含 root）。用于步骤 4 判定孤儿。实现：`out = {node.entity_id}`；`for c in node.children: out.update(_collect_all_tree_qns(c))`；`return out`

### `_estimate_tokens_in_tree(tree: CandidateTree) -> int`

token 粗估（与 `synthesizer._estimate_tokens` 同源）：

- 内部 `_count(node)` 闭包（`nonlocal chars`）递归累加：`chars += len(entity_id) + len(summary)`，且 **`code_snippet` 仅在 truthy（`if node.code_snippet:`）时** 追加 `len(code_snippet)`
- 遍历所有 subtrees 与 orphans 节点
- 公式：`max(1, int(total_chars / 3.5))`
- **魔法数字**：`3.5` chars/token（中英混合估算；实际 DashScope tokenizer 略有差异，用于阈值判断够用）
- 返回值 `≥ 1`（`max(1, ...)` 防 0 干扰下游）

---

## 4. 所有魔法数字汇总

| 数字 | 参数名 | 含义 |
|---|---|---|
| `3` | `max_entries` | 最多构造 3 棵子树 |
| `3` | `max_depth` | BFS 最大跳数（depth 0=起点，收集到 3 跳下游） |
| `6` | `max_nodes_per_subtree` | 单棵子树节点上限（含根） |
| `3` | `max_orphans` | 孤儿取前 3 条 |
| `300` | `max_summary_chars` | summary 截断字符数，超出追加 `"…"` |
| `8000` | `token_safety_cap` | token 总量上限，超过触发 `fallback_to_flat=True` |
| `3.5` | — | chars/token 中英混合估算系数 |

---

## 5. 外部依赖

### GraphProto 接口（`@ke/codegraph`）

本模块仅使用 `successors` 一个方法：

```
graph.successors(qn: str, rel_type=None) -> list[durable_key: str]
```

- 返回带签名的 durable_key 列表（如 `["Cls::m#(Long)"]`）
- 返回 `None` 或抛异常均安全（内部统一 `succs = graph.successors(...) or []` + try/except → `[]`）
- 未使用 `predecessors` / `module_of`（这两个在 retriever wiring 层使用，不在 candidate_assembly 内）

### 候选输入字段约定

`candidates` 列表每项 dict 期望字段：

| 字段 | 必须 | 备注 |
|---|---|---|
| `entity_id` | 是 | falsy（缺失/空/None）则 `if not eid: continue` 跳过 |
| `summary_text` | 否 | 缺失或 None 归一为 `""`（`c.get("summary_text") or ""`） |
| `module` | 否 | 透传至 TreeNode.module（`c.get("module")`，可能 None） |
| `score` | 否 | 仅用于文档语义；算法内部不使用（顺序由 recall 列表本身保证） |

### code_snippets

`{entity_id: source_code_string}`。查找顺序：`code_snippets.get(eid) or code_snippets.get(qn)`（完整带签名 eid → 裸 qn）。

---

## 6. 降级与边界行为

| 场景 | 行为 |
|---|---|
| `candidates` 空 | 返 `CandidateTree([], [], false, [])` |
| `entity_id` 缺失/falsy 的候选项 | 跳过（防御），不计入 `candidate_qns` |
| 同 qn 重复出现（带/不带签名） | 保首个，后续跳过 |
| `graph.successors` 返 None | `or []` 归一为空 |
| `graph.successors` 抛异常 | fail-soft：视为 `[]`，BFS 继续 |
| 独立入口数 > `max_entries` | 切片取前 N，超出部分若不在子树内则进孤儿 |
| 孤儿数 > `max_orphans` | 切片取前 `max_orphans`，按 recall 原序 |
| token 估算 > `token_safety_cap` | `fallback_to_flat=True`，subtrees/orphans 内容保留供调试 |
| 子树数 < 2 | notes 不追加"多入口"提示（`len(subtrees) >= 2` 才触发） |
| `candidate_tree=None` 或 `fallback_to_flat=True` | prompt builder 端走扁平分支（本模块只产出 flag，执行由 prompts.py 实施） |

---

## 7. 模块定位与怪癖

- **纯函数模块**：不持状态、不读外部存储、所有依赖通过参数注入
- **调用链接**：retriever.retrieve → build_candidate_tree → ctx.candidate_tree → synthesizer._ctx_to_dict → prompts.build_user_prompt
- **retriever wiring（Task 5，已上线）**：`retriever.py:291-296` 调用 `build_candidate_tree(ctx.entry_candidates, code_snippets=ctx.candidate_code_snippets or {}, graph=self.graph)`。**注意：`candidate_code_snippets` 是 `RetrievedContext` 的真实 dataclass 字段（`retriever.py:120`，`field(default_factory=dict)`），故用 `ctx.candidate_code_snippets or {}` 而非 getattr**。整体 try/except 包裹，异常时 `ctx.candidate_tree = None` 并 `logging...warning("build_candidate_tree 异常，降级为 None：%s: %s", type(exc).__name__, exc)`，不阻断 retrieve。
- **prompt 分支判断**（`prompts.py:327-329`，非本模块内但依赖本模块产出）：`candidate_tree is not None AND len(candidate_tree.subtrees) >= 2 AND NOT candidate_tree.fallback_to_flat` → 树形渲染（`_render_tree_candidates`）；否则扁平（`_render_flat_candidates`，文案含"按相关度倒序"）
- **BFS 不收非候选节点**：`_bfs_reachable` 用于 reach 集合判定时不限候选；但 `build_subtree_for_entry` 挂载节点时严格过滤——**只有在 `candidate_meta` 里的 qn 才能进子树**，防止子树突破召回边界引入 LLM 未见过的 entity
- **保序**：所有输出（`independent`、`subtrees`、`orphans`）均保 `candidate_qns` 的 recall 原序（首位为召回分最高者）
- **`_bfs_reachable` 的深度语义**：`depth >= max_depth` 时 `continue`（不扩展）但当前节点已加入 reachable（在上一轮入队时已 `reachable.add(qn)`），即深度 `max_depth` 的节点可达但不再扩展其下游
- **复杂度估算**（TDD 计划 Self-Review）：K=10 候选，两两比较 100 对，每对 BFS depth=3 约 100 节点 → 共 ~10000 节点访问 / 请求，纯内存，预计 < 10ms

---

### TS 移植额外提醒（核验员补充）

- **`sorted` 稳定性**：Python `sorted` 保证稳定排序，候选优先排序依赖这一点（key 相同保原顺序）。TS `Array.prototype.sort` 在现代引擎（V8/Node 11+）也稳定，但移植时务必显式确认运行时；若有疑虑，排序 key 应改为 `[isCandidate ? 0 : 1, originalIndex]` 复合 key 以锁定顺序。
- **`int(chars / 3.5)` 截断语义**：Python `int()` 向零截断。TS 用 `Math.trunc(chars / 3.5)`（非 `Math.floor`，虽 chars≥0 时两者等价，但语义对齐 Python）。
- **`field(default_factory=list)`**：避免 Python 可变默认陷阱；TS 移植时 `children` 默认值不要写共享数组字面量，每个节点应 new 独立数组。
- **`max(1, ...)` 返回 `≥ 1`**：`_estimate_tokens_in_tree` 即便空树也返 1，TS 须保留此下限。

源文件绝对路径：`/Users/java/knowledge-engineering/src/service/qa_engine/candidate_assembly.py`
关联 wiring（非本模块）：`/Users/java/knowledge-engineering/src/service/qa_engine/retriever.py`（line 120/128/291-305）、`/Users/java/knowledge-engineering/src/service/qa_engine/prompts.py`（line 321-333/668-）