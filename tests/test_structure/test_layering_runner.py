"""run_structure_layer 透传 layering 单测：monkeypatch 掉 Java bridge，只验证打标签接线。

该测试验证：
  1. 传入 LayeringConfig(enabled=True) 时，实体会被打上对应的 layer 标签
  2. 不传 layering 参数时，实体不会有 layer 属性（向后兼容）
"""
# 从配置模型导入 LayeringConfig，它控制架构分层采集行为
from src.config.models import LayeringConfig

# 从模型导入代码输入源与结构事实相关类
from src.models import CodeInputSource
from src.models.structure import EntityType, StructureEntity, StructureFacts

# 导入 runner 模块（后面需要通过 monkeypatch 替换模块内的函数）
import src.structure.runner as runner_mod


def test_run_structure_layer_applies_layering(monkeypatch, tmp_path):
    """伪造 javaparser bridge 返回一个 Controller 类，验证 run 后带 layer。

    Args:
        monkeypatch: pytest 提供的「猴子补丁」夹具，可临时替换函数/属性
        tmp_path:    pytest 提供的临时目录路径（每个测试独立），让 MyBatis XML 扫描成为空操作
    """
    # 构造一个只含 FooController 类的假 StructureFacts，用于模拟 JavaParser 返回结果
    # StructureEntity(attributes={}) 表示起始时没有 layer 属性，分层后应该被打上
    fake_facts = StructureFacts(entities=[
        StructureEntity(
            id="class//C",
            type=EntityType.CLASS,
            name="FooController",       # 后缀含 Controller，SSM adapter 的 entry 层规则应命中
            language="java",
            attributes={},              # 初始无任何属性
        ),
    ])

    # 定义一个假的 bridge 函数，签名与真实的 run_javaparser_bridge 一致（keyword-only 参数）
    # 直接返回 fake_facts，避免真正调用 Java 解析器
    def _fake_bridge(*, source, extract_cross_service, progress_callback):
        """伪造的 JavaParser bridge，直接返回预设的 fake_facts。"""
        return fake_facts

    # monkeypatch.setattr(目标模块, 属性名, 新值, raising=False)
    # raising=False 表示：即使该属性还不存在也不报错（因为 runner 内部是延迟导入的）
    monkeypatch.setattr(runner_mod, "run_javaparser_bridge", _fake_bridge, raising=False)

    # 同时替换 javaparser_bridge 模块内的函数，以防 runner 通过「from .javaparser_bridge import」方式调用
    import src.structure.javaparser_bridge as jb          # 导入 javaparser_bridge 模块
    monkeypatch.setattr(jb, "run_javaparser_bridge", _fake_bridge, raising=False)

    # 构造 CodeInputSource：repo_path 指向 tmp_path（空目录），这样 MyBatis XML 扫描不会找到任何 XML
    source = CodeInputSource(language="java", repo_path=str(tmp_path))

    # 构造 LayeringConfig：enabled=True 启用分层，adapter="ssm" 使用 SSM 范式预设规则
    cfg = LayeringConfig(enabled=True, adapter="ssm")

    # 调用 run_structure_layer，传入 layering=cfg
    # 当前版本该参数还不存在，所以这个测试应该 FAIL（TypeError）
    facts = runner_mod.run_structure_layer(source, layering=cfg)

    # 断言：FooController 的 attributes 中应有 layer="entry"（Controller 后缀命中入口层）
    assert facts.entities[0].attributes.get("layer") == "entry"


def test_run_structure_layer_no_layering_is_noop(monkeypatch, tmp_path):
    """不传 layering → 实体无 layer，老行为不变（向后兼容）。

    Args:
        monkeypatch: pytest 猴子补丁夹具
        tmp_path:    pytest 临时目录
    """
    # 同样的假 facts，FooController 初始无 layer 属性
    fake_facts = StructureFacts(entities=[
        StructureEntity(
            id="class//C",
            type=EntityType.CLASS,
            name="FooController",
            language="java",
            attributes={},
        ),
    ])

    # 定义假 bridge 函数，关键字参数签名与真实函数一致
    def _fake_bridge(*, source, extract_cross_service, progress_callback):
        """伪造的 bridge，返回假 facts。"""
        return fake_facts

    # 替换 javaparser_bridge 模块内的函数（runner 通过 from .javaparser_bridge import 调用）
    import src.structure.javaparser_bridge as jb          # 导入被替换的模块
    monkeypatch.setattr(jb, "run_javaparser_bridge", _fake_bridge, raising=False)

    # 构造 CodeInputSource，repo_path 指向空临时目录
    source = CodeInputSource(language="java", repo_path=str(tmp_path))

    # 不传 layering 参数（默认 None），验证向后兼容性
    facts = runner_mod.run_structure_layer(source)

    # 断言：没有 layer 属性（未触发分层逻辑）
    assert "layer" not in facts.entities[0].attributes
