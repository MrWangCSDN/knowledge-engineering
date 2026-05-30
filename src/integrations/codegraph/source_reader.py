# src/integrations/codegraph/source_reader.py
"""按 CodeGraph 节点的 file_path + 行号，从仓库源码里切出方法片段。

CodeGraph 的 nodes 表不存方法体，只有 file_path/start_line/end_line，
所以代码片段要回源码文件读。行号是 1-indexed、闭区间 [start, end]。
"""
from __future__ import annotations  # PEP 563：允许在类型注解里提前引用未定义的类名

import os  # 标准库 os：文件路径拼接、文件系统操作


def read_snippet(repo_root: str, file_path: str, start_line: int, end_line: int) -> str:
    """读 <repo_root>/<file_path> 的第 start_line~end_line 行（1-indexed，含两端）。

    文件读不到 / 行号非法 → 返回空串（调用方据此跳过，不让单个文件失败拖垮整轮）。

    Args:
        repo_root:  源码仓库根目录绝对路径（如 '/opt/repos/mall-swarm'）
        file_path:  相对路径（来自 CodeGraph nodes.file_path），如 'Ctrl.java'
        start_line: 起始行号，1-indexed（含）
        end_line:   结束行号，1-indexed（含）
    Returns:
        方法体文本（多行用 '\\n' 连接，结尾不带换行）；读不到时返回空串
    """
    # os.path.join：跨平台拼接路径；会自动处理末尾斜杠等边界情况
    full = os.path.join(repo_root, file_path)       # 拼绝对路径
    try:
        # open() 内置函数：打开文件；encoding="utf-8" 指定字符编码
        # errors="replace"：遇到非法编码用占位符替代，不抛异常（比 strict 模式更健壮）
        # with ... as fh：上下文管理器，块结束自动关闭文件句柄（即使发生异常也关）
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()                  # readlines：按行读成列表（每行带 '\n'）
    except OSError:
        # OSError 是文件不存在/无读权限等 IO 错误的基类
        return ""                                   # 文件不存在/不可读 → 空串
    if start_line < 1 or end_line < start_line:     # 行号非法校验 → 空串
        return ""
    # 列表切片：Python 列表是 0-indexed、右开区间
    # 1-indexed 闭区间 [start, end] 对应 Python 切片 [start-1 : end]
    chunk = lines[start_line - 1:end_line]
    # "".join(iterable)：把列表拼成一个字符串（带行内换行符）
    # .rstrip("\n")：去掉末尾多余换行（readlines 最后一行也可能带 '\n'）
    return "".join(chunk).rstrip("\n")              # 拼回字符串，去掉结尾多余换行
