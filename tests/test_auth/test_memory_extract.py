"""文件式记忆 S4：ReAct 抽取测试。设计：[[文件式记忆重构-设计]] §5。

fake LLM JSON + 真 MemoryFS(root=tmp_path) + 真 MemoryGen(fake LLM) +
S3 既有 _FakeWeaviateClient/_FakeEmbedder + 真 MemoryRecaller。沿用
tests/test_auth 既有 fake / tmp_path / @pytest.mark.asyncio 风格。
"""
# 导入 pytest（项目测试框架，pytest-asyncio 在 venv 中已安装）
import pytest

# 从 S1 vfs 导入：真 MemoryFS（tmp_path 注入做隔离）
from src.service.memory.vfs import MemoryFS
# 从 S2 memgen 导入：frontmatter 工具与哈希函数（S4 测试用来构造/检查 .md）
from src.service.memory.memgen import (
    _split_frontmatter,           # 拆 frontmatter / body
    _render_frontmatter,           # 序列化 frontmatter
    _sha256_hex,                   # 字符串 → SHA-256 hex
    _ABSTRACT_SUFFIX,              # ".abstract.md"
    _OVERVIEW_NAME,                # ".overview.md"
)
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
