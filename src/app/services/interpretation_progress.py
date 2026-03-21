"""解读进度持久化服务：文件与 Weaviate 双源。"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from src.core.domain_enums import InterpretPhase
from src.core.paths import interpretation_progress_path
from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_BUSINESS_INTERPRETATION,
    DEFAULT_COLLECTION_METHOD_INTERPRETATION,
    DEFAULT_WEAVIATE_HTTP_URL,
)
from src.persistence.repositories import InterpretationProgressRepository


class InterpretationProgressService(InterpretationProgressRepository):
    """解读进度加载、保存与 Weaviate 查询。"""

    def __init__(self, root: Path | None = None, get_weaviate_progress: Callable | None = None):
        self._root = root or Path(__file__).resolve().parents[3]
        self._progress_file = interpretation_progress_path(self._root)
        self._get_weaviate_progress = get_weaviate_progress

    def load(self, sf_path: str) -> dict:
        """按 structure_facts 路径读取上次解读进度。"""
        try:
            if self._progress_file.exists():
                data = json.loads(self._progress_file.read_text(encoding="utf-8"))
                key = str(Path(sf_path).resolve())
                return data.get(key, {}) or data.get(sf_path, {}) or {}
        except Exception:
            pass
        return {}

    def get(self, sf_path: str, config_path: str) -> tuple[dict, str]:
        """
        返回 (进度 dict, 来源说明)。
        优先从 Weaviate 查询；若连接失败则回退到文件。
        """
        file_data = self.load(sf_path)
        weaviate_data: dict | None = None

        if self._get_weaviate_progress:
            try:
                cfg = Path(config_path)
                if not cfg.is_absolute():
                    cfg = self._root / cfg
                if cfg.exists():
                    w = self._get_weaviate_progress(str(cfg), sf_path)
                    if (
                        w.get(InterpretPhase.TECH.value, {}).get("total", 0)
                        or w.get(InterpretPhase.BIZ.value, {}).get("total", 0)
                    ) > 0:
                        weaviate_data = w
            except Exception as e:
                logging.getLogger(__name__).debug("Weaviate 进度查询失败，回退到文件: %s", e)

        if not weaviate_data:
            return file_data, "（来自文件，Weaviate 未连接）"

        merged = dict(weaviate_data)
        source_parts: list[str] = ["（来自 Weaviate）"]

        phase_diffs: list[str] = []

        def _cap_done(done: int, total: int) -> int:
            if total and total > 0:
                return min(int(done or 0), int(total))
            return int(done or 0)

        for phase in (InterpretPhase.TECH.value, InterpretPhase.BIZ.value):
            fd = (file_data.get(phase, {}) or {}).get("done", 0)
            ft = (file_data.get(phase, {}) or {}).get("total", 0)
            wd = (weaviate_data.get(phase, {}) or {}).get("done", 0)
            wt = (weaviate_data.get(phase, {}) or {}).get("total", 0)

            # Weaviate 的聚合计数可能存在短暂延迟；当文件已记录“更高 done”时，
            # 用文件值避免 UI 刷新后出现进度回退。
            if fd > wd and (ft > 0 or fd > 0):
                total = int(ft if ft > 0 else wt)
                done = _cap_done(fd, total)
                merged[phase] = {"done": done, "total": total}
                if int(wd) != done:
                    phase_diffs.append(f"{phase} {wd}->{done}")
            else:
                total = int(wt if wt > 0 else ft)
                done = _cap_done(wd, total)
                merged[phase] = {"done": done, "total": total}

        if phase_diffs:
            # 仍保留 Weaviate 的总量语义；只修正 done 的回退。
            source_parts = [f"（来自 Weaviate，部分以文件为准：{';'.join(phase_diffs)}；避免计数延迟回退）"]

        return merged, source_parts[0]

    def diagnose(self, sf_path: str, config_path: str, *, include_existing_keys: bool = True) -> dict:
        """
        返回解读进度诊断信息，便于定位刷新回退来源。
        包含：file/weaviate/merged/source。
        """
        file_data = self.load(sf_path) or {}
        weaviate_data: dict = {}
        source = "（来自文件，Weaviate 未连接）"
        merged = file_data

        existing_method_ids_count: int | None = None
        existing_biz_key_pairs_count: int | None = None

        if self._get_weaviate_progress:
            try:
                from src.pipeline.gateways import load_project_config
                from src.knowledge.weaviate_interpretation_store import WeaviateMethodInterpretStore
                from src.knowledge.weaviate_business_store import WeaviateBusinessInterpretStore

                cfg = Path(config_path)
                if not cfg.is_absolute():
                    cfg = self._root / cfg
                if cfg.exists():
                    w = self._get_weaviate_progress(str(cfg), sf_path) or {}
                    if (
                        w.get(InterpretPhase.TECH.value, {}).get("total", 0)
                        or w.get(InterpretPhase.BIZ.value, {}).get("total", 0)
                    ) > 0:
                        weaviate_data = w

                    # 诊断：断点续跑依赖 list_existing_* 的 key 集合，而 UI 进度依赖 count()。
                    # 运行中刷新时可能会频繁访问 Weaviate，默认跳过 key 列表计算，保证实时性。
                    if include_existing_keys:
                        try:
                            config = load_project_config(str(cfg))
                            k = config.knowledge
                            if (
                                k.method_interpretation.enabled
                                and k.vectordb_interpret.enabled
                                and k.vectordb_interpret.backend == "weaviate"
                            ):
                                tech_store = WeaviateMethodInterpretStore(
                                    url=k.vectordb_interpret.weaviate_url or DEFAULT_WEAVIATE_HTTP_URL,
                                    grpc_port=k.vectordb_interpret.weaviate_grpc_port,
                                    collection_name=k.vectordb_interpret.collection_name
                                    or DEFAULT_COLLECTION_METHOD_INTERPRETATION,
                                    dimension=k.vectordb_interpret.dimension,
                                    api_key=k.vectordb_interpret.weaviate_api_key,
                                )
                                try:
                                    existing_method_ids_count = len(tech_store.list_existing_method_ids())
                                finally:
                                    tech_store.close()
                            if (
                                k.business_interpretation.enabled
                                and k.vectordb_business.enabled
                                and k.vectordb_business.backend == "weaviate"
                            ):
                                biz_store = WeaviateBusinessInterpretStore(
                                    url=k.vectordb_business.weaviate_url or DEFAULT_WEAVIATE_HTTP_URL,
                                    grpc_port=k.vectordb_business.weaviate_grpc_port,
                                    collection_name=k.vectordb_business.collection_name
                                    or DEFAULT_COLLECTION_BUSINESS_INTERPRETATION,
                                    dimension=k.vectordb_business.dimension,
                                    api_key=k.vectordb_business.weaviate_api_key,
                                )
                                try:
                                    existing_biz_key_pairs_count = len(biz_store.list_existing_entity_level_pairs())
                                finally:
                                    biz_store.close()
                        except Exception:
                            pass
            except Exception:
                pass

        if weaviate_data:
            merged, source = self.get(sf_path, config_path)

        return {
            "file": file_data,
            "weaviate": weaviate_data,
            "merged": merged,
            "source": source,
            "existing_method_ids_count": existing_method_ids_count,
            "existing_biz_key_pairs_count": existing_biz_key_pairs_count,
        }

    def save(
        self,
        sf_path: str,
        tech_done: int,
        tech_total: int,
        biz_done: int,
        biz_total: int,
    ) -> None:
        """将解读进度按 structure_facts 路径写入文件。"""
        try:
            self._progress_file.parent.mkdir(parents=True, exist_ok=True)
            key = str(Path(sf_path).resolve())
            data = {}
            if self._progress_file.exists():
                try:
                    data = json.loads(self._progress_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            existing = data.get(key, {}) or data.get(sf_path, {})
            tech = existing.get(InterpretPhase.TECH.value, {})
            biz = existing.get(InterpretPhase.BIZ.value, {})
            if tech_total > 0:
                tech = {"done": tech_done, "total": tech_total}
            if biz_total > 0:
                biz = {"done": biz_done, "total": biz_total}
            data[key] = {
                InterpretPhase.TECH.value: tech,
                InterpretPhase.BIZ.value: biz,
            }
            self._progress_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logging.getLogger(__name__).warning("解读进度保存失败: %s", e)
