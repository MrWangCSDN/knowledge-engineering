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
