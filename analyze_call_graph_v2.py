#!/usr/bin/env python3
"""
调用图拓扑分层分析 V2 — 基于 JavaParser AST 数据

改进点:
1. 使用 AST 级别的 getter/setter 检测 (不依赖有bug的is_setter标记)
2. 从 CALLS 边中过滤掉 getter/setter 调用
3. 过滤无代码方法 (接口方法/外部依赖)
4. 统计出干净的业务调用图层级
"""

import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path


def load_data(json_path: str):
    print(f"加载 structure_facts: {json_path}")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    methods = {}
    class_fields = defaultdict(set)  # class_name -> {field_names}

    for e in data.get("entities", []):
        if e["type"] == "method":
            attrs = e.get("attributes", {})
            methods[e["id"]] = {
                "name": e["name"],
                "class_name": attrs.get("class_name", ""),
                "module_id": e.get("module_id", ""),
                "signature": attrs.get("signature", ""),
                "is_getter": attrs.get("is_getter", False),
                "code_snippet": attrs.get("code_snippet", ""),
                "has_code": bool(attrs.get("code_snippet")),
                "code_len": len(attrs.get("code_snippet", "") or ""),
            }
        elif e["type"] == "field":
            attrs = e.get("attributes", {})
            cn = attrs.get("class_name", "")
            if cn:
                class_fields[cn].add(e["name"])

    calls = []
    for r in data.get("relations", []):
        if r["type"] == "calls":
            calls.append((r["source_id"], r["target_id"]))

    print(f"  方法数: {len(methods)}")
    print(f"  CALLS: {len(calls)}")
    print(f"  有字段信息的类: {len(class_fields)}")
    return methods, calls, class_fields


# ─────────── AST 级别的 getter/setter 检测 ───────────

def bean_decapitalize(name: str) -> str:
    """等价于 java.beans.Introspector.decapitalize()"""
    if not name:
        return name
    if len(name) > 1 and name[0].isupper() and name[1].isupper():
        return name  # PWD, RMB → 保持
    return name[0].lower() + name[1:]


def detect_getter_setter_by_code(method_info: dict, class_fields: dict) -> str:
    """
    基于代码片段的 getter/setter 检测
    返回: "getter" / "setter" / "none"

    检测逻辑 (和 JavaParser bridge 一致):
    1. 方法名匹配: get/is/set 前缀 + Bean命名规范 + 类有对应字段
    2. 代码结构: getter=单return, setter=单赋值(+可选return)
    """
    name = method_info["name"]
    code = method_info.get("code_snippet", "")
    class_name = method_info.get("class_name", "")

    if not code or not class_name:
        return "none"

    # ---- Getter 检测 ----
    if name.startswith("get") and len(name) > 3:
        field = bean_decapitalize(name[3:])
        if field in class_fields.get(class_name, set()):
            # 检查代码结构: 只有一个 return
            body = _extract_body(code)
            if body and _is_single_return(body):
                return "getter"

    if name.startswith("is") and len(name) > 2:
        field = bean_decapitalize(name[2:])
        if field in class_fields.get(class_name, set()):
            body = _extract_body(code)
            if body and _is_single_return(body):
                return "getter"

    # ---- Setter 检测 ----
    if name.startswith("set") and len(name) > 3:
        field = bean_decapitalize(name[3:])
        if field in class_fields.get(class_name, set()):
            body = _extract_body(code)
            if body and _is_simple_assignment(body):
                return "setter"

    return "none"


def _extract_body(code: str) -> str:
    """提取方法体 (第一个 { 到最后一个 } 之间)"""
    first_brace = code.find("{")
    last_brace = code.rfind("}")
    if first_brace == -1 or last_brace == -1 or first_brace >= last_brace:
        return ""
    return code[first_brace + 1: last_brace].strip()


def _is_single_return(body: str) -> bool:
    """检查方法体是否只有一个 return 语句"""
    lines = [l.strip() for l in body.split(";") if l.strip()]
    return len(lines) == 1 and lines[0].strip().startswith("return ")


def _is_simple_assignment(body: str) -> bool:
    """检查方法体是否只有赋值语句(+可选return)"""
    lines = [l.strip() for l in body.split(";") if l.strip()]
    if not lines or len(lines) > 2:
        return False

    has_assign = False
    for line in lines:
        if "=" in line and not line.startswith("return") and not line.startswith("if"):
            # this.field = param 或 field = param
            if "==" not in line:  # 排除比较
                has_assign = True
        elif line.startswith("return"):
            continue  # return this; 允许
        else:
            return False
    return has_assign


# ─────────── 调用图构建 (带过滤) ───────────

def build_filtered_call_graph(methods, calls, class_fields):
    """构建过滤后的调用图"""

    # Step 1: 标记所有 getter/setter
    gs_methods = set()
    gs_stats = {"getter": 0, "setter": 0, "both_flags": 0}

    for mid, info in methods.items():
        # 优先用 is_getter 标记 (已确认正确)
        if info.get("is_getter"):
            gs_methods.add(mid)
            gs_stats["both_flags"] += 1
            continue

        # 用代码检测补充 setter
        gs_type = detect_getter_setter_by_code(info, class_fields)
        if gs_type in ("getter", "setter"):
            gs_methods.add(mid)
            gs_stats[gs_type] += 1

    print(f"\n=== Getter/Setter 检测 (AST级别) ===")
    print(f"  is_getter 标记: {gs_stats['both_flags']}")
    print(f"  代码检测 getter: {gs_stats['getter']}")
    print(f"  代码检测 setter: {gs_stats['setter']}")
    print(f"  总计标记为 g/s: {len(gs_methods)}")

    # Step 2: 无代码方法 (接口方法、外部依赖)
    no_code_methods = {mid for mid, info in methods.items() if not info.get("has_code")}
    print(f"  无代码方法: {len(no_code_methods)}")

    # Step 3: 有意义的方法 (有代码 + 非getter/setter)
    meaningful = {mid for mid in methods if mid not in gs_methods and mid not in no_code_methods}
    print(f"  有意义的业务方法: {len(meaningful)}")

    # Step 4: 构建过滤后的调用图 (只保留 meaningful 方法之间的调用)
    graph = defaultdict(set)
    reverse_graph = defaultdict(set)
    filtered_calls = 0
    gs_calls_removed = 0
    nocode_calls_removed = 0

    for src, tgt in calls:
        if src == tgt:  # 排除自递归
            continue

        # 只保留 meaningful → meaningful 的边
        if src in meaningful and tgt in meaningful:
            graph[src].add(tgt)
            reverse_graph[tgt].add(src)
            filtered_calls += 1
        elif tgt in gs_methods:
            gs_calls_removed += 1
        elif tgt in no_code_methods:
            nocode_calls_removed += 1

    # 也允许 meaningful → no_code (接口方法) 作为叶子终点
    # 但不允许影响层级计算

    print(f"\n=== 过滤后的调用图 ===")
    print(f"  原始 CALLS: {len(calls)}")
    print(f"  过滤后 CALLS: {filtered_calls}")
    print(f"  移除的 getter/setter 调用: {gs_calls_removed}")
    print(f"  移除的无代码方法调用: {nocode_calls_removed}")

    return graph, reverse_graph, meaningful, gs_methods, no_code_methods


# ─────────── 拓扑分层 (无需SCC，已确认无环) ───────────

def topological_levels(graph, reverse_graph, all_nodes):
    """
    直接拓扑排序分层 (已确认调用图是 DAG)
    叶子=Level 0, 逐层递增
    """
    # 计算出度
    out_degree = {n: len(graph.get(n, set())) for n in all_nodes}

    # 叶子: 出度=0
    levels = {}
    queue = deque()
    for n in all_nodes:
        if out_degree[n] == 0:
            levels[n] = 0
            queue.append(n)

    # BFS 向上传播
    while queue:
        node = queue.popleft()
        current_level = levels[node]
        for caller in reverse_graph.get(node, set()):
            new_level = current_level + 1
            if caller not in levels or levels[caller] < new_level:
                levels[caller] = new_level
                queue.append(caller)

    # 没有入度也没有出度的孤立节点
    for n in all_nodes:
        if n not in levels:
            levels[n] = 0

    return levels


# ─────────── 统计输出 ───────────

def print_analysis(methods, method_levels, graph, meaningful, gs_methods, no_code_methods):
    max_level = max(method_levels.values()) if method_levels else 0

    # 层级分布
    level_counts = defaultdict(int)
    level_methods = defaultdict(list)
    for mid, level in method_levels.items():
        level_counts[level] += 1
        level_methods[level].append(mid)

    print(f"\n{'='*80}")
    print(f"  拓扑分层结果 (仅业务方法, 已过滤 getter/setter + 无代码方法)")
    print(f"{'='*80}")

    total = len(meaningful)
    print(f"\n  业务方法总数: {total}")
    print(f"  层级深度: {max_level + 1} 层 (L0 ~ L{max_level})")

    print(f"\n{'层级':>6} | {'方法数':>7} | {'占比':>7} | {'累计':>7} | 说明")
    print("-" * 80)

    cumulative = 0
    for level in range(max_level + 1):
        count = level_counts.get(level, 0)
        cumulative += count
        pct = count / max(total, 1) * 100
        cum_pct = cumulative / max(total, 1) * 100

        if level == 0:
            desc = "叶子: DAO查询/工具方法/简单业务方法 (无下游调用)"
        elif level == 1:
            desc = "底层: 只调用叶子方法"
        elif level == 2:
            desc = "组合: 编排底层方法"
        elif level == 3:
            desc = "高层: 编排组合方法 (通常是Service)"
        elif level == 4:
            desc = "入口: Controller/API/定时任务"
        else:
            desc = "顶层入口"

        print(f"  L{level:>3}  | {count:>6}  | {pct:>5.1f}%  | {cum_pct:>5.1f}%  | {desc}")

    # 按模块分布
    print(f"\n按模块的层级分布:")
    module_stats = defaultdict(lambda: defaultdict(int))
    for mid, level in method_levels.items():
        module_stats[methods[mid]["module_id"]][level] += 1

    for module in sorted(module_stats.keys()):
        total_m = sum(module_stats[module].values())
        dist = ", ".join(f"L{l}:{c}" for l, c in sorted(module_stats[module].items()))
        print(f"  {module:>20}: {total_m:>5} 方法 | {dist}")

    # 各层级代表性方法
    print(f"\n各层级代表性方法:")
    for level in range(min(max_level + 1, 8)):
        mids = level_methods.get(level, [])
        # 按代码长度排序取中间的
        candidates = sorted(mids, key=lambda m: methods[m].get("code_len", 0), reverse=True)

        print(f"\n  ─── Level {level} ({level_counts.get(level, 0)} 个方法) ───")

        for mid in candidates[:5]:
            info = methods[mid]
            callees = graph.get(mid, set())
            callee_names = []
            for cid in list(callees)[:8]:
                ci = methods.get(cid, {})
                callee_names.append(f"{ci.get('class_name','?')}.{ci.get('name','?')}")
            extra = f" +{len(callees)-8}" if len(callees) > 8 else ""
            print(f"    {info['class_name']}.{info['name']}")
            print(f"      模块: {info['module_id']} | 代码: {info['code_len']} 字符 | 调用 {len(callees)} 个方法")
            if callee_names:
                print(f"      → {', '.join(callee_names)}{extra}")

    # 最深方法
    print(f"\n最深层级 TOP 15:")
    for mid, level in sorted(method_levels.items(), key=lambda x: x[1], reverse=True)[:15]:
        info = methods[mid]
        n_callees = len(graph.get(mid, set()))
        print(f"  L{level}: {info['class_name']}.{info['name']} ({info['module_id']}, 调用{n_callees}个, {info['code_len']}字符)")

    # 解读策略估算
    print(f"\n{'='*80}")
    print(f"  自底向上解读策略评估 (AST过滤后)")
    print(f"{'='*80}")

    max_workers = 8
    avg_llm_time = 20  # 秒

    print(f"\n{'层级':>6} | {'方法数':>6} | {'自身代码':>10} | {'下层上下文':>10} | {'预估时间':>10}")
    print("-" * 70)

    total_time = 0
    for level in range(max_level + 1):
        mids = [m for m in level_methods.get(level, []) if m in meaningful]
        count = len(mids)
        if count == 0:
            continue

        avg_code = sum(methods[m]["code_len"] for m in mids) // max(count, 1)
        avg_ctx = 0
        for m in mids:
            n_callees = len(graph.get(m, set()))
            avg_ctx += min(n_callees, 10) * 400  # 每个callee摘要约400字
        avg_ctx = avg_ctx // max(count, 1)

        level_time = (count / max_workers) * avg_llm_time
        total_time += level_time

        print(f"  L{level:>3}  | {count:>5}  | ~{avg_code:>7} 字 | ~{avg_ctx:>7} 字 | ~{level_time/60:>6.1f} 分钟")

    print(f"\n  总方法: {total}")
    print(f"  总时间 ({max_workers} workers): ~{total_time/60:.0f} 分钟")

    return level_methods


def main():
    json_path = "/Users/wangshanhe/Desktop/myproject/knowledge-engineering/out_ui/structure_facts_for_interpret.json"
    if not Path(json_path).exists():
        print(f"ERROR: 找不到 {json_path}")
        sys.exit(1)

    # 1. 加载
    methods, calls, class_fields = load_data(json_path)

    # 2. AST 级别过滤 + 构建调用图
    graph, reverse_graph, meaningful, gs_methods, no_code_methods = \
        build_filtered_call_graph(methods, calls, class_fields)

    # 3. 拓扑分层
    print(f"\n正在计算拓扑层级...")
    method_levels = topological_levels(graph, reverse_graph, meaningful)

    # 4. 分析输出
    print_analysis(methods, method_levels, graph, meaningful, gs_methods, no_code_methods)

    print(f"\n分析完成!")


if __name__ == "__main__":
    main()
