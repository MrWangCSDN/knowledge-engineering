"""decide_status 纯函数单测。

只测 decide_status（纯函数，无 IO），真库 main 不在这里测。
"""
# 从即将创建的 scripts 模块导入被测函数
from scripts.backfill_project_status import decide_status


def test_no_weaviate_data_stays_indexing():
    """无任何解读数据（done=0, total=0）→ 应保持 indexing，不误标 ready。"""
    prog = {"tech": {"done": 0, "total": 0}, "biz": {"done": 0, "total": 0}}
    assert decide_status(prog) == ("indexing", 0, 0)


def test_partial_data_is_partial():
    """tech 有 50/200 done，biz 无数据 → partial，进度 25%，methods=200。"""
    prog = {"tech": {"done": 50, "total": 200}, "biz": {"done": 0, "total": 0}}
    assert decide_status(prog) == ("partial", 25, 200)


def test_complete_is_ready():
    """tech done==total（200/200），biz 无数据 → ready，进度 100%，methods=200。"""
    prog = {"tech": {"done": 200, "total": 200}, "biz": {"done": 0, "total": 0}}
    assert decide_status(prog) == ("ready", 100, 200)


def test_methods_exist_but_none_done_stays_indexing():
    """tech 有 200 个方法但 done=0，biz 无数据 → 仍为 indexing，进度 0%，methods=200。

    边界场景：total>0 但 done==0，意味着任务存在但尚未完成任何解读。
    绝不能误标为 partial 或 ready，应维持 indexing（保守语义）。
    """
    prog = {"tech": {"done": 0, "total": 200}, "biz": {"done": 0, "total": 0}}
    assert decide_status(prog) == ("indexing", 0, 200)
