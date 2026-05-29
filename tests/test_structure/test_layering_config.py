# tests/test_structure/test_layering_config.py
"""LayeringConfig 解析单测：验证 StructureConfig 能从 YAML dict 解析出分层配置。"""
# 从 config 模型导入待测类（实现后才存在，故此刻应 ImportError）
from src.config.models import StructureConfig


def test_layering_defaults_disabled():
    """默认：未配置 layering 时，enabled=False、adapter=three_tier、layers 为空。"""
    sc = StructureConfig()  # 不传任何字段，走默认
    assert sc.layering.enabled is False          # 默认关闭，向后兼容
    assert sc.layering.adapter == "three_tier"   # 默认范式基座
    assert sc.layering.fallback_layer == "unknown"
    assert sc.layering.layers == []              # 默认无层定义


def test_layering_parsed_from_yaml_dict():
    """model_validate 能把嵌套 dict 解析成强类型的 LayeringConfig/LayerSpec/LayerMatch。"""
    raw = {                                       # 模拟 project.yaml 的 structure 段
        "extract_cross_service": True,
        "layering": {
            "enabled": True,
            "adapter": "ssm",
            "fallback_layer": "unknown",
            "layers": [
                {
                    "id": "entry",
                    "name": "入口层",
                    "match": {"name_suffix": ["Controller"], "has_attr": ["path"]},
                }
            ],
        },
    }
    sc = StructureConfig.model_validate(raw)      # Pydantic v2 校验+构造
    assert sc.layering.enabled is True
    assert sc.layering.adapter == "ssm"
    assert sc.layering.layers[0].id == "entry"
    assert sc.layering.layers[0].match.name_suffix == ["Controller"]
    assert sc.layering.layers[0].match.has_attr == ["path"]
