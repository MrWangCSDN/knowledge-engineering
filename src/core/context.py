"""应用上下文：集中管理图与配置，替代散落的全局变量，便于测试与依赖注入。

推荐使用方式：

- **FastAPI**：路由参数注入 ``ctx: AppContext = Depends(get_app_context)``，避免在 handler 内调用
  ``AppContext.get()``，便于单测时用 ``app.dependency_overrides`` 替换。
- **流水线**：``run_pipeline(..., app_context=my_ctx)`` 写入配置/图到指定实例；省略时仍使用
  ``AppContext`` 单例（与历史 ``set_global_*`` 行为一致）。
- **兼容**：``src.service.api`` 中的 ``set_global_graph`` / ``set_global_config`` 仍委托给
  ``AppContext.get()``，旧调用方可逐步迁移。
"""
from __future__ import annotations

from typing import Any, Optional, TypeVar

T = TypeVar("T")


class AppContext:
    """
    应用上下文：持有知识图谱与配置，供 Pipeline、API、Streamlit 共享。
    替代原 service.api 中的 _graph、_global_config 全局变量。
    支持单例模式；测试时可调用 reset() 或注入 mock。
    """

    _instance: Optional["AppContext"] = None

    def __init__(self) -> None:
        self._graph: Optional[Any] = None
        self._config: Optional[dict[str, Any]] = None

    @classmethod
    def get(cls) -> "AppContext":
        """获取全局单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """重置单例（用于测试或重新初始化）。"""
        if cls._instance is not None:
            cls._instance._graph = None
            cls._instance._config = None
        cls._instance = None

    @classmethod
    def set_instance(cls, ctx: Optional["AppContext"]) -> None:
        """注入自定义实例（用于测试）。"""
        cls._instance = ctx

    # --- 图 ---

    def set_graph(self, g: Any) -> None:
        """设置知识图谱。替换前会关闭旧图上的向量库连接。"""
        if self._graph is not None:
            vs = getattr(self._graph, "_vector_store", None)
            if vs is not None and hasattr(vs, "close"):
                try:
                    vs.close()
                except Exception:
                    pass
        self._graph = g

    def get_graph(self) -> Any:
        """获取图，未加载时抛出异常。"""
        if self._graph is None:
            raise RuntimeError("知识图谱未加载，请先运行流水线构建")
        return self._graph

    def get_graph_optional(self) -> Optional[Any]:
        """获取图，未加载时返回 None。"""
        return self._graph

    # --- 配置 ---

    def set_config(self, cfg: dict[str, Any]) -> None:
        """设置全局配置（含 knowledge.graph 等）。"""
        self._config = cfg

    def get_config(self) -> Optional[dict[str, Any]]:
        """获取全局配置。"""
        return self._config


def get_app_context() -> AppContext:
    """供 FastAPI ``Depends(get_app_context)`` 使用，返回当前活动上下文（默认单例）。"""
    return AppContext.get()
