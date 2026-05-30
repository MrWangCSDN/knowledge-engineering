# src/integrations/codegraph/durable_key.py
"""KE 的持久身份：从 CodeGraph 节点派生「全名身份证」，行号无关。

- key = qualified_name（形如 'Class::method'）
- 方法再拼 '#' + 归一化参数签名以区分重载
不持久化 CodeGraph 的 node.id（含行号、不稳定）。
"""
from __future__ import annotations  # PEP 563：允许类型注解里提前引用未定义的类名

import re                                   # 正则模块，用于归一化签名（去空白、去泛型）
from src.integrations.codegraph.db import CgNode  # 引入节点数据类


def _param_sig(sig: str | None) -> str:
    """从签名里抽「参数部分」并归一化(去泛型/去空白)，作为重载区分。

    取最外层括号内容（首个 '(' 到末个 ')'），避免注解里的括号干扰。
    例：'Map (OrderParam)' → '(OrderParam)'；'List<X> (Long id)' → '(Longid)'；None → ''。

    Args:
        sig: CodeGraph 节点的 signature 字段，可为 None
    Returns:
        归一化后的参数签名字符串，格式为 '(params)' 或空串
    """
    if not sig:                             # 没签名（None 或空字符串）→ 直接返回空串
        return ""
    # re.sub(pattern, repl, string)：用 repl 替换所有匹配 pattern 的子串
    # 先去掉泛型 <...>，防止尖括号里的逗号/括号干扰后续括号定位
    s = re.sub(r"<[^>]*>", "", sig)
    # str.find(sub)：返回 sub 第一次出现的下标，找不到返回 -1
    i = s.find("(")                         # 最外层参数括号：首个 '('
    # str.rfind(sub)：返回 sub 最后一次出现的下标（从右找）
    j = s.rfind(")")                        # 最外层参数括号：末个 ')'
    # 如果找到了合法的括号对（i < j 且 i >= 0），截出括号内的内容；否则给空串
    params = s[i + 1:j] if 0 <= i < j else ""
    # 去掉参数内所有空白字符（\s+ 匹配一个或多个空白），然后用括号包起来
    return "(" + re.sub(r"\s+", "", params) + ")"


def durable_key(node: CgNode) -> str:
    """为 CgNode 生成持久身份 key，行号无关，重启/重建索引后保持稳定。

    - 方法(kind='method')：qualified_name + '#' + 归一化参数签名，区分重载
    - 其它类型(类/接口/字段等)：只用 qualified_name

    '#' 作分隔符的好处：下游 adapter 可用 split('#', 1)[0] 稳稳取回 qualified_name，
    而 qualified_name 本身（含 '::'）不会包含 '#'，不会误切。

    Args:
        node: CgNode 数据类实例
    Returns:
        持久 key 字符串，例如 'OmsService::generateOrder#(OrderParam)'
    """
    if node.kind == "method":               # 只有方法才可能重载，需要签名来区分
        # f-string 格式化：f"{变量}" 等效于 str(变量)，这里拼接 qualified_name + '#' + 参数签名
        return f"{node.qualified_name}#{_param_sig(node.signature)}"
    # 非方法（类、接口、枚举等）不存在重载，qualified_name 已够唯一
    return node.qualified_name
