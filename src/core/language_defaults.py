"""
仓库语言 / 源码高亮等 UI 与解析的默认约定。

结构层解析器仍以当前 Java 实现为主；高亮语言随 ``repo.language`` 配置变化。
"""
from __future__ import annotations

DEFAULT_REPO_LANGUAGE = "java"
"""未配置 ``repo.language`` 时，源码块语法高亮等使用的默认语言标识。"""
