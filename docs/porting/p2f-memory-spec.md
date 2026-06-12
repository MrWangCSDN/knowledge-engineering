# P2f 记忆 LLM 层行为规范（py-final-baseline 提取，经对抗核验）

> TS 移植权威。2026-06-12 提取+对抗核验。
> 关键：① embedder 已迁 DashScope text-embedding-v4（Python 源 recall.py 残留 Ollama 旧注释，勿照搬）；② 维度 1024 出现在 3 处须一致；③ 对象 UUID=sha256(uri)[:32] 重排 8-4-4-4-12（确定性派生非 UUID5）；④ archive 过滤两种写法（/archive/ in uri vs name==archive，勿混）。
> QA 接入点：withMemoryBlock prepend 注入 memory_block（session_block + global memory_block）；post-turn MemoryExtractor 抽取写 MemoryFS。当前 P2e 的 QA 只持久化消息，本层接入后才有记忆增强。



---

# 【记忆召回 + 服务编排 + context_budget】

I now have everything needed to verify every claim in the spec adversarially. I found several discrepancies. Let me compile the corrected authoritative spec with the correction list at the top.

The key findings:
1. **Embedder backend** — spec §14.1 says "底层走 urllib.urlopen (Ollama HTTP) timeout=60s". The actual code (`embedding.py` docstring lines 1-7) confirms it migrated to **DashScope text-embedding-v4 via httpx**, not Ollama/urllib. But the source comment in `recall.py` line 304-307 still says "Ollama HTTP timeout=60s" — that's stale in the Python source itself.
2. **`_DEFAULT_RESERVE_PCT`** validation has bounds `0.0 < v < 1.0`.
3. The recall hits loop uses `kind == "file"` → direct; **else** branch (not just "dir") handles dir.
4. UUID derivation: `_to_uuid` takes sha256 hex `[:32]` then formats as `8-4-4-4-12` — but `recall._index_one` wraps it in `_uuid_mod.UUID(...)`.

<br>

# 记忆召回 + 服务编排 — Python→TS 移植权威参考（核验修正版）

## 修正清单

逐字回源 8 个文件后，对原 spec 的修正/补强（✅=原 spec 正确，已复核；⚠️=需修正；➕=原 spec 遗漏，补入）：

| # | 位置 | 原 spec | 回源事实 | 处置 |
|---|---|---|---|---|
| C1 | §14.1 embedder 后端 | "底层走 urllib.urlopen (Ollama HTTP) timeout=60s" | `src/semantic/embedding.py` 已迁 **DashScope text-embedding-v4**（httpx，endpoint `dashscope.aliyuncs.com/.../text-embedding`，`RETRY_TIMES=3`，退避 `(1,2,4)s`，`DASHSCOPE_API_KEY` 缺失/重试耗尽抛 `EmbeddingError`）。注：`recall.py` 的 `_DefaultEmbedder` docstring 行 304-307 仍写 "Ollama HTTP timeout=60s" — **这是 Python 源码里残留的过时注释**，实际后端是 DashScope。TS 侧对齐 DashScope embedding，不是 Ollama。 | ⚠️ 改 |
| C2 | §9 reserve_pct env | 表里只列默认 0.45 + env `KE_CTX_RESERVE_PCT` | `_reserve_pct()` 有**值域护栏**：仅当 `0.0 < v < 1.0` 才采纳，否则回退 0.45（`KE_MODEL_CONTEXT_WINDOW` 同理须 `>= _MIN_WINDOW=1000`） | ➕ 补 |
| C3 | §7 召回算法 step 3 | "else: # dir" | 实际是 `if kind == "file"` 直拼 body，**else 分支**处理（`kind != "file"`，含 dir / None / 损坏值）；else 内再用 `endswith("/.abstract.md")` 兜底细分，不满足则仅拼 body | ⚠️ 改 |
| C4 | §5 / §13 对象 UUID | "SHA-256(uri)[:32] 重排为 UUID 字符串" | 精确：`_to_uuid(s) = sha256(s).hexdigest()[:32]` 后按 `8-4-4-4-12` 格式插连字符；`recall._index_one` 再 `uuid.UUID(...)` 包成 UUID 对象传给 v4 client。**不是 UUID5**（无 namespace），是确定性派生 | ➕ 精确化 |
| C5 | §10 step 3 prev_turn_count 守卫 | 未提类型校验 | step 3 读 `turn_count` 时有 `isinstance(tc, int) and tc >= 0` 校验，否则保持 `prev_turn_count=0` | ➕ 补 |
| C6 | §10 step 7 focus 窗口 | "messages[-12:]" | ✅ 正确（`_extract_focus_entity_ids(messages[-12:])`，上限 `_FOCUS_MAX=10`，首见序去重） | ✅ |
| C7 | §3.7 estimate_tokens | "ceil(len/1.5)" | ✅ 正确，且 `text` 为 None/空 → 返 0（短路） | ✅ |
| C8 | §2 URI archive 过滤 | "index时跳过 `/archive/`" | ✅ 但需补：`index_changed` 用 `"/archive/" in uri`；`_gen_dir_l0_l1` 与 `_supersede_identity` 用 `name == "archive"`（目录名精确等值）。**两种匹配方式不同**，移植勿混 | ➕ 精确化 |
| C9 | §3.4 compact 签名 | 列了 `db` 无、`every_n_messages=6`、`force=False` | ✅ 正确。S6 后已删 `db` 参数（step 1 改 `read_messages_for_session` 读 fs）。force=True → floor=2 / min_delta=1 | ✅ |
| C10 | §8 注入顺序 | "session 在前 / global 在后" | ✅ 正确（qa_router 5c：`session_block + "\n\n" + memory_block`） | ✅ |
| C11 | §11 turn_text | `f"用户：{question}\n助理：{answer}"` | ✅ 逐字正确（qa_router L781） | ✅ |
| C12 | §6.1 注入模板 | 三行 box + `{block}` | ✅ 逐字正确，且 `with_memory_block` 是 **prepend**（`template.format(...) + system`），模板末尾含 `\n\n` | ✅ |
| C13 | §5 schema dimension | "1024（env 无可覆盖，代码常量）" | ⚠️ 半对：`recall.py` 常量 `_VECTOR_DIM=1024` 写死；但 `MemoryL0Store` 构造时 `dimension=1024` 由 `service.py`/`qa_router` 显式传参，底层 `embedding.DIM=1024` 也写死。无 env 覆盖正确，但维度其实出现在 3 处（须三处一致） | ➕ 精确化 |
| C14 | §11 frontmatter source | `source:"react"` | ✅ 正确（`_SOURCE_REACT="react"`） | ✅ |
| C15 | §12 identity supersede 兜底 | "`_parse_react_json` 中 kind=identity 强制 sk='identity'" | ✅ 逐字正确（extract.py L129-130），且额外：`sk` 非 `("identity", None)` 先归一为 None，再对 identity 强制 | ✅ |

其余 §1/§2 URI 布局、§3 签名、§4 层次表、§6.2–6.5 prompt 逐字、§10 八步、§11 写侧链、§12 护栏表、§13 数据结构均**逐字复核通过**，仅做下方点状增补。

---

## 1. 组件用途总览（复核通过）

| 组件 | 文件 | 用途 |
|---|---|---|
| `MemoryFS` | `vfs.py` | `ke://u/` URI → 物理文件的安全异步存储层，所有 md I/O 入口 |
| `MemoryGen` | `memgen.py` | L0（`.abstract.md`）+ L1（`.overview.md`）自底向上生成管线，含 SHA-256 幂等判定 |
| `MemoryRecaller` + `MemoryL0Store` | `recall.py` | L0 灌入 Weaviate + 向量召回 → 拼装 memory_block string；`MemoryL0Store(BaseWeaviateStore)` 提供 collection schema |
| `MemoryExtractor` | `extract.py` | post-turn ReAct LLM 调用 → JSON memories → `fs.write` + S2/S3 链 + identity supersede |
| `SessionCompactor` + `read_session_summary` + `read_messages_for_session` + `write_message_to_fs` + feedback helpers | `session.py` | post-turn 会话压缩（写 summary.md）；读侧 composer；per-message file I/O；feedback I/O |
| `recall_memory_block` + `_extract_focus_entity_ids` | `service.py` | 顶层召回 wrapper（构造 store/embedder → 委托 `MemoryRecaller`，任何异常 → `""`）+ focus 实体聚合 |
| `with_memory_block` + 全部 memory prompts | `prompts.py` | memory_block prepend 注入 + `_MEM_L0/L1/EXTRACT_SYSTEM` + `_SESSION_COMPACT_SYSTEM` + `_MEMORY_BLOCK_TEMPLATE` |
| `estimate_tokens` / `model_context_window` / `history_token_budget` / `trim_history_to_budget` | `context_budget.py` | token 预算估算 + history 裁剪（纯函数，无 IO） |

---

## 2. URI 布局（三层 — 复核通过，archive 过滤精确化见 C8）

```
ke://u/{user_id}/                         ← 用户根（租户隔离前缀；user_id ∈ [1-9][0-9]*）
├── global/
│   ├── identity/
│   │   ├── {slug12}.md                   ← S4 identity 记忆文件（当前）
│   │   ├── {slug12}.abstract.md          ← S2 生成的文件 L0
│   │   ├── archive/                       ← supersede 归档（旧 .md + 旧 .abstract.md 一并 mv 入）
│   │   ├── .abstract.md                  ← 目录 L0（S3 向量检索目标）
│   │   └── .overview.md                  ← 目录 L1 导航图
│   ├── preference/                        ← 同 identity 结构
│   └── style_feedback/                   ← 同 identity 结构
└── session/
    └── {session_id}/
        ├── summary.md                    ← 会话级 working_summary（S5）
        └── messages/
            ├── {msg_id}.md               ← 每条消息 per-file（S6）
            └── {msg_id}.feedback.md      ← feedback 文件（S7，覆盖式更新）
```

**URI 约束（vfs.py 逐字）：**
- 前缀 `ke://u/`；`_URI_PREFIX = "ke://u/"`
- user_id 正则 `_UID_RE = [1-9][0-9]*`（拒 0 / 前导零，防 "07" vs "7" 租户分歧）
- 段字符集 `_SEG_RE = [A-Za-z0-9._-]+`；精确拒 `""` / `.` / `..` / 含 `\x00`（`...` 等合法）
- `resolve()` 末段空（尾 `/`）容忍剥除；其余位置空段非法
- 越界终防线：`os.path.realpath` 后断言 `target == base or target.startswith(base + os.sep)`
- **archive 过滤两种写法**（C8）：`index_changed` → `"/archive/" in uri`；`_gen_dir_l0_l1`/`_supersede_identity` → `name == "archive"`

物理路径：`{KE_MEM_ROOT}/u/{user_id}/{rest}`，默认 `<repo_root>/.ke-memory`（`Path(__file__).resolve().parents[3] / ".ke-memory"`）。

---

## 3. 公开函数签名（逐字 — 复核通过）

### 3.1 `MemoryFS`（vfs.py）
```python
def __init__(self, root: str | None = None) -> None   # root="" → ValueError；realpath 归一
@staticmethod
def _parse_uri(uri: str) -> tuple[str, list[str]]     # → (uid, segs)
def resolve(self, uri: str) -> str
@staticmethod
def _uid_of(uri: str) -> str
async def write(self, uri: str, content: str) -> None # tempfile.mkstemp + os.replace 原子；刻意不 fsync
async def read(self, uri: str) -> str                 # 不存在→MemoryNotFound；目录→MemoryPathError
async def exists(self, uri: str) -> bool
async def ls(self, uri: str) -> list[str]             # sorted()；非目录→MemoryPathError
async def rm(self, uri: str, *, recursive: bool = False) -> None
async def mv(self, src_uri: str, dst_uri: str) -> None # 跨 user→MemoryPathError；dst 存在→MemoryPathError
```
异常类：`MemoryPathError`（路径类）、`MemoryNotFound`（不存在）。per-path `asyncio.Lock`（write/rm/mv 串行化）。

### 3.2 `MemoryRecaller`（recall.py）
```python
def __init__(self, embedder: Any, weaviate_client: Any) -> None
async def index_changed(self, fs: MemoryFS, changed_uris: list[str]) -> None
async def recall_memory_block(self, fs, query, user_id, *, top_k: int = 5) -> str
# 私有：_index_one；模块级 _kind_of_uri / _overview_uri_for_dir_l0
```
- `index_changed`：去重保序 → 跳 `/archive/` → 仅 `.abstract.md` → `_index_one`；单条 try/except → debug → continue
- embedder 鸭子接口：`async embed(text: str) -> list[float]`（1024 维）

### 3.3 `MemoryExtractor`（extract.py）
```python
def __init__(self, llm: Any) -> None
async def extract_and_persist(self, fs, memgen, recaller, *, user_id: int, turn_text: str) -> None
# 私有：_write_one_memory / _supersede_identity；模块级 _compute_slug / _now_iso_z / _parse_react_json
```

### 3.4 `SessionCompactor`（session.py）
```python
def __init__(self, llm) -> None
async def compact(self, fs, *, user_id: int, session_id: str,
                  every_n_messages: int = 6, force: bool = False) -> None
```
配套模块级函数：`read_session_summary(fs, *, user_id, session_id) -> str`、`read_messages_for_session(fs, *, user_id, session_id) -> list[_FsMessage]`、`write_message_to_fs(...)`、`write_feedback_to_fs(...)`、`read_feedback_for_message(...)`、`_summary_uri` / `_messages_dir_uri` / `_message_uri` / `_feedback_uri`。

### 3.5 顶层 `recall_memory_block`（service.py）
```python
async def recall_memory_block(fs, query: str, user_id: int, *, top_k: int = 5) -> str
```
内部：读 env（`WEAVIATE_URL` 默认 `http://127.0.0.1:8080`、`WEAVIATE_GRPC_PORT` 默认 `50051`、`WEAVIATE_API_KEY` or None）→ 构造 `MemoryL0Store(url, grpc_port, collection_name="memory_l0", dimension=1024, api_key=...)` → `MemoryRecaller(_DefaultEmbedder(), store._client)` → 委托。**任何异常 → `""`**（含 store 构造期 ConnectionError）。

### 3.6 `with_memory_block`（prompts.py — 逐字）
```python
def with_memory_block(system: str, memory_block: str | None) -> str:
    if not memory_block or not memory_block.strip():
        return system
    return _MEMORY_BLOCK_TEMPLATE.format(block=memory_block.strip()) + system
```

### 3.7 context_budget（逐字 — 含 C2 护栏）
```python
def estimate_tokens(text: str | None) -> int        # not text → 0；else ceil(len/1.5)
def model_context_window() -> int                   # env KE_MODEL_CONTEXT_WINDOW；须 >= _MIN_WINDOW(1000) 否则 1_000_000
def _reserve_pct() -> float                          # env KE_CTX_RESERVE_PCT；须 0.0 < v < 1.0 否则 0.45
def history_token_budget() -> int                    # int(window × (1 − reserve_pct))
def trim_history_to_budget(history, budget) -> tuple[list[dict], int]
```

---

## 4. 记忆层次（复核通过）

| 层 | URI 模式 | 写时机 | 读时机 |
|---|---|---|---|
| **用户级 / global** | `ke://u/{uid}/global/{kind}/{slug12}.md` | post-turn S4 ReAct 抽取 | 每次 QA 前 `recall_memory_block` → Weaviate L0 向量召回（命中 dir L0 展开 L1） |
| **会话级 / summary** | `ke://u/{uid}/session/{sid}/summary.md` | post-turn S5 `SessionCompactor.compact` | 每次 QA 前 `read_session_summary` → 拼到 memory_block **头部** |
| **会话级 / messages** | `ke://u/{uid}/session/{sid}/messages/{msg_id}.md` | `persist_messages` 回调 | `read_messages_for_session`（compact step 1） |

L0 = 文件/目录都有，≤100 tok，Weaviate 检索目标；L1 = 仅目录有，≤约1500 字，命中 dir L0 时展开。

---

## 5. Weaviate collection schema（复核 + C4/C13 精确化）

- **collection name：`memory_l0`**（`_COLLECTION_NAME`，钉死）
- **multi-tenancy：开启**；tenant = `str(user_id)`；`BaseWeaviateStore` 配 `enabled=True, auto_tenant_creation=True, auto_tenant_activation=True`
- **vector dimension：1024**，三处一致：`recall._VECTOR_DIM=1024`、`MemoryL0Store(dimension=1024)`（service.py / qa_router 传参）、`embedding.DIM=1024`（无 env 覆盖）

Properties（`_schema_properties` 逐字）：`uri`(TEXT) / `kind`(TEXT, "file"|"dir") / `hash`(TEXT, file=src_hash/dir=inputs_hash) / `body`(TEXT, 脱 frontmatter)。

**对象 UUID（C4 精确）**：`obj_uuid = uuid.UUID(BaseWeaviateStore._to_uuid(uri))`，其中
```python
_to_uuid(s) = sha256(s).hexdigest()[:32]，再插连字符成 "8-4-4-4-12"
```
非 UUID5（无 namespace），纯确定性派生。

**upsert 判定**：`existing is not None and existing.properties.get("hash") == fresh_hash` → skip（零 embedding）；否则 existing is None → `data.insert`，else → `data.replace`。
**tenant 首写护栏**：`view.query.fetch_object_by_id` 抛任意异常（含 "tenant not found"）→ 吞 → `existing=None` → 继续 insert（写操作自动建 tenant）。

---

## 6. LLM Prompt 逐字（全部复核通过 — 与 prompts.py L582-659 一字不差）

### 6.1 `_MEMORY_BLOCK_TEMPLATE`（prepend，含末尾两空行）
```
═══════ 记忆（关于本用户 / 本次会话的已知事实，优先参考）═══════
{block}
═══════════════════════════════════════════════════════════════

```
（模板字符串末尾是 `…══\n\n`，prepend 到 system 前。）

### 6.2 `_MEM_L0_SYSTEM`
```
你是记忆摘要器。把给定文本压成一句可独立检索的中文摘要：聚焦关于本用户的稳定事实 / 偏好 / 身份，不超过约 100 token，不要前缀、不要解释、不要分点编号，直接输出摘要正文本身。
【关键约束 — 严禁虚构】只能复述【输入文本中已经明确出现】的事实。禁止补充、推测、扩写、关联任何输入未提及的信息（如年龄/籍贯/职业/家庭/学历/兴趣等若输入未写就一律不准出现）。如输入只是「张三」一个名字，输出也只能围绕「张三」这个名字本身，绝不许编出生年份、城市、爱好等。事实越少摘要越短是正常的。
```

### 6.3 `_MEM_L1_SYSTEM`
```
你是记忆导航图生成器。给你某目录下若干子项的摘要（以「## 子项名」分节）。聚成一张导航图：有哪些记忆条目、各自讲什么、需要时如何进一步查看其正文。中文，不超过约 1500 字，结构清晰可作为该子树索引；直接输出导航图正文，不要前缀、不要额外解释。
【关键约束 — 严禁虚构】只能复述【输入子项摘要中已经明确出现】的事实。禁止补充、推测、扩写、关联任何输入未提及的信息（如生平、教育、职业、家庭、社交、未来规划等若输入未写就一律不准出现）。子项内容稀少时，导航图也应相应简短；事实越少导航图越短是正常的、可接受的。
```

### 6.4 `_MEM_EXTRACT_SYSTEM`
```
你是用户记忆抽取器。给你一段用户与助理的对话，抽取所有值得长期记住的关于本用户的事实，分类为 preference / identity / style_feedback：
- identity：用户身份/姓名/自我称呼/角色（必含 supersedes_kind='identity'，会取代旧身份事实，先更新不并存重复，避免「王山河→李龙飞」类 bug）；
- preference：用户长期偏好（语言、风格、领域、工程范畴等）；
- style_feedback：用户对回答风格/格式/长度的反馈；
【关键约束 1 — 信源】只抽取**用户**【明确声明】的事实（「我叫X」「记住我喜欢Y」「我是Z角色」等用户陈述句）。【绝不】把助理回复里的内容当作用户事实抽取——助理可能猜测、幻觉、复述（如「你是北京的软件工程师」可能是 LLM 编的，并非用户说过）。助理回复的内容**仅供你理解上下文**，不可作为事实来源。
【关键约束 2 — 句式】疑问/澄清/引用不是事实——「我叫什么」「我的名字是什么」「你刚才说X」「你也知道啊」等返回空数组。
输出严格 JSON：{"memories":[{"kind":...,"content":"第三人称陈述事实","supersedes_kind":null|"identity"}]}。kind=identity 时 supersedes_kind 必须填 'identity'，禁止填 null。本轮无可记则 {"memories":[]}。只输出 JSON 对象本身，不要代码块、不要解释。
```
注：源码中 `"## 子项名"` 在 L0/L1 是文案，`{"memories":...}` 这段用单引号字符串拼接以容纳内部双引号 — 移植时保持 JSON 示例双引号。

### 6.5 `_SESSION_COMPACT_SYSTEM`
```
你是对话记忆压缩器。基于【已有会话摘要】（若有）与【新增对话】，输出一段更新后的会话摘要，忠实保留对后续有用的关键信息：用户陈述的事实与偏好及其先后/演变时间线、已确认的结论、当前状态、未决问题。不得丢弃【已有会话摘要】中的既有事实——把新信息融合进去，有变化则标注演变。不超过 300 字，中文，直接输出摘要正文，不要前缀、不要解释、不要分点编号。
```

---

## 7. `recall_memory_block` 召回算法（C3 修正版 — recall.py L227-298 逐字）

```
1. q_vec = await embedder.embed(query)          # 失败 try/except → return ""
2. coll = weaviate_client.collections.get("memory_l0")
   view = coll.with_tenant(str(user_id))
   result = view.query.near_vector(near_vector=q_vec, limit=top_k)   # top_k=5；失败 → return ""
   hits = result.objects
3. parts = []
   for h in hits:                                # 单 hit try/except，失败 continue 不连累
     props = h.properties
     kind = props.get("kind")
     body = (props.get("body") or "").strip()
     if not body: continue                       # 空 body 防御跳过
     if kind == "file":
         parts.append(body)
     else:                                        # ← C3：else 覆盖 dir / None / 损坏值
         dir_l0_uri = props.get("uri") or ""
         if not dir_l0_uri.endswith("/.abstract.md"):
             parts.append(body); continue         # 防御：非 dir 形态仅拼 body
         ovr_uri = _overview_uri_for_dir_l0(dir_l0_uri)   # /.abstract.md → /.overview.md
         try:
             ovr_raw = await fs.read(ovr_uri)
             _, ovr_body = _split_frontmatter(ovr_raw)
             parts.append(body + "\n---\n" + ovr_body.strip())
         except MemoryNotFound:
             parts.append(body)                    # L1 缺失 fallback 仅 L0
4. return "" if not parts else "\n\n".join(f"- {p}" for p in parts)
```

---

## 8. memory_block 注入 QA prompt 组装（qa_router L424-458，复核通过）

```python
# 5a. global L0 向量召回（防御性 try → ""）
memory_block = await recall_memory_block(MemoryFS(), body.question, user_id=user.id, top_k=5)
# 5b. 会话 summary 读侧（独立 _MemFS()，独立 try → ""）
session_block = await read_session_summary(_MemFS(), user_id=user.id, session_id=session_id)
# 5c. 拼装（session 在前，global 在后）
if session_block and memory_block:
    memory_block = session_block + "\n\n" + memory_block
elif session_block:
    memory_block = session_block
# 传入 synthesizer：with_memory_block(SYSTEM, memory_block) prepend 注入
```

---

## 9. context_budget 压缩（qa_router step 6，C2 补强）

| 常量 | 默认值 | env 覆盖 + 护栏 |
|---|---|---|
| `_DEFAULT_WINDOW` | 1,000,000 | `KE_MODEL_CONTEXT_WINDOW`，须 `int >= _MIN_WINDOW` |
| `_MIN_WINDOW` | 1,000 | — |
| `_DEFAULT_RESERVE_PCT` | 0.45 | `KE_CTX_RESERVE_PCT`，须 `0.0 < float < 1.0` |

公式：`history_budget = int(window × (1 − reserve_pct))` = 默认 550,000。
`trim_history_to_budget`：`history` 非 list/空/budget≤0 → `([],0)`；`reversed(history)` 累加 `estimate_tokens(str(m.get("content","")))`，`if kept_rev and used + t > budget: break`（**至少保留最新 1 条**），最后 `kept_rev.reverse()`。非 dict 项 `continue` 跳过。
qa_router step 6：`history_trimmed = _raw_n > len(eff_history)`，传 `force_compact=history_trimmed` 给 writer → `compact(force=True)`（floor=2, min_delta=1）。异常 → `eff_history = body.history` 原样回退、`context_usage = None`。

---

## 10. SessionCompactor 8-step（session.py L91-184 逐字，C5 补）

```
step 1: messages = read_messages_for_session(fs, user_id, session_id)   # 升序；目录无→[]
        msg_count = len(messages)
step 2: floor = 2 if force else every_n_messages(=6)；if msg_count < floor: return
step 3: 读 summary.md → fm,body；prev_summary = body.strip()
        tc = fm.get("turn_count")；if isinstance(tc,int) and tc>=0: prev_turn_count = tc  # ←C5
        （MemoryNotFound → prev_turn_count=0, prev_summary=""）
step 4: min_delta = 1 if force else every_n_messages；
        if msg_count - prev_turn_count < min_delta: return
step 5: new_msgs = messages[prev_turn_count:]
        parts = []
        if prev_summary: parts.append("【已有会话摘要】\n"+prev_summary)
        if new_msgs:     parts.append("【新增对话】\n"+"\n".join(f"[{m.role}] {(m.content or '')[:200]}" for m in new_msgs))
        convo = "\n\n".join(parts)
step 6: summary = (await llm.complete(system=_SESSION_COMPACT_SYSTEM, user=convo)).strip()
        if not summary: return
step 7: focus = _extract_focus_entity_ids(messages[-12:])    # 上限 10，cited_entities + entry_points
step 8: fm_new = {"turn_count": msg_count, "focus_entity_ids": focus, "updated_at": _now_iso_z()}
        await fs.write(uri, _render_frontmatter(fm_new, summary + "\n"))
```
全程外层 try/except → `_log.debug(..., exc_info=True)` → return（绝不抛）。

---

## 11. post-turn 写侧编排（qa_router `_make_memory_writer` 闭包，L730-814 逐字）

触发点：SSE 流结束 → `on_memory(answer)` 回调。`turn_text = f"用户：{question}\n助理：{answer}"`。

```
[S4 块, try/except → debug 静默]
  局部 import → fs = MemoryFS()；memgen = MemoryGen(llm)
  store = MemoryL0Store(url, grpc_port, "memory_l0", dimension=1024, api_key)
  recaller = MemoryRecaller(_DefaultEmbedder(), store._client)
  extractor = MemoryExtractor(llm)
  await extractor.extract_and_persist(fs, memgen, recaller, user_id, turn_text)
    → llm.complete(_MEM_EXTRACT_SYSTEM, turn_text)
    → _parse_react_json(raw)            # 剥 ```json 栅栏；非法/无 memories → ValueError
    → 空 memories → return（零 fs/S2/S3）
    → 每条 _write_one_memory（独立 try/except）:
        slug = sha256(content).hex()[:12]
        uri = ke://u/{uid}/global/{kind}/{slug}.md
        if supersedes_kind=="identity": _supersede_identity → mv 旧 .md + 旧 .abstract.md → archive/
        fs.write(uri, frontmatter{kind,slug,source:"react",created_at:ISO Z} + content+"\n")
    → if changed_uris:
        memgen.regenerate(fs, changed_uris)
        abstract_uris = [{slug}.abstract.md per file] + [{dir}/.abstract.md per ancestor]
        recaller.index_changed(fs, abstract_uris)

[S5 块, try/except → debug 静默]
  compactor = SessionCompactor(llm)
  await compactor.compact(fs, user_id=..., session_id=..., force=force_compact)
```
注：S5 块复用 S4 块构造的 `fs`；若 S4 在 `fs=MemoryFS()` 前抛 → S5 访问 `fs` 触 `UnboundLocalError` → 被 except 兜住静默退出。

---

## 12. 触发与护栏（复核通过 + C15）

| 位置 | 触发 | 护栏/降级 |
|---|---|---|
| 召回 5a | 每次 QA | 任何异常 → `memory_block=""`；S3 自身已 try（双层冗余） |
| summary 读 5b | 每次 QA | MemoryNotFound→""；其他 debug+"" |
| context 裁剪 6 | 每次 QA | 异常 → `eff_history=body.history`、`context_usage=None` |
| S4 抽取（post-turn） | on_memory 回调 | 全程 try→debug；empty memories→零 I/O return |
| S5 压缩（post-turn） | on_memory，S4 后 | step 2/4 早退；空 summary→return；外层 try→debug |
| L0/L1 空响应 | LLM 返空/纯空白 | `_gen_file_l0`/`_gen_dir_l0_l1` 抛 ValueError → 上层隔离 + 下轮重试（不固化坏 hash） |
| identity supersede | kind="identity" | `_parse_react_json` 强制 `sk="identity"`（C15，避免 leak bug）；先归一非法 sk 为 None |
| archive 过滤 | index/dir-gen | `"/archive/" in uri`（index）/ `name=="archive"`（dir gen, supersede） |
| Weaviate tenant 首写 | `fetch_object_by_id` 抛异常 | 吞 → existing=None → insert 自动建 tenant |
| `_supersede_identity` mv | 旧 .md/.abstract.md | mv 失败单条隔离 debug；abstract 缺失 (MemoryNotFound/MemoryPathError) 视作无需归档 |

---

## 13. 数据结构（复核通过）

`_FsMessage`（session.py dataclass）：`role:str / content:str|None / msg_metadata:dict|None / created_at:datetime(UTC-aware) / sections:list|None=None`。
`_FsFeedback`：`vote:str|None / user_id:int / comment:str|None / created_at:datetime`。

frontmatter：
- message：`role`, `created_at`(ISO Z), 可选 `sections`, 可选 `msg_metadata`；body=content+"\n"
- file L0：`{src_hash: sha256(body).hex}`；dir L0：`{inputs_hash: sha256(joined-child-L0s).hex}`
- 记忆 .md（S4）：`{kind, slug, source:"react", created_at:ISO Z}`，**dict 插入序保留**
- summary.md：`{turn_count:int, focus_entity_ids:[str…≤10], updated_at:ISO Z}`
- feedback.md：`{vote:str|None, user_id:int, created_at:ISO Z}`，body=comment

`_split_frontmatter`：CRLF/CR→LF 归一；非 `---\n` 起 → `({}, 原文)`；YAML 损坏/非 dict → `{}`；`_render_frontmatter`：`yaml.safe_dump(sort_keys=False, allow_unicode=True)`。

---

## 14. TS 移植要点（C1 关键修正）

1. **embedder 接口**：`{ embed(text: string): Promise<number[]> }`，1024 维。⚠️ **后端是 DashScope text-embedding-v4**（HTTP `https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding`，`DASHSCOPE_API_KEY`，重试 3 次退避 `1/2/4s`，batch≤10，空/空白文本 → 零向量不发请求，失败抛 `EmbeddingError`），**不是 Ollama**（`recall.py` 残留注释过时）。TS 侧 `@ke/llm` DashScope provider 对齐此 endpoint。Python `get_embedding(text, dimension=1024)` 的 `dimension` 参数被忽略（v4 固定 1024）。
2. **Weaviate client**：collection `memory_l0`，tenant=`String(user_id)`，需 `enabled / auto_tenant_creation / auto_tenant_activation` 全开。
3. **SHA-256 派生**：`slug = sha256(content).hex.slice(0,12)`；`obj_uuid = uuid(sha256(uri).hex.slice(0,32) 按 8-4-4-4-12 插连字符)`（C4，非 UUID5）。
4. **frontmatter I/O**：完整移植 `_split_frontmatter`/`_render_frontmatter`，含 CRLF 归一、YAML 损坏→`{}`、全数字 hash `str()` 防御、`sort_keys=false`/`allow_unicode`。
5. **identity 强制 supersede**：`_parse_react_json` 中 `kind=="identity"` 无论 LLM 输出什么都强制 `sk="identity"`（leak bug 关键护栏）。
6. **archive 双过滤**：`index_changed` 用 `"/archive/" in uri`；`_gen_dir_l0_l1`/`_supersede_identity` 用 `name === "archive"`（C8，两种匹配方式不同，勿混）。
7. **env 变量**：`KE_MEM_ROOT`、`WEAVIATE_URL`(默认 `http://127.0.0.1:8080`)、`WEAVIATE_GRPC_PORT`(默认 `50051`)、`WEAVIATE_API_KEY`、`KE_MODEL_CONTEXT_WINDOW`(默认 1_000_000，护栏 ≥1000)、`KE_CTX_RESERVE_PCT`(默认 0.45，护栏 `0<v<1`)、`DASHSCOPE_API_KEY`(embedding)。
8. **所有记忆 I/O 失败必须返 `""`**（`recall_memory_block` wrapper + `MemoryRecaller.recall_memory_block` 自包 + `read_session_summary` + `with_memory_block` 见空跳过 — 四处），绝不向主 QA 流抛错。

源码路径（均绝对）：
- `/Users/java/knowledge-engineering/src/service/memory/recall.py`
- `/Users/java/knowledge-engineering/src/service/memory/service.py`
- `/Users/java/knowledge-engineering/src/service/memory/context_budget.py`
- `/Users/java/knowledge-engineering/src/service/memory/vfs.py`
- `/Users/java/knowledge-engineering/src/service/memory/memgen.py`
- `/Users/java/knowledge-engineering/src/service/memory/extract.py`
- `/Users/java/knowledge-engineering/src/service/memory/session.py`
- `/Users/java/knowledge-engineering/src/service/qa_engine/prompts.py`（L582-659 prompt/template；L589 `with_memory_block`）
- `/Users/java/knowledge-engineering/src/service/qa_router.py`（L424-458 召回 5a/5b/5c；L460-483 step 6；L730-814 `_make_memory_writer`）
- `/Users/java/knowledge-engineering/src/knowledge/base_weaviate_store.py`（L30-33 `_to_uuid`；L97-99 tenant 配置）
- `/Users/java/knowledge-engineering/src/semantic/embedding.py`（L62 `get_embedding`，DashScope 后端 — C1 依据）

---

# 【记忆抽取 + 生成（extract/memgen）】

I have now verified every load-bearing claim in the spec against source. The spec is highly accurate. I found a small set of corrections and clarifications. Here is the corrected authoritative document.

---

## 修正清单

对照 `extract.py` / `memgen.py` / `prompts.py` / `recall.py` / `session.py` / `service.py` / `qa_router.py` 源码逐字核验。spec 整体高度准确（4 个 LLM prompt 逐字一致、URI 布局/frontmatter/护栏/supersede 算法全部正确）。修正项如下:

1. **`_MEM_L1_SYSTEM` 长度限制原文是「token」还是「字」**：spec 第三 prompt 引用「不超过约 1500 字」正确（源码 `_MEM_L1_SYSTEM` 写「不超过约 1500 字」）。但 spec 二、记忆层次表里写 L1 = 「≤1500 tok」**单位错误** —— 源码 prompt 用的是「字」，且 memgen.py 文件 docstring 写 L1 `≤1–2k tok`（注释里 tok/字混用）。统一为：L1 prompt 文本限「约 1500 字」，docstring 估算「≤1–2k tok」。

2. **S5 `compact` 的「8 步」step 1 描述需修正**：spec 写「`read_messages_for_session(fs, ...)` 读全部消息（fs 后 DB 已废除）」。源码 step 1 确实读全部消息得 `msg_count`，但 spec 漏了 step 1 与 step 2 之间没有显式 floor 常量名 —— 源码 `floor = 2 if force else every_n_messages`，`min_delta = 1 if force else every_n_messages`。spec 表述「normal=6」是因调用默认 `every_n_messages=6`，**但 6 不是硬编码常量，是形参默认值**；force 模式 floor=2/min_delta=1 正确。

3. **focus_entity_ids 聚合来源顺序**：spec 写「`cited_entities + entry_points`」，源码 `_extract_focus_entity_ids` 遍历 key 顺序为 `("cited_entities", "entry_points")` —— 顺序正确，且去重按**首见序**（cited_entities 优先占位），截 `_FOCUS_MAX = 10`。spec「截 10 个」正确。注意取的是 `messages[-12:]`（最近 12 条），spec 正确。

4. **`recall_memory_block` 服务层 wrapper 签名**：spec 在四、列了 `MemoryRecaller.recall_memory_block(fs, query, user_id, *, top_k=5)`，但**集成层入口**是 `service.recall_memory_block(fs, query, user_id, *, top_k=5)`（`service.py:16`，qa_router 实际调用此函数，它内部构造 store+recaller 再委托）。spec 未列出这层 wrapper，补入。

5. **recall query 来源**：spec 未说明 recall 的 `query` 是什么。源码 `qa_router.py:433` 传 `body.question`（当前用户问题原文）。补入。

6. **`_index_one` 的 upsert 实现细节**：spec 十「对象 UUID」正确，但降级表漏了一条关键修复 —— multi-tenancy collection 在 tenant 不存在时 `fetch_object_by_id` 会抛 `WeaviateQueryError("tenant not found")`，源码 `recall.py:199-203` 用 try/except 吞掉 fetch 异常视为 `existing=None`，让流程走到 `insert`（自动建 tenant）。这是新用户首次写记忆不被卡死的关键。补入降级表。

7. **`turn_text` 拼接格式**：spec 三 prompt 1 写 user 输入是「turn_text（本轮 user+assistant 对话文本拼接，由调用方组装后传入）」。精确格式为 `qa_router.py:781`：`f"用户：{question}\n助理：{answer}"`（中文冒号、单 `\n` 分隔，无 `[user]/[assistant]` 标签 —— 那是 S5 会话压缩的格式，勿混淆）。补入精确串。

8. **目录 L0 `joined` 拼接分隔符**：spec 七写 `joined = "## {key}\n{body}"` 链，但源码 `memgen.py:275` 是 `"\n\n".join(f"## {k}\n{v}")` —— 子项间用**双换行** `\n\n` 分隔。spec 漏了 `\n\n`。修正。

9. **`recall_memory_block` 输出格式**：spec 未列召回块最终拼装格式。源码 `recall.py:298`：非空时 `"\n\n".join(f"- {p}" for p in parts)`（每条 L0 以 `- ` bullet 起首，块间双换行）；dir hit 展开为 `body + "\n---\n" + ovr_body`。补入。

---

# Python → TS 移植：记忆系统 extract + memgen 权威文档（修正版）

## 一、模块总览与职责

| 模块 | 文件 | 职责 |
|---|---|---|
| `vfs.py` | `MemoryFS` | URI 解析 / 租户隔离 / 原子文件 IO |
| `memgen.py` | `MemoryGen` | L0/L1 自底向上摘要生成（两 LLM prompt）|
| `extract.py` | `MemoryExtractor` | 对话 → 记忆候选 ReAct 抽取（一 LLM prompt）+ 写入 + 触发 S2/S3 |
| `recall.py` | `MemoryRecaller` | Weaviate tenant 向量索引 + 召回为 system-prompt 块 |
| `session.py` | `SessionCompactor` | 会话级压缩摘要写 fs（一 LLM prompt）+ 消息/反馈 fs I/O |
| `service.py` | 集成层 | `recall_memory_block` wrapper（构造 store+recaller 后委托）+ `_extract_focus_entity_ids` |
| `prompts.py` | 常量 | **所有 LLM prompt 文本** + `with_memory_block` 注入点 |

调用入口（移植时的接线点）：`qa_router.py` 的 `_make_memory_writer` 闭包返回 `_writer(answer)`，由 sse_emitter 在 **QA 答完后（post-turn）** 调用。

---

## 二、记忆层次与 URI 布局

### 层次

```
L0 (.abstract.md)   prompt 限「约 100 token」可嵌入摘要  ← Weaviate 向量检索目标
L1 (.overview.md)   prompt 限「约 1500 字」导航图（docstring 估 ≤1–2k tok）  ← 仅目录有；召回命中目录时展开拼接
```

【修正 1】L1 单位是「字」不是「tok」（prompt 原文 `不超过约 1500 字`）。

### 完整 URI 布局

```
ke://u/{user_id}/
├── global/
│   ├── identity/
│   │   ├── {slug}.md               ← 用户身份记忆（单例，新写归档旧）
│   │   ├── {slug}.abstract.md      ← S2 L0 文件摘要
│   │   ├── .abstract.md            ← S2 L0 目录摘要
│   │   ├── .overview.md            ← S2 L1 导航图
│   │   └── archive/
│   │       ├── {old_slug}.md       ← 被 supersede 归档的旧 identity
│   │       └── {old_slug}.abstract.md
│   ├── preference/  ... (同上)
│   ├── style_feedback/  ... (同上)
│   ├── .abstract.md                ← global/ 目录 L0
│   └── .overview.md
└── session/
    └── {session_id}/
        ├── summary.md              ← 会话级压缩摘要（S5）
        └── messages/
            ├── {msg_id}.md
            └── {msg_id}.feedback.md
```

**slug 生成**：`_compute_slug(content) = sha256(content_utf8).hexdigest()[:12]`（12 hex = 48-bit，LLM 不出 slug，同 content 同 slug 天然幂等）。

**物理映射**：`ke://u/{uid}/x/y.md` → `$KE_MEM_ROOT/u/{uid}/x/y.md`（env `KE_MEM_ROOT` 优先；缺省 `<repo_root>/.ke-memory` 绝对路径）。

---

## 三、所有 LLM Prompt 文本（逐字，已核验一致）

### Prompt 1：`_MEM_EXTRACT_SYSTEM`（S4 ReAct 抽取）

```
你是用户记忆抽取器。给你一段用户与助理的对话，抽取所有值得长期记住的关于本用户的事实，分类为 preference / identity / style_feedback：
- identity：用户身份/姓名/自我称呼/角色（必含 supersedes_kind='identity'，  会取代旧身份事实，先更新不并存重复，避免「王山河→李龙飞」类 bug）；
- preference：用户长期偏好（语言、风格、领域、工程范畴等）；
- style_feedback：用户对回答风格/格式/长度的反馈；
【关键约束 1 — 信源】只抽取**用户**【明确声明】的事实（「我叫X」「记住我喜欢Y」「我是Z角色」等用户陈述句）。【绝不】把助理回复里的内容当作用户事实抽取——助理可能猜测、幻觉、复述（如「你是北京的软件工程师」可能是 LLM 编的，并非用户说过）。助理回复的内容**仅供你理解上下文**，不可作为事实来源。
【关键约束 2 — 句式】疑问/澄清/引用不是事实——「我叫什么」「我的名字是什么」「你刚才说X」「你也知道啊」等返回空数组。
输出严格 JSON：{"memories":[{"kind":...,"content":"第三人称陈述事实","supersedes_kind":null|"identity"}]}。kind=identity 时 supersedes_kind 必须填 'identity'，禁止填 null。本轮无可记则 {"memories":[]}。只输出 JSON 对象本身，不要代码块、不要解释。
```

注：源码字符串拼接中 identity 行行首有两个空格缩进（`"  会取代旧身份事实"`、`"  各自讲什么"` 等），移植时按上面逐字保留。

**User 输入（精确）**【修正 7】：`turn_text = f"用户：{question}\n助理：{answer}"`（中文全角冒号、单 `\n`，无 `[user]/[assistant]` 标签）。

---

### Prompt 2：`_MEM_L0_SYSTEM`（S2 文件/目录 L0 摘要）

```
你是记忆摘要器。把给定文本压成一句可独立检索的中文摘要：聚焦关于本用户的稳定事实 / 偏好 / 身份，不超过约 100 token，不要前缀、不要解释、不要分点编号，直接输出摘要正文本身。
【关键约束 — 严禁虚构】只能复述【输入文本中已经明确出现】的事实。禁止补充、推测、扩写、关联任何输入未提及的信息（如年龄/籍贯/职业/家庭/学历/兴趣等若输入未写就一律不准出现）。如输入只是「张三」一个名字，输出也只能围绕「张三」这个名字本身，绝不许编出生年份、城市、爱好等。事实越少摘要越短是正常的。
```

**User 输入**：文件 L0 → `{slug}.md` 的 body 段（去 frontmatter）；目录 L0 → `joined`（见下）。

---

### Prompt 3：`_MEM_L1_SYSTEM`（S2 目录 L1 导航图）

```
你是记忆导航图生成器。给你某目录下若干子项的摘要（以「## 子项名」分节）。聚成一张导航图：有哪些记忆条目、各自讲什么、需要时如何进一步查看其正文。中文，不超过约 1500 字，结构清晰可作为该子树索引；直接输出导航图正文，不要前缀、不要额外解释。
【关键约束 — 严禁虚构】只能复述【输入子项摘要中已经明确出现】的事实。禁止补充、推测、扩写、关联任何输入未提及的信息（如生平、教育、职业、家庭、社交、未来规划等若输入未写就一律不准出现）。子项内容稀少时，导航图也应相应简短；事实越少导航图越短是正常的、可接受的。
```

**User 输入**：与目录 L0 共用同一 `joined`。

---

### Prompt 4：`_SESSION_COMPACT_SYSTEM`（S5 会话压缩）

```
你是对话记忆压缩器。基于【已有会话摘要】（若有）与【新增对话】，输出一段更新后的会话摘要，忠实保留对后续有用的关键信息：用户陈述的事实与偏好及其先后/演变时间线、已确认的结论、当前状态、未决问题。不得丢弃【已有会话摘要】中的既有事实——把新信息融合进去，有变化则标注演变。不超过 300 字，中文，直接输出摘要正文，不要前缀、不要解释、不要分点编号。
```

**User 输入（§21 递归累积，精确）**：
```
【已有会话摘要】
{prev_summary}            ← prev_summary 为空时整段省略（仅当 prev_summary 真值才 append）

【新增对话】
[user] {content[:200]}
[assistant] {content[:200]}
...
```
- 用 `"\n\n".join(parts)` 拼装；`【新增对话】` 段仅当 `new_msgs` 非空才加；行格式 `f"[{m.role}] {(m.content or '')[:200]}"` 用单 `\n` join。

---

### Prompt 注入点：`with_memory_block` + `_MEMORY_BLOCK_TEMPLATE`

```python
def with_memory_block(system: str, memory_block: str | None) -> str:
    if not memory_block or not memory_block.strip():
        return system
    return _MEMORY_BLOCK_TEMPLATE.format(block=memory_block.strip()) + system
```

**模板（逐字，注意尾部两个 `\n`）**：
```
═══════ 记忆（关于本用户 / 本次会话的已知事实，优先参考）═══════
{block}
═══════════════════════════════════════════════════════════════

```
即 `"═══════ 记忆（关于本用户 / 本次会话的已知事实，优先参考）═══════\n{block}\n═══════════════════════════════════════════════════════════════\n\n"`，拼在原 system prompt 之前。

---

## 四、公开函数签名

### `MemoryExtractor`（extract.py）
```python
class MemoryExtractor:
    def __init__(self, llm: Any) -> None
    async def extract_and_persist(self, fs, memgen, recaller, *, user_id: int, turn_text: str) -> None
    async def _write_one_memory(self, fs, user_id, memory: dict, changed_uris: list[str]) -> None
    async def _supersede_identity(self, fs, user_id, new_slug: str) -> list[str]

def _compute_slug(content: str) -> str          # sha256 hex[:12]
def _parse_react_json(raw: str) -> list[dict]
def _now_iso_z() -> str                          # "%Y-%m-%dT%H:%M:%SZ"（UTC, timezone-aware）

_VALID_KINDS = ("preference", "identity", "style_feedback")
_SLUG_HEX_LEN = 12
_SOURCE_REACT = "react"
_ARCHIVE_DIRNAME = "archive"
_CODE_FENCE_RE = re.compile(r"^`{1,4}(?:json)?\s*(.*?)\s*`{1,4}$", re.DOTALL)
```

### `MemoryGen`（memgen.py）
```python
class MemoryGen:
    def __init__(self, llm) -> None
    async def regenerate(self, fs, changed_uris: list[str]) -> None
    @staticmethod
    def _is_memory_file(uri: str) -> bool
    @staticmethod
    def _ancestor_dirs(file_uris: list[str]) -> list[str]    # sorted key=(-uri.count("/"), uri)
    async def _gen_file_l0(self, fs, file_uri: str) -> None
    async def _gen_dir_l0_l1(self, fs, dir_uri: str) -> None
    @staticmethod
    async def _stale(fs, uri: str, want_hash: str) -> bool

def _sha256_hex(text: str) -> str                # 完整 64-hex（区别于 slug 的 [:12]）
def _split_frontmatter(text: str) -> tuple[dict, str]   # CRLF/CR→LF；YAMLError 吞为 {}
def _render_frontmatter(meta: dict, body: str) -> str   # sort_keys=False, allow_unicode=True
_ABSTRACT_SUFFIX = ".abstract.md"
_OVERVIEW_NAME = ".overview.md"
_MD_SUFFIX = ".md"
```

### `MemoryRecaller`（recall.py）
```python
class MemoryRecaller:
    def __init__(self, embedder: Any, weaviate_client: Any) -> None
    async def index_changed(self, fs, changed_uris: list[str]) -> None
    async def _index_one(self, fs, uri: str) -> None
    async def recall_memory_block(self, fs, query: str, user_id: int, *, top_k: int = 5) -> str

class MemoryL0Store(BaseWeaviateStore):  # collection "memory_l0", dimension=1024, MT on
class _DefaultEmbedder:
    async def embed(self, text: str) -> list[float]   # asyncio.to_thread(get_embedding, text, 1024)

def _kind_of_uri(uri: str) -> str               # 末段恰 "/.abstract.md" → "dir" 否则 "file"
def _overview_uri_for_dir_l0(dir_l0_uri: str) -> str
_VECTOR_DIM = 1024
_COLLECTION_NAME = "memory_l0"
```

### `service.py`（集成层）【修正 4】
```python
async def recall_memory_block(fs, query: str, user_id: int, *, top_k: int = 5) -> str
    # qa_router 实际调用的入口；内部构造 MemoryL0Store + _DefaultEmbedder + MemoryRecaller 再委托。
    # 整体 try/except → 返 ""（含构造期 ConnectionError）。
def _extract_focus_entity_ids(messages: Any) -> list[str]
_FOCUS_MAX = 10
```

### `SessionCompactor`（session.py）
```python
class SessionCompactor:
    def __init__(self, llm) -> None
    async def compact(self, fs, *, user_id: int, session_id: str,
                      every_n_messages: int = 6, force: bool = False) -> None

def _summary_uri(user_id, session_id) -> str    # ke://u/{uid}/session/{sid}/summary.md
async def read_session_summary(fs, *, user_id, session_id) -> str
async def write_message_to_fs(...) ; async def read_messages_for_session(...) -> list[_FsMessage]
async def write_feedback_to_fs(...) ; async def read_feedback_for_message(...) -> _FsFeedback | None
```

### `MemoryFS`（vfs.py）
```python
class MemoryFS:
    def __init__(self, root: str | None = None) -> None
    @staticmethod
    def _parse_uri(uri: str) -> tuple[str, list[str]]    # → (uid, segs)
    def resolve(self, uri: str) -> str
    async def write(self, uri, content) -> None          # 原子：mkstemp + os.replace
    async def read(self, uri) -> str                     # MemoryNotFound 若不存在
    async def exists(self, uri) -> bool
    async def ls(self, uri) -> list[str]                 # sorted
    async def rm(self, uri, *, recursive: bool = False) -> None
    async def mv(self, src_uri, dst_uri) -> None
class MemoryPathError(Exception): ...
class MemoryNotFound(Exception): ...
```

---

## 五、S4 抽取触发条件与护栏

### 触发条件
`qa_router._make_memory_writer` 返回的 `_writer(answer)` 在 **post-turn** 调用。每轮均调用 `extract_and_persist`；LLM 输出空 `{"memories":[]}` → 整函数零 fs/S2/S3 开销 return。`turn_text = f"用户：{question}\n助理：{answer}"`。

### JSON 输出 Schema
```json
{"memories":[{"kind":"preference"|"identity"|"style_feedback","content":"第三人称陈述事实","supersedes_kind":null|"identity"}]}
```

### 护栏与降级

| 护栏 | 位置 | 行为 |
|---|---|---|
| 代码栅栏剥除 | `_parse_react_json` | `_CODE_FENCE_RE` 剥 ` ```json … ``` ` |
| 空响应 | `_parse_react_json` | `raw` 空/全空白 → `ValueError("empty LLM response")` |
| 非法 JSON | `_parse_react_json` | `JSONDecodeError` → 转 `ValueError` |
| 顶层非 dict / 无 memories list | `_parse_react_json` | `ValueError` |
| 非 dict entry | `_parse_react_json` | debug skip |
| kind 白名单 | `_parse_react_json` | 不在 `_VALID_KINDS` → skip |
| 空 content | `_parse_react_json` | content 非 str 或 strip 后空 → skip |
| supersedes_kind 归一 | `_parse_react_json` | 非 `"identity"`/`None` → `None` |
| identity 强制 supersede | `_parse_react_json` 代码层兜底 | `kind=="identity"` → 强制 `sk="identity"`（DashScope qwen 对此约束不鲁棒）|
| 单条 memory 写入隔离 | `extract_and_persist` | 每条独立 try/except，debug + skip |
| S2/S3 批量触发 | `extract_and_persist` | 仅 `changed_uris` 非空才调一次 `memgen.regenerate` + 一次 `recaller.index_changed`（非每条）|
| abstract_uris 推导 | `extract_and_persist` | `{slug}.md`→`{slug}.abstract.md`，加上各 `_ancestor_dirs(changed_uris)` 的 `{dir}/.abstract.md` |

---

## 六、identity 单例取代（Supersede）算法

**触发**：`memory.get("supersedes_kind") == "identity"`（代码层在解析期已对 `kind=="identity"` 强制置 `sk`）。

**`_supersede_identity` 步骤**：
1. `fs.ls(ke://u/{uid}/global/identity)`；抛 `MemoryNotFound`/`MemoryPathError` → 返 `[]`（首次写正常路径）。
2. 跳过：`.abstract.md`、`.overview.md`、任何 `*.abstract.md`、非 `.md` 后缀（视为子目录如 `archive/`）。
3. 对每个 `{slug}.md`（`slug != new_slug`）：
   - `fs.mv(base/{slug}.md → base/archive/{slug}.md)`，单条 try/except 隔离，失败 debug + continue。
   - 同步归档 sibling `{slug}.abstract.md → archive/`（防 orphan：`MemoryNotFound`/`MemoryPathError` → continue 不算错；其它异常 debug + continue）。
   - 成功的 src + dst 加入 `changed`。
4. 返回所有 mv 的 uri 列表（供 S2/S3 对账）。

**不变量**：`recall.index_changed` 中 `"/archive/" in uri` → skip；`memgen._gen_dir_l0_l1` 中 `name == "archive"` → skip。双重保证归档身份永不进 L0/向量召回（修复「王山河→李龙飞」leak）。

---

## 七、S2 L0/L1 自底向上生成算法

`regenerate(fs, changed_uris)`：
1. 去重（set）+ 过滤 `_is_memory_file`（`ke://u/` 前缀 + `.md` 结尾 + 非 `.abstract.md`/`.overview.md` + slug 非空即非 `/.md` 末段）。
2. 逐文件 `_gen_file_l0`：读 body → `src_hash=sha256(body)`；读旧 `{slug}.abstract.md` frontmatter，`str(src_hash)` 命中 → skip（零 LLM）；否则调 `_MEM_L0_SYSTEM`，空响应抛 `ValueError`（不固化坏态），写 `{slug}.abstract.md`（frontmatter `{src_hash}`）。
3. `_ancestor_dirs(files)` 按 `(-uri.count("/"), uri)` 排序（深→浅，含租户根 `ke://u/{uid}`，不上溯到 `ke://u`）。
4. 逐目录 `_gen_dir_l0_l1`：`fs.ls` 取子项（已 sorted），跳过 `.abstract.md`/`.overview.md`/`archive`/`*.abstract.md`/空 slug；读子 L0 body；按 key 排序后 **`joined = "\n\n".join(f"## {k}\n{v}")`【修正 8：子项间双换行】**；`inputs_hash=sha256(joined)`；`_stale` 分别判 `.abstract.md`/`.overview.md` 的 `inputs_hash`，按需各调一次 LLM（L0 用 `_MEM_L0_SYSTEM`，L1 用 `_MEM_L1_SYSTEM`），空响应抛 `ValueError`。

幂等：`src_hash`/`inputs_hash` 入 frontmatter，哈希命中零 LLM；崩溃下一轮按哈希只补不一致项自愈；空响应不写防粘滞坏态。单条目失败 debug + 不连累整批。

---

## 八、S5 会话压缩算法（§21 递归累积）

`SessionCompactor.compact` 步骤（整体 try/except → debug 静默）：
1. `read_messages_for_session` → 全部消息，`msg_count = len`。
2. **floor**：`floor = 2 if force else every_n_messages`；`msg_count < floor` → return。【修正 2：6 是形参默认，非硬编码常量】
3. 读旧 `summary.md` frontmatter 取 `prev_summary`（body）+ `prev_turn_count`（`turn_count`，缺省 0）。
4. **delta**：`min_delta = 1 if force else every_n_messages`；`msg_count - prev_turn_count < min_delta` → return。
5. **拼 convo**：`new_msgs = messages[prev_turn_count:]`；`prev_summary` 真值才 append `【已有会话摘要】\n{prev_summary}`；`new_msgs` 非空才 append `【新增对话】\n` + 每条 `f"[{m.role}] {(m.content or '')[:200]}"`；`"\n\n".join(parts)`。
6. LLM `_SESSION_COMPACT_SYSTEM`；空响应 → return。
7. **focus**：`_extract_focus_entity_ids(messages[-12:])` —— 遍历 `("cited_entities","entry_points")` 顺序、首见序去重、截 `_FOCUS_MAX=10`，全程防御非 dict/非 list/非 str。
8. 写 `summary.md`：frontmatter `{turn_count: msg_count, focus_entity_ids: focus, updated_at: _now_iso_z()}` + body `summary + "\n"`。

---

## 九、文件 .md 格式（frontmatter）

`_render_frontmatter(meta, body)` → `f"---\n{yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)}---\n{body}"`。读侧 `_split_frontmatter` 先 CRLF/CR→LF 归一，YAML 损坏吞为 `{}`（自愈优先）。

| 文件 | frontmatter |
|---|---|
| `{slug}.md`（S4） | `kind`, `slug`, `source: "react"`, `created_at: <ISO Z>`（dict 保序 kind 在前）|
| `{slug}.abstract.md`（S2 文件 L0） | `src_hash: <64-hex>` |
| `.abstract.md`（S2 目录 L0） | `inputs_hash: <64-hex>` |
| `.overview.md`（S2 目录 L1） | `inputs_hash: <64-hex>` |
| `summary.md`（S5） | `turn_count: int`, `focus_entity_ids: [str...]`, `updated_at: <ISO Z>` |
| `{msg_id}.md`（S6） | `role`, `created_at`, `sections?`, `msg_metadata?` |
| `{msg_id}.feedback.md`（S7） | `vote`, `user_id`, `created_at` |

哈希用 `str()` 包裹比较（防 YAML 把全数字 hash 解析成 int）。

---

## 十、Weaviate collection schema

`memory_l0`，multi-tenancy on（tenant=`str(user_id)`），dim 1024。

| property | type | 用途 |
|---|---|---|
| `uri` | TEXT | ke:// 完整路径，逻辑主键 |
| `kind` | TEXT | `"file"`（`{slug}.abstract.md`）/ `"dir"`（末段 `/.abstract.md`），由 `_kind_of_uri` 判 |
| `hash` | TEXT | frontmatter `src_hash` 或 `inputs_hash`，幂等判定 |
| `body` | TEXT | `.abstract.md` 正文（含末尾 `\n`）|

对象 UUID：`uuid.UUID(BaseWeaviateStore._to_uuid(uri))`（SHA-256[:32] 重排为 UUID 字符串，**非** RFC 4122 UUID5，仅取确定性派生）。`index_changed`：`"/archive/" in uri` → skip；非 `.abstract.md` → skip；`fs.read` 抛 `MemoryNotFound` → `delete_by_id`；否则比 `str(hash)` 命中跳过，不命中 embed + insert/replace。

**召回输出格式**【修正 9】：`recall_memory_block` 返回 `"\n\n".join(f"- {p}" for p in parts)`；file hit `p=body`；dir hit `p = body + "\n---\n" + ovr_body`（fs.read 同目录 `.overview.md`，缺失 fallback 仅 body）。

---

## 十一、降级策略总表

| 层 | 失败场景 | 降级行为 |
|---|---|---|
| S4 LLM 调用 | `llm.complete` 抛 | 冒泡 → `_writer` try/except 吞（`_log.debug` exc_info），主答不受影响 |
| S4 JSON 解析 | 空/非法/无 memories | `ValueError` → 同上 |
| S4 单条 memory 写入 | `fs.write` 异常 | debug + skip，其他条目继续 |
| S4 identity .md mv | `fs.mv` 异常 | debug + continue，不中断归档循环 |
| S4 identity abstract mv | `MemoryNotFound`/`MemoryPathError` | continue（非错误）；其它异常 debug + continue |
| S4 identity dir 不存在 | `MemoryNotFound`/`MemoryPathError` | `_supersede_identity` 返 `[]`，首次写正常 |
| S2 文件 L0 / 目录 L0/L1 | LLM 空响应 | 抛 `ValueError` → `regenerate` 单条 debug + skip，下轮哈希不命中自愈 |
| S2 子 L0 缺失 | `MemoryNotFound` | 略过该子项（下轮其 L0 就绪后 inputs_hash 变自动重生）|
| S3 recall embed | `embedder.embed` 抛 | 返 `""` → `with_memory_block` 跳过注入 |
| S3 Weaviate query | 异常 | 返 `""` |
| **S3 fetch tenant 不存在【修正 6】** | `fetch_object_by_id` 抛 `WeaviateQueryError("tenant not found")` | try/except 吞 → `existing=None` → 走 `insert` 自动建 tenant（避免新用户首写卡死）|
| S3 index 单条 | upsert/delete 抛 | debug + continue |
| S3 frontmatter 缺 hash | `src_hash`/`inputs_hash` 皆 None | debug + skip（下轮自愈）|
| S5 压缩 | 任何异常 | 外层 `except Exception` 吞 `_log.debug(exc_info=True)` |
| service.recall wrapper | Weaviate 不可达 / `MemoryL0Store` 构造 `ConnectionError` / env 解析失败 | 整体 try/except 返 `""`（`_log.debug`）|

---

源码位置（移植参考，均绝对路径）：
- `/Users/java/knowledge-engineering/src/service/memory/extract.py`（331 行）
- `/Users/java/knowledge-engineering/src/service/memory/memgen.py`（314 行）
- `/Users/java/knowledge-engineering/src/service/memory/recall.py`（313 行）
- `/Users/java/knowledge-engineering/src/service/memory/session.py`（460 行）
- `/Users/java/knowledge-engineering/src/service/memory/service.py`（93 行）
- `/Users/java/knowledge-engineering/src/service/memory/vfs.py`（207 行）
- `/Users/java/knowledge-engineering/src/service/qa_engine/prompts.py:582-659`（5 个 prompt 常量 + `with_memory_block`）
- `/Users/java/knowledge-engineering/src/service/qa_router.py:730-814`（`_make_memory_writer` 接线点）+ `:431-436`（recall 调用，query=`body.question`, top_k=5）