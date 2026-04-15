#!/usr/bin/env python3
"""
摘要提取 + 真实向量化

功能：
  1. 清空 Weaviate 中的 MethodInterpretation / BusinessInterpretation 集合
  2. 从 structure_facts 中读取已有解读（如果存在），或从 Weaviate 备份读取
  3. 用 MiniMax 从解读文本中提取关键词摘要 (≤50字)
  4. 用 bge-m3 对摘要做真实 embedding
  5. 写入 Weaviate（summary 前缀 + 原文 + 真实向量）

用法：
  source venv/bin/activate
  PYTHONPATH=. python regenerate_summaries.py [--dry-run] [--max N] [--workers N]
"""
import argparse
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

# 清除代理
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"

sys.path.insert(0, ".")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("regenerate_summaries.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

SUMMARY_PREFIX = "[摘要]"
DETAIL_PREFIX = "[详情]"

# ─────────── LLM 调用 ───────────

class MiniMaxLLM:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def generate(self, prompt: str, timeout: int = 60, max_tokens: int = 200) -> str:
        url = self.base_url + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            js = json.loads(resp.read())
        return js["choices"][0]["message"]["content"].strip()


SUMMARY_PROMPT = """请从以下技术/业务解读中提取关键词摘要。

要求：
- 输出一行，不超过50个中文字符
- 关键词/短语密集排列，用空格分隔
- 包含：业务动作、涉及对象、技术手段
- 不要完整句子，不要"该方法""首先"等无信息量词
- 仅输出摘要，不要任何前缀、编号或解释

解读原文：
{text}

关键词摘要："""


def extract_summary(llm: MiniMaxLLM, text: str) -> Optional[str]:
    """用 LLM 从解读文本提取关键词摘要"""
    truncated = text[:3000]
    prompt = SUMMARY_PROMPT.format(text=truncated)
    try:
        result = llm.generate(prompt, timeout=60, max_tokens=150)
    except Exception as e:
        log.warning("LLM 调用失败: %s", e)
        return None

    if not result:
        return None

    # 清理: 去掉 <think>...</think>, 取第一行, 限制长度
    if "<think>" in result:
        idx = result.find("</think>")
        if idx >= 0:
            result = result[idx + len("</think>"):]
    result = result.strip().split("\n")[0].strip()
    # 去掉常见前缀
    for prefix in ["关键词摘要：", "关键词：", "摘要：", "[摘要]", "摘要:"]:
        if result.startswith(prefix):
            result = result[len(prefix):].strip()
    # 限制50字
    if len(result) > 50:
        result = result[:50]
    if len(result) < 5:
        return None
    return result


def format_text(summary: str, original_text: str) -> str:
    """组合 [摘要] + [详情] 格式"""
    return f"{SUMMARY_PREFIX} {summary}\n\n{DETAIL_PREFIX}\n{original_text}"


# ─────────── 分页读取 Weaviate ───────────

def iter_all_records(store, text_field: str, id_field: str,
                     extra_fields: list[str], page_size: int = 500):
    """分页读取集合中所有记录"""
    coll = store._get_collection()
    all_fields = [id_field, text_field] + extra_fields
    fetched = 0
    while True:
        try:
            result = coll.query.fetch_objects(
                limit=page_size, offset=fetched,
                return_properties=all_fields,
            )
        except TypeError:
            result = coll.query.fetch_objects(
                limit=200000, return_properties=all_fields,
            )
            for obj in (result.objects or []):
                yield obj.properties or {}
            return

        objs = result.objects or []
        if not objs:
            break
        for obj in objs:
            yield obj.properties or {}
        fetched += len(objs)
        if len(objs) < page_size:
            break


# ─────────── 方法解读处理 ───────────

def process_method_records(store, llm, dim, max_count=0, workers=4, dry_run=False):
    """处理 MethodInterpretation 集合"""
    from src.semantic.embedding import get_embedding

    log.info("读取 MethodInterpretation 全量数据...")
    records = list(iter_all_records(
        store,
        text_field="interpretation_text",
        id_field="method_entity_id",
        extra_fields=["class_entity_id", "class_name", "method_name",
                       "signature", "context_summary", "language",
                       "related_entity_ids_json"],
    ))
    log.info("  读取到 %d 条记录", len(records))

    if max_count > 0:
        records = records[:max_count]
        log.info("  限制处理 %d 条", max_count)

    if dry_run:
        log.info("DRY RUN: 不会清空/写入 Weaviate")
        return {"total": len(records), "ok": 0, "fail": 0}

    # 清空集合
    log.info("清空 MethodInterpretation 集合...")
    store.clear()
    log.info("  已清空")

    ok, fail = 0, 0
    lock = threading.Lock()

    def process_one(rec):
        mid = rec.get("method_entity_id", "")
        text = rec.get("interpretation_text", "")
        if not text or not mid:
            return False, mid

        # 提取原始文本 (如果已有 [摘要] 格式则取 [详情] 部分)
        original = text
        if DETAIL_PREFIX in text:
            original = text[text.index(DETAIL_PREFIX) + len(DETAIL_PREFIX):].strip()
        elif text.startswith(SUMMARY_PREFIX):
            # 只有摘要没有详情标记
            original = text[text.index("\n"):].strip() if "\n" in text else text

        # LLM 提取关键词
        summary = extract_summary(llm, original)
        if not summary:
            # 回退: 用方法名+类名
            summary = f"{rec.get('class_name', '')} {rec.get('method_name', '')}"

        # 真实 embedding
        vec = get_embedding(summary, dim)

        # 组合文本
        new_text = format_text(summary, original)

        # 写入
        success, _ = store.add_with_created(
            vector=vec,
            method_entity_id=mid,
            interpretation_text=new_text,
            class_entity_id=rec.get("class_entity_id", ""),
            class_name=rec.get("class_name", ""),
            method_name=rec.get("method_name", ""),
            signature=rec.get("signature", ""),
            context_summary=rec.get("context_summary", ""),
            language=rec.get("language", "zh"),
            related_entity_ids_json=rec.get("related_entity_ids_json", "{}"),
        )
        return success, mid

    # 并行处理
    batch_size = max(workers * 3, 20)
    total = len(records)
    for batch_start in range(0, total, batch_size):
        batch = records[batch_start: batch_start + batch_size]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(process_one, r): r for r in batch}
            for future in as_completed(futures):
                try:
                    success, mid = future.result()
                except Exception as e:
                    log.warning("  异常: %s", e)
                    success = False
                with lock:
                    if success:
                        ok += 1
                    else:
                        fail += 1
                    done = ok + fail
                    if done % 100 == 0 or done == total:
                        log.info("  方法解读进度: %d/%d (ok=%d fail=%d)", done, total, ok, fail)

    log.info("方法解读完成: ok=%d fail=%d", ok, fail)
    return {"total": total, "ok": ok, "fail": fail}


# ─────────── 业务解读处理 ───────────

def process_business_records(biz_store, llm, dim, max_count=0, workers=4, dry_run=False):
    """处理 BusinessInterpretation 集合"""
    from src.semantic.embedding import get_embedding

    log.info("读取 BusinessInterpretation 全量数据...")
    records = list(iter_all_records(
        biz_store,
        text_field="summary_text",
        id_field="entity_id",
        extra_fields=["entity_type", "level", "business_domain",
                       "business_capabilities", "language",
                       "context_json", "related_entity_ids_json"],
    ))
    log.info("  读取到 %d 条记录", len(records))

    if max_count > 0:
        records = records[:max_count]

    if dry_run:
        log.info("DRY RUN")
        return {"total": len(records), "ok": 0, "fail": 0}

    log.info("清空 BusinessInterpretation 集合...")
    biz_store.clear()
    log.info("  已清空")

    ok, fail = 0, 0
    lock = threading.Lock()

    def process_one(rec):
        eid = rec.get("entity_id", "")
        level = rec.get("level", "")
        text = rec.get("summary_text", "")
        if not text or not eid:
            return False, eid

        original = text
        if DETAIL_PREFIX in text:
            original = text[text.index(DETAIL_PREFIX) + len(DETAIL_PREFIX):].strip()

        summary = extract_summary(llm, original)
        if not summary:
            summary = eid

        vec = get_embedding(summary, dim)
        new_text = format_text(summary, original)

        success, _ = biz_store.add_with_created(
            vector=vec,
            entity_id=eid,
            level=level,
            summary_text=new_text,
            entity_type=rec.get("entity_type", ""),
            business_domain=rec.get("business_domain", ""),
            business_capabilities=rec.get("business_capabilities", ""),
            language=rec.get("language", "zh"),
            context_json=rec.get("context_json", ""),
            related_entity_ids_json=rec.get("related_entity_ids_json", "{}"),
        )
        return success, eid

    total = len(records)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_one, r): r for r in records}
        for future in as_completed(futures):
            try:
                success, eid = future.result()
            except Exception as e:
                log.warning("  异常: %s", e)
                success = False
            with lock:
                if success:
                    ok += 1
                else:
                    fail += 1
                done = ok + fail
                if done % 50 == 0 or done == total:
                    log.info("  业务解读进度: %d/%d (ok=%d fail=%d)", done, total, ok, fail)

    log.info("业务解读完成: ok=%d fail=%d", ok, fail)
    return {"total": total, "ok": ok, "fail": fail}


# ─────────── 验证 ───────────

def verify_search(store, dim, queries, collection_label):
    """验证搜索效果"""
    from src.semantic.embedding import get_embedding
    from src.knowledge.weaviate_near_vector import near_vector_property_hits

    log.info("\n=== 验证搜索: %s ===", collection_label)
    coll = store._get_collection()

    for query in queries:
        vec = get_embedding(query, dim)
        try:
            results = near_vector_property_hits(
                coll, vector=vec, dim=dim, limit=5,
                collection_name=store._collection_name,
                return_properties=["method_entity_id", "method_name",
                                   "class_name", "interpretation_text"],
            )
        except Exception:
            results = near_vector_property_hits(
                coll, vector=vec, dim=dim, limit=5,
                collection_name=store._collection_name,
                return_properties=["entity_id", "level", "summary_text"],
            )

        log.info("\n  查询: 「%s」", query)
        for props, score in results:
            name = props.get("method_name") or props.get("entity_id", "?")
            cls = props.get("class_name", "")
            text = (props.get("interpretation_text") or props.get("summary_text") or "")
            # 从 [摘要] 中取关键词
            if text.startswith(SUMMARY_PREFIX):
                summary_line = text[len(SUMMARY_PREFIX):].split("\n")[0].strip()
            else:
                summary_line = text[:50]
            label = f"{cls}.{name}" if cls else name
            log.info("    [%.4f] %s → %s", score, label, summary_line[:40])


# ─────────── MAIN ───────────

def main():
    parser = argparse.ArgumentParser(description="摘要提取 + 真实向量化")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--skip-method", action="store_true")
    parser.add_argument("--skip-business", action="store_true")
    parser.add_argument("--skip-verify", action="store_true")
    args = parser.parse_args()

    import yaml
    with open("config/project.yaml") as f:
        cfg = yaml.safe_load(f)

    k = cfg["knowledge"]

    # LLM (MiniMax)
    minimax_cfg = k["method_interpretation"]["multi_providers"][1]
    llm = MiniMaxLLM(
        api_key=minimax_cfg["openai_api_key"],
        base_url=minimax_cfg["openai_base_url"],
        model=minimax_cfg["openai_model"],
    )

    dim = k["vectordb-interpret"]["dimension"]  # 1024

    start = time.time()

    # ── 方法解读 ──
    if not args.skip_method:
        from src.knowledge.weaviate_interpretation_store import WeaviateMethodInterpretStore
        vc = k["vectordb-interpret"]
        method_store = WeaviateMethodInterpretStore(
            url=vc["weaviate_url"], grpc_port=vc["weaviate_grpc_port"],
            collection_name=vc["collection_name"], dimension=dim,
            api_key=vc.get("weaviate_api_key"),
        )
        try:
            result = process_method_records(
                method_store, llm, dim,
                max_count=args.max, workers=args.workers, dry_run=args.dry_run,
            )
            log.info("方法解读结果: %s", result)

            if not args.skip_verify and not args.dry_run:
                verify_search(method_store, dim, [
                    "用户提交订单后扣减库存",
                    "用户登录认证",
                    "商品分类查询",
                    "退货原因管理",
                    "秒杀活动场次",
                ], "MethodInterpretation")
        finally:
            method_store.close()

    # ── 业务解读 ──
    if not args.skip_business:
        from src.knowledge.weaviate_business_store import WeaviateBusinessInterpretStore
        vc_biz = k["vectordb-business"]
        biz_store = WeaviateBusinessInterpretStore(
            url=vc_biz["weaviate_url"], grpc_port=vc_biz["weaviate_grpc_port"],
            collection_name=vc_biz["collection_name"], dimension=dim,
            api_key=vc_biz.get("weaviate_api_key"),
        )
        try:
            result = process_business_records(
                biz_store, llm, dim,
                max_count=args.max, workers=args.workers, dry_run=args.dry_run,
            )
            log.info("业务解读结果: %s", result)
        finally:
            biz_store.close()

    elapsed = time.time() - start
    log.info("\n总耗时: %.1f 分钟", elapsed / 60)


if __name__ == "__main__":
    main()
