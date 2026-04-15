#!/usr/bin/env python3
"""
需求覆盖率分析 — 端到端 Demo

完整流程:
  Step 1: 需求拆解 (按业务点分项)
  Step 2: 模式A 向量召回 (每项找候选方法)
  Step 3: 碎片归集 (找到主入口方法)
  Step 4: 模式B 调用链展开 (探出完整路径)
  Step 5: LLM 差异分析 (逐条对比覆盖情况)
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict, deque
from typing import Any

for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"
sys.path.insert(0, ".")

from src.semantic.embedding import get_embedding, cosine_similarity
from src.knowledge.weaviate_interpretation_store import WeaviateMethodInterpretStore
from src.knowledge.interpretation_item_helpers import extract_summary
import yaml
import weaviate
from weaviate.classes.init import Auth
from weaviate.classes.query import MetadataQuery


# ═══════════════════════════════════════════════════════════
# 真实业务需求 (产品经理写的)
# ═══════════════════════════════════════════════════════════
REQUIREMENT = """
【商城订单创建功能】

业务场景:
用户在商城 APP 选购商品加入购物车后，进入结算页面，填写收货地址、选择优惠券和是否使用积分，点击"提交订单"按钮完成下单。

功能点清单:
1. 提交订单前必须选择收货地址，如未选择应拒绝下单
2. 获取购物车中待结算商品列表，关联查询促销信息
3. 校验所有商品的库存是否充足，库存不足应拒绝下单
4. 若用户使用了优惠券，需验证优惠券可用性并分摊优惠金额到各订单项
5. 若用户使用了积分，需计算积分抵扣金额，有最高抵扣比例限制
6. 扣减订单商品的SKU库存(锁定库存,防止超卖)
7. 计算订单总金额、优惠金额、运费、实付金额
8. 生成订单主记录和订单明细记录
9. 下单成功后清空购物车中已结算的商品
10. 发送延时消息,用于订单超时未支付自动取消
"""


def log_step(msg, level=0):
    prefix = "  " * level
    print(f"{prefix}{msg}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/project.yaml")
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    vc = cfg["knowledge"]["vectordb-interpret"]

    # 连接 Weaviate
    client = weaviate.connect_to_local(
        host="localhost", port=8080, grpc_port=50051,
        auth_credentials=Auth.api_key(vc["weaviate_api_key"]),
    )

    # 加载 structure_facts (用于调用链分析)
    with open("out_ui/structure_facts_for_interpret.json") as f:
        data = json.load(f)
    methods = {e["id"]: e for e in data["entities"] if e["type"] == "method"}

    call_graph = defaultdict(set)
    for r in data["relations"]:
        if r["type"] == "calls":
            s, t = r["source_id"], r["target_id"]
            if s != t and s in methods and t in methods:
                call_graph[s].add(t)

    # 计算方法层级 (简化版)
    meaningful = {mid for mid, m in methods.items()
                  if (m.get("attributes") or {}).get("code_snippet")
                  and not (m.get("attributes") or {}).get("is_getter")
                  and not (m.get("attributes") or {}).get("is_setter")}

    reverse_graph = defaultdict(set)
    for src in call_graph:
        for tgt in call_graph[src]:
            reverse_graph[tgt].add(src)

    out_deg = {n: len(call_graph.get(n, set()) & meaningful) for n in meaningful}
    levels = {}
    q = deque()
    for n in meaningful:
        if out_deg[n] == 0:
            levels[n] = 0
            q.append(n)
    while q:
        node = q.popleft()
        for caller in reverse_graph.get(node, set()):
            if caller in meaningful:
                lv = levels[node] + 1
                if caller not in levels or levels[caller] < lv:
                    levels[caller] = lv
                    q.append(caller)
    for n in meaningful:
        if n not in levels:
            levels[n] = 0

    def mlabel(mid):
        m = methods.get(mid, {})
        cn = m.get("attributes", {}).get("class_name", "")
        return f"{cn}.{m.get('name', '?')}"

    try:
        coll = client.collections.get(vc["collection_name"])

        # ═══════════════════════════════════════════════════════════
        # STEP 1: 需求拆解
        # ═══════════════════════════════════════════════════════════
        print("=" * 80)
        print("  STEP 1: 需求拆解")
        print("=" * 80)
        print(REQUIREMENT)

        # 拆解为子需求项 (实际产品可用 LLM 做, 这里手工列出)
        requirement_items = [
            ("收货地址校验", "提交订单前必须选择收货地址，如未选择应拒绝下单"),
            ("购物车商品查询", "获取购物车中待结算商品列表，关联查询促销信息"),
            ("库存校验", "校验所有商品的库存是否充足，库存不足应拒绝下单"),
            ("优惠券处理", "若用户使用了优惠券，需验证优惠券可用性并分摊优惠金额到各订单项"),
            ("积分抵扣", "若用户使用了积分，需计算积分抵扣金额"),
            ("库存扣减", "扣减订单商品的SKU库存(锁定库存,防止超卖)"),
            ("订单金额计算", "计算订单总金额、优惠金额、运费、实付金额"),
            ("订单持久化", "生成订单主记录和订单明细记录"),
            ("购物车清空", "下单成功后清空购物车中已结算的商品"),
            ("超时取消消息", "发送延时消息,用于订单超时未支付自动取消"),
        ]
        print(f"\n拆解为 {len(requirement_items)} 个子需求项\n")

        # ═══════════════════════════════════════════════════════════
        # STEP 2: 模式A 向量召回
        # ═══════════════════════════════════════════════════════════
        print("=" * 80)
        print("  STEP 2: 模式A 向量召回 (每个子需求找候选方法)")
        print("=" * 80)

        all_candidates = defaultdict(float)  # method_id → 累计分数
        candidate_coverage = defaultdict(list)  # method_id → 命中了哪些子需求
        item_top_hits = {}  # item → top hits

        for item_name, item_desc in requirement_items:
            q_vec = get_embedding(item_desc, 1024)
            results = coll.query.near_vector(
                near_vector=q_vec,
                limit=args.top_k,
                return_metadata=MetadataQuery(distance=True),
                return_properties=["method_entity_id", "class_name", "method_name",
                                    "interpretation_text"],
            )

            hits = []
            print(f"\n  【{item_name}】{item_desc[:40]}")
            print(f"    Top {args.top_k} 召回:")
            for r in results.objects:
                p = r.properties
                mid = p["method_entity_id"]
                score = 1 - (r.metadata.distance if r.metadata else 1)
                hits.append((mid, score, p))

                # 累计命中
                all_candidates[mid] += score
                candidate_coverage[mid].append(item_name)

                # 摘要
                text = p.get("interpretation_text", "")
                summary = ""
                if "[摘要]" in text:
                    summary = text.split("[摘要]")[1].split("[详情]")[0].strip().split("\n")[0][:45]
                cn = p.get("class_name", "")
                mn = p.get("method_name", "")
                print(f"      [{score:.3f}] L{levels.get(mid,0)} {cn}.{mn}")
                print(f"              → {summary}")

            item_top_hits[item_name] = hits

        # ═══════════════════════════════════════════════════════════
        # STEP 3: 碎片归集 - 找主入口
        # ═══════════════════════════════════════════════════════════
        print("\n" + "=" * 80)
        print("  STEP 3: 碎片归集 → 找主入口方法")
        print("=" * 80)

        # 所有候选方法打分: 层级高 + 命中多个子需求 + cos 高
        entry_scores = []
        for mid, cum_score in all_candidates.items():
            lv = levels.get(mid, 0)
            coverage_count = len(candidate_coverage[mid])
            # 分数 = 层级权重 × 10 + 命中子需求数 × 5 + 累计cos × 2
            final_score = lv * 10 + coverage_count * 5 + cum_score * 2
            entry_scores.append((mid, final_score, lv, coverage_count, cum_score))

        entry_scores.sort(key=lambda x: -x[1])

        print(f"\n  候选入口 Top 10 (按综合得分):")
        print(f"  {'得分':>7} {'层级':>4} {'命中子需求数':>10} {'累计cos':>9} {'方法':>50}")
        print(f"  {'-'*7} {'-'*4} {'-'*10} {'-'*9} {'-'*50}")
        for mid, score, lv, cov, cum in entry_scores[:10]:
            print(f"  {score:>6.1f} L{lv:<3} {cov:>9} {cum:>9.3f} {mlabel(mid)[:50]}")

        # 选出综合得分最高的 L3+ 方法作为主入口
        entry_mid = None
        for mid, score, lv, cov, cum in entry_scores:
            if lv >= 2 and cov >= 3:  # 至少 L2 且命中 3+ 子需求
                entry_mid = mid
                break
        if not entry_mid:
            entry_mid = entry_scores[0][0]

        print(f"\n  ★ 选定主入口: {mlabel(entry_mid)} (L{levels.get(entry_mid, 0)})")

        # ═══════════════════════════════════════════════════════════
        # STEP 4: 模式B 调用链展开
        # ═══════════════════════════════════════════════════════════
        print("\n" + "=" * 80)
        print("  STEP 4: 从入口 BFS 展开完整调用链")
        print("=" * 80)

        visited = set()
        chain = []  # (mid, depth)
        bfs = deque([(entry_mid, 0)])
        while bfs:
            mid, depth = bfs.popleft()
            if mid in visited or depth > 3:
                continue
            visited.add(mid)
            if mid not in meaningful:
                continue
            chain.append((mid, depth))
            for cid in call_graph.get(mid, set()):
                if cid not in visited and cid in methods:
                    bfs.append((cid, depth + 1))

        print(f"\n  完整调用链 ({len(chain)} 个方法, 最大深度 3):")
        for mid, depth in chain:
            indent = "  " + "│ " * depth
            print(f"{indent}└─ L{levels.get(mid,0)} {mlabel(mid)}")

        # 收集调用链中每个方法的解读
        chain_interpretations = {}
        for mid, _ in chain:
            # 从 Weaviate 读解读
            from weaviate.classes.query import Filter
            r = coll.query.fetch_objects(
                filters=Filter.by_property("method_entity_id").equal(mid),
                limit=1,
                return_properties=["interpretation_text"],
            )
            if r.objects:
                text = r.objects[0].properties.get("interpretation_text", "")
                if "[摘要]" in text:
                    summary = text.split("[摘要]")[1].split("[详情]")[0].strip().split("\n")[0]
                else:
                    summary = ""
                chain_interpretations[mid] = {
                    "full": text,
                    "summary": summary,
                }

        print(f"\n  调用链上获取到 {len(chain_interpretations)} 条解读")

        # ═══════════════════════════════════════════════════════════
        # STEP 5: LLM 差异分析
        # ═══════════════════════════════════════════════════════════
        print("\n" + "=" * 80)
        print("  STEP 5: LLM 差异分析 (逐条判断需求覆盖情况)")
        print("=" * 80)

        # 构建调用链上下文
        chain_context_lines = []
        for mid, depth in chain:
            m = methods[mid]
            indent = "  " * depth
            summary = chain_interpretations.get(mid, {}).get("summary", "")
            if summary:
                chain_context_lines.append(f"{indent}- {mlabel(mid)}: {summary}")

        chain_context = "\n".join(chain_context_lines)

        # 子需求列表
        items_text = "\n".join(f"  {i+1}. {name}: {desc}"
                                for i, (name, desc) in enumerate(requirement_items))

        prompt = f"""你是需求覆盖率分析师。请基于下面的代码调用链，判断每个子需求是否被实现。

## 需求
{REQUIREMENT}

## 子需求清单
{items_text}

## 实际代码调用链 (入口: {mlabel(entry_mid)})
{chain_context}

## 请输出分析报告

对每个子需求, 输出一行:
  - 序号. 子需求名称: ✅已覆盖(方法名) / ❌未覆盖 / ⚠️部分覆盖(方法名, 但缺什么)

最后总结:
  - 覆盖率: X/10
  - 缺失项: 列出未覆盖的子需求
  - 风险点: 代码实现中是否有违反需求的地方

仅输出分析报告, 不要废话。
"""

        # 调 MiniMax (Qwen 经常 timeout)
        mi = cfg["knowledge"]["method_interpretation"]
        provider = mi["multi_providers"][1]  # MiniMax

        print("\n  调用 LLM 进行需求差异分析... (prompt 大小: {} 字)".format(len(prompt)))
        t0 = time.time()

        url = provider["openai_base_url"].rstrip("/") + "/chat/completions"
        payload = {
            "model": provider["openai_model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2000,
            "temperature": 0.1,
        }
        req = urllib.request.Request(url,
            json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {provider['openai_api_key']}"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())["choices"][0]["message"]["content"].strip()

        # 清除 think 标签
        import re
        result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()

        print(f"  LLM 返回 ({time.time()-t0:.0f}秒, {len(result)} 字):\n")
        print("─" * 80)
        print(result)
        print("─" * 80)

    finally:
        client.close()


if __name__ == "__main__":
    main()
