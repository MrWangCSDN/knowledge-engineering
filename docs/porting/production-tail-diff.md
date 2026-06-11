# 生产尾差记录（production ≠ py-final-baseline）

> 2026-06-11 用户决策（TS 重构 Phase 0 Task 8）：**不**用冻结基线做最后一次 Python 部署。
> 生产保持 `a2e168e`（2026-06-08 部署）直到 TS 版切换；下面的尾差 commit 已进基线但从未上过生产。

## 影响（TS 重构期间须记住的三件事）

1. **TS Phase 3 对齐目标 = `py-final-baseline`（tag），不是生产**。eval 重跑、SSE 协议、行为对照都以基线为准。
2. **热回滚语义**：TS 切换出问题时 nginx 回切的 Python 进程跑的是 a2e168e（比基线老 13 个代码 commit）——回滚后没有候选树与画图修复，属已知降级。
3. 这批尾差 commit 在 6/4-6/5 两轮 eval **之后**提交，未单独跑过 eval；全量回归 1022 测试在基线树上全绿（2026-06-11 实测）。

## 尾差 commit 清单（a2e168e → py-final-baseline 的代码行为变化，13 个）

| hash | 主题 |
|---|---|
| ebe7e0b | fix(qa): render_call_graph mode B 加边核验 — 丢弃 CodeGraph 无支撑的伪 calls 边 |
| 69b2b65 | fix(qa): 接通 render_call_graph 的 summary_lookup → 节点 label 走真 2b 解读 |
| 3baad5e | feat(qa): candidate_assembly 模块骨架 + TreeNode/CandidateTree dataclass (Task 1) |
| 3dd98d7 | feat(qa): compute_independent_entries 算法 — 双向 reach 集合互查（Plan-Task 2） |
| 7f846a0 | feat(qa): build_subtree_for_entry — BFS 入口子树，候选优先（Plan-Task 3） |
| f27cec2 | feat(qa): build_candidate_tree 编排 — 子树/孤儿/token cap/fallback（Plan-Task 4） |
| 535c710 | feat(qa): RetrievedContext.candidate_tree + retriever 计算 + ctx→dict 透传（Plan-Task 5） |
| cdd3c7f | feat(qa): prompts 候选区树形分支 + _render_tree/_render_flat helpers（Plan-Task 6） |
| 787a03f | fix(qa): 补 freeform 边校验漏洞 — _node_qn 加 method/code 字段提取 |
| c8447ed | fix(qa): 调用图 . 归一 :: + prompt 强化"interface/impl/异步桥接"约束 |
| 8d0fb6d | feat(qa): 调用图放宽异步/语义边 — label 含关键词跳 calls 校验 |
| 1ea7b9e | fix(qa): 防 narrate-tool 退化 — prompt 强约束 + 后端剥 render_call_graph 代码块 |
| 459c58c | fix(qa): render_call_graph mode A 入口剥 method:// scheme — 治双前缀致前端渲染失败 |

（另有文档 commit 2cf1509「候选组装 TDD 实施计划」与 Phase 0 自身的 docs/merge commit，无行为影响。）

## 候选组装老计划（2026-06-08-candidate-tree-assembly.md，10 tasks）收尾状态

- Task 1-6（全部代码+测试）：✅ 已 commit 进基线
- Task 7（全量回归）：✅ 由 Phase 0 Task 2 覆盖（pytest 1022 passed, 0 failed）
- Task 8（部署+10 题手测）：⛔ 用户决策不部署，随 TS 重构 Phase 4 切换自然落地
- Task 9（可选 30 题重跑）：⛔ 移入 TS Phase 3 eval（重跑时基线即含候选树行为）
- Task 10（Obsidian 标记）：✅ 本次完成（见下）
