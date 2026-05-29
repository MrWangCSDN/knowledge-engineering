# src/structure/layering/registry.py
"""适配器注册表：adapter_id → 范式基座；并把工程覆盖合并成生效配置。"""
# __future__.annotations：让函数签名里的类型注解延迟求值（Python 3.10+ 的默认行为，3.9 及以下需要手动加）
from __future__ import annotations

# 导入分层配置模型
from src.config.models import LayeringConfig
# 导入内置范式基座字典
from src.structure.layering.presets import PRESETS


class AdapterRegistry:
    """范式基座注册表 + 生效配置解析。

    职责：
    1. 持有 adapter_id → 工厂函数 的映射（来自 PRESETS）
    2. 提供 resolve() 方法，将用户的 LayeringConfig 与基座合并为"生效配置"
    """

    def __init__(self) -> None:
        # dict(PRESETS) 做一次浅拷贝，避免外部修改全局 PRESETS 影响本实例
        # dict() 等价于 {k: v for k, v in PRESETS.items()}，但更简洁
        self._presets = dict(PRESETS)

    def resolve(self, config: LayeringConfig) -> LayeringConfig:
        """计算生效配置，优先级规则：

        1. 用户在 LayeringConfig 中写了 layers → 工程覆盖优先，直接返回原对象；
        2. 用户未写 layers 但 adapter_id 在基座字典中 → 借用基座的 layers，
           保留用户的其余字段（enabled / fallback_layer 等）；
        3. 未知 adapter 且无覆盖 → 原样返回，layers 为空，全部实体归 fallback，
           程序不抛错（渐进式接入）。

        Args:
            config: 来自项目 YAML 的原始分层配置

        Returns:
            LayeringConfig: 合并后的生效配置（有可能与入参是同一对象）
        """
        # 若用户已写了 layers（列表非空），工程覆盖优先，直接原样返回
        if config.layers:
            return config

        # 按 adapter_id 查找对应的工厂函数
        # dict.get(key) 在 key 不存在时返回 None，而不是抛 KeyError
        factory = self._presets.get(config.adapter)

        # 未知 adapter 且用户也没写 layers，退化为空 layers（不抛错）
        if factory is None:
            return config

        # 调用工厂函数生成基座配置，取出其 layers
        preset = factory()

        # model_copy(update={"layers": ...})：Pydantic v2 的不可变更新方法
        # 相当于"新建一个 LayeringConfig，大部分字段从 config 复制，只把 layers 换成基座的"
        # 注意：不直接改 config.layers，因为 Pydantic v2 默认 model 是不可变的
        return config.model_copy(update={"layers": preset.layers})
