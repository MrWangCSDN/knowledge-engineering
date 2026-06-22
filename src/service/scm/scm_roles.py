# src/service/scm/scm_roles.py
"""SCM 原生角色 → 内部 ScmRole 三档映射（纯函数）。设计 §4.1。"""
from __future__ import annotations

from typing import Optional

from src.service.scm.base import ScmRole

_GH_BIND = {"admin", "maintain"}
_GH_QUERY = {"write", "triage", "read"}


def github_role_to_scm(role_name: Optional[str], permission: Optional[str] = None) -> ScmRole:
    """GitHub collaborators permission API：优先内建 role_name；自定义 org 角色回退 legacy permission。"""
    rn = (role_name or "").lower()
    if rn in _GH_BIND:
        return ScmRole.CAN_BIND
    if rn in _GH_QUERY:
        return ScmRole.CAN_QUERY
    # role_name 可能是自定义 org 角色（非内建 5 值）→ 回退稳定的 legacy permission(admin/write/read/none)
    pm = (permission or "").lower()
    if pm == "admin":
        return ScmRole.CAN_BIND
    if pm in ("write", "read"):
        return ScmRole.CAN_QUERY
    return ScmRole.NOT_VISIBLE


def gitlab_access_level_to_scm(level: int) -> ScmRole:
    """GitLab access_level：50 Owner/40 Maintainer/30 Developer/20 Reporter/10 Guest。"""
    if level >= 40:
        return ScmRole.CAN_BIND
    if level >= 20:
        return ScmRole.CAN_QUERY   # Guest=10 < 20 → NOT_VISIBLE（Guest-trap）
    return ScmRole.NOT_VISIBLE
