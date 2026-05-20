# 文件式记忆重构 — S4：两阶段提交 + ReAct 抽取 + 替换 §22 显式路径 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 §22 既有的"显式 trigger + 关键字 detect + 轻量 LLM 意图解析 + DB identity-supersede"用户级写入链路，用单次 ReAct LLM 抽取替换：在 post-turn 闭包里 fire-and-forget 跑 `MemoryExtractor.extract_and_persist(...)` → 写文件 .md → 触发 S2.regenerate + S3.index_changed；identity 类通过 `archive/` 子目录归档老 .md 实现 supersede。

**Architecture:** 新模块 `src/service/memory/extract.py` 的 `MemoryExtractor`（纯逻辑，构造注入 LLM；fs/memgen/recaller 每次调用传入）。`OnMemoryCallback` 由 `Callable[[], Awaitable[None]]` 改为 `Callable[[str], Awaitable[None]]` — sse_emitter 在 done 后把 `answer_text = "\n\n".join(s.get("content","") for s in answer.sections)` 透传给闭包；`_make_memory_writer` 闭包替换 §22 detect/parse/write_explicit 链为 ReAct 抽取链。**S2/S3 不知 S4**，S4 是首个真正联动 S2+S3 的调用方。

**Tech Stack:** Python 3.10+；stdlib（`hashlib`/`json`/`logging`/`datetime`）+ 项目已声明依赖（pyyaml / weaviate-client）；S1 `MemoryFS`、S2 `MemoryGen` + `_split_frontmatter`/`_render_frontmatter`/`_sha256_hex`/`_ABSTRACT_SUFFIX`/`_OVERVIEW_NAME`/`_MD_SUFFIX`、S3 `MemoryRecaller`；KE LLM provider 鸭子 `async complete(system, user, **kw) -> str`。测试 pytest + `pytest-asyncio`，`./venv/bin/python -m pytest`（homebrew python3 无 pytest-asyncio）；跨 S3/S4 fake stack 复用（import `_FakeWeaviateClient`/`_FakeEmbedder` from `test_memory_recall.py`）。

**单一来源设计：** Obsidian `/Users/java/obsidian/01 Engineering/knowledge-engineering/文件式记忆重构-设计.md` §5（§5.0–§5.10）。本计划不得引入 §5 之外的设计决策。

**仓库 / 分支：** `/Users/java/knowledge-engineering-auth` @ `release-0513`（S1+S2+S3 已落盘 HEAD `2ae607d`；本计划逐任务 commit 已授权；push/merge/部署须用户拍板，**不在本计划范围**）。

**测试命令前缀（务必用仓库 venv）：** `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest`

---

## File Structure

| 文件 | 职责 | 创建/修改 |
|---|---|---|
| `src/service/memory/extract.py` | `MemoryExtractor` 引擎 + helpers（`_compute_slug` / `_parse_react_json` / `_now_iso_z`）。 | **Create** |
| `src/service/qa_engine/prompts.py` | 末尾追加 `_MEM_EXTRACT_SYSTEM` 常量；删除 line 380 起的 `_USER_MEM_INTENT_SYSTEM` 整常量。 | Modify |
| `src/service/qa_engine/sse_emitter.py` | `OnMemoryCallback` 类型：`Callable[[], ...]` → `Callable[[str], ...]`；invocation `await on_memory()` → `await on_memory(answer_text)`。 | Modify |
| `src/service/memory/service.py` | 删除 §22 全部显式 trigger / parse / write_explicit / project 同件函数（5 函数 + 7 常量）；import 行去 `_USER_MEM_INTENT_SYSTEM`。 | Modify |
| `src/service/qa_router.py` | 删 import 中 §22 函数名；按 §5.5 整段替换 `_make_memory_writer` 闭包；新签名 `_writer(answer: str)`。 | Modify |
| `tests/test_auth/test_memory_extract.py` | S4 全部测试（§5.9 九场景 + 4 个 helper 单元测试）。复用 S3 `_FakeWeaviateClient`/`_FakeEmbedder`。 | **Create** |
| `tests/test_auth/test_memory_service.py` | 删除 31 个 §22 测试函数（test_detect_* / test_parse_* / test_write_explicit_*）+ 顶部 import 行对应名称。保留 26 个 S3/S5/synthesizer 测试。 | Modify (delete obsolete) |
| `tests/test_auth/test_memory_router_hook.py` | 整文件删除（14 个 `_make_memory_writer` 测试全部针对 §22 闭包行为，被 S4 替换后失去意义；新覆盖在 `test_memory_extract.py`）。 | **Delete** |

**边界（§5.8 YAGNI，不做）：** S5 会话/消息归档迁文件、S6 DB→文件迁移、S7 多租户加固、`MemoryL0Store` 单例（S7 deferred）、`archive/` 召回忽略开关、preference/style_feedback 后台 dedup、iterative ReAct with tools、显式 trigger fast path、工程级（project_id）记忆（D4 削减）。

**关键既有接口（已核对真实代码，照此调用，勿臆造）：**
- `from src.service.memory.vfs import MemoryFS, MemoryNotFound, MemoryPathError`：`async write/read/exists/ls/rm/mv`；同步 `resolve`；`_parse_uri` 静态解析 `ke://u/{uid}/...`。
- `from src.service.memory.memgen import MemoryGen, _split_frontmatter, _render_frontmatter, _sha256_hex, _ABSTRACT_SUFFIX, _OVERVIEW_NAME, _MD_SUFFIX`：S2 已有；frontmatter CRLF 归一、YAMLError 自愈、`_sha256_hex(text)` 返 64-char hex。
- `from src.service.memory.recall import MemoryRecaller, MemoryL0Store, _DefaultEmbedder`：S3 已有；`async index_changed(fs, list[str])` / `async recall_memory_block(fs, query, user_id, *, top_k=5) -> str`。
- `from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM`（**保留**，S5/§21 还用）；`_USER_MEM_INTENT_SYSTEM` 删除。
- 测试 fake LLM 形态：`class _Fake...: async def complete(self, *, system: str, user: str, **kw) -> str:`（与 S2/S3 同 keyword-only `*`）。
- 测试 fake Weaviate / Embedder：直接 import `from tests.test_auth.test_memory_recall import _FakeEmbedder, _FakeWeaviateClient`（跨测试文件 import 在 pytest 是合规的；KE 已有相似 pattern）。

---

### Task 1: `extract.py` 骨架 + helpers + `_MEM_EXTRACT_SYSTEM` prompt

**Files:**
- Create: `src/service/memory/extract.py`
- Modify: `src/service/qa_engine/prompts.py`（line 405 后追加 `_MEM_EXTRACT_SYSTEM`，与 `_MEM_L1_SYSTEM` 同处就近；line 380 `_USER_MEM_INTENT_SYSTEM` 删除留到 T4）
- Create: `tests/test_auth/test_memory_extract.py`

- [ ] **Step 1: 写失败测试（helpers：slug 确定性 + JSON 解析容错 + ISO 时间格式）**

创建 `tests/test_auth/test_memory_extract.py`，内容：

```python
"""文件式记忆 S4：ReAct 抽取测试。设计：[[文件式记忆重构-设计]] §5。

fake LLM JSON + 真 MemoryFS(root=tmp_path) + 真 MemoryGen(fake LLM) +
S3 既有 _FakeWeaviateClient/_FakeEmbedder + 真 MemoryRecaller。沿用
tests/test_auth 既有 fake / tmp_path / @pytest.mark.asyncio 风格。
"""
# 导入 pytest（项目测试框架，pytest-asyncio 在 venv 中已安装）
import pytest

# 从 S1 vfs 导入：真 MemoryFS（tmp_path 注入做隔离）
from src.service.memory.vfs import MemoryFS
# 从 S2 memgen 导入：frontmatter 工具与哈希函数（S4 测试用来构造/检查 .md）
from src.service.memory.memgen import (
    _split_frontmatter,           # 拆 frontmatter / body
    _render_frontmatter,           # 序列化 frontmatter
    _sha256_hex,                   # 字符串 → SHA-256 hex
    _ABSTRACT_SUFFIX,              # ".abstract.md"
    _OVERVIEW_NAME,                # ".overview.md"
)
# 从被测模块导入（本 Task 实现）
from src.service.memory.extract import (
    MemoryExtractor,               # S4 主引擎
    _compute_slug,                 # helper：content → 12-char sha256 hex prefix
    _parse_react_json,             # helper：LLM 输出 → memories list（容错）
    _now_iso_z,                    # helper：当前时间 ISO 8601 Z 字符串
)


def _fs(tmp_path):
    """tests 通用 fixture：用 tmp_path 给 MemoryFS 提供隔离根目录。"""
    # MemoryFS 接受 str；pytest tmp_path 是 pathlib.Path，str() 即可
    return MemoryFS(root=str(tmp_path))


# ── Task 1：纯函数 helpers ───────────────────────────────────────
def test_compute_slug_deterministic_and_length():
    """slug = sha256(content)[:12]：同 content 同 slug；len=12；hex 字符。"""
    # 同输入恒同输出（幂等性根基：同 content 同 slug → 同 path → 不重写）
    assert _compute_slug("用户的名字是李龙飞") == _compute_slug("用户的名字是李龙飞")
    # 长度恰 12（取 sha256 hex 前缀；64 char 截断 12）
    assert len(_compute_slug("anything")) == 12
    # 只含 hex 字符
    s = _compute_slug("test content 中文")
    assert all(c in "0123456789abcdef" for c in s)
    # 不同输入 → 不同 slug（hash 碰撞概率忽略）
    assert _compute_slug("a") != _compute_slug("b")


def test_parse_react_json_valid_input():
    """合法 JSON → 返回 memories 列表（含 kind/content/supersedes_kind）。"""
    # 典型 LLM 输出：单 identity
    raw = '{"memories":[{"kind":"identity","content":"用户的名字是李龙飞","supersedes_kind":"identity"}]}'
    out = _parse_react_json(raw)
    # 返回应为列表，单元素
    assert isinstance(out, list)
    assert len(out) == 1
    m = out[0]
    assert m["kind"] == "identity"
    assert m["content"] == "用户的名字是李龙飞"
    assert m["supersedes_kind"] == "identity"


def test_parse_react_json_empty_memories():
    """空 memories 数组（绝大多数闲聊轮）→ 返回空 list。"""
    raw = '{"memories":[]}'
    assert _parse_react_json(raw) == []


def test_parse_react_json_strips_code_fence():
    """LLM 有时会包 ```json ... ``` —— 解析器要剥掉再 json.loads。"""
    raw = '```json\n{"memories":[{"kind":"preference","content":"偏好中文","supersedes_kind":null}]}\n```'
    out = _parse_react_json(raw)
    assert len(out) == 1
    assert out[0]["kind"] == "preference"
    assert out[0]["supersedes_kind"] is None


def test_parse_react_json_invalid_raises():
    """非法 JSON → 抛 ValueError（不静默；§5.7 引擎自身清晰抛错）。"""
    # 期望 ValueError；空响应 / 非 JSON / dict 但无 memories 都该被拒
    with pytest.raises(ValueError):
        _parse_react_json("not json at all")
    with pytest.raises(ValueError):
        _parse_react_json("")
    # dict 但无 memories 字段
    with pytest.raises(ValueError):
        _parse_react_json('{"other":"field"}')


def test_parse_react_json_filters_bad_entries():
    """单条 entry 缺字段 / kind 非法 → 跳过该条，其他条继续。"""
    # 第 1 条合法，第 2 条 kind 非法（_VALID_KINDS 之外），第 3 条无 content
    raw = (
        '{"memories":['
        '{"kind":"identity","content":"用户的名字是李龙飞","supersedes_kind":"identity"},'
        '{"kind":"junk","content":"无效分类","supersedes_kind":null},'
        '{"kind":"preference","supersedes_kind":null}'   # 无 content
        ']}'
    )
    out = _parse_react_json(raw)
    # 仅保留第 1 条合法
    assert len(out) == 1
    assert out[0]["kind"] == "identity"


def test_now_iso_z_format():
    """ISO 8601 Z 格式：YYYY-MM-DDTHH:MM:SSZ（与 §22 既有时间戳格式一致）。"""
    import re
    s = _now_iso_z()
    # 形如 2026-05-21T08:00:00Z（精确到秒，UTC Z 后缀）
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", s)


def test_memory_extractor_construction_accepts_llm():
    """MemoryExtractor 构造注入 LLM（鸭子接口），便于单测 fake。"""
    # 用 None 占位（本步不调任何方法、只验证签名）
    extractor = MemoryExtractor(llm=None)
    # __init__ 仅保存引用，不应抛错
    assert extractor._llm is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_extract.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.service.memory.extract'`（collection error）。

- [ ] **Step 3: 创建 `src/service/memory/extract.py` 骨架**

```python
"""文件式记忆 S4：ReAct 抽取引擎（MemoryExtractor）。

设计：[[文件式记忆重构-设计]] §5。纯逻辑，不依赖 FastAPI；与 vfs.py /
memgen.py / recall.py / service.py 并列。
- 单次 LLM 调用从一轮对话抽取所有可记忆事实（preference/identity/style_feedback）
- 单 JSON 数组输出含 kind / content / supersedes_kind
- identity 类通过 archive/ 子目录归档老 .md 实现 supersede
- 每条 memory 写入独立 try/except 隔离；引擎自身清晰抛错（§5.7 失败由调用方闭包包 try）
- 一轮所有 memory 写完后一次性串接 S2.regenerate + S3.index_changed

仅 S4：不含 S5 会话级文件化、S6 DB→文件迁移、S7 多租户加固、archive/ 召回忽略开关。
"""
# from __future__ import annotations 让 type hints 字符串化，避免运行期类型评估开销
from __future__ import annotations

# stdlib：日志（_log 模块级单例，沿用 KE 既有模式）
import datetime as _dt
import hashlib
import json
import logging
import re
from typing import Any

# S1 存储层：MemoryFS（async API）+ 异常类
from src.service.memory.vfs import MemoryFS, MemoryNotFound, MemoryPathError
# S2 已有 frontmatter / 哈希工具（同包私名 import 合理）
from src.service.memory.memgen import (
    MemoryGen,
    _render_frontmatter,
    _ABSTRACT_SUFFIX,
    _OVERVIEW_NAME,
    _MD_SUFFIX,
)
# S3 召回引擎（写入后 index_changed 同步 Weaviate）
from src.service.memory.recall import MemoryRecaller
# S4 自己的 prompt 常量（在 Step 4 加进 prompts.py）
from src.service.qa_engine.prompts import _MEM_EXTRACT_SYSTEM

# 模块级 logger（与 vfs.py / memgen.py / recall.py 同模式）
_log = logging.getLogger(__name__)

# kind 白名单（§5.3 D2 锁定 taxonomy）
_VALID_KINDS = ("preference", "identity", "style_feedback")
# slug 长度（sha256 hex 前缀，§5.3：抗碰撞 + 确定性 + 同 content 同 slug 幂等）
_SLUG_HEX_LEN = 12
# source 字段值（§5.3：区分 S4 ReAct vs §22 历史 explicit，S6 迁移辨识）
_SOURCE_REACT = "react"
# archive/ 子目录名（§5.4 identity-supersede 归档目录）
_ARCHIVE_DIRNAME = "archive"

# LLM 输出有时包 ```json ... ``` —— 解析前剥掉
_CODE_FENCE_RE = re.compile(r"^`{1,4}(?:json)?\s*(.*?)\s*`{1,4}$", re.DOTALL)


def _compute_slug(content: str) -> str:
    """content → sha256 hex 前 12 字符。

    §5.3 设计：S4 内部生成、不让 LLM 出 slug。
    - 抗碰撞：12-hex = 48-bit 空间，碰撞概率忽略
    - 确定性：同 content 同 slug → 同 path → fs.write 覆盖
    - 幂等天然：同 content 不重写（src_hash 未变，S2 不重生 L0，S3 不重 embed）
    """
    # SHA-256 hex 取前 _SLUG_HEX_LEN（12）字符
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:_SLUG_HEX_LEN]


def _now_iso_z() -> str:
    """当前 UTC 时间，ISO 8601 Z 格式：YYYY-MM-DDTHH:MM:SSZ。

    与 §22 既有 frontmatter timestamps 格式一致；写入 .md 的 created_at 字段。
    """
    # datetime.utcnow() 已 deprecated（PEP 615 / 3.12 +）；用 timezone-aware 形式
    now = _dt.datetime.now(_dt.timezone.utc)
    # strftime 输出 YYYY-MM-DDTHH:MM:SS；末尾补 Z 表示 UTC
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_react_json(raw: str) -> list[dict]:
    """LLM ReAct 输出 → memories 列表（容错 + 过滤非法）。

    输入：LLM 返回的字符串（可能带 ```json 代码栅栏）
    输出：list[dict] 每 dict 含 kind / content / supersedes_kind 三字段
    非法 JSON / dict 无 memories 字段 → 抛 ValueError（§5.7 引擎自身清晰抛错）
    单条 entry 缺 content / kind 非法 → 跳过该条（容错容忍部分输出）
    """
    # 空字符串 / 全空白直接拒
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty LLM response")
    # 剥 ```json ... ``` 代码栅栏（LLM 偶尔不听 prompt 加栅栏）
    m = _CODE_FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    # json.loads 失败抛 JSONDecodeError，转 ValueError 让上层统一兜
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    # 顶层必须是 dict 且含 memories 列表字段
    if not isinstance(obj, dict):
        raise ValueError(f"expected dict at top, got {type(obj).__name__}")
    memories = obj.get("memories")
    if not isinstance(memories, list):
        raise ValueError(f"missing 'memories' list field, got {type(memories).__name__}")
    # 过滤非法 entry：缺 kind / kind 非白名单 / content 非字符串 / content 空
    out: list[dict] = []
    for item in memories:
        if not isinstance(item, dict):
            _log.debug("parse_react_json: skip non-dict entry %r", item)
            continue
        kind = item.get("kind")
        content = item.get("content")
        sk = item.get("supersedes_kind")
        if kind not in _VALID_KINDS:
            _log.debug("parse_react_json: skip bad kind %r", kind)
            continue
        if not isinstance(content, str) or not content.strip():
            _log.debug("parse_react_json: skip missing/empty content for kind %r", kind)
            continue
        # supersedes_kind 仅允许 "identity" 或 None（§5.2 schema）
        if sk not in ("identity", None):
            sk = None
        out.append({
            "kind": kind,
            "content": content.strip(),
            "supersedes_kind": sk,
        })
    return out


class MemoryExtractor:
    """S4 主引擎：从一轮对话 ReAct 抽取记忆 + 落地全链（§5.1）。

    llm：KE 既有 provider 鸭子接口 ``async complete(system,user,**kw)->str``，
    构造注入便于单测 fake。fs/memgen/recaller 每次调用传入（同一引擎可服务
    不同 MemoryFS 实例 / 测试）。

    引擎自身清晰抛错（LLM/JSON/fs.write/S2/S3 调用失败都 raise）；
    「记忆失败不影响主答」（§22 交接 #2）由调用方 _writer 闭包包 try/except。
    """

    def __init__(self, llm: Any) -> None:
        # 仅持有 llm；fs/memgen/recaller 走 extract_and_persist 形参（§5.1）
        self._llm = llm
```

- [ ] **Step 4: 在 `src/service/qa_engine/prompts.py` 文件末尾追加 `_MEM_EXTRACT_SYSTEM`**

读 `src/service/qa_engine/prompts.py` 末尾，确认现有最后一个常量是 `_MEM_L1_SYSTEM`（在 line 404 起）。在文件最末尾（`_MEM_L1_SYSTEM` 闭合 `)` 之后）追加：

```python


# ─── 文件式记忆 S4：ReAct 抽取（2026-05-21）──────────────────────────────
# 设计：[[文件式记忆重构-设计]] §5.2。单次 LLM 调用，输出 JSON 数组含 kind /
# content / supersedes_kind 三字段。空 {"memories":[]} = 本轮无可记。
_MEM_EXTRACT_SYSTEM = (
    "你是用户记忆抽取器。给你一段用户与助理的对话，抽取所有值得长期记住的"
    "关于本用户的事实，分类为 preference / identity / style_feedback：\n"
    "- identity：用户身份/姓名/自我称呼/角色（必含 supersedes_kind='identity'，"
    "  会取代旧身份事实，先更新不并存重复，避免「王山河→李龙飞」类 bug）；\n"
    "- preference：用户长期偏好（语言、风格、领域、工程范畴等）；\n"
    "- style_feedback：用户对回答风格/格式/长度的反馈；\n"
    '输出严格 JSON：{"memories":[{"kind":...,"content":"第三人称陈述事实",'
    '"supersedes_kind":null|"identity"}]}。本轮无可记则 {"memories":[]}。'
    "只输出 JSON 对象本身，不要代码块、不要解释。"
)
```

**注**：`_USER_MEM_INTENT_SYSTEM` 的删除留到 T4（与 §22 函数删除同步），本 Task 仅追加新常量。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_extract.py -q`
Expected: PASS（8 passed）。

- [ ] **Step 6: import 自检**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.memory.extract import MemoryExtractor, _compute_slug, _parse_react_json, _now_iso_z; from src.service.qa_engine.prompts import _MEM_EXTRACT_SYSTEM; print('ok')"`
Expected: 输出 `ok`。

- [ ] **Step 7: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/extract.py src/service/qa_engine/prompts.py tests/test_auth/test_memory_extract.py && git commit -m "$(cat <<'EOF'
feat(memory-s4): MemoryExtractor 骨架 + helpers + _MEM_EXTRACT_SYSTEM prompt

新增 src/service/memory/extract.py：MemoryExtractor.__init__（构造注入 llm）
+ helpers _compute_slug (sha256[:12])/_parse_react_json (容错+filter)/
_now_iso_z (ISO 8601 Z)。prompts.py 追加 _MEM_EXTRACT_SYSTEM 常量
（_USER_MEM_INTENT_SYSTEM 删除留 T4 同步）。设计：文件式记忆重构-设计 §5.1/§5.2/§5.3。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" && git status
```

---

### Task 2: `extract_and_persist` 非 supersede 路径 + S2/S3 chain + 失败隔离

**Files:**
- Modify: `src/service/memory/extract.py`（追加 `MemoryExtractor.extract_and_persist` + 私有 `_write_one_memory` + 测试 fake LLM 类）
- Modify: `tests/test_auth/test_memory_extract.py`（追加场景 ①②④⑤⑥）

实现 §5.9 场景 ①（空数组）、②（单 preference）、④（多条 memory 但全非 identity）、⑤（LLM JSON 解析失败抛错）、⑥（单条 fs.write 失败隔离）。**不**实现 identity-supersede（T3）。

- [ ] **Step 1: 追加 fake LLM 类 + 场景测试**

在 `tests/test_auth/test_memory_extract.py` 末尾追加（在 Task 1 测试之后）：

```python
# ── Task 2：extract_and_persist 非 supersede 路径 ──────────────────
# 跨 S3/S4 fake stack 复用：S3 既有 _FakeEmbedder/_FakeWeaviateClient
# 在 test_memory_recall.py 里完整实现；S4 测试直接 import 不重复。
from tests.test_auth.test_memory_recall import (
    _FakeEmbedder,
    _FakeWeaviateClient,
)
# S2 真 MemoryGen + S3 真 MemoryRecaller（接受 fake 协作者）
from src.service.memory.memgen import MemoryGen
from src.service.memory.recall import MemoryRecaller


class _FixedJSONLLM:
    """固定返回 JSON 字符串的 fake LLM。记录调用次数 + 最近 system/user 入参。"""
    def __init__(self, ret: str):
        # ret 应是 ReAct 抽取的 JSON 字符串（或带代码栅栏）
        self.ret = ret
        # 计数（验证 LLM 被调用次数；S2 MemoryGen 也用 LLM，需区分）
        self.calls = 0
        self.last_system = None
        self.last_user = None

    async def complete(self, *, system: str, user: str, **kw) -> str:
        # 关键字参（*）与 KE provider 鸭子接口一致
        self.calls += 1
        self.last_system = system
        self.last_user = user
        # 同一 fake LLM 也被 MemoryGen 用来生成 L0/L1 摘要；
        # 但 S4 仅看自己抽取的返回，MemoryGen 拿这个 JSON 当作 L0/L1 内容也无妨
        # （测试不强检查 L0/L1 文本质量；只验证写入 + 哈希链路）
        return self.ret


def _make_extract_stack(tmp_path, react_json: str):
    """构造一个完整 S2+S3+S4 测试栈：fake LLM + 真 MemoryFS + 真 MemoryGen + 真 MemoryRecaller。"""
    llm = _FixedJSONLLM(react_json)
    fs = _fs(tmp_path)
    memgen = MemoryGen(llm)
    embedder = _FakeEmbedder()
    wv = _FakeWeaviateClient()
    recaller = MemoryRecaller(embedder=embedder, weaviate_client=wv)
    extractor = MemoryExtractor(llm)
    return extractor, fs, memgen, recaller, llm, embedder, wv


@pytest.mark.asyncio
async def test_extract_empty_memories_zero_writes(tmp_path):
    """场景①：LLM 返回 {"memories":[]} → 零 fs.write、零 S2/S3 调用、直接 return。"""
    extractor, fs, memgen, recaller, llm, emb, wv = _make_extract_stack(
        tmp_path, '{"memories":[]}'
    )
    # 跑抽取
    await extractor.extract_and_persist(
        fs, memgen, recaller, user_id=7, turn_text="用户：你好\n助理：你好！"
    )
    # 验证：LLM 调了 1 次（ReAct 自身），但 S2 MemoryGen 没被调（无记忆要写）
    # → fake LLM 总 calls 应恰为 1
    assert llm.calls == 1
    # fs 里 user 7 的 global 目录不存在（无任何写入）
    assert not await fs.exists("ke://u/7/global")
    # tenant 7 在 Weaviate 中无对象
    coll = wv.collections.get("memory_l0")
    assert coll.tenants.get("7", {}) == {}
    # embedder 零调用
    assert emb.calls == 0


@pytest.mark.asyncio
async def test_extract_single_preference_writes_md_and_chains(tmp_path):
    """场景②：LLM 返回 1 条 preference → 1 个 .md 写入 + S2.regenerate + S3.index_changed 各调一次。"""
    react_json = (
        '{"memories":[{"kind":"preference",'
        '"content":"用户偏好中文回答","supersedes_kind":null}]}'
    )
    extractor, fs, memgen, recaller, llm, emb, wv = _make_extract_stack(
        tmp_path, react_json
    )
    await extractor.extract_and_persist(
        fs, memgen, recaller, user_id=7, turn_text="用户：我喜欢中文回答"
    )
    # 验证：记忆 .md 已写
    slug = _compute_slug("用户偏好中文回答")
    pref_uri = f"ke://u/7/global/preference/{slug}.md"
    assert await fs.exists(pref_uri)
    # frontmatter 字段
    raw = await fs.read(pref_uri)
    meta, body = _split_frontmatter(raw)
    assert meta["kind"] == "preference"
    assert meta["slug"] == slug
    assert meta["source"] == "react"
    # created_at 应为 ISO 8601 Z 形式（YYYY-MM-DDTHH:MM:SSZ）
    import re
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", str(meta["created_at"]))
    # body == content + "\n"
    assert body.strip() == "用户偏好中文回答"
    # S2 自底向上生成了 .abstract.md（preference 目录 + 父目录们）
    assert await fs.exists(f"ke://u/7/global/preference/{slug}.abstract.md")
    assert await fs.exists("ke://u/7/global/preference/.abstract.md")
    # S3 把 .abstract.md 灌入 Weaviate tenant 7
    coll = wv.collections.get("memory_l0")
    assert len(coll.tenants["7"]) >= 1  # 至少文件 L0；可能还有目录 L0


@pytest.mark.asyncio
async def test_extract_multi_memory_all_non_identity(tmp_path):
    """场景④（无 supersede 版）：preference + style_feedback 两条，全非 identity →
    各写一个 .md + 一次 batched S2/S3 调用（不是每条调一次）。
    """
    react_json = (
        '{"memories":['
        '{"kind":"preference","content":"用户偏好中文","supersedes_kind":null},'
        '{"kind":"style_feedback","content":"用户嫌代码格式啰嗦","supersedes_kind":null}'
        ']}'
    )
    extractor, fs, memgen, recaller, llm, emb, wv = _make_extract_stack(
        tmp_path, react_json
    )
    await extractor.extract_and_persist(
        fs, memgen, recaller, user_id=7,
        turn_text="用户：我喜欢中文，但你之前的代码太啰嗦",
    )
    # 两个 .md 都写入了
    pref_slug = _compute_slug("用户偏好中文")
    sf_slug = _compute_slug("用户嫌代码格式啰嗦")
    assert await fs.exists(f"ke://u/7/global/preference/{pref_slug}.md")
    assert await fs.exists(f"ke://u/7/global/style_feedback/{sf_slug}.md")


@pytest.mark.asyncio
async def test_extract_llm_json_parse_failure_raises(tmp_path):
    """场景⑤：LLM 返回非 JSON 字符串 → extract_and_persist 抛 ValueError（不静默）。
    §5.7 引擎自身清晰抛错；由调用方 _writer 闭包包 try/except 兜。
    """
    extractor, fs, memgen, recaller, llm, emb, wv = _make_extract_stack(
        tmp_path, "this is not json at all"
    )
    with pytest.raises(ValueError):
        await extractor.extract_and_persist(
            fs, memgen, recaller, user_id=7, turn_text="anything"
        )
    # fs 无任何写入
    assert not await fs.exists("ke://u/7/global")


@pytest.mark.asyncio
async def test_extract_single_write_failure_isolated(tmp_path, caplog):
    """场景⑥：单条 fs.write 失败 → 该条记 _log.debug 跳过、其他条继续。
    模拟方式：让 LLM 返回的某条 content 触发 fs 错（比如 mock fs.write 对某 uri 抛错）。
    """
    react_json = (
        '{"memories":['
        '{"kind":"preference","content":"良性内容 A","supersedes_kind":null},'
        '{"kind":"preference","content":"会触发 write fail 的内容","supersedes_kind":null},'
        '{"kind":"preference","content":"良性内容 C","supersedes_kind":null}'
        ']}'
    )
    extractor, fs, memgen, recaller, llm, emb, wv = _make_extract_stack(
        tmp_path, react_json
    )
    # Monkey-patch fs.write：对 "fail" 内容的 slug 路径抛 IOError，其他放行
    bad_slug = _compute_slug("会触发 write fail 的内容")
    real_write = fs.write

    async def _fail_on_bad(uri, content):
        if bad_slug in uri:
            raise IOError("simulated write failure")
        await real_write(uri, content)

    fs.write = _fail_on_bad

    import logging
    with caplog.at_level(logging.DEBUG, logger="src.service.memory.extract"):
        await extractor.extract_and_persist(
            fs, memgen, recaller, user_id=7, turn_text="anything"
        )

    # 良性内容 A 与 C 写入成功；bad 跳过（被隔离）
    a_slug = _compute_slug("良性内容 A")
    c_slug = _compute_slug("良性内容 C")
    assert await fs.exists(f"ke://u/7/global/preference/{a_slug}.md")
    assert await fs.exists(f"ke://u/7/global/preference/{c_slug}.md")
    assert not await fs.exists(f"ke://u/7/global/preference/{bad_slug}.md")
    # caplog 含 "write failed" 调试日志
    assert any("memory write failed" in r.message for r in caplog.records)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_extract.py -q -k "extract_"`
Expected: FAIL — `MemoryExtractor` 无 `extract_and_persist` 方法（AttributeError）。

- [ ] **Step 3: 在 `MemoryExtractor` 内实现 `extract_and_persist` 非 supersede 路径**

在 `src/service/memory/extract.py` 的 `class MemoryExtractor` 内（`__init__` 之后）追加：

```python
    # ── 公开唯一 API ─────────────────────────────────────────────
    async def extract_and_persist(
        self,
        fs: MemoryFS,
        memgen: MemoryGen,
        recaller: MemoryRecaller,
        *,
        user_id: int,
        turn_text: str,
    ) -> None:
        """ReAct 抽取本轮记忆 → 写 .md → 一次性串接 S2.regenerate + S3.index_changed。

        §5.7 失败语义：本方法自身清晰抛错（LLM/JSON 调用失败 raise）；
        单条 memory 写入失败独立 try/except 隔离（_log.debug 跳过该条）；
        「记忆失败不影响主答」由调用方 _writer 闭包包 try/except 兜。
        """
        # 1) 调 LLM 跑 ReAct 抽取（_MEM_EXTRACT_SYSTEM 在 prompts.py）
        raw = await self._llm.complete(
            system=_MEM_EXTRACT_SYSTEM,
            user=turn_text,
        )
        # 2) 解析 JSON（容错过滤；非法 JSON / dict 无 memories 抛 ValueError）
        memories = _parse_react_json(raw)
        # 3) 空列表（绝大多数闲聊轮）→ 零 fs.write、零 S2/S3 调用，直接 return
        if not memories:
            _log.debug("extract: empty memories for user %d (闲聊轮)", user_id)
            return
        # 4) 收集本轮所有写入的 uri（含 supersede 归档路径），最终一次性传给 S2/S3
        changed_uris: list[str] = []
        # 5) 对每条 memory 独立 try/except 写入（单条失败不连累其他；§5.7 隔离）
        for mem in memories:
            try:
                await self._write_one_memory(fs, user_id, mem, changed_uris)
            except Exception as exc:           # noqa: BLE001 单条目隔离
                _log.debug(
                    "extract: memory write failed kind=%s content=%r: %r",
                    mem.get("kind"), mem.get("content"), exc,
                )
        # 6) 若有任何 .md 被写入 / mv，调一次 S2.regenerate + 一次 S3.index_changed
        #    （非每条调一次：batched 大幅省 LLM 与 embedding 调用）
        if changed_uris:
            await memgen.regenerate(fs, changed_uris)
            await recaller.index_changed(fs, changed_uris)

    async def _write_one_memory(
        self,
        fs: MemoryFS,
        user_id: int,
        memory: dict,
        changed_uris: list[str],
    ) -> None:
        """写入单条 memory（非 supersede 路径）；supersede 由 Task 3 接管。

        路径：ke://u/{uid}/global/{kind}/{slug}.md
        frontmatter：{kind, slug, source: "react", created_at: <ISO Z>}
        body：content + "\\n"
        """
        kind = memory["kind"]
        content = memory["content"]
        # Task 3 会接管 supersedes_kind 路径；本 Task 仅处理 None
        if memory.get("supersedes_kind") == "identity":
            # T3 实现；本 Task 暂跳过（不写、不抛 — 避免误用）
            _log.debug(
                "extract: identity-supersede deferred to T3 (mem skipped this version)"
            )
            return
        # 路径
        slug = _compute_slug(content)
        uri = f"ke://u/{user_id}/global/{kind}/{slug}.md"
        # frontmatter（dict 保序：kind 在前，便于人读）
        meta = {
            "kind": kind,
            "slug": slug,
            "source": _SOURCE_REACT,
            "created_at": _now_iso_z(),
        }
        # body：content + "\n"（与 §22 既有惯例一致）
        body = content + "\n"
        # _render_frontmatter 是 S2 已有的：序列化 frontmatter 拼回 markdown
        text = _render_frontmatter(meta, body)
        # 写入（fs.write 是原子的：tempfile.mkstemp + os.replace）
        await fs.write(uri, text)
        changed_uris.append(uri)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_extract.py -q`
Expected: PASS（Task 1 的 8 + Task 2 的 5 = 13 passed）。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/extract.py tests/test_auth/test_memory_extract.py && git commit -m "$(cat <<'EOF'
feat(memory-s4): extract_and_persist 非 supersede 路径 + S2/S3 chain + 失败隔离

MemoryExtractor.extract_and_persist 实现 §5.1 / §5.7：调 LLM → parse JSON →
对每条 memory try/except 隔离写入 .md（非 identity-supersede 路径，T3 接管
identity 类）→ 收集 changed_uris → 一次性 batched S2.regenerate +
S3.index_changed（非每条调一次）。覆盖 §5.9 场景 ①②④⑤⑥：空数组零写 /
单 preference 写入+chain / 多条非 identity 一次 batched / LLM JSON 失败抛错 /
单条 fs.write 失败隔离。设计：文件式记忆重构-设计 §5.7/§5.9。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" && git status
```

---

### Task 3: Identity-supersede via `archive/` + slug 幂等 + 端到端

**Files:**
- Modify: `src/service/memory/extract.py`（替换 `_write_one_memory` 的 supersede 占位 → 真实算法 + 新增 `_supersede_identity` helper）
- Modify: `tests/test_auth/test_memory_extract.py`（追加场景 ③⑦⑧）

实现 §5.4 identity-supersede + §5.9 场景 ③（identity-supersede 完整路径）/⑦（slug 幂等 — 同 content 同 slug 不重写）/⑧（端到端：fake LLM 跑通到 Weaviate 可召回）。

- [ ] **Step 1: 在测试文件追加 3 个场景**

在 `tests/test_auth/test_memory_extract.py` 末尾追加：

```python
# ── Task 3：identity-supersede + 幂等 + 端到端 ───────────────────
@pytest.mark.asyncio
async def test_extract_identity_supersedes_old_via_archive(tmp_path):
    """场景③：先有一份旧 identity .md → 跑 ReAct 返回新 identity →
    旧 .md 被 mv 到 archive/ 子目录 + 新 .md 写入 + changed_uris 含三件。
    """
    extractor, fs, memgen, recaller, llm, emb, wv = _make_extract_stack(
        tmp_path,
        '{"memories":[{"kind":"identity","content":"用户的名字是李龙飞",'
        '"supersedes_kind":"identity"}]}',
    )
    # 先写一份"旧 identity .md"（模拟前几轮已经写入）
    old_content = "用户的名字是王山河"
    old_slug = _compute_slug(old_content)
    old_uri = f"ke://u/7/global/identity/{old_slug}.md"
    await fs.write(
        old_uri,
        _render_frontmatter(
            {"kind": "identity", "slug": old_slug, "source": "react",
             "created_at": "2026-05-20T01:00:00Z"},
            old_content + "\n",
        ),
    )

    # 跑 ReAct 抽取（返回新 identity，supersedes_kind="identity"）
    await extractor.extract_and_persist(
        fs, memgen, recaller, user_id=7,
        turn_text="用户：我改名为李龙飞了",
    )

    # 验证：
    # 1. 新 identity .md 写入到原位置
    new_slug = _compute_slug("用户的名字是李龙飞")
    new_uri = f"ke://u/7/global/identity/{new_slug}.md"
    assert await fs.exists(new_uri)
    # 2. 旧 identity .md 已经被 mv 到 archive/ 子目录（同 slug 文件名）
    archive_uri = f"ke://u/7/global/identity/archive/{old_slug}.md"
    assert await fs.exists(archive_uri)
    # 3. 旧 path 已不再存在（被 mv 走了）
    assert not await fs.exists(old_uri)
    # 4. 验证归档后 .md 内容与原始一致（fs.mv 是 rename 不改内容）
    raw = await fs.read(archive_uri)
    _meta, body = _split_frontmatter(raw)
    assert body.strip() == old_content


@pytest.mark.asyncio
async def test_extract_slug_idempotent_same_content_no_rewrite(tmp_path):
    """场景⑦：同 content 第二次抽取 → 同 slug → 同 path → fs 已存在文件不变；
    S2.regenerate 哈希命中零 LLM；S3.index_changed 哈希命中零 embed。
    """
    react_json = (
        '{"memories":[{"kind":"preference",'
        '"content":"用户偏好中文","supersedes_kind":null}]}'
    )
    extractor, fs, memgen, recaller, llm, emb, wv = _make_extract_stack(
        tmp_path, react_json
    )
    # 第一次抽取
    await extractor.extract_and_persist(
        fs, memgen, recaller, user_id=7, turn_text="用户：我喜欢中文"
    )
    slug = _compute_slug("用户偏好中文")
    uri = f"ke://u/7/global/preference/{slug}.md"
    # 拍快照
    first_md = await fs.read(uri)
    first_emb_calls = emb.calls
    first_llm_calls = llm.calls

    # 第二次抽取（同 content）→ 同 slug → 同 path → fs.write 覆盖（内容一致）→
    # S2.regenerate 检查 .abstract.md frontmatter src_hash 命中 → 不调 LLM；
    # S3.index_changed 检查 Weaviate 对象 hash 命中 → 不调 embedder。
    await extractor.extract_and_persist(
        fs, memgen, recaller, user_id=7, turn_text="用户：我喜欢中文（重复）"
    )
    # .md 文件内容不变（覆盖写但内容相同）
    assert await fs.read(uri) == first_md
    # embedder 调用次数零增量（S3 哈希命中）
    assert emb.calls == first_emb_calls
    # LLM 调用次数：S4 ReAct 调用 +1（每轮一次）；S2 因为 src_hash 命中不调 → 仅 +1
    assert llm.calls == first_llm_calls + 1


@pytest.mark.asyncio
async def test_extract_end_to_end_recall_works(tmp_path):
    """场景⑧：端到端 — fake LLM + 真链路一次跑完 → Weaviate 中能召回该记忆。"""
    react_json = (
        '{"memories":[{"kind":"identity","content":"用户的名字是李龙飞",'
        '"supersedes_kind":"identity"}]}'
    )
    extractor, fs, memgen, recaller, llm, emb, wv = _make_extract_stack(
        tmp_path, react_json
    )
    await extractor.extract_and_persist(
        fs, memgen, recaller, user_id=7,
        turn_text="用户：我叫李龙飞",
    )
    # 召回（query 关键字"名字"应命中 identity 子树）
    block = await recaller.recall_memory_block(
        fs, "用户的名字", user_id=7, top_k=5
    )
    # block 非空（有命中）且含名字
    assert block != ""
    assert "李龙飞" in block
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_extract.py -q -k "supersedes or idempotent or end_to_end"`
Expected: FAIL — identity-supersede 当前是 T2 的 deferred 占位，不会写入新 .md，也不会归档旧 .md（test_extract_identity_supersedes_old_via_archive 因新 .md 不存在而 fail；其他两个测试也部分 fail）。

- [ ] **Step 3: 实现 `_supersede_identity` + 替换 `_write_one_memory` 中的 supersede 占位**

在 `src/service/memory/extract.py` 的 `MemoryExtractor` 类内（`_write_one_memory` 之后）追加 `_supersede_identity` 方法：

```python
    async def _supersede_identity(
        self,
        fs: MemoryFS,
        user_id: int,
        new_slug: str,
    ) -> list[str]:
        """归档同 user 同 kind=identity 的所有旧 .md（slug != new_slug）至 archive/。

        §5.4 设计：
        - 取 identity 目录直接子项
        - 筛真记忆 .md（非 .abstract.md / .overview.md / 末段恰 .md / slug != new_slug）
        - 对每个旧 .md `fs.mv` 到 archive/ 子目录
        - 返回所有发生 mv 的 uri 列表（src + dst），供 changed_uris 收集

        S3.index_changed 见 src 不 exists 删 / 见 dst exists 加 — 自洽。
        """
        # 用户的 identity 目录路径
        base = f"ke://u/{user_id}/global/identity"
        # ls 取直接子项；目录不存在抛 MemoryNotFound（首次写 identity 时正常）
        try:
            entries = await fs.ls(base)
        except MemoryNotFound:
            # 目录不存在 → 无旧 identity 可归档（首次写 identity）
            return []

        changed: list[str] = []
        for name in entries:
            # 跳过 S2 生成的 .abstract.md / .overview.md
            if name in (_ABSTRACT_SUFFIX, _OVERVIEW_NAME):
                continue
            if name.endswith(_ABSTRACT_SUFFIX):     # 子文件 L0：{slug}.abstract.md
                continue
            # 非 .md 后缀 → 视为子目录（如 archive/）；不归档子目录本身
            if not name.endswith(_MD_SUFFIX):
                continue
            # 提取 slug（末段去 .md）
            slug = name[: -len(_MD_SUFFIX)]
            if slug == new_slug:
                # 新写入的不归档（同 content 幂等场景）
                continue
            # mv 到 archive/ 子目录（同名文件）
            src = f"{base}/{name}"
            dst = f"{base}/{_ARCHIVE_DIRNAME}/{name}"
            try:
                await fs.mv(src, dst)
            except (MemoryNotFound, MemoryPathError) as exc:
                # mv 失败（如目标已存在）— 单条目隔离，记 debug 跳过
                _log.debug(
                    "extract: archive mv failed src=%r dst=%r: %r",
                    src, dst, exc,
                )
                continue
            # 记录这次 mv 的 src + dst（供 S3.index_changed 对账：
            # src 不 exists → delete Weaviate 对象 / dst exists → upsert）
            changed.append(src)
            changed.append(dst)
        return changed
```

然后**替换** `_write_one_memory` 的 supersede 占位段：

把：

```python
        # Task 3 会接管 supersedes_kind 路径；本 Task 仅处理 None
        if memory.get("supersedes_kind") == "identity":
            # T3 实现；本 Task 暂跳过（不写、不抛 — 避免误用）
            _log.debug(
                "extract: identity-supersede deferred to T3 (mem skipped this version)"
            )
            return
        # 路径
        slug = _compute_slug(content)
        uri = f"ke://u/{user_id}/global/{kind}/{slug}.md"
```

替换为（按 §5.4 完整实现）：

```python
        # 路径：先算 slug；identity-supersede 也需要 new_slug 来判断"是否归档自己"
        slug = _compute_slug(content)
        uri = f"ke://u/{user_id}/global/{kind}/{slug}.md"
        # identity-supersede（§5.4）：先归档同 kind=identity 旧 .md 到 archive/
        if memory.get("supersedes_kind") == "identity":
            archived = await self._supersede_identity(fs, user_id, slug)
            # 归档产生的 src/dst 都加入 changed_uris（S3 自洽对账）
            changed_uris.extend(archived)
```

- [ ] **Step 4: 跑全套测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_extract.py -q`
Expected: PASS（Task1 8 + Task2 5 + Task3 3 = 16 passed）。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/extract.py tests/test_auth/test_memory_extract.py && git commit -m "$(cat <<'EOF'
feat(memory-s4): identity-supersede via archive/ + slug 幂等 + 端到端

_supersede_identity：identity kind 直接子项扫描，筛真记忆 .md（非
.abstract.md / .overview.md，slug != new_slug），fs.mv 到 archive/
子目录归档，src + dst 加入 changed_uris（S3 自洽对账）。_write_one_memory
supersede 占位替换为完整 §5.4 算法。覆盖 §5.9 场景 ③⑦⑧：
identity-supersede 完整路径 / slug 幂等同 content 同 slug 不重写 /
端到端 fake LLM 跑通到 Weaviate 可召回。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" && git status
```

---

### Task 4: §22 删除 + qa_router 闭包替换 + sse_emitter `OnMemoryCallback` 签名变更 + 删除 obsolete tests + 广回归

**Files:**
- Modify: `src/service/memory/service.py`（删除 §22 5 函数 + 7 常量 + import 名）
- Modify: `src/service/qa_engine/prompts.py`（删除 line 380 起的 `_USER_MEM_INTENT_SYSTEM`）
- Modify: `src/service/qa_engine/sse_emitter.py`（`OnMemoryCallback` 签名 + invocation 改为传 `answer_text`）
- Modify: `src/service/qa_router.py`（删 imports + 替换 `_make_memory_writer` 闭包 + 调用方更新）
- Modify: `tests/test_auth/test_memory_service.py`（删除 31 个 §22 测试 + import 行）
- Delete: `tests/test_auth/test_memory_router_hook.py`（整文件删；14 个测试全 §22 闭包行为）

- [ ] **Step 1: 删除 `tests/test_auth/test_memory_router_hook.py` 整文件**

Run:
```
cd /Users/java/knowledge-engineering-auth && git rm tests/test_auth/test_memory_router_hook.py
```
Expected: `rm 'tests/test_auth/test_memory_router_hook.py'`。该文件 14 个 `_make_memory_writer` 测试全部针对 §22 闭包行为，被 S4 替换后失去意义；S4 单元覆盖在 `test_memory_extract.py`，闭包集成由现有 qa_router integration tests 自然覆盖。

- [ ] **Step 2: 删除 `tests/test_auth/test_memory_service.py` 中 31 个 §22 测试**

读文件。识别要删的测试函数（共 31 个）：

**`test_detect_*` (14 个)**：
- line 158 `test_detect_trigger_strips_prefix`
- line 164 `test_detect_no_trigger_returns_none`
- line 169 `test_detect_trigger_but_empty_content_returns_none`
- line 393 `test_detect_project_trigger_strips_prefix`
- line 399 `test_detect_project_no_trigger_or_empty`
- line 576 `test_detect_prefix_still_works_no_regression`
- line 581 `test_detect_suffix_trailing_trigger`
- line 587 `test_detect_suffix_strips_trailing_punctuation`
- line 592 `test_detect_prefix_priority_when_both`
- line 597 `test_detect_none_and_empty`
- line 607 `test_detect_suffix_bangwo_jizhu_longest_first`
- line 613 `test_detect_prefix_bangwo_jizhu`
- line 617 `test_detect_content_equals_trigger_not_overstripped`
- line 623 `test_detect_suffix_semicolon_punct`

**`test_parse_*` (9 个)**：
- line 642 `test_parse_valid_json`
- line 651 `test_parse_strips_code_fence`
- line 659 `test_parse_skip`
- line 666 `test_parse_invalid_json_falls_back`
- line 675 `test_parse_llm_raises_falls_back`
- line 682 `test_parse_bad_enum_or_missing_keys_falls_back`
- line 690 `test_parse_non_dict_json_falls_back`
- line 699 `test_parse_weird_supersedes_kind_coerced_none`
- line 707 `test_parse_whitespace_content_falls_back`

**`test_write_explicit_*` (8 个)**：
- line 221 `test_write_explicit_adds_user_memory_row`
- line 406 `test_write_explicit_project_adds_row`
- line 723 `test_write_default_still_preference_no_regression`
- line 732 `test_write_identity_supersedes_old_identity_only`
- line 751 `test_write_preference_appends_no_archive`
- line 779 `test_write_identity_select_has_where_filter`
- line 796 `test_write_identity_no_prior_just_inserts`
- line 809 `test_write_identity_archives_all_prior_actives`

**做法**：对每个测试函数，向上找最近的 `@pytest.mark.asyncio` 或 `def test_` 边界，向下找下一个 `def test_` 或文件末尾 / 类边界，**整函数删除**（包含其装饰器与 docstring）。同时删除文件顶部 import 行中的：

```python
    detect_explicit_memory,
    write_explicit_memory,
    parse_user_memory_intent,
    write_explicit_project_memory,
    detect_explicit_project_memory,
```

（保留 `recall_memory_block` / `_extract_focus_entity_ids` / `maybe_compact_session` 等）。

- [ ] **Step 3: 确认删除生效 + 其余 26 测试仍过**

Run: `cd /Users/java/knowledge-engineering-auth && grep -n "test_detect_\|test_parse_\|test_write_explicit_\|detect_explicit_memory\|write_explicit_memory\|parse_user_memory_intent\|write_explicit_project_memory\|detect_explicit_project_memory" tests/test_auth/test_memory_service.py`
Expected: 空输出（无任何匹配）。

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_service.py -q`
Expected: 26 passed（删了 31 个 §22 测试，剩 S3/S5/synthesizer 类测试全过）。

注：如果 import 行中其他 import 也指向已删 §22 名，需要一并删除；如果其他被 import 的名称（如 `_extract_focus_entity_ids`）仍然有效，import 行保留对应名称。

- [ ] **Step 4: 在 `src/service/memory/service.py` 删除 §22 函数 + 常量**

读 `/Users/java/knowledge-engineering-auth/src/service/memory/service.py`。删除以下内容（保持其他代码不动）：

**删除 line 26 起的常量块**（包括相关注释）：

```python
# 显式记忆触发词（关键词起步；spec §15 开放问题留 P1 用关键词）
# 命中后剥掉触发词 + 紧随的冒号/空白，剩余即记忆内容。
# ⚠️ 顺序约定：若将来新增触发词是已有词的「超串」（如「顺便记一下」含「记一下」），
#    必须把更具体的放前面，否则被较短前缀先匹配（当前 5 个互不为前缀，安全）。
_TRIGGERS = ("请记住", "记住", "记一下", "记下", "帮我记住")


# 句尾后缀判定前先剥掉的尾部空白与中英文标点（不影响前缀分支）。含中英文分号。
_TRAILING_PUNCT = " 　\t。，、！？.!?,；;"
# 内容右侧清理集（冒号分隔约定残留 + 尾部标点）；DRY：前缀/后缀两分支复用。
_CONTENT_RSTRIP = " :：\t，、," + _TRAILING_PUNCT
# endswith 匹配必须最长触发词优先：「记住」是「请记住」「帮我记住」的后缀，
# 按 _TRIGGERS 原序匹配会把「帮我记住」误剥成「帮我」+「记住」。
# startswith 分支不受影响（5 个触发词互不为前缀，原序安全）。
_TRIGGERS_BY_LEN = tuple(sorted(_TRIGGERS, key=len, reverse=True))
```

**删除 `detect_explicit_memory` 整函数**（line 39 起）。

**删除 `_PROJECT_TRIGGERS` 常量与其注释**（line 75-76）：

```python
#    后调通用 detect_explicit_memory，否则「记住这个工程：X」会被误判为 user 级。
_PROJECT_TRIGGERS = ("记住这个工程", "记住本工程", "记住该工程", "工程记住")
```

**删除 `detect_explicit_project_memory` 整函数**（line 80 起）。

**删除 `_CODE_FENCE_RE` + `_VALID_KINDS` 常量**（line 148 起）：

```python
_CODE_FENCE_RE = re.compile(r"^`{1,4}(?:json)?\s*(.*?)\s*`{1,4}$", re.DOTALL)


_VALID_KINDS = ("identity", "preference", "style_feedback")
```

**删除 `parse_user_memory_intent` 整函数**（line 154 起）。

**删除 `write_explicit_memory` 整函数**（line 193 起）。

**删除 `write_explicit_project_memory` 整函数**（line 240 起）。

**修改 line 17 import 行**：

把：
```python
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM, _USER_MEM_INTENT_SYSTEM
```

替换为：
```python
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM
```

**检查其他顶部 import**：扫一遍 service.py 顶部 import，若有仅服务于刚删函数的 import（如可能 unused 的 `json` / `re`），按需删除。**保留** `recall_memory_block` / `_extract_focus_entity_ids` / `maybe_compact_session` 等函数。

- [ ] **Step 5: 在 `src/service/qa_engine/prompts.py` 删除 `_USER_MEM_INTENT_SYSTEM`**

读 `src/service/qa_engine/prompts.py` line 378-394（`_USER_MEM_INTENT_SYSTEM` 常量及其前注释）。把这一整段删除：

```python


# 用户级显式记忆意图解析（轻量，仅显式记忆门控通过后调一次）。
# 设计：[[记忆系统-设计]] §22.4。CC extractMemories「不要写重复、先更新」同款指令。
_USER_MEM_INTENT_SYSTEM = (
    "你是用户记忆意图解析器。给你一段用户希望被记住的话，判定并输出严格 JSON。"
    "字段：tier（取 'user' 或 'skip'：值得长期记住关于这个用户的事 → user；"
    "无意义/临时/不该长期记 → skip）；"
    "kind（'identity'=用户身份/姓名/自我称呼/角色；'preference'=长期偏好；"
    "'style_feedback'=对回答风格的反馈）；"
    "content（把这句话规范化为一句第三人称陈述事实，如『用户的名字是李龙飞』）；"
    "supersedes_kind（若本条是身份类、会取代该用户既有身份事实 → 'identity'，否则 null）。"
    "规则：身份类（改名/我叫/称呼我）kind 必为 identity 且 supersedes_kind 必为 'identity'"
    "（先更新旧的、不要并存重复）；只输出 JSON 对象本身，不要代码块、不要解释、不要多余文字。"
    '示例输出：{"tier":"user","kind":"identity","content":"用户的名字是李龙飞","supersedes_kind":"identity"}'
)
```

- [ ] **Step 6: 修改 `src/service/qa_engine/sse_emitter.py`：`OnMemoryCallback` 签名 + invocation**

读 `src/service/qa_engine/sse_emitter.py`。找到 line 62-68（`OnMemoryCallback` 类型定义 + 上方注释）：

```python
# on_memory 回调：done + session_title 之后调用（镜像 on_title 模式）。
# router 用它来：解析显式记忆意图 → 写 qa_user_memory + 视情况压缩会话记忆。
# 返回 None；失败静默（记忆是辅助，绝不影响主答）。
OnMemoryCallback = Callable[
    [],
    Awaitable[None],
]
```

替换为：

```python
# on_memory 回调：done + session_title 之后调用（镜像 on_title 模式）。
# router 用它来：ReAct 抽取本轮可记忆事实 → 写文件 .md → S2.regenerate + S3.index_changed
# + 视情况压缩会话记忆。返回 None；失败静默（记忆是辅助，绝不影响主答）。
# 入参 answer_text：assistant 本轮答案的拼接文本（S4 ReAct 需要看 user+assistant 两侧）。
OnMemoryCallback = Callable[
    [str],
    Awaitable[None],
]
```

找到 line 326-331 附近的 `on_memory` 调用：

```python
    # 9. on_memory（记忆系统 P1，2026-05-16）：done + session_title 之后调。
    #    ...
    if on_memory is not None:
        try:
            await on_memory()
```

替换调用语句 `await on_memory()` 为传 `answer_text`：

```python
    # 9. on_memory（记忆系统 S4：ReAct 抽取，2026-05-21）：done + session_title 之后调；
    #    传 answer_text 给闭包以喂 ReAct（S4 需 user + assistant 两侧文本）。
    if on_memory is not None:
        try:
            # 拼 assistant 输出文本：所有 section content 用双换行连接
            answer_text = "\n\n".join(
                (s.get("content") or "") for s in answer.sections
            )
            await on_memory(answer_text)
```

（请基于真实文件上下文做最小化插入；不要错改其他 `await on_memory(...)` 形式以外的语句。）

- [ ] **Step 7: 修改 `src/service/qa_router.py`：替换 `_make_memory_writer` 闭包 + 删 imports**

读 `src/service/qa_router.py`。在 line 43-50 附近找到 import 块：

```python
from src.service.memory.service import (
    recall_memory_block,
    detect_explicit_memory,
    write_explicit_memory,
    parse_user_memory_intent,
    maybe_compact_session,
    detect_explicit_project_memory,
    write_explicit_project_memory,
)
```

替换为（删除 5 个 §22 函数名，保留 `recall_memory_block` + `maybe_compact_session`）：

```python
from src.service.memory.service import (
    recall_memory_block,
    maybe_compact_session,
)
```

找到 `_make_memory_writer` 闭包定义（约 line 540-593）：函数签名 + docstring + `async def _writer() -> None:` 整段。完整替换为：

```python
def _make_memory_writer(
    *,
    db,
    llm,
    user_id: int,
    session_id: str,
    question: str,
    project_id: str | None = None,
    force_compact: bool = False,
):
    """构造 on_memory 回调（闭包）。done 之后异步执行：

    1. S4 ReAct 抽取本轮可记忆事实 → 写文件 .md → S2.regenerate + S3.index_changed
    2. 会话消息达阈值 → 压缩会话工作状态（§22 暂留 DB tier，S5 后续迁文件）。

    全程异常静默（记忆是辅助，绝不影响主答）。
    设计：[[文件式记忆重构-设计]] §5.5。
    新签名：闭包返回 async def _writer(answer: str) — sse_emitter 传 assistant
    答案文本给闭包，ReAct 需要 user + assistant 两侧文本。
    """
    async def _writer(answer: str) -> None:
        # 1. S4：ReAct 抽取 + 文件写入 + S2/S3 链
        try:
            # 局部 import：避免顶部循环依赖（recall.py / extract.py / memgen.py
            # → service.py），与 service.recall_memory_block 同模式
            import os
            from src.service.memory.vfs import MemoryFS
            from src.service.memory.memgen import MemoryGen
            from src.service.memory.recall import (
                MemoryRecaller, MemoryL0Store, _DefaultEmbedder,
            )
            from src.service.memory.extract import MemoryExtractor

            # 构造 fs（默认 root 由 KE_MEM_ROOT 环境变量或仓库根派生）
            fs = MemoryFS()
            # MemoryGen + Weaviate 客户端（沿用 service.recall_memory_block 的
            # env 读取方式；S7 hardening 把这些提到 module-level singleton）
            memgen = MemoryGen(llm)
            url = os.getenv("WEAVIATE_URL", "http://127.0.0.1:8080")
            grpc_port = int(os.getenv("WEAVIATE_GRPC_PORT", "50051"))
            api_key = os.getenv("WEAVIATE_API_KEY") or None
            store = MemoryL0Store(
                url=url, grpc_port=grpc_port,
                collection_name="memory_l0", dimension=1024, api_key=api_key,
            )
            recaller = MemoryRecaller(
                embedder=_DefaultEmbedder(),
                weaviate_client=store._client,
            )
            extractor = MemoryExtractor(llm)
            # 拼本轮文本（user + assistant）喂 ReAct
            turn_text = f"用户：{question}\n助理：{answer}"
            await extractor.extract_and_persist(
                fs, memgen, recaller,
                user_id=user_id,
                turn_text=turn_text,
            )
        except Exception:
            _log.debug(
                "S4 ReAct extract failed for session %s, silently ignored",
                session_id, exc_info=True,
            )
        # 2. 会话压缩（§22 暂留 DB tier，S5 后续迁文件）
        try:
            await maybe_compact_session(
                db, llm, session_id=session_id, force=force_compact,
            )
        except Exception:
            pass

    return _writer
```

**注**：闭包构造 `_make_memory_writer` 的调用方（router 的 endpoint 函数里调 `stream_qa_answer(... on_memory=_make_memory_writer(...))`）签名未变；只是返回的 `_writer` 接受 `answer: str` 参数（sse_emitter 在 Step 6 改后会传入）。

- [ ] **Step 8: 跑全套确认通过**

Run S4 套件: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_extract.py -q`
Expected: 16 passed（同 T3 结束）。

Run test_memory_service: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_service.py -q`
Expected: 26 passed（§22 31 测试已删）。

Run 广回归: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q`
Expected: PASS（0 failed；总数减少 ~50 个测试 = 31 §22 + ~14 test_memory_router_hook + 加 16 S4 新）。

若任何用例 FAIL：STOP 报告（不能绕过）。常见预期之一：`test_qa_router_classifier.py` 或 `test_qa_router_*.py` 集成测试若 mock `_make_memory_writer` 内部组件，需更新 mock；不允许直接 skip 已有用例。

- [ ] **Step 9: import 自检 + 行为 smoke**

Run:
```
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "
from src.service.memory.extract import MemoryExtractor
from src.service.memory.service import recall_memory_block, maybe_compact_session, _extract_focus_entity_ids
from src.service.memory.recall import MemoryRecaller, MemoryL0Store, _DefaultEmbedder
from src.service.memory.memgen import MemoryGen
from src.service.memory.vfs import MemoryFS
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM, _MEM_L0_SYSTEM, _MEM_L1_SYSTEM, _MEM_EXTRACT_SYSTEM
from src.service.qa_router import _make_memory_writer, router
print('imports ok')
"
```
Expected: 输出 `imports ok`。

Run 确认 §22 已无残留：
```
cd /Users/java/knowledge-engineering-auth && grep -rn "detect_explicit_memory\|parse_user_memory_intent\|write_explicit_memory\|write_explicit_project_memory\|detect_explicit_project_memory\|_USER_MEM_INTENT_SYSTEM\|_TRIGGERS_BY_LEN\|_PROJECT_TRIGGERS" src/ tests/ 2>/dev/null | grep -v ".pyc"
```
Expected: 空输出（全部 §22 显式 trigger / parse / write 命名已根除）。

- [ ] **Step 10: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/service.py src/service/qa_engine/prompts.py src/service/qa_engine/sse_emitter.py src/service/qa_router.py tests/test_auth/test_memory_service.py && git commit -m "$(cat <<'EOF'
feat(memory-s4): 删除 §22 显式 trigger 全链 + qa_router 闭包替换 + OnMemoryCallback 签名变更

service.py 删 5 函数（detect_explicit_memory / detect_explicit_project_memory
/ parse_user_memory_intent / write_explicit_memory / write_explicit_project_memory）
+ 7 常量（_TRIGGERS / _TRAILING_PUNCT / _CONTENT_RSTRIP / _TRIGGERS_BY_LEN
/ _PROJECT_TRIGGERS / _CODE_FENCE_RE / _VALID_KINDS）+ import _USER_MEM_INTENT_SYSTEM。
prompts.py 删 _USER_MEM_INTENT_SYSTEM 常量。
sse_emitter.py：OnMemoryCallback 由 Callable[[], ...] 改为 Callable[[str], ...]；
invocation await on_memory() → await on_memory(answer_text)（answer_text 拼自
answer.sections 各 section.content）。
qa_router.py：_make_memory_writer 闭包整段替换为 S4 路径；闭包内构造 MemoryFS +
MemoryGen + MemoryL0Store + MemoryRecaller + MemoryExtractor → extract_and_persist；
保留 maybe_compact_session（§22 暂留 DB tier，S5 后续）。
test_memory_service.py：删除 31 §22 测试（14 test_detect_* + 9 test_parse_* +
8 test_write_explicit_*），保留 26 个 S3/S5/synthesizer 测试。设计：文件式
记忆重构-设计 §5.5/§5.6。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" && git status
```

---

## 实现者备注（避免常见返工）

1. **测试解释器**：homebrew `python3` 无 `pytest-asyncio`；必须用仓库 venv：`./venv/bin/python -m pytest ...`。
2. **跨 S3/S4 fake stack 复用**：S4 测试 `from tests.test_auth.test_memory_recall import _FakeEmbedder, _FakeWeaviateClient`。S3 的 fake 已严格 mirror v4 client surface，S4 测试零重复实现。
3. **不重复实现 frontmatter / 哈希工具**：S4 import 复用 S2 `memgen` 的 `_render_frontmatter` / `_sha256_hex` / `_ABSTRACT_SUFFIX` / `_OVERVIEW_NAME` / `_MD_SUFFIX`。
4. **identity-supersede 是 archive/ 不是 fs.rm**：归档至 archive/ 子目录，旧 .md 仍是合法记忆文件（S2/S3 仍处理）；这是 feature 不是 bug（用户历史身份"曾经叫王山河"是合理上下文）。S7 hardening 可加"忽略 archive/"开关。
5. **slug = sha256(content)[:12] S4 内部生成**：不让 LLM 出 slug；抗碰撞 + 确定性 + 同 content 同 slug → 幂等天然达成（同 content 不重写、src_hash 不变、S2/S3 哈希命中 zero LLM/embed）。
6. **`OnMemoryCallback` 签名变更**：原 `Callable[[], Awaitable[None]]` → `Callable[[str], Awaitable[None]]`。只有一个消费者（`_make_memory_writer` in qa_router），同 PR 协调更新；不影响其他 sse_emitter 调用方。
7. **§22 完全删除 vs 保留少量**：D2 锁定「§22 重做」，整条显式 trigger / parse / write_explicit 链都删；ReAct 看本轮文本即可识别「记住 X」类显式句，不需要双轨维护。
8. **S4 不接 service.py 的 `_extract_focus_entity_ids` / `maybe_compact_session` / `_SESSION_COMPACT_SYSTEM`**：这些是 S5/§21 territory，本计划不动。
9. **失败语义**：`MemoryExtractor.extract_and_persist` 自身**清晰抛错**（§5.7）；单条 memory 写入失败独立 try/except 隔离（_log.debug 跳过）；调用方 `_writer` 闭包包 `try/except Exception → _log.debug` 兜全部（与 S3 自包形成深度防护）。
10. **不引新依赖**：pyyaml / weaviate-client 已有；S4 仅 stdlib + 现有包。

## 自检（writing-plans Self-Review）

- **Spec 覆盖**（§5 全部要点 ↔ task 映射）：
  - §5.1 架构 + MemoryExtractor.__init__ + extract_and_persist → T1（构造）+ T2/T3（公开 API 实现）
  - §5.2 _MEM_EXTRACT_SYSTEM prompt → T1 Step 4
  - §5.3 路径 + frontmatter（kind/slug=sha256[:12]/source/created_at）→ T2 `_write_one_memory`
  - §5.4 identity-supersede via archive/ → T3 `_supersede_identity`
  - §5.5 qa_router `_make_memory_writer` 闭包替换 → T4 Step 7
  - §5.6 §22 删除清单（5 函数 + 7 常量 + 1 prompt + import 行）→ T4 Step 1/2/4/5/7
  - §5.7 失败语义（引擎抛错 / 每条隔离 / 闭包兜底）→ T2 Step 3 + T4 Step 7
  - §5.8 YAGNI 范围 → 备注 8/9
  - §5.9 测试九场景：①→T2 / ②→T2 / ③→T3 / ④→T2 / ⑤→T2 / ⑥→T2 / ⑦→T3 / ⑧→T3 / ⑨→T4 Step 8（广回归）
  - §5.10 决策日志 → 全部决策已落到任务步骤里
- **占位扫描**：无 TBD / TODO / "fill in" / "similar to Task N"。每步含完整代码块 / 确切命令 / 期望输出。注：T2 Step 3 中 `_write_one_memory` 含 "T3 接管 identity-supersede" 字样 — 这是**有意的增量 TDD pattern**（与 S2 plan 中 "T3 实现" 占位同模式），T3 Step 3 显式替换。非计划占位。
- **类型一致性**：`MemoryExtractor.__init__(self, llm: Any)` / `async extract_and_persist(self, fs: MemoryFS, memgen: MemoryGen, recaller: MemoryRecaller, *, user_id: int, turn_text: str) -> None` / helpers `_compute_slug(content: str) -> str` / `_parse_react_json(raw: str) -> list[dict]` / `_now_iso_z() -> str` / `_supersede_identity(self, fs, user_id, new_slug) -> list[str]` 跨任务一致；常量 `_VALID_KINDS` / `_SLUG_HEX_LEN=12` / `_SOURCE_REACT="react"` / `_ARCHIVE_DIRNAME="archive"` 全程同名；fake LLM `async def complete(self, *, system, user, **kw) -> str` 与 S2/S3 同。`OnMemoryCallback` 类型签名变更 sse_emitter ↔ qa_router 协调一致（T4 Step 6+7 同 PR）。
