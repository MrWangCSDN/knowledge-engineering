"""ke_grep 工具测试 — ripgrep subprocess + sandbox + result 截断。

设计：[[代码源文件查询工具-设计]] §3.1
"""
import asyncio  # 标准库：提供 asyncio.run() 用于在同步代码中运行 async 函数
import shutil   # 标准库：提供 shutil.which()，用于查找可执行文件路径

import pytest  # 第三方测试框架

from src.service.qa_engine.tools.ke_grep import build_ke_grep_tool


# pytestmark：模块级标记，对本文件所有测试生效
# pytest.mark.skipif(condition, reason)：condition 为 True 时跳过测试
# shutil.which("rg") 返回 rg 可执行文件路径，未安装时返回 None
pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None,
    reason="ripgrep not installed (brew install ripgrep)",
)


def _run(coro):
    """异步 helper：在同步测试中运行 async 协程。

    Args:
        coro: 一个 async 协程对象（coroutine）
    Returns:
        协程的返回值

    Note:
        asyncio.run() 创建新的事件循环，运行协程直到完成，然后关闭事件循环。
        与 test_qa_tool_ke_search 风格一致。
    """
    # asyncio.run() 是 Python 3.7+ 的便捷入口，等价于手动 get_event_loop().run_until_complete()
    return asyncio.run(coro)


def test_ke_grep_finds_basic_pattern(tmp_path):
    """grep 能找到字面匹配。

    Args:
        tmp_path: pytest 内置 fixture，提供一个临时目录（pathlib.Path 对象），
                  测试结束后自动清理。
    """
    # tmp_path / "Foo.java"：pathlib.Path 的 / 运算符拼接路径（等价于 os.path.join）
    # .write_text()：将字符串写入文件（UTF-8），文件不存在会自动创建
    (tmp_path / "Foo.java").write_text("import com.x.RedisTemplate;\nclass Foo {}\n")
    (tmp_path / "Bar.java").write_text("class Bar {}\n")

    # 用 str(tmp_path) 转为字符串路径传入构造函数
    tool = build_ke_grep_tool(str(tmp_path))
    result = _run(tool.handler({"pattern": "RedisTemplate"}))

    # assert：断言，条件不满足时抛 AssertionError，pytest 捕获并报错
    assert "matches" in result
    assert len(result["matches"]) == 1
    m = result["matches"][0]
    assert m["path"] == "Foo.java"
    assert m["line"] == 1
    assert "RedisTemplate" in m["text"]


def test_ke_grep_respects_glob(tmp_path):
    """glob 过滤生效：只搜 *.xml，不返回 *.java。"""
    (tmp_path / "Foo.java").write_text("RedisTemplate\n")
    (tmp_path / "Bar.xml").write_text("RedisTemplate\n")

    tool = build_ke_grep_tool(str(tmp_path))
    # glob="**/*.xml" 只匹配 xml 文件
    result = _run(tool.handler({"pattern": "RedisTemplate", "glob": "**/*.xml"}))

    assert len(result["matches"]) == 1
    assert result["matches"][0]["path"] == "Bar.xml"


def test_ke_grep_max_results_truncates(tmp_path):
    """max_results 截断：10 个命中文件只返回 3 条，且 truncated=True。"""
    # range(10)：生成 0..9 的整数序列；for 循环创建 10 个命中文件
    for i in range(10):
        (tmp_path / f"F{i}.txt").write_text("HIT\n")  # f-string：格式化字符串字面量

    tool = build_ke_grep_tool(str(tmp_path))
    result = _run(tool.handler({"pattern": "HIT", "max_results": 3}))

    assert len(result["matches"]) == 3
    # is True：严格判断布尔值（不用 == True，避免隐式转换）
    assert result["truncated"] is True


def test_ke_grep_no_match_returns_empty(tmp_path):
    """无匹配 → matches:[], truncated:False。"""
    (tmp_path / "F.txt").write_text("nothing\n")
    tool = build_ke_grep_tool(str(tmp_path))
    result = _run(tool.handler({"pattern": "RedisTemplate"}))

    assert result["matches"] == []
    assert result["truncated"] is False


def test_ke_grep_config_missing_returns_error():
    """repo_local_path 未配置（None）→ 返回 error 字段，不抛异常。"""
    # 传 None 模拟"管理员未配置源码路径"的情况
    tool = build_ke_grep_tool(None)
    result = _run(tool.handler({"pattern": "X"}))

    assert "error" in result
    # 错误信息应包含 "not configured"，方便前端/日志定位
    assert "not configured" in result["error"]


def test_ke_grep_missing_pattern_returns_error(tmp_path):
    """缺少 pattern 字段 → 返回 error，不抛异常。"""
    tool = build_ke_grep_tool(str(tmp_path))
    # 空 dict：没有 pattern 键
    result = _run(tool.handler({}))

    assert "error" in result
    # 错误信息应提示缺少 "pattern"（大小写不敏感检查）
    assert "pattern" in result["error"].lower()
