"""把 get_interpretation_progress_from_weaviate 的计数字典换算成百分比/方法数（纯函数）。

计数字典结构：
    {"tech": {"done": N, "total": M}, "biz": {"done": ..., "total": ...}}
本模块不做任何 IO，仅做数值换算，供下游任务组合使用。
"""
from __future__ import annotations  # 允许在类型注解里写 dict[str, ...] 而不需要 Dict（Python 3.9+之前的写法）


def interpretation_percent(progress: dict[str, dict[str, int]]) -> int:
    """合并各 phase 的 done/total → 0-100 整型百分比；total 为 0 时返 0（不除零）。

    Args:
        progress: 来自 get_interpretation_progress_from_weaviate 的计数字典，
                  key 为 phase 名（"tech"/"biz"），value 为 {"done": int, "total": int}。
    Returns:
        0-100 的整型百分比。total 全零时返回 0。
    """
    # sum(生成器) 对 progress 所有 phase 求 done 之和
    # p.get("done", 0) 是 dict.get(key, default)，key 不存在时安全返回 0
    done = sum(p.get("done", 0) for p in progress.values())

    # 同理求 total 之和
    total = sum(p.get("total", 0) for p in progress.values())

    # 防零除：total 为 0 说明还没有可解读方法，进度为 0
    if total <= 0:
        return 0

    # round() 四舍五入到整数；* 100 保证结果为整型百分比
    return round(done / total * 100)


def methods_total(progress: dict[str, dict[str, int]]) -> int:
    """可解读方法总数 = tech phase 的 total（biz 不是方法计数）。

    Args:
        progress: 同 interpretation_percent 的计数字典。
    Returns:
        tech phase 的 total 整数；tech 键不存在时返回 0。
    """
    # dict.get(key, default) 两次链式调用：先取 "tech" 子字典，再取 "total"
    return progress.get("tech", {}).get("total", 0)
