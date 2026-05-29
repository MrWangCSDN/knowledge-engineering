# tests/test_structure/test_layering_apply.py
"""apply_layering 单测：class 打标签、method 缺省继承类、禁用时跳过。"""

# 从配置模型导入分层配置类
from src.config.models import LayeringConfig

# 从结构模型导入实体类型、关系类型和相关模型
from src.models.structure import (
    EntityType,        # 实体类型枚举，如 CLASS、METHOD
    RelationType,      # 关系类型枚举，如 CONTAINS、BELONGS_TO
    StructureEntity,   # 单个代码实体
    StructureFacts,    # 结构事实集合（实体 + 关系）
    StructureRelation, # 单条关系
)

# 从待测模块导入核心函数（此时尚未创建，会触发 ModuleNotFoundError → 红测状态）
from src.structure.layering.apply import apply_layering


def _facts() -> StructureFacts:
    """构建测试用 StructureFacts：一个 Controller 类 + 其方法（无分层信号，应继承类）+ 一个 Mapper XML。

    Returns:
        包含三个实体和一条 CONTAINS 关系的 StructureFacts 对象
    """
    # Controller 类：name 含 "Controller" 后缀 → 应被归入 entry 层
    ctrl = StructureEntity(
        id="class//Ctrl",
        type=EntityType.CLASS,       # 实体类型为 CLASS
        name="PmsBrandController",   # 含 "Controller" 后缀，SSM 预设会匹配 entry 层
        language="java",
        attributes={"path": "/brand"},  # path 属性也是 entry 层的 has_attr 信号
    )

    # 普通方法：name 为 "getList"，无任何层分类信号，应通过 CONTAINS 关系继承 Controller 类的层
    m = StructureEntity(
        id="method//getList",
        type=EntityType.METHOD,      # 实体类型为 METHOD
        name="getList",              # 普通方法名，无层信号
        language="java",
        attributes={"class_name": "PmsBrandController"},  # 记录所属类名（仅元信息，非用于继承）
    )

    # XML 实体：language 为 "xml"，SSM 预设中 xml 语言 → 应归入 dao 层
    xml = StructureEntity(
        id="method//xml-stmt",
        type=EntityType.METHOD,      # 实体类型为 METHOD（XML 语句映射）
        name="selectAll",
        language="xml",              # language=xml，SSM 预设的 dao 层规则会命中
        attributes={},
    )

    # 关系：ctrl CONTAINS m（class→method），用于方法继承类的分层
    # 注意：这里用 CONTAINS，而非 BELONGS_TO；代码应通过 setdefault 逻辑处理此回退
    rels = [
        StructureRelation(
            type=RelationType.CONTAINS,  # 关系类型：包含
            source_id="class//Ctrl",     # source：类
            target_id="method//getList", # target：方法
        )
    ]

    # StructureFacts：聚合所有实体和关系
    return StructureFacts(entities=[ctrl, m, xml], relations=rels)


def test_full():
    """主场景：Controller→entry；其方法通过 CONTAINS 继承 entry；xml→dao。

    验证：
    1. class//Ctrl 被打上 layer="entry"
    2. method//getList 继承类的 entry 层（自身无信号）
    3. method//xml-stmt 被打上 layer="dao"（language=xml 命中）
    4. stats["skipped"] 为 False
    5. stats["applied"] 为 3（三个实体均被处理）
    """
    facts = _facts()

    # 创建启用状态的分层配置，使用 SSM 预设适配器
    cfg = LayeringConfig(enabled=True, adapter="ssm")

    # 调用被测函数
    stats = apply_layering(facts, cfg)

    # 按 id 建立索引，方便断言
    # {e.id: e for e in facts.entities} 是字典推导式：把列表转为以 id 为键的字典
    by_id = {e.id: e for e in facts.entities}

    # Controller 类应被归入 entry 层
    assert by_id["class//Ctrl"].attributes["layer"] == "entry"

    # getList 方法自身无层信号，通过 CONTAINS 关系继承 Controller 类的 entry 层
    assert by_id["method//getList"].attributes["layer"] == "entry"

    # xml 实体 language=xml，SSM 预设 dao 层规则命中
    assert by_id["method//xml-stmt"].attributes["layer"] == "dao"

    # 统计：skipped=False 表示确实执行了分层
    assert stats["skipped"] is False

    # 统计：3 个实体全部被打标签
    assert stats["applied"] == 3


def test_disabled_skips():
    """禁用场景：enabled=False 时 apply_layering 不做任何修改，直接返回 skipped=True。

    验证：
    1. stats["skipped"] 为 True
    2. 实体的 attributes 中没有写入 "layer" 键（真正的 no-op）
    """
    facts = _facts()

    # LayeringConfig(enabled=False)：总开关关闭
    stats = apply_layering(facts, LayeringConfig(enabled=False))

    # skipped 应为 True
    assert stats["skipped"] is True

    # 第一个实体（Controller 类）不应有 layer 属性（验证确实 no-op）
    assert "layer" not in facts.entities[0].attributes
