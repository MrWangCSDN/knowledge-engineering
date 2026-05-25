# tests/test_auth/test_qa_tool_ke_read_entity.py
"""
ke_read_entity：按 entity_id 从 CodeEntity store 取代码片段（代码片段展示）。
"""
import pytest

from src.service.qa_engine.tools.ke_read_entity import build_ke_read_entity_tool


class _FakeCodeStore:
    """模拟 WeaviateVectorStore.get_by_entity_id。"""
    _data = {
        "method//abc": {
            "entity_id": "method//abc",
            "name": "createOrder",
            "entity_type": "method",
            "code_snippet": "public void createOrder() { ... }",
        }
    }

    def get_by_entity_id(self, entity_id):
        return self._data.get(entity_id)


@pytest.mark.asyncio
async def test_read_entity_returns_code_snippet():
    tool = build_ke_read_entity_tool(_FakeCodeStore())
    out = await tool.handler({"entity_id": "method//abc"})
    assert out["entity_id"] == "method//abc"
    assert out["name"] == "createOrder"
    assert out["entity_type"] == "method"
    assert out["code_snippet"] == "public void createOrder() { ... }"
    assert "error" not in out


@pytest.mark.asyncio
async def test_read_entity_not_found_returns_error():
    tool = build_ke_read_entity_tool(_FakeCodeStore())
    out = await tool.handler({"entity_id": "method//missing"})
    assert out["code_snippet"] is None
    assert "error" in out


@pytest.mark.asyncio
async def test_read_entity_missing_id_returns_error():
    tool = build_ke_read_entity_tool(_FakeCodeStore())
    out = await tool.handler({})
    assert out["entity_id"] is None
    assert "error" in out


@pytest.mark.asyncio
async def test_read_entity_store_exception_returns_error():
    class _BoomStore:
        def get_by_entity_id(self, entity_id):
            raise RuntimeError("weaviate down")

    tool = build_ke_read_entity_tool(_BoomStore())
    out = await tool.handler({"entity_id": "method//abc"})
    assert out["code_snippet"] is None
    assert "error" in out


def test_read_entity_tool_metadata():
    tool = build_ke_read_entity_tool(_FakeCodeStore())
    assert tool.name == "ke_read_entity"
    assert tool.input_schema["required"] == ["entity_id"]
