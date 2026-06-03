"""run_codegraph_interpret 装配单测：mock Provider/Interpreter/_build_llm/_build_store，
验证 build_and_run 把 FactsProvider 产出的 facts 正确注入 TopologicalInterpreter。

计划 Task 2。_build_llm/_build_store 内部 load_config，测试里 mock 掉 → 不依赖真 config。
"""
from unittest.mock import MagicMock, patch

from src.models.structure import StructureFacts
import run_codegraph_interpret as cli


def test_build_and_run_wires_facts_into_interpreter():
    fake_facts = StructureFacts(entities=[], relations=[], meta={})
    with patch.object(cli, "CodeGraphFactsProvider") as P, \
         patch.object(cli, "TopologicalInterpreter") as I, \
         patch.object(cli, "_build_llm", return_value=MagicMock()), \
         patch.object(cli, "_build_store", return_value=MagicMock()):
        P.return_value.build_structure_facts.return_value = fake_facts
        I.return_value.run.return_value = {"ok": 1}

        cli.build_and_run(
            db_path="x.db", repo_path="/opt/mall-swarm",
            project_id="mall-swarm", modules={"mall-portal"},
            workers=4,
        )

        # Provider 用正确路径构造 + module_filter 透传
        P.assert_called_once_with(db_path="x.db", repo_local_path="/opt/mall-swarm")
        P.return_value.build_structure_facts.assert_called_once_with(module_filter={"mall-portal"})
        # Interpreter 用 provider 产出的 facts 构造，且 repo_path/workers 透传
        _, kwargs = I.call_args
        assert kwargs["structure_facts"] is fake_facts
        assert kwargs["repo_path"] == "/opt/mall-swarm"
        assert kwargs["max_workers"] == 4
        I.return_value.run.assert_called_once()


def test_tenant_bound_store_overrides_get_collection_with_tenant():
    """_TenantBoundStore._get_collection 应返回 with_tenant(tenant) 视图，
    让 TopologicalInterpreter 的 legacy 无租户调用自动落到 project tenant。"""
    # 构造 store 但不连真 Weaviate：直接 patch 基类 _get_collection 返回假 collection
    fake_base_coll = MagicMock()
    fake_tenant_view = MagicMock()
    fake_base_coll.with_tenant.return_value = fake_tenant_view

    with patch.object(cli.WeaviateTopologicalInterpretStore, "_get_collection",
                      return_value=fake_base_coll):
        store = cli._TenantBoundStore.__new__(cli._TenantBoundStore)  # 跳过 __init__ 的真实连接
        store._bound_tenant = "mall-swarm"
        coll = store._get_collection()

    # 返回的是 with_tenant("mall-swarm") 的视图，不是原始 collection
    fake_base_coll.with_tenant.assert_called_once_with("mall-swarm")
    assert coll is fake_tenant_view
