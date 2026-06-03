"""集成（indices 层）：手建 StructureFacts(id=qualified_name) → TopologicalInterpreter
建索引后，该 qualified_name 正确进入 _methods / meaningful / levels，且能取到代码片段。

计划 Task 3（Step2 降级版）：full run() 的线程池/重试编排过重、mock 成本高且属解读器内部（不改），
故集成测试只验证"facts.id=qualified_name 正确流入解读器处理集"——这保证解读器最终写入的
method_entity_id（= method_id，topological_interpreter.py:569 `method_entity_id=method_id`）就是 qualified_name。
真正"写入 Weaviate key=qualified_name"由服务器 E2E（计划 Task 4）验收。
"""
import tempfile
from unittest.mock import MagicMock

from src.models.structure import (
    StructureFacts, StructureEntity, StructureRelation, EntityType, RelationType,
)
from src.knowledge.topological_interpreter import TopologicalInterpreter


def _one_method_facts() -> StructureFacts:
    """1 个业务方法 + 其类 + CONTAINS 边；id 均为 qualified_name。"""
    return StructureFacts(
        entities=[
            StructureEntity(
                id="OmsPortalOrderServiceImpl::generateOrder", type=EntityType.METHOD,
                name="generateOrder", module_id="mall-portal",
                attributes={
                    "code_snippet": "public CommonResult generateOrder(OrderParam p){ return ok; }",
                    "class_name": "OmsPortalOrderServiceImpl",
                    "signature": "(OrderParam)",
                },
            ),
            StructureEntity(
                id="OmsPortalOrderServiceImpl", type=EntityType.CLASS,
                name="OmsPortalOrderServiceImpl",
                attributes={"class_name": "OmsPortalOrderServiceImpl"},
            ),
        ],
        relations=[
            StructureRelation(
                type=RelationType.CONTAINS,
                source_id="OmsPortalOrderServiceImpl",
                target_id="OmsPortalOrderServiceImpl::generateOrder",
            )
        ],
        meta={},
    )


def test_qualified_name_flows_into_interpreter_indices():
    """facts(id=qualified_name) → 解读器 _build_indices/_filter_meaningful/_compute_levels
    后，该 qualified_name 都在；且 _get_code_with_sql 取得到片段。"""
    qn = "OmsPortalOrderServiceImpl::generateOrder"
    facts = _one_method_facts()
    store = MagicMock()
    store.list_existing_method_ids.return_value = set()
    store.get_by_method_id.return_value = None
    td = tempfile.mkdtemp()

    interp = TopologicalInterpreter(
        structure_facts=facts, llm=MagicMock(), weaviate_store=store,
        repo_path=td, max_workers=1, state_file=td + "/state.json",
    )
    # 建索引（从 facts.entities/relations 构图）
    interp._build_indices()
    # ① qualified_name 进入 method 索引
    assert qn in interp._methods
    # ② 业务方法（非 getter、code_snippet 非空）通过 meaningful 过滤
    meaningful = interp._filter_meaningful()
    assert qn in meaningful
    # ③ 拓扑分层把它纳入（叶子 → L0）
    levels = interp._compute_levels(meaningful)
    assert qn in levels
    # ④ 能取到代码片段（解读器据此调 LLM；空则 _interpret_one 提前 return）
    assert interp._get_code_with_sql(qn).strip() != ""
    # ⑤ class_entity_id 可由 CONTAINS 边解析（写入时 class_entity_id 用）
    class_id = ""
    for r in facts.relations:
        if r.type == RelationType.CONTAINS and r.target_id == qn:
            class_id = r.source_id
            break
    assert class_id == "OmsPortalOrderServiceImpl"
