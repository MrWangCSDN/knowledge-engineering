#!/usr/bin/env python3
"""
调用图拓扑分层分析器
分析 mall-swarm 工程的方法调用依赖层级，验证"自底向上解读"策略的可行性。

思路:
1. 从 structure_facts 加载所有 METHOD 实体和 CALLS 关系
2. 构建调用图 (caller → callee)
3. Tarjan 算法找强连通分量 (处理循环依赖)
4. 缩点后拓扑排序
5. 计算每个方法的层级 (叶子=Level 0, 往上递增)
6. 输出层级分布统计
"""

import json
import sys
from collections import defaultdict, deque
from pathlib import Path


# ─────────────────────────── 1. 数据加载 ───────────────────────────

def load_structure_facts(json_path: str) -> dict:
    print(f"加载 structure_facts: {json_path}")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    entities = data.get("entities", [])
    relations = data.get("relations", [])

    methods = {}
    for e in entities:
        if e["type"] == "method":
            methods[e["id"]] = {
                "name": e["name"],
                "class_name": e.get("attributes", {}).get("class_name", ""),
                "module_id": e.get("module_id", ""),
                "signature": e.get("attributes", {}).get("signature", ""),
                "is_getter": e.get("attributes", {}).get("is_getter", False),
                "is_setter": e.get("attributes", {}).get("is_setter", False),
                "has_code": bool(e.get("attributes", {}).get("code_snippet")),
                "code_len": len(e.get("attributes", {}).get("code_snippet", "") or ""),
            }

    calls = []
    for r in relations:
        if r["type"] == "calls":
            src = r["source_id"]
            tgt = r["target_id"]
            if src in methods and tgt in methods:
                calls.append((src, tgt))

    print(f"  方法数: {len(methods)}")
    print(f"  调用关系数: {len(calls)}")
    return methods, calls


# ─────────────────────────── 2. 构建调用图 ───────────────────────────

def build_call_graph(methods: dict, calls: list):
    """构建邻接表: caller -> [callees]"""
    graph = defaultdict(set)       # caller -> callees (正向)
    reverse_graph = defaultdict(set)  # callee -> callers (反向)

    for src, tgt in calls:
        if src != tgt:  # 排除自递归
            graph[src].add(tgt)
            reverse_graph[tgt].add(src)

    # 统计
    all_nodes = set(methods.keys())
    nodes_with_outgoing = set(graph.keys())
    nodes_with_incoming = {n for targets in reverse_graph.values() for n in targets} | set(reverse_graph.keys())
    leaf_nodes = all_nodes - nodes_with_outgoing  # 不调用任何方法
    root_nodes = all_nodes - set(reverse_graph.keys())  # 不被任何方法调用
    isolated_nodes = all_nodes - nodes_with_outgoing - set(reverse_graph.keys())

    print(f"\n调用图统计:")
    print(f"  有调用关系的方法: {len(nodes_with_outgoing | set(reverse_graph.keys()))}")
    print(f"  叶子节点 (不调用其他方法): {len(leaf_nodes)}")
    print(f"  根节点 (不被其他方法调用): {len(root_nodes)}")
    print(f"  孤立节点 (无任何调用关系): {len(isolated_nodes)}")

    # 排除 getter/setter
    getter_setter_count = sum(1 for mid, info in methods.items()
                              if info["is_getter"] or info["is_setter"])
    print(f"  getter/setter 方法: {getter_setter_count}")

    return graph, reverse_graph, leaf_nodes, root_nodes, isolated_nodes


# ─────────────────────────── 3. Tarjan SCC ───────────────────────────

def tarjan_scc(graph: dict, all_nodes: set) -> list:
    """
    Tarjan 强连通分量算法
    返回: list of sets, 每个 set 是一个 SCC
    """
    index_counter = [0]
    stack = []
    lowlink = {}
    index = {}
    on_stack = set()
    result = []

    def strongconnect(v):
        index[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in graph.get(v, set()):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])

        if lowlink[v] == index[v]:
            scc = set()
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.add(w)
                if w == v:
                    break
            result.append(scc)

    # Tarjan 原版用递归, 大图可能爆栈, 这里先用递归版
    # 如果 mall-swarm 8954 个方法导致爆栈, 改用迭代版
    sys.setrecursionlimit(20000)

    for node in all_nodes:
        if node not in index:
            strongconnect(node)

    return result


# ─────────────────────────── 4. 缩点 + 拓扑排序 ───────────────────────────

def condense_and_topo_sort(graph: dict, sccs: list, all_nodes: set):
    """
    1. 将 SCC 缩成超级节点
    2. 构建 DAG
    3. 拓扑排序
    4. 计算层级 (叶子=0, 逐层递增)
    """
    # node -> scc_index 映射
    node_to_scc = {}
    for i, scc in enumerate(sccs):
        for node in scc:
            node_to_scc[node] = i

    # 构建缩点后的 DAG
    n_sccs = len(sccs)
    dag = defaultdict(set)        # scc_i -> {scc_j, ...} (i 调用 j)
    dag_reverse = defaultdict(set) # scc_j -> {scc_i, ...} (j 被 i 调用)

    for src, targets in graph.items():
        src_scc = node_to_scc.get(src)
        if src_scc is None:
            continue
        for tgt in targets:
            tgt_scc = node_to_scc.get(tgt)
            if tgt_scc is not None and src_scc != tgt_scc:
                dag[src_scc].add(tgt_scc)
                dag_reverse[tgt_scc].add(src_scc)

    # 拓扑排序 + 层级计算 (BFS, 从叶子节点开始)
    # 叶子 SCC: 不调用其他 SCC 的
    in_degree = defaultdict(int)  # 这里的 "in_degree" 是出度 (调用了多少个其他SCC)
    # 反过来想: 我们要从叶子(不调用任何人)开始, 所以用反向BFS

    # 计算每个 SCC 的出度 (调用了多少其他 SCC)
    out_degree = {i: len(dag[i]) for i in range(n_sccs)}

    # 叶子: 出度=0 (不调用任何其他 SCC)
    levels = {}
    queue = deque()
    for i in range(n_sccs):
        if out_degree.get(i, 0) == 0:
            levels[i] = 0
            queue.append(i)

    # BFS: 从叶子向上传播层级
    while queue:
        scc_id = queue.popleft()
        current_level = levels[scc_id]
        # 找到所有调用当前 SCC 的 SCC (上游)
        for caller_scc in dag_reverse.get(scc_id, set()):
            # caller 的层级 = max(所有 callee 的层级) + 1
            new_level = current_level + 1
            if caller_scc not in levels or levels[caller_scc] < new_level:
                levels[caller_scc] = new_level
                queue.append(caller_scc)  # 重新传播

    # 处理没有被赋予层级的 SCC (孤立的)
    for i in range(n_sccs):
        if i not in levels:
            levels[i] = 0

    return node_to_scc, sccs, levels, dag


# ─────────────────────────── 5. 统计分析 ───────────────────────────

def analyze_levels(methods: dict, node_to_scc: dict, sccs: list,
                   scc_levels: dict, graph: dict):
    """分析每个方法的层级, 输出统计"""

    # 方法 -> 层级
    method_levels = {}
    for mid in methods:
        scc_id = node_to_scc.get(mid)
        if scc_id is not None:
            method_levels[mid] = scc_levels[scc_id]
        else:
            method_levels[mid] = 0  # 不在调用图中的方法

    # SCC 统计
    multi_sccs = [scc for scc in sccs if len(scc) > 1]
    print(f"\n强连通分量 (SCC) 统计:")
    print(f"  总 SCC 数: {len(sccs)}")
    print(f"  单节点 SCC: {len(sccs) - len(multi_sccs)}")
    print(f"  多节点 SCC (有循环依赖): {len(multi_sccs)}")
    if multi_sccs:
        for i, scc in enumerate(sorted(multi_sccs, key=len, reverse=True)[:10]):
            members = []
            for mid in list(scc)[:5]:
                info = methods.get(mid, {})
                members.append(f"{info.get('class_name', '?')}.{info.get('name', '?')}")
            extra = f" ... +{len(scc)-5}" if len(scc) > 5 else ""
            print(f"    SCC-{i+1} ({len(scc)}个方法): {', '.join(members)}{extra}")

    # 层级分布
    level_counts = defaultdict(int)
    level_methods = defaultdict(list)
    for mid, level in method_levels.items():
        level_counts[level] += 1
        level_methods[level].append(mid)

    max_level = max(level_counts.keys()) if level_counts else 0

    print(f"\n层级分布 (共 {max_level + 1} 层):")
    print(f"{'层级':>6} | {'方法数':>8} | {'占比':>8} | {'累计':>8} | 说明")
    print("-" * 75)

    cumulative = 0
    total = len(methods)
    level_descriptions = {
        0: "叶子节点: DAO/工具/getter/setter/无调用方法",
        1: "底层方法: 只调用叶子节点",
        2: "组合方法: 调用底层方法",
        3: "高层方法: 编排组合方法",
        4: "入口方法: Controller/API",
    }

    for level in range(max_level + 1):
        count = level_counts.get(level, 0)
        cumulative += count
        pct = count / total * 100
        cum_pct = cumulative / total * 100
        desc = level_descriptions.get(level, "更高层级")
        print(f"  L{level:>3}  | {count:>7}  | {pct:>6.1f}%  | {cum_pct:>6.1f}%  | {desc}")

    # 按模块分布
    print(f"\n按模块的层级分布:")
    module_level_counts = defaultdict(lambda: defaultdict(int))
    for mid, level in method_levels.items():
        module = methods[mid].get("module_id", "unknown")
        module_level_counts[module][level] += 1

    for module in sorted(module_level_counts.keys()):
        total_in_module = sum(module_level_counts[module].values())
        level_dist = ", ".join(f"L{l}:{c}" for l, c in sorted(module_level_counts[module].items()))
        print(f"  {module:>20}: {total_in_module:>5} 方法 | {level_dist}")

    # 各层级代表性方法抽样
    print(f"\n各层级代表性方法 (排除getter/setter):")
    for level in range(min(max_level + 1, 8)):
        level_mids = level_methods.get(level, [])
        # 排除 getter/setter, 优先选有代码的
        candidates = [mid for mid in level_mids
                      if methods[mid].get("has_code")
                      and not methods[mid].get("is_getter")
                      and not methods[mid].get("is_setter")]

        # 按代码长度排序, 取中等长度的
        candidates.sort(key=lambda m: methods[m].get("code_len", 0))

        if not candidates:
            candidates = level_mids[:3]

        print(f"\n  === Level {level} ({level_counts.get(level, 0)} 个方法) ===")
        # 取 3 个样本: 短/中/长
        samples = []
        if len(candidates) >= 3:
            samples = [candidates[0], candidates[len(candidates)//2], candidates[-1]]
        else:
            samples = candidates[:3]

        for mid in samples:
            info = methods[mid]
            callees = graph.get(mid, set())
            callee_names = []
            for cid in list(callees)[:5]:
                cinfo = methods.get(cid, {})
                callee_names.append(f"{cinfo.get('class_name', '?')}.{cinfo.get('name', '?')}")

            callee_str = ", ".join(callee_names) if callee_names else "(无)"
            extra_callees = f" +{len(callees)-5}个" if len(callees) > 5 else ""

            print(f"    {info.get('class_name', '?')}.{info.get('name', '?')}")
            print(f"      模块: {info.get('module_id', '?')} | 代码长度: {info.get('code_len', 0)} 字符")
            print(f"      调用: {callee_str}{extra_callees}")

    # 深度分析: 调用链最深的方法
    print(f"\n最深层级方法 TOP 10:")
    deep_methods = sorted(method_levels.items(), key=lambda x: x[1], reverse=True)[:10]
    for mid, level in deep_methods:
        info = methods[mid]
        callees_count = len(graph.get(mid, set()))
        print(f"  L{level}: {info.get('class_name', '?')}.{info.get('name', '?')} "
              f"(模块: {info.get('module_id', '?')}, 调用{callees_count}个方法)")

    # getter/setter 层级分析
    gs_level_counts = defaultdict(int)
    for mid, level in method_levels.items():
        if methods[mid].get("is_getter") or methods[mid].get("is_setter"):
            gs_level_counts[level] += 1

    total_gs = sum(gs_level_counts.values())
    gs_in_l0 = gs_level_counts.get(0, 0)
    print(f"\ngetter/setter 分布:")
    print(f"  总计: {total_gs}")
    print(f"  在 Level 0: {gs_in_l0} ({gs_in_l0/max(total_gs,1)*100:.1f}%)")
    if any(l > 0 for l in gs_level_counts):
        print(f"  在 Level 1+: {total_gs - gs_in_l0} (异常, getter/setter不应调用其他方法)")

    return method_levels, level_methods


# ─────────────────────────── 6. 解读策略评估 ───────────────────────────

def estimate_interpretation_cost(methods: dict, method_levels: dict,
                                 level_methods: dict, graph: dict):
    """评估自底向上解读的成本"""

    max_level = max(method_levels.values()) if method_levels else 0

    print(f"\n{'='*75}")
    print(f"自底向上解读策略评估")
    print(f"{'='*75}")

    # 排除 getter/setter 后的实际解读数
    interpretable = {mid: level for mid, level in method_levels.items()
                     if not methods[mid].get("is_getter")
                     and not methods[mid].get("is_setter")
                     and methods[mid].get("has_code")}

    print(f"\n需要解读的方法: {len(interpretable)} (排除getter/setter和无代码方法)")

    # 按层级统计 prompt 大小
    print(f"\n按层级的 Prompt 复杂度估算:")
    print(f"{'层级':>6} | {'方法数':>6} | {'自身代码':>10} | {'下层上下文':>10} | {'总prompt':>10} | {'并行度':>6}")
    print("-" * 80)

    total_tokens = 0
    total_time = 0
    max_workers = 8

    for level in range(max_level + 1):
        level_interpretable = [mid for mid in level_methods.get(level, []) if mid in interpretable]
        count = len(level_interpretable)
        if count == 0:
            continue

        # 估算每个方法的 prompt 大小
        avg_code_len = 0
        avg_callee_context = 0
        for mid in level_interpretable:
            avg_code_len += methods[mid].get("code_len", 0)
            # 每个 callee 的解读摘要约 300 字符
            callee_count = len(graph.get(mid, set()))
            avg_callee_context += min(callee_count, 10) * 300  # 最多取10个callee的摘要

        avg_code_len = avg_code_len // max(count, 1)
        avg_callee_context = avg_callee_context // max(count, 1)
        total_prompt = avg_code_len + avg_callee_context

        # token 估算 (中文约 1.5 字符/token, 英文约 4 字符/token, 混合取 2.5)
        tokens_per_method = total_prompt / 2.5
        level_tokens = tokens_per_method * count
        total_tokens += level_tokens

        # 时间估算: 每次 LLM 调用约 15-30 秒
        avg_llm_time = 20  # 秒
        level_time = (count / max_workers) * avg_llm_time  # 并行
        total_time += level_time

        print(f"  L{level:>3}  | {count:>5}  | ~{avg_code_len:>7} 字 | ~{avg_callee_context:>7} 字 | ~{total_prompt:>7} 字 | {min(count, max_workers):>5}")

    print(f"\n总估算:")
    print(f"  总 token: ~{total_tokens/1000:.0f}K tokens")
    print(f"  总时间 ({max_workers} workers): ~{total_time/60:.0f} 分钟")
    print(f"  比无序解读多出: ~{total_time*0.25/60:.0f} 分钟 (层间等待开销)")


# ─────────────────────────── MAIN ───────────────────────────

def main():
    json_path = "/Users/wangshanhe/Desktop/myproject/knowledge-engineering/out_ui/structure_facts_for_interpret.json"

    if not Path(json_path).exists():
        print(f"ERROR: 找不到 {json_path}")
        print("请先运行 pipeline 生成 structure_facts")
        sys.exit(1)

    # 1. 加载数据
    methods, calls = load_structure_facts(json_path)

    # 2. 构建调用图
    graph, reverse_graph, leaf_nodes, root_nodes, isolated_nodes = build_call_graph(methods, calls)

    # 3. Tarjan SCC
    print(f"\n正在计算强连通分量 (Tarjan SCC)...")
    all_method_ids = set(methods.keys())
    sccs = tarjan_scc(dict(graph), all_method_ids)

    # 4. 缩点 + 拓扑排序
    print(f"正在执行拓扑排序...")
    node_to_scc, sccs, scc_levels, dag = condense_and_topo_sort(
        dict(graph), sccs, all_method_ids
    )

    # 5. 统计分析
    method_levels, level_methods = analyze_levels(
        methods, node_to_scc, sccs, scc_levels, dict(graph)
    )

    # 6. 解读策略评估
    estimate_interpretation_cost(methods, method_levels, level_methods, dict(graph))

    print(f"\n分析完成!")


if __name__ == "__main__":
    main()
