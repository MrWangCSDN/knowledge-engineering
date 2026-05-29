# src/structure/layering/presets.py
"""内置范式基座：每个 adapter_id 对应一套默认 LayeringConfig.layers。

新增范式 = 在此加一个 _xxx_preset() 并注册进 PRESETS（扩展点）。
"""
# 从配置模型导入分层相关的三个类
from src.config.models import LayeringConfig, LayerMatch, LayerSpec


def _ssm_preset() -> LayeringConfig:
    """SSM / Spring Boot + MyBatis 三层基座（mall-swarm 实测指纹驱动）。

    Returns:
        LayeringConfig: 包含 entry / business / dao 三层规则的基座配置
    """
    # 返回一个 LayeringConfig 实例，内置 SSM 三层的匹配规则
    return LayeringConfig(
        enabled=True,          # 该基座默认启用
        adapter="ssm",         # 标识适配器类型为 ssm
        fallback_layer="unknown",  # 无法匹配任何层时的兜底层名
        layers=[
            # 入口层：Controller / REST API 类
            LayerSpec(id="entry", name="入口层", match=LayerMatch(
                # annotation：类上标注了这些注解则命中（mall-swarm 主用 @Controller）
                annotation=["Controller", "RestController"],
                # name_suffix：类名以这些词结尾则命中
                name_suffix=["Controller", "Resource", "Api"],
                # has_attr：拥有 path 属性（即 @*Mapping 路由注解）则命中
                has_attr=["path"],
            )),
            # 业务逻辑层：Service / Manager 类
            LayerSpec(id="business", name="业务逻辑层", match=LayerMatch(
                # Impl 在前，优先匹配更具体的实现类，再匹配接口
                annotation=["Service"],
                name_suffix=["ServiceImpl", "Service", "Manager"],
                # package_contains：所在包路径包含 .service 子串则命中
                package_contains=[".service"],
            )),
            # 数据访问层：Mapper / DAO / Repository 类及 MyBatis XML
            LayerSpec(id="dao", name="数据访问层", match=LayerMatch(
                # language=["xml"]：MyBatis XML statement 文件直接归 Dao，无需看类名
                language=["xml"],
                name_suffix=["Mapper", "Dao", "Repository"],
                package_contains=[".dao", ".mapper", ".repository"],
                # annotation 命中率低，作辅助兜底
                annotation=["Mapper", "Repository"],
            )),
        ],
    )


# PRESETS：adapter_id → 生成基座配置的工厂函数（callable）
# three_tier 与 ssm v1 共用同一套默认三层规则，指向同一个工厂
PRESETS = {
    "ssm": _ssm_preset,           # Spring Boot + MyBatis 标准 SSM
    "three_tier": _ssm_preset,    # 通用三层架构，与 ssm 规则相同（暂时共享）
}
