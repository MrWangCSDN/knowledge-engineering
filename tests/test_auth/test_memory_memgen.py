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


def test_split_frontmatter_crlf_normalized():
    # CRLF 源（S4/S6 迁移/外部撰写可能产生）：frontmatter 仍被正确探测，
    # 且返回正文行尾归一为 \n（保证 Task 2 的 src_hash 跨行尾稳定）
    crlf = "---\r\nsrc_hash: deadbeef\r\n---\r\n用户的名字是李龙飞\r\n"
    meta, body = _split_frontmatter(crlf)
    assert meta == {"src_hash": "deadbeef"}
    assert body == "用户的名字是李龙飞\n"
    assert "\r" not in body


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


@pytest.mark.asyncio
async def test_file_l0_empty_llm_response_not_persisted(tmp_path, caplog):
    fs = _fs(tmp_path)
    gen = MemoryGen(_FixedLLM("   \n  "))             # 纯空白 LLM 响应
    uri = "ke://u/7/global/identity/user-name.md"
    await fs.write(uri, _MEM_FM)

    import logging
    with caplog.at_level(logging.DEBUG, logger="src.service.memory.memgen"):
        await gen.regenerate(fs, [uri])
    # 空响应不得固化为空 .abstract.md（否则 src_hash 命中源正文→永久跳过=粘滞坏态）
    assert not await fs.exists("ke://u/7/global/identity/user-name.abstract.md")
    # 经单条目失败隔离记 debug、不抛出（下轮可重试自愈）
    assert any("file L0 failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_file_l0_legacy_unquoted_numeric_src_hash_no_crash_regenerates(tmp_path):
    fs = _fs(tmp_path)
    llm = _FixedLLM("新摘要")
    gen = MemoryGen(llm)
    uri = "ke://u/7/global/identity/user-name.md"
    abs_uri = "ke://u/7/global/identity/user-name.abstract.md"
    await fs.write(uri, _MEM_FM)
    # 模拟 S6 迁移/手写：未加引号纯数字 src_hash → yaml.safe_load 解析为 int
    await fs.write(abs_uri, "---\nsrc_hash: 12345\n---\n旧摘要\n")

    # str() 防御保证 int 不让比较抛错；12345 ≠ 真 hex 哈希 → 重生（自愈）
    await gen.regenerate(fs, [uri])
    meta, body = _split_frontmatter(await fs.read(abs_uri))
    assert str(meta["src_hash"]) == _sha256_hex("用户的名字是李龙飞\n")
    assert "新摘要" in body
    assert llm.calls == 1
