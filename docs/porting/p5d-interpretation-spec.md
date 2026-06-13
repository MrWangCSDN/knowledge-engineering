# P5d 4 种解读 runner 行为规范（py-final-baseline 提取，经对抗核验）

> TS 移植权威。2026-06-12 提取+对抗核验（LLM prompt 全文保留）。
> **关键怪癖（核验抓出）**：① 写入走 tenant=project_id，但断点续跑 list_existing 与孤儿清理 delete_by_id **走 _get_collection() 无 tenant**（分区错配，TS 须如实复刻或评估修正）；② _to_uuid=sha256(s)[:32] 重排（与 @ke/store toUuid 同算法）；③ TopologicalInterpretation collection（无独立 business collection）；④ MultiProvider round-robin + 限流指数退避，max_workers=8 两平台各半；⑤ 解读条目 UUID=method_entity_id|interpret；⑥ 模式B callchain 实时返回不落库。



---

# 【base runner + 方法级技术解读（模式A）】

I have everything I need. Let me verify the `get_embedding` signature and the orphan deletion path (`_resolve_collection` vs `_get_collection`) — the spec says orphan cleanup uses `weaviate_store._get_collection().data.delete_by_id(uid)` which does NOT pass tenant. This is a critical correctness detail for the TS port. Let me confirm the `_to_uuid` is sha256-based (spec said "字符串哈希" — vague but let me make it precise).

I now have all the facts. Key findings to correct in the spec:

1. **`_to_uuid` is SHA-256 based**, not a generic "字符串哈希" — exact algorithm matters for cross-language UUID compat (load-bearing for断点续跑/孤儿清理).
2. **Orphan cleanup uses `_get_collection()` (NO tenant)** — deletes from the legacy/default partition, NOT the tenant partition where writes go. This is a real bug/quirk: writes go to `tenant=project_id` but `list_existing_method_ids` reads from `_get_collection()` (no tenant) and orphan delete also from no-tenant. Must document faithfully.
3. **`list_existing_method_ids` reads from `_get_collection()` (no tenant)** — same tenant mismatch.
4. **MultiProvider round-robin + backoff** is entirely missing from the spec — the task explicitly flags "多 provider 轮询" as a focus.
5. **`llm.generate(prompt, timeout=timeout)`** — passed as kwarg, not positional.
6. **`count()` fallback** to `list_existing_method_ids(limit=200000)`.
7. **`list_existing_keys` default limit is 100000** in `MethodInterpretationStoreAdapter` (overrides protocol's 200000).
8. Unknown backend → ollama fallback (not RuntimeError).

Let me produce the corrected spec.

---

## 修正清单

逐条回源核验结果（编号对应原 spec 章节）。**LLM prompt 全文已逐字保留**，与源码 `_build_prompt`（method_interpretation_runner.py:110-157）完全一致，无需改动。

1. **【严重 / 缺失】MultiProvider 轮询 + 退避逻辑整章缺失**。任务明确点名「多 provider 轮询」，但原 spec 仅在 §13 提及 fallback，对 `multi_provider.py` 的 round-robin / 限流退避 / 抛错策略只字未提。新增 §15。
2. **【严重 / 落库】`_to_uuid` 是 SHA-256（非泛指"字符串哈希"）**。源码 `base_weaviate_store.py:31` 为 `hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]` 再按 `8-4-4-4-12` 切片拼成 UUID 字符串。这是跨语言幂等 UUID 的精确算法，TS 必须逐字节对齐。原 spec §6 含糊，已修正。
3. **【严重 / 孤儿同步 + 断点续跑 tenant 不一致 — 真 bug 级怪癖】**：写入走 `tenant=project_id`（`_resolve_collection` → `with_tenant`），但 **`list_existing_method_ids` 与孤儿删除 `delete_by_id` 都走 `_get_collection()`（无 tenant，default/legacy 分区）**。即读侧/清理侧与写侧分区不一致。原 spec §9/§10 把读和删都笼统写成"在 Weaviate"，未点明分区错配。TS 移植若照搬会导致断点续跑永远读不到 tenant 写入的对象（全量重跑）、孤儿清理删不到 tenant 分区里的对象。已在 §9/§10/§13 显式标注。
4. **【中 / 签名】`llm.generate` 调用是 `generate(prompt, timeout=timeout)`（kwarg）**，provider 实际签名是 `generate(prompt, **kwargs)`，从 kwargs 取 `timeout`/`model`/`max_tokens`。原 spec 写成 `.generate(prompt, timeout) -> str` 易误导为位置参数。已修正 §2.3。
5. **【中 / 魔法数字】`count()` 的 fallback 上限是 `list_existing_method_ids(limit=200000)`**（store 层），而 `MethodInterpretationStoreAdapter.list_existing_keys` 默认 `limit=100000`。两个 200000/100000 不要混。已在 §8 拆清。
6. **【中 / 工厂】未注册 backend → 静默回退 ollama（不抛错）**。`create_with_meta` 对 registry 未命中的 backend 一律按 ollama 解析（`resolved_backend=requested`、`fallback_reason=""`）。RuntimeError 仅发生在 openai/anthropic 库 ImportError 且 `allow_fallback=False` 时；multi 缺 `multi_providers` 抛 ValueError。原 spec §13 只说了 ImportError 分支。已补 §13。
7. **【轻 / 返回值】`skipped` 分支返回的是 `{"skipped": True, "written": 0, "failed": 0}`**（不止 `skipped` 一个键）。且触发条件是 `not mi.enabled` **或** `vinterp.backend != "weaviate"` **或** `not vinterp.enabled`。原 spec §2.2 漏了 backend 判断与 written/failed 键。已修正。
8. **【轻 / 校验】`coerce_*` 用 Pydantic v2 `model_validate(dict(cfg))`**；dimension 默认 1024 但 `int(vinterp.dimension) if vinterp.dimension else 64`，仅当 dimension 落为 0/假值才退 64。无误，保留并明确。
9. **【轻】`BaseInterpretationRunner` 的 `publish_item_list` 实际传入的是 `list[tuple[str, bool]]`**（`(label, done)` 元组列表），不是 `list[str]`。`item_list_callback` 形参类型注解虽为 `Callable[[list[str]], None]`，但运行时传 tuple 列表。已在 §9 标注。
10. 其余（候选选取 §7、`_is_trivial_accessor` §7、`_build_method_context` §12、progress 映射 §8、batch 分批 §8、可恢复异常集合 §2.3、关闭逻辑 §14）核对无误，保留。

---

# Base 解读 Runner + 方法级技术解读（模式 A）— TS 移植规范（修正版）

## 1. 组件用途

| 组件 | 文件 | 用途 |
|---|---|---|
| `BaseInterpretationRunner` | `base_interpretation_runner.py`（60 行） | dataclass，持有 5 个可选回调；每个回调内部 try/except，异常仅 `_LOG.debug(..., exc_info=True)` 不上抛 |
| `run_method_interpretations` | `method_interpretation_runner.py`（426 行） | 方法级技术解读主入口；候选选取 → 孤儿清理 → 断点续跑 → 分批并发 LLM → 写 Weaviate |
| `interpret_one_llm_embed_store` | `interpretation_item_helpers.py`（114 行） | 单条：LLM → 去 think 标签 → 提取摘要 → embedding(仅摘要) → persist |
| `WeaviateTopologicalInterpretStore` | `weaviate_interpretation_store.py` | Weaviate 写/读；collection = `TopologicalInterpretation`；Multi-Tenancy（写带 tenant、读/删不带） |
| `MethodInterpretationStoreAdapter` | `interpretation_store_adapter.py` | 薄包装，暴露 `list_existing_keys`/`count`/`clear`/`close`/`add` |
| `LLMProviderFactory` | `llm/factory.py` | 按 backend 字符串经 registry 创建 provider；ollama / openai / anthropic / multi；未知 backend 回退 ollama |
| `MultiProvider` | `llm/multi_provider.py` | 多 provider round-robin + 限流指数退避 |

> 注：`interpretation_runner_inputs.py` **不存在**（重构中已删）。其 4 个符号（`MethodInterpretInput`、`VectorDbInterpretInput`、`coerce_method_interpretation_config`、`coerce_vectordb_config`）已内联进 `method_interpretation_runner.py` 顶部（行 33-51）。

---

## 2. 公开函数签名

### 2.1 `BaseInterpretationRunner`（dataclass）

```
@dataclass
class BaseInterpretationRunner:
    step_callback:           Optional[Callable[[str], None]]                  = None
    progress_callback:       Optional[Callable[[int, int, str], None]]        = None
    item_completed_callback: Optional[Callable[[str, bool], None]]            = None
    item_started_callback:   Optional[Callable[[str, InterpretPhase], None]]  = None
    item_list_callback:      Optional[Callable[[Any], None]]                  = None

    def step(msg: str) -> None
    def progress(current: int, total: int, message: str) -> None
    def complete_item(label: str, done: bool) -> None
    def start_item(label: str, phase: InterpretPhase = InterpretPhase.TECH) -> None
    def publish_item_list(items: Any) -> None
```

所有方法对回调异常静默 `debug` log（带 `exc_info=True`），不上抛。

### 2.2 `run_method_interpretations`

```
def run_method_interpretations(
    structure_facts: StructureFacts,
    interpret_cfg:   TopologicalInterpretationConfig | Mapping[str, Any],
    vectordb_cfg:    VectorDBConfig | Mapping[str, Any],
    *,
    step_callback, progress_callback, item_list_callback,
    item_completed_callback, item_started_callback,
    interpretation_stats_callback: Optional[Callable[[int, int, InterpretPhase], None]] = None,
    project_id:                    Optional[str] = None,
) -> dict[str, Any]
```

**返回值字段：**

| 场景 | 返回 |
|---|---|
| 正常完成 | `{written, failed, total_candidates, already_done_before, todo_this_run}` |
| **跳过**（`not enabled` 或 `backend != "weaviate"` 或 `not vinterp.enabled`） | `{"skipped": True, "written": 0, "failed": 0}` |

| 字段 | 类型 | 含义 |
|---|---|---|
| `written` | int | 本轮新写入成功条数（`ok`） |
| `failed` | int | 本轮失败条数（`fail`） |
| `total_candidates` | int | 有 code_snippet 且非 getter/setter 的方法总数 |
| `already_done_before` | int | 本轮开始前已有解读的方法数（`already_done`，**注意是用孤儿清理后的 existing_ids 匹配**） |
| `todo_this_run` | int | `len(todo_methods)`，已裁剪 `max_methods` |

### 2.3 `interpret_one_llm_embed_store`

```
def interpret_one_llm_embed_store(
    runner, label, phase: InterpretPhase, *,
    llm:           Any,          # provider，调用方式为 llm.generate(prompt, timeout=timeout)
    prompt:        str,
    timeout:       int,          # 秒；作为 **kwargs 里的 timeout 传入 provider.generate
    min_text_len:  int,          # 方法解读硬编码 10
    embedding_dim: int,
    persist:       Callable[[str, list[float]], tuple[bool, bool]],
) -> tuple[int, int]   # (ok_delta, fail_delta)，各 0 或 1
```

**调用形态**（精确）：`raw_text = llm.generate(prompt, timeout=timeout)`。所有 provider 的签名是 `generate(self, prompt: str, **kwargs) -> str`，内部从 `kwargs` 取 `timeout`（默认回退自身 `self.timeout`/`self._timeout`），也可取 `model`/`max_tokens`。TS 端 provider 接口应为 `generate(prompt, opts?: {timeout?, model?, maxTokens?})`。

**失败短路**：
- `not raw_text or len(raw_text) < min_text_len` → 返回 `(0,1)`（去 think **前**判一次）
- 去 think 后 `len(text) < min_text_len` → 再判一次，`(0,1)`

**可恢复异常集合**（记失败、`complete_item(label, False)`、不打 error 堆栈）：`urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError`。其余 `Exception` 走 `_LOG.exception(...)`（打堆栈）后同样计失败。

### 2.4 `WeaviateTopologicalInterpretStore.add_with_created`

```
def add_with_created(
    vector: list[float], method_entity_id: str, interpretation_text: str, *,
    tenant: Optional[str] = None,        # 运行时由 runner 透传 project_id
    class_entity_id: str = "", class_name: str = "", method_name: str = "",
    signature: str = "", context_summary: str = "", language: str = "zh",
    related_entity_ids_json: str = "{}",
) -> tuple[bool, bool]   # (成功, 是否新建)
```

### 2.5 `LLMProviderFactory`

```
@staticmethod
def from_method_interpretation(m) -> LLMProviderSelection   # → create_with_meta(backend=m.llm_backend, **kwargs)

@staticmethod
def create(backend="ollama", **kwargs) -> LLMProvider       # 只取 .provider
@staticmethod
def create_with_meta(backend="ollama", **kwargs) -> LLMProviderSelection

@dataclass(frozen=True)
class LLMProviderSelection:
    provider:          LLMProvider
    requested_backend: str
    resolved_backend:  str
    fallback_reason:   str = ""
```

`interpretation_llm_kwargs_from_config(m)` 抽取的 kwargs 键：`ollama_base_url, ollama_model, timeout_seconds, openai_api_key, openai_base_url, openai_model, openai_max_tokens, anthropic_api_key, anthropic_model, anthropic_max_tokens, llm_allow_fallback_to_ollama, multi_providers`。

backend registry 在模块导入时安装 `openai`/`anthropic`/`multi` 三个 builder（`ollama` 不在 registry，靠默认分支）。

---

## 3. 配置模型（`TopologicalInterpretationConfig` / `VectorDBConfig`）

**`TopologicalInterpretationConfig`**（Pydantic v2 BaseModel）:

| 字段 | 类型 | 默认值 |
|---|---|---|
| `enabled` | bool | `False` |
| `language` | str | `"zh"` |
| `ollama_base_url` | str | `"http://127.0.0.1:11434"` |
| `ollama_model` | str | `"qwen2.5:32b"` |
| `timeout_seconds` | int | `120` |
| `max_methods` | int | `0`（0=不限） |
| `max_workers` | int | `4` |
| `llm_backend` | str | `"ollama"` |
| `openai_api_key` | Optional[str] | None |
| `openai_base_url` | Optional[str] | None |
| `openai_model` | str | `"gpt-4o-mini"` |
| `openai_max_tokens` | Optional[int] | None |
| `anthropic_api_key` | Optional[str] | None |
| `anthropic_model` | str | `"claude-3-5-sonnet-20241022"` |
| `anthropic_max_tokens` | int | `8192` |
| `llm_allow_fallback_to_ollama` | bool | `False` |
| `multi_providers` | Optional[list[dict]] | None |

**`VectorDBConfig`**（关键字段）：`enabled=True`、`backend="weaviate"`、`dimension=1024`、`weaviate_url`/`weaviate_grpc_port`/`weaviate_api_key`、`collection_name`（interpret 默认 `DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION`）、`allow_fallback_to_memory=False`。

`coerce_*` 用 `model_validate(dict(cfg))`（Pydantic v2）规整入参。

---

## 4. LLM Prompt 全文（逐字，与 `_build_prompt` 完全一致）

分支条件：`(language or "zh").lower().startswith("en")` → 英文，否则中文。

### 4.1 中文 Prompt（`language` 不以 `en` 开头）

```
你是一名资深 Java 工程师。请根据下面的「类与调用链上下文」以及「方法代码」，输出该方法的技术解读。

要求：
- 使用简体中文，分两部分输出。
- 第一部分 [摘要]：关键词密集，不超过50个中文字符，用空格分隔关键词/短语，不要完整句子。包含：业务动作、涉及对象、关键技术手段。
- 第二部分 [详情]：完整技术解读，说明方法职责、关键逻辑、与上下游调用的关系；不要大段重复粘贴源码。

### 上下文
{context_summary}

### 方法签名
{signature}

### 方法体（节选）
```
{code_snippet[:10000]}
```

### 请输出（严格按以下格式）
[摘要] <关键词1 关键词2 关键词3 ...>

[详情]
<完整技术解读>
```

### 4.2 英文 Prompt（`language` 以 `en` 开头）

```
You are a senior Java engineer. Based on the following CLASS/CALL-CHAIN CONTEXT and METHOD CODE, produce a two-part interpretation.

Requirements:
- Part 1 [Summary]: Keyword-dense, max 50 characters, space-separated key phrases. Include: business actions, involved objects, key technical approaches. No full sentences.
- Part 2 [Detail]: Full technical interpretation covering responsibility, key logic, and call-graph relationships. Do not dump the raw code again.

### Context
{context_summary}

### Signature
{signature}

### Method body (excerpt)
```
{code_snippet[:10000]}
```

### Output (strict format)
[Summary] <keyword1 keyword2 keyword3 ...>

[Detail]
<full technical interpretation>
```

**变量替换**：注意中英文均无 trailing 换行（f-string 以 `<完整技术解读>` / `<full technical interpretation>` 结尾，无尾随 `\n`）。`code_snippet` 在 prompt 内截断 `[:10000]`。

**`context_summary` 来源（`_build_method_context`，6 行 `"\n".join`）**：

```
所属类 ID: {class_id 或 "未知"}
类名: {class_name 或 "未知"}              # = method.attributes.class_name
方法签名: {sig 或 method.name}            # sig = method.attributes.signature
模块: {method.module_id 或 ""}
直接调用本方法的上游方法（节选）: {callers[:5] 用 ", " 连接，无则"无"}
本方法直接调用的下游方法（节选）: {callees[:8] 用 ", " 连接，无则"无"}
```

- callers/callees 元素是 `_entity_name_by_id`（取 entity.name，找不到回退 eid），**非 id**。
- 连接符是 `", "`（逗号+空格）。
- prompt 里 `signature` 这一行用的 `sig` 是 `method.attributes.signature or method.name`（在 `_process_one` 内单独算一次，与 context 内的 sig 同源）。
- `context_summary` 传 persist 前截断 `[:4000]`；prompt 内不另截。

---

## 5. LLM 输出解析（`interpret_one_llm_embed_store` + helpers）

1. **去 think**：`clean_think_tags` = `re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()`（注意末尾 `.strip()`）。
2. **提取摘要 `extract_summary`**：
   - 常量 `SUMMARY_PREFIX = "[摘要]"`、`DETAIL_PREFIX = "[详情]"`（**中英文摘要都用同一对中文标记 `[摘要]`/`[详情]` 切分**，英文 prompt 产出的 `[Summary]`/`[Detail]` 不会命中该分支，会落到回退逻辑）。
   - 命中 `[摘要]` → 取其后；若含 `[详情]` 取到 `[详情]` 前，否则取首个 `\n` 前；`strip()`；`len > 50` 截断 `[:50]`；非空则返回。
   - 回退：遍历行，`line.strip().lstrip("#*- ")`，首个 `len >= 5` 的行返回 `[:50]`。
   - 兜底：`text[:50]`。
3. **向量化**：只对 `summary` 做 `get_embedding(summary, embedding_dim)`；完整 `text`（去 think 后的全文）存库。
4. **维度**：`embedding_dim` 来自 runner 的 `dim = int(vinterp.dimension) if vinterp.dimension else 64`（dimension 默认 1024，仅 0/假值退 64）。

`get_embedding` 签名：`get_embedding(text: str, dimension: int = DIM) -> list[float]`。

---

## 6. 落库结构

**Collection**：`TopologicalInterpretation`（`DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION`，可被 `vinterp.collection_name` 覆盖）。

**UUID 规则**（精确，跨语言必须对齐）：
```
uid = _to_uuid(method_entity_id + "|interpret")
_to_uuid(s):
    h = sha256(s.encode("utf-8")).hexdigest()[:32]    # 取 hex 前 32 字符
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
```
即 SHA-256 十六进制摘要前 32 字符按 8-4-4-4-12 拼成 UUID 字符串。**TS 端必须 `sha256(utf8(method_entity_id + "|interpret")).hex().slice(0,32)` 后同样切片**，否则幂等/孤儿清理全失效。

**Tenant**：写入路径 `_resolve_collection(tenant)` → `tenant` 非空走 `coll.with_tenant(tenant)`；`tenant=None` 打 deprecation warning 后返回无 tenant 的 `coll`。runner 透传 `tenant=project_id`。

**Schema 属性（写入时的截断在 `add_with_created` 内执行）：**

| 字段 | 类型 | 写入截断 | 来源 |
|---|---|---|---|
| `method_entity_id` | TEXT | 无 | `method.id` |
| `class_entity_id` | TEXT | 无（`or ""`） | CONTAINS 上游 class/interface 的 `id` |
| `class_name` | TEXT | `[:500]` | `method.attributes.class_name` |
| `method_name` | TEXT | `[:300]` | `method.name` |
| `signature` | TEXT | `[:2000]` | `method.attributes.signature or method.name` |
| `interpretation_text` | TEXT | `[:48000]` | 去 think 后完整 LLM 文本 |
| `context_summary` | TEXT | `[:12000]`（schema 写入截断） | 但 runner 在 persist 前已先 `[:4000]`，故实际最多 4000 |
| `language` | TEXT | 无（`or "zh"`） | `"en" if lang.startswith("en") else "zh"` |
| `related_entity_ids_json` | TEXT | `[:8000]` | `json.dumps(related_ids, ensure_ascii=False)`，最多 24 个 id |

**向量**：写入用 `vector[:self._dim]`；前置校验 `if not vector or len(vector) < self._dim: return False, False`（不足维度直接拒绝）。

**写入策略（upsert）**：先 `coll.data.insert(properties, vector, uuid)`；catch 后若 `"already exists" in str(e).lower() or "422" in str(e)` → `coll.data.replace(uuid, properties, vector)`；replace 成功返 `(True, False)`，失败 warning 返 `(False, False)`；其它 insert 异常 warning 返 `(False, False)`。返回值 `(成功, 是否新建)`：insert 成功 `(True, True)`、replace 成功 `(True, False)`。

---

## 7. 候选选取

```
all_methods = [
    e for e in structure_facts.entities
    if e.type == EntityType.METHOD
    and (e.attributes or {}).get("code_snippet")    # 必须非空 code_snippet
    and not _is_trivial_accessor(e)                 # 排除 getter/setter
]
```

**`_is_trivial_accessor`：**
1. `attrs.get("is_getter") or attrs.get("is_setter")` 为真 → True（结构层 AST 标记优先）。
2. 回退启发式：`name = method.name.strip()`，`sig = (attributes.signature or name).strip()`；若 sig 无 `(` 或 `)` → False；取括号内 `inside`，按 `,` 分割并过滤空白项得 `params`；
   - `name.startswith("get") or name.startswith("is")` 且 `not params` → True
   - `name.startswith("set")` 且 `len(params) == 1` → True
   - 否则 False

---

## 8. 并发与批次（魔法数字）

| 参数 | 值/来源 |
|---|---|
| `max_workers` | `max(1, int(getattr(mi, "max_workers", 4) or 4))`，默认 4 |
| `batch_size` | `max(max_workers * 2, 20)`（默认 = max(8,20)=20） |
| `timeout_sec` | `int(mi.timeout_seconds or 120)`，作为 kwarg 传 `generate` |
| `max_callers` | 5（`_build_method_context` 默认参数） |
| `max_callees` | 8（同上） |
| `related_ids` 上限 | 24（`list(rid_set)[:24]`） |
| `min_text_len` | 10（`_process_one` 调用处硬编码） |
| `page_size`（list ids 分页） | 2000 |
| `MethodInterpretationStoreAdapter.list_existing_keys` 默认 limit | **100000**（覆盖 protocol 的 200000 默认） |
| `count()` fallback limit | **200000**（store 层 `list_existing_method_ids(limit=200000)`） |
| context_summary 截断 | persist 前 `[:4000]`；schema 再 `[:12000]`（实际 4000 生效） |
| progress 百分比 | `min(85 + int(15 * processed_count / total), 99)`；最后 runner 收尾发 `progress(100,100,"流水线全部完成")` |
| 预估时间提示 | `total * 25 / max_workers / 60` 分钟（仅 `total > 1` 时打印） |

**并发模型**：外层 `for batch_start in range(0, total, batch_size)`，**每批新建一个 `ThreadPoolExecutor(max_workers=max_workers)`**（with 语句，批结束即关池）；`futures = {pool.submit(_process_one, m): m}`；`as_completed` 收集。`future.result()` 抛异常 → `_LOG.exception(...)` 计 `(0,1)`。`ok/fail/processed_count` 用 `threading.Lock()`（`counter_lock`）保护，且为闭包 `nonlocal`。

---

## 9. 断点续跑

```
total_candidates = len(all_methods)
existing_ids     = store.list_existing_keys()      # = store.list_existing_method_ids(limit=100000)
already_done     = sum(1 for m in all_methods if m.id in existing_ids)   # 用孤儿清理后的 existing_ids
todo_methods     = [m for m in all_methods if m.id not in existing_ids]
if max_m > 0:
    todo_methods = todo_methods[:max_m]
```

**`list_existing_method_ids`（store 层细节）：**
- 通过 **`_get_collection()`（无 tenant！）** 分页 `fetch_objects(limit=cur_limit, offset=fetched, return_properties=["method_entity_id"])`，`page_size=2000`，`fetched` 递增至 `target=max(0,int(limit))`。
- 返回空批或 `len(objs) < cur_limit` 即停。
- SDK 不支持 `offset`/`return_properties`（`TypeError`）→ 退化单页 `fetch_objects(limit=target, ...)`（再 TypeError 退到只传 limit）后 `break`。
- 整体异常 → 返回已收集的 ids（部分集合）。

> **⚠ tenant 错配（核心怪癖）**：写入走 `with_tenant(project_id)`，但本读取走 `_get_collection()`（default 分区）。在真正启用 multi-tenancy 的 Weaviate 上，tenant 分区写入的对象**不会**出现在 default 分区的 `fetch_objects` 里 → `existing_ids` 恒空 → 每轮全量重跑、断点续跑失效。TS 移植必须决策：要么读取也带 tenant（推荐，修正此 bug），要么逐字复刻该行为（不推荐）。本规范记录现状如上，**移植时应统一读/写/删 tenant**。

已存在解读的方法在 `publish_item_list` 中预标记 `done=True`（见 §13 第 4 条）。

---

## 10. 一致性同步（孤儿清理）

在计算 todo **之前**、每次执行都跑（与 `max_methods` 无关）：

```
valid_method_ids = {e.id for e in all_methods}      # structure_facts 真相源
existing_ids = store.list_existing_keys()           # 见 §9（无 tenant 读取）
orphan_ids = existing_ids - valid_method_ids
if orphan_ids:
    runner.step("一致性同步：发现 N 条孤儿解读…")
    for oid in orphan_ids:
        try:
            uid = weaviate_store._to_uuid(oid + "|interpret")
            weaviate_store._get_collection().data.delete_by_id(uid)   # ⚠ 无 tenant
        except Exception:
            pass                                     # 删除失败静默，不影响主流程
    existing_ids -= orphan_ids
    runner.step("一致性同步：已清理 N 条孤儿")
```

- 触发：method_entity_id 因重构/重命名不再出现在 structure_facts → 旧 id 成孤儿。
- 清理后 `existing_ids` 立即修正，本轮断点续跑以修正集合为准。
- **⚠ tenant 错配（同 §9）**：删除走 `_get_collection().data.delete_by_id(uid)`（无 tenant），与写入的 tenant 分区不一致 → 真启用 multi-tenancy 时删不到 tenant 分区里的对象。TS 移植应改为 `with_tenant(project_id).data.delete_by_id(uid)`。

---

## 11. method_entity_id 规范化（`method_entity_id_normalize.py`）

- **写入侧**：直接用 `method.id` 原值（结构层多为 `method//...`），不 normalize。
- **`normalize_method_entity_id(eid)`**：`method://` 原样；`method//` → 替换前缀为 `method://`；其它原样。**仅用于展示/合并对齐，不用于写入**。
- **`method_entity_id_variants(eid)`**（读取兼容，最多 2 变体）：

| 输入 | 输出 |
|---|---|
| 空 | `[]`（**空字符串返回空 list，非单元素**） |
| `method://X` | `["method://X", "method//X"]` |
| `method//X` | `["method//X", "method://X"]` |
| 其它 | `[原值]` |

读取方法 `get_by_method_id` / `get_by_entity_with_tenant` 依次尝试 variants；后者带 `.with_tenant(tenant)`。

---

## 12. `_build_method_context` 的 `related_ids` 收集

```
rid_set = {method.id}
if class_id: rid_set.add(class_id)             # class_id 来自 CONTAINS 上游 class/interface
for r in facts.relations:
    if r.type == CALLS and r.source_id == method.id: rid_set.add(r.target_id)   # 下游
    if r.type == CALLS and r.target_id == method.id: rid_set.add(r.source_id)   # 上游
related_ids = list(rid_set)[:24]
```

`class_id` 解析：遍历 relations 找 `CONTAINS` 且 `target_id == method.id`，其 `source_id` 对应 entity 是 `CLASS`/`INTERFACE` 则取为 class_id（首个命中即 break）。

---

## 13. 诊断 / 工厂 fallback / 怪癖

- **诊断提示**（仅 `already_done == 0 and total_candidates > 0 and runner.step_callback`）：
  - `store.count() > 0 and not existing_ids` → 提示连接问题，全量处理。
  - `store.count() > 0 and existing_ids` → 提示 entity_id 与当前结构事实不匹配（疑似非同一项目），全量处理。
- **`interpretation_stats_callback`**：用 `store.count()`（= `coll.aggregate.over_all(total_count=True).total_count`，失败 fallback `len(list_existing_method_ids(limit=200000))`，再失败 0）作真实进度，**不依赖 structure_facts id 匹配**，避免 id 漂移导致进度恒 0。
- **`count()` 也走 `_get_collection()`（无 tenant）** —— 同 §9 tenant 错配，真 multi-tenant 时 count 反映的是 default 分区。
- **`publish_item_list` 传的是 tuple 列表**：`[(_method_display_label(m), m.id in existing_ids) for m in all_methods]`，已存在解读的预标 `done=True`。`_method_display_label` = `f"{sig}（{cls}）"`（cls 非空）或 `sig`。
- **LLMProviderFactory fallback 全貌**：
  - openai/anthropic 库 `ImportError` 且 `allow_fallback=False` → **抛 `RuntimeError`**（提示 pip install）；`allow_fallback=True` → 回退 ollama，`fallback_reason` 记原因，`resolved_backend="ollama"`。
  - `multi` 缺 `multi_providers` 列表 → 抛 `ValueError`；子项未知 backend → 抛 `ValueError`。
  - **未注册 backend（含拼写错误）→ 静默回退 ollama**（`resolved_backend=requested`、`fallback_reason=""`），与历史行为一致，不抛错。
  - runner 用 `llm_sel.resolved_backend` 显示、`llm_sel.fallback_reason` 追加 step。

---

## 14. 关闭 / 资源管理

`run_method_interpretations` 末尾 `finally`：`if store is not None: store.close()`；catch `OSError` → `_LOG.warning(...)`；其它 `Exception` → `_LOG.exception(...)`；均不上抛。

`BaseWeaviateStore.close()` 关闭 client（吞异常）；`__del__` 调 close（吞异常）。`clear()` 删 collection 后重建 schema（吞异常）。

---

## 15. MultiProvider 轮询 + 限流退避（`multi_provider.py`，原 spec 缺失）

**用途**：把多个 provider 组合为一个，按 round-robin 分发；典型场景 Qwen + MiniMax 双 Coding Plan 并用。

**构造**：`MultiProvider(providers: list[LLMProvider], names: list[str] | None)`；`providers` 空抛 `ValueError`；`names` 缺省为 `provider-{i}`；内部用 `itertools.cycle(range(n))` 做轮询游标、`threading.Lock` 保护游标/计数、`_call_counts` 逐 provider 计数、`_backoff_count` 统计退避次数。

**退避序列**：`_BACKOFF_DELAYS = [30, 60, 120]`（秒）。

**`generate(prompt, **kwargs) -> str` 算法**：
```
for attempt in range(len(_BACKOFF_DELAYS) + 1):     # 最多 1 + 3 = 4 轮
    start_idx = next(self._cycle)                    # 加锁取轮询起点
    all_rate_limited = True
    for offset in range(n):                          # 从 start_idx 起绕一圈试所有 provider
        idx = (start_idx + offset) % n
        try:
            result = providers[idx].generate(prompt, **kwargs)
            call_counts[idx] += 1                    # 加锁
            return result                            # 任一成功立即返回
        except Exception as e:
            if not _is_rate_limit_error(e): all_rate_limited = False
            last_error = e
            continue                                 # 失败立即切下一个
    if not all_rate_limited:
        break                                        # 存在非限流错误 → 不退避，跳出
    if attempt < len(_BACKOFF_DELAYS):
        delay = _BACKOFF_DELAYS[attempt]
        sleep_time = delay + random.uniform(0, 5)    # 抖动 0-5s
        backoff_count += 1                           # 加锁
        time.sleep(sleep_time)
    # else: 最后一轮仍限流 → 落到循环外
raise RuntimeError(f"MultiProvider: 所有 {n} 个 Provider 均失败") from last_error
```

**限流判定 `_is_rate_limit_error(err)`**（`str(err).lower()` 命中任一）：`"429"` / `"rate_limit"` / `"rate limit"` / `"throttling"` / `"quota exceeded"` / `"usage limit"` / `"too many requests"`。

**关键语义**：
- 轮询起点每轮 `next(cycle)` 推进；一轮内绕一圈试完所有 provider。
- 单 provider 失败 → 立即切下一个（不退避）。
- 只有**一整轮全部是限流错误**才退避（30→60→120s + 0-5s 抖动），最多 3 次退避（共 4 轮尝试）。
- 出现任何**非限流**错误 → 立即跳出、抛 `RuntimeError`（链 `last_error`）。
- `stats()` 返回 `{name: count, "_backoff_count": n}`。

**TS 移植注意**：`time.sleep` 在线程池里阻塞 worker；TS 端在 `@ke/agent` provider 层若用 async 应改 `await sleep`。round-robin 游标须跨并发线程共享并加锁（对应 §8 ThreadPoolExecutor 多 worker 同时调 `generate`）。

---

## 关键移植红线（汇总）

1. **`_to_uuid` = SHA-256 hex[:32] 按 8-4-4-4-12 切片**，输入 `method_entity_id + "|interpret"`。逐字节对齐，否则幂等失效。
2. **tenant 三处错配**：写带 `project_id`，但 `list_existing_method_ids` / 孤儿 `delete_by_id` / `count` 都走无 tenant 的 `_get_collection()`。移植时**统一改为带 tenant 读/删/计数**（修 bug），不要逐字复刻。
3. **prompt 全文逐字**（§4），含中英两套，`code_snippet[:10000]`、无尾随换行。
4. **summary 切分始终用中文 `[摘要]`/`[详情]`** 标记，英文输出走回退行逻辑。
5. **每批新建线程池**、`batch_size = max(max_workers*2, 20)`、`min_text_len=10`、向量不足维度拒写。
6. **MultiProvider** round-robin + `[30,60,120]` 退避 + 限流关键词判定（§15）。

源码文件（绝对路径）：
- `/Users/java/knowledge-engineering/src/knowledge/base_interpretation_runner.py`
- `/Users/java/knowledge-engineering/src/knowledge/method_interpretation_runner.py`
- `/Users/java/knowledge-engineering/src/knowledge/interpretation_item_helpers.py`
- `/Users/java/knowledge-engineering/src/knowledge/weaviate_interpretation_store.py`
- `/Users/java/knowledge-engineering/src/knowledge/interpretation_store_adapter.py`
- `/Users/java/knowledge-engineering/src/knowledge/method_entity_id_normalize.py`
- `/Users/java/knowledge-engineering/src/knowledge/base_weaviate_store.py`
- `/Users/java/knowledge-engineering/src/knowledge/llm/factory.py`、`multi_provider.py`、`ollama.py`、`openai_provider.py`、`anthropic_provider.py`、`protocol.py`
- `/Users/java/knowledge-engineering/src/config/models.py`（行 143-179）
- `interpretation_runner_inputs.py` **不存在**（已内联进 runner）

---

# 【拓扑解读（批量）+ MultiProvider 轮询】

I now have everything I need to do the adversarial verification. The critical finding: `_to_uuid` is **SHA256-based**, NOT UUID v5. The spec says "UUID v5 / deterministic hash" which is wrong/ambiguous. Let me also verify a few specific claims against the source before writing the corrected spec.

Key cross-checks completed:
- `_to_uuid(s)` = SHA256(s)[:32] formatted as UUID — **NOT uuid5**.
- Multi-tenancy: `_resolve_collection(tenant)` exists; `add_with_created` takes `tenant=None` default and calls `_resolve_collection`. The interpreter never passes tenant (legacy path). The TenantBoundStore overrides `_get_collection()`, but `add_with_created` uses `_resolve_collection` not `_get_collection`! This is a **critical bug in the spec's tenancy story** — let me re-verify.

Looking at `add_with_created`: it calls `coll = self._resolve_collection(tenant)`. With `tenant=None` (interpreter never passes it), `_resolve_collection(None)` returns `self._get_collection()` (after a deprecation warning). And `_get_collection()` IS overridden by TenantBoundStore to add `.with_tenant`. So the write path IS tenant-scoped via the override. Good — the spec's mechanism holds for writes too, because `_resolve_collection(None)` falls through to `_get_collection()`.

Now I have enough to produce the corrected spec.

---

## 修正清单

逐条回源核验结果（标注 ✅ 正确 / ❌ 错误 / ⚠️ 不准确或遗漏）：

1. **❌ UUID 生成算法严重错误（最高优先）**。spec §9 说「`toUUID(...)` 需用 UUID v5 / deterministic hash」。源码 `base_weaviate_store.py:30-33` 是 **SHA256**：`h = hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]`，再格式化为 `h[:8]-h[8:12]-h[12:16]-h[16:20]-h[20:32]`。**不是 UUID v5（uuid5 用 SHA-1 + namespace）**。TS 必须用 `crypto.createHash('sha256')` 复现，否则 UUID 不一致 → 断点续跑/孤儿清理/upsert 全部失效。已改正。

2. **❌ 多租户写路径机制描述不全 / spec 落库结构遗漏 tenant 维度**。spec §13 说「`add_with_created` 内部全程不传 tenant 走 legacy」，但实际 `add_with_created(tenant=None)` → `_resolve_collection(None)` → 返回 `self._get_collection()`（仅打 deprecation warning）。`TenantBoundStore` 重写的是 `_get_collection()`，所以**写路径确实被 tenant-scoped**（经由 `_resolve_collection` 回落到 `_get_collection`）。spec 漏了 `_resolve_collection` 这一中间层，且 §9「写入必须指定 tenant」对原始 `TopologicalInterpreter` 不成立（它从不传 tenant，靠 store override）。已补正。

3. **❌ DDL 解析正则与「同时解析 SQL」描述不准**。spec §3 说「同时解析仓库内 `**/*.sql` 的 CREATE TABLE DDL」，但顺序是**先**调 `load_dao_sql_for_repo`，**再** `_load_table_ddls()`。DDL 正则是 `r'CREATE TABLE\s+\`(\w+)\`\s*\((.*?)\)\s*ENGINE'`（DOTALL），**强制要求反引号包裹表名 + 以 `ENGINE` 结尾**，存的是 `match.group(0)`（含 CREATE TABLE...ENGINE 整段），不是「列定义部分」。已改正。

4. **⚠️ `fieldComments` 键类型**。spec §1/§8 用 `"ClassName.fieldName"` 字符串拼接键。源码用 **元组键** `dict[tuple[str, str], str]`，键来自 FIELD 实体的 `attributes.class_name` + `e.name`（**实体名 `e.name`，不是 attributes 里的 field name**）。TS 可用 `"class\x00field"` 或 `Map<string, Map>`，但务必区分。已标注。

5. **⚠️ `class_name` 来源**。spec §6 `_interpretOne` 写库用 `method.attributes.class_name`，§8 `_buildContextSummary` 同。但 `_buildIndices` 里 `_class_map` 也存了 `attrs.class_name`——`_class_map` 实际在源码中**从未被读取**（dead field）。TS 不必移植 `_class_map`。已标注。

6. **❌ `_interpret_level` 的 `total_todo`/`processed_before` 参数被 spec 漏掉**。源码签名是 `_interpret_level(method_ids, level, processed_before, total_todo)`，且 `_run_layer_with_gate` 调用时传 `(todo, level, 0, len(todo))`——`processed_before` **恒为 0**，`total_todo = len(todo)`。进度回调 `done = processed_before + ok + fail`。spec §5 的伪代码省略了这两个参数。已补正。

7. **⚠️ 进度上报条件**。源码是 `(ok + fail) % 50 == 0 or (ok + fail) == len(method_ids)`，其中 `len(method_ids)` 是**本轮 todo 总数**（非批大小）。spec「每 50 条或批末尾」不准——是「每 50 条或本轮全部处理完」。已改正。

8. **✅ Prompt 全文**：中文 + 英文 prompt 均逐字核对，与源码 `topological_interpreter.py:608-678` 完全一致。保留全文。

9. **❌ 英文 prompt 的 bug 描述方向错误**。spec §7.2/§16 说「英文分支引用了未赋值的 `bean_section`」。实际源码：`bean_section` 在英文分支（line 603-629）和中文分支（line 632-638）**都**被引用在 f-string 里（`{callee_section}{bean_section}`），但 `bean_section` 变量**只在中文分支 line 636-638 才赋值**。英文分支在 line 619 引用 `{bean_section}` 时该变量**尚未定义** → Python 运行时 `NameError`（不是「未赋值的变量」而是 `NameError: name 'bean_section' is not defined`）。**英文分支实质从未被成功执行过**（`language` 默认 `"zh"`，CodeGraph 模式也是 zh，所以从未触发）。TS 修复方向 spec 给对了（先算 `beanSection` 再拼），但 bug 性质要说清。已改正。

10. **⚠️ `_get_related_ids` 包含 self + CONTAINS source**。spec §6 调 `_getRelatedIds` 但未在辅助函数节定义。源码 `_get_related_ids` 返回 `{method_id}` ∪ CALLS 双向邻居 ∪ CONTAINS 的 source（即所属类）。`set` 无序，`[:24]` 截断**顺序不确定**——TS 用 `Set` 迭代序复现即可，无需排序（原实现也未排序）。已补充函数定义。

11. **⚠️ MultiProvider 轮询是全局 `itertools.cycle`，不是「线程安全轮询」的简单取模**。源码用 `itertools.cycle(range(n))` + `threading.Lock` 保护 `next()`。每次 `generate` 调用消耗一个 cycle 值作为 `start_idx`。TS 需用一个受锁/原子保护的递增计数器 `counter++ % n`。已标注。

12. **✅ MultiProvider 退避/限流检测**：`_BACKOFF_DELAYS=[30,60,120]`、`jitter=random.uniform(0,5)`、`for attempt in range(len(_BACKOFF_DELAYS)+1)`（共 4 轮，最后一轮只 log 不 sleep）、限流关键词 7 个、`stats()` 返回 `{name: count, "_backoff_count": n}` 全部核对一致。已保留。

13. **⚠️ factory 默认 backend 回落**：未注册的 backend **一律按 ollama 解析**（`create_with_meta` line 200-204）。`multi` 子 provider 的 `name` 缺省为 `f"{sub_backend}-{i}"`（不是 `provider-{i}`）；MultiProvider 内部缺省名才是 `provider-{i}`。已标注。

14. **⚠️ `embedding_dim` / store `dimension` 默认值冲突**。`TopologicalInterpreter` 默认 `embedding_dim=1024`；但 `WeaviateTopologicalInterpretStore.__init__` 默认 `dimension=64`。生产由 `run_codegraph_interpret._build_store` 用 env `WEAVIATE_DIMENSION`（默认 "1024"）覆盖。`add_with_created` 用 `self._dim`（store 的 dimension）做 `vector[:self._dim]` 截断和 `len(vector) < self._dim` 校验——**两个 dim 必须一致**否则写入被拒。spec §15 只列了 interpreter 的 1024，漏了 store 默认 64 的坑。已标注。

15. **✅ 字段截断长度**：class_name 500 / method_name 300 / signature 2000 / interpretation_text 48000 / context_summary 12000 / related_ids_json 8000，与 `add_with_created` line 137-142 逐字一致。注意 `related_entity_ids_json[:8000]` 直接切（无 `or ""` 兜底），TS 入参须保证非 null。

16. **⚠️ `min_text_len` 两次校验**：源码先 `if not raw_text or len(raw_text) < 10`（对 raw），清洗后再 `if len(text) < 10`（对 cleaned）。spec §6 写对了，但魔法数字 §15 只列一次。已确认两处都是 10。

17. **✅ 孤儿清理用 `_get_collection().data.delete_by_id(uid)`**（非 tenant-scoped store 时全局；TenantBoundStore 时自动 tenant-scoped，因 override）。`uid = _to_uuid(oid + "|interpret")`。逐条 try/except 静默。一致。

---

# 拓扑解读 Runner (TopologicalInterpreter) — TS 重写规范（修正版）

**源文件**：
- `/Users/java/knowledge-engineering/src/knowledge/topological_interpreter.py`（802 行，主体）
- `/Users/java/knowledge-engineering/src/knowledge/llm/multi_provider.py`（114 行）
- `/Users/java/knowledge-engineering/src/knowledge/llm/factory.py`（204 行）
- `/Users/java/knowledge-engineering/src/knowledge/weaviate_interpretation_store.py`（落库 store）
- `/Users/java/knowledge-engineering/src/knowledge/interpretation_item_helpers.py`（辅助）
- `/Users/java/knowledge-engineering/src/knowledge/method_entity_id_normalize.py`（id variants）
- `/Users/java/knowledge-engineering/src/knowledge/base_weaviate_store.py`（`_to_uuid` / MT）
- `/Users/java/knowledge-engineering/run_codegraph_interpret.py`（CLI 入口，CodeGraph 重生路径）

---

## 1. 组件用途

`TopologicalInterpreter` 是自底向上分层解读引擎（「大厦理论」）：

- 把代码工程视为一座大厦，每个方法是一块砖
- 先解读叶子方法（在 meaningful 范围内无下游调用，L0），再逐层往上
- 上层方法 prompt 注入下层 summary，让 LLM 在理解子调用业务含义的基础上解读上层
- 输出写入 Weaviate `TopologicalInterpretation` collection（QA 召回读取的数据）

**两种运行模式**（物理隔离，引擎逻辑完全一致，差别仅在 `entity_id` 来源 + state 文件名 + store 连接来源）：

| 模式 | 源 | entity_id 格式 | CLI 入口 | state 文件 |
|---|---|---|---|---|
| canonical_v1（结构分析） | StructureFacts（解析器产物） | `method//` 前缀 hash | run_topological_interpret.py | `out_ui/interpretation_state.json` |
| CodeGraph 重生（2b） | CodeGraphFactsProvider | `qualified_name`（全限定类名.方法名，无前缀） | run_codegraph_interpret.py | `out_ui/2b_interp_state_{project_id}.json` |

---

## 2. 公开函数签名

### 构造参数

```typescript
interface TopologicalInterpreterOptions {
  structureFacts: StructureFacts;        // 调用图/方法实体来源（CodeGraph 模式下也产出 StructureFacts）
  llm: LLMProvider;                      // generate(prompt, { timeout }) → Promise<string>
  weaviateStore: WeaviateInterpretStore;
  language?: string;                     // 默认 "zh"
  embeddingDim?: number;                 // interpreter 默认 1024（注意：store dimension 默认 64，必须对齐，见 §15 第14条）
  maxWorkers?: number;                   // 默认 8
  llmTimeout?: number;                   // 默认 90（秒）
  repoPath?: string;                     // 默认 ""（空则跳过 DAO SQL 加载）
  layerGate?: number;                    // 默认 1.0（100%）
  maxRetryCycles?: number;               // 默认 5
  retryDelays?: number[];                // 默认 [60, 300, 1800, 3600, 7200]（秒）
  stateFile?: string;                    // 默认 "out_ui/interpretation_state.json"
  stepCallback?: (msg: string) => void;       // 默认空函数
  progressCallback?: (done: number, total: number, msg: string) => void;  // 默认空函数
}
```

### run() → Promise\<RunResult\>

```typescript
interface RunResult {
  total_methods: number;      // = meaningful.size（有效业务方法总数）
  levels: number;             // = maxLevel + 1（金字塔总层数）
  already_done: number;       // 启动时 existingIds ∩ meaningful 的数量
  ok: number;                 // 本次成功写入累计
  fail: number;               // 本次失败累计
  permanent_failed: Record<string, number>;  // { "L0": 3, "L1": 1 }（每层永久失败数）
  elapsed_minutes: number;    // round(elapsed/60, 1)
}
```

---

## 3. 算法步骤（run() 主流程）

### Step 1：构建索引 `_buildIndices()`

遍历 `StructureFacts.entities`：

- `EntityType.METHOD` → `methods: Map<string, StructureEntity>`（method_id → 实体）；同时建 `classMap`（method_id → `attributes.class_name`，**注意：此 map 在原实现中从未被读取，TS 可省略**）。
- `RelationType.CALLS` → `callGraph: Map<string, Set<string>>`（caller→callees）+ `reverseGraph`（callee→callers）。**仅当 `src !== tgt` 且 src、tgt 都在 `methods` 内**才建边（排除自调用 + 排除非方法实体的边）。
- `EntityType.FIELD` 且 `attributes.comment` 非空 → `fieldComments`，键 = **元组 `(attributes.class_name, e.name)`**（FIELD 实体的 `e.name` 作为字段名），值 = comment。

### Step 2：过滤有效方法 `_filterMeaningful()`

排除满足任一条件的方法：

- `attributes.code_snippet` 为空/falsy
- `attributes.is_getter` 为真
- `attributes.is_setter` 为真

返回 `meaningful: Set<string>`。

### Step 3：加载 DAO SQL `_loadDaoSql()`（仅当 `repoPath` 非空）

1. 调 `load_dao_sql_for_repo(repoPath, {})`（插件，失败则 warning 后 return，不抛）。
2. `_loadTableDdls()`：`glob` 仓库 `**/*.sql`，逐文件读（`errors="ignore"`），正则 `CREATE TABLE\s+\`(\w+)\`\s*\((.*?)\)\s*ENGINE`（DOTALL）匹配。**表名必须被反引号包裹，且必须以 ENGINE 结尾**。存 `tableDdls[table_name] = match.group(0)`（**整段 CREATE TABLE...ENGINE 文本**，不裁列）。
3. 建 `nameToIds: Map<(class_name, method_name), string[]>`（method_id 列表，可一对多）。
4. 对每个 sql_result（键为 `namespace.methodName`）：`namespace = key.rsplit(".",1)[0]`，`class_name = namespace.rsplit(".",1)[-1]`（namespace 末段）。对 `[class_name, class_name.replace("Dao","Mapper"), class_name.replace("Mapper","Dao")]` 三种 class 名 + method_name 查 `nameToIds`，命中则 `sqlIndex[mid]=annotated_sql`、`sqlTables[mid]=tables||[]`。

### Step 4：拓扑分层 `_computeLevels(meaningful)`

BFS 自底向上：

- `outDegree[mid] = |callGraph.get(mid) ∩ meaningful|`（**仅 meaningful 之间的边**）。
- 叶子（outDegree==0）→ level 0，入队。
- BFS：弹出 node，对 `reverseGraph.get(node) ∩ meaningful` 的每个 caller：`newLevel = levels[node]+1`；若 `caller ∉ levels || levels[caller] < newLevel` → 更新并入队。
- 遍历结束后，仍不在 `levels` 的 meaningful 节点（孤立）→ level 0。
- 产出 `levels: Map<string,number>`；run() 据此构建 `levelGroups: Map<number,string[]>`，`maxLevel = max(values)`（空则 0）。

### Step 5：一致性同步（孤儿清理）

```
existingIds = store.list_existing_method_ids(limit=200000)   // try/except，失败则 existingIds = ∅
orphans = existingIds - meaningful
for oid of orphans:
    try:
        uid = store._to_uuid(oid + "|interpret")              // SHA256，见 §9
        store._getCollection().data.deleteById(uid)
    catch: 静默忽略
```

### Step 6：断点续跑初始化

```
alreadyDone = existingIds ∩ meaningful
for mid of alreadyDone:
    rec = store.get_by_method_id(mid)
    if rec: summaryCache.set(mid, extractSummary(rec.interpretation_text || ""))   // 加锁
prevState = loadState(stateFile)   // 见 §10
```

### Step 7：逐层解读（带门禁）

```
totalOk = totalFail = 0
totalPermanentFailed: Map<level, Set>
for lv of 0..maxLevel:
    methodsAtLevel = levelGroups.get(lv) || []
    permanentFailed = new Set(prevState["L{lv}"]?.permanent_failed || [])   // 从历史状态恢复
    try:
        [ok, fail, pf] = await _runLayerWithGate(lv, methodsAtLevel, permanentFailed)
        totalOk += ok; totalFail += fail; totalPermanentFailed.set(lv, pf)
    catch RuntimeError:        // Gate 未达标
        saveState(lv, "GATE_FAILED", permanentFailed)
        totalPermanentFailed.set(lv, permanentFailed)
        break                  // 终止后续所有层
```

---

## 4. 层级门禁算法（_runLayerWithGate(level, methodsAtLevel, permanentFailed)）

```
totalMethodsCount = methodsAtLevel.length
totalOk = totalFail = 0; cycle = 0
while true:
  doneIds = store.list_existing_method_ids()           // 每轮同步最新状态

  // 新完成方法装入 summaryCache（仅未缓存的）
  for mid of (doneIds ∩ methodsAtLevel) where !summaryCache.has(mid):
      rec = store.get_by_method_id(mid)
      if rec: summaryCache.set(mid, extractSummary(rec.interpretation_text || ""))  // 加锁

  todo = methodsAtLevel filter (m ∉ doneIds && m ∉ permanentFailed)

  attempting = totalMethodsCount - permanentFailed.size
  completeness = attempting > 0 ? (attempting - todo.length) / attempting : 1.0

  if todo.length == 0 || completeness >= layerGate:
      status = permanentFailed.size == 0 ? "COMPLETE" : "PARTIAL"
      saveState(level, status, permanentFailed)
      return [totalOk, totalFail, permanentFailed]

  if cycle >= maxRetryCycles:
      for m of todo: permanentFailed.add(m)
      finalAttempting = totalMethodsCount - permanentFailed.size
      finalCompleteness = totalMethodsCount ? finalAttempting / totalMethodsCount : 0
      if finalCompleteness >= layerGate:
          saveState(level, "PARTIAL_ACCEPT", permanentFailed)
          return [totalOk, totalFail, permanentFailed]
      else:
          saveState(level, "GATE_FAILED", permanentFailed)
          throw RuntimeError(`L${level} 完成率 ... 低于 Gate 阈值 ...`)

  // 跑一轮：processed_before 恒 0，total_todo = todo.length
  [ok, fail] = await _interpretLevel(todo, level, 0, todo.length)
  totalOk += ok; totalFail += fail; cycle++

  if cycle < maxRetryCycles:
      doneAfter = store.list_existing_method_ids()
      stillTodo = methodsAtLevel filter (m ∉ doneAfter && m ∉ permanentFailed)
      if stillTodo.length == 0: continue   // 下轮判断会退出
      delay = retryDelays[min(cycle-1, retryDelays.length-1)]
      await sleep(delay * 1000)
```

**Gate 状态值**：`COMPLETE` / `PARTIAL` / `PARTIAL_ACCEPT` / `GATE_FAILED`。
**注意 `finalCompleteness` 分母是 `totalMethodsCount`（含永久失败），与中途的 `completeness`（分母 attempting，不含永久失败）口径不同**——这是源码刻意的，TS 必须区分。

---

## 5. 层内并发执行（_interpretLevel(methodIds, level, processedBefore, totalTodo)）

源码用 `ThreadPoolExecutor(max_workers)` + 分批：

```
ok = fail = 0
batchSize = max(maxWorkers * 3, 20)        // 默认 = max(24, 20) = 24
for batchStart of 0, batchSize, 2*batchSize, ...:
    batch = methodIds.slice(batchStart, batchStart + batchSize)
    // 批内并发：每批新建一个 max_workers 的 pool（TS 用 p-limit(maxWorkers)）
    results = await allSettled(batch.map(mid => _interpretOne(mid, level)))
    for each result:
        success ? ok++ : fail++       // _interpretOne 抛异常 → success=false（catch 计 fail）
        done = processedBefore + ok + fail
        if (ok + fail) % 50 == 0 || (ok + fail) == methodIds.length:
            progressCallback(done, totalTodo, `L${level} ${ok+fail}/${methodIds.length}`)
return [ok, fail]
```

**注意**：进度判断里 `len(method_ids)` 是**本轮 todo 总数**（非 batchSize）。`processedBefore` 恒为 0（调用方固定传 0），`totalTodo = methodIds.length`。

---

## 6. 单条解读（_interpretOne(methodId, level)）

```typescript
async function _interpretOne(methodId, level): Promise<boolean> {
  const method = methods.get(methodId)
  if (!method) return false
  const attrs = method.attributes || {}

  const code = _getCodeWithSql(methodId)   // DAO 方法追加 SQL + DDL
  if (!code) return false

  const prompt = _buildPrompt(method, level, code)

  let rawText: string
  try {
    rawText = await llm.generate(prompt, { timeout: llmTimeout })
  } catch (e) {
    if (isRecoverable(e)) return false      // 见下；不打堆栈
    log.exception(...); return false        // 其它异常也返回 false（不抛）
  }
  if (!rawText || rawText.length < 10) return false

  const text = cleanThinkTags(rawText)      // 去 <think>...</think>
  if (text.length < 10) return false

  const summary = extractSummary(text)
  summaryCache.set(methodId, summary)       // 加锁；供上层使用

  const vec = await getEmbedding(summary, embeddingDim)   // 仅对 summary embedding

  // class_entity_id：线性扫描 relations 找 CONTAINS && targetId==methodId 的第一条，取 sourceId
  let classId = ""
  for (const r of facts.relations) {
    if (r.type === CONTAINS && r.targetId === methodId) { classId = r.sourceId; break }
  }

  try {
    const [success] = await store.addWithCreated({
      vector: vec,
      method_entity_id: methodId,
      interpretation_text: text,
      class_entity_id: classId,
      class_name: attrs.class_name ?? "",
      method_name: method.name,
      signature: attrs.signature ?? "",
      context_summary: _buildContextSummary(method),
      language: language,
      related_entity_ids_json: JSON.stringify([..._getRelatedIds(methodId)].slice(0, 24)),
    })
    return success
  } catch (e) {
    log.exception(...); return false
  }
}
```

**可恢复异常**（`INTERP_ITEM_RECOVERABLE_EXCEPTIONS`，记失败不打堆栈，来自 `interpretation_item_helpers.py`）：`urllib.error.URLError` / `TimeoutError` / `OSError` / `json.JSONDecodeError` / `KeyError`。TS 映射：网络错误 / 超时 / 文件/IO 错误 / JSON 解析错误 / 键缺失。

---

## 7. LLM Prompt 全文（逐字保留，已核对一致）

### 7.1 中文 Prompt（默认，`language.startsWith("zh")` 即非 en 分支）

```
你是企业代码业务架构师。读者是产品经理 / 业务方 / 新人开发者——**不懂**框架细节（MyBatis、BCrypt、Stream API、静态代理等）。你要把代码翻译成"这个方法做什么业务、为业务方解决什么问题"。

【输出要求】

- **[摘要]**：≤50 字，空格分隔的业务关键词。
  - ✅ 只写业务动作 + 业务对象 + 业务价值，例：`创建订单 校验库存 锁定优惠券 写入订单表 通知发货`
  - ❌ 禁止技术词：MyBatis / BCrypt / Stream / Example / Mapper / Selective / 静态代理 / 委托模式 / SQL / Hash / Redis / Spring / Cypher / Bean

- **[详情]**：用业务语言描述。读者读完应该知道"这个方法在业务流程中扮演什么角色"。
  1. **业务职责**：这个方法在整体业务里做什么？（1-2 句话回答）
  2. **业务流程**：分步骤讲"先做 X、再做 Y、最后做 Z"，每一步说**业务含义**而非技术细节
     - ❌ "调用 productMapper.updateByExampleSelective 执行选择性更新"
     - ✅ "把该品牌名下所有商品的品牌名称同步刷新，保证商品列表显示一致"
  3. **业务价值**：为什么需要这个方法？业务方关心什么？
  4. **上下游关系**：如果下游方法的解读里说了它们的**业务功能**，引用其业务功能描述本方法如何编排（不要列下游方法名）

【硬约束】

- 禁止出现"框架/技术/算法"层面的词汇（除非业务方常用，如"短信、缓存、数据库"这种概念性词）
- 禁止编造调用链——只能基于"### 下游方法功能" 段里给你的内容
- 如果上游/下游为"无"，不要硬编"通常由 Controller 调用"
- **接口契约的精确判定**：仅当方法体只有 `;` 结尾的签名行（即"int foo(Bar b);"这种纯声明、无 `{...}` 主体）时才说"此为接口契约，实际业务由实现类完成"。**只要有 `{...}` 主体——哪怕主体很短（如 @Bean 工厂方法、单行委托）——都必须正常解读业务职责**，**不要**因为代码短就退化为"接口契约"模板。

### 上下文
{context}

### 方法签名
{sig}
{callee_section}{bean_section}
### 方法体（节选）
```
{code[:8000]}
```

### 请严格按以下格式输出
[摘要] <业务关键词1 业务关键词2 业务关键词3 ...>

[详情]
<业务视角的解读>
```

变量：
- `{context}` = `_buildContextSummary(method)`（§8）
- `{sig}` = `attributes.signature || method.name`
- `{callee_section}` = 仅当 `level > 0` 且 `_buildCalleeSummaries` 非空时：`"\n### 下游方法功能（已解读）\n{calleeContext}\n"`（注意前后各一个换行）
- `{bean_section}` = 仅当 `_buildBeanFieldContext(code)` 非空时：`"\n### 代码中使用的 Bean 字段说明\n{beanContext}\n"`
- `{code[:8000]}` = 代码（DAO 已追加 SQL+DDL）截断至 8000 字符

### 7.2 英文 Prompt（`language.startsWith("en")`）

```
You are a senior Java engineer. Produce a two-part interpretation.

Requirements:
- [Summary]: Keyword-dense, max 50 chars, space-separated. Include: business actions, objects, techniques.
- [Detail]: Full technical interpretation. Leverage the called method interpretations below to explain business logic accurately.

### Context
{context}

### Signature
{sig}
{callee_section}{bean_section}
### Method body (excerpt)
```
{code[:8000]}
```

### Output (strict format)
[Summary] <keywords>

[Detail]
<interpretation>
```

英文分支 `{callee_section}` = 仅当 calleeContext 非空：`"\n### Called methods (interpreted)\n{calleeContext}\n"`。

**Bug（已核实，修正方向）**：源码英文分支（line 603-629）的 f-string 引用了 `{bean_section}`，但该变量**仅在中文分支 line 636-638 才赋值** → 英文分支运行时 **`NameError: name 'bean_section' is not defined`**。因 `language` 实际恒为 `"zh"`（CodeGraph 模式也是 zh），英文分支**从未被成功执行**。TS 修复：在语言分支**之前**先计算 `beanSection`（无 bean 时为 `""`），再分别构建 `calleeSection`，再拼 prompt。另：`extractSummary` 只识别 `[摘要]`/`[详情]`，不识别 `[Summary]`/`[Detail]`——英文 prompt 输出会走回退分支取首行。建议 TS 统一中文前缀，或为 `extractSummary` 增加双语前缀支持。

---

## 8. 辅助函数规范

### `_buildContextSummary(method)` → string

线性扫描 `facts.relations`（CALLS）收集 callers（`target_id==method.id` 的 source）与 callees（`source_id==method.id` 的 target），名字经 `_entityName`。输出（`\n` join）：

```
类名: {class_name}
方法签名: {attributes.signature || method.name}
模块: {method.module_id || ""}
上游调用方: {callers[:5].join(", ") || "无"}
下游被调用: {callees[:8].join(", ") || "无"}
```

`_entityName(entity_id)`：若在 `methods`，返回 `{class_name}.{name}`（class_name 空则仅 `name`）；否则 `entity_id.slice(0,20)`。

### `_buildCalleeSummaries(methodId, maxTotalChars=2000)` → string

- 取 `callGraph.get(methodId)`（全部 callee，**无 ∩ meaningful**，但取 method 实体；不在 methods 的跳过）。
- 跳过 getter/setter。
- 跳过 `summaryCache` 中无 summary 的（加锁读）。
- 行格式：`- {m.name}: {summary}`。
- 累计 `total + line.length > 2000` 时：追加 `- ... 还有 {callees.size - lines.length} 个方法省略` 后 break。

### `_buildBeanFieldContext(code, maxChars=1500)` → string

1. `typeMap`：先 `(\w+)\s+(\w+)\s*=\s*new\s+(\w+)` → `typeMap[g2]=g3`；再 `(\w+(?:<[^>]+>)?)\s+(\w+)\s*[=;]` → 取 `g1.split("<")[0]`，仅当首字母大写且 `g2` 未在 typeMap 时 `typeMap[g2]=t`。
2. `calls = code.matchAll(/(\w+)\.(set|get|is)(\w+)\(/g)` → `[varName, prefix, fieldPart]`。
3. 字段名还原：`fieldPart` 长度>1 且 `[0]` 大写 `[1]` 小写 → 首字母小写；`[0][1]` 都大写（如 PWD）→ 保持原样；否则原样。
4. `className = typeMap.get(varName)`，空则 skip；`(className, fieldName)` 已 seen 则 skip。
5. `comment = fieldComments.get((className, fieldName))`，空则 skip。
6. 行 `{className}.{fieldName}: {comment}`；`total + line.length > 1500` 则 break（**不追加省略提示**）。

### `cleanThinkTags(text)` → string

```typescript
text.replace(/<think>[\s\S]*?<\/think>/g, "").trim()   // 等价 Python re.DOTALL 非贪婪
```

### `extractSummary(text)` → string（来自 interpretation_item_helpers.py）

```typescript
const SUMMARY_PREFIX = "[摘要]", DETAIL_PREFIX = "[详情]"
if (text.includes(SUMMARY_PREFIX)) {
  const after = text.split(SUMMARY_PREFIX)[1]   // split 一次后取右半（Python split(prefix,1)[1]）
  let summary = after.includes(DETAIL_PREFIX) ? after.split(DETAIL_PREFIX)[0] : after.split("\n")[0]
  summary = summary.trim()
  if (summary.length > 50) summary = summary.slice(0, 50)
  if (summary) return summary
}
for (const raw of text.trim().split("\n")) {
  const line = raw.trim().replace(/^[#*\- ]+/, "")   // Python lstrip("#*- ")：去开头的 # * - 空格字符集
  if (line.length >= 5) return line.slice(0, 50)
}
return text.slice(0, 50)
```
**注意**：`lstrip("#*- ")` 是字符集剥离（去掉开头连续的任意 `#`/`*`/`-`/空格），不是前缀字符串匹配。`/^[#*\- ]+/` 正确复现。

### `_getRelatedIds(methodId)` → Set\<string\>（spec 原缺失，补充）

```
ids = {methodId}
for r of facts.relations:
  if r.type == CALLS:
    if r.source_id == methodId: ids.add(r.target_id)
    else if r.target_id == methodId: ids.add(r.source_id)
  if r.type == CONTAINS && r.target_id == methodId: ids.add(r.source_id)
return ids   // 无序 Set，[:24] 截断顺序由迭代序决定（与原实现一致即可）
```

### `_getCodeWithSql(methodId)` → string

```
m = methods.get(methodId); if (!m) return ""
code = m.attributes?.code_snippet || ""
sql = sqlIndex.get(methodId) || ""
if (!sql) return code
parts = code ? [code] : []
parts.push(`-- [MyBatis SQL]\n${sql}`)
for (table of sqlTables.get(methodId) || []) {
  ddl = tableDdls.get(table)
  if (ddl) parts.push(`-- [表结构 ${table}]\n${ddl}`)
}
return parts.join("\n\n")
```

---

## 9. 落库结构（Weaviate）

**Collection**：`TopologicalInterpretation`（`DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION`）。

**Multi-Tenancy**：collection 创建时 `Configure.multi_tenancy(enabled=True, auto_tenant_creation=True, auto_tenant_activation=True)`（base_weaviate_store.py:96-100）。

**tenant 写入机制（修正）**：`TopologicalInterpreter` 调 `store.add_with_created(...)` **从不传 tenant**。`add_with_created(tenant=None)` → `coll = _resolve_collection(None)` → 因 tenant 为 None，打 deprecation warning 后返回 `self._get_collection()`。在 **CodeGraph 模式**下 store 是 `_TenantBoundStore`，它**重写 `_get_collection()` 返回 `super()._get_collection().with_tenant(boundTenant)`**，所以写路径自动被 tenant-scoped（boundTenant = project_id）。canonical_v1 模式用普通 store，写入 legacy 无 tenant 分区。**TS 若把 store 设计为方法级 tenant 参数，需在调用层把 tenant 注入，或复制此 override 策略。**

**UUID 生成（修正——非 uuid5）**：`_to_uuid(s)` = **SHA256**：
```typescript
const h = createHash("sha256").update(s, "utf8").digest("hex").slice(0, 32)
const uid = `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20,32)}`
```
写入/删除/续跑全部用 `_to_uuid(method_entity_id + "|interpret")`。**必须用 SHA256，不能用 uuid5（uuid5 是 SHA-1+namespace，结果不同）。**

**写操作**（`add_with_created`）：
- 前置校验：`if (!vector || vector.length < store._dim) return [false, false]`。
- `vec = vector.slice(0, store._dim)`（截断到 store 维度）。
- 先 `coll.data.insert({properties, vector: vec, uuid})` → 成功返回 `[true, true]`。
- catch：若 `err.message.toLowerCase().includes("already exists") || err.message.includes("422")` → `coll.data.replace({uuid, properties, vector: vec})`，成功 `[true, false]`，失败 `[false, false]`。
- 其它异常 → warning + `[false, false]`。

**字段清单 + 截断**（`add_with_created` props）：

| 字段名 | 类型 | 截断 | 取值 |
|---|---|---|---|
| `method_entity_id` | TEXT | 无 | methodId（原值不截断） |
| `class_entity_id` | TEXT | 无 | `class_entity_id \|\| ""` |
| `class_name` | TEXT | 500 | `(class_name \|\| "").slice(0,500)` |
| `method_name` | TEXT | 300 | `(method_name \|\| "").slice(0,300)` |
| `signature` | TEXT | 2000 | `(signature \|\| "").slice(0,2000)` |
| `interpretation_text` | TEXT | 48000 | `(text \|\| "").slice(0,48000)` |
| `context_summary` | TEXT | 12000 | `(ctx \|\| "").slice(0,12000)` |
| `language` | TEXT | 无 | `language \|\| "zh"` |
| `related_entity_ids_json` | TEXT | 8000 | `related_entity_ids_json.slice(0,8000)`（**无 `\|\| ""` 兜底，入参须非 null**） |

**向量**：仅对 `summary`（`[摘要]` 提取，≤50 字）做 embedding，维度 = `embeddingDim`，写入前再 `slice(0, store._dim)`。
**addWithCreated 返回** `[success, created]`：`[true,true]`=首次 insert；`[true,false]`=replace；`[false,false]`=失败或 vector 校验不过。

---

## 10. 断点续跑

启动时（run() Step 5/6）：
1. `existingIds = store.list_existing_method_ids(limit=200000)`（try/except，失败 ∅）。
2. `alreadyDone = existingIds ∩ meaningful`。
3. 对 alreadyDone 每条 `get_by_method_id` → `extractSummary(interpretation_text)` 存 `summaryCache`（加锁）。
4. `loadState(stateFile)`：文件存在则 `JSON.parse`（失败返 `{}`），每层 `L{lv}` 的 `permanent_failed` 数组恢复为该层初始 `permanentFailed` Set。

`list_existing_method_ids(limit=100000 默认)` 实现：`pageSize=2000`，`offset` 从 0 递增，`fetch_objects(limit=cur_limit, offset=fetched, return_properties=["method_entity_id"])`，累积到 Set，直到空页 / `objs.length < cur_limit` / `fetched >= target`。SDK 不支持 offset/return_properties（TypeError）时退化为单页 `limit=target` 拉取。

---

## 11. 孤儿同步（一致性清理）

在 Step 5 执行（**发生在断点续跑加载 summaryCache 之前**，但 existingIds 已先取得）：
```
orphans = existingIds - meaningful
for oid of orphans:
  try { store._getCollection().data.deleteById(_to_uuid(oid + "|interpret")) } catch { /* 静默 */ }
```
CodeGraph 模式下 `_getCollection()` 被 override 为 tenant-scoped，故删除作用于 project 分区。

---

## 12. 多 Provider 负载均衡（MultiProvider）

### 12.1 generate(prompt, kwargs)

```typescript
const BACKOFF_DELAYS = [30, 60, 120]   // 秒
let lastError = null
for (let attempt = 0; attempt < BACKOFF_DELAYS.length + 1; attempt++) {   // 共 4 轮
  const startIdx = nextCycle()         // 受锁/原子保护的 itertools.cycle，每次消耗一个值
  let allRateLimited = true
  for (let offset = 0; offset < providers.length; offset++) {
    const idx = (startIdx + offset) % providers.length
    try {
      const result = providers[idx].generate(prompt, kwargs)
      callCounts[idx]++                 // 加锁
      return result
    } catch (e) {
      const is429 = isRateLimitError(e)
      if (!is429) allRateLimited = false
      lastError = e
      continue                          // 立即切下一个 provider
    }
  }
  if (!allRateLimited) break            // 非限流错误 → 不退避，跳出
  if (attempt < BACKOFF_DELAYS.length) {
    const delay = BACKOFF_DELAYS[attempt] + random(0, 5)   // 抖动 0-5s
    backoffCount++                      // 加锁
    sleep(delay * 1000)
  } else {
    log.error("退避重试3次后仍然限流，放弃")    // 最后一轮只 log，不 sleep
  }
}
throw new Error(`MultiProvider: 所有 ${providers.length} 个 Provider 均失败`)   // cause = lastError
```

**轮询实现**：源码用全局 `itertools.cycle(range(n))` + `threading.Lock` 保护 `next()`。TS 用受保护的递增计数器 `idx = (counter++) % n`。**每次 `generate` 调用消耗一个 cycle 值作为 `start_idx`**——保证跨调用的负载分散。

### 12.2 isRateLimitError(err)

`err.message.toLowerCase()` 含任一：`"429"` / `"rate_limit"` / `"rate limit"` / `"throttling"` / `"quota exceeded"` / `"usage limit"` / `"too many requests"`。

### 12.3 factory `_build_multi_backend`

从 `kwargs.multi_providers: list[dict]` 读取（空则抛 ValueError）。逐项：`sub_backend = (pcfg.backend || "openai").toLowerCase()`，查 `_LLM_BACKEND_BUILDERS`（未知则抛 ValueError），`sub_kwargs = pcfg 去掉 backend 键`，调 sub_builder 得 provider，`name = pcfg.name || \`${sub_backend}-${i}\``。组装 `MultiProvider(providers, names)`，`resolved_backend = \`multi(${names.join(",")})\``。

典型 config（qwen + minimax 各半，配合 max_workers=8 → 各 4）：
```yaml
multi_providers:
  - { backend: openai, name: qwen,    openai_base_url: https://dashscope.aliyuncs.com/compatible-mode/v1, openai_api_key: ..., openai_model: qwen-coder-plus }
  - { backend: openai, name: minimax, openai_base_url: https://api.minimax.chat/v1, openai_api_key: ..., openai_model: MiniMax-Text-01 }
```
**注意**：「两平台各半 4+4」是**部署约定，非代码强制**——MultiProvider 只做轮询，并发由 interpreter 的 `maxWorkers` 单一池控制，并非每 provider 独立 4 线程。

### 12.4 stats()

`{ [name]: callCount, "_backoff_count": number }`。

### 12.5 factory 默认回落

`create_with_meta`：未在 registry 注册的 backend **一律按 ollama 解析**（不报错）。`llm_allow_fallback_to_ollama` 控制 openai/anthropic 库缺失时是否回退 ollama（否则抛 RuntimeError）。

---

## 13. CodeGraph 重生模式（method_entity_id = qualified_name）

`run_codegraph_interpret.py` 的 `_TenantBoundStore`（关键适配层）：

```typescript
class TenantBoundStore extends WeaviateTopologicalInterpretStore {
  private boundTenant: string
  constructor({ tenant, ...superArgs }) { super(superArgs); this.boundTenant = tenant }
  _getCollection() { return super._getCollection().withTenant(this.boundTenant) }
}
```

- `entity_id = qualified_name`（如 `com.macro.mall.service.impl.UmsAdminServiceImpl.login`），**无 `method//` / `method://` 前缀**。
- QA 召回侧用 `method_entity_id_variants` 兼容（见 §16 第4条）。
- store 连接参数从**环境变量**取（`WEAVIATE_URL` 默认 `http://localhost:8080`、`WEAVIATE_GRPC_PORT` 默认 `50051`、`WEAVIATE_DIMENSION` 默认 `"1024"`、`WEAVIATE_API_KEY` 空则 None），**不从 config.yaml 取**（实测 project.yaml=localhost 而 env=真实地址）。
- LLM 从 config 取（`config.knowledge.topological_interpretation`，经 `LLMProviderFactory.from_method_interpretation(mi).provider`）。
- `build_and_run`：`CodeGraphFactsProvider(db_path, repo_local_path).build_structure_facts(module_filter=modules)` → interpreter 默认 `embedding_dim=1024 / language=zh / llm_timeout=90`，`state_file=out_ui/2b_interp_state_{project_id}.json`，`max_workers=workers`（CLI 默认 8）。
- CLI 模块白名单：逗号分隔去空白过滤空串，全空 → None（全量）。

---

## 14. 状态文件格式

**路径**：canonical_v1 `out_ui/interpretation_state.json`；CodeGraph `out_ui/2b_interp_state_{project_id}.json`。

```json
{
  "L0": { "status": "COMPLETE", "permanent_failed": ["...", "..."], "timestamp": 1718000000.0 },
  "L1": { "status": "PARTIAL_ACCEPT", "permanent_failed": ["..."], "timestamp": 1718001000.0 }
}
```
- `status` 枚举：`COMPLETE | PARTIAL | PARTIAL_ACCEPT | GATE_FAILED`。
- `permanent_failed`：**sorted 后写入**（`sorted(permanent_failed)`），TS 需排序保证 golden 一致。
- `timestamp`：`time.time()`（Unix 秒，浮点）。
- `_saveState`：先 `loadState()` 读现有全文，合并该层后整体 `JSON.stringify(state, null, 2)`（`ensure_ascii=False` → 中文不转义），写回；先 `mkdir -p` 父目录。**非原子，原实现无锁。**

---

## 15. 魔法数字汇总（修正补充）

| 参数 | 值 | 位置 | 说明 |
|---|---|---|---|
| `maxWorkers` | 8 | 构造 | 默认并发；单一 ThreadPool（非 per-provider） |
| `batchSize` | `max(maxWorkers*3, 20)` = 24 | `_interpretLevel` | 分批大小 |
| `llmTimeout` | 90 秒 | 构造 | 单次 generate 超时 |
| `layerGate` | 1.0 | 构造 | 每层完成率阈值 |
| `maxRetryCycles` | 5 | 构造 | 最大重试轮次 |
| `retryDelays` | `[60,300,1800,3600,7200]` 秒 | 构造 | 退避序列 |
| `backoffDelays`(multi) | `[30,60,120]` 秒 | MultiProvider | 限流退避 |
| `jitter`(multi) | `random(0,5)` 秒 | MultiProvider | 抖动 |
| multi 退避轮数 | `len(BACKOFF_DELAYS)+1` = 4（末轮不 sleep） | MultiProvider | 注意是 4 次循环，3 次实际 sleep |
| 进度上报 | `(ok+fail)%50==0` 或 `==本轮 todo 总数` | `_interpretLevel` | |
| calleeSummaries 上限 | 2000 字符 | `_buildCalleeSummaries` | |
| beanFieldContext 上限 | 1500 字符 | `_buildBeanFieldContext` | |
| code 截断 | 8000 字符 | `_buildPrompt` | |
| max callee in context | 8 | `_buildContextSummary` | |
| max caller in context | 5 | `_buildContextSummary` | |
| related_entity_ids | 最多 24 | `_interpretOne` | |
| list_existing 分页 | 2000/页 | `list_existing_method_ids` | |
| list_existing 默认/续跑上限 | 默认 100000；续跑/孤儿传 200000 | | |
| min_text_len | 10（raw 与 cleaned 各校验一次） | `_interpretOne` | |
| summary 最大长度 | 50 | `extractSummary` | |
| **interpreter embeddingDim 默认** | **1024** | 构造 | |
| **store dimension 默认** | **64** | WeaviateTopologicalInterpretStore | **⚠️ 两者必须对齐；生产用 env=1024 覆盖 store，否则 vector 截断到 64/校验失败** |
| 字段截断 | class_name 500 / method_name 300 / signature 2000 / interpretation_text 48000 / context_summary 12000 / related_ids_json 8000 | store | |
| DDL 正则 | `CREATE TABLE \`(\w+)\` (...) ENGINE`（DOTALL） | `_loadTableDdls` | 必须反引号+ENGINE 结尾 |

---

## 16. 已知怪癖 / 注意事项（修正补充）

1. **`_to_uuid` 是 SHA256 不是 uuid5**（最高优先）：`sha256(s)[:32]` 格式化为 UUID 串。TS 用 `crypto.createHash('sha256')` 复现，否则全链路 id 不一致。

2. **英文 prompt `bean_section` 是 `NameError` 不是「未赋值」**：英文分支引用了一个在该分支内从未定义的变量，运行即崩。因 language 恒 zh 从未触发。TS 修复：分支前统一计算 `beanSection`。

3. **summary 向量而非全文向量**：刻意设计，仅 `[摘要]`（≤50 字）embedding，提高检索精度。

4. **`method_entity_id_variants` 兼容查询**：`get_by_method_id` / `get_by_entity_with_tenant` 对 `method://` ↔ `method//` 互转依次 fetch（`method_entity_id_normalize.py`）。无前缀（CodeGraph qualified_name）则 variants 只返回原值一项。

5. **DAO SQL 注入顺序**：code → `-- [MyBatis SQL]\n{sql}` → 按 sqlTables 顺序 `-- [表结构 {table}]\n{ddl}`，`\n\n` 连接。

6. **class_entity_id 查找是 CONTAINS 反查**：找 `type==CONTAINS && target_id==methodId` 的第一条取 `source_id`（线性扫描）。建议 TS 在 `_buildIndices` 预建 `methodToClass: Map<string,string>` 索引优化。`_buildContextSummary` 与 `_getRelatedIds` 也各自线性扫 relations，可一并预建。

7. **Gate 失败抛 RuntimeError 中止后续层**：run() catch 后 `break`，保证不在残缺数据上做高层解读。

8. **孤儿删除 / get / 写入异常全部静默或返 false**：单条失败不影响整体。

9. **状态文件每层独立 read-merge-write，非原子无锁**：TS 移植保持行为即可（离线单进程跑）。`permanent_failed` 写入前 `sorted`。

10. **TenantBoundStore 重写 `_getCollection()` 是零改动复用的关键**：interpreter 全程不传 tenant，靠 `add_with_created(tenant=None)` → `_resolve_collection(None)` → `_get_collection()`（被 override）这条链路落到正确 tenant。`get_by_method_id` 用的是 `_get_collection()`（同样被 override），所以续跑读也在正确分区。

11. **`embeddingDim` vs store `dimension` 双轨**：interpreter 用 `embeddingDim` 调 `getEmbedding`，store 用自己的 `dimension`（`self._dim`）做截断与校验。生产经 env 统一为 1024。TS 必须让两个 dim 一致，否则静默写不进（vector 校验 `< _dim` 返 `[false,false]`）。

12. **`_class_map` 是 dead field**：`_buildIndices` 填充但全程未读，TS 可省略。

13. **`_interpret_level` 的 `processed_before` 恒 0**：调用方固定传 0，进度 `done` 等于本层累计 `ok+fail`。

14. **`list_existing_method_ids` 在 gate 循环里每轮调两次**（轮首同步 + 轮末 stillTodo 判断），高频；TS 可保持，但注意 Weaviate 分页 IO 成本。

---

# 【链路级业务解读（模式B 实时）+ 业务三层】

I have everything needed. Now I'll produce the corrected complete specification.

---

## 修正清单

逐条回源核对后发现的偏差（按严重度排序）：

1. **【严重 / UUID 算法错误】** spec §4.3 称 `_to_uuid` "通常是对字符串做 UUID v5 确定性 hash"。**错。** 实际是 SHA256 取前 32 个 hex 字符再按 8-4-4-4-12 切片拼成 UUID 形状（`base_weaviate_store.py:30-33`）。**不是** UUID v5，TS 实现必须用 SHA256 否则 UUID 不一致、整个断点续跑/孤儿清理失效。已补全确切实现。

2. **【严重 / 完全遗漏 multi-provider 轮询】** 任务最高优先项之一"多 provider 轮询"，spec 全文无一字。实际 `llm_backend="multi"` 走 `MultiProvider`（`multi_provider.py`）：`itertools.cycle` round-robin + 全限流时指数退避 `[30,60,120]s`（带 0-5s 抖动）+ 最多 3 轮重试。已新增 §十一整节。

3. **【严重 / embedding_dim 默认值错误】** spec 多处把 1024 当作技术解读默认维度。**错。** `WeaviateTopologicalInterpretStore.__init__` 默认 `dimension=64`；`run_method_interpretations` 里 `dim = int(vinterp.dimension) if vinterp.dimension else 64`——回退 **64** 不是 1024。1024 仅是 `TopologicalInterpreter.__init__` 的 `embedding_dim` 默认。已修正。

4. **【重要 / context_summary 截断不一致】** spec §4.2 说 `context_summary [:12000]`。这是 **Weaviate store 写入层**的截断。但 `method_interpretation_runner._persist` 在调 store 前**先**截 `cctx[:4000]`；`TopologicalInterpreter` 不预截。两条路径不同，已分别标注。

5. **【重要 / multi-tenancy 仅技术解读 runner 传 tenant】** spec §4.4 暗示拓扑解读也传 tenant。**错。** `TopologicalInterpreter._interpret_one` 调 `add_with_created` **不传 tenant**（走 legacy 无 tenant 路径，会打 deprecation warning）；只有 `method_interpretation_runner._persist` 传 `tenant=project_id`。已修正。

6. **【重要 / `_to_uuid` 输入键】** 确认无误：技术解读 runner 与拓扑解读孤儿清理均用 `oid + "|interpret"`；写入 UUID 同为 `method_entity_id + "|interpret"`。spec 正确，保留。

7. **【次要 / `_compute_levels` 返回类型】** 实际 `dict[str, int]`（method_id→level），spec §2.3 描述算法正确，补类型。

8. **【次要 / `BusinessInterpretLevel` 值大小写】** 枚举成员名是 `API/CLASS/MODULE`，值是 `"api"/"class"/"module"`（`str, Enum`）。spec 写 "api/class/module" 指值，正确，已明确。

9. **【次要 / 模式B `<think>` 清洗位置】** 确认模式B 用 `index("</think>")` 字符串切片（`callchain_interpreter.py:295-297`），与 helpers 的 `re.sub` 不同。spec §10.5 正确。

10. **【次要 / 模式B 接口跳转 ChainNode 字段未填充】** spec §1.2 `ChainNode` 列出 `is_interface_impl/interface_name/branch_index`，但 `_trace_chain` 构建 ChainNode 时**从不设置**这三个字段（恒为默认 `False/None/0`）。即字段存在但当前死代码。已标注。

11. **【次要 / 模式B `class_name` 回退】** `ChainNode.class_name = node.get("class_name","") or node.get("name","")`——无 class_name 时回退用 name。spec 未提，已补。

12. **【次要 / DDL 正则反引号】** spec §2.3 用 `\x60` 转义表示反引号，实际正则字面量是 `` `(\w+)` ``。已用真实字符呈现。

13. **【次要 / 业务三层 runner 不存在 + adapter 调用未实现方法】** 确认 `business_interpretation_runner.py`、`business_interpretation_context.py`、`business_question_lexical_rerank.py` **均不存在**。`BusinessInterpretationStoreAdapter.list_existing_keys` 调 `store.list_existing_entity_level_pairs(limit=...)`——该方法在 `WeaviateTopologicalInterpretStore` 上**未实现**（调用即 AttributeError）。spec §8/§10.7 方向对，已收紧措辞。

14. **prompt 全文逐字复核**：模式B 中/英 header、拓扑中/英 prompt、技术解读 runner 中/英 prompt 全部逐字比对 ✅ 与源码一致，原样保留。

---

# 链路解读（模式B）+ 业务三层解读 规范（修正版）

源文件（只读权威，已逐行核对）：
- `/Users/java/knowledge-engineering/src/knowledge/callchain_interpreter.py`（539 行）
- `/Users/java/knowledge-engineering/src/knowledge/topological_interpreter.py`（803 行）
- `/Users/java/knowledge-engineering/src/knowledge/method_interpretation_runner.py`（427 行）
- `/Users/java/knowledge-engineering/src/knowledge/business_interpretation_strategies.py`（23 行）
- `/Users/java/knowledge-engineering/src/knowledge/interpretation_item_helpers.py`（115 行）
- `/Users/java/knowledge-engineering/src/knowledge/weaviate_interpretation_store.py`（338 行）
- `/Users/java/knowledge-engineering/src/knowledge/recall_rerank.py`（204 行）
- `/Users/java/knowledge-engineering/src/knowledge/interpretation_store_adapter.py`（74 行）
- `/Users/java/knowledge-engineering/src/knowledge/base_weaviate_store.py`（`_to_uuid`）
- `/Users/java/knowledge-engineering/src/knowledge/llm/factory.py` + `llm/multi_provider.py`（多 provider）
- `/Users/java/knowledge-engineering/src/core/domain_enums.py`（枚举）
- `/Users/java/knowledge-engineering/src/core/weaviate_defaults.py`（默认值）

---

## 一、模式B 链路实时解读（CallChainInterpreter）

### 1.1 组件用途

实时、不落库的链路解读器（模式B）。给定方法实体 ID，BFS 展开调用链（向下/向上/双向），拼接全量代码，一次性发 LLM 生成 8 段式业务解读，结果直接返回，**不写 Weaviate**。与模式A（预解读存向量库）并存，适合深度分析（6-90 秒）。

### 1.2 公开函数签名

```
CallChainInterpreter.__init__(
  graph: Any,                   // KnowledgeGraph（有 .get_node/.successors/.predecessors）或裸 NetworkX 图（取 ._graph 或自身）
  llm: Any,                     // LLMProvider，需有 .generate(prompt, timeout=, max_tokens=)
  structure_facts: Any = None,  // 预建 entity_id→code_snippet 索引；没传则尝试从 out_ui 缓存加载
  language: str = "zh",         // 以 "en" 开头走英文 header，否则中文
  repo_path: str = "",          // 非空才加载 DAO SQL 插件
  dao_config: dict | None = None
)

CallChainInterpreter.interpret(
  method_id: str,
  direction: str = "down",   // "down" | "up" | "both"
  max_depth: int = 5,
  max_methods: int = 30,
  max_tokens: int = 4000,    // 透传给 llm.generate(max_tokens=)
  timeout: int = 120
) -> CallChainResult
```

`CallChainResult` 字段（dataclass，有 `.to_dict()`）：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `method_id` | str | — | 起始方法 ID |
| `chain` | list[ChainNode] | `[]` | BFS 结果，按 `(depth, class_name, method_name)` 排序 |
| `interpretation` | str | `""` | LLM 8 段式文本（去 `<think>` 后） |
| `prompt_tokens` | int | 0 | `len(prompt) // 4` 估算 |
| `llm_time_seconds` | float | 0.0 | `round(耗时, 1)` |
| `chain_size` | int | 0 | `len(chain)` |
| `total_code_chars` | int | 0 | `sum(len(n.code_snippet or ""))` |
| `error` | str\|None | None | 无链路 / LLM 失败时填充 |

`ChainNode` 字段：`method_id, class_name, method_name, signature, depth, code_snippet(Optional), module_id(Optional), location(Optional), is_interface_impl=False, interface_name=None, branch_index=0`

> **怪癖（修正10）**：`is_interface_impl / interface_name / branch_index` 三字段虽在 dataclass 定义，但 `_trace_chain` 构建 ChainNode 时**从不赋值**，恒为默认值。TS 移植可保留字段占位但不必实现填充逻辑（当前为死字段）。

> **怪癖（修正11）**：`class_name = node.get("class_name","") or node.get("name","")`，`signature = node.get("signature","") or node.get("name","")`——缺失时均回退到 `name`。

### 1.3 链路展开算法（`_trace_chain`，BFS）

1. **起始节点验证**：先 `graph.get_node(start_id)`，抛 `(AttributeError, TypeError)` 则退到 `getattr(graph,'_graph',graph).nodes[start_id]`。找不到 → 返回 `[]`（→ `result.error = f"未找到方法 {method_id} 或无调用关系"`）。
2. **BFS 队列**：`deque([(start_id, 0)])`，`visited: set`。
3. **循环条件**：`while queue and len(chain) < max_methods`。出队后若 `nid in visited or depth > max_depth` → `continue`。
4. **节点类型过滤**：取 node 后 `entity_type != "method"` → `continue`。
5. **接口→实现自动跳转**（两路）：
   - `__init__` 时 `_build_interface_to_impl_index()` 预建 `self._iface_to_impls: dict[接口方法ID → list[实现方法ID]]`（兼容属性 `self._iface_to_impl` = 每项取 `[0]`）。
   - 匹配规则：非 `Impl` 结尾的类，找 `candidate == cls_name+"Impl"`（精确）**或** `candidate.endswith("Impl") and cls_name in candidate`（扩展，如 `AlipayXxxServiceImpl`）。
   - BFS 时先按 `nid` 查 `_iface_to_impls`；未命中且 `class_name && method_name && not class_name.endswith("Impl")` → 调 `_find_impls_by_name(class_name, method_name)` 再找。
   - `len(impl_ids)==1`：单实现直接替换当前节点（`nid=impl_id; node=impl_node; visited.add(impl_id)`）。
   - `len(impl_ids)>1`：多实现全部 `queue.append((impl_id, depth))`（**同层并行分支，depth 不变**），当前接口节点仍保留在链路中。
   - `_find_impls_by_name` 多候选去重：`len(results)>1` 时优先取**有 code_snippet** 的，且只取 `[:1]`（取一个）。
6. **代码合并**（`combined`）：
   - Java + SQL 都有：`{java}\n\n-- ========== MyBatis SQL ==========\n{sql}`
   - 仅 SQL：`-- [DAO 接口方法，SQL 来自 MyBatis XML]\n\n{sql}`
   - 仅 Java：`java` 或 `None`
7. **邻居展开**：`direction in ("down","both")` 走 `successors(nid, rel_type="calls")`（异常退到 `nx_graph.out_edges`，过滤 `edata["rel_type"]=="calls"`）；`("up","both")` 走 `predecessors`/`in_edges`。**跳过 getter/setter**：`not self._is_getter_setter(callee/caller_id)`（按节点 `is_getter`/`is_setter` 属性）。入队 `depth+1`。
8. **终止**：`len(chain) >= max_methods` 或队列空（第三方 JAR 方法不在图中，无出边自然停）；单节点 `depth > max_depth` 跳过但不终止整个循环。
9. **排序**：`chain.sort(key=lambda n: (n.depth, n.class_name, n.method_name))`。

**DAO SQL 匹配策略（`_load_dao_sql`，优先级）**：① 精确 `(class_simple_name, method_name)`；② `class_name.endswith("Dao")` → `class_name[:-3]+"Mapper"`；③ `class_name.endswith("Mapper")` → `class_name[:-6]+"Dao"`。

### 1.4 模式B LLM Prompt 全文（逐字，已核对一致）

`prompt = header + "\n".join(code_parts)`，`MAX_PROMPT_CHARS = 60000`。

**中文 header（默认，`not language.startswith("en")`）：**

```
你是一位同时具备业务分析和技术架构能力的资深专家。
你正在编写「代码业务解读文档」，目标读者是：新入职开发者、产品经理、测试工程师。

以下是「{entry_name}」的完整调用链代码（{len(chain)} 个方法，{max_depth} 层深度）。

请按以下结构输出分析报告：

## 1. 业务场景
- 谁（什么角色/系统）在什么场景下触发这个操作
- 对应的产品功能描述（用非技术语言）

## 2. 前置条件
- 执行这个操作前必须满足的条件

## 3. 主流程（按执行顺序）
- 用编号列出每一步，用业务语言描述而非代码语言
- 标注每一步涉及的系统或服务

## 4. 业务规则
- 从代码中提炼出的业务约束（用「当...时，则...」格式）
- 每条规则标注规则来源（哪段代码/哪个条件）

## 5. 数据变更
- 读取了哪些数据库表的哪些字段
- 修改了哪些数据库表的哪些字段
- 数据变更的原子性和一致性保障

## 6. 异常场景
- 列出所有可能的失败场景
- 每个场景：触发条件 → 系统行为 → 用户感知

## 7. 上下游影响
- 这个操作的结果会影响哪些下游功能
- 哪些上游变更会影响这个操作的行为

## 8. 风险与建议
- 从代码中识别出的业务风险（不是纯技术风险）
- 每个风险给出具体的改进建议
```

> 注：`{entry_name} = f"{chain[0].class_name}.{chain[0].method_name}"`（空链路时 `"unknown"`）；`{max_depth} = max(n.depth for n in chain)`（**是链路实际最大深度，非入参 max_depth**）。

**英文 header（`language.startswith("en")`）：**

```
You are a senior expert with both business analysis and technical architecture capabilities.
You are writing a 'Code Business Interpretation Document'.
Target readers: new developers, product managers, QA engineers.

Below is the complete call chain code for '{entry_name}' ({len(chain)} methods, {max_depth} levels deep).

Please output your analysis in the following structure:

## 1. Business Scenario
- Who triggers this operation, in what context

## 2. Preconditions
- What must be true before execution

## 3. Main Flow (in execution order)
- Numbered steps in business language, not code language

## 4. Business Rules
- Extract from code, format: 'When X, then Y'
- Cite which code/condition each rule comes from

## 5. Data Changes
- Which tables/fields are read and written
- Atomicity and consistency guarantees

## 6. Exception Scenarios
- Each: trigger condition → system behavior → user impact

## 7. Upstream/Downstream Impact
- What downstream functions are affected by this operation
- What upstream changes would affect this operation

## 8. Risks & Recommendations
- Business-level risks (not just technical)
- Specific improvement suggestions for each
```

**Code section 格式（每节点，`{'=' * 60}` = 60 个 `=`）：**

```

============================================================
[L{depth}{'入口' if depth==0 else ''}] {class_name}.{signature}
============================================================
{code_snippet 或 "(无代码片段)"}
```

**上下文保护截断**：累计 `total_chars + len(section) > 60000` 时：若 `remaining = 60000 - total_chars > 300`，当前节点代码截为 `snippet[:remaining-100] + "\n// ... 代码截断（上下文保护）..."` 并追加该 section；再无条件追加 `\n// ⚠ 后续 {len(chain)-len(code_parts)} 个方法因上下文限制被省略`，然后 `break`。

**`<think>` 清洗**（`interpret()` 内）：`if "<think>" in interpretation and "</think>" in interpretation:` → `interpretation = interpretation[interpretation.index("</think>")+len("</think>"):].strip()`。注意：**两个标签都在**才裁，用字符串 `index` 切片（区别于 helpers 的 `re.sub`）。

### 1.5 魔法数字（模式B）

| 常量 | 值 | 用途 |
|---|---|---|
| `MAX_PROMPT_CHARS` | 60000 | prompt 字符上限（注释称约 15K tokens，Qwen 上下文 50%） |
| `max_depth` 默认 | 5 | BFS 最大深度 |
| `max_methods` 默认 | 30 | 链路最大方法数 |
| `max_tokens` 默认 | 4000 | 透传 LLM 输出限制 |
| `timeout` 默认 | 120 s | LLM 超时 |
| 截断保护阈值 | `remaining > 300` | 决定是否还塞截断片段 |
| prompt_tokens 估算 | `len(prompt)//4` | — |

### 1.6 落库结构

模式B **不落库**，结果直接返回 `CallChainResult`（含 `.to_dict()` 序列化）。

---

## 二、拓扑解读（TopologicalInterpreter）— 自底向上方法解读

### 2.1 组件用途

方法级技术/业务解读的批量引擎（"大厦理论"：砖→楼层）。自底向上（叶子=L0）逐层解读，下层 summary 注入上层 prompt 实现语义传播。落入 Weaviate `TopologicalInterpretation` collection。

> **业务三层现状（修正13）**：`BusinessInterpretLevel` 枚举（值 `"api"/"class"/"module"`）、`BusinessInterpretTierSpec` 描述符、`BusinessInterpretationStoreAdapter` 三者已定义，但**调用三层的独立 runner 不存在**（`business_interpretation_runner.py` / `business_interpretation_context.py` / `business_question_lexical_rerank.py` 经确认**均不存在**）。当前实际落库的解读即 `TopologicalInterpreter`（无 tenant）+ `method_interpretation_runner`（带 tenant），共写同一 `TopologicalInterpretation` collection（P5c 已确认无独立 business collection）。

### 2.2 公开函数签名

```
TopologicalInterpreter.__init__(
  structure_facts: StructureFacts,
  llm: Any,
  weaviate_store: Any,
  *,
  language: str = "zh",
  embedding_dim: int = 1024,   // ← 此处默认 1024（仅本类）
  max_workers: int = 8,
  llm_timeout: int = 90,
  repo_path: str = "",
  layer_gate: float = 1.0,
  max_retry_cycles: int = 5,
  retry_delays: list[int] | None = None,   // 默认 [60,300,1800,3600,7200]
  state_file: str = "out_ui/interpretation_state.json",
  step_callback: Callable[[str], None] | None = None,        // 默认 no-op
  progress_callback: Callable[[int,int,str], None] | None = None,  // 默认 no-op
)

TopologicalInterpreter.run() -> dict[str, Any]
// 返回 {total_methods, levels, already_done, ok, fail,
//        permanent_failed: {f"L{lv}": count}, elapsed_minutes}
```

### 2.3 算法步骤（`run()`）

**Step 1 `_build_indices()`**：
- 遍历 `facts.entities`，`e.type == EntityType.METHOD` → `_methods[e.id]=e`，`_class_map[e.id]=attrs["class_name"]`。
- 遍历 `facts.relations`，`r.type == RelationType.CALLS` 且 `src != tgt` 且两端都在 `_methods` → 填 `_call_graph[src].add(tgt)` 和 `_reverse_graph[tgt].add(src)`。
- 遍历 `EntityType.FIELD`，有 `comment` → `_field_comments[(class_name, e.name)] = comment`。

**Step 1.5 `_load_dao_sql()`**（`repo_path` 空则跳过）：
- `from src.plugins.dao_sql.registry import load_dao_sql_for_repo`，`load_dao_sql_for_repo(repo_path, {})`。
- `_load_table_ddls()`：`glob(repo_path/**/*.sql, recursive=True)`，正则 `` CREATE TABLE\s+`(\w+)`\s*\((.*?)\)\s*ENGINE ``（`re.DOTALL`），`_table_ddls[table_name] = match.group(0)`（保留整段含 ENGINE）。
- SQL key 解析：`key.rsplit(".",1)` → `(namespace, method_name)`，`class_name = namespace.rsplit(".",1)[-1]`；对 `[class_name, class_name.replace("Dao","Mapper"), class_name.replace("Mapper","Dao")]` × method_name 查 `name_to_ids`，命中则 `_sql_index[mid]=annotated_sql`，`_sql_tables[mid]=sql_result.tables or []`。

**Step 2 `_compute_levels(meaningful) -> dict[str,int]`**（拓扑分层）：
- `out_degree[mid] = len(_call_graph[mid] & meaningful)`。
- 叶子 `out_degree==0` → `level=0` 入队；BFS 向上：`callers = _reverse_graph[node] & meaningful`，`new_level = current+1`，`if caller not in levels or levels[caller] < new_level: levels[caller]=new_level; 入队`。
- 孤立节点（未进 levels）→ `level=0`。

**`_filter_meaningful() -> set[str]`**：排除无 `code_snippet`、排除 `is_getter or is_setter`。

**Step 3 孤儿清理**：`existing_ids = store.list_existing_method_ids(limit=200000)`（异常吞掉为空 set），`orphans = existing_ids - set(meaningful)`，逐条 `uid = store._to_uuid(oid+"|interpret")` → `store._get_collection().data.delete_by_id(uid)`。

**断点续跑加载**：`already_done = existing_ids & set(meaningful)`，对每个 `store.get_by_method_id(mid)` → `extract_summary(text)` → 加锁写 `_summary_cache[mid]`。

**`_load_state()`**：读 `state_file`（JSON），失败返回 `{}`。

**Step 4 逐层（`for lv in range(max_level+1)`）**：从 `prev_state[f"L{lv}"]["permanent_failed"]` 恢复 set，调 `_run_layer_with_gate`；捕获 `RuntimeError`（Gate 未达）→ `_save_state(lv,"GATE_FAILED",...)` 并 `break`（**阻断所有后续层**）。

**`_run_layer_with_gate(level, methods_at_level, permanent_failed)`** 循环：
1. `done_ids = store.list_existing_method_ids()`（**默认 limit=100000**，非 200000）；新完成的加载 summary 到缓存。
2. `todo = [m not in done_ids and m not in permanent_failed]`；`attempting = total_count - len(permanent_failed)`；`completeness = (attempting - len(todo))/attempting`（attempting≤0 时 =1.0）。
3. `not todo or completeness >= layer_gate` → `status = "COMPLETE" if not permanent_failed else "PARTIAL"`，`_save_state`，返回 `(ok,fail,permanent_failed)`。
4. `cycle >= max_retry_cycles` → 剩余 `todo` 全标 `permanent_failed`；`final_completeness = (total - len(permanent_failed))/total`；`>= layer_gate` → `PARTIAL_ACCEPT` 返回；否则 `GATE_FAILED` 并 `raise RuntimeError`。
5. 跑一轮 `_interpret_level(todo, level, 0, len(todo))`；`cycle += 1`。
6. `cycle < max_retry_cycles` 且仍有 `still_todo` → `delay = retry_delays[min(cycle-1, len(retry_delays)-1)]`，`time.sleep(delay)` 后下轮。

**Step 7 `_interpret_level`（层内并行）**：`batch_size = max(max_workers*3, 20)`；按 batch 提交 `ThreadPoolExecutor(max_workers=max_workers)`，`as_completed`；进度 `(ok+fail) % 50 == 0 or (ok+fail) == len(method_ids)` 时触发 `_progress`。

**Step 8 `_interpret_one(method_id, level) -> bool`**：
1. `code = _get_code_with_sql(method_id)`；空 → `return False`。
2. `prompt = _build_prompt(method, level, code)`。
3. `raw_text = llm.generate(prompt, timeout=llm_timeout)`；捕获 `INTERP_ITEM_RECOVERABLE_EXCEPTIONS` 或其它 Exception → `return False`。
4. `not raw_text or len(raw_text) < 10` → False；`clean_think_tags` 后 `len < 10` → False。
5. `summary = extract_summary(text)`；加锁 `_summary_cache[method_id]=summary`。
6. `vec = get_embedding(summary, self.dim)`（**只对摘要**）。
7. `class_id`：遍历 `facts.relations` 找首个 `CONTAINS && target_id==method_id` 的 `source_id`（无类型校验，与技术 runner 不同——见下）。
8. `store.add_with_created(...)`，`related_entity_ids_json = json.dumps(list(_get_related_ids(method_id))[:24])`（**`_get_related_ids` 含 method 自身 + CALLS 两端 + CONTAINS source**；`json.dumps` 默认 `ensure_ascii=True`，与技术 runner 的 `ensure_ascii=False` 不同）。**不传 tenant**（修正5：走 legacy 路径）。

`_get_code_with_sql`：无 SQL → 仅 code；有 SQL → `[code?] + ["-- [MyBatis SQL]\n{sql}"] + 逐表 "-- [表结构 {table}]\n{ddl}"`，`"\n\n".join(...)`。

### 2.4 拓扑解读 LLM Prompt 全文（逐字，已核对一致）

`_build_prompt(method, level, code)`：`sig = attrs["signature"] or method.name`；`context = _build_context_summary(method)`；`level > 0` 才 `callee_context = _build_callee_summaries(method.id)`；`bean_context = _build_bean_field_context(code)`。

**中文：**

```
你是企业代码业务架构师。读者是产品经理 / 业务方 / 新人开发者——**不懂**框架细节（MyBatis、BCrypt、Stream API、静态代理等）。你要把代码翻译成"这个方法做什么业务、为业务方解决什么问题"。

【输出要求】

- **[摘要]**：≤50 字，空格分隔的业务关键词。
  - ✅ 只写业务动作 + 业务对象 + 业务价值，例：`创建订单 校验库存 锁定优惠券 写入订单表 通知发货`
  - ❌ 禁止技术词：MyBatis / BCrypt / Stream / Example / Mapper / Selective / 静态代理 / 委托模式 / SQL / Hash / Redis / Spring / Cypher / Bean

- **[详情]**：用业务语言描述。读者读完应该知道"这个方法在业务流程中扮演什么角色"。
  1. **业务职责**：这个方法在整体业务里做什么？（1-2 句话回答）
  2. **业务流程**：分步骤讲"先做 X、再做 Y、最后做 Z"，每一步说**业务含义**而非技术细节
     - ❌ "调用 productMapper.updateByExampleSelective 执行选择性更新"
     - ✅ "把该品牌名下所有商品的品牌名称同步刷新，保证商品列表显示一致"
  3. **业务价值**：为什么需要这个方法？业务方关心什么？
  4. **上下游关系**：如果下游方法的解读里说了它们的**业务功能**，引用其业务功能描述本方法如何编排（不要列下游方法名）

【硬约束】

- 禁止出现"框架/技术/算法"层面的词汇（除非业务方常用，如"短信、缓存、数据库"这种概念性词）
- 禁止编造调用链——只能基于"### 下游方法功能" 段里给你的内容
- 如果上游/下游为"无"，不要硬编"通常由 Controller 调用"
- **接口契约的精确判定**：仅当方法体只有 `;` 结尾的签名行（即"int foo(Bar b);"这种纯声明、无 `{...}` 主体）时才说"此为接口契约，实际业务由实现类完成"。**只要有 `{...}` 主体——哪怕主体很短（如 @Bean 工厂方法、单行委托）——都必须正常解读业务职责**，**不要**因为代码短就退化为"接口契约"模板。

### 上下文
{context}

### 方法签名
{sig}
{callee_section}{bean_section}
### 方法体（节选）
```
{code[:8000]}
```

### 请严格按以下格式输出
[摘要] <业务关键词1 业务关键词2 业务关键词3 ...>

[详情]
<业务视角的解读>
```

注入段（中文）：
- `callee_section` = `\n### 下游方法功能（已解读）\n{callee_context}\n`（仅 `callee_context` 非空）
- `bean_section` = `\n### 代码中使用的 Bean 字段说明\n{bean_context}\n`（仅 `bean_context` 非空）
- 模板中 `{callee_section}{bean_section}` 紧贴在 `{sig}` 行之后、`### 方法体` 之前，**两者无分隔符直接拼接**。

**英文：**

```
You are a senior Java engineer. Produce a two-part interpretation.

Requirements:
- [Summary]: Keyword-dense, max 50 chars, space-separated. Include: business actions, objects, techniques.
- [Detail]: Full technical interpretation. Leverage the called method interpretations below to explain business logic accurately.

### Context
{context}

### Signature
{sig}
{callee_section}{bean_section}
### Method body (excerpt)
```
{code[:8000]}
```

### Output (strict format)
[Summary] <keywords>

[Detail]
<interpretation>
```

> **英文 prompt 怪癖**：英文分支只构造了 `callee_section`（`\n### Called methods (interpreted)\n{callee_context}\n`），**未重新定义 `bean_section`**——它复用中文分支前面计算的 `bean_section` 变量。因为英文分支在中文 `bean_section` 赋值**之前** return，实际英文 prompt 里 `{bean_section}` 引用的是函数内尚未赋值的名字……核对源码：英文分支 `return` 在第 608-629 行，而 `bean_section` 赋值在第 636 行（中文分支内）。**英文分支内 `bean_section` 未定义会 NameError**。这是 Python 源码的潜在 bug；TS 移植英文路径时应将 `bean_section` 视为空串（英文实际不注入 bean 字段），不要照抄这个引用错误。

**`_build_callee_summaries(method_id, max_total_chars=2000)`**：遍历 `_call_graph[method_id]`，跳过 getter/setter，取 `_summary_cache[cid]`（加锁，空则跳过），行 `- {m.name}: {summary}`；超 2000 字符时追加 `- ... 还有 {N} 个方法省略` 并 break。

**`_build_bean_field_context(code, max_chars=1500)`**：正则推断变量类型（`(\w+)\s+(\w+)\s*=\s*new\s+(\w+)` 和 `(\w+(?:<[^>]+>)?)\s+(\w+)\s*[=;]` 首字母大写），提取 `(\w+)\.(set|get|is)(\w+)\(` 调用，Bean 命名反推字段名（首字母大写+第二位小写→首字母转小写；连续大写如 PWD 保持），查 `_field_comments`，仅有注释的输出 `{class_name}.{field_name}: {comment}`，超 1500 字符 break。

**`_build_context_summary(method)`**（5 行）：`类名 / 方法签名 / 模块 / 上游调用方(callers[:5] 或 '无') / 下游被调用(callees[:8] 或 '无')`，调用方/被调用方名经 `_entity_name`（`{class_name}.{name}` 或 `name`，非 method 取 `entity_id[:20]`）。

### 2.5 技术解读 Runner（`run_method_interpretations`）

独立函数，**更简洁的技术视角** prompt（与拓扑业务视角不同），**无下层 summary 注入**。

签名：
```
run_method_interpretations(
  structure_facts: StructureFacts,
  interpret_cfg: TopologicalInterpretationConfig | Mapping,   // 经 coerce 校验
  vectordb_cfg: VectorDBConfig | Mapping,
  *,
  step_callback / progress_callback / item_list_callback /
  item_completed_callback / item_started_callback /
  interpretation_stats_callback = None,
  project_id: str | None = None,    // → 透传 tenant
) -> dict
// {written, failed, total_candidates, already_done_before, todo_this_run}
// 或 {skipped:True, written:0, failed:0}（未启用时）
```

启用条件：`mi.enabled` 为真，且 `vinterp.backend == "weaviate" and vinterp.enabled`。

**候选方法**：`type == METHOD and code_snippet and not _is_trivial_accessor(e)`。`_is_trivial_accessor`：优先 `is_getter/is_setter`；否则名称启发式（`get*/is*` 无参 = getter，`set*` 单参 = setter）。

**维度（修正3）**：`dim = int(vinterp.dimension) if vinterp.dimension else 64`（**回退 64，非 1024**）。

**并发**：`max_workers = max(1, int(getattr(mi,"max_workers",4) or 4))`（**默认 4**）；`batch_size = max(max_workers*2, 20)`；`timeout_sec = int(mi.timeout_seconds or 120)`。

**LLM 选择**：`LLMProviderFactory.from_method_interpretation(mi)` → `LLMProviderSelection{provider, requested_backend, resolved_backend, fallback_reason}`，支持 `backend="multi"`（见 §十一）。

**技术解读 Prompt 全文（逐字，已核对一致）：**

中文：
```
你是一名资深 Java 工程师。请根据下面的「类与调用链上下文」以及「方法代码」，输出该方法的技术解读。

要求：
- 使用简体中文，分两部分输出。
- 第一部分 [摘要]：关键词密集，不超过50个中文字符，用空格分隔关键词/短语，不要完整句子。包含：业务动作、涉及对象、关键技术手段。
- 第二部分 [详情]：完整技术解读，说明方法职责、关键逻辑、与上下游调用的关系；不要大段重复粘贴源码。

### 上下文
{context_summary}

### 方法签名
{signature}

### 方法体（节选）
```
{code_snippet[:10000]}
```

### 请输出（严格按以下格式）
[摘要] <关键词1 关键词2 关键词3 ...>

[详情]
<完整技术解读>
```

英文：
```
You are a senior Java engineer. Based on the following CLASS/CALL-CHAIN CONTEXT and METHOD CODE, produce a two-part interpretation.

Requirements:
- Part 1 [Summary]: Keyword-dense, max 50 characters, space-separated key phrases. Include: business actions, involved objects, key technical approaches. No full sentences.
- Part 2 [Detail]: Full technical interpretation covering responsibility, key logic, and call-graph relationships. Do not dump the raw code again.

### Context
{context_summary}

### Signature
{signature}

### Method body (excerpt)
```
{code_snippet[:10000]}
```

### Output (strict format)
[Summary] <keyword1 keyword2 keyword3 ...>

[Detail]
<full technical interpretation>
```

> 语言判定：`(language or "zh").lower().startswith("en")`。

**技术 runner 的 `_build_method_context`**（6 行，与拓扑略不同）：`所属类 ID / 类名 / 方法签名 / 模块 / 直接调用本方法的上游方法（节选）callers[:5] / 本方法直接调用的下游方法（节选）callees[:8]`。**class_id 解析带类型校验**：要求 `CONTAINS` source 的实体 `type in (CLASS, INTERFACE)`（拓扑 `_interpret_one` 无此校验）。`related_ids = list(rid_set)[:24]`。

**关键区别**：技术解读 `code_snippet[:10000]`；拓扑业务解读 `code[:8000]`。技术解读无 callee summary 注入；拓扑有。

---

## 三、LLM 输出格式与共用管道

### 3.1 输出格式约定

```
[摘要] 业务关键词1 业务关键词2 ...   ← ≤50 字，空格分隔
[详情]
完整解读文本...
```
英文 `[Summary]` / `[Detail]`。常量：`SUMMARY_PREFIX = "[摘要]"`，`DETAIL_PREFIX = "[详情]"`。

### 3.2 `interpret_one_llm_embed_store`（共用，`interpretation_item_helpers.py`）

```
interpret_one_llm_embed_store(
  runner: BaseInterpretationRunner,
  label: str,
  phase: InterpretPhase,   // TECH="tech" | BIZ="biz"
  *,
  llm: Any,
  prompt: str,
  timeout: int,
  min_text_len: int,       // 技术 runner 传 10
  embedding_dim: int,
  persist: Callable[[str, list[float]], tuple[bool,bool]],  // 返回 (success, created)
) -> tuple[int,int]        // (ok_delta, fail_delta) ∈ {(1,0),(0,1)}
```

步骤：① `runner.start_item(label, phase)`；② `llm.generate(prompt, timeout=timeout)`，`not raw_text or len < min_text_len` → `(0,1)`；③ `clean_think_tags`，`len < min_text_len` → `(0,1)`；④ `extract_summary`；⑤ `get_embedding(summary, embedding_dim)`（**只摘要**）；⑥ `persist(text, vec)`，`success` → `complete_item(label, created)` 返 `(1,0)`，否则 `(0,1)`；⑦ 捕获 `INTERP_ITEM_RECOVERABLE_EXCEPTIONS` → `(0,1)`，其它 Exception → 打 `_LOG.exception` + `(0,1)`。

> 注意：技术 runner 的 `_persist` **不调用** `interpret_one_llm_embed_store` 之外的 embedding——embedding 由该 helper 完成，`persist` 只负责写库。拓扑 `_interpret_one` **不走** 此 helper（自带 LLM+embedding+写库逻辑），但清洗/摘要/embedding 行为等价。

### 3.3 文本清洗

`clean_think_tags(text)`: `re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()`

`extract_summary(text)`:
- 含 `[摘要]`：`after = text.split("[摘要]",1)[1]`；含 `[详情]` → 取 `[详情]` 前，否则取首个 `\n` 前；strip；`len>50` → `[:50]`；非空则返回。
- 否则回退：逐行 strip + `lstrip("#*- ")`，首个 `len>=5` 的行 `[:50]`；全空 → `text[:50]`。

`INTERP_ITEM_RECOVERABLE_EXCEPTIONS = (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError)`。

---

## 四、落库结构（TopologicalInterpretation Collection）

### 4.1 Collection 名

`DEFAULT_COLLECTION_TOPOLOGICAL_INTERPRETATION = "TopologicalInterpretation"`（`weaviate_defaults.py:12`）。技术解读与拓扑解读**共用同一 collection**。
默认连接：`DEFAULT_WEAVIATE_HTTP_URL = "http://localhost:8080"`，`DEFAULT_WEAVIATE_GRPC_PORT = 50051`。

### 4.2 Schema Properties（`_schema_properties`，全 TEXT）

| Property | 写入截断 | 说明 |
|---|---|---|
| `method_entity_id` | — | 图谱方法节点 ID，关联键 |
| `class_entity_id` | `or ""` | 所属类实体 ID |
| `class_name` | `[:500]` | 类简名 |
| `method_name` | `[:300]` | 方法名 |
| `signature` | `[:2000]` | 方法签名 |
| `interpretation_text` | `[:48000]` | 完整 LLM 输出（摘要+详情） |
| `context_summary` | `[:12000]`（store 层） | 上下文文本 |
| `language` | `or "zh"` | "zh" \| "en" |
| `related_entity_ids_json` | `[:8000]` | JSON 数组，调用方传时已 `[:24]` |

向量：`vector`，维度 `= dimension`（store 默认 64；TopologicalInterpreter 注入 1024）。**对 `summary` embedding，非全文。**

> **修正4（双重截断）**：`context_summary` 在 store 层截 `[:12000]`；但 `method_interpretation_runner._persist` **先**传入 `cctx[:4000]`（预截 4000）。拓扑 `_interpret_one` 不预截，靠 store 层 12000。TS 须复刻这一差异：技术解读路径 context 有效上限 4000，拓扑路径 12000。

### 4.3 UUID 生成规则（修正1）

`uid = _to_uuid(method_entity_id + "|interpret")`，其中：

```python
@staticmethod
def _to_uuid(s: str) -> str:
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
```

**不是 UUID v5**——是 SHA256 hex 取前 32 字符按 8-4-4-4-12 切片。TS 必须按位复刻（`crypto.createHash('sha256')`），否则与 Python 写入的 UUID 不一致，断点续跑/孤儿删除全失效。

### 4.4 Multi-Tenancy（修正5）

`_resolve_collection(tenant)`：`tenant` 非空 → `coll.with_tenant(tenant)`；`tenant` 为 None → 打 deprecation `warning` 后返回普通 collection（legacy 全局视图）。

- **技术解读 runner**（`method_interpretation_runner._persist`）：传 `tenant=project_id`（v2.0 多租户写路径）。
- **拓扑解读**（`TopologicalInterpreter._interpret_one`）：**不传 tenant** → 走 legacy 路径（会打 warning）。

### 4.5 写入逻辑（`add_with_created`，Upsert）

```python
if not vector or len(vector) < self._dim:
    return False, False
coll = self._resolve_collection(tenant)
uid = self._to_uuid(method_entity_id + "|interpret")
props = {... 各字段按 §4.2 截断 ...}
vec = vector[: self._dim]
try:
    coll.data.insert(properties=props, vector=vec, uuid=uid)
    return True, True       # 新建
except Exception as e:
    if "already exists" in str(e).lower() or "422" in str(e):
        coll.data.replace(uuid=uid, properties=props, vector=vec)   # 覆盖
        return True, False
    return False, False     # 失败
```

> 前置守卫：`vector` 为空或 `len(vector) < dim` 直接 `(False, False)`，不写库。`vec = vector[:dim]` 截断到维度。`add()` 是 `add_with_created` 的薄封装，只返 `ok`。

### 4.6 读取接口

- `get_by_method_id(method_entity_id)` — **无 tenant**（legacy）；用 `method_entity_id_variants(eid)` 兼容 ID 变体逐一 `fetch_objects(limit=1)`。
- `get_by_entity_with_tenant(entity_id, *, tenant, level=None)` — `with_tenant(tenant)` 版（Task 23 补齐；`level` 为预留参数，解读库无层级过滤，仅保签名兼容）。
- `list_existing_method_ids(limit=100000)` — 分页 `page_size=2000`（`offset` 递增；`TypeError` 时退化为单页 `fetch_objects(limit=target)`）；返回 `set[str]`。
- `count()` — `aggregate.over_all(total_count=True).total_count`；失败 fallback `len(list_existing_method_ids(limit=200000))`，再失败返 0。
- `search_by_text(query_text, top_k=10)` — `get_embedding` + `near_vector_property_hits`，返回 `[(method_entity_id, float(score))]`（score 越大越相似）。

---

## 五、断点续跑

### 5.1 技术解读 Runner（无跨轮状态文件）

1. `existing_ids = store.list_existing_keys()`（adapter → `list_existing_method_ids(limit=100000)`）。
2. 孤儿清理（见 §6.1），`existing_ids -= orphan_ids`。
3. `already_done = sum(1 for m in all_methods if m.id in existing_ids)`。
4. `todo_methods = [m for m in all_methods if m.id not in existing_ids]`；`max_m > 0` 时 `todo_methods[:max_m]`。
5. **无状态文件**：重启靠 Weaviate 现存集合判定。
6. 诊断分支：`already_done==0 and total_candidates>0` 时，按 `store.count()` 与 `existing_ids` 是否一致打不同提示（连接问题 / entity_id 不匹配）。

### 5.2 拓扑解读（`state_file`）

`out_ui/interpretation_state.json` 结构：
```json
{
  "L0": {
    "status": "COMPLETE",   // COMPLETE | PARTIAL | PARTIAL_ACCEPT | GATE_FAILED
    "permanent_failed": ["mid1","mid2"],   // sorted
    "timestamp": 1718000000.0
  }
}
```
- 重启 `_load_state` → 按层恢复 `permanent_failed`，不重试。
- 已完成方法 summary 从 `get_by_method_id`（无 tenant）恢复到 `_summary_cache`（供上层 prompt）。
- `_save_state` 每次 `_load_state` 合并后整体重写（`indent=2, ensure_ascii=False`），`mkdir(parents=True, exist_ok=True)`。

---

## 六、一致性同步（孤儿清理）

### 6.1 技术解读 Runner（开始前一次性）

```python
valid_method_ids = {e.id for e in all_methods}
existing_ids = store.list_existing_keys()
orphan_ids = existing_ids - valid_method_ids
for oid in orphan_ids:
    uid = weaviate_store._to_uuid(oid + "|interpret")
    weaviate_store._get_collection().data.delete_by_id(uid)   # 无 tenant
existing_ids -= orphan_ids
```
（单条删除失败 `except: pass`，不影响主流程。）

### 6.2 拓扑解读（`run()` Step 3）

```python
existing_ids = store.list_existing_method_ids(limit=200000)
orphans = existing_ids - set(meaningful)
for oid in orphans:
    uid = store._to_uuid(oid + "|interpret")
    store._get_collection().data.delete_by_id(uid)
```

> 两者均通过**默认（无 tenant）collection** 删除，即便技术 runner 写入时带了 tenant。TS 移植需注意：多租户写入 + 无租户删除，孤儿清理可能删不到 tenant 分区的对象（这是源码现状的潜在不一致，照实复刻 + 标注）。

---

## 七、召回重排（`recall_rerank.py`）

### 7.1 `classify_entity(entity_id) -> str`

entity_id 格式：`com.pkg.ClassName::methodName#(ParamType ParamName,...)`。解析：`split("#",1)[0]` → 无 `::` 返 `"neutral"` → `partition("::")` → `qualified_class.rsplit(".",1)[-1]` = simple_class。

| 分类 | 条件（按代码顺序） | 排序调整 |
|---|---|---|
| `drop` | `method == "Base_Column_List"` 或 `simple_class.endswith("Example")` | 硬过滤 |
| `boost` | `simple_class.endswith(("Controller","ServiceImpl","Service"))` | `adj = score + boost` |
| `demote` | `_is_accessor(method)` 或 `method in _GENERATED_BY_EXAMPLE` | `adj = score - demote` |
| `neutral` | 其余 | `adj = score` |

`_GENERATED_BY_EXAMPLE = frozenset{selectByExample, countByExample, deleteByExample, updateByExample, updateByExampleSelective}`。
`_is_accessor`：前缀 `get/set/is` 后第一个字符 `isupper()`（如 `isDeleted`=True，`issueRefund`=False）。

### 7.2 `rerank_and_filter(hits, limit, *, boost=0.05, demote=0.05) -> list[(str,float)]`

- 遍历 hits，`drop` 跳过，其余算 `adj`，存 `(eid, 原始score, adj)`。
- 全 drop 空兜底：`return list(hits[:limit])`。
- `scored.sort(key=lambda t: t[2], reverse=True)`（稳定排序）。
- 返回 `[(eid, 原始score) for ... in scored[:limit]]` — **adj 仅排序用，返回值始终原始 cosine 分**。

### 7.3 `is_callchain_noise(entity_id) -> bool`

`classify_entity in ("drop","demote")` → True；额外 `simple_class in _RESULT_WRAPPER_CLASSES = {"CommonResult","IErrorCode"}` → True。空串/格式异常 → False（保守保留）。
区别：召回里 `demote` 是降权保留；调用图里 `demote` 直接剔除为噪声。

---

## 八、业务三层解读描述符（`BusinessInterpretTierSpec`）

`business_interpretation_strategies.py`（23 行，`@dataclass(frozen=True)`）：
```python
items: Sequence[Any]
msg_prefix: str
min_text_len: int
pct_cap: int
label_fn: Callable[[Any], str]
prompt_fn: Callable[[Any], str]
add_kwargs_fn: Callable[[Any, str], dict[str, Any]]
```
意图：类/API/模块三层共用同一执行循环。`BusinessInterpretLevel(str, Enum)`：`API="api" / CLASS="class" / MODULE="module"`。

**当前状态（修正13）**：描述符 + `BusinessInterpretationStoreAdapter`（键 `tuple[str,str]`）已定义，但：
- **无 runner 调用描述符**（`business_interpretation_runner.py` 不存在）。
- `BusinessInterpretationStoreAdapter.list_existing_keys` 调 `store.list_existing_entity_level_pairs(limit=...)`——该方法在 `WeaviateTopologicalInterpretStore` **未实现**，调用即 `AttributeError`。
- TS 重写若要实现业务三层 runner，需：① 自建执行循环（参照 `interpret_one_llm_embed_store` 管道 + `BusinessInterpretTierSpec` 三个构造器）；② 实现 `list_existing_entity_level_pairs`（断点续跑键为 `(entity_id, level)` 对）。

---

## 九、魔法数字汇总

| 常量 | 值 | 位置 |
|---|---|---|
| `MAX_PROMPT_CHARS` | 60000 | callchain_interpreter |
| 截断保护阈值 | `remaining > 300` | callchain `_build_prompt` |
| LLM 超时（模式B 默认） | 120 s | `interpret()` |
| LLM 超时（拓扑 `llm_timeout`） | 90 s | TopologicalInterpreter |
| LLM 超时（技术 runner） | `mi.timeout_seconds or 120` | `_run_items` |
| max_workers（拓扑） | 8 | TopologicalInterpreter |
| max_workers（技术 runner） | 4 | `getattr(mi,"max_workers",4) or 4` |
| batch_size（拓扑） | `max(max_workers*3, 20)` | `_interpret_level` |
| batch_size（技术 runner） | `max(max_workers*2, 20)` | `_run_items` |
| embedding_dim（拓扑默认） | **1024** | TopologicalInterpreter.__init__ |
| embedding dim（技术 runner） | **`vinterp.dimension or 64`** | run_method_interpretations |
| store 默认 dimension | **64** | WeaviateTopologicalInterpretStore.__init__ |
| layer_gate 默认 | 1.0 | TopologicalInterpreter |
| max_retry_cycles 默认 | 5 | TopologicalInterpreter |
| retry_delays | [60,300,1800,3600,7200] s | TopologicalInterpreter |
| 孤儿扫描 limit（拓扑） | 200000 | run() |
| 孤儿扫描 limit（技术 runner） | 100000（adapter 默认） | list_existing_keys |
| `list_existing_method_ids` 默认 limit | 100000 | store |
| 已完成扫描 page_size | 2000 | list_existing_method_ids |
| 摘要最大长度 | 50 | extract_summary |
| 下层 summary 注入上限 | 2000 字符 | `_build_callee_summaries` |
| Bean 字段注释上限 | 1500 字符 | `_build_bean_field_context` |
| 上游调用方最多 | 5 | context summary |
| 下游被调用最多 | 8 | context summary |
| related_entity_ids 最多 | 24 | `_get_related_ids` / `_build_method_context` |
| code 截断（技术 runner） | `[:10000]` | `_build_prompt` |
| code 截断（拓扑业务） | `[:8000]` | topological `_build_prompt` |
| interpretation_text 写入上限 | `[:48000]` | store |
| context_summary 写入上限 | `[:12000]`（store）/ `[:4000]`（技术 runner 预截） | — |
| min_text_len（技术 runner） | 10 | — |
| boost/demote 幅度 | ±0.05 | rerank_and_filter |
| 多 provider 退避 | [30,60,120] s + 0-5s 抖动，3 轮 | multi_provider |

---

## 十、怪癖与注意事项

1. **技术解读 vs 拓扑业务解读用不同 prompt**：技术 runner = "资深 Java 工程师"技术视角（含技术手段）；拓扑 = "企业代码业务架构师"业务视角（含严格禁词表）。两套都要实现，不可复用同一模板。
2. **embedding 只对摘要**：全文存 `interpretation_text`，向量来自 `[摘要]` 部分（短文本高密度）。
3. **接口→实现跳转两路**：先按 entity ID 查 `_iface_to_impls`，未命中再按 `(class_name, method_name)` 走 `_find_impls_by_name`；多候选优先取有 code_snippet 的、只取一个。
4. **多实现展开为同层分支**：`depth` 不变并行入队，接口节点仍留链路。
5. **`<think>` 清洗两种实现**：模式B `index("</think>")` 切片（要求 `<think>` 和 `</think>` 都在）；helpers/拓扑 `re.sub(..., re.DOTALL)`。TS 都要支持。
6. **孤儿清理时机/口径**：技术 runner 开始前一次性（limit=100000）；拓扑 `run()` Step3（limit=200000）。两者均经**无 tenant** collection 删除（修正6/§6.2 警告：可能删不到 tenant 分区对象）。
7. **`json.dumps` 编码不一致**：拓扑 `_interpret_one` 用默认 `ensure_ascii=True`；技术 runner 用 `ensure_ascii=False`。related_entity_ids_json 在两条路径里编码风格不同。
8. **`state_file` 仅拓扑使用**，技术 runner 无跨轮状态文件。
9. **SQL + DDL 组合注入**：拓扑 `_get_code_with_sql` 为 `Java代码 + "\n\n-- [MyBatis SQL]\n{sql}" + 逐表 "\n\n-- [表结构 {table}]\n{ddl}"`；模式B `combined` 用 `-- ========== MyBatis SQL ==========` 分隔（两者格式不同，分别复刻）。
10. **`_to_uuid` 非 UUID v5，是 SHA256 切片**（见 §4.3）——最高风险一致性点。
11. **英文拓扑 prompt 的 `bean_section` 引用未定义变量**（见 §2.4），TS 英文路径应将其视为空串。
12. **class_id 解析差异**：技术 runner `_build_method_context` 要求 CONTAINS source 是 CLASS/INTERFACE；拓扑 `_interpret_one` 取首个 CONTAINS source 无类型校验。
13. **多租户写、无租户删**：技术 runner 写带 tenant，但断点续跑读（`list_existing_method_ids`/`get_by_method_id`）和孤儿删都走无 tenant collection。这是源码现状的潜在不对称，照实移植并标注。

---

## 十一、多 Provider 负载均衡（修正2，原 spec 完全遗漏）

源：`src/knowledge/llm/factory.py` + `src/knowledge/llm/multi_provider.py`。任务最高优先项之一，TS 必须实现。

### 11.1 触发与装配

`LLMProviderFactory.from_method_interpretation(mi)` → `create_with_meta(backend=mi.llm_backend, ...)`。当 `llm_backend == "multi"` 走 `_build_multi_backend`：
- 需配置 `multi_providers: list[dict]`（缺失 → `ValueError`）。
- 每项 `{backend, name?, openai_base_url, openai_api_key, openai_model, ...}`，`backend` 默认 `"openai"`；未知 backend → `ValueError(f"multi_providers[{i}]: 未知 backend ...")`。
- 逐项用对应 builder 构造子 provider，组装 `MultiProvider(providers, names)`。
- `LLMProviderSelection.resolved_backend = f"multi({','.join(names)})"`。

`LLMProviderSelection` 字段：`provider, requested_backend, resolved_backend, fallback_reason(默认"")`。单 backend（openai/anthropic）失败可按 `llm_allow_fallback_to_ollama` 回退 Ollama 并填 `fallback_reason`。

### 11.2 `MultiProvider.generate(prompt, **kwargs)` 算法

- 构造：`itertools.cycle(range(len(providers)))`、`threading.Lock`、`_call_counts[]`、`_backoff_count`。空 providers → `ValueError`。
- 外层 `for attempt in range(len(_BACKOFF_DELAYS)+1)`（最多 1+3=4 轮）：
  - 加锁 `start_idx = next(self._cycle)`（**round-robin 起点轮转**）。
  - 内层按 `(start_idx + offset) % N` 遍历所有 provider：
    - 成功 → 加锁 `_call_counts[idx]+=1`，**立即返回结果**。
    - 失败 → 判 `_is_rate_limit_error`（429 / rate_limit / rate limit / throttling / quota exceeded / usage limit / too many requests，大小写不敏感）；非限流则 `all_rate_limited=False`；记 `last_error`，`continue` 下一个。
  - 内层结束：`not all_rate_limited`（存在非限流错误）→ `break`，直接抛 `RuntimeError(...) from last_error`，**不退避**。
  - 全部限流且 `attempt < 3` → `delay = _BACKOFF_DELAYS[attempt]`（`[30,60,120]`）+ `random.uniform(0,5)` 抖动，`_backoff_count+=1`，`time.sleep`。
  - 全部限流且耗尽重试 → `_LOG.error` 放弃。
- 最终 `raise RuntimeError(f"MultiProvider: 所有 {N} 个 Provider 均失败") from last_error`。
- `stats()` 返回 `{name: call_count, "_backoff_count": n}`。

`_BACKOFF_DELAYS = [30, 60, 120]`（秒）。容错策略注释：① 单 provider 失败立即切下一个；② 全 429 → 指数退避最多 3 轮；③ 非限流错误不退避直接抛。

> TS 移植要点：round-robin 起点必须在并发下用锁轮转（多线程/多 worker 共享同一 `cycle`）；限流判定的关键词表逐字复刻；退避带抖动避免雪崩；"任一非限流错误即放弃整轮"的短路语义不能丢。