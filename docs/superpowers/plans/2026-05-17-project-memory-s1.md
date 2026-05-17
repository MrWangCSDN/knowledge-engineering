# 工程级记忆 S1（最小可用，纯 SQL）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 加最小可用工程级记忆：用户「记住这个工程：…」→ 写私有 `qa_project_memory` 行；下次同工程提问时该工程记忆注入 system prompt（用户块之后，§7 末位）。

**Architecture:** 镜像已上线的 user 级三件套——新表 `qa_project_memory`（全 §5 列，纯 SQL，零新基建）+ 独立工程显式检测器（先于通用 user 检测，防错路由）+ `recall_memory_block` 加 `project_id` 拼工程块。失败不变量、多租户过滤、软删、向后兼容全沿用既有。无 Weaviate/异步/grounding/晋升/pending_review（S2-S4）。

**Tech Stack:** Python / SQLAlchemy 2.0 async / Alembic / pytest（Fake DB/LLM + monkeypatch，沿用 `tests/test_auth` 既有风格）

**Spec:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/记忆系统-设计.md` §19（S1 定稿）+ §4.2/§5/§7

**Repo:** `/Users/java/knowledge-engineering-auth`，分支 `release-0513`（沿用，无 worktree）。逐任务提交（已授权）。

---

## 现状基线（已核对真实代码 2026-05-17）

- `src/service/db_models_homepage.py`：imports 含 `BigInteger,Boolean,DateTime,ForeignKey,Index,Integer,JSON,String,Text,func,text`（**无 `Float`**，需加）+ `Optional`/`datetime` 已可用 + `Mapped,mapped_column`。`QAUserMemory`(line 321)/`QASessionMemory`(362) 是模型范式参考。`Project.__tablename__="projects"`，`Project.id` 为 `String(64)` 主键；既有 `ForeignKey("projects.id", ondelete="CASCADE")` 用法在 line 174/197。
- Alembic head = `qa_memory_p1_v1`（P2①/②无迁移）。迁移范式见 `alembic/versions/qa_memory_p1_v1.py`（module 级 `revision/down_revision/branch_labels=None/depends_on=None`；`op.create_table` + `sa.Column`；字符串/数值 server_default 用 `sa.text("'explicit'")`/`sa.text("0")`；`op.create_index`）。
- `src/service/memory/service.py`：`from sqlalchemy import select`（**无 `or_`**，需加）。`detect_explicit_memory(question)->str|None`（`_TRIGGERS=("请记住","记住","记一下","记下","帮我记住")`，startswith→`q[len(trig):].lstrip(" :：\t").strip()`→内容或 None）。`recall_memory_block(db,*,user_id,session_id)->str`：建 `parts`，先会话块（QASessionMemory + focus），再用户块（QAUserMemory where user_id & status=='active' order_by created_at，`f"- {r.content}"`），`return "\n\n".join(parts)`。`write_explicit_memory(db,*,user_id,session_id,content)`：`db.add(QAUserMemory(user_id=,kind="preference",content=,source="explicit",source_session_id=session_id,status="active"))`+`await db.commit()`。模块级 `_log=logging.getLogger(__name__)` 已有。
- `src/service/qa_router.py`：`async def explain(project_id: str, ...)`（line 155-156，`project_id` 是 path 参数，作用域内）。line 273-278：`try: memory_block = await recall_memory_block(db, user_id=user.id, session_id=session_id) except Exception: memory_block=""`。`_make_memory_writer(*, db, llm, user_id, session_id, question, force_compact=False)`：内 `_writer`：① try `content=detect_explicit_memory(question)`; if content `await write_explicit_memory(db,user_id=,session_id=,content=)`; `except Exception: _log.debug(...)` ② try `await maybe_compact_session(db,llm,session_id=session_id,force=force_compact)` `except Exception: pass`；`return _writer`。调用点 `on_memory=_make_memory_writer(db=db, llm=synthesizer.llm, user_id=user.id, session_id=session_id, question=body.question, force_compact=history_trimmed)`。
- 测试：`tests/test_auth/test_models_memory.py`（metadata 全等断言 `cols == {...}`、PK、FK ondelete、index 名集合）；`tests/test_auth/test_memory_service.py`（`_FakeMemDB(user_rows,session_row,msg_rows)` 按 `stmt.column_descriptions[0]["entity"]` 分派 + `_FakeResult.scalars().all()/.one_or_none()`、`_FakeMemLLM`、`_FakeMsg(role,content,msg_metadata)`、`pytest.mark.asyncio`）；`tests/test_auth/test_memory_router_hook.py`（`_make_memory_writer` + `_FakeDB(msg_rows)`/`_FakeMsg`/`_FakeLLM`）。
- 跑测试前缀：`cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest <…> -q`

---

## File Structure

| 文件 | 改动 |
|---|---|
| `src/service/db_models_homepage.py` | imports 加 `Float`；末尾追加 `QAProjectMemory` 模型（全 §5 列，String(64) project_id FK CASCADE、Integer user_id 无 FK、枚举 String(32)、3 索引） |
| `alembic/versions/qa_project_memory_p2s1_v1.py` | 🆕 `down_revision="qa_memory_p1_v1"`，建表+3 索引；downgrade 删表。**生产门控用户跑** |
| `src/service/memory/service.py` | 加 `or_` import；`_PROJECT_TRIGGERS` + `detect_explicit_project_memory`；`write_explicit_project_memory`；`recall_memory_block` 加 `project_id:str\|None=None` 拼工程块 |
| `src/service/qa_router.py` | recall 调用加 `project_id=project_id`；`_make_memory_writer` 加 `project_id` 参数 + `_writer` 先工程检测后 user 检测；调用点加 `project_id=project_id` |
| `tests/test_auth/test_models_memory.py` | 追加 `QAProjectMemory` metadata 测试 |
| `tests/test_auth/test_memory_service.py` | 追加 detect/write/recall 工程级测试 |
| `tests/test_auth/test_memory_router_hook.py` | 追加 `_make_memory_writer` 工程写 + 优先级测试 |

---

## Task 1: QAProjectMemory ORM 模型

**Files:** Modify `src/service/db_models_homepage.py`; Test `tests/test_auth/test_models_memory.py`

- [ ] **Step 1: 失败测试** — 追加到 `tests/test_auth/test_models_memory.py` 末尾：

```python
# ───────── qa_project_memory（工程级 S1，spec §19）─────────
from src.service.db_models_homepage import QAProjectMemory


def test_project_memory_table_name():
    assert QAProjectMemory.__tablename__ == "qa_project_memory"


def test_project_memory_columns():
    cols = {c.name for c in QAProjectMemory.__table__.columns}
    assert cols == {
        "id", "project_id", "user_id", "scope", "content",
        "entity_id", "entity_kind", "grounding_status", "source",
        "source_session_id", "confidence", "status",
        "promoted_by", "promoted_at", "vector_synced", "last_verified_at",
        "created_at", "updated_at",
    }


def test_project_memory_pk_is_id():
    cols = {c.name: c for c in QAProjectMemory.__table__.columns}
    assert cols["id"].primary_key is True


def test_project_memory_project_id_fk_cascade():
    fks = [fk for fk in QAProjectMemory.__table__.foreign_keys
           if fk.column.table.name == "projects"]
    assert len(fks) == 1
    assert fks[0].ondelete == "CASCADE"


def test_project_memory_user_id_no_fk():
    # 与 QASession/qa_user_memory 一致：user_id 不加 FK（保留已删用户记忆）
    fks = [fk for fk in QAProjectMemory.__table__.foreign_keys
           if fk.column.table.name == "users"]
    assert fks == []


def test_project_memory_indexes():
    idx = {i.name for i in QAProjectMemory.__table__.indexes}
    assert {"idx_proj_scope", "idx_proj_user", "idx_entity"} <= idx
```

- [ ] **Step 2: 跑，确认失败** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_models_memory.py -q`
  Expected: FAIL — `ImportError: cannot import name 'QAProjectMemory'`

- [ ] **Step 3: 实现** —
  (a) `src/service/db_models_homepage.py` imports：在 `from sqlalchemy import (` 块内、`DateTime,` 之后加一行 `Float,`（保持字母序：`...DateTime, Float, ForeignKey,...`）。
  (b) 文件**末尾**（`QASessionMemory` 之后）追加：

```python


# ─── 8. qa_project_memory（记忆系统 P2-S1：工程级部落知识）──────────────────
# 设计：[[记忆系统-设计]] §19 / §4.2 / §5。S1 纯 SQL；全列一次建全，
# entity_*/grounding/confidence/promoted_*/vector_synced/last_verified_at
# 留给 S2-S4，S1 不读写。project_id 用 String(64) FK（§5 BIGINT 是示意，
# 实际对齐 Project.id / QASession.project_id 既有约定，同 P1 session_id）。

class QAProjectMemory(Base):
    """工程级记忆：某代码库的人类部落知识。A3 默认私有，可晋升 team（S4）。"""
    __tablename__ = "qa_project_memory"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    """归属工程。删工程级联删其工程记忆（与 QASession.project_id 一致）。"""

    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    """作者（对应 users.id）。与 QASession 一致不加 FK：保留已删用户的记忆。"""

    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="private")
    """private（A3 默认）/ team（S4 晋升后）。String(32) 留余量，不用 sa.Enum。"""

    content: Mapped[str] = mapped_column(Text, nullable=False)
    """部落知识软笔记，如『orders_v2 是现行表 orders 已废弃』。"""

    entity_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    """C3 可选锚 canonical_v1 实体 ID（S4 用，S1 留空）。"""

    entity_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    """file / class / method（S4 失效复核归类，S1 留空）。"""

    grounding_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ungrounded"
    )
    """grounded / ungrounded / stale（S4 用；S1 恒 ungrounded）。"""

    source: Mapped[str] = mapped_column(String(32), nullable=False, default="explicit")
    """explicit（S1 显式『记住这个工程：』）/ extracted（S3 异步抽取）。"""

    source_session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    """来源会话 ID（可追溯，不加 FK 硬绑）。"""

    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    """extracted 可调低（S3 用，召回排序）；S1 显式恒 1.0。"""

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    """active / archived（软删，宪法禁物理删）/ pending_review（S3 用）。"""

    promoted_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    """晋升 team 的操作者 user.id（S4 审计，S1 留空）。"""

    promoted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    vector_synced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """是否已写入 Weaviate（S2 用，S1 恒 False）。"""

    last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    """老化告警基准（S4 用，S1 留空）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_proj_scope", "project_id", "scope", "status"),
        Index("idx_proj_user", "project_id", "user_id", "status"),
        Index("idx_entity", "project_id", "entity_id"),
    )
```

- [ ] **Step 4: 跑，确认通过** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_models_memory.py -q`
  Expected: 全 passed（新 6 + 既有不回归）

- [ ] **Step 5: Commit**
```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/db_models_homepage.py tests/test_auth/test_models_memory.py
git commit -m "$(cat <<'EOF'
feat(memory): P2-S1 QAProjectMemory ORM 模型（全 §5 列，String(64) FK CASCADE，TDD）

project_id=String(64) FK projects.id CASCADE / user_id Integer 无 FK（订正 §5 示意 BIGINT，
对齐代码库既有约定）；全列一次建，S2-S4 列留空。设计 [[记忆系统-设计]] §19。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Alembic 迁移 qa_project_memory_p2s1_v1

**Files:** Create `alembic/versions/qa_project_memory_p2s1_v1.py`

- [ ] **Step 1: 写迁移**（镜像 `qa_memory_p1_v1.py` 风格）：

```python
"""工程级记忆 S1：建 qa_project_memory 表

Revision ID: qa_project_memory_p2s1_v1
Revises: qa_memory_p1_v1
Create Date: 2026-05-17

设计：[[记忆系统-设计]] §19（S1 仅此表；S2-S4 列留空）
"""
from alembic import op
import sqlalchemy as sa

revision = "qa_project_memory_p2s1_v1"
down_revision = "qa_memory_p1_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qa_project_memory",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False, server_default=sa.text("'private'")),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=True),
        sa.Column("entity_kind", sa.String(32), nullable=True),
        sa.Column("grounding_status", sa.String(32), nullable=False, server_default=sa.text("'ungrounded'")),
        sa.Column("source", sa.String(32), nullable=False, server_default=sa.text("'explicit'")),
        sa.Column("source_session_id", sa.String(64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("promoted_by", sa.Integer(), nullable=True),
        sa.Column("promoted_at", sa.DateTime(), nullable=True),
        sa.Column("vector_synced", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_proj_scope", "qa_project_memory", ["project_id", "scope", "status"])
    op.create_index("idx_proj_user", "qa_project_memory", ["project_id", "user_id", "status"])
    op.create_index("idx_entity", "qa_project_memory", ["project_id", "entity_id"])


def downgrade() -> None:
    op.drop_index("idx_entity", table_name="qa_project_memory")
    op.drop_index("idx_proj_user", table_name="qa_project_memory")
    op.drop_index("idx_proj_scope", table_name="qa_project_memory")
    op.drop_table("qa_project_memory")
```

- [ ] **Step 2: 校验单 head（离线，不连库）** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m alembic heads`
  Expected: 仅一行 `qa_project_memory_p2s1_v1 (head)`。若多 head=分叉，STOP 报 BLOCKED。

- [ ] **Step 3: 校验脚本可解析** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m alembic history | head -2`
  Expected: 顶部 `qa_memory_p1_v1 -> qa_project_memory_p2s1_v1 (head), 工程级记忆 S1...`，无 traceback。（若环境无 DB 连接报错而非脚本错，改用 `python3 -c "import importlib.util as u;s=u.spec_from_file_location('m','alembic/versions/qa_project_memory_p2s1_v1.py');m=u.module_from_spec(s);s.loader.exec_module(m);print(m.revision,m.down_revision)"` 期望 `qa_project_memory_p2s1_v1 qa_memory_p1_v1`，并报 DONE_WITH_CONCERNS 说明环境无库）

- [ ] **Step 4: Commit**
```bash
cd /Users/java/knowledge-engineering-auth
git add alembic/versions/qa_project_memory_p2s1_v1.py
git commit -m "$(cat <<'EOF'
feat(memory): P2-S1 Alembic 迁移 qa_project_memory_p2s1_v1（head 接 qa_memory_p1_v1）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

> ⚠️ **不对生产库跑 `alembic upgrade head`**：生产迁移由用户手动执行（与本仓既往约定一致，命令会被分类器拦截）。Phase DoD 提示用户自行升级。

---

## Task 3: service.py —— 工程检测器 + 工程写 + recall 工程块

**Files:** Modify `src/service/memory/service.py`; Test `tests/test_auth/test_memory_service.py`

- [ ] **Step 1: 失败测试** — 追加到 `tests/test_auth/test_memory_service.py` 末尾：

```python
# ───────── 工程级 S1（spec §19）─────────
from src.service.memory.service import (
    detect_explicit_project_memory,
    write_explicit_project_memory,
)
from src.service.db_models_homepage import QAProjectMemory


def test_detect_project_trigger_strips_prefix():
    assert detect_explicit_project_memory("记住这个工程：orders_v2 是现行表") == "orders_v2 是现行表"
    assert detect_explicit_project_memory("记住本工程 用 Java 21") == "用 Java 21"
    assert detect_explicit_project_memory("工程记住：回调有重试") == "回调有重试"


def test_detect_project_no_trigger_or_empty():
    assert detect_explicit_project_memory("记住我喜欢简短") is None  # 通用 user 触发，非工程
    assert detect_explicit_project_memory("下单流程怎么走") is None
    assert detect_explicit_project_memory("记住这个工程：   ") is None


@pytest.mark.asyncio
async def test_write_explicit_project_adds_row():
    db = _FakeMemDB()
    await write_explicit_project_memory(
        db, project_id="deposit", user_id=7, session_id="s1", content="orders_v2 现行表"
    )
    rows = [o for o in db.added if isinstance(o, QAProjectMemory)]
    assert len(rows) == 1
    r = rows[0]
    assert r.project_id == "deposit" and r.user_id == 7
    assert r.content == "orders_v2 现行表"
    assert r.scope == "private" and r.source == "explicit" and r.status == "active"
    assert r.source_session_id == "s1"
    assert db.committed is True


@pytest.mark.asyncio
async def test_recall_includes_project_block_after_user_block():
    pm = QAProjectMemory(project_id="deposit", user_id=1, scope="private",
                          content="orders_v2 是现行表", source="explicit", status="active")
    um = QAUserMemory(user_id=1, kind="preference", content="回答简短",
                       source="explicit", status="active")
    db = _FakeMemDB(user_rows=[um], session_row=None, project_rows=[pm])
    block = await recall_memory_block(db, user_id=1, session_id="s1", project_id="deposit")
    assert "回答简短" in block and "orders_v2 是现行表" in block
    assert "【工程记忆】" in block
    # §7 顺序：用户块在工程块之前
    assert block.index("回答简短") < block.index("orders_v2 是现行表")


@pytest.mark.asyncio
async def test_recall_no_project_block_when_project_id_none():
    pm = QAProjectMemory(project_id="deposit", user_id=1, scope="private",
                          content="X", source="explicit", status="active")
    db = _FakeMemDB(user_rows=[], session_row=None, project_rows=[pm])
    block = await recall_memory_block(db, user_id=1, session_id="s1")  # 无 project_id
    assert "【工程记忆】" not in block
```

并扩展 `_FakeMemDB`：找到 `tests/test_auth/test_memory_service.py` 中 `class _FakeMemDB` 的 `__init__` 与 `execute`，加 `project_rows` 支持。把：
```python
class _FakeMemDB:
    def __init__(self, user_rows=None, session_row=None, msg_rows=None):
        self._user_rows = user_rows or []
        self._session_row = session_row
        self._msg_rows = msg_rows or []
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        ent = stmt.column_descriptions[0]["entity"]
        if ent is QAUserMemory:
            return _FakeResult(self._user_rows)
        if ent is QASessionMemory:
            return _FakeResult([self._session_row] if self._session_row else [])
        return _FakeResult(self._msg_rows)
```
改为（加 `project_rows` 入参 + `QAProjectMemory` 分派；其余不动）：
```python
class _FakeMemDB:
    def __init__(self, user_rows=None, session_row=None, msg_rows=None,
                 project_rows=None):
        self._user_rows = user_rows or []
        self._session_row = session_row
        self._msg_rows = msg_rows or []
        self._project_rows = project_rows or []
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        ent = stmt.column_descriptions[0]["entity"]
        if ent is QAUserMemory:
            return _FakeResult(self._user_rows)
        if ent is QASessionMemory:
            return _FakeResult([self._session_row] if self._session_row else [])
        if ent is QAProjectMemory:
            return _FakeResult(self._project_rows)
        return _FakeResult(self._msg_rows)
```
（`QAProjectMemory` 在该测试文件需可见——上面的新 import 已加。若文件顶部既有 `from src.service.db_models_homepage import QAUserMemory, QASessionMemory, QAMessage` 一行，把 `QAProjectMemory` 并入该行而非重复 import。）

- [ ] **Step 2: 跑，确认失败** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
  Expected: FAIL — `ImportError: cannot import name 'detect_explicit_project_memory'`

- [ ] **Step 3: 实现** — `src/service/memory/service.py`：
  (a) import：`from sqlalchemy import select` → `from sqlalchemy import select, or_`；并确保顶部 model import 含 `QAProjectMemory`（现有 `from src.service.db_models_homepage import QAUserMemory, QASessionMemory, QAMessage` → 加 `, QAProjectMemory`）。
  (b) 在 `detect_explicit_memory` **之后**加：

```python
# 工程级显式触发词。不含冒号——靠 lstrip 容错「：/:/空格」分隔。
# ⚠️ 这些是「记住」的超串，调用方（_make_memory_writer）必须先调本检测器、
#    后调通用 detect_explicit_memory，否则「记住这个工程：X」会被误判为 user 级。
_PROJECT_TRIGGERS = ("记住这个工程", "记住本工程", "记住该工程", "工程记住")


def detect_explicit_project_memory(question: str) -> str | None:
    """检测工程级显式记忆意图（「记住这个工程：…」）。

    命中 → 剥前缀 + 起始冒号/空白，返回内容；未命中/空 → None。
    """
    q = (question or "").strip()
    for trig in _PROJECT_TRIGGERS:
        if q.startswith(trig):
            rest = q[len(trig):].lstrip(" :：\t").strip()
            return rest or None
    return None
```
  (c) 在 `write_explicit_memory` **之后**加：

```python
async def write_explicit_project_memory(
    db: Any, *, project_id: str, user_id: int, session_id: str, content: str
) -> None:
    """落一条工程级显式记忆（S1：scope=private，source=explicit，status=active）。
    注：自身不吞异常（同 recall/user-write 契约）；调用点 try/except。
    """
    db.add(
        QAProjectMemory(
            project_id=project_id,
            user_id=user_id,
            scope="private",
            content=content,
            source="explicit",
            source_session_id=session_id,
            status="active",
        )
    )
    await db.commit()
```
  (d) `recall_memory_block` 签名与返回前的工程块。签名改：
```python
async def recall_memory_block(
    db: Any, *, user_id: int, session_id: str, project_id: str | None = None
) -> str:
```
  docstring 顺序说明改为「会话 > 用户 > 工程（§7；project_id=None 不出工程块，向后兼容）」。在 `return "\n\n".join(parts)` **之前**插入：
```python
    if project_id is not None:
        pm_res = await db.execute(
            select(QAProjectMemory)
            .where(
                QAProjectMemory.project_id == project_id,
                QAProjectMemory.status == "active",
                or_(
                    QAProjectMemory.scope == "team",
                    QAProjectMemory.user_id == user_id,
                ),
            )
            .order_by(QAProjectMemory.created_at)
            .limit(20)
        )
        proj_rows = pm_res.scalars().all()
        if proj_rows:
            lines = "\n".join(
                f"- {r.content}" for r in proj_rows if (r.content or "").strip()
            )
            if lines:
                parts.append("【工程记忆】\n" + lines)
```

- [ ] **Step 4: 跑，确认通过** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_service.py -q`
  Expected: 全 passed（新 5 + 既有不回归——`recall_memory_block` 新参默认 None，既有调用/测试行为不变）

- [ ] **Step 5: Commit**
```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/memory/service.py tests/test_auth/test_memory_service.py
git commit -m "$(cat <<'EOF'
feat(memory): P2-S1 工程检测器 + 工程写 + recall 工程块（§7 末位，TDD）

detect_explicit_project_memory（先于通用 user 检测）；write_explicit_project_memory
（scope=private）；recall_memory_block 加 project_id 拼【工程记忆】于用户块后，
多租户 project_id+(team OR user_id) 过滤封顶 20；project_id=None 向后兼容。
设计 [[记忆系统-设计]] §19。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: qa_router.py 接线

**Files:** Modify `src/service/qa_router.py`; Test `tests/test_auth/test_memory_router_hook.py`

- [ ] **Step 1: 失败测试** — 追加到 `tests/test_auth/test_memory_router_hook.py` 末尾：

```python
from src.service.db_models_homepage import QAProjectMemory


@pytest.mark.asyncio
async def test_writer_project_trigger_writes_project_not_user():
    # 「记住这个工程：X」→ 进工程级（QAProjectMemory），不进 user 级
    db = _FakeDB()
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="记住这个工程：orders_v2 是现行表", project_id="deposit",
    )
    await writer()
    proj = [o for o in db.added if isinstance(o, QAProjectMemory)]
    usr = [o for o in db.added if isinstance(o, QAUserMemory)]
    assert len(proj) == 1 and proj[0].content == "orders_v2 是现行表"
    assert proj[0].project_id == "deposit"
    assert usr == []  # 未误写 user 级


@pytest.mark.asyncio
async def test_writer_generic_trigger_still_user_level():
    # 通用「记住…」（非工程）→ 仍走 user 级，不写工程
    db = _FakeDB()
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="记住我喜欢简短回答", project_id="deposit",
    )
    await writer()
    assert any(isinstance(o, QAUserMemory) for o in db.added)
    assert not any(isinstance(o, QAProjectMemory) for o in db.added)


@pytest.mark.asyncio
async def test_writer_project_id_none_no_crash():
    # 旧调用未传 project_id → 工程检测跳过，不抛
    db = _FakeDB()
    writer = _make_memory_writer(
        db=db, llm=_FakeLLM(), user_id=3, session_id="s1",
        question="记住这个工程：X",
    )
    await writer()  # 不抛
    assert not any(isinstance(o, QAProjectMemory) for o in db.added)
```
确保该测试文件能 `isinstance(..., QAUserMemory)`（顶部若无 `QAUserMemory` import 则与既有 `from src.service.db_models_homepage import ...` 合并加上 `QAUserMemory, QAProjectMemory`）。`_FakeDB` 已有 `add`/`committed`；`detect_explicit_*` 走真实逻辑，`maybe_compact_session` 因 `_FakeDB` 无项目数据/消息不触发或被内部 try 兜底——这些测试只断言显式写分流。

- [ ] **Step 2: 跑，确认失败** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_router_hook.py -q`
  Expected: FAIL — `_make_memory_writer() got an unexpected keyword argument 'project_id'`

- [ ] **Step 3: 实现** — `src/service/qa_router.py`：
  (a) import：现有 `from src.service.memory.service import (recall_memory_block, detect_explicit_memory, write_explicit_memory, maybe_compact_session)`（约 line 44）→ 加 `detect_explicit_project_memory, write_explicit_project_memory`。
  (b) `_make_memory_writer` 签名加 `project_id`：
```python
def _make_memory_writer(*, db, llm, user_id, session_id, question, project_id=None, force_compact: bool = False):
```
  docstring 第 1 条改为：`1. 显式记忆意图 → 工程级（「记住这个工程：…」）优先，否则用户级（「记住…」）`。`_writer` 内①显式写块替换为（先工程后通用，分流互斥）：
```python
        # 1. 显式写入（先工程后通用：工程触发词是「记住」超串，必须先判）
        try:
            proj_content = (
                detect_explicit_project_memory(question)
                if project_id is not None else None
            )
            if proj_content:
                await write_explicit_project_memory(
                    db, project_id=project_id, user_id=user_id,
                    session_id=session_id, content=proj_content,
                )
            else:
                content = detect_explicit_memory(question)
                if content:
                    await write_explicit_memory(
                        db, user_id=user_id, session_id=session_id, content=content
                    )
        except Exception:
            _log.debug(
                "explicit memory write failed for session %s, silently ignored",
                session_id, exc_info=True,
            )
```
（第 2 块 `maybe_compact_session` 不动。）
  (c) recall 调用（约 line 273-278）：`memory_block = await recall_memory_block(db, user_id=user.id, session_id=session_id)` → 加 `project_id=project_id`：
```python
        memory_block = await recall_memory_block(
            db, user_id=user.id, session_id=session_id, project_id=project_id
        )
```
  (d) `_make_memory_writer(...)` 调用点（`on_memory=_make_memory_writer(db=db, llm=synthesizer.llm, user_id=user.id, session_id=session_id, question=body.question, force_compact=history_trimmed)`）加 `project_id=project_id,`（`project_id` 是 `explain` 的 path 参数，作用域内）。

- [ ] **Step 4: 跑，确认通过** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_memory_router_hook.py -q`
  Expected: 全 passed（新 3 + 既有不回归——`project_id` 默认 None 向后兼容）

- [ ] **Step 5: 回归（pre-existing vs new 分类）** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_qa_router.py -q`
  Expected: 全 passed（本会话已修绿 test_qa_router.py；本任务纯加默认 None 参数 + 显式写分流，未触发记忆时与改前一致）。若现新红（基线绿→红）必须查修。

- [ ] **Step 6: Commit**
```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/qa_router.py tests/test_auth/test_memory_router_hook.py
git commit -m "$(cat <<'EOF'
feat(memory): P2-S1 router 接线——project_id 透传 recall + _make_memory_writer 先工程后用户分流（TDD）

recall_memory_block(project_id=)；_make_memory_writer 加 project_id，_writer 先工程
检测→工程写否则通用→user 写（防「记住这个工程：」误路由）。project_id=None 向后兼容。
设计 [[记忆系统-设计]] §19。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 回归验证（无新文件/commit）

- [ ] **Step 1: 记忆全套 + 模型** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/test_models_memory.py tests/test_auth/test_memory_prompt.py tests/test_auth/test_memory_service.py tests/test_auth/test_memory_router_hook.py tests/test_auth/test_context_budget.py -q`
  Expected: 全 passed（既有 51 + S1 新增：模型 6 + service 5 + router-hook 3 = 14 → 共 65）
- [ ] **Step 2: import 自检** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -c "import src.service.db_models_homepage, src.service.memory.service, src.service.qa_router; print('import OK')"` → `import OK`
- [ ] **Step 3: 单 alembic head** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m alembic heads` → 仅 `qa_project_memory_p2s1_v1 (head)`
- [ ] **Step 4: QA 链路广回归** — `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate && python3 -m pytest tests/test_auth/ -q -k "qa or models or prompt or memory or chitchat or context_budget" -p no:warnings`
  Expected: 0 fail（基线本会话 293/0 + S1 新增 14 → 307/0）

---

## Self-Review（实施者过一遍）

- [ ] spec §19 逐条：QAProjectMemory 全 §5 列 + project_id String(64) FK CASCADE + user_id Integer 无 FK ✓(T1)；迁移 down_revision=qa_memory_p1_v1 ✓(T2)；detect_explicit_project_memory 先于通用 ✓(T3+T4)；write scope=private/source=explicit/status=active ✓(T3)；recall §7 末位 + project_id+(team OR user_id) 过滤 + 封顶 20 + project_id=None 向后兼容 ✓(T3)；router 透传 ✓(T4)；失败不变量沿用既有 ✓(T4 try/except + _log.debug)
- [ ] 占位扫描：每 code step 完整可粘贴、命令带 Expected ✓
- [ ] 类型一致：`QAProjectMemory` 字段在 T1 定义、T2 迁移/T3 写/T3-T4 测试一致；`detect_explicit_project_memory(question)->str|None`、`write_explicit_project_memory(db,*,project_id,user_id,session_id,content)`、`recall_memory_block(...,project_id:str|None=None)`、`_make_memory_writer(...,project_id=None,...)` 跨任务签名一致 ✓
- [ ] YAGNI：仅 S1；Weaviate/异步/污染门控/grounding/entity_id 逻辑/晋升/pending_review/管理 UI/老化 全不做（列建好留空但无读写逻辑）✓
- [ ] 向后兼容：recall/`_make_memory_writer` 新参默认 None；既有测试不改；多租户严格过滤；软删=archived ✓

## Phase Definition of Done

- [ ] S1 新增 14 测试全绿；记忆+模型+context_budget 全套 65 passed
- [ ] QA 链路 307/0（不回归本会话 293/0 基线）
- [ ] import OK；单 alembic head = qa_project_memory_p2s1_v1
- [ ] 4 feat commit 干净（模型 / 迁移 / service / router）
- [ ] 已向用户交付「生产库需手动 `alembic upgrade head`」说明（建 qa_project_memory）
