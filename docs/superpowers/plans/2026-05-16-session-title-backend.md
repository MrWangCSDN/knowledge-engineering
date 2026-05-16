# 会话标题（后端）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 后端支持会话重命名（PATCH 接口 + `title_custom` 保护）+ 首轮问答后异步 LLM 总结标题（SSE 流末尾 `session_title` 事件）。

**Architecture:** `QASession` 加 `title_custom` 布尔列（Alembic migration）；新增 `PATCH /sessions/{sid}` rename 端点；`sse_emitter.stream_qa_answer` 加 `on_title` 回调，在 `done` 事件后（先 commit DB 再 emit）产出 `session_title` 事件；qa_router 构造 `generate_title` 回调，复用 `synthesizer.llm.complete` 做轻量总结。

**Tech Stack:** FastAPI / SQLAlchemy 2.0 async / Alembic / pytest / pytest-asyncio

**Spec:** `/Users/java/obsidian/01 Engineering/knowledge-engineering/会话标题-重命名与智能总结-设计.md`

**Repo:** `/Users/java/knowledge-engineering-auth`（运行中后端，分支当前 `release-0513` 或当前分支，uvicorn --reload pid 61190）

---

## File Structure

| 文件 | 改动 |
|---|---|
| `src/service/db_models_homepage.py` | QASession 加 `title_custom` 列 + import `text` |
| `alembic/versions/session_title_custom_v1.py` | 🆕 migration（加列，down_revision=`qa_archive_v1`）|
| `src/service/qa_engine/prompts.py` | 🆕 `_TITLE_SUMMARY_SYSTEM` prompt 常量 |
| `src/service/qa_engine/sse_emitter.py` | 加 `OnTitleCallback` 类型 + `on_title` 参数 + done 后 emit `session_title` |
| `src/service/qa_router.py` | 🆕 `PATCH /sessions/{sid}` rename 端点；构造 `generate_title` 回调传入 stream_qa_answer |
| `tests/test_auth/test_qa_session_router.py` | 加 rename 测试 |
| `tests/test_auth/test_qa_session_title.py` | 🆕 异步标题总结逻辑测试 |

---

## Task 1: QASession 加 title_custom 列

**Files:**
- Modify: `src/service/db_models_homepage.py`

- [ ] **Step 1: 确认 `text` 导入**

查看 `src/service/db_models_homepage.py` 第 23-33 行的 `from sqlalchemy import (...)`。`Boolean` 已在（第 24 行）。检查 `text` 是否在列表里；**不在则加上**。

修改 import 块，确保含 `text`（按字母序插入，示例）：

```python
from sqlalchemy import (
    Boolean,
    # ... 其他已有 ...
    text,
)
```

- [ ] **Step 2: QASession 加字段**

在 `src/service/db_models_homepage.py` 的 `QASession` 类里，`archived_at` 字段块之后、`__table_args__` 之前，插入：

```python
    title_custom: Mapped[bool] = mapped_column(
        Boolean, server_default=text("0"), nullable=False, default=False
    )
    """标题是否被用户手动重命名过。
    False = 系统生成（截断 or 异步总结），可被异步总结覆盖；
    True  = 用户手动 rename 过，异步总结跳过、永不覆盖。
    设计：[[会话标题-重命名与智能总结-设计]] §2。"""
```

- [ ] **Step 3: 语法自检**

Run: `cd /Users/java/knowledge-engineering-auth && python3 -c "import ast; ast.parse(open('src/service/db_models_homepage.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/service/db_models_homepage.py
git commit -m "feat(db): QASession 加 title_custom 列（手动重命名保护标志）"
```

---

## Task 2: Alembic Migration

**Files:**
- Create: `alembic/versions/session_title_custom_v1.py`

- [ ] **Step 1: 写 migration**

新建 `alembic/versions/session_title_custom_v1.py`：

```python
"""qa_sessions 加 title_custom 列

Revision ID: session_title_custom_v1
Revises: qa_archive_v1
Create Date: 2026-05-16

设计：[[会话标题-重命名与智能总结-设计]] §2
存量行 server_default=0（False），历史会话标题保持现状不回溯。
"""
from alembic import op
import sqlalchemy as sa

# Alembic 版本标识
revision = "session_title_custom_v1"
down_revision = "qa_archive_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "qa_sessions",
        sa.Column(
            "title_custom",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("qa_sessions", "title_custom")
```

- [ ] **Step 2: 验证 migration 链（不实际 apply）**

Run: `cd /Users/java/knowledge-engineering-auth && python3 -c "import ast; ast.parse(open('alembic/versions/session_title_custom_v1.py').read()); print('syntax OK')"`
Expected: `syntax OK`

Run: `cd /Users/java/knowledge-engineering-auth && grep -l "down_revision = 'session_title_custom_v1'\|down_revision = \"session_title_custom_v1\"" alembic/versions/*.py | head -1`
Expected: 无输出（没有别的 migration 以此为 down_revision，即它是新 head）

- [ ] **Step 3: 实际 apply 到蓝队云库**

⚠️ 这步改生产库。先确认 SSH 隧道 :3307 活着（`nc -z localhost 3307`）。

Run: `cd /Users/java/knowledge-engineering-auth && source venv/bin/activate 2>/dev/null; alembic upgrade head`
Expected: 输出含 `Running upgrade qa_archive_v1 -> session_title_custom_v1`

Run: `cd /Users/java/knowledge-engineering-auth && alembic current`
Expected: 含 `session_title_custom_v1 (head)`

- [ ] **Step 4: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add alembic/versions/session_title_custom_v1.py
git commit -m "feat(migration): qa_sessions.title_custom 加列"
```

---

## Task 3: 标题总结 Prompt 常量

**Files:**
- Modify: `src/service/qa_engine/prompts.py`

- [ ] **Step 1: 看现有 prompt 常量风格**

Run: `cd /Users/java/knowledge-engineering-auth && grep -n "_SYSTEM\|_CHIT_CHAT_SYSTEM\|^_" src/service/qa_engine/prompts.py | head -5`
Expected: 看到如 `_CHIT_CHAT_SYSTEM = """..."""` 风格

- [ ] **Step 2: 追加 title 总结 prompt**

在 `src/service/qa_engine/prompts.py` 文件末尾追加：

```python
# ─── 会话标题总结（v1，2026-05-16）──────────────────────────────────────────
# 首轮问答后异步调用，用首个问题生成一个 ≤15 字的概括性标题。
# 设计：[[会话标题-重命名与智能总结-设计]] §3.2
_TITLE_SUMMARY_SYSTEM = (
    "你是会话标题生成器。请用不超过 15 个汉字概括用户问题的主题，"
    "直接输出标题本身：不要解释、不要引号、不要标点结尾、不要前缀。"
    "若问题是寒暄（你好/在吗等），输出「日常问候」。"
)
```

- [ ] **Step 3: 语法自检 + Commit**

Run: `cd /Users/java/knowledge-engineering-auth && python3 -c "from src.service.qa_engine.prompts import _TITLE_SUMMARY_SYSTEM; print(len(_TITLE_SUMMARY_SYSTEM))"`
Expected: 打印一个 > 0 的整数

```bash
git add src/service/qa_engine/prompts.py
git commit -m "feat(qa): 加会话标题总结 prompt 常量"
```

---

## Task 4: sse_emitter 加 on_title 回调 + session_title 事件

**Files:**
- Modify: `src/service/qa_engine/sse_emitter.py`

- [ ] **Step 1: 加 OnTitleCallback 类型**

在 `src/service/qa_engine/sse_emitter.py` 现有 `OnCompleteCallback = Callable[...]` 定义之后，追加：

```python
# on_title 回调：done 之后调用，返回新标题（str）或 None（跳过/失败）。
# router 用它来：判断是否首轮 + 未被手动改 → 调 LLM 总结 → UPDATE+commit DB → 返回标题。
OnTitleCallback = Callable[
    [],
    Awaitable[str | None],
]
```

- [ ] **Step 2: stream_qa_answer 签名加参数**

在 `stream_qa_answer` 的关键字参数里，`on_complete: OnCompleteCallback | None = None,` 之后加一行：

```python
    on_title: OnTitleCallback | None = None,
```

并在 docstring 的 Args 里补一句：
```
        on_title: done 之后调用；返回非空 str 时额外 emit 一个 session_title 事件。
```

- [ ] **Step 3: done 之后 emit session_title**

找到文件里 `# 7. done` 注释 + `yield format_sse("done", {` 那一段。在那个 `yield format_sse("done", {...})` **整段结束之后**（done 的 dict 闭合 `})` 之后），追加：

```python

    # 8. session_title（v1，2026-05-16）：仅当 router 传了 on_title 且返回非空
    # 设计：[[会话标题-重命名与智能总结-设计]] §3.2
    # 注意：on_title 内部已先 commit DB（DB 是 source of truth），
    # 这里 emit 失败（客户端断开）也无妨——下次进会话能看到新标题。
    if on_title is not None:
        try:
            new_title = await on_title()
            if new_title:
                yield format_sse("session_title", {
                    "session_id": session_id,
                    "title": new_title,
                })
        except Exception:
            # 静默降级：标题总结是辅助功能，绝不影响主流程
            pass
```

- [ ] **Step 4: 语法自检**

Run: `cd /Users/java/knowledge-engineering-auth && python3 -c "import ast; ast.parse(open('src/service/qa_engine/sse_emitter.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add src/service/qa_engine/sse_emitter.py
git commit -m "feat(sse): stream_qa_answer 加 on_title 回调 + done 后 session_title 事件"
```

---

## Task 5: qa_router rename 端点（TDD）

**Files:**
- Modify: `tests/test_auth/test_qa_session_router.py`
- Modify: `src/service/qa_router.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth/test_qa_session_router.py` 末尾追加（沿用文件已有 fixtures：`client`, `auth_headers`, 建 project/session 的 helper —— 阅读文件前 80 行确认 fixture 名后套用；下面用占位 fixture 名 `client` / `auth_headers` / `seed_session`，按实际改）：

```python
class TestRenameSession:
    """PATCH /projects/{pid}/qa/sessions/{sid} 重命名。"""

    @pytest.mark.asyncio
    async def test_rename_ok_sets_title_custom(self, client, auth_headers, seed_session):
        pid, sid = seed_session  # helper 返回 (project_id, session_id)
        r = client.patch(
            f"/projects/{pid}/qa/sessions/{sid}",
            json={"title": "我的自定义标题"},
            headers=auth_headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["title"] == "我的自定义标题"
        assert body["title_custom"] is True

    @pytest.mark.asyncio
    async def test_rename_empty_title_400(self, client, auth_headers, seed_session):
        pid, sid = seed_session
        r = client.patch(
            f"/projects/{pid}/qa/sessions/{sid}",
            json={"title": "   "},
            headers=auth_headers,
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_rename_too_long_400(self, client, auth_headers, seed_session):
        pid, sid = seed_session
        r = client.patch(
            f"/projects/{pid}/qa/sessions/{sid}",
            json={"title": "x" * 101},
            headers=auth_headers,
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_rename_unknown_session_404(self, client, auth_headers, seed_session):
        pid, _ = seed_session
        r = client.patch(
            f"/projects/{pid}/qa/sessions/sess_doesnotexist",
            json={"title": "x"},
            headers=auth_headers,
        )
        assert r.status_code == 404
```

⚠️ 实施者：先 `head -90 tests/test_auth/test_qa_session_router.py` 看真实 fixture 名（client / headers / 如何 seed project+session），把上面占位名替换成真名。归档拒绝的 case（409）放 Task 6。

- [ ] **Step 2: 跑测试，确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && python3 -m pytest tests/test_auth/test_qa_session_router.py::TestRenameSession -x -q`
Expected: FAIL（404/405，因为 PATCH 端点还不存在）

- [ ] **Step 3: 加 rename 端点**

在 `src/service/qa_router.py`，现有 DELETE session 端点（`@router.delete("/sessions/{session_id}", ...)` ~line 405）**之后**，加：

```python
from pydantic import BaseModel  # 若文件顶部没有则在顶部 import 区加


class RenameSessionBody(BaseModel):
    title: str


@router.patch(
    "/sessions/{session_id}",
    dependencies=[Depends(require_project_role("reporter"))],
)
async def rename_session(
    project_id: str,
    session_id: str,
    body: RenameSessionBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """重命名会话。置位 title_custom=True，异步总结将永不覆盖。

    设计：[[会话标题-重命名与智能总结-设计]] §3.1
    """
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    if len(title) > 100:
        raise HTTPException(status_code=400, detail="标题不能超过 100 字")

    sess = await db.get(QASession, session_id)
    if not sess or sess.project_id != project_id or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    if sess.archived_at is not None:
        raise HTTPException(status_code=409, detail="已归档会话不可重命名")

    sess.title = title
    sess.title_custom = True
    await db.commit()

    return {"id": sess.id, "title": sess.title, "title_custom": sess.title_custom}
```

⚠️ 实施者：核对 `qa_router.py` 顶部已有的 import 名——`HTTPException`/`Depends`/`get_db`/`get_current_user`/`require_project_role`/`AsyncSession`/`User`/`QASession` 应该都已 import（其他端点在用）。`BaseModel` 若没 import 则在顶部加 `from pydantic import BaseModel`。`get_current_user` 的真实依赖名以文件现有端点为准（可能叫 `user` 注入方式不同，照抄同文件其他端点的签名模式）。

- [ ] **Step 4: 跑测试，确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && python3 -m pytest tests/test_auth/test_qa_session_router.py::TestRenameSession -x -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/service/qa_router.py tests/test_auth/test_qa_session_router.py
git commit -m "feat(qa): PATCH 会话重命名端点（置 title_custom，归档拒绝，TDD 4 测试）"
```

---

## Task 6: rename 归档拒绝测试（补 409 case）

**Files:**
- Modify: `tests/test_auth/test_qa_session_router.py`

- [ ] **Step 1: 加 409 测试**

在 `TestRenameSession` 类里加（用文件已有的归档 helper / 直接 set archived_at）：

```python
    @pytest.mark.asyncio
    async def test_rename_archived_session_409(self, client, auth_headers, seed_session, db_session):
        pid, sid = seed_session
        # 直接把 session 标记归档
        from datetime import datetime
        from src.service.db_models_homepage import QASession
        from sqlalchemy import select
        s = (await db_session.execute(
            select(QASession).where(QASession.id == sid))).scalar_one()
        s.archived_at = datetime.utcnow()
        await db_session.commit()

        r = client.patch(
            f"/projects/{pid}/qa/sessions/{sid}",
            json={"title": "改个名"},
            headers=auth_headers,
        )
        assert r.status_code == 409
```

⚠️ 实施者：`db_session` fixture 名以文件实际为准（看其他测试怎么拿 async session）。

- [ ] **Step 2: 跑测试**

Run: `cd /Users/java/knowledge-engineering-auth && python3 -m pytest tests/test_auth/test_qa_session_router.py::TestRenameSession -x -q`
Expected: 5 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth/test_qa_session_router.py
git commit -m "test(qa): rename 归档会话 409 case"
```

---

## Task 7: qa_router 接 generate_title 回调（TDD）

**Files:**
- Create: `tests/test_auth/test_qa_session_title.py`
- Modify: `src/service/qa_router.py`

- [ ] **Step 1: 写失败测试（标题总结逻辑）**

新建 `tests/test_auth/test_qa_session_title.py`。这里测的是「generate_title 回调」的纯逻辑——抽成可单测的函数 `_make_title_generator`（Task 7 Step 3 会实现）。

```python
"""异步会话标题总结逻辑测试。
设计：[[会话标题-重命名与智能总结-设计]] §3.2, §6.1
"""
import pytest

from src.service.qa_router import _make_title_generator


class _FakeLLM:
    def __init__(self, reply=None, raises=False):
        self._reply = reply
        self._raises = raises

    async def complete(self, *, system: str, user: str, **kw) -> str:
        if self._raises:
            raise RuntimeError("LLM down")
        return self._reply


class _FakeSession:
    def __init__(self, title_custom=False, archived_at=None):
        self.title = "你好 在吗"[:30]
        self.title_custom = title_custom
        self.archived_at = archived_at


class _FakeDB:
    def __init__(self, sess):
        self._sess = sess
        self.committed = False

    async def get(self, model, sid):
        return self._sess

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_title_generated_on_first_turn():
    sess = _FakeSession()
    db = _FakeDB(sess)
    gen = _make_title_generator(
        db=db, session_id="s1", question="杭州周末两天去哪玩",
        llm=_FakeLLM(reply="杭州周末游玩攻略"), is_new_session=True,
    )
    title = await gen()
    assert title == "杭州周末游玩攻略"
    assert sess.title == "杭州周末游玩攻略"
    assert sess.title_custom is False  # 系统生成，不置 custom
    assert db.committed is True


@pytest.mark.asyncio
async def test_skip_when_not_new_session():
    sess = _FakeSession()
    db = _FakeDB(sess)
    gen = _make_title_generator(
        db=db, session_id="s1", question="继续问",
        llm=_FakeLLM(reply="不该用到"), is_new_session=False,
    )
    assert await gen() is None
    assert db.committed is False


@pytest.mark.asyncio
async def test_skip_when_title_custom():
    sess = _FakeSession(title_custom=True)
    db = _FakeDB(sess)
    gen = _make_title_generator(
        db=db, session_id="s1", question="问题",
        llm=_FakeLLM(reply="不该用到"), is_new_session=True,
    )
    assert await gen() is None
    assert db.committed is False


@pytest.mark.asyncio
async def test_llm_failure_returns_none_no_raise():
    sess = _FakeSession()
    db = _FakeDB(sess)
    gen = _make_title_generator(
        db=db, session_id="s1", question="问题",
        llm=_FakeLLM(raises=True), is_new_session=True,
    )
    assert await gen() is None       # 静默降级
    assert sess.title == "你好 在吗"[:30]  # 原临时标题不变


@pytest.mark.asyncio
async def test_overlong_title_truncated():
    sess = _FakeSession()
    db = _FakeDB(sess)
    gen = _make_title_generator(
        db=db, session_id="s1", question="问题",
        llm=_FakeLLM(reply="超" * 50), is_new_session=True,
    )
    title = await gen()
    assert title is not None
    assert len(title) <= 30
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && python3 -m pytest tests/test_auth/test_qa_session_title.py -x -q`
Expected: FAIL（`ImportError: cannot import name '_make_title_generator'`）

- [ ] **Step 3: 实现 `_make_title_generator` + 接入**

在 `src/service/qa_router.py`，加工厂函数（放在 rename 端点附近、模块级）：

```python
from src.service.qa_engine.prompts import _TITLE_SUMMARY_SYSTEM


def _make_title_generator(*, db, session_id, question, llm, is_new_session):
    """构造一个 on_title 回调（闭包）。

    仅当 is_new_session 且 session.title_custom==False 时调 LLM 总结，
    UPDATE+commit DB（先落库，DB 是 source of truth），返回新标题；
    否则 / 失败 返回 None（静默降级）。
    设计：[[会话标题-重命名与智能总结-设计]] §3.2
    """
    async def _gen():
        if not is_new_session:
            return None
        try:
            from src.service.db_models_homepage import QASession
            sess = await db.get(QASession, session_id)
            if sess is None or sess.title_custom:
                return None
            raw = await llm.complete(
                system=_TITLE_SUMMARY_SYSTEM,
                user=question,
            )
            title = (raw or "").strip().strip('"').strip("「」").strip()
            if not title:
                return None
            if len(title) > 30:
                title = title[:30]
            sess.title = title
            # title_custom 保持 False（系统生成）
            await db.commit()
            return title
        except Exception:
            return None  # 静默降级，绝不影响主流程

    return _gen
```

然后在 `StreamingResponse(stream_qa_answer(... on_complete=persist_messages))` 调用处，**加 `on_title` 参数**：

```python
            on_complete=persist_messages,
            on_title=_make_title_generator(
                db=db,
                session_id=session_id,
                question=body.question,
                llm=synthesizer.llm,
                is_new_session=is_new_session,
            ),
```

⚠️ 实施者：`synthesizer.llm` 是 LLMProviderProto（有 `async complete(system=, user=)`，见 synthesizer.py:30）。`db`/`session_id`/`is_new_session`/`body.question` 在该函数作用域已存在（line 172 `is_new_session`，line 184 `session_id`）。

- [ ] **Step 4: 跑测试，确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && python3 -m pytest tests/test_auth/test_qa_session_title.py -x -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/service/qa_router.py tests/test_auth/test_qa_session_title.py
git commit -m "feat(qa): 异步标题总结 generate_title 回调接入 SSE（TDD 5 测试）"
```

---

## Task 8: 全量回归 + 后端验证

**Files:** 无新文件

- [ ] **Step 1: 跑 qa 相关全量测试**

Run: `cd /Users/java/knowledge-engineering-auth && python3 -m pytest tests/test_auth/test_qa_session_router.py tests/test_auth/test_qa_session_title.py -q`
Expected: 全 passed（rename 5 + title 5 + 文件原有 session 测试不回归）

- [ ] **Step 2: 确认后端 --reload 已加载（uvicorn pid 61190 在跑）**

Run: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/openapi.json`
Expected: `200`

Run: `curl -s http://localhost:8000/openapi.json | python3 -c "import sys,json; p=json.load(sys.stdin)['paths']; print([k for k in p if 'sessions/{session_id}' in k and 'patch' in p[k]])"`
Expected: 含 rename 路径（PATCH 已注册）

- [ ] **Step 3: curl 实测 rename**

```bash
cd /tmp
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login -H "Content-Type: application/json" -d '{"username":"admin","password":"admin12345"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s -X PATCH "http://localhost:8000/projects/demo-system/qa/sessions/sess_b97b37db2055" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title":"重命名测试OK"}' -w "\nHTTP %{http_code}\n"
```
Expected: `{"id":...,"title":"重命名测试OK","title_custom":true}` + `HTTP 200`

- [ ] **Step 4: 无新 commit（验证用，前面已分任务 commit）**

---

## Self-Review 检查项（实施者跑完全部 task 后过一遍）

- [ ] 设计 §2（title_custom 列）→ Task 1+2 ✓
- [ ] 设计 §3.1（rename 端点：空/超长/越权/归档）→ Task 5+6 ✓
- [ ] 设计 §3.2（异步总结：首轮/custom 跳过/失败兜底/超长截断）→ Task 7 ✓
- [ ] 设计 §3.2（先 commit DB 再 emit SSE）→ Task 7 Step 3（_gen 内 `await db.commit()` 在 return 前）+ Task 4（sse emit 在 on_title 之后）✓
- [ ] 设计 §6.1 测试矩阵 → Task 5/6/7 覆盖 10 例 ✓

## Phase Definition of Done

- [ ] migration applied 到蓝队云（`alembic current` = session_title_custom_v1 head）
- [ ] rename 端点 5 测试 + 标题总结 5 测试全 pass
- [ ] curl 实测 rename 200 + title_custom=true
- [ ] 后端无回归（原 session 测试仍 pass）
- [ ] git 历史按 task 干净分段
