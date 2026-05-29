# src/structure/layering/apply.py
"""把架构分层标签写进 StructureFacts：classify 每个 class/method，method 缺省继承类。"""

# from __future__ import annotations：让函数签名里的类型注解延迟求值
# 允许在注解中使用尚未定义的类名（Python 3.10+ 默认行为，3.9 及以下需要手动加）
from __future__ import annotations

# logging 是 Python 标准库的日志模块；getLogger(__name__) 按当前模块名创建 logger
import logging

# Optional[X] 等价于 X | None，表示该参数可以传 None（Python 3.10+ 可直接写 X | None）
from typing import Optional

# 从配置模型导入分层配置类
from src.config.models import LayeringConfig

# 从结构模型导入实体类型枚举、关系类型枚举和结构事实集合
from src.models.structure import EntityType, RelationType, StructureFacts

# 从 Task2 引入规则驱动适配器（classify 单个实体 → layer_id）
from src.structure.layering.adapter import RuleBasedAdapter

# 从 Task3 引入适配器注册表（合并范式基座 + 工程覆盖 → 生效配置）
from src.structure.layering.registry import AdapterRegistry

# 按当前模块名（src.structure.layering.apply）获取 logger 实例
# 用 getLogger(__name__) 而非 getLogger("固定字符串")：模块层级自动与日志树对齐
_LOG = logging.getLogger(__name__)

# 类型集合：class-like 实体类型（都参与分层打标签）
# set 用花括号 {} 定义；EntityType.XXX 是枚举成员，in 运算符用于成员检测（O(1) 时间复杂度）
_CLASSLIKE = {
    EntityType.CLASS,            # 普通类
    EntityType.INTERFACE,        # 接口
    EntityType.ENUM,             # 枚举类
    EntityType.ANNOTATION_TYPE,  # 注解类型（Java @interface）
}


def apply_layering(facts: StructureFacts, config: Optional[LayeringConfig]) -> dict:
    """给 facts 中的 class/method 实体打 layer 标签，并写入 layer_name。

    处理顺序：
    1. 配置未启用 → 直接返回 skipped=True（no-op，向后兼容）
    2. 先处理 class-like 实体，建立 classId→layer 映射
    3. 再处理 method 实体；若 method 自身无信号（落到 fallback），则继承所属类的层

    Args:
        facts: 结构事实集合（含实体列表和关系列表），**原地修改** attributes
        config: 分层配置；None 或 enabled=False 时直接跳过

    Returns:
        统计 dict，如 {"applied": 12, "skipped": False}
        - applied：实际被打了标签的实体数
        - skipped：True 表示因 enabled=False 而跳过，False 表示正常执行
    """
    # 总开关：未配置或 enabled=False → 直接跳过（向后兼容，老流水线零影响）
    # 短路逻辑：config is None 为 True 时 Python 不再计算右侧的 config.enabled
    if config is None or not config.enabled:
        # 返回 skipped=True，不修改 facts 中的任何实体属性（真正的 no-op）
        return {"applied": 0, "skipped": True}

    # AdapterRegistry().resolve(config)：合并范式基座（preset）+ 工程覆盖 → 生效配置
    # 如果用户只写了 adapter="ssm" 而未写 layers，resolve 会把 SSM 预设的 layers 合并进来
    effective = AdapterRegistry().resolve(config)

    # RuleBasedAdapter 持有生效配置，classify() 方法对单个实体判定 layer_id
    adapter = RuleBasedAdapter(effective)

    # 构建 layer_id → layer_name 的映射字典（用于写入 layer_name 可读名称）
    # 字典推导式：{key: value for item in iterable}
    # l.id 是层的唯一标识符（如 "entry"），l.name 是层的中文名（如 "入口层"）
    name_by_id = {layer.id: layer.name for layer in effective.layers}

    # --- Step 1: 先处理 class-like 实体，建立 classId→layer 映射 ---
    # 这个映射在 Step 2 处理 method 时用于「继承」父类的层
    class_layer: dict[str, str] = {}  # 类型注解：键为实体 id（str），值为 layer_id（str）

    applied = 0  # 已打标签的实体计数器

    # for...in 遍历 facts.entities 列表；e 是每次循环中的 StructureEntity 对象
    for e in facts.entities:
        # e.type in _CLASSLIKE：检查实体类型是否属于 class-like（set 的 in 运算是 O(1)）
        if e.type in _CLASSLIKE:
            # adapter.classify(e)：根据配置规则判定实体所属 layer_id
            # 全不命中时返回 effective.fallback_layer（默认 "unknown"）
            layer = adapter.classify(e)

            # 原地修改 attributes 字典：写入 layer 和 layer_name
            # e.attributes 是 dict[str, Any]，直接赋值即可（Pydantic 允许原地修改 dict 内容）
            e.attributes["layer"] = layer

            # name_by_id.get(layer, layer)：查找对应中文名；若未找到则用 layer_id 本身兜底
            # dict.get(key, default)：键不存在时返回 default 而不是抛 KeyError
            e.attributes["layer_name"] = name_by_id.get(layer, layer)

            # 记录 classId → layer，供后续 method 继承使用
            class_layer[e.id] = layer

            # 计数器递增；+= 是 Python 的原地加法赋值，等价于 applied = applied + 1
            applied += 1

    # --- Step 2: 处理 method 实体；自身无信号则继承所属类的层 ---

    # 预先构建 methodId → classId 的索引（避免 O(n²) 嵌套循环）
    owner = _method_owner_index(facts)  # dict[str, str]

    for e in facts.entities:
        # 只处理 METHOD 类型的实体
        if e.type == EntityType.METHOD:
            # 先尝试 method 自身的 classify
            layer = adapter.classify(e)

            # 若 method 自身落到 fallback_layer（无任何分层信号），则继承所属类的层
            # effective.fallback_layer 默认为 "unknown"
            if layer == effective.fallback_layer:
                # 查找该 method 属于哪个 class
                # owner.get(e.id)：若该 method 没有记录在 owner 索引里，返回 None
                cid = owner.get(e.id)

                # cid 不为 None 且存在于 class_layer 字典中，则继承
                # Python 中 None 是 falsy，if cid 等价于 if cid is not None（此处 cid 为 str，不含空串风险）
                if cid and cid in class_layer:
                    layer = class_layer[cid]  # 继承所属类的 layer_id

            # 写入 layer 和 layer_name（与 class 处理逻辑相同）
            e.attributes["layer"] = layer
            e.attributes["layer_name"] = name_by_id.get(layer, layer)
            applied += 1  # 每处理一个 method 计数加一

    # 记录一条 INFO 级别日志，方便运维排查分层结果
    # %s 是 Python logging 的惰性格式化占位符（不会在 debug 级别提前拼字符串）
    _LOG.info("[layering] adapter=%s 打标签实体数=%d", effective.adapter, applied)

    # 返回统计 dict：skipped=False 表示正常执行，applied 为打标签实体总数
    return {"applied": applied, "skipped": False}


def _method_owner_index(facts: StructureFacts) -> dict[str, str]:
    """构建 methodId → classId 的归属索引。

    优先级规则（两种关系方向都要处理）：
    1. 优先用 BELONGS_TO(method→class)：source=method, target=class
    2. 回退用 CONTAINS(class→method)：source=class, target=method
    setdefault 保证「已有的记录不被覆盖」，即 BELONGS_TO 优先级高于 CONTAINS。

    Args:
        facts: 包含 relations 列表的结构事实集合

    Returns:
        dict[str, str]，键为 method 实体 id，值为对应 class 实体 id
    """
    # 初始化空字典，用于存储 methodId → classId 映射
    owner: dict[str, str] = {}

    # 第一轮：收集 BELONGS_TO 关系（method → class）
    # 这类关系的语义是"方法属于某个类"，source 是 method id，target 是 class id
    for r in facts.relations:
        # r.type == RelationType.BELONGS_TO：枚举比较，Python 中枚举成员用 == 比较值
        if r.type == RelationType.BELONGS_TO:
            # owner[r.source_id] = r.target_id：记录 methodId → classId
            owner[r.source_id] = r.target_id

    # 第二轮：用 CONTAINS 关系（class → method）补全没有 BELONGS_TO 的 method
    # 这类关系的语义是"类包含某个方法"，source 是 class id，target 是 method id
    for r in facts.relations:
        if r.type == RelationType.CONTAINS:
            # dict.setdefault(key, value)：若 key 已存在则不修改（保留第一轮 BELONGS_TO 结果）
            # 若 key 不存在则插入；这正是"BELONGS_TO 优先"语义的实现方式
            # 等价写法（更啰嗦）：if r.target_id not in owner: owner[r.target_id] = r.source_id
            owner.setdefault(r.target_id, r.source_id)  # setdefault：已有则不覆盖

    return owner
