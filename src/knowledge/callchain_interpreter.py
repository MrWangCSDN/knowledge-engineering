"""模式 B：实时调用链解读 — 给定任意方法，追踪调用链，拼接全量代码，一次性发 LLM 解读。

与模式 A（预解读碎片存向量库）并存：
- 模式 A 适合搜索/浏览（毫秒级响应）
- 模式 B 适合深度分析（完整业务流程，6-90 秒）
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

_LOG = logging.getLogger(__name__)

# Prompt 上下文保护：最大字符数（约 15K tokens，Qwen 3.5 上下文的 50%）
MAX_PROMPT_CHARS = 60000


@dataclass
class ChainNode:
    """调用链中的一个方法节点。"""
    method_id: str
    class_name: str
    method_name: str
    signature: str
    depth: int
    code_snippet: Optional[str] = None
    module_id: Optional[str] = None
    location: Optional[str] = None
    is_interface_impl: bool = False     # 是否从接口跳转过来的实现类
    interface_name: Optional[str] = None  # 原接口名（如 "OmsOrderService"）
    branch_index: int = 0               # 多实现时的分支编号（0=主分支/唯一实现）


@dataclass
class CallChainResult:
    """调用链解读结果。"""
    method_id: str
    chain: list[ChainNode] = field(default_factory=list)
    interpretation: str = ""
    prompt_tokens: int = 0
    llm_time_seconds: float = 0.0
    chain_size: int = 0
    total_code_chars: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "method_id": self.method_id,
            "chain": [asdict(n) for n in self.chain],
            "interpretation": self.interpretation,
            "prompt_tokens": self.prompt_tokens,
            "llm_time_seconds": self.llm_time_seconds,
            "chain_size": self.chain_size,
            "total_code_chars": self.total_code_chars,
            "error": self.error,
        }


class CallChainInterpreter:
    """
    实时调用链解读器。

    用法:
        interpreter = CallChainInterpreter(graph, llm_provider, structure_facts)
        result = interpreter.interpret("method//abc123", direction="down", max_depth=5)
        print(result.interpretation)
    """

    def __init__(
        self,
        graph: Any,                     # KnowledgeGraph 实例
        llm: Any,                       # LLMProvider 实例
        structure_facts: Any = None,    # StructureFacts（用于获取 code_snippet）
        language: str = "zh",
        repo_path: str = "",            # 仓库路径（用于 DAO SQL 插件）
        dao_config: Optional[dict] = None,  # schema 配置段
    ):
        self._graph = graph
        self._llm = llm
        self._language = language

        # 预建 entity_id → code_snippet 索引
        self._snippet_index: dict[str, str] = {}
        if structure_facts:
            for entity in (structure_facts.entities if hasattr(structure_facts, 'entities') else []):
                eid = entity.id if hasattr(entity, 'id') else entity.get('id', '')
                attrs = entity.attributes if hasattr(entity, 'attributes') else entity.get('attributes', {})
                snippet = (attrs or {}).get('code_snippet', '')
                if snippet:
                    self._snippet_index[eid] = snippet

        # 如果 structure_facts 没传，尝试从缓存文件加载
        if not self._snippet_index:
            self._load_snippets_from_cache()

        # 构建 接口方法 → 实现类方法 映射（解决接口调用链断裂问题）
        self._iface_to_impl: dict[str, str] = {}
        self._build_interface_to_impl_index()

        # DAO SQL 插件：加载 SQL 并关联到方法实体
        self._sql_index: dict[str, str] = {}  # method_entity_id → annotated_sql
        if repo_path:
            self._load_dao_sql(repo_path, dao_config or {})

    def _build_interface_to_impl_index(self):
        """构建 接口方法ID → [实现类方法ID列表] 映射。

        解决问题: SymbolSolver 将 CALLS 指向接口方法（如 OmsOrderService.list），
        但实际业务逻辑在实现类（OmsOrderServiceImpl.list）。
        BFS 追踪到接口方法时需要自动跳转到实现类。

        支持多实现: 一个接口方法可能有多个实现类
          PaymentService.pay → [AlipayServiceImpl.pay, WechatPayServiceImpl.pay]
        BFS 时会把所有实现展开为并行分支。

        匹配策略: 查找所有 类名以"接口名"结尾+"Impl" 的类
          OmsOrderService → OmsOrderServiceImpl (单实现)
          PaymentService  → [AlipayServiceImpl, WechatPayServiceImpl] (多实现)
        """
        self._iface_to_impls: dict[str, list[str]] = {}  # 接口方法ID → [实现类方法ID列表]

        graph = self._graph
        nx_graph = getattr(graph, '_graph', graph)

        # 按 class_name 分组方法
        methods_by_class: dict[str, dict[str, str]] = {}  # class_name → {method_name → method_id}
        all_class_names: set[str] = set()
        for nid, attrs in nx_graph.nodes(data=True):
            if attrs.get("entity_type") == "method":
                cls = attrs.get("class_name", "")
                name = attrs.get("name", "")
                if cls and name:
                    methods_by_class.setdefault(cls, {})[name] = nid
                    all_class_names.add(cls)

        # 对每个非Impl类，查找所有以它命名的Impl实现类
        for cls_name, methods in methods_by_class.items():
            if cls_name.endswith("Impl"):
                continue

            # 查找所有实现类: XxxServiceImpl, 或 XxxService 的其他实现如 AlipayXxxServiceImpl
            impl_classes = []
            for candidate in all_class_names:
                if candidate == cls_name:
                    continue
                # 匹配规则:
                # 1. 精确: XxxService → XxxServiceImpl
                # 2. 扩展: XxxService → AlipayXxxServiceImpl (以 Impl 结尾且包含接口名)
                if candidate == cls_name + "Impl":
                    impl_classes.append(candidate)
                elif candidate.endswith("Impl") and cls_name in candidate:
                    impl_classes.append(candidate)

            if not impl_classes:
                continue

            for method_name, iface_method_id in methods.items():
                impl_method_ids = []
                for impl_cls in impl_classes:
                    impl_methods = methods_by_class.get(impl_cls, {})
                    impl_method_id = impl_methods.get(method_name)
                    if impl_method_id:
                        impl_method_ids.append(impl_method_id)
                if impl_method_ids:
                    self._iface_to_impls[iface_method_id] = impl_method_ids

        # 兼容旧属性名（单实现快捷访问）
        self._iface_to_impl = {k: v[0] for k, v in self._iface_to_impls.items() if v}

        total_mappings = sum(len(v) for v in self._iface_to_impls.values())
        multi_count = sum(1 for v in self._iface_to_impls.values() if len(v) > 1)
        if self._iface_to_impls:
            _LOG.info("接口→实现映射: %d 个方法 (%d 个多实现)", total_mappings, multi_count)

    def _load_dao_sql(self, repo_path: str, config: dict):
        """通过 DAO SQL 插件加载 SQL，匹配到图中的方法实体。"""
        try:
            from src.plugins.dao_sql import load_dao_sql_for_repo
        except ImportError:
            _LOG.debug("DAO SQL 插件未安装")
            return

        results = load_dao_sql_for_repo(repo_path, config)
        if not results:
            return

        # 构建图中 (class_name, method_name) → method_entity_id 索引
        nx_graph = getattr(self._graph, '_graph', self._graph)
        method_index: dict[tuple[str, str], str] = {}
        for nid, attrs in nx_graph.nodes(data=True):
            if attrs.get("entity_type") == "method":
                cls = attrs.get("class_name", "")
                name = attrs.get("name", "")
                if cls and name:
                    method_index[(cls, name)] = nid

        # 匹配 SQL → 方法实体
        matched = 0
        for key, sql_result in results.items():
            # 策略1: 精确匹配 class_simple_name + method_name
            cls_name = sql_result.class_simple_name
            method_name = sql_result.method_name
            entity_id = method_index.get((cls_name, method_name))

            # 策略2: Dao → Mapper 别名（OmsOrderDao → OmsOrderMapper）
            if not entity_id and cls_name.endswith("Dao"):
                mapper_name = cls_name[:-3] + "Mapper"
                entity_id = method_index.get((mapper_name, method_name))

            # 策略3: Mapper → Dao 别名
            if not entity_id and cls_name.endswith("Mapper"):
                dao_name = cls_name[:-6] + "Dao"
                entity_id = method_index.get((dao_name, method_name))

            if entity_id:
                self._sql_index[entity_id] = sql_result.annotated_sql
                matched += 1

        _LOG.info("DAO SQL 匹配: %d/%d 条关联到方法实体", matched, len(results))

    def _load_snippets_from_cache(self):
        """从 out_ui/structure_facts_for_interpret.json 缓存加载代码片段。"""
        cache_paths = [
            Path("out_ui/structure_facts_for_interpret.json"),
            Path.cwd() / "out_ui" / "structure_facts_for_interpret.json",
        ]
        for path in cache_paths:
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    for entity in data.get("entities", []):
                        eid = entity.get("id", "")
                        snippet = (entity.get("attributes") or {}).get("code_snippet", "")
                        if snippet:
                            self._snippet_index[eid] = snippet
                    _LOG.info("从缓存加载了 %d 个代码片段", len(self._snippet_index))
                    return
                except Exception as e:
                    _LOG.warning("加载缓存失败 %s: %s", path, e)

    def interpret(
        self,
        method_id: str,
        direction: str = "down",
        max_depth: int = 5,
        max_methods: int = 30,
        max_tokens: int = 4000,
        timeout: int = 120,
    ) -> CallChainResult:
        """
        实时解读一条调用链。

        Args:
            method_id: 起始方法实体 ID（如 method//abc123 或完整 ID）
            direction: "down" 下游调用 | "up" 上游调用者 | "both" 双向
            max_depth: BFS 最大深度
            max_methods: 链路最大方法数
            max_tokens: LLM 输出 token 限制
            timeout: LLM 超时秒数

        Returns:
            CallChainResult 包含调用链结构和 LLM 解读
        """
        result = CallChainResult(method_id=method_id)

        # Step 1: BFS 追踪调用链
        chain = self._trace_chain(method_id, direction, max_depth, max_methods)
        if not chain:
            result.error = f"未找到方法 {method_id} 或无调用关系"
            return result
        result.chain = chain
        result.chain_size = len(chain)

        # Step 2: 构建 prompt
        prompt = self._build_prompt(chain)
        result.prompt_tokens = len(prompt) // 4
        result.total_code_chars = sum(len(n.code_snippet or "") for n in chain)

        # Step 3: 调用 LLM
        try:
            t0 = time.time()
            interpretation = self._llm.generate(
                prompt,
                timeout=timeout,
                max_tokens=max_tokens,
            )
            result.llm_time_seconds = round(time.time() - t0, 1)

            # 清理 <think> 标签（部分模型会输出思考过程）
            if "<think>" in interpretation and "</think>" in interpretation:
                think_end = interpretation.index("</think>") + len("</think>")
                interpretation = interpretation[think_end:].strip()

            result.interpretation = interpretation

        except Exception as e:
            result.error = f"LLM 调用失败: {type(e).__name__}: {str(e)[:200]}"
            _LOG.warning("调用链解读 LLM 失败: %s", result.error)

        return result

    def _trace_chain(
        self,
        start_id: str,
        direction: str,
        max_depth: int,
        max_methods: int,
    ) -> list[ChainNode]:
        """BFS 追踪调用链，返回按深度排序的方法节点列表。"""
        graph = self._graph

        # 验证起始节点存在（兼容 KnowledgeGraph 和裸 NetworkX 图）
        start_node = None
        try:
            start_node = graph.get_node(start_id)
        except (AttributeError, TypeError):
            nx_graph = getattr(graph, '_graph', graph)
            if start_id in nx_graph.nodes:
                start_node = dict(nx_graph.nodes[start_id])
        if not start_node:
            return []

        # BFS
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(start_id, 0)])
        chain: list[ChainNode] = []

        while queue and len(chain) < max_methods:
            nid, depth = queue.popleft()
            if nid in visited or depth > max_depth:
                continue
            visited.add(nid)

            try:
                node = graph.get_node(nid)
            except (AttributeError, TypeError):
                nx_graph = getattr(graph, '_graph', graph)
                node = dict(nx_graph.nodes[nid]) if nid in nx_graph.nodes else None
            if not node:
                continue

            # 只保留 METHOD 类型
            entity_type = node.get("entity_type", "")
            if entity_type != "method":
                continue

            # 接口→实现跳转：如果当前方法是接口方法，自动跳转到实现类方法
            # 匹配策略：先按 entity ID 精确匹配，再按 (class_name, method_name) 模糊匹配
            impl_ids = self._iface_to_impls.get(nid, [])
            if not impl_ids:
                # entity ID 没匹配上 → 用 (class_name, method_name) 再找一次
                cls_name = node.get("class_name", "")
                method_name = node.get("name", "")
                if cls_name and method_name and not cls_name.endswith("Impl"):
                    impl_ids = self._find_impls_by_name(cls_name, method_name)
            if impl_ids:
                if len(impl_ids) == 1:
                    # 单实现：直接跳转替代
                    impl_id = impl_ids[0]
                    if impl_id not in visited:
                        impl_node = None
                        try:
                            impl_node = graph.get_node(impl_id)
                        except (AttributeError, TypeError):
                            nx_g = getattr(graph, '_graph', graph)
                            impl_node = dict(nx_g.nodes[impl_id]) if impl_id in nx_g.nodes else None
                        if impl_node:
                            nid = impl_id
                            node = impl_node
                            visited.add(impl_id)
                else:
                    # 多实现：将所有实现加入队列作为并行分支
                    # 当前接口方法仍保留在链路中作为"分支点"标记
                    for impl_id in impl_ids:
                        if impl_id not in visited:
                            queue.append((impl_id, depth))  # 同层展开（分支）

            # 构建 ChainNode — 合并 Java 代码 + SQL
            java_snippet = self._snippet_index.get(nid, "")
            sql_snippet = self._sql_index.get(nid, "")
            if sql_snippet and java_snippet:
                combined = f"{java_snippet}\n\n-- ========== MyBatis SQL ==========\n{sql_snippet}"
            elif sql_snippet:
                combined = f"-- [DAO 接口方法，SQL 来自 MyBatis XML]\n\n{sql_snippet}"
            else:
                combined = java_snippet or None

            chain_node = ChainNode(
                method_id=nid,
                class_name=node.get("class_name", "") or node.get("name", ""),
                method_name=node.get("name", ""),
                signature=node.get("signature", "") or node.get("name", ""),
                depth=depth,
                code_snippet=combined,
                module_id=node.get("module_id"),
                location=node.get("location"),
            )
            chain.append(chain_node)

            # 展开邻居（兼容 KnowledgeGraph 和裸 NetworkX 图）
            # 注：第三方 JAR 的方法不在图中，CALLS 边不存在，BFS 自然停止
            #     不需要额外判断"有无代码"——没有出边就是终点
            # 跳过 callee 是 getter/setter 的边——这些只是属性读写，不是业务逻辑调用
            if direction in ("down", "both"):
                try:
                    callees = graph.successors(nid, rel_type="calls")
                    for callee_id in callees:
                        if callee_id not in visited and not self._is_getter_setter(callee_id):
                            queue.append((callee_id, depth + 1))
                except (AttributeError, TypeError):
                    try:
                        nx_graph = getattr(graph, '_graph', graph)
                        for _, tgt, edata in nx_graph.out_edges(nid, data=True):
                            if edata.get("rel_type") == "calls" and tgt not in visited and not self._is_getter_setter(tgt):
                                queue.append((tgt, depth + 1))
                    except Exception:
                        pass

            if direction in ("up", "both"):
                try:
                    callers = graph.predecessors(nid, rel_type="calls")
                    for caller_id in callers:
                        if caller_id not in visited and not self._is_getter_setter(caller_id):
                            queue.append((caller_id, depth + 1))
                except (AttributeError, TypeError):
                    try:
                        nx_graph = getattr(graph, '_graph', graph)
                        for src, _, edata in nx_graph.in_edges(nid, data=True):
                            if edata.get("rel_type") == "calls" and src not in visited and not self._is_getter_setter(src):
                                queue.append((src, depth + 1))
                    except Exception:
                        pass

        # 按深度排序
        chain.sort(key=lambda n: (n.depth, n.class_name, n.method_name))
        return chain

    def _find_impls_by_name(self, interface_class: str, method_name: str) -> list[str]:
        """通过 (class_name, method_name) 查找实现类方法（解决重复实体 ID 不匹配问题）。"""
        nx_graph = getattr(self._graph, '_graph', self._graph)
        results = []
        for nid, attrs in nx_graph.nodes(data=True):
            if attrs.get("entity_type") != "method":
                continue
            cls = attrs.get("class_name", "")
            name = attrs.get("name", "")
            if name != method_name:
                continue
            # 检查是否是 interface 的实现类
            if cls == interface_class + "Impl":
                results.append(nid)
            elif cls.endswith("Impl") and interface_class in cls:
                results.append(nid)
        # 去重：优先选有代码片段的
        if len(results) > 1:
            with_code = [r for r in results if self._snippet_index.get(r)]
            if with_code:
                results = with_code[:1]
        return results

    def _is_getter_setter(self, method_id: str) -> bool:
        """判断方法是否是 getter/setter（BFS 时跳过这些属性读写调用）。"""
        graph = self._graph
        try:
            node = graph.get_node(method_id)
        except (AttributeError, TypeError):
            nx_graph = getattr(graph, '_graph', graph)
            node = dict(nx_graph.nodes[method_id]) if method_id in nx_graph.nodes else None
        if not node:
            return False
        return bool(node.get("is_getter")) or bool(node.get("is_setter"))

    def _build_prompt(self, chain: list[ChainNode]) -> str:
        """构建完整 prompt：系统指令 + 调用链代码。"""
        entry = chain[0] if chain else None
        entry_name = f"{entry.class_name}.{entry.method_name}" if entry else "unknown"
        max_depth = max(n.depth for n in chain) if chain else 0

        # 系统指令
        if self._language.startswith("en"):
            header = (
                f"You are a senior expert with both business analysis and technical architecture capabilities.\n"
                f"You are writing a 'Code Business Interpretation Document'.\n"
                f"Target readers: new developers, product managers, QA engineers.\n\n"
                f"Below is the complete call chain code for '{entry_name}' "
                f"({len(chain)} methods, {max_depth} levels deep).\n\n"
                f"Please output your analysis in the following structure:\n\n"
                f"## 1. Business Scenario\n- Who triggers this operation, in what context\n\n"
                f"## 2. Preconditions\n- What must be true before execution\n\n"
                f"## 3. Main Flow (in execution order)\n- Numbered steps in business language, not code language\n\n"
                f"## 4. Business Rules\n- Extract from code, format: 'When X, then Y'\n- Cite which code/condition each rule comes from\n\n"
                f"## 5. Data Changes\n- Which tables/fields are read and written\n- Atomicity and consistency guarantees\n\n"
                f"## 6. Exception Scenarios\n- Each: trigger condition → system behavior → user impact\n\n"
                f"## 7. Upstream/Downstream Impact\n- What downstream functions are affected by this operation\n- What upstream changes would affect this operation\n\n"
                f"## 8. Risks & Recommendations\n- Business-level risks (not just technical)\n- Specific improvement suggestions for each\n"
            )
        else:
            header = (
                f"你是一位同时具备业务分析和技术架构能力的资深专家。\n"
                f"你正在编写「代码业务解读文档」，目标读者是：新入职开发者、产品经理、测试工程师。\n\n"
                f"以下是「{entry_name}」的完整调用链代码"
                f"（{len(chain)} 个方法，{max_depth} 层深度）。\n\n"
                f"请按以下结构输出分析报告：\n\n"
                f"## 1. 业务场景\n- 谁（什么角色/系统）在什么场景下触发这个操作\n- 对应的产品功能描述（用非技术语言）\n\n"
                f"## 2. 前置条件\n- 执行这个操作前必须满足的条件\n\n"
                f"## 3. 主流程（按执行顺序）\n- 用编号列出每一步，用业务语言描述而非代码语言\n- 标注每一步涉及的系统或服务\n\n"
                f"## 4. 业务规则\n- 从代码中提炼出的业务约束（用「当...时，则...」格式）\n- 每条规则标注规则来源（哪段代码/哪个条件）\n\n"
                f"## 5. 数据变更\n- 读取了哪些数据库表的哪些字段\n- 修改了哪些数据库表的哪些字段\n- 数据变更的原子性和一致性保障\n\n"
                f"## 6. 异常场景\n- 列出所有可能的失败场景\n- 每个场景：触发条件 → 系统行为 → 用户感知\n\n"
                f"## 7. 上下游影响\n- 这个操作的结果会影响哪些下游功能\n- 哪些上游变更会影响这个操作的行为\n\n"
                f"## 8. 风险与建议\n- 从代码中识别出的业务风险（不是纯技术风险）\n- 每个风险给出具体的改进建议\n"
            )

        # 拼接代码
        code_parts = []
        total_chars = len(header)

        for node in chain:
            snippet = node.code_snippet or "(无代码片段)"
            label = f"[L{node.depth}{'入口' if node.depth == 0 else ''}] {node.class_name}.{node.signature}"
            section = f"\n{'=' * 60}\n{label}\n{'=' * 60}\n{snippet}\n"

            if total_chars + len(section) > MAX_PROMPT_CHARS:
                remaining = MAX_PROMPT_CHARS - total_chars
                if remaining > 300:
                    truncated = snippet[:remaining - 100] + "\n// ... 代码截断（上下文保护）..."
                    section = f"\n{'=' * 60}\n{label}\n{'=' * 60}\n{truncated}\n"
                    code_parts.append(section)
                code_parts.append(f"\n// ⚠ 后续 {len(chain) - len(code_parts)} 个方法因上下文限制被省略")
                break
            total_chars += len(section)
            code_parts.append(section)

        return header + "\n".join(code_parts)
