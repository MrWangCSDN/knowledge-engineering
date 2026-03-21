"""数据与触发层输出：统一抽象的「代码输入源」。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class FileItem(BaseModel):
    """单个文件在输入源中的描述。"""
    path: str  # 相对仓库根路径
    module_id: Optional[str] = None  # 所属模块/服务 ID
    language: Optional[str] = None  # 如 java, py


class ModuleItem(BaseModel):
    """模块/服务在输入源中的描述。"""
    id: str  # 如 mall-admin, mall-portal
    name: Optional[str] = None
    path: Optional[str] = None  # 相对根路径，多模块时有用
    business_domains: list[str] = Field(default_factory=list)  # 承载的业务域 ID 列表


class CodeInputSource(BaseModel):
    """数据与触发层输出：供结构层消费的代码输入源。"""
    repo_path: str  # 仓库根目录绝对路径
    version: Optional[str] = None  # 分支/标签/commit
    modules: list[ModuleItem] = Field(default_factory=list)  # 模块/服务列表
    files: list[FileItem] = Field(default_factory=list)  # 待分析文件列表（可为空表示全量）
    language: Optional[str] = None  # 主语言，如 java
    meta: dict = Field(default_factory=dict)  # JDK/依赖版本等

    def repo_root(self) -> Path:
        return Path(self.repo_path)

    def resolve_file_path(self, relative_path: str) -> Path:
        return self.repo_root() / relative_path
