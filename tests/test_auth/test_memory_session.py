"""文件式记忆 S5：SessionCompactor + read_session_summary 单测。
设计：[[文件式记忆重构-设计]] §6。
沿用 tests/test_auth 既有 fake + tmp_path + @pytest.mark.asyncio 风格；
fake LLM 模式同 test_memory_extract.py（鸭子 async complete）。
"""
from __future__ import annotations

import pytest

from src.service.memory.session import _summary_uri


def test_summary_uri_basic():
    """_summary_uri 拼正确的 ke:// URI（§6.2）。"""
    assert _summary_uri(7, "sess_abc") == "ke://u/7/session/sess_abc/summary.md"


def test_summary_uri_different_users_isolated():
    """不同 user_id 派生不同路径（S1 路径前缀隔离的基础）。"""
    u1 = _summary_uri(1, "s")
    u2 = _summary_uri(2, "s")
    assert u1 != u2
    assert "/u/1/" in u1 and "/u/2/" in u2


def test_summary_uri_different_sessions_isolated():
    """同 user 不同 session 派生不同路径。"""
    s1 = _summary_uri(7, "sess_a")
    s2 = _summary_uri(7, "sess_b")
    assert s1 != s2


from src.service.memory.session import read_session_summary
from src.service.memory.vfs import MemoryFS


@pytest.mark.asyncio
async def test_read_session_summary_not_exists_returns_empty(tmp_path):
    """summary.md 不存在 → 返 ""（与 recall_memory_block 同自包失败语义，§6.5）。"""
    # tmp_path 是 pytest 内置的临时目录 fixture；MemoryFS(root=...) 接收物理根
    fs = MemoryFS(root=str(tmp_path))
    # user 7 + sess_x 路径下没写过任何文件
    result = await read_session_summary(fs, user_id=7, session_id="sess_x")
    # 不存在 → 返 "" 让 composer 走零开销不注入路径
    assert result == ""


@pytest.mark.asyncio
async def test_read_session_summary_corrupt_frontmatter_returns_body(tmp_path):
    """frontmatter YAML 损坏但 body 可读 → 返裸 body（自愈优先，§6.5）。

    场景：S5 部署初期 / 手工编辑误 / partial write 崩溃产出半损坏文件；
    composer 不能因为 frontmatter 坏就丢 summary 内容（用户体验降级）。
    """
    fs = MemoryFS(root=str(tmp_path))
    # 手写一份「frontmatter YAML 损坏 + body 完整」的文件直接落盘
    # _split_frontmatter 见非法 YAML 容错为空 dict，body 仍正确返
    bad_yaml = "---\n: : : invalid yaml ::: \n---\n用户讨论 PaymentGateway。\n"
    # 直接写文件（绕开 _render_frontmatter，模拟外部损坏）
    await fs.write("ke://u/7/session/sess_x/summary.md", bad_yaml)

    result = await read_session_summary(fs, user_id=7, session_id="sess_x")
    # body 仍可读 → 返裸文本（去尾换行）
    assert result == "用户讨论 PaymentGateway。"


from src.service.memory.session import SessionCompactor

from dataclasses import dataclass
from typing import Any


@dataclass
class _FakeMessage:
    """fake QAMessage row（duck-type：role / content / msg_metadata / created_at）。

    设计：仅满足 SessionCompactor.compact step 1 select 之后的 ORM-like
    属性访问 + step 7 _extract_focus_entity_ids 需要的 msg_metadata 字段。
    """
    role: str
    content: str
    msg_metadata: dict | None = None
    created_at: Any = None


class _FakeMsgScalars:
    """fake .scalars() 返回值：仅暴露 .all() → list[_FakeMessage]。"""
    def __init__(self, rows: list[_FakeMessage]):
        self._rows = rows

    def all(self) -> list[_FakeMessage]:
        return self._rows


class _FakeMsgResult:
    """fake db.execute() 返回值：仅暴露 .scalars() → _FakeMsgScalars。"""
    def __init__(self, rows: list[_FakeMessage]):
        self._rows = rows

    def scalars(self) -> _FakeMsgScalars:
        return _FakeMsgScalars(self._rows)


class _FakeDB:
    """fake AsyncSession：仅响应 execute(select(QAMessage).where(...).order_by(...))。

    不实现 commit/add/flush；S5 fs 落盘不走 DB 写路径，DB 仅读 QAMessage（S6 才迁）。
    """
    def __init__(self, messages: list[_FakeMessage]):
        self._messages = messages

    async def execute(self, stmt) -> _FakeMsgResult:
        # stmt 是 SQLAlchemy select 表达式，我们不解析（fake 只服务 compact 的单条 select）
        # 返回固定 messages（每次测试单独构造 _FakeDB(messages=...)）
        return _FakeMsgResult(self._messages)


def _msgs(*roles_contents: tuple[str, str]) -> list[_FakeMessage]:
    """便捷构造一批 fake message。

    用法：_msgs(("user", "你好"), ("assistant", "你好！"), ...)
    """
    return [_FakeMessage(role=r, content=c) for r, c in roles_contents]


class _FakeLLM:
    """记录 complete() 调用入参；返回固定的 fake summary 文本。

    与 test_memory_extract.py 既有 fake LLM 同形态（鸭子 async complete）。
    """
    def __init__(self, *, response: str = "fake summary 文本"):
        self.calls: list[dict] = []
        self.response = response

    async def complete(self, *, system: str, user: str, **kw) -> str:
        # 记录每次调用的 system / user 参数（断言用）
        self.calls.append({"system": system, "user": user})
        return self.response


def test_session_compactor_init_holds_llm():
    """SessionCompactor 仅持 llm（同 S4 MemoryExtractor，fs/db 走方法形参）。"""
    import inspect
    llm = _FakeLLM()
    compactor = SessionCompactor(llm)
    # 类型断言：实例确为 SessionCompactor
    assert isinstance(compactor, SessionCompactor)
    # compact 必须是 async 协程方法（contract 锁定，T2 不能改成同步）
    assert callable(compactor.compact)
    assert inspect.iscoroutinefunction(compactor.compact)


@pytest.mark.asyncio
async def test_compact_first_time_writes_new_summary(tmp_path):
    """首压路径：summary.md 不存在 + msg_count=6 + every_n=6（§6.7 场景 1）。

    fs.read 抛 MemoryNotFound → prev_turn_count=0 / prev_summary=""
    → 拼 convo 仅含【新增对话】段 → 调 LLM → 写新 summary.md。
    断言：frontmatter.turn_count == 6，body 含 LLM 输出。
    """
    fs = MemoryFS(root=str(tmp_path))
    db = _FakeDB(_msgs(*[("user", f"q{i}") if i % 2 == 0 else ("assistant", f"a{i}") for i in range(6)]))
    llm = _FakeLLM(response="用户讨论了 6 条消息，主要话题是 q0/q2/q4。")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_first", every_n_messages=6)

    # 断言文件已写
    uri = "ke://u/7/session/sess_first/summary.md"
    assert await fs.exists(uri)
    raw = await fs.read(uri)
    # frontmatter 解析回来验证 turn_count
    from src.service.memory.memgen import _split_frontmatter
    meta, body = _split_frontmatter(raw)
    assert meta["turn_count"] == 6
    # body 含 LLM 输出（去尾换行后）
    assert "用户讨论了 6 条消息" in body
    # LLM 被调用 1 次
    assert len(llm.calls) == 1
    # convo 不含【已有会话摘要】（首压）
    assert "【已有会话摘要】" not in llm.calls[0]["user"]
    # convo 含【新增对话】
    assert "【新增对话】" in llm.calls[0]["user"]


@pytest.mark.asyncio
async def test_compact_recursive_folds_prior_summary_and_only_new_msgs(tmp_path):
    """递归累积：summary.md 存在（turn_count=6）+ msg_count=12（§6.7 场景 2）。

    读 prev_summary + 取 messages[6:] 作 new_msgs → 拼 convo（含两段）
    → 调 LLM → 写新（turn_count=12）。
    断言：convo 含【已有会话摘要】+【新增对话】两段；仅 new_msgs 入 convo（旧 6 条不重摘）。
    """
    fs = MemoryFS(root=str(tmp_path))
    uri = "ke://u/7/session/sess_x/summary.md"

    # 预置旧 summary（turn_count=6, body="旧摘要：用户偏好哈密瓜"）
    from src.service.memory.memgen import _render_frontmatter
    prev_content = _render_frontmatter(
        {"turn_count": 6, "focus_entity_ids": [], "updated_at": "2026-05-21T10:00:00Z"},
        "旧摘要：用户偏好哈密瓜\n",
    )
    await fs.write(uri, prev_content)

    # 12 条消息：前 6 已被压缩进 prev_summary；后 6 是新增（带新事实）
    msgs = [_FakeMessage(role="user" if i % 2 == 0 else "assistant",
                          content=f"老消息 {i}" if i < 6 else f"新消息 {i}-讨论西瓜")
            for i in range(12)]
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="更新后摘要：用户偏好哈密瓜+西瓜")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

    # convo 包含两段
    assert len(llm.calls) == 1
    user_input = llm.calls[0]["user"]
    assert "【已有会话摘要】" in user_input
    assert "旧摘要：用户偏好哈密瓜" in user_input
    assert "【新增对话】" in user_input
    # 旧消息（前 6 条）不入 convo（已被 prev_summary 浓缩）
    assert "老消息 0" not in user_input
    assert "老消息 5" not in user_input
    # 新消息（后 6 条）入 convo — 严格验证 all 6 都在（防 messages[6:] 切片 off-by-one bug）
    assert all(f"新消息 {i}-讨论西瓜" in user_input for i in range(6, 12))

    # 写后 turn_count=12
    raw = await fs.read(uri)
    from src.service.memory.memgen import _split_frontmatter
    meta, body = _split_frontmatter(raw)
    assert meta["turn_count"] == 12
    assert "更新后摘要" in body


@pytest.mark.asyncio
async def test_compact_skips_below_floor(tmp_path):
    """floor 守卫：msg_count=5 + every_n=6 + force=False（§6.7 场景 3）。

    早退（不读 fs，不调 LLM，不写）。
    """
    fs = MemoryFS(root=str(tmp_path))
    msgs = _msgs(*[("user", f"q{i}") for i in range(5)])  # 5 条
    db = _FakeDB(msgs)
    llm = _FakeLLM()
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

    # LLM 未调用 / 文件未写
    assert len(llm.calls) == 0
    assert not await fs.exists("ke://u/7/session/sess_x/summary.md")


@pytest.mark.asyncio
async def test_compact_skips_when_no_new_messages_since_last(tmp_path):
    """delta 守卫：summary.md 存在 turn_count=6 + msg_count=10 + every_n=6（场景 4）。

    10-6=4 < 6 → 早退（避免过阈后每轮压缩 = 成本 bug，对齐旧版守卫）。
    """
    fs = MemoryFS(root=str(tmp_path))
    uri = "ke://u/7/session/sess_x/summary.md"
    from src.service.memory.memgen import _render_frontmatter
    prev = _render_frontmatter(
        {"turn_count": 6, "focus_entity_ids": [], "updated_at": "2026-05-21T10:00:00Z"},
        "旧\n",
    )
    await fs.write(uri, prev)
    msgs = _msgs(*[("user", f"q{i}") for i in range(10)])  # 10 条
    db = _FakeDB(msgs)
    llm = _FakeLLM()
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

    # LLM 未调用
    assert len(llm.calls) == 0
    # 文件未被改写：raw 仍是旧内容
    raw = await fs.read(uri)
    assert "旧" in raw


@pytest.mark.asyncio
async def test_compact_force_bypasses_n_floor(tmp_path):
    """force=True 路径：msg_count=2 + 首压 + force=True（场景 5）。

    floor=2、min_delta=1 → 进 LLM 路径（首压：prev_turn_count=0）。
    """
    fs = MemoryFS(root=str(tmp_path))
    msgs = _msgs(("user", "短"), ("assistant", "回"))
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="强制压缩摘要")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6, force=True)

    assert len(llm.calls) == 1
    uri = "ke://u/7/session/sess_x/summary.md"
    raw = await fs.read(uri)
    from src.service.memory.memgen import _split_frontmatter
    meta, body = _split_frontmatter(raw)
    assert meta["turn_count"] == 2
    assert "强制压缩摘要" in body


@pytest.mark.asyncio
async def test_compact_llm_returns_empty_skips_write(tmp_path):
    """LLM 返空：mock LLM 返 "" → 早退 step 6（不写文件）（场景 6）。

    LLM 偶发返空（限流 / token bug）不应破坏既有 summary.md。
    """
    fs = MemoryFS(root=str(tmp_path))
    msgs = _msgs(*[("user", f"q{i}") for i in range(6)])
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="   ")  # 全空白 → strip 后变 "" → 早退
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

    # LLM 被调用 1 次（说明走到 step 6）
    assert len(llm.calls) == 1
    # 文件未写
    assert not await fs.exists("ke://u/7/session/sess_x/summary.md")


@pytest.mark.asyncio
async def test_compact_with_corrupt_frontmatter_self_heals(tmp_path):
    """frontmatter 损坏自愈：手写非法 YAML → prev_turn_count=0 → 重写干净文件（场景 7）。

    场景：S5 部署初期 / 手工编辑误 / partial write 崩溃产出半损坏文件。
    _split_frontmatter 容错为空 dict {} → tc 字段缺失 → prev_turn_count 维持 0
    → delta 守卫按首压路径走（msg_count >= floor + msg_count - 0 >= min_delta）
    → 重新压缩 → 写干净 frontmatter（与 S2 自愈同模式）。
    """
    fs = MemoryFS(root=str(tmp_path))
    uri = "ke://u/7/session/sess_x/summary.md"

    # 手写损坏 YAML 文件（绕开 _render_frontmatter）
    bad = "---\n: : : invalid yaml :::\n---\n旧 body\n"
    await fs.write(uri, bad)

    msgs = _msgs(*[("user", f"q{i}") for i in range(6)])
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="自愈后新摘要")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

    # LLM 被调用（说明自愈走通）
    assert len(llm.calls) == 1
    # 文件重写为干净 frontmatter
    raw = await fs.read(uri)
    from src.service.memory.memgen import _split_frontmatter
    meta, body = _split_frontmatter(raw)
    assert meta["turn_count"] == 6  # 新水位线
    assert "自愈后新摘要" in body


@pytest.mark.asyncio
async def test_compact_persists_focus_entity_ids(tmp_path):
    """focus_entity_ids 持久化：mock messages 末段含 cited_entities（§6.7 场景 8）。

    _extract_focus_entity_ids(messages[-12:]) 从 msg_metadata.cited_entities
    + entry_points 聚合（service.py 既有，S4/S5 共用）→ 写入 frontmatter。
    """
    fs = MemoryFS(root=str(tmp_path))
    # 末 3 条 assistant 消息带 cited_entities（聚合源）
    msgs = [
        _FakeMessage(role="user", content="q0"),
        _FakeMessage(role="assistant", content="a0"),
        _FakeMessage(role="user", content="q1"),
        _FakeMessage(role="assistant", content="a1",
                      msg_metadata={"cited_entities": ["ent_alpha"]}),
        _FakeMessage(role="user", content="q2"),
        _FakeMessage(role="assistant", content="a2",
                      msg_metadata={"cited_entities": ["ent_beta"], "entry_points": ["ent_gamma"]}),
    ]
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="摘要")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, db, user_id=7, session_id="sess_x", every_n_messages=6)

    # 文件已写
    uri = "ke://u/7/session/sess_x/summary.md"
    raw = await fs.read(uri)
    from src.service.memory.memgen import _split_frontmatter
    meta, body = _split_frontmatter(raw)
    # focus_entity_ids 按 _extract_focus_entity_ids 的首见顺序去重收集
    focus = meta.get("focus_entity_ids", [])
    assert "ent_alpha" in focus
    assert "ent_beta" in focus
    assert "ent_gamma" in focus


@pytest.mark.asyncio
async def test_compact_cross_tenant_isolation(tmp_path):
    """跨租户隔离：user_id=1 写 → user_id=2 读不到（§6.7 场景 9）。

    S1 路径前缀隔离自带，本测试是回归保险（防 _summary_uri / fs 误改导致泄漏）。
    """
    fs = MemoryFS(root=str(tmp_path))
    msgs = _msgs(*[("user", f"q{i}") for i in range(6)])
    db = _FakeDB(msgs)
    llm = _FakeLLM(response="user1 的会话摘要")
    compactor = SessionCompactor(llm)

    # user_id=1 写
    await compactor.compact(fs, db, user_id=1, session_id="sess_x", every_n_messages=6)

    # user_id=1 自己读得到
    r1 = await read_session_summary(fs, user_id=1, session_id="sess_x")
    assert "user1 的会话摘要" in r1

    # user_id=2 读同 session_id 拿不到（路径前缀不同）
    r2 = await read_session_summary(fs, user_id=2, session_id="sess_x")
    assert r2 == ""
