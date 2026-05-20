"""文件式记忆 S3：Weaviate 向量召回测试。设计：[[文件式记忆重构-设计]] §4。

fake embedder + fake weaviate + 真 MemoryFS(root=tmp_path)，沿用 tests/test_auth
既有 fake / tmp_path / @pytest.mark.asyncio 风格。
"""
# 导入 pytest（项目测试框架，pytest-asyncio 在 venv 中已安装）
import pytest

# 从 S1 vfs 导入：真 MemoryFS（其 root 由 tmp_path 注入做隔离）
from src.service.memory.vfs import MemoryFS
# 从 S2 memgen 导入：frontmatter 工具与哈希函数，S3 测试用来构造 .abstract.md 内容
from src.service.memory.memgen import (
    _split_frontmatter,           # 拆 frontmatter / body（CRLF 归一、YAMLError 自愈）
    _render_frontmatter,           # 用 PyYAML 序列化 frontmatter 拼回 markdown
    _sha256_hex,                   # 字符串 → SHA-256 hex（计算 src_hash / inputs_hash）
    _ABSTRACT_SUFFIX,              # ".abstract.md" 常量
    _OVERVIEW_NAME,                # ".overview.md" 常量
)
# 从被测模块导入（本 Task 实现）
from src.service.memory.recall import (
    MemoryRecaller,                # S3 主引擎（含 index_changed / recall_memory_block）
    MemoryL0Store,                 # Weaviate collection schema 子类
    _kind_of_uri,                  # helper：判定 uri 是 "file" / "dir" L0
    _overview_uri_for_dir_l0,      # helper：dir L0 uri → 同目录 overview uri
)


def _fs(tmp_path):
    """tests 通用 fixture：用 tmp_path 给 MemoryFS 提供隔离根目录。"""
    # MemoryFS 接受 str；pytest 的 tmp_path 是 pathlib.Path，str() 即可
    return MemoryFS(root=str(tmp_path))


# ── Task 1：纯函数 helpers ───────────────────────────────────────
def test_kind_of_uri_file_vs_dir():
    """判定 .abstract.md uri 是文件 L0（带 slug 前缀）还是目录 L0（裸 .abstract.md）。"""
    # 目录 L0：末段恰为 ".abstract.md"
    assert _kind_of_uri("ke://u/7/global/identity/.abstract.md") == "dir"
    # 文件 L0：末段为 "{slug}.abstract.md"
    assert _kind_of_uri("ke://u/7/global/identity/user-name.abstract.md") == "file"
    # 租户根目录 L0 也属于 "dir"（uri 末段仍是裸 ".abstract.md"）
    assert _kind_of_uri("ke://u/7/.abstract.md") == "dir"


def test_overview_uri_for_dir_l0():
    """目录 L0 uri → 同目录 .overview.md uri（用于 recall 时 fs.read L1 展开）。"""
    # 标准 identity 目录
    assert _overview_uri_for_dir_l0(
        "ke://u/7/global/identity/.abstract.md"
    ) == "ke://u/7/global/identity/.overview.md"
    # 租户根目录
    assert _overview_uri_for_dir_l0(
        "ke://u/7/.abstract.md"
    ) == "ke://u/7/.overview.md"


def test_memory_recaller_construction_accepts_embedder_and_client():
    """MemoryRecaller 构造注入 embedder + weaviate_client，便于 fake 测试。"""
    # 用 None 占位（本步不调任何方法、只验证签名）；后续测试用真 fake
    rec = MemoryRecaller(embedder=None, weaviate_client=None)
    # __init__ 仅保存引用，不应抛错
    assert rec._embedder is None
    assert rec._weaviate_client is None


def test_overview_uri_for_dir_l0_rejects_file_uri():
    """防御性断言：传入文件 L0 uri（不以 "/.abstract.md" 结尾）→ AssertionError。"""
    # 文件 L0：末段是 "user-name.abstract.md"（带 slug 前缀，不是裸 ".abstract.md"）
    # 此调用违反 _overview_uri_for_dir_l0 的契约，应被 assert 兜住
    with pytest.raises(AssertionError):
        _overview_uri_for_dir_l0("ke://u/7/global/identity/user-name.abstract.md")
