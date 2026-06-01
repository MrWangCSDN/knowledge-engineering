"""ke_table_access 工具：合并图谱 accesses_table 边 + summary_text 里的『<table> 表』模式。

复用 QARetriever._extract_tables_from_text（从 summary_text 抽『<table> 表』），
data-flow 子技能已退役但该解析函数仍由本工具复用；作为独立 tool 暴露，方便 LLM / 外部 client 单独调用。

设计动机：
  - 图边在某些项目里不完整（如 PetClinic 完全没 accesses_table 边）
  - summary_text 由业务解读 LLM 写的，自然语言提到的表名是宝贵补充
  - 两个来源合并去重，给 LLM 最完整的数据访问视图
"""
from __future__ import annotations

from typing import Any

from src.service.qa_engine.retriever import GraphProto, QARetriever
from src.service.qa_engine.tools.base import Tool


_KE_TABLE_ACCESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "实体 ID（一般是 method 级，类 / 模块也支持）",
        },
        "summary_text": {
            "type": "string",
            "description": "可选；如果传入，会从中提取『<table> 表』模式作为补充来源",
        },
    },
    "required": ["entity_id"],
}


def build_ke_table_access_tool(graph: GraphProto) -> Tool:
    """构造一个绑定到指定 GraphProto 的 ke_table_access Tool。"""

    async def handler(input: dict[str, Any]) -> dict[str, Any]:
        entity_id = (input.get("entity_id") or "").strip()
        if not entity_id:
            return {
                "tables": [],
                "error": "missing required field: entity_id",
            }

        # 1) 图边来源：rel_type='accesses_table'
        tables: list[dict[str, str]] = []
        # 用 set 跟踪已加入的表名做去重；保持原序
        seen: set[str] = set()
        try:
            for tid in graph.successors(entity_id, rel_type="accesses_table"):
                if tid and tid not in seen:
                    tables.append({"table_id": tid, "operation": "unknown"})
                    seen.add(tid)
        except Exception:
            # 图谱后端挂了 → 静默继续，让 summary 兜底
            pass

        # 2) summary_text 来源（兜底）：复用 retriever 里写好的正则
        # 直接用 QARetriever._extract_tables_from_text 类方法（@classmethod）
        summary_text = input.get("summary_text") or ""
        if summary_text:
            for tid in QARetriever._extract_tables_from_text(summary_text):
                if tid not in seen:
                    tables.append({"table_id": tid, "operation": "mentioned"})
                    seen.add(tid)

        return {"entity_id": entity_id, "tables": tables}

    return Tool(
        name="ke_table_access",
        description="查询某个代码实体访问的数据库表（图边 + summary_text 双源合并去重）。",
        input_schema=_KE_TABLE_ACCESS_SCHEMA,
        handler=handler,
    )
