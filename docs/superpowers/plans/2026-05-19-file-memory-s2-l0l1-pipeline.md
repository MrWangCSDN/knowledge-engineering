# 文件式记忆重构 — S2：L0/L1 生成管线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增纯逻辑组件 `MemoryGen`，在记忆文件被写入/变更后自底向上生成 `.abstract.md`(L0) 与 `.overview.md`(L1)，并用内容哈希做幂等跳过与自愈。

**Architecture:** `src/service/memory/memgen.py` 的 `MemoryGen` 与 S1 `MemoryFS` 并列、仅依赖它（不 import FastAPI）。唯一对外 API `async regenerate(fs, changed_uris)`：① 对去重后的变更记忆文件各生成其 `{slug}.abstract.md`（`src_hash`=该 `.md` 正文 SHA-256）；② 收集所有变更文件的祖先目录，按路径深度降序逐级生成目录 `.abstract.md`(L0)+`.overview.md`(L1)（输入同为「该目录直接子项 L0 集合」，故同一 `inputs_hash`）。生成前算输入哈希、与目标 frontmatter 内哈希比对，一致则零 LLM 跳过；单条目失败 `_log.debug` 跳过不连累整批，下一轮按哈希补齐 = 自愈。S2 不碰 router/SSE、不自起任务（异步点由 S4/S6 持有）。

**Tech Stack:** Python 3.10+，stdlib（`hashlib`/`logging`），`pyyaml>=6.0`（已声明依赖，frontmatter 解析/序列化），S1 `MemoryFS`，KE LLM provider 鸭子接口 `async complete(system,user,**kw)->str`。测试 pytest + `pytest-asyncio`（`@pytest.mark.asyncio`），fake LLM + `MemoryFS(root=str(tmp_path))`，沿用 `tests/test_auth` 既有风格。

**单一来源设计：** Obsidian `/Users/java/obsidian/01 Engineering/knowledge-engineering/文件式记忆重构-设计.md` §3（§3.0–§3.8）。本计划不得引入 §3 之外的设计决策。

**仓库 / 分支：** `/Users/java/knowledge-engineering-auth` @ `release-0513`（本会话后端记忆工作长期分支；逐任务 commit 已授权；push/merge/部署须用户拍板，**不在本计划范围**）。

**测试命令前缀（务必用仓库 venv，homebrew python 无 pytest-asyncio）：**
`cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest`

---

## File Structure

| 文件 | 职责 | 创建/修改 |
|---|---|---|
| `src/service/memory/memgen.py` | `MemoryGen` 引擎 + frontmatter/哈希纯函数辅助。**新增**，不改既有文件。 | Create |
| `src/service/qa_engine/prompts.py` | 追加两个 system prompt 常量 `_MEM_L0_SYSTEM` / `_MEM_L1_SYSTEM`（紧接 `_USER_MEM_INTENT_SYSTEM` 之后，line 391 后）。 | Modify: `src/service/qa_engine/prompts.py:391`（在文件末尾追加，不动既有常量） |
| `tests/test_auth/test_memory_memgen.py` | S2 全部测试（§3.7 八条场景）。fake/捕获 LLM + 真 `MemoryFS(root=tmp_path)`。 | Create |

**边界（§3.6 YAGNI，不做）：** S3 向量化/召回/目录递归/可观测；S4 ReAct 抽取与轮末接线；S5 会话；S6 迁移；给 S1 加 stat/mtime；跨实例；记忆正文/业务 frontmatter 的产生（S4/S6 职责）。S2 只生成/维护 `.abstract.md`/`.overview.md` 及其哈希字段。

**关键既有接口（已核对真实代码，照此调用，勿臆造）：**
- `from src.service.memory.vfs import MemoryFS, MemoryNotFound`
- `MemoryFS(root: str | None = None)`；`resolve(uri)->str`**同步**；`async write(uri, content)->None`（原子、自动 mkdir -p 父目录）；`async read(uri)->str`（不存在→`MemoryNotFound`）；`async exists(uri)->bool`；`async ls(uri)->list[str]`（**已 `sorted`**；不存在→`MemoryNotFound`；非目录→`MemoryPathError`；空→`[]`）。
- prompts.py 既有 `_USER_MEM_INTENT_SYSTEM` 结束于 `src/service/qa_engine/prompts.py:391`；新常量紧随其后追加。
- 测试 fake LLM 形态（照抄）：`class _Fake...: async def complete(self, *, system: str, user: str, **kw) -> str:`（关键字参 `*`）。`@pytest.mark.asyncio`，`def _fs(tmp_path): return MemoryFS(root=str(tmp_path))`。

---

### Task 1: 模块骨架 + frontmatter/哈希纯函数 + 两个 prompt 常量

**Files:**
- Create: `src/service/memory/memgen.py`
- Modify: `src/service/qa_engine/prompts.py`（在 `:391` 之后文件末尾追加两常量）
- Test: `tests/test_auth/test_memory_memgen.py`

- [ ] **Step 1: 写失败测试（纯函数：哈希稳定 + frontmatter 往返 + 无 frontmatter 容错）**

创建 `tests/test_auth/test_memory_memgen.py`，内容：

```python
"""文件式记忆 S2：L0/L1 生成管线测试。设计：[[文件式记忆重构-设计]] §3。

fake/捕获 LLM + 真 MemoryFS(root=tmp_path)，沿用 tests/test_auth 既有风格。
"""
import pytest

from src.service.memory.vfs import MemoryFS
from src.service.memory.memgen import (
    MemoryGen,
    _sha256_hex,
    _split_frontmatter,
    _render_frontmatter,
)


def _fs(tmp_path):
    return MemoryFS(root=str(tmp_path))


# ── Task 1：纯函数 ───────────────────────────────────────────────
def test_sha256_hex_stable_and_utf8():
    # 同输入同输出（确定性）；中文按 UTF-8 编码
    assert _sha256_hex("用户的名字是李龙飞") == _sha256_hex("用户的名字是李龙飞")
    # 已知向量：空串 SHA-256
    assert _sha256_hex("") == (
        "e3b0c44298fc1c149afbf4c8996fb924"
        "27ae41e4649b934ca495991b7852b855"
    )
    # 不同输入→不同摘要
    assert _sha256_hex("a") != _sha256_hex("b")


def test_split_and_render_frontmatter_roundtrip():
    meta = {"src_hash": "deadbeef"}
    body = "用户的名字是李龙飞\n"
    text = _render_frontmatter(meta, body)
    assert text.startswith("---\n")
    got_meta, got_body = _split_frontmatter(text)
    assert got_meta == {"src_hash": "deadbeef"}
    assert got_body == body


def test_split_frontmatter_no_frontmatter_returns_empty_meta():
    # 不以 '---\n' 起 → ({}, 原文)
    meta, body = _split_frontmatter("just a body, no fm")
    assert meta == {}
    assert body == "just a body, no fm"


def test_split_frontmatter_unicode_preserved():
    # allow_unicode：中文不被转义成 \uXXXX
    text = _render_frontmatter({"k": "v"}, "中文正文\n")
    assert "中文正文" in text
    meta, body = _split_frontmatter(text)
    assert meta == {"k": "v"} and body == "中文正文\n"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_memgen.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.service.memory.memgen'`（collection error）。

- [ ] **Step 3: 创建 `src/service/memory/memgen.py` 骨架（仅纯函数 + `__init__`，先不实现 `regenerate`）**

```python
"""文件式记忆 S2：L0/L1 自底向上生成管线（MemoryGen）。

设计：[[文件式记忆重构-设计]] §3。纯逻辑，不依赖 FastAPI（与 vfs.py 并列）。
- L0 = `.abstract.md`（≤100 tok，S3 向量检索目标）：记忆文件与目录都有。
- L1 = `.overview.md`（≤1–2k tok，导航图）：仅目录有。
- 自底向上：先各变更记忆文件 L0，再其祖先目录按深度降序逐级 L0+L1。
- 内容哈希（SHA-256 hex 存 frontmatter）幂等跳过：哈希一致零 LLM；
  S6 整树 / 崩溃中途下一轮按哈希只补不一致项 = 幂等自愈，无 mtime。
仅 S2：不含 S3 召回/向量、S4 抽取/接线、S5 会话、S6 迁移、跨实例。
"""
from __future__ import annotations

import hashlib
import logging

import yaml  # pyproject 已声明依赖 pyyaml>=6.0

from src.service.memory.vfs import MemoryFS, MemoryNotFound
from src.service.qa_engine.prompts import _MEM_L0_SYSTEM, _MEM_L1_SYSTEM

_log = logging.getLogger(__name__)

# 生成文件名（固定，§3.3）：
#   记忆文件 {slug}.md  → 同目录 {slug}.abstract.md（L0）
#   目录              → 目录内 .abstract.md（L0）+ .overview.md（L1）
_ABSTRACT_SUFFIX = ".abstract.md"
_OVERVIEW_NAME = ".overview.md"
_MD_SUFFIX = ".md"


def _sha256_hex(text: str) -> str:
    """文本 UTF-8 的 SHA-256 十六进制摘要。

    纯内容派生的陈旧判定基元（§3.3）：无 mtime、无时钟，
    同输入恒同输出 → S6 整树/崩溃中途下一轮按哈希自愈、幂等。
    """
    # hashlib.sha256 接收 bytes，故先按 UTF-8 编码；hexdigest() 返回 64 位小写十六进制 str
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """拆 ``---\\nYAML\\n---\\n正文`` → (meta dict, 正文 str)。

    不以 ``---\\n`` 起（无 frontmatter）→ ``({}, 原文)``；
    YAML 段为空或非 dict → ``({}, 正文)``。用 PyYAML 安全解析
    （``yaml.safe_load`` 只认基本类型，杜绝任意对象构造）。

    约定（§3.2）：frontmatter 为简单 ``key: value``，不含裸 ``\\n---`` 行；
    S2 生成文件满足此约定，S4/S6 记忆文件须遵循 §3.2 schema。
    """
    # 没有起始分隔符 → 视为纯正文
    if not text.startswith("---\n"):
        return {}, text
    rest = text[4:]                       # 去掉开头的 "---\n"
    end = rest.find("\n---")              # 第一处 "\n---" 即 frontmatter 闭合
    if end == -1:                         # 没有闭合 → 容错为纯正文
        return {}, text
    yaml_src = rest[:end]                 # 两个 --- 之间的 YAML 源
    after = rest[end + 4:]               # 跳过 "\n---" 之后
    # 闭合行后常跟一个换行（"---\n正文"），剥掉它得到纯正文
    if after.startswith("\n"):
        after = after[1:]
    # 空 YAML 段不调用 safe_load（避免 None）
    meta = yaml.safe_load(yaml_src) if yaml_src.strip() else {}
    # safe_load 可能返回非 dict（如纯标量）→ 归一为 {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, after


def _render_frontmatter(meta: dict, body: str) -> str:
    """``(meta, body)`` → ``---\\nYAML\\n---\\n{body}``。

    ``sort_keys=False`` 保持插入序；``allow_unicode=True`` 让中文按原样
    输出（不转义成 ``\\uXXXX``，便于人读与 S3 向量化）。
    """
    # yaml.safe_dump 输出已自带末尾换行，形如 "src_hash: abc\n"
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    # f-string 拼装：开分隔符 + YAML + 闭分隔符 + 正文
    return f"---\n{fm}---\n{body}"


class MemoryGen:
    """L0/L1 自底向上生成引擎（§3.1）。

    llm：KE 既有 provider 鸭子接口 ``async complete(system,user,**kw)->str``，
    构造注入便于单测 fake。fs 每次调用传入（同一引擎可服务不同 MemoryFS
    实例/测试）。S2 不自起后台任务、不接 SSE（异步点由 S4/S6 持有）。
    """

    def __init__(self, llm) -> None:
        # 仅持有 llm；fs 走 regenerate 形参（§3.1）
        self._llm = llm
```

- [ ] **Step 4: 在 `src/service/qa_engine/prompts.py` 文件末尾（`:391` `_USER_MEM_INTENT_SYSTEM` 之后）追加两常量**

在 `prompts.py` 末尾追加（紧接 `_USER_MEM_INTENT_SYSTEM` 的闭合 `)` 之后）：

```python


# ─── 文件式记忆 S2：L0/L1 自底向上生成（2026-05-19）────────────────────────
# 设计：[[文件式记忆重构-设计]] §3.5。L0=可嵌入摘要（≤100 tok，S3 向量检索
# 目标）；L1=导航图（≤1–2k tok）。两常量就近置此（与会话压缩/意图解析同处）。
_MEM_L0_SYSTEM = (
    "你是记忆摘要器。把给定文本压成一句可独立检索的中文摘要："
    "聚焦关于本用户的稳定事实 / 偏好 / 身份，不超过约 100 token，"
    "不要前缀、不要解释、不要分点编号，直接输出摘要正文本身。"
)


_MEM_L1_SYSTEM = (
    "你是记忆导航图生成器。给你某目录下若干子项的摘要"
    "（以「## 子项名」分节）。聚成一张导航图：有哪些记忆条目、"
    "各自讲什么、需要时如何进一步查看其正文。中文，不超过约 1500 字，"
    "结构清晰可作为该子树索引；直接输出导航图正文，不要前缀、不要额外解释。"
)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_memgen.py -q`
Expected: PASS（4 passed）。

- [ ] **Step 6: import 自检（确认无 E402 / 循环导入）**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "from src.service.memory.memgen import MemoryGen, _sha256_hex, _split_frontmatter, _render_frontmatter; from src.service.qa_engine.prompts import _MEM_L0_SYSTEM, _MEM_L1_SYSTEM; print('ok')"`
Expected: 输出 `ok`。

- [ ] **Step 7: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/memgen.py src/service/qa_engine/prompts.py tests/test_auth/test_memory_memgen.py && git commit -m "$(cat <<'EOF'
feat(memory-s2): MemoryGen 骨架 + frontmatter/哈希纯函数 + 两 prompt 常量

文件式记忆重构 S2 T1：新增 src/service/memory/memgen.py（纯逻辑，
不依赖 FastAPI），含 _sha256_hex/_split_frontmatter/_render_frontmatter
（PyYAML，pyproject 已声明依赖）与 MemoryGen.__init__；prompts.py 追加
_MEM_L0_SYSTEM/_MEM_L1_SYSTEM。设计：文件式记忆重构-设计 §3.1/§3.3/§3.5。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" && git status
```

---

### Task 2: 记忆文件 L0（步骤①）+ 哈希幂等 + 单文件失败隔离

**Files:**
- Modify: `src/service/memory/memgen.py`（新增 `_is_memory_file`、`_gen_file_l0`，并实现 `regenerate` 的步骤①；步骤② Task 3 再加）
- Test: `tests/test_auth/test_memory_memgen.py`

实现 §3.7 场景 ①（单文件→旁 `.abstract.md` 含 `src_hash`+fake 内容）、④ 文件层（同输入再调 → fake LLM 未被再调用、文件未变）、⑦ 文件层（fake LLM 抛错→该文件跳过记 debug、不抛出）。

- [ ] **Step 1: 在测试文件追加失败测试（文件 L0：生成 / 幂等 / 失败隔离）**

在 `tests/test_auth/test_memory_memgen.py` 末尾追加：

```python
# ── Task 2：记忆文件 L0（步骤①）─────────────────────────────────
class _FixedLLM:
    """固定返回 + 记录调用次数与最后一次入参（捕获式 fake）。"""
    def __init__(self, ret="MOCK_SUMMARY"):
        self.ret = ret
        self.calls = 0
        self.last_system = None
        self.last_user = None

    async def complete(self, *, system: str, user: str, **kw) -> str:
        # 关键字参（*）与 KE provider 鸭子接口一致
        self.calls += 1
        self.last_system = system
        self.last_user = user
        return self.ret


class _BoomLLM:
    """每次 complete 都抛错（验证单条目失败隔离）。"""
    def __init__(self):
        self.calls = 0

    async def complete(self, *, system: str, user: str, **kw) -> str:
        self.calls += 1
        raise RuntimeError("llm boom")


_MEM_FM = (
    "---\n"
    "kind: identity\n"
    "slug: user-name\n"
    "---\n"
    "用户的名字是李龙飞\n"
)


@pytest.mark.asyncio
async def test_single_file_generates_sibling_abstract_with_src_hash(tmp_path):
    fs = _fs(tmp_path)
    llm = _FixedLLM("李龙飞的身份摘要")
    gen = MemoryGen(llm)
    uri = "ke://u/7/global/identity/user-name.md"
    await fs.write(uri, _MEM_FM)

    await gen.regenerate(fs, [uri])

    abs_uri = "ke://u/7/global/identity/user-name.abstract.md"
    assert await fs.exists(abs_uri)
    meta, body = _split_frontmatter(await fs.read(abs_uri))
    # src_hash = 该 .md 正文（frontmatter 之后）的 SHA-256（§3.3）
    assert meta["src_hash"] == _sha256_hex("用户的名字是李龙飞\n")
    assert "李龙飞的身份摘要" in body
    # L0 prompt 被用于文件摘要；user 入参 = 该 .md 正文
    assert llm.last_user == "用户的名字是李龙飞\n"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_file_l0_hash_idempotent_no_second_llm_call(tmp_path):
    fs = _fs(tmp_path)
    llm = _FixedLLM()
    gen = MemoryGen(llm)
    uri = "ke://u/7/global/identity/user-name.md"
    await fs.write(uri, _MEM_FM)

    await gen.regenerate(fs, [uri])
    abs_uri = "ke://u/7/global/identity/user-name.abstract.md"
    first = await fs.read(abs_uri)
    assert llm.calls == 1

    # 输入未变 → 再调 regenerate：哈希命中、零 LLM、文件逐字节不变
    await gen.regenerate(fs, [uri])
    assert llm.calls == 1
    assert await fs.read(abs_uri) == first


@pytest.mark.asyncio
async def test_file_l0_input_change_regenerates(tmp_path):
    fs = _fs(tmp_path)
    llm = _FixedLLM("v1")
    gen = MemoryGen(llm)
    uri = "ke://u/7/global/identity/user-name.md"
    await fs.write(uri, _MEM_FM)
    await gen.regenerate(fs, [uri])
    abs_uri = "ke://u/7/global/identity/user-name.abstract.md"
    h1 = _split_frontmatter(await fs.read(abs_uri))[0]["src_hash"]

    # 正文变更 → src_hash 变 → 重生
    llm.ret = "v2"
    await fs.write(uri, "---\nkind: identity\n---\n用户改名为王山河\n")
    await gen.regenerate(fs, [uri])
    meta, body = _split_frontmatter(await fs.read(abs_uri))
    assert meta["src_hash"] != h1
    assert meta["src_hash"] == _sha256_hex("用户改名为王山河\n")
    assert "v2" in body
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_file_l0_llm_error_is_isolated_not_raised(tmp_path, caplog):
    fs = _fs(tmp_path)
    gen = MemoryGen(_BoomLLM())
    uri = "ke://u/7/global/identity/user-name.md"
    await fs.write(uri, _MEM_FM)

    # 不抛出（§3.5 单条目失败隔离），且未写出 .abstract.md
    import logging
    with caplog.at_level(logging.DEBUG, logger="src.service.memory.memgen"):
        await gen.regenerate(fs, [uri])
    assert not await fs.exists("ke://u/7/global/identity/user-name.abstract.md")
    assert any("file L0 failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_regenerate_skips_non_memory_file_uris(tmp_path):
    fs = _fs(tmp_path)
    llm = _FixedLLM()
    gen = MemoryGen(llm)
    # .abstract.md / .overview.md / 非 .md 均非记忆文件 → 步骤①跳过
    await gen.regenerate(fs, [
        "ke://u/7/global/identity/x.abstract.md",
        "ke://u/7/global/identity/.overview.md",
        "ke://u/7/global/identity/notes.txt",
    ])
    assert llm.calls == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_memgen.py -q -k "single_file or hash_idempotent or input_change or llm_error_is_isolated or skips_non_memory"`
Expected: FAIL — `MemoryGen` 无 `regenerate`（`AttributeError`）。

- [ ] **Step 3: 在 `memgen.py` 的 `MemoryGen` 内实现步骤① + 分类辅助**

在 `class MemoryGen` 的 `__init__` 之后追加方法（`regenerate` 此版只做步骤①，步骤②为占位 `pass`，Task 3 替换）：

```python
    # ── 公开唯一 API ───────────────────────────────────────────────
    async def regenerate(self, fs: MemoryFS, changed_uris: list[str]) -> None:
        """对去重后的变更记忆文件：① 各生成其 ``{slug}.abstract.md``；
        ② 收集祖先目录、按深度降序逐级生成 ``.abstract.md``(L0)+
        ``.overview.md``(L1)（Task 3 实现）。单条目失败 ``_log.debug``
        跳过、不连累整批（下一轮按哈希补齐 = 自愈，§3.5）。
        """
        # 去重并保持稳定序；仅取记忆文件（.md 且非 .abstract.md/.overview.md）
        files: list[str] = []
        seen: set[str] = set()
        for uri in changed_uris:
            if uri in seen:                    # set 去重，O(1) 命中判断
                continue
            seen.add(uri)
            if self._is_memory_file(uri):
                files.append(uri)
            else:
                _log.debug("regenerate: skip non-memory-file uri %r", uri)

        # ① 记忆文件 L0：逐个 try，失败隔离（§3.5）
        for uri in files:
            try:
                await self._gen_file_l0(fs, uri)
            except Exception as exc:           # noqa: BLE001 单条目隔离
                _log.debug("regenerate: file L0 failed %r: %r", uri, exc)

        # ② 目录 L0/L1：Task 3 实现
        pass

    # ── 分类辅助 ──────────────────────────────────────────────────
    @staticmethod
    def _is_memory_file(uri: str) -> bool:
        """记忆文件 = 以 .md 结尾，且不是 .abstract.md / .overview.md。"""
        return (
            uri.endswith(_MD_SUFFIX)
            and not uri.endswith(_ABSTRACT_SUFFIX)
            and not uri.endswith(_OVERVIEW_NAME)
        )

    # ── 步骤①：记忆文件 L0 ────────────────────────────────────────
    async def _gen_file_l0(self, fs: MemoryFS, file_uri: str) -> None:
        """读 ``{slug}.md`` 正文 → LLM 压成一句 → 写同目录
        ``{slug}.abstract.md``，frontmatter 存 ``src_hash``（正文 SHA-256）。
        正文哈希命中已存在 .abstract → 跳过（零 LLM，§3.3）。
        """
        raw = await fs.read(file_uri)                 # 不存在→MemoryNotFound（上层捕获）
        _meta, body = _split_frontmatter(raw)         # src_hash 只认正文（§3.3）
        src_hash = _sha256_hex(body)
        # {slug}.md → {slug}.abstract.md（file_uri 必以 .md 结尾，调用前已 _is_memory_file）
        abs_uri = file_uri[: -len(_MD_SUFFIX)] + _ABSTRACT_SUFFIX
        if await fs.exists(abs_uri):
            old_meta, _ = _split_frontmatter(await fs.read(abs_uri))
            # str() 防御：SHA-256 全数字（极罕见）会被 YAML 解析成 int
            if str(old_meta.get("src_hash")) == src_hash:
                _log.debug("file L0 hash hit, skip %r", abs_uri)
                return
        summary = await self._llm.complete(system=_MEM_L0_SYSTEM, user=body)
        await fs.write(
            abs_uri,
            _render_frontmatter({"src_hash": src_hash}, summary.strip() + "\n"),
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_memgen.py -q`
Expected: PASS（Task 1 的 4 条 + Task 2 的 5 条 = 9 passed）。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/memgen.py tests/test_auth/test_memory_memgen.py && git commit -m "$(cat <<'EOF'
feat(memory-s2): 记忆文件 L0 生成（步骤①）+ src_hash 幂等 + 失败隔离

regenerate 去重并仅取记忆文件；_gen_file_l0 读 .md 正文 → LLM 压成
{slug}.abstract.md，frontmatter 存正文 SHA-256 src_hash，命中则零 LLM
跳过；单文件 LLM 失败 _log.debug 跳过不连累整批。设计 §3.3/§3.5/§3.7①④⑦。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" && git status
```

---

### Task 3: 目录 L0+L1（步骤②）+ 祖先收集/深度降序 + inputs_hash 幂等 + 目录失败隔离

**Files:**
- Modify: `src/service/memory/memgen.py`（新增 `_ancestor_dirs`、`_gen_dir_l0_l1`、`_stale`；用 `_ancestor_dirs` 替换 `regenerate` 步骤②的 `pass`）
- Test: `tests/test_auth/test_memory_memgen.py`

实现 §3.7 场景 ②（多文件同目录→目录 `.abstract`+`.overview` 含 `inputs_hash`、聚合子 L0）、③（自底向上：深目录 L1 先于浅目录、父见子新值）、⑤（输入变更只重生该链路）、⑦ 目录层（某目录 LLM 抛错→该目录跳过、其余仍生成）。

- [ ] **Step 1: 在测试文件追加失败测试（目录 L0/L1 + 自底向上 + 幂等 + 失败隔离）**

在 `tests/test_auth/test_memory_memgen.py` 末尾追加：

```python
# ── Task 3：目录 L0+L1（步骤②）──────────────────────────────────
class _RoutingLLM:
    """按 system 路由返回，并记录 (system, user) 调用序列（验证自底向上序）。"""
    def __init__(self):
        from src.service.qa_engine.prompts import _MEM_L0_SYSTEM, _MEM_L1_SYSTEM
        self._L0, self._L1 = _MEM_L0_SYSTEM, _MEM_L1_SYSTEM
        self.calls = []  # list[tuple[str tag, str user]]

    async def complete(self, *, system: str, user: str, **kw) -> str:
        tag = "L0" if system == self._L0 else "L1"
        self.calls.append((tag, user))
        # 回显 user 摘要，便于断言「父确实看到子的 L0」
        return f"{tag}:{user[:40]}"


def _mem(kind, body):
    return f"---\nkind: {kind}\n---\n{body}\n"


@pytest.mark.asyncio
async def test_multi_files_same_dir_overview_aggregates_child_l0(tmp_path):
    fs = _fs(tmp_path)
    llm = _RoutingLLM()
    gen = MemoryGen(llm)
    u1 = "ke://u/7/global/identity/name.md"
    u2 = "ke://u/7/global/identity/role.md"
    await fs.write(u1, _mem("identity", "名字是李龙飞"))
    await fs.write(u2, _mem("identity", "角色是架构师"))

    await gen.regenerate(fs, [u1, u2])

    # 每文件各有 L0
    assert await fs.exists("ke://u/7/global/identity/name.abstract.md")
    assert await fs.exists("ke://u/7/global/identity/role.abstract.md")
    # identity 目录有 .abstract.md(L0) + .overview.md(L1)，同一 inputs_hash
    am, ab = _split_frontmatter(
        await fs.read("ke://u/7/global/identity/.abstract.md"))
    om, ob = _split_frontmatter(
        await fs.read("ke://u/7/global/identity/.overview.md"))
    assert am["inputs_hash"] == om["inputs_hash"]
    # 聚合输入按子项名排序拼接（name 在 role 前）：两子 L0 都进入了 user
    l1_user = [u for tag, u in llm.calls if tag == "L1"][-1]
    assert "## name" in l1_user and "## role" in l1_user
    assert l1_user.index("## name") < l1_user.index("## role")
    assert "名字是李龙飞" in l1_user and "角色是架构师" in l1_user
    assert "L1:" in ob and "L0:" in ab


@pytest.mark.asyncio
async def test_bottom_up_deep_before_shallow_parent_sees_child_l0(tmp_path):
    fs = _fs(tmp_path)
    llm = _RoutingLLM()
    gen = MemoryGen(llm)
    # 深层文件：ke://u/7/global/identity/name.md
    # 祖先目录（深→浅）：.../global/identity → .../global → ke://u/7
    uri = "ke://u/7/global/identity/name.md"
    await fs.write(uri, _mem("identity", "名字是李龙飞"))

    await gen.regenerate(fs, [uri])

    # 三级目录各有 L0+L1
    for d in ("ke://u/7/global/identity",
              "ke://u/7/global",
              "ke://u/7"):
        assert await fs.exists(d + "/.abstract.md"), d
        assert await fs.exists(d + "/.overview.md"), d

    # 自底向上：identity 目录的 L0/L1 调用，必早于 global，更早于 ke://u/7
    l1_users = [u for tag, u in llm.calls if tag == "L1"]
    # 第 1 个 L1 = identity（其 user 含子文件 name 的 L0 回显）
    assert "名字是李龙飞" in l1_users[0]
    # global 的 L1（第 2 个）user 含 identity 目录的 L0 回显（"L0:" 前缀）
    assert "L0:" in l1_users[1]
    # ke://u/7 的 L1（第 3 个）user 含 global 目录 L0 回显
    assert "L0:" in l1_users[2]
    assert len(l1_users) == 3


@pytest.mark.asyncio
async def test_dir_idempotent_then_only_changed_chain_regenerates(tmp_path):
    fs = _fs(tmp_path)
    llm = _RoutingLLM()
    gen = MemoryGen(llm)
    a = "ke://u/7/global/identity/name.md"
    b = "ke://u/7/global/preference/lang.md"
    await fs.write(a, _mem("identity", "名字是李龙飞"))
    await fs.write(b, _mem("preference", "偏好中文"))
    await gen.regenerate(fs, [a, b])
    calls_after_first = len(llm.calls)

    # 完全相同输入再跑：哈希全命中，零新增 LLM 调用、文件不变
    snap = {
        p: await fs.read(p)
        for p in ("ke://u/7/global/identity/.overview.md",
                  "ke://u/7/global/.overview.md",
                  "ke://u/7/.overview.md")
    }
    await gen.regenerate(fs, [a, b])
    assert len(llm.calls) == calls_after_first
    for p, v in snap.items():
        assert await fs.read(p) == v

    # 只改 identity 链路：仅 identity 与其祖先（global、ke://u/7）重生；
    # preference 目录 .overview.md 不变（inputs_hash 未变）
    pref_before = await fs.read("ke://u/7/global/preference/.overview.md")
    await fs.write(a, _mem("identity", "改名为王山河"))
    await gen.regenerate(fs, [a])
    assert len(llm.calls) > calls_after_first
    assert await fs.read(
        "ke://u/7/global/preference/.overview.md") == pref_before
    nm, _ = _split_frontmatter(
        await fs.read("ke://u/7/global/identity/.abstract.md"))
    # identity 的 inputs_hash 变了（子 name.md 的 L0 变了）
    assert nm["inputs_hash"] == _sha256_hex(
        "## name\n" + _split_frontmatter(
            await fs.read("ke://u/7/global/identity/name.abstract.md"))[1].strip())


@pytest.mark.asyncio
async def test_dir_llm_error_isolated_other_dirs_still_generated(tmp_path, caplog):
    fs = _fs(tmp_path)

    class _OnlyDirBoom:
        """文件 L0 正常；目录 L0/L1 第一次调用抛错（验证目录层失败隔离）。"""
        async def complete(self, *, system: str, user: str, **kw) -> str:
            # 目录聚合输入恒以 "## " 开头（"## {子项名}\n..."）；
            # 据此区分文件 L0（记忆正文）与目录 L0/L1（聚合输入）
            if user.startswith("## "):
                # 首个目录调用抛错，验证被隔离且不连累后续
                if not getattr(self, "_boomed", False):
                    self._boomed = True
                    raise RuntimeError("dir boom")
            return "OK"

    gen = MemoryGen(_OnlyDirBoom())
    uri = "ke://u/7/global/identity/name.md"
    await fs.write(uri, _mem("identity", "名字是李龙飞"))

    import logging
    with caplog.at_level(logging.DEBUG, logger="src.service.memory.memgen"):
        await gen.regenerate(fs, [uri])

    # 文件 L0 成功（_OnlyDirBoom 对非 "## " 输入返回 OK）
    assert await fs.exists("ke://u/7/global/identity/name.abstract.md")
    # identity 目录首调抛错被隔离、记 debug；regenerate 未抛出
    assert any("dir L0/L1 failed" in r.message for r in caplog.records)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_memgen.py -q -k "multi_files or bottom_up or only_changed_chain or dir_llm_error"`
Expected: FAIL — 目录 `.abstract.md`/`.overview.md` 未生成（步骤② 仍为 `pass`）。

- [ ] **Step 3: 实现步骤②（祖先目录 + 深度降序 + 目录 L0/L1 + inputs_hash 幂等 + 失败隔离）**

在 `memgen.py` 中：① 把 `regenerate` 步骤② 的 `# ② 目录 L0/L1：Task 3 实现` + `pass` 两行替换为下面的循环；② 在类内（`_gen_file_l0` 之后）追加 `_ancestor_dirs`、`_gen_dir_l0_l1`、`_stale`。

把：

```python
        # ② 目录 L0/L1：Task 3 实现
        pass
```

替换为：

```python
        # ② 祖先目录：按路径深度降序（深先浅后），逐级 L0+L1（§3.3）
        for dir_uri in self._ancestor_dirs(files):
            try:
                await self._gen_dir_l0_l1(fs, dir_uri)
            except Exception as exc:           # noqa: BLE001 单条目隔离
                _log.debug("regenerate: dir L0/L1 failed %r: %r",
                           dir_uri, exc)
```

在 `_gen_file_l0` 方法之后追加：

```python
    # ── 祖先目录收集 ──────────────────────────────────────────────
    @staticmethod
    def _ancestor_dirs(file_uris: list[str]) -> list[str]:
        """变更文件的祖先目录集合，按深度降序（深先浅后；同深按 uri 字典序）。

        file uri = ``ke://u/{uid}/m1/.../name.md`` → 目录依次为
        ``ke://u/{uid}/m1/.../m_k``（k=层数..0；k=0 即租户根
        ``ke://u/{uid}``，S1 隔离根，不再上溯到 ``ke://u``）。子目录 L0
        必须先于父目录生成 → 故按深度降序。
        """
        dirs: set[str] = set()
        prefix = "ke://u/"
        for uri in file_uris:
            head, _, _name = uri.rpartition("/")   # 去掉末段 name.md
            if not head.startswith(prefix):
                continue
            rest = head[len(prefix):]              # "{uid}[/m1/...]"
            segs = rest.split("/")                  # ["{uid}", "m1", ...]
            uid = segs[0]
            mids = segs[1:]                          # 目录层（不含 uid）
            # k 从全长到 0：逐级父目录，含租户根（mids[:0] → ke://u/{uid}）
            for k in range(len(mids), -1, -1):
                d = prefix + uid
                if mids[:k]:
                    d += "/" + "/".join(mids[:k])
                dirs.add(d)
        # 深度 = uri 中 '/' 计数（越深越先）；同深按 uri 升序保证可测/确定
        return sorted(dirs, key=lambda d: (-d.count("/"), d))

    # ── 步骤②：目录 L0(.abstract.md) + L1(.overview.md) ────────────
    async def _gen_dir_l0_l1(self, fs: MemoryFS, dir_uri: str) -> None:
        """聚合该目录「直接子项 L0」→ 生成目录 .abstract.md(L0)+
        .overview.md(L1)。直接子项 L0 = 子记忆文件 ``{slug}.abstract.md``
        ∪ 子目录 ``.abstract.md``（§3.3）。二者输入同 → 同一 inputs_hash；
        各自 frontmatter 内 inputs_hash 命中则跳过（零 LLM）。
        """
        try:
            entries = await fs.ls(dir_uri)         # 已 sorted；不存在→MemoryNotFound
        except MemoryNotFound:
            _log.debug("dir gone, skip %r", dir_uri)
            return
        # 收集 (子项名, 子 L0 正文) —— 子项名用于排序与导航图分节标题
        pairs: list[tuple[str, str]] = []
        for name in entries:                        # entries 已排序
            if name in (_ABSTRACT_SUFFIX, _OVERVIEW_NAME):
                continue                            # 本目录自身 L0/L1
            if name.endswith(_ABSTRACT_SUFFIX):
                continue                            # 子文件 L0，经其 .md 计入，勿重复
            if name.endswith(_MD_SUFFIX):
                # 子记忆文件 {slug}.md → 其 L0 = {slug}.abstract.md
                key = name[: -len(_MD_SUFFIX)]
                child_l0 = f"{dir_uri}/{key}{_ABSTRACT_SUFFIX}"
            else:
                # 否则视为子目录（路径 scheme §3.2：非 .md 段即目录）
                key = name
                child_l0 = f"{dir_uri}/{name}/{_ABSTRACT_SUFFIX}"
            try:
                _m, child_body = _split_frontmatter(await fs.read(child_l0))
            except MemoryNotFound:
                # 子 L0 缺失（其生成失败/未就绪）→ 本轮略过该子项；
                # 下一轮其 L0 就绪后 inputs_hash 变 → 自动重生（自愈，§3.3）
                _log.debug("child L0 missing, omit %r", child_l0)
                continue
            pairs.append((key, child_body.strip()))
        if not pairs:
            _log.debug("no child L0, skip dir %r", dir_uri)
            return
        # 按子项名排序后拼接：确定性 → 同输入恒同 inputs_hash（§3.3）
        pairs.sort(key=lambda kv: kv[0])
        joined = "\n\n".join(f"## {k}\n{v}" for k, v in pairs)
        inputs_hash = _sha256_hex(joined)
        abs_uri = f"{dir_uri}/{_ABSTRACT_SUFFIX}"
        ovr_uri = f"{dir_uri}/{_OVERVIEW_NAME}"
        need_abs = await self._stale(fs, abs_uri, inputs_hash)
        need_ovr = await self._stale(fs, ovr_uri, inputs_hash)
        if need_abs:
            a = await self._llm.complete(system=_MEM_L0_SYSTEM, user=joined)
            await fs.write(abs_uri, _render_frontmatter(
                {"inputs_hash": inputs_hash}, a.strip() + "\n"))
        if need_ovr:
            o = await self._llm.complete(system=_MEM_L1_SYSTEM, user=joined)
            await fs.write(ovr_uri, _render_frontmatter(
                {"inputs_hash": inputs_hash}, o.strip() + "\n"))
        if not need_abs and not need_ovr:
            _log.debug("dir L0/L1 hash hit, skip %r", dir_uri)

    @staticmethod
    async def _stale(fs: MemoryFS, uri: str, want_hash: str) -> bool:
        """目标不存在、或其 frontmatter ``inputs_hash`` ≠ want → 需重生。"""
        if not await fs.exists(uri):
            return True
        meta, _ = _split_frontmatter(await fs.read(uri))
        # str() 防御 YAML 把全数字 hash 解析成 int；缺键 str(None)!=hash → 重生
        return str(meta.get("inputs_hash")) != want_hash
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_memgen.py -q`
Expected: PASS（Task1 4 + Task2 5 + Task3 4 = 13 passed）。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add src/service/memory/memgen.py tests/test_auth/test_memory_memgen.py && git commit -m "$(cat <<'EOF'
feat(memory-s2): 目录 L0+L1（步骤②）+ 祖先深度降序 + inputs_hash 幂等

_ancestor_dirs 收集变更文件祖先目录并按路径深度降序（深先浅后，确保
子目录 L0 先于父目录）；_gen_dir_l0_l1 聚合「直接子项 L0」生成目录
.abstract.md(L0)+.overview.md(L1)，同一 inputs_hash，_stale 命中跳过；
单目录失败 _log.debug 隔离。设计 §3.3/§3.4/§3.5/§3.7②③⑤⑦。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" && git status
```

---

### Task 4: 整树自愈（S6 场景）+ 端到端幂等 + 回归

**Files:**
- Modify: `tests/test_auth/test_memory_memgen.py`（追加 §3.7 ⑥ S6 整树自愈、端到端零增量 LLM）
- 无源码改动（验证 Task 1–3 的算法对「整树一次性传入」即自愈，§3.3）
- Test: `tests/test_auth/test_memory_memgen.py` + 既有 `tests/test_auth` 回归（§3.7 ⑧）

- [ ] **Step 1: 追加 S6 整树自愈 + 端到端幂等测试**

在 `tests/test_auth/test_memory_memgen.py` 末尾追加：

```python
# ── Task 4：S6 整树自愈 + 端到端幂等（§3.7⑥④）───────────────────
@pytest.mark.asyncio
async def test_s6_whole_tree_one_regenerate_self_heals(tmp_path):
    """模拟 S6 迁移：直接经 MemoryFS 批量写一棵树（无 L0/L1），
    调一次 regenerate(整树 uri 列表) → 全部 L0/L1 正确生成。
    """
    fs = _fs(tmp_path)
    llm = _RoutingLLM()
    gen = MemoryGen(llm)
    tree = {
        "ke://u/9/global/identity/name.md": _mem("identity", "名字是李龙飞"),
        "ke://u/9/global/identity/alias.md": _mem("identity", "别名老李"),
        "ke://u/9/global/preference/lang.md": _mem("preference", "偏好中文"),
        "ke://u/9/project/p1/preference/scope.md":
            _mem("preference", "只看支付域"),
    }
    for uri, content in tree.items():
        await fs.write(uri, content)

    await gen.regenerate(fs, list(tree.keys()))

    # 每个记忆文件 L0
    for uri in tree:
        assert await fs.exists(uri[: -len(".md")] + ".abstract.md"), uri
    # 每个被触及目录 L0+L1（含租户根 ke://u/9）
    for d in (
        "ke://u/9/global/identity",
        "ke://u/9/global/preference",
        "ke://u/9/global",
        "ke://u/9/project/p1/preference",
        "ke://u/9/project/p1",
        "ke://u/9/project",
        "ke://u/9",
    ):
        assert await fs.exists(d + "/.abstract.md"), d
        assert await fs.exists(d + "/.overview.md"), d
    # 自底向上一致性：租户根 .overview.md 的 inputs_hash 与其 .abstract.md 相同
    am, _ = _split_frontmatter(await fs.read("ke://u/9/.abstract.md"))
    om, _ = _split_frontmatter(await fs.read("ke://u/9/.overview.md"))
    assert am["inputs_hash"] == om["inputs_hash"]


@pytest.mark.asyncio
async def test_end_to_end_idempotent_zero_incremental_llm(tmp_path):
    """整树跑两次：第二次哈希全命中 → fake LLM 零新增调用、全部文件逐字节不变。"""
    fs = _fs(tmp_path)
    llm = _RoutingLLM()
    gen = MemoryGen(llm)
    tree = {
        "ke://u/9/global/identity/name.md": _mem("identity", "名字是李龙飞"),
        "ke://u/9/global/preference/lang.md": _mem("preference", "偏好中文"),
    }
    for uri, content in tree.items():
        await fs.write(uri, content)
    await gen.regenerate(fs, list(tree.keys()))
    calls_1 = len(llm.calls)
    assert calls_1 > 0

    # 收集全部生成文件快照
    def _all_gen_uris():
        return [
            "ke://u/9/global/identity/name.abstract.md",
            "ke://u/9/global/preference/lang.abstract.md",
            "ke://u/9/global/identity/.abstract.md",
            "ke://u/9/global/identity/.overview.md",
            "ke://u/9/global/preference/.abstract.md",
            "ke://u/9/global/preference/.overview.md",
            "ke://u/9/global/.abstract.md",
            "ke://u/9/global/.overview.md",
            "ke://u/9/.abstract.md",
            "ke://u/9/.overview.md",
        ]
    snap = {p: await fs.read(p) for p in _all_gen_uris()}

    await gen.regenerate(fs, list(tree.keys()))
    assert len(llm.calls) == calls_1               # 零增量 LLM 调用
    for p, v in snap.items():
        assert await fs.read(p) == v               # 逐字节不变
```

- [ ] **Step 2: 跑 S2 全套确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth/test_memory_memgen.py -q`
Expected: PASS（13 + 2 = 15 passed）。

- [ ] **Step 3: 既有套件回归（§3.7 ⑧ — vfs / memory / qa / prompt / chitchat / sse 不回归）**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth -q -k "memory or qa or prompt or chitchat or sse or vfs"`
Expected: PASS，0 failed（S2 纯新增 `memgen.py` + 测试 + prompts.py 追加常量，未改既有逻辑；S1 vfs 48 条仍全过）。若任一既有用例 FAIL → 用 superpowers:systematic-debugging 定位根因（先确认是否 S2 引入），不得绕过。

- [ ] **Step 4: import 全链路自检**

Run: `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "import src.service.qa_router; from src.service.memory.memgen import MemoryGen; print('import ok')"`
Expected: 输出 `import ok`（确认追加常量未破坏既有 `prompts.py` 消费方）。

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth && git add tests/test_auth/test_memory_memgen.py && git commit -m "$(cat <<'EOF'
test(memory-s2): S6 整树自愈 + 端到端幂等（零增量 LLM）+ 回归校验

§3.7⑥：直接经 MemoryFS 批量写一树、调一次 regenerate(整树) → 全部
L0/L1 自愈生成；§3.7④端到端：再跑一次哈希全命中、fake LLM 零增量、
文件逐字节不变。§3.7⑧既有 vfs/memory/qa 套件回归通过。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" && git status
```

---

## 实现者备注（避免常见返工）

1. **测试解释器**：homebrew `python3` 无 `pytest-asyncio`；必须用仓库 venv：`./venv/bin/python -m pytest ...`（已验证 S1 48/48、`pytest_asyncio 1.3.0`、strict 模式需 `@pytest.mark.asyncio`）。
2. **导入路径**：`from src.service.memory.vfs import MemoryFS, MemoryNotFound`、`from src.service.qa_engine.prompts import _MEM_L0_SYSTEM, _MEM_L1_SYSTEM`（与既有 `service.py:17` 同风格；`pyproject` `pythonpath=["src"]` 且测试用 `src.` 前缀）。
3. **不改既有文件逻辑**：`prompts.py` 仅在末尾**追加**两常量，不动既有任何常量/函数（回归零风险来源）。
4. **`src_hash` 只哈希正文**：记忆 `.md` 的 frontmatter（kind/slug/时间戳，S4/S6 维护）变化**不**应使 L0 失效 → `_gen_file_l0` 用 `_split_frontmatter` 取 body 再哈希（§3.3 明确「src_hash = 该 .md 正文」）。
5. **目录直接子项分类靠路径 scheme**（§3.2）：entry 以 `.md` 结尾且非 `.abstract.md`/`.overview.md` = 记忆文件；`.abstract.md`/`.overview.md` = 生成物（跳过）；其余 = 子目录。无需 `os`/`isdir`，全程走 S1 `fs` 抽象。
6. **PyYAML 已是声明依赖**（`pyproject.toml:14 "pyyaml>=6.0"`）——用 `yaml.safe_load`/`yaml.safe_dump`，**不**手写 frontmatter 解析器、不引新依赖。`str(meta.get(...))` 防御 YAML 把极端全数字 hash 解析成 int。
7. **失败语义**：`MemoryGen` 自身清晰抛错（`fs.read` 不存在抛 `MemoryNotFound` 等）；`regenerate` 内对**每个目标**包 `try/except Exception` 记 `_log.debug` 续跑（§3.5 单条目隔离）。「记忆失败绝不影响主答」的外层 try 由 S4/S6 调用点持有（**本计划不做**，§3.6）。
8. **S2 不碰 router/SSE、不自起任务**（§3.4）：`regenerate` 是被 await 的协程，调度/异步点由 S4/S6 持有。

## 自检（writing-plans Self-Review，写计划者已过）

- **Spec 覆盖**：§3.7 ①→T2、②→T3、③→T3、④→T2(文件)+T4(端到端)、⑤→T3、⑥→T4、⑦→T2(文件)+T3(目录)、⑧→T4 Step3；§3.1 形态（`__init__(llm)`/`regenerate(fs,changed_uris)`）→T1/T2；§3.3 两哈希字段+自底向上→T2/T3；§3.5 两 prompt 常量+失败隔离→T1/T2/T3；§3.6 边界→「不做」表 + 备注 7/8。无遗漏。
- **占位扫描**：自检发现 T3 Step1 初版含 `caplog_ctx` 中间占位 + Step1b 替换桥段（违反 no-placeholder）→ 已就地修正：T3 Step1 直接给出 `test_dir_llm_error_isolated_other_dirs_still_generated(tmp_path, caplog)` 最终完整代码（用 pytest 内建 `caplog` fixture + `caplog.at_level`），删除 Step1b。复扫全文：无 TBD/TODO/「类似 TaskN」/裸描述步骤；每个改码步骤均附完整代码块、确切 `./venv/bin/python -m pytest ...` 命令与期望输出。
- **类型一致性**：`MemoryGen.__init__(self, llm)` / `regenerate(self, fs: MemoryFS, changed_uris: list[str]) -> None` 贯穿一致；辅助 `_is_memory_file`/`_ancestor_dirs`/`_gen_file_l0`/`_gen_dir_l0_l1`/`_stale` 命名跨任务一致；常量 `_ABSTRACT_SUFFIX`/`_OVERVIEW_NAME`/`_MD_SUFFIX`、`_MEM_L0_SYSTEM`/`_MEM_L1_SYSTEM` 全程同名。fake LLM 一律 `async def complete(self, *, system, user, **kw) -> str`。