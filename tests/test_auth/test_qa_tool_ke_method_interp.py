# tests/test_auth/test_qa_tool_ke_method_interp.py
"""
ke_method_interp：按 method entity_id 取方法级技术解读（Mode A）。
"""
import pytest

from src.service.qa_engine.tools.ke_method_interp import build_ke_method_interp_tool


class _FakeInterpStore:
    """模拟 WeaviateTopologicalInterpretStore.get_by_method_id。"""
    _data = {
        "method//abc": {
            "method_entity_id": "method//abc",
            "method_name": "createOrder",
            "signature": "void createOrder(Order o)",
            "interpretation_text": "创建订单：校验库存后落库并发事件。",
            "class_entity_id": "class//svc",
            "class_name": "OrderService",
            "language": "zh",
            "context_summary": "订单域核心写入",
            "related_entity_ids_json": "[]",
        }
    }

    def get_by_method_id(self, method_entity_id):
        return self._data.get(method_entity_id)


@pytest.mark.asyncio
async def test_method_interp_returns_interpretation():
    tool = build_ke_method_interp_tool(_FakeInterpStore())
    out = await tool.handler({"entity_id": "method//abc"})
    assert out["entity_id"] == "method//abc"
    assert out["interpretation"]["interpretation_text"].startswith("创建订单")
    assert out["interpretation"]["method_name"] == "createOrder"
    assert "error" not in out


@pytest.mark.asyncio
async def test_method_interp_not_found_returns_error():
    tool = build_ke_method_interp_tool(_FakeInterpStore())
    out = await tool.handler({"entity_id": "method//missing"})
    assert out["interpretation"] is None
    assert "error" in out


@pytest.mark.asyncio
async def test_method_interp_missing_id_returns_error():
    tool = build_ke_method_interp_tool(_FakeInterpStore())
    out = await tool.handler({})
    assert out["entity_id"] is None
    assert "error" in out


@pytest.mark.asyncio
async def test_method_interp_store_exception_returns_error():
    class _BoomStore:
        def get_by_method_id(self, method_entity_id):
            raise RuntimeError("weaviate down")

    tool = build_ke_method_interp_tool(_BoomStore())
    out = await tool.handler({"entity_id": "method//abc"})
    assert out["interpretation"] is None
    assert "error" in out


def test_method_interp_tool_metadata():
    tool = build_ke_method_interp_tool(_FakeInterpStore())
    assert tool.name == "ke_method_interp"
    assert tool.input_schema["required"] == ["entity_id"]
