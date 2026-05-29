# tests/test_structure/test_layering_registry.py
"""AdapterRegistry 单测：基座解析 + 工程覆盖优先 + 未知 adapter 容错。"""
from src.config.models import LayeringConfig, LayerMatch, LayerSpec
from src.structure.layering.registry import AdapterRegistry


def test_ssm_preset_used_when_no_user_layers():
    """adapter=ssm 且用户没写 layers → 借用 SSM 基座的 3 层。"""
    cfg = LayeringConfig(enabled=True, adapter="ssm")  # layers 为空
    eff = AdapterRegistry().resolve(cfg)
    ids = [l.id for l in eff.layers]
    assert ids == ["entry", "business", "dao"]
    assert eff.adapter == "ssm"          # 其它字段保留


def test_user_layers_override_preset():
    """用户写了 layers → 工程覆盖优先，忽略基座。"""
    cfg = LayeringConfig(enabled=True, adapter="ssm", layers=[
        LayerSpec(id="api", name="API", match=LayerMatch(name_suffix=["Endpoint"])),
    ])
    eff = AdapterRegistry().resolve(cfg)
    assert [l.id for l in eff.layers] == ["api"]


def test_unknown_adapter_no_layers_is_empty():
    """未知 adapter 且无覆盖 → 空 layers（不抛错，全部归 fallback）。"""
    cfg = LayeringConfig(enabled=True, adapter="nonexistent")
    eff = AdapterRegistry().resolve(cfg)
    assert eff.layers == []
