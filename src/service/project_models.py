"""Project（工程）相关 Pydantic schemas（API 层）。

跟前端 src/types/project.ts 一一对应，确保前后端类型一致。

设计文档：[[首页设计]] §6.3
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ─── 工程状态枚举 ───────────────────────────────────────────────────────────

ProjectStatus = Literal["ready", "indexing", "partial", "failed"]
"""4 种工程状态：
- ready    💚 pipeline 跑完
- indexing 🟡 pipeline 正在跑（不可选）
- partial  🟠 解读未完成但已有数据
- failed   🔴 pipeline 报错
"""


# ─── 子结构 ─────────────────────────────────────────────────────────────────

class ProjectStats(BaseModel):
    """工程统计 — 在选择器下拉里展示。"""
    methods_count: int = 0
    classes_count: int = 0
    # ge=0, le=100 限定范围；超出会触发 Pydantic ValidationError
    interpretation_progress: int = Field(0, ge=0, le=100, description="解读完成百分比")


class IndexingProgress(BaseModel):
    """索引进度 — 仅 status='indexing' 时存在。"""
    phase: str = Field(..., description="parsing/embedding/interpreting 等")
    percent: int = Field(..., ge=0, le=100)
    eta_seconds: int = Field(0, ge=0, description="预计还需多少秒")


# ─── 工程主类型（响应模型） ────────────────────────────────────────────────

class Project(BaseModel):
    """工程对象 — GET /api/projects 列表项 + GET /api/projects/{id}。"""
    # min_length=1 防止传空字符串
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    status: ProjectStatus = "indexing"
    stats: ProjectStats
    # ISO 8601；indexing 时为 None
    pipeline_at: Optional[str] = None
    # 仅 indexing 时有值；其他状态都是 None
    indexing_progress: Optional[IndexingProgress] = None


class ProjectListResponse(BaseModel):
    """GET /api/projects 响应包装。"""
    projects: list[Project]


# ─── 创建请求（admin only） ────────────────────────────────────────────────

class ProjectCreateRequest(BaseModel):
    """POST /api/projects body — admin 才能调用。

    v1 主要给 CLI 工具用（ke_admin_create_project.py），前端不暴露。
    """
    # 严格的 id 格式：小写字母开头，2-64 字符，可含数字和连字符，不能以连字符结尾
    id: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$",
        description="工程 ID（如 'deposit-system'）",
    )
    name: str = Field(..., min_length=1, max_length=128)
    repo_url: Optional[str] = Field(None, max_length=512)
    language: str = Field("java", description="主语言；目前只支持 java")
