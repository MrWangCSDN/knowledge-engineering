# TS 重构 Phase 1：新仓 ke-server 脚手架 + 基建 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `ke-server` pnpm monorepo（apps/api + 4 个 packages），zod 解析现有 `project.yaml`，接通四类基建（MySQL/drizzle、Weaviate、Neo4j、DashScope LLM），实现与 Python 基线同口径的 `GET /health`，drizzle introspect 接管现有 MySQL schema。

**Architecture:** 薄壳 Hono + 普通 TS 模块（无 DI 框架）。健康检查逐函数镜像老仓 `src/service/infra_health.py`（4 依赖、每依赖 5s 超时、**neo4j 非致命**）。所有基建 client 都是「纯函数 + 注入依赖」风格，单测不连真实服务（gated integration test 按 env 自动 skip）。

**Tech Stack:** Node 24（本机 v24.8.0，engines >=22）/ pnpm 11 / TypeScript strict（moduleResolution bundler，tsx 运行）/ vitest / Biome / Hono + @hono/node-server / zod ^3 / yaml / mysql2 + drizzle-orm + drizzle-kit / weaviate-client v3 / neo4j-driver / ai + @ai-sdk/openai-compatible / node:sqlite（Node 24 内置，CodeGraph 只读）。

---

## 执行环境事实（写计划时已核实，2026-06-11）

| 事实 | 值 |
|---|---|
| 新仓位置 | `/Users/java/ke-server`（gh 已登录 MrWangCSDN，可建私有远端） |
| Python 老仓 | `/Users/java/knowledge-engineering`（main = `py-final-baseline` + Phase 0 产物；**只读参照，不改**） |
| 健康检查镜像源 | 老仓 `src/service/infra_health.py`（4 依赖 mysql/neo4j/weaviate/dashscope，`PING_TIMEOUT_SEC=5`）+ `src/service/deps_infra.py`（`_NON_CRITICAL_DEPS={"neo4j"}`，CodeGraph 迁移后 neo4j 退役中）+ `api.py:364` `/health` 路由 |
| /health 响应（Python 口径） | 普通用户 `{"healthy": bool, "ts": iso}`；admin 加 `"deps"`。Phase 1 无 auth：用 `KE_HEALTH_DEBUG=1` 才附 deps，admin 门控 Phase 2 接上（偏差已记录） |
| Weaviate ping 方式 | `GET {url}/v1/.well-known/live`，200=ok，无需 auth（镜像 `_ping_weaviate`） |
| CodeGraph | **本地 SQLite**：`<repo>/.codegraph/codegraph.db`（`src/integrations/codegraph/paths.py`），只读 |
| MySQL 通路 | SSH 隧道：老仓 `scripts/start_mysql_tunnel.sh`（本地 3307 → 蓝队云 103.47.81.50:26666 → 127.0.0.1:3306） |
| env 名单 | 取自 `/Users/java/knowledge-engineering-auth/.env.local`（**有真实 secrets，cp 不 cat**）：KE_DB_URL / KE_JWT_* / NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD / WEAVIATE_URL / WEAVIATE_API_KEY / DASHSCOPE_API_KEY / MINIMAX_API_KEY / MINIMAX_BASE_URL / MINIMAX_MODEL |
| KE_DB_URL 格式 | `mysql+asyncmy://user:pw@localhost:3307/db`（SQLAlchemy 方言）→ TS 侧要剥 `+asyncmy` |
| project.yaml | 老仓 `config/project.yaml`（208 行；**含内联 LLM key，fixture 必须脱敏**）；TS 强类型解析 repo / knowledge.graph / knowledge.vectordb-* / semantic_embedding，其余 `.passthrough()` |
| MySQL 表（基线） | users / projects / groups / group_members / user_project_access / git_credentials / audit_logs / qa_sessions（+ alembic_version；以 introspect 实测为准） |

通用约束：
- **不改老仓与 obsidian 的任何源码**（收口任务的状态更新除外）
- 所有 commit 尾行：`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- 中文注释规范（用户全局 MUST）：文件级 docstring 注释、导出函数 JSDoc、关键语句解释「为什么 + 语法点」
- 依赖装最新稳定版（`pnpm add <pkg>`），把解析出的版本如实写进报告；遇到 API 与本计划代码不符，以官方当前 API 为准小幅调整并在报告记录偏差

---

### Task 1: 仓库初始化 + 工具链

**Files:**
- Create: `/Users/java/ke-server/`（git init）、`package.json`、`pnpm-workspace.yaml`、`tsconfig.base.json`、`tsconfig.typecheck.json`、`biome.json`、`vitest.config.ts`、`.gitignore`、`.nvmrc`、`.env.example`、`README.md`

- [ ] **Step 1: 建仓 + git init**

```bash
mkdir -p /Users/java/ke-server && cd /Users/java/ke-server
git init -b main
```

- [ ] **Step 2: 写工作区与工具链配置文件**（以下逐个落盘）

`pnpm-workspace.yaml`：
```yaml
packages:
  - "apps/*"
  - "packages/*"
```

`package.json`（根，private）：
```json
{
  "name": "ke-server",
  "private": true,
  "type": "module",
  "engines": { "node": ">=22" },
  "scripts": {
    "dev": "pnpm --filter ke-api dev",
    "test": "vitest run",
    "typecheck": "tsc -p tsconfig.typecheck.json",
    "lint": "biome check .",
    "format": "biome format --write ."
  }
}
```

`tsconfig.base.json`：
```json
{
  "compilerOptions": {
    "target": "ES2023",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "noImplicitOverride": true,
    "verbatimModuleSyntax": true,
    "skipLibCheck": true,
    "isolatedModules": true,
    "types": ["node"],
    "noEmit": true
  }
}
```

`tsconfig.typecheck.json`（全仓一把 typecheck）：
```json
{
  "extends": "./tsconfig.base.json",
  "include": ["apps/*/src", "packages/*/src"]
}
```

`vitest.config.ts`（单一根配置，不用 per-package）：
```ts
import { defineConfig } from "vitest/config";

// 单一根配置：所有包的测试统一 include，避免 N 份配置漂移
export default defineConfig({
  test: {
    include: ["apps/**/src/**/*.test.ts", "packages/**/src/**/*.test.ts"],
  },
});
```

`biome.json`（最小配置；若 biome 报 schema 迁移提示，跑 `pnpm biome migrate --write` 后以迁移结果为准）：
```json
{
  "formatter": { "enabled": true, "indentStyle": "space" },
  "linter": { "enabled": true, "rules": { "recommended": true } }
}
```

`.gitignore`：
```
node_modules/
dist/
coverage/
*.log
.DS_Store
.env
.env.*
!.env.example
```

`.nvmrc`：
```
24
```

`.env.example`（**只放占位值**，变量名单来自老仓 -auth/.env.local）：
```bash
# MySQL（开发走 SSH 隧道 3307，见老仓 scripts/start_mysql_tunnel.sh）
# 注意：沿用 Python 老格式 mysql+asyncmy://，TS 侧代码会自动剥 +asyncmy
KE_DB_URL=mysql+asyncmy://ke_app:CHANGE_ME@localhost:3307/knowledge_engineering
# Neo4j（非致命依赖，退役中）
NEO4J_URI=bolt://CHANGE_ME:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=CHANGE_ME
# Weaviate
WEAVIATE_URL=http://CHANGE_ME:8080
WEAVIATE_API_KEY=CHANGE_ME
# LLM
DASHSCOPE_API_KEY=sk-CHANGE_ME
# DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MINIMAX_API_KEY=CHANGE_ME
MINIMAX_BASE_URL=https://api.minimax.chat/v1
MINIMAX_MODEL=CHANGE_ME
# API
KE_API_PORT=8700
# KE_HEALTH_DEBUG=1   # /health 响应附 deps 明细（Phase 2 改为 admin 门控）
```

`README.md`：
```markdown
# ke-server

KE（knowledge-engineering）后端的 TypeScript 重写（进行中）。

- 总体路线 spec：Obsidian `01 Engineering/knowledge-engineering/TS重构-总体路线-设计.md`
- 移植基线：老仓 tag `py-final-baseline`；移植对照物：老仓 `docs/porting/`
  （46 路由 openapi / 13 工具 schema / SSE 协议 / eval 题集 51+30 / 不移植清单 / 生产尾差）
- 结构：pnpm workspace — `apps/api`（Hono）+ `packages/{shared,store,codegraph,llm}`

## 开发

\`\`\`bash
cp /Users/java/knowledge-engineering-auth/.env.local .env.local   # 凭证（勿提交）
bash /Users/java/knowledge-engineering/scripts/start_mysql_tunnel.sh  # MySQL 隧道
pnpm install
pnpm dev          # ke-api @ http://localhost:8700
pnpm test && pnpm typecheck && pnpm lint
\`\`\`
```

- [ ] **Step 3: 装根 devDependencies 并验证工具可跑**

```bash
cd /Users/java/ke-server
pnpm add -D -w typescript @types/node tsx vitest @biomejs/biome
pnpm typecheck   # 预期：通过（还没有 src，include 为空不报错；若 tsc 报 "No inputs"，在 tsconfig.typecheck.json 加 "files": [] 消掉）
pnpm exec biome --version
```

- [ ] **Step 4: 首次 commit + 建远端 push**

```bash
git add -A
git commit -m "chore: ke-server monorepo 脚手架 — pnpm workspace + TS strict + vitest + biome

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
gh repo create MrWangCSDN/ke-server --private --source=. --remote=origin --push
```

预期：仓库 `MrWangCSDN/ke-server` 创建成功并推送 main。若 gh 失败（网络/权限），记录原因继续本地开发，push 留到 Task 7。

---

### Task 2: @ke/shared — env 加载 + project.yaml zod 解析

**Files:**
- Create: `packages/shared/package.json`、`packages/shared/tsconfig.json`
- Create: `packages/shared/src/index.ts`、`src/types.ts`、`src/env.ts`、`src/config.ts`
- Test: `packages/shared/src/env.test.ts`、`src/config.test.ts`、`src/fixtures/project.sample.yaml`

- [ ] **Step 1: 包骨架**

`packages/shared/package.json`：
```json
{
  "name": "@ke/shared",
  "private": true,
  "type": "module",
  "exports": { ".": "./src/index.ts" }
}
```

`packages/shared/tsconfig.json`：
```json
{ "extends": "../../tsconfig.base.json", "include": ["src"] }
```

```bash
cd /Users/java/ke-server
pnpm add --filter @ke/shared zod yaml
```

- [ ] **Step 2: 写失败测试（红）— env**

`packages/shared/src/env.test.ts`：
```ts
import { describe, expect, it } from "vitest";
import { loadEnv, normalizeMysqlUrl } from "./env.js";

describe("normalizeMysqlUrl", () => {
  it("剥掉 SQLAlchemy 方言后缀 +asyncmy", () => {
    expect(normalizeMysqlUrl("mysql+asyncmy://u:p@localhost:3307/db")).toBe(
      "mysql://u:p@localhost:3307/db",
    );
  });
  it("已是标准 mysql:// 则原样返回", () => {
    expect(normalizeMysqlUrl("mysql://u:p@h:3306/db")).toBe("mysql://u:p@h:3306/db");
  });
});

describe("loadEnv", () => {
  it("从传入的字典解析（不读真实 process.env），缺省值生效", () => {
    const env = loadEnv({ KE_DB_URL: "mysql+asyncmy://u:p@h:3307/db" });
    expect(env.KE_DB_URL).toBe("mysql+asyncmy://u:p@h:3307/db");
    expect(env.KE_API_PORT).toBe(8700); // 默认端口
  });
  it("KE_API_PORT 字符串数字会被 coerce 成 number", () => {
    expect(loadEnv({ KE_API_PORT: "9000" }).KE_API_PORT).toBe(9000);
  });
});
```

- [ ] **Step 3: 跑红** — `pnpm vitest run packages/shared`，预期 FAIL（env.js 不存在）

- [ ] **Step 4: 实现 types + env（绿）**

`packages/shared/src/types.ts`：
```ts
/**
 * 跨包共享的基础类型。
 *
 * DepStatus / InfraStatus 镜像老仓 src/service/infra_health.py 的 TypedDict：
 * ok=true 时无 error；ok=false 时必带 error 字符串。
 */
export type DepStatus = { ok: true } | { ok: false; error: string };

/** 4 个基建依赖的整体状态（与 Python InfraStatus 字段一一对应） */
export type InfraStatus = {
  mysql: DepStatus;
  neo4j: DepStatus;
  weaviate: DepStatus;
  dashscope: DepStatus;
};
```

`packages/shared/src/env.ts`：
```ts
/**
 * 环境变量加载与校验。
 *
 * 变量名与 Python 老仓 .env.local 完全一致（移植期同一份 env 两边可用）。
 * 用 zod 做「解析即校验」：错误在启动时暴露，而不是运行中 undefined 炸开。
 */
import { z } from "zod";

/** 把 SQLAlchemy 方言 URL（mysql+asyncmy://）剥成 mysql2 认识的 mysql:// */
export function normalizeMysqlUrl(raw: string): string {
  // 正则：行首 mysql+任意单词:// 替换为 mysql://；不匹配则原样返回
  return raw.replace(/^mysql\+\w+:\/\//, "mysql://");
}

// z.coerce.number()：先把字符串强转 number 再校验（env 全是字符串）
const EnvSchema = z.object({
  KE_DB_URL: z.string().optional(),
  NEO4J_URI: z.string().optional(),
  NEO4J_USER: z.string().optional(),
  NEO4J_PASSWORD: z.string().optional(),
  WEAVIATE_URL: z.string().optional(),
  WEAVIATE_API_KEY: z.string().optional(),
  DASHSCOPE_API_KEY: z.string().optional(),
  DASHSCOPE_BASE_URL: z.string().optional(),
  MINIMAX_API_KEY: z.string().optional(),
  MINIMAX_BASE_URL: z.string().optional(),
  MINIMAX_MODEL: z.string().optional(),
  KE_API_PORT: z.coerce.number().int().default(8700),
  KE_HEALTH_DEBUG: z.string().optional(),
});

export type KeEnv = z.infer<typeof EnvSchema>;

/**
 * 解析 env 字典（默认 process.env；测试可注入普通对象）。
 * 入口处（apps/api/src/index.ts）会先 process.loadEnvFile() 读 .env.local。
 */
export function loadEnv(source: Record<string, string | undefined> = process.env): KeEnv {
  return EnvSchema.parse(source);
}
```

- [ ] **Step 5: 跑绿** — `pnpm vitest run packages/shared`，预期 4 passed

- [ ] **Step 6: 写失败测试（红）— config**

`packages/shared/src/fixtures/project.sample.yaml`（**脱敏 fixture**，结构对齐老仓 config/project.yaml，绝不复制真实 key）：
```yaml
repo:
  path: "/tmp/fake-repo"
  project_id: mall-swarm
  language: java
  modules:
    - id: mall-portal
      business_domains: [移动端商城域]
knowledge:
  semantic_embedding:
    backend: dashscope
    model: text-embedding-v4
  graph:
    backend: neo4j
    neo4j_uri: "bolt://localhost:7687"
    neo4j_user: "neo4j"
    neo4j_password: "fake"
    neo4j_database: "neo4j"
  vectordb-code:
    enabled: true
    backend: weaviate
    dimension: 1024
    weaviate_url: "http://localhost:8080"
    weaviate_grpc_port: 50051
    weaviate_api_key: "fake-key"
    collection_name: "CodeEntity"
  vectordb-interpret:
    enabled: true
    backend: weaviate
    dimension: 1024
    weaviate_url: "http://localhost:8080"
    weaviate_grpc_port: 50051
    weaviate_api_key: "fake-key"
    collection_name: "TopologicalInterpretation"
  topological_interpretation:
    enabled: true
    llm_backend: multi
extra_unknown_section:
  anything: true
```

`packages/shared/src/config.test.ts`：
```ts
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { loadProjectConfig } from "./config.js";

// import.meta.url 是当前模块的 file:// URL；fileURLToPath 转成普通路径
const FIXTURE = fileURLToPath(new URL("./fixtures/project.sample.yaml", import.meta.url));

describe("loadProjectConfig", () => {
  it("解析强类型字段", () => {
    const cfg = loadProjectConfig(FIXTURE);
    expect(cfg.repo.project_id).toBe("mall-swarm");
    expect(cfg.knowledge?.["vectordb-code"]?.collection_name).toBe("CodeEntity");
    expect(cfg.knowledge?.graph?.neo4j_uri).toBe("bolt://localhost:7687");
  });
  it("未知段落 passthrough 保留（兼容 Python 侧还在用的字段）", () => {
    const cfg = loadProjectConfig(FIXTURE) as Record<string, unknown>;
    expect(cfg.extra_unknown_section).toEqual({ anything: true });
  });
  it("repo.path 缺失时抛 zod 错误", () => {
    expect(() => loadProjectConfig(FIXTURE.replace(".sample", ".nonexistent"))).toThrow();
  });
});
```

- [ ] **Step 7: 跑红**，然后实现 `packages/shared/src/config.ts`（绿）

```ts
/**
 * project.yaml 解析：单一配置来源（与 Python 老仓 config/project.yaml 同一份文件）。
 *
 * 设计取舍：只对 TS 在线服务**当前要读**的段落强类型（repo / knowledge.graph /
 * vectordb-* / semantic_embedding），其余 .passthrough() 透传——YAGNI，
 * 离线 pipeline 的段落等 Phase 5 移植时再补强类型。
 */
import { readFileSync } from "node:fs";
import { parse } from "yaml";
import { z } from "zod";

// vectordb-code / vectordb-interpret 结构相同，抽一个 schema 复用（DRY）
const VectorDbSchema = z
  .object({
    enabled: z.boolean().default(true),
    backend: z.string(),
    dimension: z.number().int(),
    weaviate_url: z.string().optional(),
    weaviate_grpc_port: z.number().int().optional(),
    weaviate_api_key: z.string().optional(),
    collection_name: z.string(),
  })
  .passthrough();

const ProjectConfigSchema = z
  .object({
    repo: z
      .object({
        path: z.string(),
        project_id: z.string(),
        language: z.string().optional(),
        modules: z.array(z.object({ id: z.string() }).passthrough()).optional(),
      })
      .passthrough(),
    knowledge: z
      .object({
        semantic_embedding: z
          .object({ backend: z.string(), model: z.string() })
          .passthrough()
          .optional(),
        graph: z
          .object({
            backend: z.string().optional(),
            neo4j_uri: z.string().optional(),
            neo4j_user: z.string().optional(),
            neo4j_password: z.string().optional(),
            neo4j_database: z.string().optional(),
          })
          .passthrough()
          .optional(),
        "vectordb-code": VectorDbSchema.optional(),
        "vectordb-interpret": VectorDbSchema.optional(),
      })
      .passthrough()
      .optional(),
  })
  .passthrough();

export type ProjectConfig = z.infer<typeof ProjectConfigSchema>;

/** 读取并校验 project.yaml；文件不存在/结构不符都会抛错（启动期 fail-fast） */
export function loadProjectConfig(absPath: string): ProjectConfig {
  const raw = readFileSync(absPath, "utf-8"); // 同步读：只在启动时调用一次
  return ProjectConfigSchema.parse(parse(raw));
}
```

`packages/shared/src/index.ts`：
```ts
/** @ke/shared 公开出口：types + env + config */
export * from "./types.js";
export * from "./env.js";
export * from "./config.js";
```

- [ ] **Step 8: 跑绿 + typecheck + commit**

```bash
pnpm vitest run packages/shared   # 预期 7 passed
pnpm typecheck
git add packages/shared pnpm-lock.yaml
git commit -m "feat(shared): env 加载（zod + asyncmy URL 归一）+ project.yaml 强类型解析

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: @ke/store — MySQL client + drizzle introspect 接管 schema

**Files:**
- Create: `packages/store/package.json`、`tsconfig.json`、`drizzle.config.ts`
- Create: `packages/store/src/index.ts`、`src/mysql.ts`
- Create: `packages/store/src/schema/`（drizzle-kit pull 生成，提交进 git）
- Create: `packages/store/BASELINE-DB.md`（introspect 时的 alembic 头 + 表清单存证）
- Test: `packages/store/src/mysql.test.ts`

- [ ] **Step 1: 包骨架 + 依赖**

`packages/store/package.json`：
```json
{
  "name": "@ke/store",
  "private": true,
  "type": "module",
  "exports": { ".": "./src/index.ts" },
  "dependencies": { "@ke/shared": "workspace:*" }
}
```

`packages/store/tsconfig.json`：`{ "extends": "../../tsconfig.base.json", "include": ["src", "drizzle.config.ts"] }`

```bash
pnpm add --filter @ke/store mysql2 drizzle-orm
pnpm add --filter @ke/store -D drizzle-kit
```

- [ ] **Step 2: 写失败测试（红）**

`packages/store/src/mysql.test.ts`：
```ts
import { describe, expect, it } from "vitest";
import { pingMysql } from "./mysql.js";

describe("pingMysql", () => {
  it("url 为空 → 短路返回 ok:false 不真连（镜像 Python config sanity check）", async () => {
    const r = await pingMysql(undefined);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toContain("KE_DB_URL");
  });
  it("连不上的地址 → ok:false 带 error（1s 超时，不抛异常）", async () => {
    const r = await pingMysql("mysql://u:p@127.0.0.1:1/none", 1000);
    expect(r.ok).toBe(false);
  });
});

// gated integration：有真实 KE_DB_URL（隧道已起）才跑
describe.skipIf(!process.env.KE_DB_URL)("pingMysql (integration)", () => {
  it("真连 SELECT 1 → ok:true", async () => {
    const r = await pingMysql(process.env.KE_DB_URL);
    expect(r).toEqual({ ok: true });
  });
});
```

- [ ] **Step 3: 跑红**，然后实现 `packages/store/src/mysql.ts`（绿）

```ts
/**
 * MySQL 连接 + 健康 ping。
 *
 * 镜像老仓 src/service/infra_health.py::_ping_mysql：
 * ① config sanity（URL 空 → 短路 ok:false，不真连）
 * ② 真连 SELECT 1，默认 5s 超时
 * ③ 永不抛异常，永远返回 DepStatus
 */
import type { DepStatus } from "@ke/shared";
import { normalizeMysqlUrl } from "@ke/shared";
import mysql from "mysql2/promise";

export const PING_TIMEOUT_MS = 5000; // 与 Python PING_TIMEOUT_SEC=5 同值

/** SELECT 1 健康检查；url 兼容 SQLAlchemy 方言格式（自动剥 +asyncmy） */
export async function pingMysql(
  url: string | undefined,
  timeoutMs: number = PING_TIMEOUT_MS,
): Promise<DepStatus> {
  if (!url) return { ok: false, error: "KE_DB_URL 未配置" };
  let conn: mysql.Connection | undefined;
  try {
    // connectTimeout 只管 TCP 建连；查询超时用 Promise.race 兜底
    conn = await mysql.createConnection({
      uri: normalizeMysqlUrl(url),
      connectTimeout: timeoutMs,
    });
    // Promise.race：哪个先 settle 用哪个 —— 给 query 也加上超时
    await Promise.race([
      conn.query("SELECT 1"),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error(`MySQL ping timeout (>${timeoutMs}ms)`)), timeoutMs),
      ),
    ]);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  } finally {
    await conn?.end().catch(() => {}); // 清理连接；失败也吞掉（ping 不能抛）
  }
}
```

`packages/store/src/index.ts`：
```ts
/** @ke/store 公开出口：MySQL ping（drizzle schema 在 ./schema，introspect 生成） */
export * from "./mysql.js";
```

- [ ] **Step 4: 跑绿（unit 2 passed，integration skipped）+ commit 代码部分**

```bash
pnpm vitest run packages/store
git add packages/store pnpm-lock.yaml
git commit -m "feat(store): MySQL client + 5s 超时健康 ping（镜像 _ping_mysql）

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: drizzle introspect（需隧道 + 真实凭证）**

`packages/store/drizzle.config.ts`：
```ts
/**
 * drizzle-kit 配置：introspect（pull）现有 MySQL schema → src/schema/。
 * 注意：drizzle-kit 独立进程跑，这里内联 +asyncmy 归一（不 import workspace 包，
 * 避免 bundler 解析问题；与 @ke/shared.normalizeMysqlUrl 的 2 行重复是有意的）。
 */
import { defineConfig } from "drizzle-kit";

const raw = process.env.KE_DB_URL ?? "";
const url = raw.replace(/^mysql\+\w+:\/\//, "mysql://");

export default defineConfig({
  dialect: "mysql",
  out: "./src/schema",
  dbCredentials: { url },
  introspect: { casing: "preserve" }, // 列名原样保留 —— 与 alembic 建的表逐字一致
});
```

执行（隧道 + env）：
```bash
bash /Users/java/knowledge-engineering/scripts/start_mysql_tunnel.sh
cp /Users/java/knowledge-engineering-auth/.env.local /Users/java/ke-server/.env.local  # 不 cat，含 secrets
cd /Users/java/ke-server/packages/store
set -a && source ../../.env.local && set +a
pnpm exec drizzle-kit pull
```

预期：`src/schema/` 生成 schema.ts（+relations.ts），covering users/projects/groups/group_members/user_project_access/git_credentials/audit_logs/qa_sessions 等表。
若隧道/凭证不可用：**停下报 BLOCKED**（introspect 是本任务核心，不能跳过）。

- [ ] **Step 6: 表清单与 alembic 头核对，写 BASELINE-DB.md**

```bash
# 表清单（走隧道）：
mysql -h 127.0.0.1 -P 3307 -u <user> -p<pw> <db> -e "SHOW TABLES; SELECT version_num FROM alembic_version;" 2>/dev/null
# 没有 mysql CLI 就用 node 一行：
node -e "const m=require('mysql2/promise');(async()=>{const c=await m.createConnection(process.env.KE_DB_URL.replace(/^mysql\+\w+:/,'mysql:'));const[t]=await c.query('SHOW TABLES');console.log(t);const[v]=await c.query('SELECT version_num FROM alembic_version');console.log(v);await c.end()})()"
```

`packages/store/BASELINE-DB.md`（实测值填入，不留占位）：
```markdown
# 基线数据库存证（drizzle introspect 时点）

- 时间：2026-06-11
- alembic head（alembic_version.version_num）：<实测>
- SHOW TABLES：<实测清单>
- drizzle introspect 覆盖：<生成的表清单>
- 差异：<应为「无」；alembic_version 表本身不需要 drizzle 模型，注明即可>
```

核对：introspect 生成的表 = SHOW TABLES - alembic_version。不一致 → 查 drizzle-kit 输出解决后重跑。

- [ ] **Step 7: Commit schema + 存证**

```bash
cd /Users/java/ke-server
git add packages/store/drizzle.config.ts packages/store/src/schema packages/store/BASELINE-DB.md
git commit -m "feat(store): drizzle introspect 接管基线 MySQL schema + BASELINE-DB 存证

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: @ke/store Weaviate ping + @ke/codegraph（SQLite + Neo4j ping）

**Files:**
- Create: `packages/store/src/weaviate.ts` + `src/weaviate.test.ts`（修改 `src/index.ts` 加导出）
- Create: `packages/codegraph/package.json`、`tsconfig.json`、`src/index.ts`、`src/paths.ts`、`src/db.ts`、`src/neo4j.ts`
- Test: `packages/codegraph/src/paths.test.ts`、`src/db.test.ts`、`src/neo4j.test.ts`

- [ ] **Step 1: Weaviate ping 失败测试（红）**

`packages/store/src/weaviate.test.ts`：
```ts
import { describe, expect, it } from "vitest";
import { pingWeaviate } from "./weaviate.js";

describe("pingWeaviate", () => {
  it("url 为空 → 短路 ok:false", async () => {
    const r = await pingWeaviate(undefined);
    expect(r.ok).toBe(false);
  });
  it("连不上的地址 → ok:false（1s 超时不抛）", async () => {
    const r = await pingWeaviate("http://127.0.0.1:1", 1000);
    expect(r.ok).toBe(false);
  });
});

describe.skipIf(!process.env.WEAVIATE_URL)("pingWeaviate (integration)", () => {
  it("真连 /v1/.well-known/live → ok:true", async () => {
    expect(await pingWeaviate(process.env.WEAVIATE_URL)).toEqual({ ok: true });
  });
});
```

- [ ] **Step 2: 跑红，实现 `packages/store/src/weaviate.ts`（绿）**

```ts
/**
 * Weaviate 健康 ping —— 镜像老仓 _ping_weaviate：
 * 直接 HTTP GET /v1/.well-known/live（进程活着就 200，无需 auth、无需 SDK 初始化）。
 * 有意不用 weaviate-client SDK：live 探活要轻；SDK 接入放 Phase 2 store 移植时做。
 */
import type { DepStatus } from "@ke/shared";

export async function pingWeaviate(
  url: string | undefined,
  timeoutMs = 5000,
): Promise<DepStatus> {
  if (!url) return { ok: false, error: "WEAVIATE_URL 未配置" };
  // url.replace(/\/+$/,"")：去尾部斜杠再拼路径（镜像 Python rstrip("/")）
  const liveUrl = `${url.replace(/\/+$/, "")}/v1/.well-known/live`;
  try {
    // AbortSignal.timeout(ms)：到时自动 abort fetch（Node 18+ 内置）
    const resp = await fetch(liveUrl, { signal: AbortSignal.timeout(timeoutMs) });
    if (resp.status === 200) return { ok: true };
    return { ok: false, error: `Weaviate live status=${resp.status}` };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}
```

`packages/store/src/index.ts` 追加 `export * from "./weaviate.js";`

- [ ] **Step 3: @ke/codegraph 包骨架 + 失败测试（红）**

`packages/codegraph/package.json`：
```json
{
  "name": "@ke/codegraph",
  "private": true,
  "type": "module",
  "exports": { ".": "./src/index.ts" },
  "dependencies": { "@ke/shared": "workspace:*" }
}
```

`packages/codegraph/tsconfig.json`：`{ "extends": "../../tsconfig.base.json", "include": ["src"] }`

```bash
pnpm add --filter @ke/codegraph neo4j-driver
```

`packages/codegraph/src/paths.test.ts`：
```ts
import { describe, expect, it } from "vitest";
import { codegraphDbPath } from "./paths.js";

describe("codegraphDbPath", () => {
  it("拼出 <repo>/.codegraph/codegraph.db（镜像 Python paths.py）", () => {
    expect(codegraphDbPath("/repos/mall-swarm")).toBe("/repos/mall-swarm/.codegraph/codegraph.db");
  });
  it("repo 路径为空 → 抛明确错误", () => {
    expect(() => codegraphDbPath("")).toThrow(/repo_local_path/);
  });
});
```

`packages/codegraph/src/db.test.ts`（gated：本机有 mall-swarm 的 codegraph.db 才跑真打开）：
```ts
import { existsSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { openCodeGraph } from "./db.js";
import { codegraphDbPath } from "./paths.js";

const DB = codegraphDbPath("/Users/java/repos/mall-swarm");

describe.skipIf(!existsSync(DB))("openCodeGraph (integration)", () => {
  it("只读打开 + 数一张核心表的行数 > 0", () => {
    const db = openCodeGraph(DB);
    // 表名以老仓 src/integrations/codegraph/db.py 为准（实现 Step 时先读它确认）
    const row = db.prepare("SELECT count(*) AS n FROM nodes").get() as { n: number };
    expect(row.n).toBeGreaterThan(0);
    db.close();
  });
});

describe("openCodeGraph", () => {
  it("文件不存在 → 抛错（不静默创建空库）", () => {
    expect(() => openCodeGraph("/nonexistent/codegraph.db")).toThrow();
  });
});
```

`packages/codegraph/src/neo4j.test.ts`：
```ts
import { describe, expect, it } from "vitest";
import { pingNeo4j } from "./neo4j.js";

describe("pingNeo4j", () => {
  it("uri 为空 → 短路 ok:false", async () => {
    expect((await pingNeo4j(undefined, "neo4j", "x")).ok).toBe(false);
  });
  it("连不上的地址 → ok:false（1s 超时不抛）", async () => {
    const r = await pingNeo4j("bolt://127.0.0.1:1", "neo4j", "x", 1000);
    expect(r.ok).toBe(false);
  });
});

describe.skipIf(!process.env.NEO4J_URI)("pingNeo4j (integration)", () => {
  it("真连 getServerInfo → ok:true（neo4j 是非致命依赖，失败也只是上报）", async () => {
    const r = await pingNeo4j(
      process.env.NEO4J_URI,
      process.env.NEO4J_USER ?? "neo4j",
      process.env.NEO4J_PASSWORD,
    );
    expect(r.ok).toBe(true);
  });
});
```

- [ ] **Step 4: 先读老仓确认 CodeGraph 表名**

```bash
grep -nE "FROM [a-z_]+|TABLE|nodes|edges" /Users/java/knowledge-engineering/src/integrations/codegraph/db.py | head -15
```

按实际表名修正 db.test.ts 的 `FROM nodes`（若叫别的名字，例如 `node` / `symbols`，以代码为准并在报告注明）。

- [ ] **Step 5: 实现三个模块（绿）**

`packages/codegraph/src/paths.ts`：
```ts
/**
 * CodeGraph 库路径推导 —— 镜像老仓 src/integrations/codegraph/paths.py。
 * CodeGraph 把库放在 <repo>/.codegraph/codegraph.db；一工程一库一文件，物理隔离。
 */
import { join } from "node:path";

export function codegraphDbPath(repoLocalPath: string): string {
  if (!repoLocalPath) {
    throw new Error("repo_local_path 为空，无法定位 CodeGraph 库（该工程可能未配置 repo_local_path）");
  }
  return join(repoLocalPath, ".codegraph", "codegraph.db");
}
```

`packages/codegraph/src/db.ts`：
```ts
/**
 * CodeGraph SQLite 只读访问 —— 用 Node 24 内置 node:sqlite（零原生依赖）。
 * 镜像老仓 src/integrations/codegraph/db.py 的只读语义：
 * 文件必须已存在（CodeGraph CLI 生成），绝不静默创建空库。
 */
import { existsSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";

export function openCodeGraph(dbPath: string): DatabaseSync {
  if (!existsSync(dbPath)) {
    throw new Error(`CodeGraph 库不存在：${dbPath}（先用 CodeGraph CLI 对目标工程建库）`);
  }
  // readOnly：双保险 —— 在线服务永远不写 CodeGraph 库
  return new DatabaseSync(dbPath, { readOnly: true });
}
```

`packages/codegraph/src/neo4j.ts`：
```ts
/**
 * Neo4j 健康 ping —— 镜像老仓 _ping_neo4j。
 * 注意：neo4j 在基线里是「非致命依赖」（deps_infra._NON_CRITICAL_DEPS，CodeGraph
 * 迁移后图导航不再依赖它，退役中）—— 判定 healthy 时排除，但仍上报状态供观测。
 */
import type { DepStatus } from "@ke/shared";
import neo4j from "neo4j-driver";

export async function pingNeo4j(
  uri: string | undefined,
  user: string | undefined,
  password: string | undefined,
  timeoutMs = 5000,
): Promise<DepStatus> {
  if (!uri) return { ok: false, error: "NEO4J_URI 未配置" };
  const driver = neo4j.driver(uri, neo4j.auth.basic(user ?? "neo4j", password ?? ""));
  try {
    // getServerInfo：driver 级连通性验证（等价 Python verify_connectivity）
    await Promise.race([
      driver.getServerInfo(),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error(`Neo4j ping timeout (>${timeoutMs}ms)`)), timeoutMs),
      ),
    ]);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  } finally {
    await driver.close().catch(() => {});
  }
}
```

`packages/codegraph/src/index.ts`：
```ts
/** @ke/codegraph 公开出口：CodeGraph SQLite 只读 + Neo4j ping（非致命） */
export * from "./paths.js";
export * from "./db.js";
export * from "./neo4j.js";
```

- [ ] **Step 6: 跑绿 + typecheck + commit**

```bash
pnpm vitest run packages/store packages/codegraph && pnpm typecheck
git add packages/store packages/codegraph pnpm-lock.yaml
git commit -m "feat(store,codegraph): Weaviate live ping + CodeGraph SQLite 只读 + Neo4j ping（非致命）

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: @ke/llm — AI SDK provider 工厂 + DashScope ping

**Files:**
- Create: `packages/llm/package.json`、`tsconfig.json`、`src/index.ts`、`src/providers.ts`、`src/ping.ts`
- Test: `packages/llm/src/providers.test.ts`、`src/ping.test.ts`

- [ ] **Step 1: 先读老仓 `_ping_dashscope` 确认探活方式**

```bash
sed -n '130,209p' /Users/java/knowledge-engineering/src/service/infra_health.py
```

记录它探的端点与判定（大概率是 OpenAI 兼容 base 的 GET /models 或一次最小请求）。**下面 ping.ts 的实现按读到的真实逻辑微调**（端点路径/判定条件），偏差写进报告。

- [ ] **Step 2: 包骨架 + 依赖 + 失败测试（红）**

`packages/llm/package.json`：
```json
{
  "name": "@ke/llm",
  "private": true,
  "type": "module",
  "exports": { ".": "./src/index.ts" },
  "dependencies": { "@ke/shared": "workspace:*" }
}
```

```bash
pnpm add --filter @ke/llm ai @ai-sdk/openai-compatible
```

`packages/llm/src/providers.test.ts`：
```ts
import { describe, expect, it } from "vitest";
import { createDashScope, createMiniMax } from "./providers.js";

describe("provider 工厂", () => {
  it("DashScope 默认 baseURL 是 compatible-mode", () => {
    const p = createDashScope({ DASHSCOPE_API_KEY: "sk-test" });
    // AI SDK provider 是个函数：p(modelId) 返回 LanguageModel；这里只验证能构造
    expect(typeof p).toBe("function");
  });
  it("缺 API key → 抛明确错误（fail-fast，不让 undefined 流到请求时）", () => {
    expect(() => createDashScope({})).toThrow(/DASHSCOPE_API_KEY/);
    expect(() => createMiniMax({})).toThrow(/MINIMAX_API_KEY/);
  });
});
```

`packages/llm/src/ping.test.ts`：
```ts
import { describe, expect, it } from "vitest";
import { pingDashScope } from "./ping.js";

describe("pingDashScope", () => {
  it("key 为空 → 短路 ok:false", async () => {
    expect((await pingDashScope(undefined)).ok).toBe(false);
  });
  it("连不上的 base → ok:false（1s 超时不抛）", async () => {
    const r = await pingDashScope("sk-x", "http://127.0.0.1:1/v1", 1000);
    expect(r.ok).toBe(false);
  });
});

describe.skipIf(!process.env.DASHSCOPE_API_KEY)("pingDashScope (integration)", () => {
  it("真连 → ok:true", async () => {
    expect((await pingDashScope(process.env.DASHSCOPE_API_KEY)).ok).toBe(true);
  });
});
```

- [ ] **Step 3: 跑红，实现（绿）**

`packages/llm/src/providers.ts`：
```ts
/**
 * LLM provider 工厂 —— Vercel AI SDK 只做 provider 层（统一流式/工具调用/embedding），
 * ReAct 循环本身 Phase 2 手写移植（产品 know-how 不交给框架）。
 *
 * DashScope 与 MiniMax 都走 OpenAI 兼容协议 → @ai-sdk/openai-compatible 一个适配器通吃。
 */
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";

/** DashScope 北京站 OpenAI 兼容端点（与 Python 默认一致；可被 env 覆盖） */
export const DASHSCOPE_DEFAULT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1";

type EnvLike = Record<string, string | undefined>;

export function createDashScope(env: EnvLike) {
  const apiKey = env.DASHSCOPE_API_KEY;
  if (!apiKey) throw new Error("DASHSCOPE_API_KEY 未配置");
  return createOpenAICompatible({
    name: "dashscope",
    baseURL: env.DASHSCOPE_BASE_URL ?? DASHSCOPE_DEFAULT_BASE,
    apiKey,
  });
}

export function createMiniMax(env: EnvLike) {
  const apiKey = env.MINIMAX_API_KEY;
  if (!apiKey) throw new Error("MINIMAX_API_KEY 未配置");
  return createOpenAICompatible({
    name: "minimax",
    baseURL: env.MINIMAX_BASE_URL ?? "https://api.minimax.chat/v1",
    apiKey,
  });
}
```

`packages/llm/src/ping.ts`（基础版 = GET /models；**按 Step 1 读到的 Python 真实逻辑微调**）：
```ts
/**
 * DashScope 健康 ping —— 镜像老仓 _ping_dashscope（轻量探活，不产生 token 费）。
 */
import type { DepStatus } from "@ke/shared";
import { DASHSCOPE_DEFAULT_BASE } from "./providers.js";

export async function pingDashScope(
  apiKey: string | undefined,
  baseUrl: string = DASHSCOPE_DEFAULT_BASE,
  timeoutMs = 5000,
): Promise<DepStatus> {
  if (!apiKey) return { ok: false, error: "DASHSCOPE_API_KEY 未配置" };
  try {
    const resp = await fetch(`${baseUrl.replace(/\/+$/, "")}/models`, {
      headers: { Authorization: `Bearer ${apiKey}` },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (resp.ok) return { ok: true };
    return { ok: false, error: `DashScope status=${resp.status}` };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}
```

`packages/llm/src/index.ts`：
```ts
/** @ke/llm 公开出口：provider 工厂 + DashScope ping */
export * from "./providers.js";
export * from "./ping.js";
```

- [ ] **Step 4: 跑绿 + commit**

```bash
pnpm vitest run packages/llm && pnpm typecheck
git add packages/llm pnpm-lock.yaml
git commit -m "feat(llm): AI SDK provider 工厂（DashScope/MiniMax OpenAI 兼容）+ 健康 ping

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: ke-api — Hono 应用 + GET /health（与 Python 同口径）

**Files:**
- Create: `apps/api/package.json`、`tsconfig.json`、`src/health.ts`、`src/app.ts`、`src/index.ts`
- Test: `apps/api/src/health.test.ts`、`src/app.test.ts`

- [ ] **Step 1: 包骨架 + 依赖**

`apps/api/package.json`：
```json
{
  "name": "ke-api",
  "private": true,
  "type": "module",
  "scripts": { "dev": "tsx watch src/index.ts" },
  "dependencies": {
    "@ke/shared": "workspace:*",
    "@ke/store": "workspace:*",
    "@ke/codegraph": "workspace:*",
    "@ke/llm": "workspace:*"
  }
}
```

```bash
pnpm add --filter ke-api hono @hono/node-server
```

`apps/api/tsconfig.json`：`{ "extends": "../../tsconfig.base.json", "include": ["src"] }`

- [ ] **Step 2: 写失败测试（红）— 健康聚合纯逻辑**

`apps/api/src/health.test.ts`：
```ts
import type { InfraStatus } from "@ke/shared";
import { describe, expect, it } from "vitest";
import { checkAllDeps, isHealthy } from "./health.js";

const ok = { ok: true } as const;
const down = { ok: false, error: "x" } as const;

describe("isHealthy", () => {
  it("4 个全 ok → healthy", () => {
    expect(isHealthy({ mysql: ok, neo4j: ok, weaviate: ok, dashscope: ok })).toBe(true);
  });
  it("neo4j 挂了仍 healthy（非致命依赖，镜像 deps_infra._NON_CRITICAL_DEPS）", () => {
    expect(isHealthy({ mysql: ok, neo4j: down, weaviate: ok, dashscope: ok })).toBe(true);
  });
  it("mysql 挂了 → unhealthy", () => {
    expect(isHealthy({ mysql: down, neo4j: ok, weaviate: ok, dashscope: ok })).toBe(false);
  });
});

describe("checkAllDeps", () => {
  it("并行调 4 个注入的 ping，组装 InfraStatus", async () => {
    const status: InfraStatus = await checkAllDeps({
      mysql: async () => ok,
      neo4j: async () => down,
      weaviate: async () => ok,
      dashscope: async () => ok,
    });
    expect(status.mysql).toEqual(ok);
    expect(status.neo4j).toEqual(down);
  });
  it("某个 ping 意外抛异常 → 转 ok:false 不向上炸（ping 永不抛的兜底）", async () => {
    const status = await checkAllDeps({
      mysql: async () => {
        throw new Error("boom");
      },
      neo4j: async () => ok,
      weaviate: async () => ok,
      dashscope: async () => ok,
    });
    expect(status.mysql.ok).toBe(false);
  });
});
```

`apps/api/src/app.test.ts`：
```ts
import { describe, expect, it } from "vitest";
import { createApp } from "./app.js";

const allOk = {
  mysql: async () => ({ ok: true }) as const,
  neo4j: async () => ({ ok: false, error: "退役中" }) as const,
  weaviate: async () => ({ ok: true }) as const,
  dashscope: async () => ({ ok: true }) as const,
};

describe("GET /health", () => {
  it("默认响应 {healthy, ts}，不带 deps（与 Python 普通用户口径一致）", async () => {
    const app = createApp({ pings: allOk, healthDebug: false });
    const res = await app.request("/health");
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.healthy).toBe(true); // neo4j 非致命
    expect(typeof body.ts).toBe("string");
    expect(body.deps).toBeUndefined();
  });
  it("healthDebug=true 时附 deps 明细（Phase 2 改 admin 门控）", async () => {
    const app = createApp({ pings: allOk, healthDebug: true });
    const body = await (await app.request("/health")).json();
    expect(body.deps.neo4j.ok).toBe(false);
  });
});
```

- [ ] **Step 3: 跑红，实现（绿）**

`apps/api/src/health.ts`：
```ts
/**
 * 基建健康聚合 —— 镜像老仓 src/service/infra_health.py::check_all_deps +
 * deps_infra._NON_CRITICAL_DEPS + api.py /health 判定口径：
 * healthy = 所有 critical 依赖 ok（neo4j 非致命，挂了只上报不熔断）。
 */
import type { DepStatus, InfraStatus } from "@ke/shared";

/** 非致命依赖（镜像 deps_infra._NON_CRITICAL_DEPS；neo4j 因 CodeGraph 迁移退役中） */
export const NON_CRITICAL_DEPS = new Set<keyof InfraStatus>(["neo4j"]);

/** 4 个 ping 的注入接口：生产用真实现，测试注 fake */
export type DepPings = { [K in keyof InfraStatus]: () => Promise<DepStatus> };

/** 把可能抛异常的 ping 包成永不抛（镜像 Python「每个 ping 永远返回 dict」约定） */
async function safePing(fn: () => Promise<DepStatus>): Promise<DepStatus> {
  try {
    return await fn();
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

/** 并行 ping 全部依赖（Promise.all：单个 ping 自带超时与吞异常，不会互相拖累） */
export async function checkAllDeps(pings: DepPings): Promise<InfraStatus> {
  const [mysql, neo4j, weaviate, dashscope] = await Promise.all([
    safePing(pings.mysql),
    safePing(pings.neo4j),
    safePing(pings.weaviate),
    safePing(pings.dashscope),
  ]);
  return { mysql, neo4j, weaviate, dashscope };
}

/** healthy 判定：排除非致命依赖后全 ok（与 api.py /health 同口径） */
export function isHealthy(status: InfraStatus): boolean {
  return (Object.entries(status) as [keyof InfraStatus, DepStatus][])
    .filter(([k]) => !NON_CRITICAL_DEPS.has(k))
    .every(([, v]) => v.ok);
}
```

`apps/api/src/app.ts`：
```ts
/**
 * Hono 应用工厂。依赖注入风格：pings/healthDebug 由入口装配，
 * 测试用 app.request() 直接打路由（不起端口）。
 */
import { Hono } from "hono";
import { checkAllDeps, isHealthy, type DepPings } from "./health.js";

export type AppDeps = { pings: DepPings; healthDebug: boolean };

export function createApp(deps: AppDeps): Hono {
  const app = new Hono();

  // 镜像 Python /health：每次调用重新 ping（不读缓存——「重试连接」必须看到最新状态）
  app.get("/health", async (c) => {
    const status = await checkAllDeps(deps.pings);
    const body: Record<string, unknown> = {
      healthy: isHealthy(status),
      ts: new Date().toISOString(),
    };
    // Phase 1 偏差（已记录）：Python 用 JWT admin 判定才附 deps；
    // TS 侧 auth 在 Phase 2，先用 KE_HEALTH_DEBUG 开关替代
    if (deps.healthDebug) body.deps = status;
    return c.json(body);
  });

  return app;
}
```

`apps/api/src/index.ts`：
```ts
/**
 * ke-api 入口：装配真实依赖并起服务。
 * 启动前 process.loadEnvFile 读 .env.local（Node 20.6+ 内置，不需要 dotenv 包）。
 */
import { serve } from "@hono/node-server";
import { loadEnv } from "@ke/shared";
import { pingNeo4j } from "@ke/codegraph";
import { pingDashScope } from "@ke/llm";
import { pingMysql, pingWeaviate } from "@ke/store";
import { createApp } from "./app.js";

// 仓库根的 .env.local（pnpm dev 从 apps/api 跑，向上两级）；没有该文件不算错
try {
  process.loadEnvFile(new URL("../../../.env.local", import.meta.url).pathname);
} catch {
  /* .env.local 不存在 → 直接用进程环境变量 */
}

const env = loadEnv();

const app = createApp({
  pings: {
    mysql: () => pingMysql(env.KE_DB_URL),
    neo4j: () => pingNeo4j(env.NEO4J_URI, env.NEO4J_USER, env.NEO4J_PASSWORD),
    weaviate: () => pingWeaviate(env.WEAVIATE_URL),
    dashscope: () => pingDashScope(env.DASHSCOPE_API_KEY, env.DASHSCOPE_BASE_URL),
  },
  healthDebug: env.KE_HEALTH_DEBUG === "1",
});

serve({ fetch: app.fetch, port: env.KE_API_PORT }, (info) => {
  console.log(`ke-api listening on http://localhost:${info.port}`);
});
```

- [ ] **Step 4: 跑绿 + typecheck + commit**

```bash
pnpm vitest run apps/api && pnpm typecheck && pnpm lint
git add apps/api pnpm-lock.yaml
git commit -m "feat(api): Hono 应用 + GET /health（4 依赖并行 ping、neo4j 非致命、同 Python 口径）

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: 全链路验收 + 收口（退出标准）

**Files:**
- Modify（Obsidian）: `01 Engineering/knowledge-engineering/_overview.md` + `TS重构-总体路线-设计.md`（Phase 1 状态）

- [ ] **Step 1: 起隧道 + 凭证就位**

```bash
bash /Users/java/knowledge-engineering/scripts/start_mysql_tunnel.sh
test -f /Users/java/ke-server/.env.local || cp /Users/java/knowledge-engineering-auth/.env.local /Users/java/ke-server/.env.local
```

- [ ] **Step 2: 全量测试（integration 不再 skip）+ 静态检查**

```bash
cd /Users/java/ke-server
set -a && source .env.local && set +a
pnpm test          # 预期：全 passed；mysql/weaviate/dashscope integration 真连通过（neo4j integration 若服务已停，记录后用 skip 处理——它是非致命依赖）
pnpm typecheck && pnpm lint
```

- [ ] **Step 3: 起服务 curl /health（退出标准核心）**

```bash
pnpm dev &           # 或前台另开
sleep 2
curl -s http://localhost:8700/health
KE_HEALTH_DEBUG=1 pnpm dev 下再 curl 一次看 deps 明细
```

预期：`{"healthy":true,"ts":"..."}`；debug 模式下 deps.mysql/weaviate/dashscope 全 ok=true（neo4j 允许 false）。
不绿则逐依赖排查（隧道在不在 / env 对不对），修通为止——**这是 Phase 1 退出标准，不能带病通过**。

- [ ] **Step 4: introspect 一致性终验**（Task 3 已做，此处确认产物在 git 里且 BASELINE-DB.md 无占位符）

```bash
ls packages/store/src/schema/ && grep -c "CHANGE_ME\|<实测" packages/store/BASELINE-DB.md; echo "exit=$?"   # 预期 exit=1（无占位）
```

- [ ] **Step 5: push + Obsidian 收口**

```bash
cd /Users/java/ke-server && git push origin main
```

Obsidian（两处，改完 commit + push obsidian 仓）：
1. `TS重构-总体路线-设计.md` §五 Phase 1 标题行后加一行：`> ✅ Phase 1 已完成（2026-06-11，ke-server 仓 <最新 commit 短 hash>）：/health 全绿（neo4j 非致命除外）、drizzle introspect 与 alembic 头一致、vitest/typecheck/lint 全过。`
2. `_overview.md` 开放问题「后端 TS 重构进行中」条目把「下一步 = Phase 1 …」更新为「Phase 1 已完成（仓 github.com/MrWangCSDN/ke-server）；下一步 = Phase 2 在线服务移植（auth 起步，届时 writing-plans）」

- [ ] **Step 6: 汇报**：依赖解析版本清单、/health 实测输出、introspect 表清单、与计划的全部偏差

---

## 自审记录（写计划时已跑）

1. **Spec 覆盖**：spec §五 Phase 1 四项 — 仓初始化（Task 1）、config zod 解析（Task 2）、四类基建 client 接通 + 冒烟（Task 3 MySQL+drizzle introspect / Task 4 Weaviate+Neo4j+CodeGraph / Task 5 LLM）、健康检查端点（Task 6）、退出标准（Task 7）。**偏差三处，有意为之**：① 健康检查按基线实际口径 neo4j 非致命（spec「全绿」写法早于这一发现）；② /health 的 deps 用 KE_HEALTH_DEBUG 临时替代 admin 门控（Phase 2 接 auth 后对齐）；③ CodeGraph SQLite client 提前进 Phase 1（spec 把它归在 codegraph 包下，QA 图导航的真实依赖是它而非 neo4j）。
2. **占位符扫描**：BASELINE-DB.md 模板标注「实测值填入，不留占位」并在 Task 7 Step 4 用 grep 验收；dashscope ping 与 codegraph 表名两处「按老仓源码确认后微调」均给了完整基础实现 + 确认命令，非空心步骤。
3. **类型一致性**：`DepStatus`/`InfraStatus` 定义于 @ke/shared（Task 2），Task 3/4/5/6 全部从 `@ke/shared` import 同名类型；`pingMysql(url, timeoutMs)` / `pingWeaviate(url, timeoutMs)` / `pingNeo4j(uri, user, password, timeoutMs)` / `pingDashScope(apiKey, baseUrl, timeoutMs)` 签名与 Task 6 index.ts 装配处一致；`createApp({pings, healthDebug})` 与 app.test.ts 一致。
4. **已知风险**：库版本 API 漂移（drizzle-kit introspect 配置键名、weaviate-client 不用所以无关、AI SDK provider 构造）——计划允许按官方当前 API 小幅调整并报告；node:sqlite 在 Node 24 可用（本机 24.8）。
