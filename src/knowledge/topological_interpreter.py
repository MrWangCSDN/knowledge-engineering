"""
自底向上拓扑解读引擎

核心思想（大厦理论）：
  代码工程 = 一座大厦
  每个方法 = 一块砖
  每条交易 = 一层楼

  先解读最底层的砖（叶子方法），再逐层往上解读。
  上层方法的 prompt 中注入下层方法的 summary，
  让 LLM 在解读上层时已经理解了每个子调用的业务含义。

流程：
  1. 从 structure_facts 构建调用图
  2. 过滤 getter/setter + 无代码方法
  3. 拓扑排序分层 (叶子=L0, 逐层递增)
  4. 逐层解读: L0 → L1 → L2 → ... (层内并行, 层间串行)
  5. 每层解读完, summary 可被上层读取
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

from src.knowledge.interpretation_item_helpers import (
    clean_think_tags,
    extract_summary,
    INTERP_ITEM_RECOVERABLE_EXCEPTIONS,
)
from src.models.structure import EntityType, RelationType, StructureFacts, StructureEntity
from src.semantic.embedding import get_embedding

_LOG = logging.getLogger(__name__)


class TopologicalInterpreter:
    """自底向上分层解读引擎"""

    def __init__(
        self,
        structure_facts: StructureFacts,
        llm: Any,
        weaviate_store: Any,
        *,
        language: str = "zh",
        embedding_dim: int = 1024,
        max_workers: int = 8,
        llm_timeout: int = 90,
        repo_path: str = "",
        layer_gate: float = 1.0,
        max_retry_cycles: int = 5,
        retry_delays: Optional[list[int]] = None,
        state_file: str = "out_ui/interpretation_state.json",
        step_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ):
        self.facts = structure_facts
        self.llm = llm
        self.store = weaviate_store
        self.language = language
        self.dim = embedding_dim
        self.max_workers = max_workers
        self.llm_timeout = llm_timeout
        self.repo_path = repo_path
        # 层级门禁配置
        self.layer_gate = layer_gate  # 每层完成率阈值 (默认 100%)
        self.max_retry_cycles = max_retry_cycles  # 最大重试轮次 (默认 5)
        # 退避延迟: 1分/5分/30分/1小时/2小时
        self.retry_delays = retry_delays or [60, 300, 1800, 3600, 7200]
        self.state_file = state_file
        self._step = step_callback or (lambda msg: None)
        self._progress = progress_callback or (lambda c, t, msg: None)

        # 内存中的 summary 缓存: method_id → summary 文本
        self._summary_cache: dict[str, str] = {}
        self._cache_lock = threading.Lock()

        # DAO SQL 索引: method_entity_id → annotated_sql
        self._sql_index: dict[str, str] = {}
        # 表 DDL 索引: table_name → CREATE TABLE DDL
        self._table_ddls: dict[str, str] = {}
        # method_entity_id → 涉及的表名列表
        self._sql_tables: dict[str, list[str]] = {}

        # Bean 字段注释索引: (class_name, field_name) → comment
        self._field_comments: dict[tuple[str, str], str] = {}

        # 构建索引
        self._methods: dict[str, StructureEntity] = {}
        self._call_graph: dict[str, set[str]] = defaultdict(set)
        self._reverse_graph: dict[str, set[str]] = defaultdict(set)
        self._class_map: dict[str, str] = {}  # method_id → class_name

    def run(self) -> dict[str, Any]:
        """执行自底向上全量解读，返回统计信息"""
        t0 = time.time()

        # Step 1: 构建数据
        self._step("拓扑解读：构建调用图...")
        self._build_indices()
        meaningful = self._filter_meaningful()
        self._step(f"拓扑解读：有效业务方法 {len(meaningful)} 个")

        # Step 1.5: 加载 DAO SQL (MyBatis XML → 注入到 DAO 方法)
        self._load_dao_sql()
        if self._sql_index:
            self._step(f"拓扑解读：已加载 {len(self._sql_index)} 条 DAO SQL")

        # Step 2: 拓扑分层
        self._step("拓扑解读：计算拓扑层级...")
        levels = self._compute_levels(meaningful)
        max_level = max(levels.values()) if levels else 0

        level_groups: dict[int, list[str]] = defaultdict(list)
        for mid, lv in levels.items():
            level_groups[lv].append(mid)

        self._step(f"拓扑解读：{max_level + 1} 层金字塔")
        for lv in range(max_level + 1):
            self._step(f"  L{lv}: {len(level_groups[lv])} 个方法")

        # Step 3: 一致性检查 - 清理孤儿
        existing_ids = set()
        try:
            existing_ids = self.store.list_existing_method_ids(limit=200000)
        except Exception:
            pass
        orphans = existing_ids - set(meaningful)
        if orphans:
            self._step(f"拓扑解读：清理 {len(orphans)} 条孤儿解读...")
            for oid in orphans:
                try:
                    uid = self.store._to_uuid(oid + "|interpret")
                    self.store._get_collection().data.delete_by_id(uid)
                except Exception:
                    pass

        # 已有解读的加载到 summary_cache (断点续跑)
        already_done = existing_ids & set(meaningful)
        for mid in already_done:
            rec = self.store.get_by_method_id(mid)
            if rec:
                text = rec.get("interpretation_text", "")
                summary = extract_summary(text)
                with self._cache_lock:
                    self._summary_cache[mid] = summary

        self._step(f"拓扑解读：已有解读 {len(already_done)} 条（断点续跑）")

        # 加载历史状态 (之前运行的永久失败记录)
        prev_state = self._load_state()
        self._step(f"拓扑解读：Gate={self.layer_gate*100:.0f}%, 最大重试轮次={self.max_retry_cycles}")

        # Step 4: 逐层解读 (带门禁机制)
        total_todo = len(meaningful) - len(already_done)
        total_ok, total_fail = 0, 0
        total_permanent_failed: dict[int, set[str]] = {}

        for lv in range(max_level + 1):
            methods_at_level = level_groups.get(lv, [])

            # 从历史状态恢复该层永久失败的方法
            layer_state = prev_state.get(f"L{lv}", {})
            permanent_failed = set(layer_state.get("permanent_failed", []))

            try:
                ok, fail, permanent_failed = self._run_layer_with_gate(
                    lv, methods_at_level, permanent_failed
                )
                total_ok += ok
                total_fail += fail
                total_permanent_failed[lv] = permanent_failed
            except RuntimeError as e:
                # Gate 未达标, 终止后续层
                self._step(f"❌ L{lv} Gate 未达标: {e}")
                self._step(f"❌ 停止执行, 后续层级不会启动")
                self._save_state(lv, "GATE_FAILED", permanent_failed)
                total_permanent_failed[lv] = permanent_failed
                break

        elapsed = time.time() - t0
        result = {
            "total_methods": len(meaningful),
            "levels": max_level + 1,
            "already_done": len(already_done),
            "ok": total_ok,
            "fail": total_fail,
            "permanent_failed": {f"L{lv}": len(s) for lv, s in total_permanent_failed.items()},
            "elapsed_minutes": round(elapsed / 60, 1),
        }
        self._step(f"拓扑解读完成: {result}")
        return result

    def _run_layer_with_gate(
        self, level: int, methods_at_level: list[str],
        permanent_failed: set[str],
    ) -> tuple[int, int, set[str]]:
        """
        跑一层, 带门禁 + 自动重试 + 永久失败兜底
        Returns: (ok, fail, permanent_failed)
        """
        total_methods_count = len(methods_at_level)
        total_ok, total_fail = 0, 0
        cycle = 0

        while True:
            # 从 Weaviate 同步最新完成状态
            done_ids = self.store.list_existing_method_ids()

            # 新完成的方法加载 summary 到缓存 (供上层使用)
            for mid in (done_ids & set(methods_at_level)):
                if mid not in self._summary_cache:
                    rec = self.store.get_by_method_id(mid)
                    if rec:
                        summary = extract_summary(rec.get("interpretation_text", ""))
                        with self._cache_lock:
                            self._summary_cache[mid] = summary

            # 本轮还需处理的方法 (排除已完成、永久失败)
            todo = [m for m in methods_at_level
                    if m not in done_ids and m not in permanent_failed]

            # 计算当前完成率 (不含永久失败)
            attempting = total_methods_count - len(permanent_failed)
            if attempting > 0:
                completeness = (attempting - len(todo)) / attempting
            else:
                completeness = 1.0

            # 已达到门禁阈值
            if not todo or completeness >= self.layer_gate:
                status = "COMPLETE" if not permanent_failed else "PARTIAL"
                self._step(
                    f"✅ L{level}: 完成 完成率={completeness*100:.1f}%, "
                    f"永久失败={len(permanent_failed)}, "
                    f"已解读={attempting-len(todo)}/{total_methods_count}"
                )
                self._save_state(level, status, permanent_failed)
                return total_ok, total_fail, permanent_failed

            # 超过最大重试轮次
            if cycle >= self.max_retry_cycles:
                # 剩余的标记为永久失败
                for m in todo:
                    permanent_failed.add(m)

                final_attempting = total_methods_count - len(permanent_failed)
                final_completeness = final_attempting / total_methods_count if total_methods_count else 0

                self._step(
                    f"⚠️ L{level}: 重试 {self.max_retry_cycles} 轮后, "
                    f"{len(todo)} 条标记为永久失败, "
                    f"实际完成率={final_completeness*100:.1f}%"
                )

                if final_completeness >= self.layer_gate:
                    self._save_state(level, "PARTIAL_ACCEPT", permanent_failed)
                    return total_ok, total_fail, permanent_failed
                else:
                    self._save_state(level, "GATE_FAILED", permanent_failed)
                    raise RuntimeError(
                        f"L{level} 完成率 {final_completeness*100:.1f}% "
                        f"低于 Gate 阈值 {self.layer_gate*100:.0f}%"
                    )

            # 跑一轮
            self._step(
                f"🔄 L{level} 第 {cycle+1}/{self.max_retry_cycles} 轮: "
                f"处理 {len(todo)} 个方法 (并发 {self.max_workers})"
            )
            ok, fail = self._interpret_level(todo, level, 0, len(todo))
            total_ok += ok
            total_fail += fail
            cycle += 1

            # 如果还有剩余, 等待后重试
            if cycle < self.max_retry_cycles:
                done_ids_after = self.store.list_existing_method_ids()
                still_todo = [m for m in methods_at_level
                              if m not in done_ids_after and m not in permanent_failed]
                if not still_todo:
                    continue  # 全部完成, 下轮判断会退出

                delay = self.retry_delays[min(cycle - 1, len(self.retry_delays) - 1)]
                self._step(
                    f"⏱️ L{level}: 还剩 {len(still_todo)} 条, "
                    f"等待 {delay//60} 分钟后重试..."
                )
                time.sleep(delay)

    def _load_state(self) -> dict:
        """加载历史状态"""
        import json
        from pathlib import Path
        path = Path(self.state_file)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_state(self, level: int, status: str, permanent_failed: set[str]):
        """保存层级状态"""
        import json
        from pathlib import Path
        state = self._load_state()
        state[f"L{level}"] = {
            "status": status,  # COMPLETE | PARTIAL | PARTIAL_ACCEPT | GATE_FAILED
            "permanent_failed": sorted(permanent_failed),
            "timestamp": time.time(),
        }
        path = Path(self.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    # ─────────── 内部方法 ───────────

    def _build_indices(self):
        """从 structure_facts 构建方法索引和调用图"""
        for e in self.facts.entities:
            if e.type == EntityType.METHOD:
                self._methods[e.id] = e
                attrs = e.attributes or {}
                self._class_map[e.id] = attrs.get("class_name", "")

        for r in self.facts.relations:
            if r.type == RelationType.CALLS:
                src, tgt = r.source_id, r.target_id
                if src != tgt and src in self._methods and tgt in self._methods:
                    self._call_graph[src].add(tgt)
                    self._reverse_graph[tgt].add(src)

        # 加载 FIELD 实体的 comment 注释 (来自 @Schema/@ApiModelProperty/Javadoc)
        for e in self.facts.entities:
            if e.type == EntityType.FIELD:
                attrs = e.attributes or {}
                comment = attrs.get("comment", "")
                if comment:
                    cn = attrs.get("class_name", "")
                    self._field_comments[(cn, e.name)] = comment

        _LOG.info("字段注释: %d 个 Bean 字段有中文注释", len(self._field_comments))

    def _load_dao_sql(self):
        """加载 DAO SQL 插件 + 表 DDL，将 MyBatis SQL 和表结构匹配到方法实体"""
        if not self.repo_path:
            return
        try:
            from src.plugins.dao_sql.registry import load_dao_sql_for_repo
            sql_results = load_dao_sql_for_repo(self.repo_path, {})
        except Exception as e:
            _LOG.warning("DAO SQL 插件加载失败: %s", e)
            return

        # 加载表 DDL (从 document/sql/*.sql 文件中解析 CREATE TABLE)
        self._load_table_ddls()

        # 构建 (class_name, method_name) → method_id 索引
        name_to_ids: dict[tuple[str, str], list[str]] = defaultdict(list)
        for mid, m in self._methods.items():
            attrs = m.attributes or {}
            cn = attrs.get("class_name", "")
            name_to_ids[(cn, m.name)].append(mid)

        matched = 0
        for key, sql_result in sql_results.items():
            parts = key.rsplit(".", 1)
            if len(parts) != 2:
                continue
            namespace, method_name = parts
            class_name = namespace.rsplit(".", 1)[-1]
            for cn_try in [class_name, class_name.replace("Dao", "Mapper"), class_name.replace("Mapper", "Dao")]:
                for mid in name_to_ids.get((cn_try, method_name), []):
                    self._sql_index[mid] = sql_result.annotated_sql
                    self._sql_tables[mid] = sql_result.tables or []
                    matched += 1

        _LOG.info("DAO SQL 匹配: %d 条 SQL → %d 个方法实体, DDL %d 张表",
                  len(sql_results), matched, len(self._table_ddls))

    def _load_table_ddls(self):
        """从仓库的 SQL 文件中解析 CREATE TABLE DDL"""
        import glob
        import re
        sql_files = glob.glob(os.path.join(self.repo_path, "**/*.sql"), recursive=True)
        for sql_file in sql_files:
            try:
                with open(sql_file, encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                for match in re.finditer(
                    r'CREATE TABLE\s+`(\w+)`\s*\((.*?)\)\s*ENGINE',
                    content, re.DOTALL
                ):
                    table_name = match.group(1)
                    # 只保留列定义部分 (去掉 ENGINE 等尾部)
                    self._table_ddls[table_name] = match.group(0)
            except Exception:
                pass

    def _get_code_with_sql(self, method_id: str) -> str:
        """获取方法代码，如果是 DAO 方法则追加 SQL + 涉及表的 DDL"""
        m = self._methods.get(method_id)
        if not m:
            return ""
        code = (m.attributes or {}).get("code_snippet", "")
        sql = self._sql_index.get(method_id, "")
        if not sql:
            return code

        # 追加 SQL
        parts = [code] if code else []
        parts.append(f"-- [MyBatis SQL]\n{sql}")

        # 追加涉及表的 DDL (含字段注释，如 COMMENT '订单状态：0->待付款')
        tables = self._sql_tables.get(method_id, [])
        for table_name in tables:
            ddl = self._table_ddls.get(table_name, "")
            if ddl:
                parts.append(f"-- [表结构 {table_name}]\n{ddl}")

        return "\n\n".join(parts)

    def _filter_meaningful(self) -> set[str]:
        """过滤出有意义的业务方法 (排除 getter/setter + 无代码)"""
        meaningful = set()
        for mid, m in self._methods.items():
            attrs = m.attributes or {}
            if not attrs.get("code_snippet"):
                continue
            if attrs.get("is_getter") or attrs.get("is_setter"):
                continue
            meaningful.add(mid)
        return meaningful

    def _compute_levels(self, meaningful: set[str]) -> dict[str, int]:
        """拓扑排序分层: 叶子=L0, 逐层递增"""
        # 只看 meaningful 之间的调用关系
        out_degree = {}
        for mid in meaningful:
            callees = self._call_graph.get(mid, set()) & meaningful
            out_degree[mid] = len(callees)

        levels = {}
        queue = deque()

        # 叶子: 出度=0
        for mid in meaningful:
            if out_degree[mid] == 0:
                levels[mid] = 0
                queue.append(mid)

        # BFS 向上传播
        while queue:
            node = queue.popleft()
            current_level = levels[node]
            callers = self._reverse_graph.get(node, set()) & meaningful
            for caller in callers:
                new_level = current_level + 1
                if caller not in levels or levels[caller] < new_level:
                    levels[caller] = new_level
                    queue.append(caller)

        # 孤立节点
        for mid in meaningful:
            if mid not in levels:
                levels[mid] = 0

        return levels

    def _interpret_level(
        self, method_ids: list[str], level: int,
        processed_before: int, total_todo: int,
    ) -> tuple[int, int]:
        """解读一个层级的所有方法 (层内并行)"""
        ok, fail = 0, 0
        lock = threading.Lock()

        batch_size = max(self.max_workers * 3, 20)
        for batch_start in range(0, len(method_ids), batch_size):
            batch = method_ids[batch_start: batch_start + batch_size]

            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {
                    pool.submit(self._interpret_one, mid, level): mid
                    for mid in batch
                }
                for future in as_completed(futures):
                    mid = futures[future]
                    try:
                        success = future.result()
                    except Exception:
                        _LOG.exception("解读异常 method=%s", mid[:30])
                        success = False

                    with lock:
                        if success:
                            ok += 1
                        else:
                            fail += 1
                        done = processed_before + ok + fail
                        if (ok + fail) % 50 == 0 or (ok + fail) == len(method_ids):
                            self._progress(
                                done, total_todo,
                                f"L{level} {ok + fail}/{len(method_ids)}"
                            )
        return ok, fail

    def _interpret_one(self, method_id: str, level: int) -> bool:
        """解读单个方法"""
        method = self._methods.get(method_id)
        if not method:
            return False

        attrs = method.attributes or {}
        # 获取代码 (DAO 方法自动追加 SQL)
        code = self._get_code_with_sql(method_id)
        if not code:
            return False

        # 构建 prompt
        prompt = self._build_prompt(method, level, code)

        # 调 LLM
        try:
            raw_text = self.llm.generate(prompt, timeout=self.llm_timeout)
        except INTERP_ITEM_RECOVERABLE_EXCEPTIONS:
            return False
        except Exception:
            _LOG.exception("LLM 调用失败 method=%s", method_id[:30])
            return False

        if not raw_text or len(raw_text) < 10:
            return False

        # 清洗
        text = clean_think_tags(raw_text)
        if len(text) < 10:
            return False

        # 提取 summary
        summary = extract_summary(text)

        # 缓存 summary (供上层使用)
        with self._cache_lock:
            self._summary_cache[method_id] = summary

        # embedding (只对 summary)
        vec = get_embedding(summary, self.dim)

        # 存 Weaviate
        class_id = ""
        for r in self.facts.relations:
            if r.type == RelationType.CONTAINS and r.target_id == method_id:
                class_id = r.source_id
                break

        try:
            success, _ = self.store.add_with_created(
                vector=vec,
                method_entity_id=method_id,
                interpretation_text=text,
                class_entity_id=class_id,
                class_name=attrs.get("class_name", ""),
                method_name=method.name,
                signature=attrs.get("signature", ""),
                context_summary=self._build_context_summary(method),
                language=self.language,
                related_entity_ids_json=json.dumps(
                    list(self._get_related_ids(method_id))[:24]
                ),
            )
            return success
        except Exception:
            _LOG.exception("Weaviate 写入失败 method=%s", method_id[:30])
            return False

    def _build_prompt(self, method: StructureEntity, level: int, code: str = "") -> str:
        """构建 prompt，L1+ 注入下层 summary，DAO 方法带 SQL"""
        attrs = method.attributes or {}
        if not code:
            code = self._get_code_with_sql(method.id)
        sig = attrs.get("signature", "") or method.name
        class_name = attrs.get("class_name", "")
        context = self._build_context_summary(method)

        # 下层 summary 注入 (核心差异)
        callee_context = ""
        if level > 0:
            callee_context = self._build_callee_summaries(method.id)

        # Bean 字段注释注入 (从 get/set 调用中提取)
        bean_context = self._build_bean_field_context(code)

        if self.language.startswith("en"):
            callee_section = ""
            if callee_context:
                callee_section = f"\n### Called methods (interpreted)\n{callee_context}\n"

            return f"""You are a senior Java engineer. Produce a two-part interpretation.

Requirements:
- [Summary]: Keyword-dense, max 50 chars, space-separated. Include: business actions, objects, techniques.
- [Detail]: Full technical interpretation. Leverage the called method interpretations below to explain business logic accurately.

### Context
{context}

### Signature
{sig}
{callee_section}{bean_section}
### Method body (excerpt)
```
{code[:8000]}
```

### Output (strict format)
[Summary] <keywords>

[Detail]
<interpretation>"""

        # 中文
        callee_section = ""
        if callee_context:
            callee_section = f"\n### 下游方法功能（已解读）\n{callee_context}\n"

        bean_section = ""
        if bean_context:
            bean_section = f"\n### 代码中使用的 Bean 字段说明\n{bean_context}\n"

        return f"""你是一名资深 Java 工程师。请根据上下文、方法代码以及下游方法的已有解读，输出技术解读。

要求：
- [摘要]：关键词密集，不超过50字，空格分隔，包含业务动作、涉及对象、关键技术手段。
- [详情]：完整技术解读。请结合下游方法的功能说明，准确描述本方法的业务逻辑，而非仅列出调用了哪些方法。

### 上下文
{context}

### 方法签名
{sig}
{callee_section}{bean_section}
### 方法体（节选）
```
{code[:8000]}
```

### 请输出（严格按以下格式）
[摘要] <关键词1 关键词2 关键词3 ...>

[详情]
<完整技术解读>"""

    def _build_callee_summaries(self, method_id: str, max_total_chars: int = 2000) -> str:
        """获取下游方法的 summary 列表，用于注入 prompt"""
        callees = self._call_graph.get(method_id, set())
        lines = []
        total = 0

        for cid in callees:
            m = self._methods.get(cid)
            if not m:
                continue
            attrs = m.attributes or {}
            if attrs.get("is_getter") or attrs.get("is_setter"):
                continue

            with self._cache_lock:
                summary = self._summary_cache.get(cid, "")

            if not summary:
                continue

            line = f"- {m.name}: {summary}"
            if total + len(line) > max_total_chars:
                remaining = len(callees) - len(lines)
                lines.append(f"- ... 还有 {remaining} 个方法省略")
                break

            lines.append(line)
            total += len(line)

        return "\n".join(lines)

    def _build_bean_field_context(self, code: str, max_chars: int = 1500) -> str:
        """从代码中提取 setter/getter 调用，查找对应字段的中文注释。
        只返回有注释的字段，格式紧凑。"""
        # 提取 xxx.setYYY(...) 和 xxx.getYYY() 调用
        # 同时提取变量类型: Type xxx = new Type() 或 Type xxx
        import re

        # 推断变量类型: "OmsOrder order" / "new OmsOrder()"
        type_map: dict[str, str] = {}  # 变量名 → 类名
        for match in re.finditer(r'(\w+)\s+(\w+)\s*=\s*new\s+(\w+)', code):
            type_map[match.group(2)] = match.group(3)
        for match in re.finditer(r'(\w+(?:<[^>]+>)?)\s+(\w+)\s*[=;]', code):
            t = match.group(1).split('<')[0]
            if t[0].isupper() and match.group(2) not in type_map:
                type_map[match.group(2)] = t

        # 提取 set/get 调用
        calls = re.findall(r'(\w+)\.(set|get|is)(\w+)\(', code)

        seen = set()
        lines = []
        total = 0
        for var_name, prefix, field_part in calls:
            # Bean 命名规范反推字段名
            if len(field_part) > 1 and field_part[0].isupper() and field_part[1].islower():
                field_name = field_part[0].lower() + field_part[1:]
            elif len(field_part) > 1 and field_part[0].isupper() and field_part[1].isupper():
                field_name = field_part  # PWD, RMB 等保持
            else:
                field_name = field_part

            class_name = type_map.get(var_name, "")
            if not class_name:
                continue

            key = (class_name, field_name)
            if key in seen:
                continue
            seen.add(key)

            comment = self._field_comments.get(key, "")
            if not comment:
                continue

            line = f"{class_name}.{field_name}: {comment}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)

        return "\n".join(lines)

    def _build_context_summary(self, method: StructureEntity) -> str:
        """构建基础上下文信息"""
        attrs = method.attributes or {}
        class_name = attrs.get("class_name", "")

        callers = []
        callees = []
        for r in self.facts.relations:
            if r.type == RelationType.CALLS:
                if r.target_id == method.id:
                    callers.append(self._entity_name(r.source_id))
                elif r.source_id == method.id:
                    callees.append(self._entity_name(r.target_id))

        return "\n".join([
            f"类名: {class_name}",
            f"方法签名: {attrs.get('signature', method.name)}",
            f"模块: {method.module_id or ''}",
            f"上游调用方: {', '.join(callers[:5]) if callers else '无'}",
            f"下游被调用: {', '.join(callees[:8]) if callees else '无'}",
        ])

    def _entity_name(self, entity_id: str) -> str:
        m = self._methods.get(entity_id)
        if m:
            cn = (m.attributes or {}).get("class_name", "")
            return f"{cn}.{m.name}" if cn else m.name
        return entity_id[:20]

    def _get_related_ids(self, method_id: str) -> set[str]:
        ids = {method_id}
        for r in self.facts.relations:
            if r.type == RelationType.CALLS:
                if r.source_id == method_id:
                    ids.add(r.target_id)
                elif r.target_id == method_id:
                    ids.add(r.source_id)
            if r.type == RelationType.CONTAINS and r.target_id == method_id:
                ids.add(r.source_id)
        return ids
