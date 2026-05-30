# Phase 0 — CodeGraph vs 自研 结构覆盖对照 + go/no-go

> 实测于 mall-swarm（`/Users/java/repos/mall-swarm/.codegraph/codegraph.db`，702 文件）。
> 本报告是「先验证再切」闸门的产出物。**结论：GO（CodeGraph 侧已足够）**。

## 1. 节点覆盖（by kind）

| kind | 数量 |
|---|---|
| method | 15,625 |
| import | 2,842 |
| field | 2,230 |
| file | 650 |
| class | 595 |
| constant | 288 |
| **route** | **252**（Spring 入口，自研此前没有结构化） |
| interface | 164 |
| enum_member / enum | 7 / 2 |

## 2. 边覆盖（by kind）

| kind | 数量 | 对 KE 的意义 |
|---|---|---|
| contains | 21,465 | 包含层级（file→class→method） |
| **calls** | **12,083** | **callers/callees/impact 的命脉** ✅ |
| references | 12,071 | 泛引用 |
| imports | 2,842 | 文件依赖 |
| instantiates | 549 | new X() |
| extends / implements | 96 / 51（合计 147） | 类层级 ✅ |

## 3. KE QA 需求逐条判定

| KE 需要 | CodeGraph 提供 | 判定 |
|---|---|---|
| callers / callees | `calls` 边 12,083 + 精确节点查询（已实证） | ✅ 够 |
| impact（多跳） | 沿 `calls` 多跳 | ✅ 够 |
| 方法 → 源码 | `file_path` + `start_line`/`end_line` | ✅ 够 |
| 入口识别 | `route` 节点 252 | ✅ 比自研强 |
| 类层级 | `extends`/`implements` 147 | ✅ 够 |
| 稳定身份 | `qualified_name` 列（行号无关，已实证 3 个 generateOrder 可分） | ✅ 够 |
| 精确节点导航（不按名糊） | 按 `qualified_name`/`id` 精确查 edges（已实证） | ✅ 够（CLI/MCP 做不到，直读可以） |

## 4. 暂缓项（需 KE 数据栈在线，不阻塞 GO）

- **头对头对照**：CodeGraph callees vs 自研 Neo4j callees 逐条覆盖比对——需 Neo4j 有 mall-swarm 图（当前为空）+ Weaviate 隧道。
- **Java 精度抽查**：tree-sitter vs javaparser 在复杂注解/泛型/重载上的细节差异——抽几个复杂方法对照。核心导航能力已足够，此为风险细查，不是 GO 阻塞项。

## 5. 结论

**GO** —— CodeGraph 的结构覆盖（calls/references/contains/extends/implements/route + qualified_name + 精确导航）足够支撑 KE 的结构导航需求，进入 Phase 1。暂缓的头对头/精度抽查在 KE 栈在线后补做，作为风险确认。
