"""导出 13 个 agent 工具的 name / description / input_schema — TS 移植对照物②。

跑法：
    venv/bin/python scripts/export_tool_schemas.py

原理：工具 = 元数据 + handler；导出只读元数据，所以所有后端依赖
（graph / store）一律用 MagicMock 占位，handler 永远不会被调用。
MagicMock 是 Python unittest.mock 提供的"万能代理对象"：任意属性访问、
任意方法调用都不会抛 AttributeError，返回另一个 MagicMock，适合在只需
"装配"但不需要"运行"时替换真实后端依赖。

产物：
    docs/porting/agent-tools-schema.json   13 个工具的元数据（name/description/input_schema）
"""
from __future__ import annotations

# sys：手动操控 Python 的模块搜索路径列表（sys.path）
import sys
# inspect：内省函数签名（Signature / Parameter），用来动态探知必填参数
import inspect
# json：把 Python 数据结构序列化为 JSON 字符串落盘
import json
# Path：面向对象路径操作，比字符串拼接更安全（自动处理分隔符）
from pathlib import Path
# MagicMock：unittest.mock 提供的万能桩对象（见模块 docstring）
from unittest.mock import MagicMock

# 将项目根目录加入 sys.path，使 `from src.xxx` 形式的导入在脚本直接跑时也能找到模块
# （pytest 由根目录 conftest.py 把项目根插入 sys.path 来支持 `from src.xxx`；脚本直接跑时
#   没有 pytest 介入，需在这里手动插入，效果与 conftest.py 的 sys.path.insert 等价）
_ROOT = Path(__file__).resolve().parent.parent   # scripts/ → 项目根
if str(_ROOT) not in sys.path:
    # insert(0, ...) 优先级最高：确保 src.xxx 找到项目内版本，而非全局安装的同名包
    sys.path.insert(0, str(_ROOT))

# 工具注册表工厂（装 12 个工具，含 render_call_graph）
from src.service.qa_engine.tools import build_default_registry           # noqa: E402
# render_call_graph 单独工厂（用于检验是否已在 registry；若未装入则补装）
from src.service.qa_engine.tools.render_call_graph import (              # noqa: E402
    build_render_call_graph_tool,
)


def main() -> None:
    """导出全量 agent 工具 I/O schema 到 docs/porting/agent-tools-schema.json。

    无参数，无返回值。
    产物路径锚定项目根（_ROOT），重复运行内容确定性，git diff 为空。

    执行流程：
    1. 用 MagicMock 占位所有后端依赖，调用 build_default_registry() 构建注册表
    2. 确认 render_call_graph 已在注册表内（v1.4 已合入 build_default_registry）
       若因某次重构被移出，则用 inspect.signature 自动补装，保证健壮性
    3. 按 name 排序后断言恰好 13 个工具
    4. 序列化写入 JSON
    """
    # MagicMock(name=...) 的 name 只是调试标签，不影响任何行为
    # graph / interpretation_store / code_store / method_interp_store 全为 MagicMock：
    # 工厂函数只是把它们塞进闭包，装配阶段不会实际调用任何方法
    reg = build_default_registry(
        graph=MagicMock(name="graph"),
        interpretation_store=MagicMock(name="interpretation_store"),
        project_id="porting-export",         # 非空字符串 → ke_search 闭包正常注册
        code_store=MagicMock(name="code_store"),                   # 非 None → ke_read_entity 注册
        method_interp_store=MagicMock(name="method_interp_store"), # 非 None → ke_method_interp 注册
        repo_local_path=".",                 # 非 None → 4 个文件类工具（ke_grep/ke_glob/ke_read_file/ke_ls）注册
    )

    # list_tools() 返回注册表中所有 Tool 的列表（按注册顺序）
    tools = list(reg.list_tools())

    # render_call_graph 在 v1.4 已并入 build_default_registry（见 __init__.py 第 112 行）
    # 此处做防御性检查：若未来重构把它移出 registry，自动补装，保证脚本健壮
    if "render_call_graph" not in {t.name for t in tools}:
        # inspect.signature 获取函数签名对象（包含所有参数的名称、默认值、类型注解）
        # 等效于：手动查函数定义，但这里用代码自动探知，避免硬编码参数名
        sig = inspect.signature(build_render_call_graph_tool)

        # 字典推导式：筛选"必填参数"（无默认值 + 非 *args/**kwargs 类型）
        # p.default is inspect.Parameter.empty 表示该参数没有默认值（即必填）
        # p.kind in (...) 过滤掉 *args（VAR_POSITIONAL）/ **kwargs（VAR_KEYWORD）
        kwargs = {
            p.name: MagicMock(name=p.name)
            for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        }

        # ** 解包字典为关键字参数（等效于 build_render_call_graph_tool(graph=MagicMock(...))）
        tools.append(build_render_call_graph_tool(**kwargs))

    # 生成器表达式 + sorted：把每个 Tool 对象映射为只含三个字段的 dict，
    # 再按 name 字母序排列 → 产物顺序确定，git diff 稳定
    payload = sorted(
        (
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ),
        key=lambda d: d["name"],   # lambda：匿名函数，这里取 dict 的 "name" 字段做排序键
    )

    # 仅提取名字用于断言消息和打印
    names = [d["name"] for d in payload]

    # assert：运行时断言（不满足则 AssertionError 中止并打印诊断信息）
    # 若 count != 13，说明有工具被意外添加/删除，脚本强制失败而非静默产出错误产物
    assert len(payload) == 13, f"期望 13 个工具，实际 {len(payload)}: {names}"

    # 锚定项目根，确保无论在哪个目录执行脚本，产物路径都一致
    out = _ROOT / "docs" / "porting" / "agent-tools-schema.json"
    # parents=True：自动创建所有缺失的父目录；exist_ok=True：目录已存在不报错
    out.parent.mkdir(parents=True, exist_ok=True)

    # ensure_ascii=False：中文字符直接写 UTF-8，不转义为 \uXXXX
    # indent=2：缩进 2 格，产物人读友好
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 13 tools → {out}")
    print(names)


# Python 惯用法：只有「直接运行」该文件时才执行 main()
# 被 import 时 __name__ == 模块名，不执行，避免副作用
if __name__ == "__main__":
    main()
