# src/knowledge/recall_rerank.py
"""召回降噪 + 加权（query-time 纯函数）。

设计 [[召回降噪加权-设计]]：CodeEntity 召回里 ~96% 是 MyBatis 样板/getter/Mapper-CRUD，
business 实体仅 ~4%。本模块按 entity_id(qualified_name) 把候选分四类——硬删纯样板、
降权低价值、加权业务、其余中性——在 composite 兜底处过滤+重排，让业务实体浮上来。
"""
from __future__ import annotations  # PEP 563：注解延迟求值，避免 3.10 以下的类型前向引用问题

# frozenset：不可变集合，查找 O(1)，放 MyBatisGenerator 生成的 *ByExample 系列方法名
# 这类方法是"按 Example 条件"的批量操作，由 MBG 自动生成，业务价值极低
_GENERATED_BY_EXAMPLE: frozenset[str] = frozenset({
    "selectByExample",           # 按 Example 条件查询
    "countByExample",            # 按 Example 条件计数
    "deleteByExample",           # 按 Example 条件删除
    "updateByExample",           # 按 Example 条件全字段更新
    "updateByExampleSelective",  # 按 Example 条件选择性更新
})


def _is_accessor(method: str) -> bool:
    """判断方法名是否为 getter / setter / is 布尔访问器。

    规则：前缀（get/set/is）之后的第一个字符必须是大写字母，
    这是 Java camelCase 的访问器约定：
      - getStatus  → True（get + 大写 S）
      - setStatus  → True（set + 大写 S）
      - isDeleted  → True（is + 大写 D）
      - issueRefund→ False（is + 小写 s，不是访问器）
      - insert     → False（不含任何前缀）

    Args:
        method: 方法名字符串，不含类名和参数

    Returns:
        True 表示是访问器，应被降权；False 表示不是访问器
    """
    # 遍历三种 Java 访问器前缀；依次检查
    for pre in ("get", "set", "is"):
        n = len(pre)  # 前缀长度：get/set=3, is=2
        # startswith：方法名以 pre 开头
        # len(method) > n：确保前缀后还有字符（不是方法名本身就叫 "get"）
        # method[n].isupper()：前缀后第一个字符是大写 → 是 camelCase 访问器
        if method.startswith(pre) and len(method) > n and method[n].isupper():
            return True
    return False  # 不匹配任何访问器模式


def classify_entity(entity_id: str) -> str:
    """把 entity_id (qualified_name) 分类为 drop | demote | boost | neutral。

    entity_id 格式：`com.pkg.ClassName::methodName#(ParamTypeParamName,...)`
    解析步骤：
      1. 按 '#' 切掉参数签名，只保留 'ClassName::methodName' 部分
      2. 按第一个 '::' 分成 (qualified_class, method)
      3. 取 qualified_class 最后一个 '.' 之后的 simple_class

    分类优先级（高→低）：
      drop   → MyBatis 样板，零业务价值，硬过滤
      boost  → 业务实体（Controller/ServiceImpl/Service）
      demote → 访问器或 *ByExample 生成 CRUD
      neutral→ 其余（含 Mapper 的 insert/selectByPrimaryKey 等真实操作）

    Args:
        entity_id: qualified_name 格式的实体 ID，空字符串或格式异常返回 "neutral"

    Returns:
        "drop" | "demote" | "boost" | "neutral" 四选一
    """
    # split('#', 1)：只切第一个 '#'，最多分成两段；[0] 取前半段（类名::方法名）
    head = (entity_id or "").split("#", 1)[0]

    # '::' 是 entity_id 格式必须有的分隔符；没有则格式异常，保守返回 neutral
    if "::" not in head:
        return "neutral"

    # partition('::')：只切第一个 '::'，返回 (before, sep, after) 三元组
    # 比 split('::') 更安全——方法名本身不会含 '::'，但 partition 在语义上更明确
    qualified_class, _, method = head.partition("::")

    # rsplit('.', 1)：从右切一次 '.'，取 [-1] 得最后一段即简单类名
    # 'com.macro.mall.mapper.OmsOrderMapper' → 'OmsOrderMapper'
    # 若本来就没有 '.'（如 'OmsOrder'），rsplit 返回 ['OmsOrder']，[-1] 仍正确
    simple_class = qualified_class.rsplit(".", 1)[-1]

    # --- 1) DROP：MyBatis 纯样板，不含任何业务逻辑，硬删除不返回 ---
    # Base_Column_List：MBG 生成的 SQL 列名片段（不是真实方法，但会被解析成 entity）
    # endswith("Example")：XxxExample 是 MBG 生成的条件类，所有方法均为条件构造器
    if method == "Base_Column_List" or simple_class.endswith("Example"):
        return "drop"

    # --- 2) BOOST：业务实体，优先级最高，应当浮到顶部 ---
    # endswith(tuple)：Python 允许传元组，等价于 or 连接多个 endswith
    # Controller：入口层；ServiceImpl：业务实现层；Service：业务接口（含抽象方法）
    if simple_class.endswith(("Controller", "ServiceImpl", "Service")):
        return "boost"

    # --- 3) DEMOTE：低价值，降权但不删除（可能还有参考意义）---
    # _is_accessor：getter/setter/is 布尔访问器
    # _GENERATED_BY_EXAMPLE：MBG 生成的 *ByExample 批量操作（Mapper 上的）
    if _is_accessor(method) or method in _GENERATED_BY_EXAMPLE:
        return "demote"

    # --- 4) NEUTRAL：其余保持原始分数 ---
    # 含：Mapper 的 insert / insertSelective / updateByPrimaryKeySelective /
    #     selectByPrimaryKey 等真实数据库操作，以及其它未命中规则的实体
    return "neutral"


def rerank_and_filter(
    hits: list[tuple[str, float]],
    limit: int,
    *,
    boost: float = 0.05,    # 业务实体加权幅度（小幅，语义分仍主导排序）
    demote: float = 0.05,   # 低价值实体降权幅度
) -> list[tuple[str, float]]:
    """过滤纯样板 + 按"调整分"重排，返回前 limit 个（对外暴露原始 cosine 分）。

    调整分（adj）只用于此函数内部排序，不写入返回值，保证调用方拿到的始终是
    原始相似度，召回日志可信、门控 top-1 诚实。

    流程：
      1. 遍历 hits，drop 类直接丢弃，其余按分类计算 adj
      2. 全部丢弃时兜底：原样返回 hits[:limit]，绝不比现状差
      3. 按 adj 降序稳定排序
      4. 取前 limit，返回 (entity_id, 原始score)

    Args:
        hits:   [(entity_id, cosine_score), ...]，通常是 over-fetch 后的候选集
        limit:  最终返回条数上限
        boost:  业务实体的 adj 加量（默认 +0.05）
        demote: 低价值实体的 adj 减量（默认 -0.05）

    Returns:
        [(entity_id, 原始score), ...]，长度 ≤ limit
    """
    # scored 列表：三元组 (entity_id, 原始score, adj排序分)
    # 用 list[tuple[str, float, float]] 类型注解让阅读代码更清晰
    scored: list[tuple[str, float, float]] = []

    for eid, score in hits:
        cat = classify_entity(eid)  # 分类：drop/demote/boost/neutral

        if cat == "drop":
            # 纯样板：直接跳过，不进入候选集
            continue

        # 根据分类计算 adj（调整分），仅用于排序
        # 三元表达式嵌套：boost → score+boost；demote → score-demote；neutral → score
        adj = (
            score + boost if cat == "boost"
            else score - demote if cat == "demote"
            else score  # neutral：保持原分
        )
        # 将三元组追加到 scored 列表
        scored.append((eid, score, adj))

    # 2) 空兜底：所有候选都被 drop 时，回退到原始 hits 的前 limit 条
    # 这保证调用方不会拿到空列表，降噪失效时的最坏情况与不加过滤相同
    if not scored:
        return list(hits[:limit])  # list() 复制切片，避免外部修改原 hits

    # 3) 按 adj 降序稳定排序
    # sorted/list.sort 均稳定：adj 相同时保留 hits 原始顺序（通常是相似度降序）
    # key=lambda t: t[2]：取三元组第三个元素（adj）作为排序键
    # reverse=True：从大到小（分数越高越靠前）
    scored.sort(key=lambda t: t[2], reverse=True)

    # 4) 截取前 limit 条，返回二元组 (entity_id, 原始score)
    # 列表推导式解包三元组，丢弃 _adj（下划线前缀表示有意忽略的变量）
    return [(eid, score) for (eid, score, _adj) in scored[:limit]]
