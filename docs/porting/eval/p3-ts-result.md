# Phase 3 eval 对等结果（TS 版重跑 vs Python 基线）

> 2026-06-12 TS 版（ke-server `15d345a`）对真 DashScope + Weaviate(全量 mall-swarm 解读库) + codegraph 重跑两套 eval。
> harness：`ke-server/tools/eval/`（直驱 retriever+ReActSynthesizer，跳过 HTTP/auth）。判准：7 域 opus 并行读 mall-swarm 源码核对，准/部/错三档（与 2026-06-04 基线同方法）。
> 原始数据：`p3-qa51-results.json`（答案）/ `p3-qa51-verdicts.json`（判准）/ `p3-diagram30-results.json`。

## 30 题出图 eval

| 指标 | TS 版 | 基线 |
|---|---|---|
| 出图率（agent 自调 render_call_graph） | **30/30 = 100%** | 30/30 |
| 执行错误 | 0 | 0 |
| 平均 recall | 0.794 | — |

✅ **持平基线**。

## 51 题 QA eval

| 档 | TS 版 | 基线（py-final-baseline P1） |
|---|---|---|
| 准确 | **29 (57%)** | 24 (47%) |
| 部分 | 17 (33%) | 26 (51%) |
| 错误 | **5 (10%)** | 1 (2%) |

执行层：51/51 全跑通、0 失败、51/51 出图、平均 recall 0.799、平均答案 1683 字、单题均 ~40s。

### 分域（准/部/错）

| 域 | TS 版 | 基线 | 判定 |
|---|---|---|---|
| 订单 | 7/5/0 | 6/6/0 | ↑ 更好 |
| 支付 | 2/2/1 | 3/2/0 | ↓ 1 错（#17 细节） |
| 购物车 | 7/1/0 | 5/3/0 | ↑↑ 更好 |
| 会员 | 5/3/0 | 5/3/0 | = 持平 |
| 营销 | 3/2/0 | 0/5/0 | ↑↑ 0准→3准 |
| 内容 | 3/1/**4** | 4/4/0 | ↓↓ **退化集中点** |
| 互动 | 2/3/0 | 1/3/1 | ↑ 修掉基线唯一错(#51 Mongo) |

**6/7 域持平或更好**，含修掉基线唯一已知错误（#51 互动域 Mongo 被坐实成 SQL）。退化全部集中在**内容域**（首页/商品查询）。

### 5 个错误根因（按 cited 实体 + 源码核对诊断）

| # | 域 | 根因类型 |
|---|---|---|
| #43 | 内容 | **召回跨模块串扰**：anchor=hotProductList(前台 portal)，但召回命中 `SmsHomeNewProductService::list`（**mall-admin** 新品管理服务），答串到后台 |
| #45 | 内容 | **召回未命中**：cited 为空，agent 把独立 mall-search 模块的 ES 能力错挂到 mall-portal 纯 MySQL search |
| #46 | 内容 | **召回跨模块串扰**：命中正确的 `PmsPortalProductController::categoryTreeList`(portal) + 错误的 `PmsProductCategoryController::listWithChildren`(**mall-admin**)，agent 答成 admin 的两级树 |
| #40 | 内容 | **agent 在 stub 方法上臆造**：入口对（recommendProductList），但该方法实为 TODO「默认推荐所有」，agent 没读简单方法体、编了 HomeDao JOIN |
| #17 | 支付 | **细节断言错**：把 notify_url/return_url 误列为 bizContent 内字段（实为外层 request.setNotifyUrl）；入口与主流程对 |

### 关键洞察：基线 vs TS 召回数据条件不同（非 TS 移植 bug）

- 基线（2026-06-04）跑时 **「限 mall-portal 已解读模块」**（51 题 JSON `note` 明载）——彼时其他模块 2b 解读库为空（2026-06-05 才全量重生 809 条 8 模块），召回天然不会串到 admin。
- TS 版跑在**全量 mall-swarm 解读库**（线上当前状态）——召回会命中 admin 模块的同名/近似方法 → 内容域 3 个跨模块串扰（#43/#45/#46）。
- **这不是 TS 逻辑 bug**：召回门控/rerank/candidate_assembly 均经对抗审查逐字镜像 + P2d 真链路验证。同样的全量解读库下，线上 Python 版（2026-06-05 起即全量）大概率犯同样的内容域错误——即 24/26/1 基线本身是「旧数据条件」下的成绩，不代表当前线上 Python 真实表现。

## 门禁判定（待用户决策）

- spec 门禁原文：「51 题准确率 + 30 题出图率 ≥ Python 基线」。
- **准确率**：47%→57%（↑），**出图率**：100%（持平）→ 按字面门禁**达标**。
- **但错误档** 1→5（退化），4/5 集中内容域，主因 = 召回数据条件差异（全量解读库跨模块串扰）+ 1 个 agent stub 臆造 + 1 个支付细节。
- **结论**：TS 版整体质量净增（准确↑、基线已知错已修、6/7 域≥），退化非移植 bug 而是召回数据/agent 行为层问题。

## ✅ 门禁判定（2026-06-12 用户裁定）

**Phase 3 通过**。依据：准确率 47%→57% 达标、出图 100% 持平、6/7 域≥基线、基线唯一已知错（#51 Mongo）已修；5 个错经诊断为召回数据条件差异（全量解读库跨模块串扰，线上 Python 同样会犯）+ 1 agent stub 臆造 + 1 支付细节，**非 TS 移植 bug**。内容域跨模块串扰记为**已知产品改进项**（后续 GraphRAG / 召回加模块亲和过滤治理，与移植对等无关）。下一步推进 Phase 5 离线 pipeline。
