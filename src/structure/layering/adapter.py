# src/structure/layering/adapter.py
"""分层适配器：按 LayeringConfig 的 match 规则把单个实体判定到某一层。"""
from __future__ import annotations

# typing 模块提供类型注解工具
# Protocol：结构化类型（鸭子类型的静态版），定义"接口"，任何实现了对应方法的类都视为该协议的实现者
from typing import Protocol

# 从配置模型导入分层规则相关类
from src.config.models import LayerMatch, LayeringConfig

# 从结构层模型导入实体定义
from src.models.structure import StructureEntity


class LayerAdapter(Protocol):
    """适配器协议：给一个实体判定 layer_id。

    Protocol 是 Python 3.8+ 引入的"结构化接口"机制：
    任何实现了 classify(entity) -> str 方法的类，
    都自动满足 LayerAdapter 协议，无需显式声明继承关系（鸭子类型的静态版本）。
    """

    def classify(self, entity: StructureEntity) -> str:
        # ... 是 Python 中的"省略号"字面量，常用于 Protocol / 抽象方法 / 类型存根中表示"无实现体"
        ...


def _entity_package_path(entity: StructureEntity) -> str:
    """从 location（文件路径）推导包路径近似，用于 package_contains 子串匹配。

    location 形如 'mall-admin/src/main/java/com/macro/mall/service/Foo.java:20'，
    去掉 ':行号' 后把目录用 '.' 连接，便于用 '.service' 这种子串匹配。

    Args:
        entity: 待查询的结构实体，其 location 字段为文件路径（可为 None）
    Returns:
        以 '.' 开头的近似包路径字符串，如 '.mall.src.main.java.com.macro.mall.service.Foo.java'
        若 location 为 None，返回空字符串 '.'
    """
    # entity.location 是 Optional[str]，or "" 保证不为 None
    loc = entity.location or ""              # location 可能为 None，用空字符串兜底

    # split(":") 按冒号切分，取第一部分去掉行号（如 "Foo.java:20" → "Foo.java"）
    path = loc.split(":")[0]                 # 去掉结尾的 ':行号'

    # replace 统一斜杠方向（Windows 路径 '\' → '/'）；split('/') 按斜杠切成目录列表；
    # '.'.join(...) 把目录列表用 '.' 连接成伪包路径；前缀 '.' 保证子串匹配 ".service" 不会误命中 "aservice"
    return "." + ".".join(path.replace("\\", "/").split("/"))


class RuleBasedAdapter:
    """纯规则驱动的适配器：按 config.layers 顺序，第一条 match 命中的层即归属。

    实现了 LayerAdapter 协议（隐式，无需继承），可直接作为适配器使用。
    优先级语义：layers 列表靠前的层优先级更高，第一个命中即返回。
    """

    def __init__(self, config: LayeringConfig) -> None:
        """初始化时持有生效配置（已由 registry 解析过）。

        Args:
            config: 分层配置，包含 layers 列表和 fallback_layer 等字段
        """
        # self._config：实例属性，命名以下划线开头表示"内部使用，不对外暴露"
        self._config = config                # 持有生效配置（已被 registry 解析过）

    def classify(self, entity: StructureEntity) -> str:
        """返回实体所属 layer_id；全不命中返回 fallback_layer。

        遍历 layers 列表，按顺序检查每层的 match 规则（OR 语义），
        第一条命中的层 id 直接返回，全不命中则返回 fallback_layer。

        Args:
            entity: 待分类的代码结构实体
        Returns:
            layer_id 字符串，如 "entry" / "business" / "dao" / "unknown"
        """
        # for...in 遍历列表；layers 列表中靠前的层优先级更高（first-match 语义）
        for layer in self._config.layers:    # 按列表顺序遍历 → 靠前的层优先级更高
            # 调用私有方法 _match 判断当前 entity 是否命中该层的规则
            if self._match(entity, layer.match):
                return layer.id              # 第一条命中即返回，不再继续检查后续层
        # 所有层都不命中时，返回兜底层 id（配置中的 fallback_layer，默认 "unknown"）
        return self._config.fallback_layer

    def _match(self, e: StructureEntity, m: LayerMatch) -> bool:
        """OR 语义：任一信号命中即算该层。v1 实现实体自带的信号子集。

        五类信号按序检查，任一信号命中返回 True；全部信号都不命中返回 False。
        v1 暂不实现 xml_paired / extends / implements（Plan 2 context-backed 扩展）。

        Args:
            e: 待判断的实体
            m: 该层的 LayerMatch 规则对象
        Returns:
            True 表示命中（实体属于该层），False 表示未命中
        """
        # e.name 是 str 类型，理论上不为 None，但加 or "" 做防御性处理
        name = e.name or ""

        # 信号 1：名字后缀匹配
        # any(iterable) 是内置函数：iterable 中有任意一个元素为 True 即返回 True，等同于"OR 折叠"
        # name.endswith(s) 检查 name 是否以字符串 s 结尾，如 "PmsBrandController".endswith("Controller") → True
        if any(name.endswith(s) for s in m.name_suffix):
            return True

        # 信号 2：名字前缀匹配
        # name.startswith(p) 检查 name 是否以 p 开头，如 "AbstractService".startswith("Abstract") → True
        if any(name.startswith(p) for p in m.name_prefix):
            return True

        # 信号 3：编程语言匹配（大小写不敏感）
        # m.language 为空列表时条件为假（空列表在 Python 中是 falsy），直接跳过
        # (e.language or "").lower() 保证 language 为 None 时不报错
        # [x.lower() for x in m.language] 将配置中的语言名统一转小写，再用 in 检查
        if m.language and (e.language or "").lower() in [x.lower() for x in m.language]:
            return True

        # 信号 4：applied 注解名匹配（依赖 Java 端持久化的 attributes["annotations"]）
        # e.attributes 是 dict[str, Any]；.get("annotations") 若键不存在返回 None
        # or [] 把 None 转成空列表，防止 any() 报错
        # any(a in anns for a in m.annotation)：只要 m.annotation 中有任意一个注解名在 anns 列表里，即命中
        if m.annotation:
            # anns：从实体 attributes 中取 annotations 列表，缺省为空列表
            anns = e.attributes.get("annotations") or []
            if any(a in anns for a in m.annotation):
                return True

        # 信号 5：存在且非空的 attribute 键（如 "path" 表示含 @*Mapping 路由注解）
        # bool(e.attributes.get(k))：若键不存在返回 None，bool(None) = False；
        # 若存在但值为空字符串/0/[]，bool() 也为 False，只有非空值才算命中
        if m.has_attr and any(bool(e.attributes.get(k)) for k in m.has_attr):
            return True

        # 信号 6：包路径子串匹配（基于 location 推导）
        # 先检查 m.package_contains 是否有值（空列表时跳过，避免无效调用 _entity_package_path）
        if m.package_contains:
            # 调用辅助函数把 location 路径转成伪包路径（以 '.' 分隔）
            pkg = _entity_package_path(e)
            # sub in pkg：检查包路径子串是否出现在 pkg 中，如 ".service" in ".mall.service.Foo.java" → True
            if any(sub in pkg for sub in m.package_contains):
                return True

        # v1 暂不实现 xml_paired / extends / implements（Plan 2 ctx-backed 扩展预留字段）
        return False
