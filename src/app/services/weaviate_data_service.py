"""Weaviate 数据服务：源码、技术解读、业务解读。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.core.language_defaults import DEFAULT_REPO_LANGUAGE
from src.core.weaviate_defaults import (
    DEFAULT_COLLECTION_BUSINESS_INTERPRETATION,
    DEFAULT_COLLECTION_CODE_ENTITY,
    DEFAULT_COLLECTION_METHOD_INTERPRETATION,
    DEFAULT_WEAVIATE_GRPC_PORT,
    DEFAULT_WEAVIATE_HTTP_URL,
)
from src.knowledge.factories import VectorStoreFactory
from src.knowledge.weaviate_interpretation_store import WeaviateMethodInterpretStore
from src.knowledge.weaviate_business_store import WeaviateBusinessInterpretStore


class WeaviateDataService:
    """从 Weaviate 获取方法源码、技术解读、业务解读。"""

    def __init__(self, config_loader, root: Path):
        self._load_config = config_loader
        self._root = root
        self._default_config_path = str(root / "config/project.yaml")

    def _get_config(self) -> dict:
        try:
            cfg = self._load_config(self._default_config_path)
            if cfg is None:
                return {}
            if hasattr(cfg, "model_dump"):
                return cfg.model_dump()
            return cfg or {}
        except Exception:
            return {}

    def code_highlight_language(self) -> str:
        """Streamlit ``st.code`` 语法高亮语言：随 ``repo.language``，缺省为 java。"""
        try:
            cfg = self._get_config()
            repo = cfg.get("repo") or {}
            lang = (repo.get("language") or DEFAULT_REPO_LANGUAGE).strip().lower()
            return lang or DEFAULT_REPO_LANGUAGE
        except Exception:
            return DEFAULT_REPO_LANGUAGE

    def fetch_method_snippet(self, entity_id: str) -> str:
        """从 vectordb-code 按 entity_id 取方法源码。"""
        vs = None
        try:
            cfg = self._get_config()
            vcfg = (cfg.get("knowledge") or {}).get("vectordb-code") or {}
            if not (vcfg.get("enabled") and vcfg.get("backend") == "weaviate"):
                return "// 未启用 Weaviate 源代码向量库（knowledge.vectordb-code）。"
            vs = VectorStoreFactory.create(
                vcfg.get("backend", "weaviate"),
                True,
                int(vcfg.get("dimension") or 64),
                allow_fallback_to_memory=bool(vcfg.get("allow_fallback_to_memory", False)),
                weaviate_url=vcfg.get("weaviate_url") or DEFAULT_WEAVIATE_HTTP_URL,
                weaviate_grpc_port=vcfg.get("weaviate_grpc_port") or DEFAULT_WEAVIATE_GRPC_PORT,
                collection_name=vcfg.get("collection_name") or DEFAULT_COLLECTION_CODE_ENTITY,
                weaviate_api_key=vcfg.get("weaviate_api_key") or None,
            )
            if vs is None:
                return "// 未启用 Weaviate 源代码向量库（knowledge.vectordb-code）。"
            obj = vs.get_by_entity_id(entity_id)
            return (obj or {}).get("code_snippet") or "// 未在向量库中找到该方法的源码片段"
        except Exception as e:
            return f"// 从 Weaviate 获取源码失败: {e}"
        finally:
            if vs is not None and hasattr(vs, "close"):
                try:
                    vs.close()
                except Exception:
                    pass

    def fetch_method_interpretation(self, entity_id: str) -> Optional[dict]:
        """从 vectordb-interpret 按 method_entity_id 取技术解读。"""
        store = None
        try:
            cfg = self._get_config()
            icfg = (cfg.get("knowledge") or {}).get("vectordb-interpret") or {}
            if not (icfg.get("enabled") and icfg.get("backend") == "weaviate"):
                return None
            store = WeaviateMethodInterpretStore(
                url=icfg.get("weaviate_url") or DEFAULT_WEAVIATE_HTTP_URL,
                grpc_port=int(icfg.get("weaviate_grpc_port") or DEFAULT_WEAVIATE_GRPC_PORT),
                collection_name=icfg.get("collection_name") or DEFAULT_COLLECTION_METHOD_INTERPRETATION,
                dimension=int(icfg.get("dimension") or 64),
                api_key=icfg.get("weaviate_api_key"),
            )
            return store.get_by_method_id(entity_id)
        except Exception:
            return None
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass

    def fetch_business_interpretation(
        self, entity_id: str, level: Optional[str] = None
    ) -> Optional[dict]:
        """从 vectordb-business 按 entity_id 取业务解读。"""
        store = None
        try:
            cfg = self._get_config()
            bcfg = (cfg.get("knowledge") or {}).get("vectordb-business") or {}
            if not (bcfg.get("enabled") and bcfg.get("backend") == "weaviate"):
                return None
            store = WeaviateBusinessInterpretStore(
                url=bcfg.get("weaviate_url") or DEFAULT_WEAVIATE_HTTP_URL,
                grpc_port=int(bcfg.get("weaviate_grpc_port") or DEFAULT_WEAVIATE_GRPC_PORT),
                collection_name=bcfg.get("collection_name") or DEFAULT_COLLECTION_BUSINESS_INTERPRETATION,
                dimension=int(bcfg.get("dimension") or 1024),
                api_key=bcfg.get("weaviate_api_key"),
            )
            return store.get_by_entity(entity_id, level=level)
        except Exception:
            return None
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass

    @staticmethod
    def is_trivial_accessor_node(node: dict) -> bool:
        """UI 侧简易 getter/setter 判断。"""
        if not node:
            return False
        if node.get("is_getter") or node.get("is_setter"):
            return True
        name = (node.get("name") or "").strip()
        sig = (node.get("signature") or name).strip()
        if not name or "(" not in sig or ")" not in sig:
            return False
        inside = sig[sig.find("(") + 1 : sig.rfind(")")]
        params = [p for p in (inside.split(",") if inside else []) if p.strip()]
        if (name.startswith("get") or name.startswith("is")) and not params:
            return True
        if name.startswith("set") and len(params) == 1:
            return True
        return False
