"""验证 Project Pydantic schemas（用于 /api/projects 路由）。

设计文档：[[首页设计]] §6.3
"""
import pytest
from pydantic import ValidationError

from src.service.project_models import (
    Project,
    ProjectStats,
    IndexingProgress,
    ProjectListResponse,
    ProjectCreateRequest,
)


# ───────── ProjectStats ─────────

def test_stats_default_values():
    s = ProjectStats()
    assert s.methods_count == 0
    assert s.classes_count == 0
    assert s.interpretation_progress == 0


def test_stats_progress_must_be_0_to_100():
    """0-100 范围校验。"""
    with pytest.raises(ValidationError):
        ProjectStats(interpretation_progress=101)
    with pytest.raises(ValidationError):
        ProjectStats(interpretation_progress=-1)
    # 边界值合法
    assert ProjectStats(interpretation_progress=0).interpretation_progress == 0
    assert ProjectStats(interpretation_progress=100).interpretation_progress == 100


# ───────── IndexingProgress ─────────

def test_indexing_progress_basic():
    p = IndexingProgress(phase="parsing", percent=45, eta_seconds=120)
    assert p.phase == "parsing"
    assert p.percent == 45


def test_indexing_progress_percent_range():
    with pytest.raises(ValidationError):
        IndexingProgress(phase="x", percent=200, eta_seconds=0)


# ───────── Project ─────────

def test_project_minimal():
    """status='ready' 时 indexing_progress 可缺省。"""
    p = Project(
        id="deposit-system",
        name="存款系统",
        status="ready",
        stats=ProjectStats(methods_count=100, classes_count=20, interpretation_progress=92),
        pipeline_at="2026-05-06T10:00:00Z",
    )
    assert p.id == "deposit-system"
    assert p.indexing_progress is None


def test_project_status_must_be_enum_value():
    """status 必须是 4 个合法值之一。"""
    with pytest.raises(ValidationError):
        Project(
            id="x",
            name="x",
            status="invalid",  # 不在 Literal 范围
            stats=ProjectStats(),
            pipeline_at=None,
        )


def test_project_id_min_length():
    """id 至少 1 字符。"""
    with pytest.raises(ValidationError):
        Project(
            id="",  # 空字符串
            name="x",
            status="ready",
            stats=ProjectStats(),
            pipeline_at=None,
        )


def test_project_indexing_with_progress():
    """status='indexing' 时可附带 indexing_progress。"""
    p = Project(
        id="x",
        name="x",
        status="indexing",
        stats=ProjectStats(),
        pipeline_at=None,
        indexing_progress=IndexingProgress(phase="parsing", percent=30, eta_seconds=60),
    )
    assert p.indexing_progress is not None
    assert p.indexing_progress.percent == 30


# ───────── ProjectListResponse ─────────

def test_project_list_response_empty():
    r = ProjectListResponse(projects=[])
    assert r.projects == []


def test_project_list_response_with_data():
    r = ProjectListResponse(projects=[
        Project(id="p1", name="P1", status="ready", stats=ProjectStats(), pipeline_at=None),
    ])
    assert len(r.projects) == 1


# ───────── ProjectCreateRequest ─────────

def test_create_request_valid_id():
    """合法 id：小写开头，可含数字、连字符。"""
    r = ProjectCreateRequest(id="deposit-system", name="存款系统")
    assert r.id == "deposit-system"


def test_create_request_id_pattern_rejected():
    """非法 id：大写、空格、特殊符号 → reject。"""
    bad_ids = ["DepositSystem", "deposit system", "deposit_system", "1deposit", "-deposit", "deposit-"]
    for bad in bad_ids:
        with pytest.raises(ValidationError):
            ProjectCreateRequest(id=bad, name="x")


def test_create_request_default_language_java():
    """language 默认 'java'。"""
    r = ProjectCreateRequest(id="abc", name="x")
    assert r.language == "java"


def test_create_request_id_min_2_chars():
    """id 至少 2 字符（regex `^[a-z][a-z0-9-]{0,62}[a-z0-9]$` 强制）。"""
    with pytest.raises(ValidationError):
        ProjectCreateRequest(id="x", name="x")  # 1 字符 reject
    # 2 字符 OK
    assert ProjectCreateRequest(id="ab", name="x").id == "ab"
