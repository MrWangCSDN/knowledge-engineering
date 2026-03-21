from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol


class InterpretationProgressRepository(Protocol):
    """解读进度的持久化仓储接口（可替换文件/数据库/缓存等后端）。"""

    def load(self, sf_path: str) -> dict:
        """直接读取上次进度（不保证数据来自真实后端）。"""

    def get(self, sf_path: str, config_path: str) -> tuple[dict, str]:
        """读取并返回 (进度 dict, 来源说明)。"""

    def save(
        self,
        sf_path: str,
        tech_done: int,
        tech_total: int,
        biz_done: int,
        biz_total: int,
    ) -> None:
        """保存本次（累计）进度。"""


class InMemoryInterpretationProgressRepository(InterpretationProgressRepository):
    """内存后端：主要用于测试/调试。"""

    def __init__(self):
        self._data: dict[str, dict] = {}

    def _key(self, sf_path: str) -> str:
        try:
            return str(Path(sf_path).resolve())
        except Exception:
            return sf_path

    def load(self, sf_path: str) -> dict:
        return self._data.get(self._key(sf_path), {}) or {}

    def get(self, sf_path: str, config_path: str) -> tuple[dict, str]:
        # config_path 目前不使用（仅为接口一致性）
        return self.load(sf_path), "（来自内存，非真实后端）"

    def save(
        self,
        sf_path: str,
        tech_done: int,
        tech_total: int,
        biz_done: int,
        biz_total: int,
    ) -> None:
        key = self._key(sf_path)
        existing = self._data.get(key, {}) or {}
        tech = existing.get("tech", {}) or {}
        biz = existing.get("biz", {}) or {}
        if tech_total > 0:
            tech = {"done": tech_done, "total": tech_total}
        if biz_total > 0:
            biz = {"done": biz_done, "total": biz_total}
        self._data[key] = {"tech": tech, "biz": biz}

