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


from src.service.memory.session import SessionCompactor, write_message_to_fs

from datetime import datetime, timezone, timedelta as _td


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
    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(6):
        role = "user" if i % 2 == 0 else "assistant"
        await write_message_to_fs(
            fs, user_id=7, session_id="sess_first", msg_id=f"msg_{i:03d}",
            role=role, content=f"q{i}" if role == "user" else f"a{i}",
            created_at=base + _td(seconds=i),
        )
    llm = _FakeLLM(response="用户讨论了 6 条消息，主要话题是 q0/q2/q4。")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, user_id=7, session_id="sess_first", every_n_messages=6)

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
    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(12):
        role = "user" if i % 2 == 0 else "assistant"
        content = f"老消息 {i}" if i < 6 else f"新消息 {i}-讨论西瓜"
        await write_message_to_fs(
            fs, user_id=7, session_id="sess_x", msg_id=f"msg_{i:03d}",
            role=role, content=content, created_at=base + _td(seconds=i),
        )
    llm = _FakeLLM(response="更新后摘要：用户偏好哈密瓜+西瓜")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, user_id=7, session_id="sess_x", every_n_messages=6)

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
    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        await write_message_to_fs(
            fs, user_id=7, session_id="sess_x", msg_id=f"msg_{i:03d}",
            role="user", content=f"q{i}", created_at=base + _td(seconds=i),
        )
    llm = _FakeLLM()
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, user_id=7, session_id="sess_x", every_n_messages=6)

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
    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(10):
        await write_message_to_fs(
            fs, user_id=7, session_id="sess_x", msg_id=f"msg_{i:03d}",
            role="user", content=f"q{i}", created_at=base + _td(seconds=i),
        )
    llm = _FakeLLM()
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, user_id=7, session_id="sess_x", every_n_messages=6)

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
    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_000",
        role="user", content="短", created_at=base,
    )
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_001",
        role="assistant", content="回", created_at=base + _td(seconds=1),
    )
    llm = _FakeLLM(response="强制压缩摘要")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, user_id=7, session_id="sess_x", every_n_messages=6, force=True)

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
    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(6):
        role = "user" if i % 2 == 0 else "assistant"
        await write_message_to_fs(
            fs, user_id=7, session_id="sess_x", msg_id=f"msg_{i:03d}",
            role=role, content=f"q{i}", created_at=base + _td(seconds=i),
        )
    llm = _FakeLLM(response="   ")  # 全空白 → strip 后变 "" → 早退
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, user_id=7, session_id="sess_x", every_n_messages=6)

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

    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(6):
        role = "user" if i % 2 == 0 else "assistant"
        await write_message_to_fs(
            fs, user_id=7, session_id="sess_x", msg_id=f"msg_{i:03d}",
            role=role, content=f"q{i}", created_at=base + _td(seconds=i),
        )
    llm = _FakeLLM(response="自愈后新摘要")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, user_id=7, session_id="sess_x", every_n_messages=6)

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
    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    # 末 3 条 assistant 消息带 cited_entities（聚合源）
    msg_specs = [
        ("user", "q0", None),
        ("assistant", "a0", None),
        ("user", "q1", None),
        ("assistant", "a1", {"cited_entities": ["ent_alpha"]}),
        ("user", "q2", None),
        ("assistant", "a2", {"cited_entities": ["ent_beta"], "entry_points": ["ent_gamma"]}),
    ]
    for i, (role, content, metadata) in enumerate(msg_specs):
        await write_message_to_fs(
            fs, user_id=7, session_id="sess_x", msg_id=f"msg_{i:03d}",
            role=role, content=content, msg_metadata=metadata,
            created_at=base + _td(seconds=i),
        )
    llm = _FakeLLM(response="摘要")
    compactor = SessionCompactor(llm)

    await compactor.compact(fs, user_id=7, session_id="sess_x", every_n_messages=6)

    # 文件已写
    uri = "ke://u/7/session/sess_x/summary.md"
    raw = await fs.read(uri)
    from src.service.memory.memgen import _split_frontmatter
    meta, body = _split_frontmatter(raw)
    # focus_entity_ids 按 _extract_focus_entity_ids 的首见顺序去重收集
    # 严格顺序断言：a1.cited_entities[0]=ent_alpha 先入；a2.cited_entities[0]=ent_beta 次；a2.entry_points[0]=ent_gamma 末
    focus = meta.get("focus_entity_ids", [])
    assert focus == ["ent_alpha", "ent_beta", "ent_gamma"]


@pytest.mark.asyncio
async def test_compact_cross_tenant_isolation(tmp_path):
    """跨租户隔离：user_id=1 写 → user_id=2 读不到（§6.7 场景 9）。

    S1 路径前缀隔离自带，本测试是回归保险（防 _summary_uri / fs 误改导致泄漏）。
    """
    fs = MemoryFS(root=str(tmp_path))
    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(6):
        await write_message_to_fs(
            fs, user_id=1, session_id="sess_x", msg_id=f"msg_{i:03d}",
            role="user", content=f"q{i}", created_at=base + _td(seconds=i),
        )
    llm = _FakeLLM(response="user1 的会话摘要")
    compactor = SessionCompactor(llm)

    # user_id=1 写
    await compactor.compact(fs, user_id=1, session_id="sess_x", every_n_messages=6)

    # user_id=1 自己读得到（严格 equality — read 路径 deterministic，body 就是 LLM 输出）
    r1 = await read_session_summary(fs, user_id=1, session_id="sess_x")
    assert r1 == "user1 的会话摘要"

    # user_id=2 读同 session_id 拿不到（路径前缀不同）
    r2 = await read_session_summary(fs, user_id=2, session_id="sess_x")
    assert r2 == ""


@pytest.mark.asyncio
async def test_compact_fs_write_failure_silently_logged(tmp_path, monkeypatch):
    """失败隔离：mock fs.write 抛 → compact 中层 catch → _log.debug → return（不抛）。

    §6.5 关键不变量：summary.md 缺失/损坏永不阻塞 SSE 流。
    """
    fs = MemoryFS(root=str(tmp_path))
    # 准备 messages（在 monkeypatch 前，fs.write 仍正常工作）
    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(6):
        role = "user" if i % 2 == 0 else "assistant"
        await write_message_to_fs(
            fs, user_id=7, session_id="sess_x", msg_id=f"msg_{i:03d}",
            role=role, content=f"q{i}", created_at=base + _td(seconds=i),
        )
    # 现在 monkeypatch fs.write 抛 — 影响后续 compact step 8 写 summary.md
    async def _explode(*args):
        raise OSError("simulated disk error")
    monkeypatch.setattr(fs, "write", _explode)

    llm = _FakeLLM(response="摘要")
    compactor = SessionCompactor(llm)

    # 关键断言：不抛 → compact 中层 catch 兜住
    # （pytest 默认 fail 在未捕获异常 → 不需要 try/except wrap）
    await compactor.compact(fs, user_id=7, session_id="sess_x", every_n_messages=6)

    # 选 LLM 调用计数而非 fs.exists 检查，因为前者强证算法走到 step 6（LLM 调用是 fs.write 的 precondition）；
    # fs.exists 仅证"文件不存在"，不区分"早退跳过 step 6/7/8"还是"step 6 跑了但 step 8 fs.write 中层 catch 兜住"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_compact_then_read_returns_body_strip_frontmatter(tmp_path):
    """端到端：compact 写 + read_session_summary 读 链路一致（§6.7 场景 13）。

    write 后 read 拿到的就是 LLM 输出的 body（去 frontmatter + strip）。
    复制粘贴 prev_summary 的语义一致性回归。
    """
    fs = MemoryFS(root=str(tmp_path))
    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(6):
        role = "user" if i % 2 == 0 else "assistant"
        await write_message_to_fs(
            fs, user_id=7, session_id="sess_e2e", msg_id=f"msg_{i:03d}",
            role=role, content=f"q{i}", created_at=base + _td(seconds=i),
        )
    llm = _FakeLLM(response="端到端摘要正文")
    compactor = SessionCompactor(llm)

    # 写
    await compactor.compact(fs, user_id=7, session_id="sess_e2e", every_n_messages=6)

    # 读
    body = await read_session_summary(fs, user_id=7, session_id="sess_e2e")

    # 链路一致：write 时 body=summary+"\n"，read 时 strip → 等于 LLM 输出原文
    assert body == "端到端摘要正文"


# ─── S6 T1: fs message helpers 单元测试（§7.9 场景 1-6） ─────────────

from src.service.memory.session import (
    _FsMessage, read_messages_for_session,
    _messages_dir_uri, _message_uri,
)


@pytest.mark.asyncio
async def test_write_message_to_fs_user(tmp_path):
    """write_message_to_fs 写 user 消息：frontmatter.role=user + body=content + created_at ISO（§7.9 场景 1）。"""
    fs = MemoryFS(root=str(tmp_path))
    ts = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_abc",
        role="user", content="帮我看 PaymentGateway", created_at=ts,
    )
    raw = await fs.read("ke://u/7/session/sess_x/messages/msg_abc.md")
    from src.service.memory.memgen import _split_frontmatter
    fm, body = _split_frontmatter(raw)
    assert fm["role"] == "user"
    assert fm["created_at"] == "2026-05-21T10:00:00Z"
    assert "sections" not in fm
    assert "msg_metadata" not in fm
    assert body.strip() == "帮我看 PaymentGateway"


@pytest.mark.asyncio
async def test_write_message_to_fs_assistant_with_sections_and_metadata(tmp_path):
    """write_message_to_fs 写 assistant + sections + msg_metadata（§7.9 场景 2）。"""
    fs = MemoryFS(root=str(tmp_path))
    ts = datetime(2026, 5, 21, 10, 0, 5, tzinfo=timezone.utc)
    sections = [
        {"type": "overview", "title": "概览", "content": "正文", "references": []},
    ]
    metadata = {
        "entry_points": ["PaymentGateway.charge"],
        "cited_entities": ["method:PaymentGateway.retry"],
        "token_usage": 1234,
    }
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_def",
        role="assistant", content=None,
        sections=sections, msg_metadata=metadata, created_at=ts,
    )
    raw = await fs.read("ke://u/7/session/sess_x/messages/msg_def.md")
    from src.service.memory.memgen import _split_frontmatter
    fm, body = _split_frontmatter(raw)
    assert fm["role"] == "assistant"
    assert fm["sections"] == sections
    assert fm["msg_metadata"] == metadata
    assert body.strip() == ""


@pytest.mark.asyncio
async def test_read_messages_for_session_dir_not_exists_returns_empty(tmp_path):
    """read_messages_for_session 目录不存在 → 返 []（§7.9 场景 3）。"""
    fs = MemoryFS(root=str(tmp_path))
    result = await read_messages_for_session(fs, user_id=7, session_id="sess_new")
    assert result == []


@pytest.mark.asyncio
async def test_read_messages_for_session_sorts_by_created_at(tmp_path):
    """read_messages_for_session 多文件按 created_at 升序返（§7.9 场景 4）。"""
    fs = MemoryFS(root=str(tmp_path))
    t1 = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 21, 10, 0, 5, tzinfo=timezone.utc)
    # msg_b 文件名字典序在 msg_a 之后，但写入时间更早
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_b",
        role="user", content="第一条", created_at=t1,
    )
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_a",
        role="assistant", content="第二条", created_at=t2,
    )
    result = await read_messages_for_session(fs, user_id=7, session_id="sess_x")
    assert len(result) == 2
    assert result[0].content == "第一条"
    assert result[1].content == "第二条"
    assert result[0].created_at < result[1].created_at


@pytest.mark.asyncio
async def test_read_messages_for_session_skips_corrupt_file(tmp_path):
    """read_messages_for_session 单文件损坏 → _log.debug 跳过，其他文件正常返（§7.9 场景 5）。"""
    fs = MemoryFS(root=str(tmp_path))
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_x", msg_id="msg_good",
        role="user", content="正常消息",
        created_at=datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc),
    )
    # 缺 role 字段的损坏文件
    bad_raw = "---\ncreated_at: \"2026-05-21T11:00:00Z\"\n---\n损坏消息\n"
    await fs.write("ke://u/7/session/sess_x/messages/msg_bad.md", bad_raw)

    result = await read_messages_for_session(fs, user_id=7, session_id="sess_x")
    assert len(result) == 1
    assert result[0].content == "正常消息"


def test_fs_message_duck_type_contract():
    """_FsMessage 鸭子契约：5 属性齐备 + created_at 是 datetime（§7.9 场景 6）。"""
    ts = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    m = _FsMessage(
        role="assistant",
        content="正文",
        msg_metadata={"cited_entities": ["e1"]},
        created_at=ts,
    )
    assert m.role == "assistant"
    assert m.content == "正文"
    assert m.msg_metadata == {"cited_entities": ["e1"]}
    assert isinstance(m.created_at, datetime)
    # sections 是 T2 加的第 5 字段，default None；不传时 dataclass 注入 None
    assert m.sections is None


@pytest.mark.asyncio
async def test_session_compactor_compact_with_fs_messages_end_to_end(tmp_path):
    """SessionCompactor.compact 改 fs source 端到端（§7.9 场景 7）。

    write 4 messages (2 user + 2 assistant) → compact → 验 fs summary.md：
    - frontmatter.turn_count == 4
    - LLM 输入含【新增对话】4 条
    """
    fs = MemoryFS(root=str(tmp_path))
    base = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    pairs = [
        ("msg_u1", "user", "q1"),
        ("msg_a1", "assistant", "a1"),
        ("msg_u2", "user", "q2"),
        ("msg_a2", "assistant", "a2"),
    ]
    for i, (mid, role, c) in enumerate(pairs):
        await write_message_to_fs(
            fs, user_id=7, session_id="sess_e2e", msg_id=mid,
            role=role, content=c, created_at=base + _td(seconds=i),
        )
    llm = _FakeLLM(response="4 条对话浓缩")
    compactor = SessionCompactor(llm)

    # 新签名：不传 db；force=True 让 floor=2 / min_delta=1 触发
    await compactor.compact(
        fs, user_id=7, session_id="sess_e2e", every_n_messages=6, force=True,
    )

    uri = "ke://u/7/session/sess_e2e/summary.md"
    raw = await fs.read(uri)
    from src.service.memory.memgen import _split_frontmatter
    meta, body = _split_frontmatter(raw)
    assert meta["turn_count"] == 4
    assert "4 条对话浓缩" in body
    assert len(llm.calls) == 1
    user_input = llm.calls[0]["user"]
    assert "【新增对话】" in user_input
    assert "[user] q1" in user_input
    assert "[assistant] a1" in user_input
    assert "[user] q2" in user_input
    assert "[assistant] a2" in user_input


@pytest.mark.asyncio
async def test_read_messages_for_session_skips_unparseable_iso_date(tmp_path):
    """read_messages_for_session 跳过 created_at 非法 ISO 文件（命中 outer except）。

    覆盖 fromisoformat ValueError 路径 — 与"缺 role"内层 skip 不同，是 outer
    except Exception 兜底的关键路径（M6 review fix 补足覆盖）。
    """
    fs = MemoryFS(root=str(tmp_path))
    # 正常文件
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_iso", msg_id="msg_good",
        role="user", content="正常",
        created_at=datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc),
    )
    # 非法 ISO 日期文件（fromisoformat 抛 ValueError）
    bad_raw = "---\nrole: user\ncreated_at: \"not-an-iso-date\"\n---\n损坏\n"
    await fs.write("ke://u/7/session/sess_iso/messages/msg_bad.md", bad_raw)

    result = await read_messages_for_session(fs, user_id=7, session_id="sess_iso")
    # 仅返正常那一条；fromisoformat 抛被 outer except 吃掉
    assert len(result) == 1
    assert result[0].content == "正常"


@pytest.mark.asyncio
async def test_write_message_normalizes_naive_to_utc(tmp_path):
    """write_message_to_fs naive datetime 归一为 UTC-aware（I1 review fix）。

    naive datetime → frontmatter.created_at 必带 Z 后缀；fromisoformat 回读
    必返 UTC-aware datetime → 混合 tz sort 不再抛 TypeError。
    """
    fs = MemoryFS(root=str(tmp_path))
    naive = datetime(2026, 5, 21, 10, 0, 0)  # 无 tzinfo
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_tz", msg_id="msg_naive",
        role="user", content="naive 输入", created_at=naive,
    )
    msgs = await read_messages_for_session(fs, user_id=7, session_id="sess_tz")
    assert len(msgs) == 1
    # 关键断言：回读 created_at 必是 UTC-aware（不是 naive）
    assert msgs[0].created_at.tzinfo is timezone.utc
    # 时间值不变（naive 视作 UTC，不平移）
    assert msgs[0].created_at == datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_write_message_normalizes_non_utc_to_utc(tmp_path):
    """write_message_to_fs 非 UTC tz-aware 归一为 UTC（I1 review fix）。

    输入 +08:00 时区 → 转 UTC（-8 小时）再写 ISO Z 字符串；防止本地时间被
    错标为 UTC 导致跨 client 时间显示错乱。
    """
    fs = MemoryFS(root=str(tmp_path))
    from datetime import timezone as _tz, timedelta
    shanghai_tz = _tz(timedelta(hours=8))
    sh_time = datetime(2026, 5, 21, 18, 0, 0, tzinfo=shanghai_tz)  # UTC 时间 10:00
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_tz2", msg_id="msg_sh",
        role="user", content="Shanghai 输入", created_at=sh_time,
    )
    msgs = await read_messages_for_session(fs, user_id=7, session_id="sess_tz2")
    # UTC 归一后时间 = 10:00 而非 18:00
    assert msgs[0].created_at == datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_read_messages_for_session_role_tiebreak_same_second(tmp_path):
    """同 created_at 时按 role tie-break（user 在前 assistant 在后）（M3 review fix）。

    模拟 persist_messages 同一调用写 user+assistant 同秒场景（strftime 截微秒
    导致同 created_at 字符串）→ sort 应稳定按 role tie-break，与旧 DB
    qa_router.py:463 case 同语义。
    """
    fs = MemoryFS(root=str(tmp_path))
    same_ts = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    # 先写 assistant（让 fs.ls 字典序倾向于把 assistant 排在前）
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_tie", msg_id="z_assistant",
        role="assistant", content="answer", created_at=same_ts,
    )
    await write_message_to_fs(
        fs, user_id=7, session_id="sess_tie", msg_id="a_user",
        role="user", content="question", created_at=same_ts,
    )
    result = await read_messages_for_session(fs, user_id=7, session_id="sess_tie")
    assert len(result) == 2
    # 同 created_at + role tie-break → user 在前 assistant 在后（不依赖文件名字典序）
    assert result[0].role == "user"
    assert result[1].role == "assistant"


@pytest.mark.asyncio
async def test_session_compactor_compact_signature_no_db(tmp_path):
    """SessionCompactor.compact 新签名不含 db 参数（§7.9 场景 8）。"""
    fs = MemoryFS(root=str(tmp_path))
    llm = _FakeLLM(response="摘要")
    compactor = SessionCompactor(llm)
    # 不传 db 参数 — S6 后正确签名
    await compactor.compact(
        fs, user_id=7, session_id="sess_sig", every_n_messages=6, force=False,
    )
    # msg_count=0 < floor=6 → 早退，文件未写
    assert not await fs.exists("ke://u/7/session/sess_sig/summary.md")
