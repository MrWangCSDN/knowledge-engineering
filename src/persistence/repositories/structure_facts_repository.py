from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Protocol, Union

from src.core.paths import structure_facts_interpret_cache_path_from_config
from src.models.structure import StructureFacts

ConfigPath = Union[str, Path]
PathLike = Union[str, Path]


def default_structure_facts_cache_path(config_path: ConfigPath) -> Path:
    """项目默认缓存：项目根/out_ui/structure_facts_for_interpret.json（见 ``src.core.paths``）。"""
    return structure_facts_interpret_cache_path_from_config(config_path)


class StructureFactsRepository(Protocol):
    """结构事实的读写仓储接口（可替换存储后端）。"""

    def get_default_cache_path(self, config_path: ConfigPath) -> Path:
        """在未指定 structure_facts_json 时，返回默认缓存路径。"""

    def resolve_structure_facts_path(
        self, *, config_path: ConfigPath, structure_facts_json: Optional[PathLike] = None
    ) -> Path:
        """返回实际用于读取的 JSON 路径（可能为默认缓存，或用户指定文件）。"""

    def load(
        self, *, config_path: ConfigPath, structure_facts_json: Optional[PathLike] = None
    ) -> StructureFacts:
        """读取并解析 StructureFacts。"""

    def save(
        self,
        structure_facts: StructureFacts,
        *,
        config_path: ConfigPath,
        out_dir: Optional[PathLike] = None,
        write_cache: bool = False,
    ) -> None:
        """写出 structure_facts（可选写出调试输出与缓存）。"""


class FileStructureFactsRepository(StructureFactsRepository):
    """文件系统后端：输出 JSON 到 out_dir 与 out_ui 缓存。"""

    def get_default_cache_path(self, config_path: ConfigPath) -> Path:
        return default_structure_facts_cache_path(config_path)

    def resolve_structure_facts_path(
        self, *, config_path: ConfigPath, structure_facts_json: Optional[PathLike] = None
    ) -> Path:
        if structure_facts_json is None:
            return self.get_default_cache_path(config_path)
        path = Path(structure_facts_json)
        if not path.is_absolute():
            # 与旧逻辑保持一致：相对配置所在项目根（config/project.yaml -> repo 根）
            path = Path(config_path).resolve().parent.parent / path
        return path

    def load(
        self, *, config_path: ConfigPath, structure_facts_json: Optional[PathLike] = None
    ) -> StructureFacts:
        path = self.resolve_structure_facts_path(config_path=config_path, structure_facts_json=structure_facts_json)
        if not path.exists():
            raise FileNotFoundError(f"未找到结构事实缓存: {path}。请先完整运行一次流水线以生成缓存，或指定正确的 structure_facts JSON 路径。")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return StructureFacts.model_validate(raw)

    def save(
        self,
        structure_facts: StructureFacts,
        *,
        config_path: ConfigPath,
        out_dir: Optional[PathLike] = None,
        write_cache: bool = False,
    ) -> None:
        if out_dir is not None:
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "structure_facts.json").write_text(
                structure_facts.model_dump_json(indent=2, exclude_none=True),
                encoding="utf-8",
            )
        if write_cache:
            cache_path = self.get_default_cache_path(config_path)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            (cache_path).write_text(
                structure_facts.model_dump_json(indent=2, exclude_none=True),
                encoding="utf-8",
            )


class InMemoryStructureFactsRepository(StructureFactsRepository):
    """内存后端：主要用于测试/调试（不落盘）。"""

    def __init__(self):
        self._data: dict[str, StructureFacts] = {}

    def get_default_cache_path(self, config_path: ConfigPath) -> Path:
        return default_structure_facts_cache_path(config_path)

    def resolve_structure_facts_path(
        self, *, config_path: ConfigPath, structure_facts_json: Optional[PathLike] = None
    ) -> Path:
        # 复用文件后端的路径解析规则，保证键稳定
        return FileStructureFactsRepository().resolve_structure_facts_path(
            config_path=config_path, structure_facts_json=structure_facts_json
        )

    def load(
        self, *, config_path: ConfigPath, structure_facts_json: Optional[PathLike] = None
    ) -> StructureFacts:
        path = self.resolve_structure_facts_path(config_path=config_path, structure_facts_json=structure_facts_json)
        key = str(path.resolve())
        if key not in self._data:
            raise FileNotFoundError(f"未找到结构事实缓存（内存）：{path}")
        return self._data[key]

    def save(
        self,
        structure_facts: StructureFacts,
        *,
        config_path: ConfigPath,
        out_dir: Optional[PathLike] = None,
        write_cache: bool = False,
    ) -> None:
        # out_dir 在内存后端不做单独存储；只用 cache_path 作为主键。
        cache_path = self.get_default_cache_path(config_path)
        key = str(cache_path.resolve())
        self._data[key] = structure_facts

