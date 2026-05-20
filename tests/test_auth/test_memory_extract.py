"""文件式记忆 S4：ReAct 抽取测试。设计：[[文件式记忆重构-设计]] §5。

fake LLM JSON + 真 MemoryFS(root=tmp_path) + 真 MemoryGen(fake LLM) +
S3 既有 _FakeWeaviateClient/_FakeEmbedder + 真 MemoryRecaller。沿用
tests/test_auth 既有 fake / tmp_path / @pytest.mark.asyncio 风格。
"""
# 导入 pytest（项目测试框架，pytest-asyncio 在 venv 中已安装）
import pytest

# 从 S1 vfs 导入：真 MemoryFS（tmp_path 注入做隔离）
from src.service.memory.vfs import MemoryFS
# 从 S2 memgen 导入：_split_frontmatter (T2 验证 frontmatter) + _render_frontmatter
# (T3 测试构造老 identity .md 时序列化 frontmatter)
from src.service.memory.memgen import _split_frontmatter, _render_frontmatter
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


def test_parse_react_json_strips_multiline_code_fence_dotall():
    """DOTALL 回归：pretty-printed 多行 JSON 包在 ```json ``` 内 — 必须靠 re.DOTALL
    才能让 `.*?` 跨行匹配。若未来 refactor 误删 re.DOTALL 标志，此测试会失败。
    """
    # 故意构造多行 pretty-printed JSON（内部含真实换行而非仅 fence 边界处的换行）
    # 没有 re.DOTALL，`.` 不匹配 \n → 整段 raw 不被识别为 fenced，json.loads
    # 直接处理含 ```json 头尾的原文本 → 抛 JSONDecodeError → ValueError
    raw = (
        '```json\n'
        '{\n'
        '  "memories": [\n'
        '    {"kind": "preference", "content": "用户偏好中文", "supersedes_kind": null}\n'
        '  ]\n'
        '}\n'
        '```'
    )
    out = _parse_react_json(raw)
    # 确认 DOTALL 生效：raw 被正确剥栅栏 + 解析为单 entry
    assert len(out) == 1
    assert out[0]["kind"] == "preference"
    assert out[0]["content"] == "用户偏好中文"
    assert out[0]["supersedes_kind"] is None


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
    # .md body 不变（覆盖写但 content 相同）；用 _split_frontmatter 比对 body 而非
    # 整个 .md，因为 frontmatter 中 created_at 是 _now_iso_z() 秒级精度 — 跨秒边界
    # 两次 extract 会得到不同字符串。S2 哈希链路只关心 body（src_hash = body 的 SHA-256），
    # 比 body 才是正确的不变量检查。
    second_md = await fs.read(uri)
    _, first_body = _split_frontmatter(first_md)
    _, second_body = _split_frontmatter(second_md)
    assert first_body == second_body
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
