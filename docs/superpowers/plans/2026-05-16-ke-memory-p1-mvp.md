# KE 记忆系统 P1 MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 KE 加用户级 + 会话级记忆（纯软笔记、全量注入、显式写入触发、会话级固定 N 轮压缩），零新基建（不碰 Weaviate/Neo4j）。

**Architecture:** 新增 `src/service/memory/` 纯服务模块（recall/detect/write/compact，可用 Fake 单测）；2 张 MySQL 表加进 `db_models_homepage.py`（Alembic env.py 已 import 此文件，自动注册）；召回在 `qa_router` 进 `stream_qa_answer` 前拼成 `memory_block`，经 `sse_emitter` → `synthesizer` 注入 system prompt 顶部；写入用 `_make_memory_writer` 工厂回调，完全镜像已验证的 `_make_title_generator` / `on_title` 模式（done 后异步执行、自 commit、失败静默）。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy 2.0 async / Alembic / pytest（`pytest.mark.asyncio` + Fake DB/LLM，不起真 DB）

**Spec:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/记忆系统-设计.md`（§4.1 用户级、§4.3 会话级、§6 写入、§7 召回、§12 P1 范围）

**Repo:** `/Users/java/knowledge-engineering-auth`（分支 `release-0513`；venv 在 `venv/`）

**P1 范围边界（YAGNI）：** 不做工程级（P2）、不做 grounding/entity_id（P3）、不做异步抽取/污染门控（P2）、不做晋升/毕业/管理 UI（P3）、不发 `memory_written` SSE 事件（spec §10 标可选，P1 略）。显式写入只落 **用户级**（会话级是自动压缩产物，非显式）。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/service/db_models_homepage.py` | 首页/QA ORM 模型 | 加 `QAUserMemory` + `QASessionMemory` 两个 model |
| `alembic/versions/qa_memory_p1_v1.py` | 迁移 | 🆕 建 2 表，`down_revision="session_title_custom_v1"` |
| `src/service/qa_engine/prompts.py` | 所有 prompt 文本 | 加 `with_memory_block()` 纯函数 + `_SESSION_COMPACT_SYSTEM` 常量 |
| `src/service/memory/__init__.py` | 记忆服务包 | 🆕 空包标记 |
| `src/service/memory/service.py` | 记忆 读/检测/写/压缩（纯逻辑，无 HTTP） | 🆕 `recall_memory_block` / `detect_explicit_memory` / `write_explicit_memory` / `maybe_compact_session` |
| `src/service/qa_engine/synthesizer.py` | LLM 合成 | 4 个 LLM 调用路径加 `memory_block` 参数，经 `with_memory_block` 注入 system |
| `src/service/qa_engine/sse_emitter.py` | SSE 流 | `stream_qa_answer` 加 `memory_block` 参数 + `on_memory` 回调（done 后调，镜像 on_title） |
| `src/service/qa_router.py` | QA 路由 | 进流前调 `recall_memory_block`；加 `_make_memory_writer` 工厂；接线两个新 kwarg |
| `tests/test_auth/test_models_memory.py` | 模型 metadata 契约 | 🆕（镜像 `test_models_homepage.py`） |
| `tests/test_auth/test_memory_prompt.py` | `with_memory_block` 契约 | 🆕 |
| `tests/test_auth/test_memory_service.py` | 服务逻辑（Fake DB/LLM） | 🆕 |
| `tests/test_auth/test_memory_router_hook.py` | `_make_memory_writer` 工厂（Fake，镜像 `test_qa_session_title.py`） | 🆕 |

**约定基线（已核对真实代码）：**
- 模型风格：SQLAlchemy 2.0 `Mapped` / `mapped_column`，`from src.service.db import Base`，枚举用 `String(n)`+docstring（代码库无 `sa.Enum` 用法），FK `ondelete` 显式写。
- `QASession.user_id` 故意**不加 FK**（保留已删用户历史）→ `qa_user_memory.user_id` 同样不加 FK。
- `QAMessage.session_id` 有 `ForeignKey("qa_sessions.id", ondelete="CASCADE")` → `qa_session_memory.session_id` 同样 CASCADE。
- 测试惯例：`test_models_homepage.py` 用 `cols == {...}` 全等断言列集 + 断言 index/PK/FK；异步回调测试用手写 `_FakeDB/_FakeLLM`（见 `test_qa_session_title.py`），不起真 engine。
- 跑测试命令前缀：`cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest <…> -q`
- Alembic 当前 head = `session_title_custom_v1`。

---

## Task 1: 记忆 ORM 模型（2 表）

**Files:**
- Modify: `src/service/db_models_homepage.py`（在文件末尾、`QAFeedback` 之后追加）
- Test: `tests/test_auth/test_models_memory.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_models_memory.py`：

```python
"""验证 P1 记忆系统 2 张 ORM 表的 metadata 契约。

跟 test_models_homepage.py 一致：只断言 table_name / columns / PK / FK / index，
不起真 DB engine。
设计：[[记忆系统-设计]] §5（P1 仅 qa_user_memory + qa_session_memory）
"""
from src.service.db_models_homepage import QAUserMemory, QASessionMemory


# ───────── qa_user_memory ─────────

def test_user_memory_table_name():
    assert QAUserMemory.__tablename__ == "qa_user_memory"


def test_user_memory_columns():
    cols = {c.name for c in QAUserMemory.__table__.columns}
    assert cols == {
        "id", "user_id", "kind", "content", "source",
        "source_session_id", "status", "created_at", "updated_at",
    }


def test_user_memory_pk_is_id():
    cols = {c.name: c for c in QAUserMemory.__table__.columns}
    assert cols["id"].primary_key is True


def test_user_memory_no_user_fk():
    # 与 QASession.user_id 一致：故意不加 FK，保留已删用户的记忆。
    # 强断言『零 FK』（QAUserMemory 设计上无任何外键），避免 all() 空集合真空通过。
    fks = list(QAUserMemory.__table__.foreign_keys)
    assert fks == []


def test_user_memory_has_lookup_index():
    index_names = {idx.name for idx in QAUserMemory.__table__.indexes}
    assert "idx_qa_user_memory_user_active" in index_names


# ───────── qa_session_memory ─────────

def test_session_memory_table_name():
    assert QASessionMemory.__tablename__ == "qa_session_memory"


def test_session_memory_columns():
    cols = {c.name for c in QASessionMemory.__table__.columns}
    assert cols == {
        "id", "session_id", "working_summary",
        "focus_entity_ids", "turn_count", "updated_at",
    }


def test_session_memory_session_id_unique():
    cols = {c.name: c for c in QASessionMemory.__table__.columns}
    assert cols["session_id"].unique is True


def test_session_memory_has_session_cascade_fk():
    fks = list(QASessionMemory.__table__.foreign_keys)
    sess_fks = [fk for fk in fks if fk.column.table.name == "qa_sessions"]
    assert len(sess_fks) == 1
    assert sess_fks[0].ondelete == "CASCADE"
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_models_memory.py -q`
Expected: FAIL —— `ImportError: cannot import name 'QAUserMemory' from 'src.service.db_models_homepage'`

- [ ] **Step 3: 加两个 model**

在 `src/service/db_models_homepage.py` 顶部 import 段，把 `BigInteger` 加进现有 `from sqlalchemy import (...)`（该 import 块当前已含 `Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, func, text`）：

```python
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
    text,
)
```

在文件**末尾**（`QAFeedback` 类之后）追加：

```python
# ─── 6. qa_user_memory（记忆系统 P1：用户级软笔记）───────────────────────────
# 设计：[[记忆系统-设计]] §4.1。跨工程，量小，召回时全量注入 system prompt。

class QAUserMemory(Base):
    """用户级记忆：偏好 / 身份 / 风格反馈。跨所有工程，纯软笔记。"""
    __tablename__ = "qa_user_memory"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    """归属用户（对应 users.id）。与 QASession 一致不加 FK：保留已删用户的记忆。"""

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    """preference / identity / style_feedback（沿用代码库 String+约定，不用 sa.Enum；
    String(32) 留余量：'style_feedback' 已 14 字，16 无余量，schema 改动有数据后昂贵）。"""

    content: Mapped[str] = mapped_column(Text, nullable=False)
    """自然语言软笔记，如『回答尽量简短』。"""

    source: Mapped[str] = mapped_column(String(32), nullable=False, default="explicit")
    """explicit（用户显式『记住…』）/ extracted（P2 异步抽取，P1 不产出）。"""

    source_session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    """来源会话 ID（可追溯，不加 FK 硬绑）。"""

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    """active / archived（软删；遵守工程宪法禁物理删）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # 召回主查询：where user_id + status='active'
        Index("idx_qa_user_memory_user_active", "user_id", "status"),
    )


# ─── 7. qa_session_memory（记忆系统 P1：会话级工作状态）──────────────────────
# 设计：[[记忆系统-设计]] §4.3。一会话一行，滚动覆盖压缩摘要。

class QASessionMemory(Base):
    """会话级记忆：压缩后的工作状态。一对一绑定 QASession，覆盖式更新。"""
    __tablename__ = "qa_session_memory"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("qa_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    """绑定的会话。删会话级联删其记忆。unique 保证一会话一行。"""

    working_summary: Mapped[str] = mapped_column(Text, nullable=False)
    """压缩后的工作状态（本次目标 / 已确认 / 已排除）。"""

    focus_entity_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    """当前聚焦的 entity_id 列表（P1 可留空，为 P2/P3 预留）。"""

    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """上次压缩时的 message_count（用于判断是否需要再压缩）。"""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
```

- [ ] **Step 4: 跑测试，确认全过**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_models_memory.py -q`
Expected: 8 passed

- [ ] **Step 5: 回归——确认没破坏既有模型测试**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_models_homepage.py -q`
Expected: 全 passed（只追加新类，未动既有 5 表）

- [ ] **Step 6: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/db_models_homepage.py tests/test_auth/test_models_memory.py
git commit -m "feat(memory): P1 ORM — qa_user_memory + qa_session_memory 两表（TDD 8 测试）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Alembic 迁移（2 表）

**Files:**
- Create: `alembic/versions/qa_memory_p1_v1.py`

- [ ] **Step 1: 写迁移文件**

新建 `alembic/versions/qa_memory_p1_v1.py`（镜像 `session_title_custom_v1.py` 风格）：

```python
"""记忆系统 P1：建 qa_user_memory + qa_session_memory 两表

Revision ID: qa_memory_p1_v1
Revises: session_title_custom_v1
Create Date: 2026-05-16

设计：[[记忆系统-设计]] §5（P1 仅这两表，工程级 P2 再加）
"""
from alembic import op
import sqlalchemy as sa

# Alembic 版本标识
revision = "qa_memory_p1_v1"
down_revision = "session_title_custom_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qa_user_memory",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default=sa.text("'explicit'")),
        sa.Column("source_session_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "idx_qa_user_memory_user_active", "qa_user_memory", ["user_id", "status"]
    )
    op.create_table(
        "qa_session_memory",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(64), nullable=False, unique=True),
        sa.Column("working_summary", sa.Text(), nullable=False),
        sa.Column("focus_entity_ids", sa.JSON(), nullable=True),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["qa_sessions.id"], ondelete="CASCADE"
        ),
    )


def downgrade() -> None:
    op.drop_table("qa_session_memory")
    op.drop_index("idx_qa_user_memory_user_active", table_name="qa_user_memory")
    op.drop_table("qa_user_memory")
```

- [ ] **Step 2: 校验迁移链单 head（离线，不连生产库）**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m alembic heads`
Expected: 只打印一行 `qa_memory_p1_v1 (head)`（若打印多行=分叉，需修 down_revision）

- [ ] **Step 3: 校验脚本可被 alembic 解析**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m alembic history | head -3`
Expected: 顶部出现 `session_title_custom_v1 -> qa_memory_p1_v1 (head), 记忆系统 P1...`，无 traceback

- [ ] **Step 4: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add alembic/versions/qa_memory_p1_v1.py
git commit -m "feat(memory): P1 Alembic 迁移 qa_memory_p1_v1（建 2 表，head 接 session_title_custom_v1）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

> ⚠️ **不在本计划内对生产库跑 `alembic upgrade head`**：生产迁移由用户手动执行（与本仓既往约定一致——迁移命令会被 Claude Code 分类器拦截）。Phase DoD 会提示用户自行升级。

---

## Task 3: prompts.py —— `with_memory_block` 纯函数 + 压缩 prompt

**Files:**
- Modify: `src/service/qa_engine/prompts.py`（文件末尾追加）
- Test: `tests/test_auth/test_memory_prompt.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_memory_prompt.py`：

```python
"""with_memory_block 纯函数契约 + 会话压缩 prompt 存在性。
设计：[[记忆系统-设计]] §7（记忆块注入 system 顶部）
"""
from src.service.qa_engine.prompts import (
    with_memory_block,
    _SESSION_COMPACT_SYSTEM,
)

BASE = "你是企业代码知识分析师。"


def test_none_block_returns_system_unchanged():
    assert with_memory_block(BASE, None) == BASE


def test_empty_block_returns_system_unchanged():
    assert with_memory_block(BASE, "   ") == BASE


def test_block_prepended_with_delimiter_and_system_kept():
    out = with_memory_block(BASE, "用户偏好：回答简短")
    assert "用户偏好：回答简短" in out
    assert BASE in out
    # 记忆块在 system 之前（注入顶部，优先级最高）
    assert out.index("用户偏好：回答简短") < out.index(BASE)
    # 有明确分隔标记，避免与正文混淆
    assert "记忆" in out


def test_compact_system_prompt_exists_and_nonempty():
    assert isinstance(_SESSION_COMPACT_SYSTEM, str)
    assert len(_SESSION_COMPACT_SYSTEM) > 20
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_prompt.py -q`
Expected: FAIL —— `ImportError: cannot import name 'with_memory_block'`

- [ ] **Step 3: 实现**

在 `src/service/qa_engine/prompts.py` **末尾**（`_TITLE_SUMMARY_SYSTEM` 之后）追加：

```python
# ─── 记忆系统 P1（2026-05-16）──────────────────────────────────────────────
# 设计：[[记忆系统-设计]] §7。记忆块注入 system prompt 顶部（优先级最高，
# 早于角色与规则），让模型先读「人类真相」再按既有规则作答。

_MEMORY_BLOCK_TEMPLATE = (
    "═══════ 记忆（关于本用户 / 本次会话的已知事实，优先参考）═══════\n"
    "{block}\n"
    "═══════════════════════════════════════════════════════════════\n\n"
)


def with_memory_block(system: str, memory_block: str | None) -> str:
    """把召回的记忆块拼到 system prompt 最前面。

    memory_block 为 None / 全空白 → 原样返回 system（零开销、行为不变）。
    """
    if not memory_block or not memory_block.strip():
        return system
    return _MEMORY_BLOCK_TEMPLATE.format(block=memory_block.strip()) + system


# 会话级压缩：把最近若干轮对话压成一段「工作状态」（本次目标/已确认/已排除）。
# 设计：[[记忆系统-设计]] §4.3 会话级。
_SESSION_COMPACT_SYSTEM = (
    "你是会话工作状态压缩器。基于给定的多轮问答，用中文输出一段不超过 150 字的"
    "「当前工作状态」概括，只保留对后续追问有用的信息：本次会话目标、已确认的结论、"
    "已排除的方向、当前聚焦点。直接输出概括正文，不要前缀、不要解释、不要分点编号。"
)
```

- [ ] **Step 4: 跑测试，确认全过**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_prompt.py -q`
Expected: 4 passed

- [ ] **Step 5: 回归 prompt 既有测试**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_prompts.py tests/test_auth/test_chit_chat_prompt.py -q`
Expected: 全 passed（只追加，未改既有常量/函数）

- [ ] **Step 6: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/prompts.py tests/test_auth/test_memory_prompt.py
git commit -m "feat(memory): P1 with_memory_block 注入器 + 会话压缩 prompt（TDD 4 测试）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: synthesizer.py —— 4 个 LLM 路径透传 `memory_block`

**Files:**
- Modify: `src/service/qa_engine/synthesizer.py`
- Test: `tests/test_auth/test_memory_service.py`（本任务先建该文件并放 synthesizer 注入测试；Task 6 再往同文件加 service 测试）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_memory_service.py`：

```python
"""记忆注入 + 服务逻辑测试（Fake DB/LLM，不起真 engine）。
设计：[[记忆系统-设计]] §6 §7
"""
import pytest

from src.service.qa_engine.synthesizer import QASynthesizer
from src.service.qa_engine.retriever import RetrievedContext


class _CapturingLLM:
    """记录最后一次 complete 的 system 入参。"""
    def __init__(self):
        self.last_system = None

    async def complete(self, *, system: str, user: str, **kw) -> str:
        self.last_system = system
        # 返回最简合法 6 段式，避免解析降级影响断言
        return '```json\n{"sections":[{"type":"overview",' \
               '"title":"t","content":"c","references":[]}]}\n```'


def _ctx(skill_id="architecture"):
    return RetrievedContext(
        question="下单流程怎么走",
        entry_candidates=[],
        callees_by_entry={},
        callers_by_entry={},
        table_access_by_entry={},
        skill_id=skill_id,
    )


@pytest.mark.asyncio
async def test_memory_block_injected_into_system():
    llm = _CapturingLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize(_ctx(), memory_block="用户偏好：只看支付域")
    assert "用户偏好：只看支付域" in llm.last_system
    # 既有 SYSTEM_PROMPT 仍在
    assert "企业代码知识分析师" in llm.last_system


@pytest.mark.asyncio
async def test_no_memory_block_keeps_system_unchanged():
    llm = _CapturingLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize(_ctx())
    assert "记忆（关于本用户" not in llm.last_system


@pytest.mark.asyncio
async def test_memory_block_injected_in_chit_chat_path():
    llm = _CapturingLLM()
    syn = QASynthesizer(llm)
    await syn.synthesize(_ctx(skill_id="chit-chat"), memory_block="用户偏好：用 Java")
    assert "用户偏好：用 Java" in llm.last_system
```

> ⚠️ 实施者：先确认 `RetrievedContext` 的构造参数名。若与上面不符，运行
> `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -c "import inspect,src.service.qa_engine.retriever as r; print(inspect.signature(r.RetrievedContext.__init__))"`
> 按真实签名调整 `_ctx()`（只改测试夹具，不改被测逻辑）。

- [ ] **Step 2: 跑测试，确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
Expected: FAIL —— `TypeError: synthesize() got an unexpected keyword argument 'memory_block'`

- [ ] **Step 3: 改 synthesizer 的 4 个路径**

在 `src/service/qa_engine/synthesizer.py`：

(a) 顶部 import 加 `with_memory_block`：

```python
from src.service.qa_engine.prompts import (
    SYSTEM_PROMPT,
    _CHIT_CHAT_SYSTEM,  # v1.2 chit-chat 专属
    build_user_prompt,
    build_user_prompt_with_history,
    with_memory_block,
)
```

(b) `_synthesize_chit_chat` 签名与调用：

```python
    async def _synthesize_chit_chat(
        self, ctx: RetrievedContext, *, memory_block: str | None = None
    ) -> SynthesizedAnswer:
        """v1.2 chit-chat 闲聊路径：用专属 prompt 调 LLM，返回单段 chit-chat section。
        设计：[[chit-chat-闲聊路径-设计]] §4.3, §4.4；记忆注入见 [[记忆系统-设计]] §7。"""
        reply = await self.llm.complete(
            system=with_memory_block(_CHIT_CHAT_SYSTEM, memory_block),
            user=ctx.question,
        )
```
（函数体其余不变）

(c) `_synthesize_chit_chat_stream` 签名与调用：

```python
    async def _synthesize_chit_chat_stream(
        self,
        ctx: RetrievedContext,
        on_token: Optional[Callable[[str], Awaitable[None]]] = None,
        *,
        memory_block: str | None = None,
    ) -> SynthesizedAnswer:
        """v1.2 chit-chat 流式版：边收 LLM token 边调 on_token。
        设计：[[chit-chat-闲聊路径-设计]] §4.3, §4.6；记忆注入见 [[记忆系统-设计]] §7。"""
        parts: list[str] = []
        async for tok in self.llm.complete_stream(
            system=with_memory_block(_CHIT_CHAT_SYSTEM, memory_block),
            user=ctx.question,
        ):
```
（函数体其余不变）

(d) `synthesize` 签名、chit-chat 分发、两处 `system=SYSTEM_PROMPT`：

```python
    async def synthesize(
        self,
        ctx: RetrievedContext,
        *,
        history: list[dict] | None = None,
        memory_block: str | None = None,
    ) -> SynthesizedAnswer:
```
分发改为：
```python
        if ctx.skill_id == "chit-chat":
            return await self._synthesize_chit_chat(ctx, memory_block=memory_block)
```
LLM 调用改为：
```python
            raw = await self.llm.complete(
                system=with_memory_block(SYSTEM_PROMPT, memory_block),
                user=user_prompt,
            )
```

(e) `synthesize_stream` 签名、chit-chat 分发、`complete_stream` 调用：

```python
    async def synthesize_stream(
        self,
        ctx: RetrievedContext,
        history: list[dict] | None = None,
        on_token: Optional[Callable[[str], Awaitable[None]]] = None,
        *,
        memory_block: str | None = None,
    ) -> SynthesizedAnswer:
```
分发改为：
```python
        if ctx.skill_id == "chit-chat":
            return await self._synthesize_chit_chat_stream(
                ctx, on_token=on_token, memory_block=memory_block
            )
```
流式 LLM 调用改为：
```python
            async for chunk in self.llm.complete_stream(
                system=with_memory_block(SYSTEM_PROMPT, memory_block),
                user=user_prompt,
            ):
```

> 注：`_estimate_tokens(SYSTEM_PROMPT, user_prompt, raw)` 保持不变（粗算，无需算上记忆块，避免影响既有 token 断言）。

- [ ] **Step 4: 跑测试，确认全过**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
Expected: 3 passed

- [ ] **Step 5: 回归 synthesizer 既有测试**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_synthesizer.py tests/test_auth/test_qa_router_chitchat.py -q`
Expected: 全 passed（新参数 default=None，未传时行为与改前完全一致）

- [ ] **Step 6: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/synthesizer.py tests/test_auth/test_memory_service.py
git commit -m "feat(memory): P1 synthesizer 4 路径透传 memory_block（含 chit-chat，TDD 3 测试）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: sse_emitter.py —— `stream_qa_answer` 加 `memory_block` + `on_memory`

**Files:**
- Modify: `src/service/qa_engine/sse_emitter.py`
- Test: `tests/test_auth/test_memory_service.py`（追加一组）

- [ ] **Step 1: 追加失败测试**

在 `tests/test_auth/test_memory_service.py` **末尾**追加：

```python
# ───────── stream_qa_answer 透传 memory_block + on_memory ─────────

from src.service.qa_engine.sse_emitter import stream_qa_answer


class _StubAnswer:
    sections = [{"type": "overview", "title": "t", "content": "c", "references": []}]
    token_usage = 1
    cost_yuan = 0.0
    raw_output = "c"


class _SpySynth:
    """记录 synthesize 收到的 memory_block。无 synthesize_stream → 走非流式兜底。"""
    def __init__(self):
        self.seen_memory_block = "UNSET"

    async def synthesize(self, ctx, *, history=None, memory_block=None):
        self.seen_memory_block = memory_block
        return _StubAnswer()


class _StubRetriever:
    async def retrieve(self, **kw):
        return RetrievedContext(
            question="q", entry_candidates=[], callees_by_entry={},
            callers_by_entry={}, table_access_by_entry={}, skill_id="architecture",
        )


@pytest.mark.asyncio
async def test_stream_passes_memory_block_and_calls_on_memory():
    synth = _SpySynth()
    called = {"on_memory": False}

    async def _on_memory():
        called["on_memory"] = True

    chunks = []
    async for ev in stream_qa_answer(
        question="q", project_id="p1", session_id="s1",
        retriever=_StubRetriever(), synthesizer=synth, router=None,
        memory_block="用户偏好：简短", on_memory=_on_memory,
    ):
        chunks.append(ev)

    assert synth.seen_memory_block == "用户偏好：简短"
    assert called["on_memory"] is True
    assert any("event: done" in c for c in chunks)
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py::test_stream_passes_memory_block_and_calls_on_memory -q`
Expected: FAIL —— `TypeError: stream_qa_answer() got an unexpected keyword argument 'memory_block'`

- [ ] **Step 3: 改 sse_emitter**

在 `src/service/qa_engine/sse_emitter.py`：

(a) 在 `OnTitleCallback` 定义之后加 `OnMemoryCallback`：

```python
# on_memory 回调：done + session_title 之后调用（镜像 on_title 模式）。
# router 用它来：解析显式记忆意图 → 写 qa_user_memory + 视情况压缩会话记忆。
# 返回 None；失败静默（记忆是辅助，绝不影响主答）。
OnMemoryCallback = Callable[
    [],
    Awaitable[None],
]
```

(b) `stream_qa_answer` 签名加两个 kwarg（放在 `on_title` 之后）：

```python
async def stream_qa_answer(
    *,
    question: str,
    project_id: str,
    session_id: str,
    retriever: QARetriever,
    synthesizer: QASynthesizer,
    router: SkillRouter | None = None,
    history: list[dict] | None = None,
    on_complete: OnCompleteCallback | None = None,
    on_title: OnTitleCallback | None = None,
    memory_block: str | None = None,
    on_memory: OnMemoryCallback | None = None,
) -> AsyncIterator[str]:
```

(c) 把 `memory_block` 透传给 3 处 synthesizer 调用：

- 流式分支（`stream_kwargs` 字典构造处）追加一行：
```python
            stream_kwargs: dict[str, Any] = {
                "history": history,
                "on_token": _on_token,
                "memory_block": memory_block,
            }
```
- ReAct 非流式兜底：
```python
            answer = await synthesizer.synthesize(
                ctx, history=history, on_tool_call=_on_tool_call,
                memory_block=memory_block,
            )
```
- 旧 QASynthesizer 兜底：
```python
            answer = await synthesizer.synthesize(
                ctx, history=history, memory_block=memory_block
            )
```

(d) 在文件末尾 `on_title` 处理块**之后**追加 `on_memory` 处理（镜像 on_title）：

```python
    # 9. on_memory（记忆系统 P1，2026-05-16）：done + session_title 之后调。
    # 设计：[[记忆系统-设计]] §6。回调内部自行 commit DB（DB 是 source of truth）；
    # 这里失败（客户端断开 / 写库异常）静默——记忆是辅助功能，绝不影响主答。
    if on_memory is not None:
        try:
            await on_memory()
        except Exception:
            pass
```

> ⚠️ 实施者：`Any` 已在该文件被使用（`stream_kwargs: dict[str, Any]`）。若运行报 `NameError: Any`，在顶部 `from typing import AsyncIterator, Awaitable, Callable` 后补 `, Any`。

- [ ] **Step 4: 跑测试，确认全过**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
Expected: 4 passed（含本任务新增 1）

- [ ] **Step 5: 回归 sse / router 既有测试**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_engine_e2e.py tests/test_auth/test_qa_router.py -q`
Expected: 全 passed（新 kwarg default=None，未传时与改前一致）

- [ ] **Step 6: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_engine/sse_emitter.py tests/test_auth/test_memory_service.py
git commit -m "feat(memory): P1 stream_qa_answer 透传 memory_block + on_memory 回调（镜像 on_title，TDD）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: memory/service.py —— recall / detect / write / compact

**Files:**
- Create: `src/service/memory/__init__.py`
- Create: `src/service/memory/service.py`
- Test: `tests/test_auth/test_memory_service.py`（追加一组）

- [ ] **Step 1: 追加失败测试**

在 `tests/test_auth/test_memory_service.py` **末尾**追加：

```python
# ───────── memory.service 逻辑（Fake DB/LLM）─────────

from src.service.memory.service import (
    detect_explicit_memory,
    recall_memory_block,
    write_explicit_memory,
    maybe_compact_session,
)
from src.service.db_models_homepage import QAUserMemory, QASessionMemory


# --- detect_explicit_memory：纯函数，关键词起步 ---

def test_detect_trigger_strips_prefix():
    assert detect_explicit_memory("记住我喜欢简短的回答") == "我喜欢简短的回答"
    assert detect_explicit_memory("请记住：用 Java 不要 Kotlin") == "用 Java 不要 Kotlin"
    assert detect_explicit_memory("记一下 我关注支付域") == "我关注支付域"


def test_detect_no_trigger_returns_none():
    assert detect_explicit_memory("下单流程怎么走") is None
    assert detect_explicit_memory("解释下快排") is None


def test_detect_trigger_but_empty_content_returns_none():
    assert detect_explicit_memory("记住") is None
    assert detect_explicit_memory("记住：   ") is None


# --- Fake DB：够支撑 service 的 execute/scalars/add/commit/get ---

class _FakeResult:
    def __init__(self, rows): self._rows = rows
    def scalars(self): return self
    def all(self): return self._rows
    def one_or_none(self): return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, user_rows=None, session_row=None, msg_rows=None):
        self._user_rows = user_rows or []
        self._session_row = session_row
        self._msg_rows = msg_rows or []
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        # 用 stmt 的 column_descriptions 粗判返回哪类行（够测试用）
        ent = stmt.column_descriptions[0]["entity"]
        if ent is QAUserMemory:
            return _FakeResult(self._user_rows)
        if ent is QASessionMemory:
            return _FakeResult([self._session_row] if self._session_row else [])
        return _FakeResult(self._msg_rows)

    def add(self, obj): self.added.append(obj)
    async def commit(self): self.committed = True
    async def get(self, model, pk): return self._session_row


class _FakeLLM:
    def __init__(self, reply="本次目标：排查下单超时；已确认瓶颈在 PaymentGateway"):
        self._reply = reply
    async def complete(self, *, system, user, **kw): return self._reply


@pytest.mark.asyncio
async def test_recall_empty_when_nothing():
    db = _FakeDB()
    block = await recall_memory_block(db, user_id=1, session_id="s1")
    assert block == ""


@pytest.mark.asyncio
async def test_recall_combines_session_then_user():
    um = QAUserMemory(user_id=1, kind="preference", content="回答简短",
                       source="explicit", status="active")
    sm = QASessionMemory(session_id="s1", working_summary="已确认瓶颈在网关",
                         turn_count=6)
    db = _FakeDB(user_rows=[um], session_row=sm)
    block = await recall_memory_block(db, user_id=1, session_id="s1")
    assert "已确认瓶颈在网关" in block
    assert "回答简短" in block
    # 会话级在用户级之前（spec §7 注入顺序）
    assert block.index("已确认瓶颈在网关") < block.index("回答简短")


@pytest.mark.asyncio
async def test_write_explicit_adds_user_memory_row():
    db = _FakeDB()
    await write_explicit_memory(db, user_id=7, session_id="s1", content="我用 Java")
    assert len(db.added) == 1
    row = db.added[0]
    assert isinstance(row, QAUserMemory)
    assert row.user_id == 7 and row.content == "我用 Java"
    assert row.kind == "preference" and row.source == "explicit"
    assert row.source_session_id == "s1"
    assert db.committed is True


@pytest.mark.asyncio
async def test_compact_skips_below_threshold():
    sm = QASessionMemory(session_id="s1", working_summary="old", turn_count=0)
    db = _FakeDB(session_row=sm, msg_rows=[object(), object()])  # 2 条 < 6
    await maybe_compact_session(db, _FakeLLM(), session_id="s1", every_n_messages=6)
    assert db.committed is False  # 未达阈值不压缩


@pytest.mark.asyncio
async def test_compact_creates_summary_when_threshold_reached():
    db = _FakeDB(session_row=None, msg_rows=[object()] * 6)  # 6 条 ≥ 6，无既有摘要
    await maybe_compact_session(db, _FakeLLM(), session_id="s1", every_n_messages=6)
    assert len(db.added) == 1
    row = db.added[0]
    assert isinstance(row, QASessionMemory)
    assert row.session_id == "s1"
    assert "PaymentGateway" in row.working_summary
    assert db.committed is True


@pytest.mark.asyncio
async def test_compact_skips_when_no_new_messages_since_last():
    # turn_count == msg_count → 距上次压缩零新增，跳过（不调 LLM、不 commit）。
    # 同时是 Important-1（过阈后每轮都压）的回归守卫。
    sm = QASessionMemory(session_id="s1", working_summary="prev", turn_count=6)
    db = _FakeDB(session_row=sm, msg_rows=[object()] * 6)
    await maybe_compact_session(db, _FakeLLM(), session_id="s1", every_n_messages=6)
    assert db.committed is False
    assert db.added == []
```

> ⚠️ 实施者交付时为避免与 Task 7 测试的 `_FakeDB` 重名、并让压缩格式化路径被真正执行，
> 已把上述 fake 改名 `_FakeMemDB`/`_FakeMemLLM` 并引入 `_FakeMsg`（含 `.role`/`.content`），
> `msg_rows` 用 `[_FakeMsg() for _ in range(N)]`。本计划文本保留示意命名，**以提交代码为准**。

- [ ] **Step 2: 跑测试，确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'src.service.memory'`

- [ ] **Step 3: 建包 + 实现 service**

新建 `src/service/memory/__init__.py`：

```python
"""记忆系统服务包（P1：用户级 + 会话级）。设计：[[记忆系统-设计]]。"""
```

新建 `src/service/memory/service.py`：

```python
"""记忆系统 P1 核心逻辑：召回 / 显式意图检测 / 写入 / 会话压缩。

设计：[[记忆系统-设计]] §4.1 §4.3 §6 §7。
纯逻辑，不依赖 FastAPI；DB 用 duck-typed AsyncSession（真跑用 SQLAlchemy，
单测用 Fake），LLM 用 duck-typed provider（有 async complete(system,user)）。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from src.service.db_models_homepage import QAUserMemory, QASessionMemory, QAMessage
from src.service.qa_engine.prompts import _SESSION_COMPACT_SYSTEM

_log = logging.getLogger(__name__)


# 显式记忆触发词（关键词起步；spec §15 开放问题留 P1 用关键词）
# 命中后剥掉触发词 + 紧随的冒号/空白，剩余即记忆内容。
# ⚠️ 顺序约定：若将来新增触发词是已有词的「超串」（如「顺便记一下」含「记一下」），
#    必须把更具体的放前面，否则被较短前缀先匹配（当前 5 个互不为前缀，安全）。
_TRIGGERS = ("请记住", "记住", "记一下", "记下", "帮我记住")


def detect_explicit_memory(question: str) -> str | None:
    """从用户问题里检测显式记忆意图。

    命中触发词 → 返回剥离触发词后的内容（去首尾空白与起始的中英文冒号）。
    未命中 / 内容为空 → None。
    """
    q = (question or "").strip()
    for trig in _TRIGGERS:
        if q.startswith(trig):
            rest = q[len(trig):]
            # 去掉紧跟的中/英文冒号与空白，如「记住：xxx」「记住 xxx」
            rest = rest.lstrip(" :：\t").strip()
            return rest or None
    return None


async def recall_memory_block(db: Any, *, user_id: int, session_id: str) -> str:
    """召回当前用户 + 当前会话的记忆，拼成一个文本块。

    顺序（spec §7）：会话级在前（工作上下文，最高优先），用户级在后。
    全空 → 返回 ""（调用方据此跳过注入，零开销）。

    注：本函数自身不吞异常（保持纯逻辑可测）。若调用方要求「记忆失败绝不
    影响主答」，须自行 try/except —— Task 7 的 router 调用点已这样包裹。
    """
    parts: list[str] = []

    # 会话级：一会话一行
    sm_res = await db.execute(
        select(QASessionMemory).where(QASessionMemory.session_id == session_id)
    )
    sm = sm_res.scalars().one_or_none()
    if sm is not None and (sm.working_summary or "").strip():
        parts.append("【本次会话工作状态】\n" + sm.working_summary.strip())

    # 用户级：active 全量（量小）
    um_res = await db.execute(
        select(QAUserMemory)
        .where(QAUserMemory.user_id == user_id, QAUserMemory.status == "active")
        .order_by(QAUserMemory.created_at)
    )
    user_rows = um_res.scalars().all()
    if user_rows:
        lines = "\n".join(f"- {r.content}" for r in user_rows if (r.content or "").strip())
        if lines:
            parts.append("【用户偏好 / 已知事实】\n" + lines)

    return "\n\n".join(parts)


async def write_explicit_memory(
    db: Any, *, user_id: int, session_id: str, content: str
) -> None:
    """落一条用户级显式记忆（P1：显式只进用户级）。

    注：自身不吞异常（同 recall_memory_block 契约）；Task 7 调用点已 try/except。
    """
    db.add(
        QAUserMemory(
            user_id=user_id,
            kind="preference",
            content=content,
            source="explicit",
            source_session_id=session_id,
            status="active",
        )
    )
    await db.commit()


async def maybe_compact_session(
    db: Any, llm: Any, *, session_id: str, every_n_messages: int = 6
) -> None:
    """会话级压缩：每「自上次压缩以来新增 ≥ every_n_messages 条消息」压缩一次。

    设计：[[记忆系统-设计]] §4.3（P1 固定 N 轮，N=6 条≈3 轮问答）。
    turn_count 记录上次压缩时的 message_count；用「增量 ≥ N」判定，
    而非「过阈值后每轮都压」——否则消息每轮 +2，过阈后每轮都会调 LLM（成本 bug）。
    任何异常都吞掉并 debug 记录（记忆是辅助，绝不影响主答）。
    """
    try:
        msg_res = await db.execute(
            select(QAMessage)
            .where(QAMessage.session_id == session_id)
            .order_by(QAMessage.created_at)
        )
        messages = msg_res.scalars().all()
        msg_count = len(messages)
        if msg_count < every_n_messages:
            return

        sm_res = await db.execute(
            select(QASessionMemory).where(QASessionMemory.session_id == session_id)
        )
        sm = sm_res.scalars().one_or_none()

        # 距上次压缩的新增量不足 N → 跳过（实现「每 N 条压一次」而非「过阈后每轮压」）
        prev = (sm.turn_count or 0) if sm is not None else 0
        if msg_count - prev < every_n_messages:
            return

        # 拼最近对话喂给 LLM（截断控 token：每条 ≤200 字，最多最近 12 条）
        convo = "\n".join(
            f"[{m.role}] {(m.content or '')[:200]}" for m in messages[-12:]
        )
        summary = await llm.complete(system=_SESSION_COMPACT_SYSTEM, user=convo)
        summary = (summary or "").strip()
        if not summary:
            return

        if sm is None:
            db.add(
                QASessionMemory(
                    session_id=session_id,
                    working_summary=summary,
                    turn_count=msg_count,
                )
            )
        else:
            sm.working_summary = summary
            sm.turn_count = msg_count
        await db.commit()
    except Exception:
        # 压缩失败绝不影响主流程（spec §4.3）；debug 留痕便于排查（不影响主答）
        _log.debug(
            "maybe_compact_session failed for session %s, silently ignored",
            session_id, exc_info=True,
        )
        return
```

> ⚠️ 实施者：`_FakeResult.scalars()` 返回自身、再 `.one_or_none()` / `.all()`，对应 service 里 `.scalars().one_or_none()` 与 `.scalars().all()` 两种取值。真实 SQLAlchemy 2.0：`(await session.execute(select(...))).scalars()` 得 `ScalarResult`，其上有 `.one_or_none()` / `.all()`（注意：`.scalar_one_or_none()` 是 `Result` 上的快捷方法，不在 `ScalarResult` 上；本服务统一走 `.scalars().one_or_none()` / `.scalars().all()`，正确且惯用）。

- [ ] **Step 4: 跑测试，确认全过**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
Expected: 全 passed（detect 3 + recall 2 + write 1 + compact 2 + 前序 synthesizer/stream 4 = 12 passed）

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/__init__.py src/service/memory/service.py tests/test_auth/test_memory_service.py
git commit -m "feat(memory): P1 service —— recall/detect/write/compact（TDD 8 新测试）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: qa_router.py —— 接线召回（流前）+ `_make_memory_writer`（流后）

**Files:**
- Modify: `src/service/qa_router.py`
- Test: `tests/test_auth/test_memory_router_hook.py`（新建，镜像 `test_qa_session_title.py`）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_memory_router_hook.py`：

```python
"""_make_memory_writer 工厂测试（Fake DB/LLM，镜像 test_qa_session_title.py）。
设计：[[记忆系统-设计]] §6。
"""
import pytest

from src.service.qa_router import _make_memory_writer
from src.service.db_models_homepage import QAUserMemory, QASessionMemory, QAMessage


class _FakeResult:
    def __init__(self, rows): self._rows = rows
    def scalars(self): return self
    def all(self): return self._rows
    def one_or_none(self): return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, msg_rows=None):
        self._msg_rows = msg_rows or []
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        ent = stmt.column_descriptions[0]["entity"]
        if ent is QAMessage:
            return _FakeResult(self._msg_rows)
        return _FakeResult([])  # 无既有会话记忆

    def add(self, obj): self.added.append(obj)
    async def commit(self): self.committed = True
    async def get(self, model, pk): return None


class _FakeLLM:
    async def complete(self, *, system, user, **kw):
        return "本次目标：排查下单；已确认瓶颈在 PaymentGateway"


@pytest.mark.asyncio
async def test_writer_persists_explicit_user_memory():
    db = _FakeDB()
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="记住我喜欢简短回答",
    )
    await writer()
    user_mems = [o for o in db.added if isinstance(o, QAUserMemory)]
    assert len(user_mems) == 1
    assert user_mems[0].content == "我喜欢简短回答"
    assert user_mems[0].user_id == 3


@pytest.mark.asyncio
async def test_writer_noop_when_no_trigger_and_below_threshold():
    db = _FakeDB(msg_rows=[object(), object()])  # 2 < 6
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="下单流程怎么走",   # 无触发词
    )
    await writer()
    assert db.added == []   # 不写用户记忆、不压缩


@pytest.mark.asyncio
async def test_writer_compacts_session_when_threshold_reached():
    db = _FakeDB(msg_rows=[object()] * 6)  # 达阈值
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="继续追问下一个问题",   # 无触发词，但会触发压缩
    )
    await writer()
    sess_mems = [o for o in db.added if isinstance(o, QASessionMemory)]
    assert len(sess_mems) == 1
    assert "PaymentGateway" in sess_mems[0].working_summary


@pytest.mark.asyncio
async def test_writer_never_raises_on_llm_failure():
    class _BoomLLM:
        async def complete(self, *, system, user, **kw):
            raise RuntimeError("LLM down")

    db = _FakeDB(msg_rows=[object()] * 6)
    writer = _make_memory_writer(
        db=db, llm=_BoomLLM(), user_id=3, session_id="s1",
        question="记住我用 Java",
    )
    # 显式写入仍应成功；压缩失败被吞，不抛
    await writer()
    assert any(isinstance(o, QAUserMemory) for o in db.added)
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_router_hook.py -q`
Expected: FAIL —— `ImportError: cannot import name '_make_memory_writer' from 'src.service.qa_router'`

- [ ] **Step 3: 加工厂 + 接线**

在 `src/service/qa_router.py`：

(a) import 段加（与现有 import 同区）：

```python
from src.service.memory.service import (
    recall_memory_block,
    detect_explicit_memory,
    write_explicit_memory,
    maybe_compact_session,
)
```

(b) 在 `_make_title_generator` 函数**之后**新增工厂（镜像其结构）：

```python
def _make_memory_writer(*, db, llm, user_id, session_id, question):
    """构造 on_memory 回调（闭包）。done 之后异步执行：

    1. 显式记忆意图（『记住…』）→ 写一条用户级记忆；
    2. 会话消息达阈值 → 压缩会话工作状态（覆盖式 upsert）。

    全程异常静默（记忆是辅助，绝不影响主答）。
    设计：[[记忆系统-设计]] §6。
    """
    async def _writer() -> None:
        # 1. 显式写入（高信任，同步生效）
        try:
            content = detect_explicit_memory(question)
            if content:
                await write_explicit_memory(
                    db, user_id=user_id, session_id=session_id, content=content
                )
        except Exception:
            pass
        # 2. 会话压缩（固定 N 轮；service 内部已 try/except 兜底）
        try:
            await maybe_compact_session(db, llm, session_id=session_id)
        except Exception:
            pass

    return _writer
```

(c) 在 `return StreamingResponse(stream_qa_answer(...))` **之前**召回（与 `persist_messages` 定义同一函数体内，`stream_qa_answer(` 调用之前）：

```python
    # 记忆召回（spec §7）：进流前查用户级+会话级，拼 memory_block。
    # 失败静默 → 空串，不影响主答。
    try:
        memory_block = await recall_memory_block(
            db, user_id=user.id, session_id=session_id
        )
    except Exception:
        memory_block = ""
```

(d) 给 `stream_qa_answer(...)` 调用加两个 kwarg（在现有 `on_title=_make_title_generator(...)` 之后）：

```python
            on_title=_make_title_generator(
                db=db,
                session_id=session_id,
                question=body.question,
                llm=synthesizer.llm,
                is_new_session=is_new_session,
            ),
            memory_block=memory_block,
            on_memory=_make_memory_writer(
                db=db,
                llm=synthesizer.llm,
                user_id=user.id,
                session_id=session_id,
                question=body.question,
            ),
```

> ⚠️ 实施者：`user`（`User = Depends(get_current_user)`）与 `body.question` 在该 endpoint 函数作用域内已存在（见 `persist_messages` 同函数体用到 `db`/`session_id`；`is_new_session` 已被 `_make_title_generator` 使用）。`synthesizer.llm` 是 `QASynthesizer` 实例属性（见 `synthesizer.py` `__init__`）。若实际变量名不同（如 `current_user`），按真实名调整——只改接线，不改 service。

- [ ] **Step 4: 跑测试，确认全过**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_router_hook.py -q`
Expected: 4 passed

- [ ] **Step 5: 回归 router 既有测试**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_router.py tests/test_auth/test_qa_session_title.py tests/test_auth/test_qa_session_router.py -q`
Expected: 全 passed（召回失败兜空串、on_memory 失败静默；未触发记忆时行为与改前一致）

- [ ] **Step 6: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_router.py tests/test_auth/test_memory_router_hook.py
git commit -m "feat(memory): P1 接线 —— 流前召回 memory_block + _make_memory_writer 流后写入（TDD 4 测试）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: 全量回归 + import 自检 + 收尾

**Files:** 无新文件

- [ ] **Step 1: import 自检（venv 内）**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -c "import src.service.qa_router, src.service.memory.service, src.service.qa_engine.sse_emitter, src.service.qa_engine.synthesizer, src.service.db_models_homepage; print('import OK')"`
Expected: 打印 `import OK`（无 ImportError / 语法错误）

- [ ] **Step 2: 记忆系统全部测试**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_models_memory.py tests/test_auth/test_memory_prompt.py tests/test_auth/test_memory_service.py tests/test_auth/test_memory_router_hook.py -q`
Expected: 全 passed（8 + 4 + 12 + 4 = 28 passed）

- [ ] **Step 3: QA 链路总回归（确认零破坏）**

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/ -q -k "qa or models or prompt or memory or chitchat"`
Expected: 全 passed（无 fail / error；新功能 default 关闭，未触发时完全向后兼容）

- [ ] **Step 4: 后端健康（若本地 uvicorn 在跑）**

Run: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/openapi.json`
Expected: `200`（若本地未起服务则跳过此步，非阻断）

- [ ] **Step 5: 收尾说明（不自动执行，交付给用户）**

> 生产库迁移需用户手动执行（与本仓约定一致）：
> `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m alembic upgrade head`
> 升级后生效表：`qa_user_memory`、`qa_session_memory`。

---

## Self-Review（实施者跑完过一遍）

- [ ] **Spec 覆盖**：§4.1 用户级 → Task 1 `QAUserMemory` + Task 6 recall/write ✓；§4.3 会话级 → Task 1 `QASessionMemory` + Task 6 `maybe_compact_session` ✓；§6 写入（显式 + 触发）→ Task 6 `detect/write` + Task 7 `_make_memory_writer` ✓；§7 召回（会话>用户、全量注入）→ Task 6 `recall_memory_block` 顺序 + Task 3/4/5 注入链 ✓；§12 P1 范围（仅 2 层、零新基建、显式触发）→ 全程不碰 Weaviate/Neo4j、不建工程级表 ✓；§9 软删 → `status` 字段 + 无物理删 ✓
- [ ] **占位扫描**：每个 code step 均为完整可粘贴代码，无 TBD/TODO/“类似上文”；命令均有 Expected ✓
- [ ] **类型/签名一致性**：`memory_block: str | None` 贯穿 prompts→synthesizer→sse_emitter→qa_router 同名；`on_memory: OnMemoryCallback` 与 `_make_memory_writer()` 返回的 `_writer` 签名 `() -> Awaitable[None]` 一致；service 4 函数签名在 Task 6 定义、Task 7 按同签名调用；`QAUserMemory/QASessionMemory` 字段在 Task 1 定义、Task 2 迁移/Task 6 service/测试一致 ✓
- [ ] **YAGNI**：未做工程级/grounding/异步抽取/污染门控/晋升/毕业/管理 UI/`memory_written` SSE 事件（均 P2/P3 或 spec 标可选）✓
- [ ] **向后兼容**：所有新参数 default=None / 默认关闭；未触发记忆时各回归 step 验证既有测试全绿 ✓

## Phase Definition of Done

- [ ] 4 个新测试文件全 pass（28 用例）
- [ ] QA 链路回归无破坏（Task 8 Step 3 全绿）
- [ ] `import OK`（Task 8 Step 1）
- [ ] `alembic heads` 单 head = `qa_memory_p1_v1`
- [ ] 7 个功能 commit + 计划文档已落 `docs/superpowers/plans/`
- [ ] 已向用户交付「生产库需手动 `alembic upgrade head`」说明
