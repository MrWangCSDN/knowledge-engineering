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
        # 仅按「文件 L0」计数/记录：目录聚合输入恒以 "## " 开头（_gen_dir_l0_l1
        # 的 joined = "## {k}\n{v}..." ），跳过不计；Task 3 步骤②起 regenerate
        # 含目录 LLM 调用，本计数器保留对 Task 2 文件 L0 用例「llm.calls==1」
        # 等断言的语义不变。目录 L0/L1 行为由 Task 3 的 _RoutingLLM 覆盖。
        # 边界：若未来用例的记忆正文本身以「## 」起首（Markdown 标题），会被
        # 此启发式误判为目录调用 → 此类场景改用 _RoutingLLM 或新增 fake。
        if user.startswith("## "):
            return self.ret
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
    # 首个 L1 = 最深目录（identity）的 L1：两子项 name/role 在其聚合输入中完整呈现；
    # _RoutingLLM 截断 user[:40] 会令更浅祖先的 L1 user 多级传播后被截掉「## role」
    # 末尾字符，故此处显式取最深一级（即首个）L1 调用。
    l1_user = [u for tag, u in llm.calls if tag == "L1"][0]
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


@pytest.mark.asyncio
async def test_dir_corrupt_yaml_frontmatter_self_heals(tmp_path):
    fs = _fs(tmp_path)
    llm = _RoutingLLM()
    gen = MemoryGen(llm)
    # 模拟 partial-write 崩溃 / 手工误改 / S6 迁移 bug：目录 .abstract.md
    # 的 frontmatter YAML 损坏。若 _split_frontmatter 不吞 YAMLError，每轮
    # 都因解析抛错被隔离 → 永不被覆盖 = 自愈死锁（§3.3/§3.5）。
    uri = "ke://u/7/global/identity/name.md"
    dir_abs = "ke://u/7/global/identity/.abstract.md"
    await fs.write(uri, _mem("identity", "名字是李龙飞"))
    # 写入损坏 YAML（: : : 非法）
    await fs.write(dir_abs, "---\n: : :\n---\n破损内容\n")

    # 不抛错（YAMLError 吞为空 meta → 按 stale 重生）→ 文件被自愈覆盖
    await gen.regenerate(fs, [uri])
    meta, _ = _split_frontmatter(await fs.read(dir_abs))
    # 自愈成功：inputs_hash 已写入，损坏内容被覆盖
    assert "inputs_hash" in meta


@pytest.mark.asyncio
async def test_pathological_md_named_slug_does_not_collide_with_dir_l0(tmp_path):
    fs = _fs(tmp_path)
    llm = _FixedLLM("不应被聚合的病理摘要")
    gen = MemoryGen(llm)
    # 病理：末段恰为 ".md"（空 slug；S4 规范 kebab slug 非空，不会真实产生）
    # 若不防护：bad 的 abs_uri = "ke://u/7/g/identity/.abstract.md" 与该目录
    # 自身 L0 路径冲突，会被相互覆盖、inputs_hash 链锁震荡。
    bad = "ke://u/7/global/identity/.md"
    good = "ke://u/7/global/identity/name.md"
    await fs.write(bad, _mem("identity", "病理：空 slug 的异常体"))
    await fs.write(good, _mem("identity", "名字是李龙飞"))

    await gen.regenerate(fs, [bad, good])

    # 病理被 _is_memory_file 与 dir-loop 双重拒收 → 不产生独立 sibling abstract
    # （注：dir_abs 是目录自身 L0，独立由步骤②生成；这里检查它聚合的是 good 而非 bad）
    assert await fs.exists("ke://u/7/global/identity/name.abstract.md")
    dir_abs = "ke://u/7/global/identity/.abstract.md"
    assert await fs.exists(dir_abs)
    _meta, dir_body = _split_frontmatter(await fs.read(dir_abs))
    # 病理正文未被聚合进目录 L0 输入链
    assert "病理：空 slug 的异常体" not in dir_body
    # 仅 good 的文件 L0 被计入（_FixedLLM 已过滤目录调用）
    assert llm.calls == 1
