# eval 基线（TS 重构门禁②：Phase 3 TS 版重跑须 ≥ 本基线）

> 对照物④。这是 Python→TS 重构「三门禁」之②的依据：TS 版 Phase 3 要重跑同样的题集，成绩 ≥ 本基线。
> 两套 eval 均为历史 Claude 会话**编排执行**（仓库无 eval 脚本）。题集与成绩从历史 transcript 抢救归档，来源逐条标注于下方与各题集 JSON 的 `source` 字段。

## 51 题 QA eval（2026-06-04 P1 gate，KE_QA_USE_REACT=1，agent 自由输出路径）

| 指标 | pre-P1 baseline | P1（= py-final-baseline 行为） |
|---|---|---|
| 准确 | 15 (29%) | **24 (47%)** |
| 部分 | 27 (53%) | 26 (51%) |
| 错误 | **9 (18%)** | **1 (2%)** |

净效果：准确 +60%（15→24），实质错误 −89%（9→1）。19 题 fixed、8 题 softened、7 题 regressed（仅 1 题真正掉到「错误」档）。

判准方法：7 个并行子代理按业务域分工（订单/支付/购物车/会员/营销/内容/互动），逐题读 mall-swarm 真实源码核对答案，准确/部分/错误 三档；baseline vs P1 配对判。

分域（准/部/错，baseline → P1）：订单 6/3/3→6/6/0｜支付 1/4/0→3/2/0｜购物车 4/3/1→5/3/0｜会员 2/5/1→5/3/0｜营销 0/4/1→0/5/0｜内容 1/6/1→4/4/0｜互动 1/2/2→1/3/1。

已知唯一残留错误：**#51 互动域「怎么清空商品浏览历史」** Mongo 被坐实成 SQL（agent 自信画 `DELETE FROM member_read_history ...` + 虚构越权校验，真相是 Spring-Data `MongoRepository.deleteAllByMemberId()` 无 SQL 无校验）。根因：P1 注入方法体，体内是 `repository.deleteAllByMemberId(...)` 调用，但看不到 `repository` 声明为 `MongoRepository`。Spring-Data 防臆造未做（TS 版后续阶段处理）。

> 注：题集 JSON 的 `domain` 取自 eval driver 脚本的域字段（权威：订单12/支付5/购物车8/会员8/营销5/内容8/互动5）。设计文档 §七 的「分域」按判准子代理的分工口径统计，两者对订单/支付的题数切分略有出入（如 #11 防超卖、#12 回调按单号更新支付状态在脚本里归「订单」，判准时算到「订单/支付」域），不影响总分 24/26/1。

## 30 题出图 eval（2026-06-05 reactflow 御用画图工具上线 eval，KE_QA_USE_REACT=1）

| 指标 | 基线 |
|---|---|
| 出图率（agent 自主调 render_call_graph） | 30/30 = 100% |
| 折叠进 sections（call_chain 段，持久化+有序） | 30/30 |
| 模式 B（freeform nodes/edges 逻辑图）使用次数 | 4 |
| 异常 | 0 |
| 已知残留 | 3/30 agent 多手画 mermaid（前端无条件 strip 不显示，仅浪费少量 token） |

模式 B 出现在 #6（订单超时取消）、#8（支付完整流程）、#20（商品状态机）为主图，外加 #17（秒杀配置）的第三次 render → 计「4 次」。
残留手画 mermaid：#18、#26、#30。
逐题 render 模式 / 次数 / 残留标记见 `diagram-30-questions.json` 各题字段。

## 题集完整性

- **51 题：full（51/51）**。来源 = transcript `~/.claude/projects/-Users-java-knowledge-engineering/fd2d311f-42dc-4073-bfcf-ba1b870d825d.jsonl`，eval driver `/tmp/_eval50.py` 的 `QUESTIONS` 数组逐字（L11829）；P1 复跑（L12045）题集完全一致，已交叉核对。每题含出题时锚定的真实 public 方法（`anchor_method`，判准用）。
- **30 题：full（30/30）**。来源 = 同一 transcript，eval driver `/tmp/_rv2eval.py` 的 `QUESTIONS` 数组逐字（L14628）；逐题出图结果取自 eval 原始日志（L14658）。
- **缺失处置：无缺失**，两套题集均完整恢复，无需主会话决策。

## 抢救过程备注

- 任务书原指 transcript 在 `-Users-java-knowledge-engineering-auth/`，实际历史会话落在 `-Users-java-knowledge-engineering/`（eval 虽在 `-auth` clone 的工作目录跑，但会话 transcript 归在主项目目录）。`/tmp/_eval50_*.json`、`/tmp/_rv2eval.*` 等 eval 产物已不在本机，故全部回到 transcript 抢救。
- 题集恢复来源是 eval driver 脚本里的 `QUESTIONS` 字面量（最权威，非答案/进度日志的截断文本），故为逐字原文。
- 两套成绩数字与 Obsidian 设计文档（`业务问答-源码优先接地-P1设计.md` §七、`业务问答-reactflow御用画图工具-设计.md` §九）一致，已交叉核对。
