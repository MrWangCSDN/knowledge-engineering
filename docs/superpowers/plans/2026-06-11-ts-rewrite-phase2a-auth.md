# TS 重构 Phase 2a：auth 模块移植 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把老仓 auth 全链路（5 条 `/auth/*` 路由 + Bearer 中间件 + bcrypt/JWT/cookie 兼容）移植到 ke-server，跨语言 golden 测试锁死「老 hash 能验、老 token 能用」。

**Architecture:** Phase 2 整体按模块拆子计划，本计划是 **P2a（auth 先行）**——它是所有后续路由的地基，也是兼容性风险最集中的面。代码放 `apps/api/src/auth/`（security/middleware/schemas/routes 四件）+ `packages/store`（db 工厂 + users repo）。行为以老仓为唯一规范：`src/service/auth_security.py` / `auth_router.py` / `auth_dependencies.py` / `auth_schemas.py`（执行时并排对照）。

**Tech Stack:** 已有底座（TS 6 / Hono 4.12 / drizzle 0.45 / zod 4）+ 新增 `jose`（JWT，对端 python-jose HS256）+ `bcryptjs`（对端 passlib/bcrypt `$2b$` cost 12）+ `@hono/zod-openapi`（路由即 schema → openapi.json 给前端 codegen）。

---

## 行为规范速查（老仓实读提取，2026-06-11；执行时仍以源码为准）

### 路由（5 条；错误体一律 FastAPI 形状 `{"detail": <string>}`）

| 路由 | 行为要点 |
|---|---|
| `POST /auth/login` | body `{username(3-255, 可为邮箱), password(8-128), remember_me=false}`。按 `username == body.username OR email == body.username` 查 users。**防枚举**：用户不存在 / is_active=false / 密码错 → 一律 401 `用户名或密码不正确`。**锁定**：`locked_until > now` → 423 `账号已锁定，请 {N} 分钟后重试`（N=剩余分钟向上取整）。**密码错**：failed_attempts+1，≥5 → locked_until=now+15min，**先落库再返回 401**。**成功**：failed_attempts=0、locked_until=NULL 落库；签 access+refresh；`Set-Cookie refresh_token`（httponly、secure=env、samesite=env(strict)、domain=env(空略)、path=env(/api/auth)、max_age=refresh TTL）；返回 `{access_token, token_type:"bearer", expires_in:3600}` |
| `POST /auth/refresh` | **CSRF**：`origin` 与 `host` 头都存在且 `host` 不是 `origin` 的子串 → 403 `Cross-origin refresh forbidden`。无 cookie → 401 `Missing refresh cookie`。decode 失败或 `type!="refresh"` → 401 `Invalid refresh token`。`sub` 非整数 → 401 `Invalid token payload`。用户不存在/inactive → 401 `User not found or inactive`。成功 → `{access_token, expires_in}`（不轮换 refresh） |
| `GET /auth/me` | Bearer 中间件 → `{id, email, username, is_admin, created_at, preferred_model}` |
| `PATCH /auth/me/model` | body `{model_id(1-64)}`；白名单外 → 400 `` 不支持的模型 id: '<id>' ``（注意 Python repr 单引号）；成功更新 preferred_model 返回 MeResponse |
| `POST /auth/logout` | 删 cookie（同 path、samesite strict）→ `{ok:true}` |

### JWT（python-jose HS256，KE_JWT_SECRET ≥32 字符强制）

- access：`{sub: String(userId), username, type:"access", iat, exp=iat+KE_JWT_ACCESS_TTL_MIN*60}`（默认 60 分钟）
- refresh：`{sub, type:"refresh", jti: 16字节base64url, iat, exp=+1天 或 remember_me 时 +7天}`
- decode：签名错/过期 → null（不抛）

### Bearer 中间件（get_current_user）

无 token / decode 失败 / `type!="access"` / sub 非整数 / 用户不存在 / inactive → 401 `{"detail":"Not authenticated"}` + 响应头 `WWW-Authenticate: Bearer`。admin 守卫：`is_admin=false` → 403 `{"detail":"Admin only"}`。

### bcrypt

passlib `CryptContext(schemes=["bcrypt"], bcrypt__rounds=12)` → `$2b$12$...`（60 字符）。verify 任何异常 → false。

### 模型白名单（llm_factory）

`SUPPORTED_MODELS = [{id:"qwen-plus",label:"Qwen-Plus",vendor:"DashScope"},{id:"MiniMax-M2",label:"MiniMax-M2",vendor:"MiniMax"}]`；`DEFAULT_MODEL_ID = env KE_DEFAULT_MODEL ?? "qwen-plus"`。

### 已知允许差异（不算违规，记录在案）

1. 请求体校验失败：FastAPI 返 422 `{"detail":[{loc,msg,type}...]}`，zod-openapi 默认 400 + zod issues。前端登录表单自带客户端校验，正常流程打不到；记录为已知差异，Phase 3 前端回归时若有依赖再对齐。
2. `created_at` 序列化：pydantic 输出 naive ISO（无 Z 无毫秒），TS Date JSON 化带 `.000Z`。前端 `new Date()` 两者都能解析；记录为已知差异。
3. MySQL `datetime` 全程 **naive UTC**（Python 用 `now.replace(tzinfo=None)` 比较）。TS 侧统一用「UTC naive 字符串」读写比较（见 Task 1 helper）。

---

### Task 1: @ke/store — db 工厂 + users repo

**Files:**
- Create: `packages/store/src/db.ts`、`packages/store/src/users.ts`
- Modify: `packages/store/src/index.ts`（加导出）
- Test: `packages/store/src/users.itest.ts`（gated integration，`KE_DB_IT=1` 才跑）

- [ ] **Step 1: 先看 introspect 产物的 users 表字段与 datetime mode**

```bash
grep -n "export const users" -A 25 /Users/java/ke-server/packages/store/src/schema/schema.ts
```

记录：列名（id/username/email/hashed_password/is_active/is_admin/failed_attempts/locked_until/preferred_model/created_at/updated_at…以实际为准）与 `datetime(...{ mode: ... })`。**下面代码假设 mode:'string'（drizzle-kit pull 默认）；若实际是 Date mode，按实际调整 helper 与比较逻辑并报告。**

- [ ] **Step 2: 写失败测试（红）— gated integration（写操作只动自建测试用户）**

`packages/store/src/users.itest.ts`：
```ts
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createDb, type KeDb } from "./db.js";
import {
  deleteUserById, findUserByLogin, insertUser,
  recordLoginFailure, recordLoginSuccess, toMysqlDatetime, updatePreferredModel,
} from "./users.js";

// 双闸门：KE_DB_IT=1（显式同意写库）+ KE_DB_URL（隧道凭证）。库是共享的，
// 测试只允许动自己创建的 __ke_ts_it__ 前缀用户，afterAll 必删。
const GATE = process.env.KE_DB_IT !== "1" || !process.env.KE_DB_URL;
const T_USERNAME = "__ke_ts_it_user__";

describe.skipIf(GATE)("users repo (integration)", () => {
  let db: KeDb;
  let uid: number;

  beforeAll(async () => {
    db = createDb(process.env.KE_DB_URL!);
    await deleteUserById(db, (await findUserByLogin(db, T_USERNAME))?.id ?? -1); // 残留清理
    uid = await insertUser(db, {
      username: T_USERNAME,
      email: "__ke_ts_it__@test.local",
      hashedPassword: "$2b$12$placeholderplaceholderplaceholderplaceholderplacehol",
    });
  });

  afterAll(async () => {
    await deleteUserById(db, uid);
    await db.$client.end(); // 关连接池，防 vitest 挂着不退出
  });

  it("findUserByLogin 按 username 命中", async () => {
    const u = await findUserByLogin(db, T_USERNAME);
    expect(u?.id).toBe(uid);
    expect(u?.isActive).toBe(true);
  });

  it("findUserByLogin 按 email 命中同一行", async () => {
    const u = await findUserByLogin(db, "__ke_ts_it__@test.local");
    expect(u?.id).toBe(uid);
  });

  it("recordLoginFailure 累加计数并可设锁", async () => {
    const lockUntil = toMysqlDatetime(new Date(Date.now() + 15 * 60_000));
    await recordLoginFailure(db, uid, { failedAttempts: 5, lockedUntil: lockUntil });
    const u = await findUserByLogin(db, T_USERNAME);
    expect(u?.failedAttempts).toBe(5);
    expect(u?.lockedUntil).toBeTruthy();
  });

  it("recordLoginSuccess 清零计数与锁", async () => {
    await recordLoginSuccess(db, uid);
    const u = await findUserByLogin(db, T_USERNAME);
    expect(u?.failedAttempts).toBe(0);
    expect(u?.lockedUntil).toBeNull();
  });

  it("updatePreferredModel 写入偏好", async () => {
    await updatePreferredModel(db, uid, "qwen-plus");
    expect((await findUserByLogin(db, T_USERNAME))?.preferredModel).toBe("qwen-plus");
  });
});

describe("toMysqlDatetime", () => {
  it("Date → 'YYYY-MM-DD HH:MM:SS' UTC naive（对齐 Python replace(tzinfo=None)）", () => {
    expect(toMysqlDatetime(new Date("2026-06-11T03:04:05.678Z"))).toBe("2026-06-11 03:04:05");
  });
});
```

注意：`.itest.ts` 后缀已被根 vitest include 覆盖（glob 是 `*.test.ts`——**不匹配**）。两种处理任选其一并报告：① 文件名就用 `users.test.ts`（gate 已保证默认 skip）；② 根 vitest include 增加 `**/*.itest.ts`。推荐 ①，少一处配置。

- [ ] **Step 3: 跑红**，然后实现

`packages/store/src/db.ts`：
```ts
/**
 * drizzle db 工厂 —— mysql2 连接池 + introspect schema 绑定。
 * 单例由 apps/api 入口持有；测试各自 createDb 并自行关闭。
 */
import { normalizeMysqlUrl } from "@ke/shared";
import { drizzle } from "drizzle-orm/mysql2";
import mysql from "mysql2/promise";
import * as schema from "./schema/schema.js";

export type KeDb = ReturnType<typeof createDb>;

export function createDb(url: string) {
  // createPool：连接池（并发请求复用 TCP）；uri 先剥 +asyncmy 方言
  const pool = mysql.createPool({ uri: normalizeMysqlUrl(url), connectionLimit: 10 });
  // casing 不设：列名走 schema.ts 里 introspect 的原样定义
  return drizzle(pool, { schema, mode: "default" });
}
```

`packages/store/src/users.ts`（字段名以 Step 1 实测 schema 为准微调）：
```ts
/**
 * users 表 repo —— 镜像老仓 auth_router/auth_dependencies 的查询面。
 * 全部函数接收 db 参数（注入式），不持全局状态。
 */
import { eq, or } from "drizzle-orm";
import type { KeDb } from "./db.js";
import { users } from "./schema/schema.js";

/** Date → MySQL DATETIME 字符串（UTC naive，对齐 Python now.replace(tzinfo=None)） */
export function toMysqlDatetime(d: Date): string {
  return d.toISOString().slice(0, 19).replace("T", " ");
}

export type UserRow = typeof users.$inferSelect;

/** login 入参既可能是 username 也可能是 email（老仓 or_ 查询） */
export async function findUserByLogin(db: KeDb, login: string): Promise<UserRow | undefined> {
  const rows = await db.select().from(users)
    .where(or(eq(users.username, login), eq(users.email, login))).limit(1);
  return rows[0];
}

export async function findUserById(db: KeDb, id: number): Promise<UserRow | undefined> {
  const rows = await db.select().from(users).where(eq(users.id, id)).limit(1);
  return rows[0];
}

/** 密码错：累加失败计数，达到阈值时同时写 locked_until（一次 UPDATE，老仓是同事务两字段） */
export async function recordLoginFailure(
  db: KeDb, id: number,
  v: { failedAttempts: number; lockedUntil: string | null },
): Promise<void> {
  await db.update(users)
    .set({ failedAttempts: v.failedAttempts, lockedUntil: v.lockedUntil })
    .where(eq(users.id, id));
}

/** 登录成功：清零计数与锁 */
export async function recordLoginSuccess(db: KeDb, id: number): Promise<void> {
  await db.update(users).set({ failedAttempts: 0, lockedUntil: null }).where(eq(users.id, id));
}

export async function updatePreferredModel(db: KeDb, id: number, modelId: string): Promise<void> {
  await db.update(users).set({ preferredModel: modelId }).where(eq(users.id, id));
}

/** 仅供测试/管理脚本：建用户（最小字段集） */
export async function insertUser(
  db: KeDb,
  v: { username: string; email: string; hashedPassword: string; isAdmin?: boolean },
): Promise<number> {
  const r = await db.insert(users).values({
    username: v.username, email: v.email, hashedPassword: v.hashedPassword,
    isActive: true, isAdmin: v.isAdmin ?? false, failedAttempts: 0,
  }).$returningId();
  return r[0]!.id;
}

export async function deleteUserById(db: KeDb, id: number): Promise<void> {
  if (id < 0) return; // 残留清理路径的容错
  await db.delete(users).where(eq(users.id, id));
}
```

注意：introspect 的列属性名可能是 snake_case（casing preserve）——例如 `users.hashed_password` 而非 `hashedPassword`。**以 schema.ts 实际导出为准统一全文件**，测试里的属性同步。

- [ ] **Step 4: 跑绿（无 env 时 integration skip、toMysqlDatetime 单测过）+ 三门禁 + commit**

```bash
cd /Users/java/ke-server && pnpm vitest run packages/store && pnpm typecheck && pnpm lint
git add packages/store && git commit -m "feat(store): db 工厂 + users repo（注入式，gated 集成测试只动自建测试用户）

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

（隧道可用时再跑一次 `KE_DB_IT=1` 真集成，结果写报告。）

---

### Task 2: auth security（jose + bcryptjs）+ shared env 扩展

**Files:**
- Modify: `packages/shared/src/env.ts`（EnvSchema 增 JWT/cookie 字段）+ `packages/shared/src/env.test.ts`
- Create: `apps/api/src/auth/security.ts`
- Test: `apps/api/src/auth/security.test.ts`

- [ ] **Step 1: 装依赖**

```bash
pnpm add --filter ke-api jose bcryptjs
pnpm add --filter ke-api -D @types/bcryptjs
```

（bcryptjs v3 起或自带类型——若 @types 安装提示 deprecated/stub，去掉 devDep 并报告。）

- [ ] **Step 2: shared env 扩展（先红后绿）**

env.test.ts 追加：
```ts
describe("loadEnv auth 字段", () => {
  it("JWT/cookie 默认值", () => {
    const env = loadEnv({});
    expect(env.KE_JWT_ALGORITHM).toBe("HS256");
    expect(env.KE_JWT_ACCESS_TTL_MIN).toBe(60);
    expect(env.KE_JWT_REFRESH_TTL_DAYS).toBe(1);
    expect(env.KE_JWT_REFRESH_TTL_REMEMBER_DAYS).toBe(7);
    expect(env.KE_COOKIE_SECURE).toBe("true");
    expect(env.KE_COOKIE_SAMESITE).toBe("strict");
    expect(env.KE_COOKIE_PATH).toBe("/api/auth");
  });
});
```

EnvSchema 增（沿用既有风格；数字字段用与 KE_API_PORT 相同的空串预处理 + coerce）：
```ts
  KE_JWT_SECRET: z.string().optional(),          // 启动时由 auth 装配处强制 ≥32（fail-fast 不放 schema：health-only 启动不需要它）
  KE_JWT_ALGORITHM: z.string().default("HS256"),
  KE_JWT_ACCESS_TTL_MIN: emptyAsUndef(z.coerce.number().int().positive().default(60)),
  KE_JWT_REFRESH_TTL_DAYS: emptyAsUndef(z.coerce.number().int().positive().default(1)),
  KE_JWT_REFRESH_TTL_REMEMBER_DAYS: emptyAsUndef(z.coerce.number().int().positive().default(7)),
  KE_COOKIE_DOMAIN: z.string().optional(),
  KE_COOKIE_SECURE: z.string().default("true"),
  KE_COOKIE_SAMESITE: z.string().default("strict"),
  KE_COOKIE_PATH: z.string().default("/api/auth"),
  KE_DEFAULT_MODEL: z.string().optional(),
```

（`emptyAsUndef` = 把 KE_API_PORT 那个空串预处理抽成本文件内小 helper，三处复用；不外导。）

- [ ] **Step 3: security 失败测试（红）**

`apps/api/src/auth/security.test.ts`：
```ts
import { describe, expect, it } from "vitest";
import {
  createAccessToken, createRefreshToken, decodeToken,
  hashPassword, verifyPassword,
} from "./security.js";

// 测试专用 secret（≥32 字符，非真实）
const SEC = { secret: "test-secret-0123456789abcdef0123456789abcdef", alg: "HS256" };

describe("JWT", () => {
  it("access 往返：claims 形状对齐 Python（sub 是字符串、type=access）", async () => {
    const tok = await createAccessToken({ userId: 7, username: "u1", ttlSec: 60, ...SEC });
    const p = await decodeToken(tok, SEC.secret, SEC.alg);
    expect(p?.sub).toBe("7");
    expect(p?.username).toBe("u1");
    expect(p?.type).toBe("access");
    expect(typeof p?.iat).toBe("number");
    expect(typeof p?.exp).toBe("number");
  });
  it("refresh 带唯一 jti", async () => {
    const t1 = await createRefreshToken({ userId: 7, ttlSec: 60, ...SEC });
    const t2 = await createRefreshToken({ userId: 7, ttlSec: 60, ...SEC });
    const [p1, p2] = [await decodeToken(t1, SEC.secret, SEC.alg), await decodeToken(t2, SEC.secret, SEC.alg)];
    expect(p1?.type).toBe("refresh");
    expect(p1?.jti).not.toBe(p2?.jti);
  });
  it("过期 → null（镜像 decode_token 吞异常）", async () => {
    const tok = await createAccessToken({ userId: 1, username: "x", ttlSec: -10, ...SEC });
    expect(await decodeToken(tok, SEC.secret, SEC.alg)).toBeNull();
  });
  it("签名篡改 → null", async () => {
    const tok = await createAccessToken({ userId: 1, username: "x", ttlSec: 60, ...SEC });
    expect(await decodeToken(tok, "another-secret-0123456789abcdef0123456789", SEC.alg)).toBeNull();
  });
});

describe("bcrypt", () => {
  it("hash $2b$ 12 轮 + 往返 verify", async () => {
    const h = await hashPassword("Passw0rd!xyz");
    expect(h.startsWith("$2b$12$")).toBe(true);
    expect(await verifyPassword("Passw0rd!xyz", h)).toBe(true);
    expect(await verifyPassword("wrong", h)).toBe(false);
  });
  it("垃圾 hash → false 不抛（镜像 verify_password 吞异常）", async () => {
    expect(await verifyPassword("x", "not-a-hash")).toBe(false);
  });
});
```

- [ ] **Step 4: 实现 `apps/api/src/auth/security.ts`（绿）**

```ts
/**
 * 密码 hash/verify + JWT 编解码 + cookie 配置 —— 镜像老仓 auth_security.py。
 * 兼容承诺：老库 $2b$12 hash 可验（bcryptjs）；python-jose HS256 token 互通（jose）。
 */
import { randomBytes } from "node:crypto";
import bcrypt from "bcryptjs";
import { SignJWT, jwtVerify, type JWTPayload } from "jose";

const enc = new TextEncoder();

export type KeJwtPayload = JWTPayload & {
  sub?: string; username?: string; type?: string; jti?: string;
};

/** bcrypt cost 12（设计约定，与 passlib bcrypt__rounds=12 一致） */
export async function hashPassword(plain: string): Promise<string> {
  return bcrypt.hash(plain, 12);
}

/** 失败/异常一律 false（镜像 Python 吞异常；bcrypt.compare 本身即常量时间比较） */
export async function verifyPassword(plain: string, hashed: string): Promise<boolean> {
  try {
    return await bcrypt.compare(plain, hashed);
  } catch {
    return false;
  }
}

type SignBase = { secret: string; alg: string; ttlSec: number };

export async function createAccessToken(
  v: { userId: number; username: string } & SignBase,
): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({ username: v.username, type: "access" })
    .setProtectedHeader({ alg: v.alg })
    .setSubject(String(v.userId))   // Python: sub=str(user_id)
    .setIssuedAt(now)
    .setExpirationTime(now + v.ttlSec)
    .sign(enc.encode(v.secret));
}

export async function createRefreshToken(v: { userId: number } & SignBase): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({ type: "refresh" })
    .setProtectedHeader({ alg: v.alg })
    .setSubject(String(v.userId))
    .setJti(randomBytes(16).toString("base64url")) // 镜像 secrets.token_urlsafe(16)
    .setIssuedAt(now)
    .setExpirationTime(now + v.ttlSec)
    .sign(enc.encode(v.secret));
}

/** 签名错/过期 → null（镜像 decode_token；jose 校验 exp 默认开启） */
export async function decodeToken(
  token: string, secret: string, alg: string,
): Promise<KeJwtPayload | null> {
  try {
    const { payload } = await jwtVerify(token, enc.encode(secret), { algorithms: [alg] });
    return payload as KeJwtPayload;
  } catch {
    return null;
  }
}

/** Hono setCookie 参数（镜像 cookie_settings()） */
export function cookieSettings(env: {
  KE_COOKIE_SECURE: string; KE_COOKIE_SAMESITE: string;
  KE_COOKIE_DOMAIN?: string; KE_COOKIE_PATH: string;
}, maxAgeSec: number) {
  return {
    httpOnly: true,
    secure: env.KE_COOKIE_SECURE.toLowerCase() === "true",
    sameSite: (env.KE_COOKIE_SAMESITE.toLowerCase() === "strict" ? "Strict"
      : env.KE_COOKIE_SAMESITE.toLowerCase() === "lax" ? "Lax" : "None") as "Strict" | "Lax" | "None",
    ...(env.KE_COOKIE_DOMAIN ? { domain: env.KE_COOKIE_DOMAIN } : {}),
    path: env.KE_COOKIE_PATH,
    maxAge: maxAgeSec,
  };
}
```

- [ ] **Step 5: 跑绿 + 三门禁 + commit**

```bash
pnpm vitest run packages/shared apps/api && pnpm typecheck && pnpm lint
git add packages/shared apps/api pnpm-lock.yaml
git commit -m "feat(auth): security 层 — jose JWT + bcryptjs（镜像 auth_security.py）+ env 扩展

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: 跨语言 golden 兼容测试（python-jose/passlib ↔ jose/bcryptjs）

**Files:**
- Create: `apps/api/scripts/gen_auth_fixtures.py`（用**老仓 venv** 跑，产物入 git）
- Create: `apps/api/src/auth/fixtures/python-auth.json`（脚本产物）
- Create: `apps/api/src/auth/golden.test.ts`
- Create: `apps/api/src/auth/fixtures/REVERSE-VERIFIED.md`（反向验证存证）

- [ ] **Step 1: 写 fixture 生成脚本（测试专用 secret，绝不用真实 KE_JWT_SECRET）**

`apps/api/scripts/gen_auth_fixtures.py`：
```python
"""用老仓的 passlib/python-jose 生成 auth golden fixtures（跨语言兼容锚点）。

跑法（必须用老仓 venv，保证与生产同一套库版本）：
    cd /Users/java/ke-server/apps/api
    KE_JWT_SECRET=golden-test-secret-0123456789abcdef0123456789abcdef \
      /Users/java/knowledge-engineering/venv/bin/python scripts/gen_auth_fixtures.py
"""
import json
import os
import sys
from pathlib import Path

# 把老仓加进 sys.path，复用其 auth_security（与生产逐字同源）
sys.path.insert(0, "/Users/java/knowledge-engineering")
from src.service import auth_security as sec  # noqa: E402

PASSWORD = "Golden#Pass123!"

def main() -> None:
    secret = os.environ["KE_JWT_SECRET"]
    assert len(secret) >= 32 and "golden-test" in secret, "只允许测试 secret"
    out = {
        "comment": "passlib/python-jose 产物 — TS 侧 golden 测试用；secret 是测试专用值",
        "secret": secret,
        "algorithm": "HS256",
        "password": PASSWORD,
        "bcrypt_hash": sec.hash_password(PASSWORD),
        "access_token": sec.create_access_token(user_id=42, username="golden"),
        "refresh_token": sec.create_refresh_token(user_id=42, remember_me=True),
    }
    p = Path(__file__).parent.parent / "src/auth/fixtures/python-auth.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK -> {p}")

if __name__ == "__main__":
    main()
```

跑它（注意 access_token 含 exp=+60min——golden 测试只验签名与 claims，**用 jose 的 clockTolerance 不可行（已过期 token 会拒）**，所以 decode 用「忽略过期」模式验：见 Step 2 测试里 `currentDate` 注入）。

- [ ] **Step 2: TS golden 测试（红→绿）**

`apps/api/src/auth/golden.test.ts`：
```ts
/**
 * 跨语言 golden：passlib 的 $2b$ hash 必须能被 bcryptjs 验证；
 * python-jose 签的 HS256 token 必须能被 jose 验证（claims 逐字段）。
 * fixture 由 apps/api/scripts/gen_auth_fixtures.py 用老仓 venv 生成。
 */
import { jwtVerify } from "jose";
import { describe, expect, it } from "vitest";
import { verifyPassword } from "./security.js";
import fx from "./fixtures/python-auth.json" with { type: "json" };

const key = new TextEncoder().encode(fx.secret);
// fixture 生成时刻的 token 会随时间过期 → 校验时把"当前时间"固定到 iat+1s（只验签名/claims）
async function verifyIgnoringExp(token: string) {
  const { payload } = await jwtVerify(token, key, {
    algorithms: [fx.algorithm],
    currentDate: new Date(((await decodeIat(token)) + 1) * 1000),
  });
  return payload;
}
async function decodeIat(token: string): Promise<number> {
  const body = JSON.parse(Buffer.from(token.split(".")[1]!, "base64url").toString());
  return body.iat as number;
}

describe("golden: passlib → bcryptjs", () => {
  it("老 hash 验对正确密码", async () => {
    expect(await verifyPassword(fx.password, fx.bcrypt_hash)).toBe(true);
  });
  it("老 hash 拒绝错误密码", async () => {
    expect(await verifyPassword("wrong-password", fx.bcrypt_hash)).toBe(false);
  });
});

describe("golden: python-jose → jose", () => {
  it("access token 验签 + claims 对齐", async () => {
    const p = await verifyIgnoringExp(fx.access_token);
    expect(p.sub).toBe("42");
    expect(p.username).toBe("golden");
    expect(p.type).toBe("access");
  });
  it("refresh token 验签 + jti 存在", async () => {
    const p = await verifyIgnoringExp(fx.refresh_token);
    expect(p.type).toBe("refresh");
    expect(typeof p.jti).toBe("string");
  });
});
```

（`import ... with { type: "json" }`：TS 6 / Node 24 的 JSON import attribute 语法。若 vitest 对该语法报错，退回 `readFileSync + JSON.parse`，报告记录。）

- [ ] **Step 3: 反向验证（TS 产物 → Python 验）并存证**

```bash
cd /Users/java/ke-server
# TS 生成 hash+token
pnpm exec tsx -e "
import { hashPassword, createAccessToken } from './apps/api/src/auth/security.js';
const h = await hashPassword('Golden#Pass123!');
const t = await createAccessToken({ userId: 42, username: 'golden', ttlSec: 3600, secret: 'golden-test-secret-0123456789abcdef0123456789abcdef', alg: 'HS256' });
console.log(JSON.stringify({ h, t }));
" > /tmp/ts-auth-artifacts.json
# Python 验
KE_JWT_SECRET=golden-test-secret-0123456789abcdef0123456789abcdef \
/Users/java/knowledge-engineering/venv/bin/python - <<'EOF'
import json, sys
sys.path.insert(0, "/Users/java/knowledge-engineering")
from src.service import auth_security as sec
a = json.load(open("/tmp/ts-auth-artifacts.json"))
assert sec.verify_password("Golden#Pass123!", a["h"]), "bcryptjs hash 未通过 passlib 验证"
p = sec.decode_token(a["t"])
assert p and p["sub"] == "42" and p["type"] == "access", f"jose token 未通过 python-jose: {p}"
print("REVERSE OK: bcryptjs→passlib ✓  jose→python-jose ✓")
EOF
```

把两条命令与输出原文写进 `apps/api/src/auth/fixtures/REVERSE-VERIFIED.md`（含日期与库版本：passlib/bcrypt/python-jose 版本用 `venv/bin/pip show` 查）。

- [ ] **Step 4: 三门禁 + commit**

```bash
pnpm vitest run apps/api && pnpm typecheck && pnpm lint
git add apps/api
git commit -m "test(auth): 跨语言 golden — passlib/python-jose ↔ bcryptjs/jose 双向验证 + 存证

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Bearer 中间件（getCurrentUser / requireAdmin）

**Files:**
- Create: `apps/api/src/auth/middleware.ts`
- Test: `apps/api/src/auth/middleware.test.ts`

- [ ] **Step 1: 失败测试（红）**

`apps/api/src/auth/middleware.test.ts`：
```ts
import { Hono } from "hono";
import { describe, expect, it } from "vitest";
import { createAccessToken } from "./security.js";
import { authMiddleware, requireAdmin, type AuthEnv } from "./middleware.js";

const SECRET = "test-secret-0123456789abcdef0123456789abcdef";
const fakeUser = {
  id: 7, username: "u1", email: "u1@x.com", isActive: true, isAdmin: false,
  preferredModel: null as string | null,
};

function buildApp(user: typeof fakeUser | undefined) {
  const app = new Hono<AuthEnv>();
  app.use("*", authMiddleware({
    secret: SECRET, alg: "HS256",
    findUserById: async (id: number) => (user && user.id === id ? user : undefined),
  }));
  app.get("/whoami", (c) => c.json({ id: c.get("user").id }));
  app.get("/admin", requireAdmin, (c) => c.json({ ok: true }));
  return app;
}

async function bearer(userId = 7) {
  const t = await createAccessToken({ userId, username: "u1", ttlSec: 60, secret: SECRET, alg: "HS256" });
  return { Authorization: `Bearer ${t}` };
}

describe("authMiddleware", () => {
  it("无 token → 401 detail=Not authenticated + WWW-Authenticate 头", async () => {
    const res = await buildApp(fakeUser).request("/whoami");
    expect(res.status).toBe(401);
    expect(res.headers.get("www-authenticate")).toBe("Bearer");
    expect(await res.json()).toEqual({ detail: "Not authenticated" });
  });
  it("合法 token → 注入 user", async () => {
    const res = await buildApp(fakeUser).request("/whoami", { headers: await bearer() });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ id: 7 });
  });
  it("refresh 型 token 不能当 access 用 → 401", async () => {
    const { createRefreshToken } = await import("./security.js");
    const t = await createRefreshToken({ userId: 7, ttlSec: 60, secret: SECRET, alg: "HS256" });
    const res = await buildApp(fakeUser).request("/whoami", { headers: { Authorization: `Bearer ${t}` } });
    expect(res.status).toBe(401);
  });
  it("用户不存在 → 401", async () => {
    const res = await buildApp(undefined).request("/whoami", { headers: await bearer() });
    expect(res.status).toBe(401);
  });
  it("inactive 用户 → 401", async () => {
    const res = await buildApp({ ...fakeUser, isActive: false }).request("/whoami", { headers: await bearer() });
    expect(res.status).toBe(401);
  });
});

describe("requireAdmin", () => {
  it("非 admin → 403 Admin only", async () => {
    const res = await buildApp(fakeUser).request("/admin", { headers: await bearer() });
    expect(res.status).toBe(403);
    expect(await res.json()).toEqual({ detail: "Admin only" });
  });
  it("admin → 200", async () => {
    const res = await buildApp({ ...fakeUser, isAdmin: true }).request("/admin", { headers: await bearer() });
    expect(res.status).toBe(200);
  });
});
```

- [ ] **Step 2: 实现 `apps/api/src/auth/middleware.ts`（绿）**

```ts
/**
 * Bearer 鉴权中间件 —— 镜像老仓 auth_dependencies.get_current_user / get_current_admin。
 * 失败语义：一律 401 {"detail":"Not authenticated"} + WWW-Authenticate: Bearer。
 * 注入式：findUserById 由装配处提供（生产=users repo，测试=fake）。
 */
import type { MiddlewareHandler } from "hono";
import { decodeToken } from "./security.js";

/** 中间件注入到 context 的最小用户形状（与 users repo 行兼容的子集） */
export type AuthedUser = {
  id: number; username: string; email: string;
  isActive: boolean; isAdmin: boolean; preferredModel: string | null;
};

export type AuthEnv = { Variables: { user: AuthedUser } };

export type AuthMiddlewareDeps = {
  secret: string;
  alg: string;
  findUserById: (id: number) => Promise<AuthedUser | undefined>;
};

function unauthorized(c: Parameters<MiddlewareHandler>[0]) {
  // FastAPI HTTPException(headers={"WWW-Authenticate": "Bearer"}) 的逐字段镜像
  return c.json({ detail: "Not authenticated" }, 401, { "WWW-Authenticate": "Bearer" });
}

export function authMiddleware(deps: AuthMiddlewareDeps): MiddlewareHandler<AuthEnv> {
  return async (c, next) => {
    const h = c.req.header("authorization") ?? "";
    // OAuth2PasswordBearer 语义：必须 "Bearer <token>" 前缀（大小写不敏感）
    const m = /^bearer\s+(.+)$/i.exec(h);
    if (!m) return unauthorized(c);

    const payload = await decodeToken(m[1]!, deps.secret, deps.alg);
    if (!payload || payload.type !== "access") return unauthorized(c);

    const idNum = Number.parseInt(payload.sub ?? "", 10);
    if (Number.isNaN(idNum)) return unauthorized(c);

    const user = await deps.findUserById(idNum);
    if (!user || !user.isActive) return unauthorized(c);

    c.set("user", user);
    await next();
  };
}

/** admin 守卫：挂在 authMiddleware 之后 */
export const requireAdmin: MiddlewareHandler<AuthEnv> = async (c, next) => {
  if (!c.get("user").isAdmin) return c.json({ detail: "Admin only" }, 403);
  await next();
};
```

- [ ] **Step 3: 跑绿 + 三门禁 + commit**

```bash
pnpm vitest run apps/api && pnpm typecheck && pnpm lint
git add apps/api/src/auth
git commit -m "feat(auth): Bearer 中间件 — 401/403 语义逐字段镜像 get_current_user

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 5 条 /auth 路由

**Files:**
- Create: `apps/api/src/auth/schemas.ts`、`apps/api/src/auth/routes.ts`、`apps/api/src/models.ts`
- Test: `apps/api/src/auth/routes.test.ts`

- [ ] **Step 1: 装 @hono/zod-openapi 并确认 zod 4 兼容**

```bash
pnpm add --filter ke-api @hono/zod-openapi
pnpm exec tsx -e "import { OpenAPIHono, createRoute, z } from '@hono/zod-openapi'; console.log('zod-openapi ok', typeof createRoute)"
```

若与 zod 4 冲突装不上/跑不通：**降级方案** = 普通 Hono + 手动 zod parse（路由行为不变，openapi 端点推迟到 P2b），报告记录决策。

- [ ] **Step 2: models.ts（白名单）**

```ts
/** 模型白名单 —— 镜像老仓 llm_factory.SUPPORTED_MODELS / DEFAULT_MODEL_ID */
export const SUPPORTED_MODELS = [
  { id: "qwen-plus", label: "Qwen-Plus", vendor: "DashScope" },
  { id: "MiniMax-M2", label: "MiniMax-M2", vendor: "MiniMax" },
] as const;

export function defaultModelId(env: { KE_DEFAULT_MODEL?: string }): string {
  return env.KE_DEFAULT_MODEL ?? SUPPORTED_MODELS[0].id;
}

export function isSupportedModel(id: string): boolean {
  return SUPPORTED_MODELS.some((m) => m.id === id);
}
```

- [ ] **Step 3: 路由失败测试（红）— 行为矩阵**

`apps/api/src/auth/routes.test.ts`（核心用例；fake repo 内存实现 + 真 security）：
```ts
import { describe, expect, it } from "vitest";
import { buildTestApp, seedUser } from "./test-helpers.js"; // 见 Step 4，先写测试（红：模块不存在）

describe("POST /auth/login", () => {
  it("成功：返回 access + bearer + expires_in，Set-Cookie refresh（HttpOnly/Path）", async () => {
    const { app } = await buildTestApp([await seedUser({ username: "u1", password: "Passw0rd!xyz" })]);
    const res = await app.request("/auth/login", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ username: "u1", password: "Passw0rd!xyz" }),
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.token_type).toBe("bearer");
    expect(body.expires_in).toBe(3600);
    const sc = res.headers.get("set-cookie") ?? "";
    expect(sc).toContain("refresh_token=");
    expect(sc.toLowerCase()).toContain("httponly");
    expect(sc).toContain("Path=/api/auth");
  });
  it("按 email 也能登录", async () => {
    const { app } = await buildTestApp([await seedUser({ username: "u1", email: "a@b.c", password: "Passw0rd!xyz" })]);
    const res = await app.request("/auth/login", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ username: "a@b.c", password: "Passw0rd!xyz" }),
    });
    expect(res.status).toBe(200);
  });
  it("密码错 → 401 防枚举文案；第 5 次错 → 后续 423 锁定", async () => {
    const { app } = await buildTestApp([await seedUser({ username: "u1", password: "Passw0rd!xyz" })]);
    const bad = () => app.request("/auth/login", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ username: "u1", password: "WRONGpass123" }),
    });
    for (let i = 0; i < 5; i++) {
      const r = await bad();
      expect(r.status).toBe(401);
      expect((await r.json()).detail).toBe("用户名或密码不正确");
    }
    const locked = await bad();
    expect(locked.status).toBe(423);
    expect((await locked.json()).detail).toMatch(/账号已锁定，请 \d+ 分钟后重试/);
  });
  it("inactive 用户 → 401（同防枚举文案）", async () => {
    const { app } = await buildTestApp([await seedUser({ username: "u1", password: "Passw0rd!xyz", isActive: false })]);
    const res = await app.request("/auth/login", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ username: "u1", password: "Passw0rd!xyz" }),
    });
    expect(res.status).toBe(401);
  });
});

describe("POST /auth/refresh", () => {
  it("cookie 换新 access", async () => {
    const { app } = await buildTestApp([await seedUser({ username: "u1", password: "Passw0rd!xyz" })]);
    const login = await app.request("/auth/login", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ username: "u1", password: "Passw0rd!xyz" }),
    });
    const cookie = (login.headers.get("set-cookie") ?? "").split(";")[0]!;
    const res = await app.request("/auth/refresh", { method: "POST", headers: { cookie } });
    expect(res.status).toBe(200);
    expect(typeof (await res.json()).access_token).toBe("string");
  });
  it("缺 cookie → 401 Missing refresh cookie", async () => {
    const { app } = await buildTestApp([]);
    const res = await app.request("/auth/refresh", { method: "POST" });
    expect(res.status).toBe(401);
    expect((await res.json()).detail).toBe("Missing refresh cookie");
  });
  it("跨站 origin → 403", async () => {
    const { app } = await buildTestApp([]);
    const res = await app.request("/auth/refresh", {
      method: "POST",
      headers: { origin: "https://evil.example", host: "ke.internal" },
    });
    expect(res.status).toBe(403);
  });
  it("access 型 token 冒充 refresh → 401 Invalid refresh token", async () => {
    const { app, security } = await buildTestApp([await seedUser({ username: "u1", password: "Passw0rd!xyz" })]);
    const t = await security.access(1);
    const res = await app.request("/auth/refresh", {
      method: "POST", headers: { cookie: `refresh_token=${t}` },
    });
    expect(res.status).toBe(401);
    expect((await res.json()).detail).toBe("Invalid refresh token");
  });
});

describe("GET /auth/me + PATCH /auth/me/model + POST /auth/logout", () => {
  it("me 返回六字段", async () => {
    const { app, security } = await buildTestApp([await seedUser({ username: "u1", password: "Passw0rd!xyz" })]);
    const res = await app.request("/auth/me", { headers: { Authorization: `Bearer ${await security.access(1)}` } });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(Object.keys(body).sort()).toEqual(
      ["created_at", "email", "id", "is_admin", "preferred_model", "username"],
    );
  });
  it("model 白名单外 → 400 含 repr 单引号", async () => {
    const { app, security } = await buildTestApp([await seedUser({ username: "u1", password: "Passw0rd!xyz" })]);
    const res = await app.request("/auth/me/model", {
      method: "PATCH",
      headers: { Authorization: `Bearer ${await security.access(1)}`, "content-type": "application/json" },
      body: JSON.stringify({ model_id: "gpt-99" }),
    });
    expect(res.status).toBe(400);
    expect((await res.json()).detail).toBe("不支持的模型 id: 'gpt-99'");
  });
  it("model 合法 → 200 且 preferred_model 更新", async () => {
    const { app, security } = await buildTestApp([await seedUser({ username: "u1", password: "Passw0rd!xyz" })]);
    const res = await app.request("/auth/me/model", {
      method: "PATCH",
      headers: { Authorization: `Bearer ${await security.access(1)}`, "content-type": "application/json" },
      body: JSON.stringify({ model_id: "MiniMax-M2" }),
    });
    expect(res.status).toBe(200);
    expect((await res.json()).preferred_model).toBe("MiniMax-M2");
  });
  it("logout 清 cookie（Max-Age=0）", async () => {
    const { app } = await buildTestApp([]);
    const res = await app.request("/auth/logout", { method: "POST" });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });
    const sc = res.headers.get("set-cookie") ?? "";
    expect(sc).toContain("refresh_token=");
    expect(sc).toMatch(/[Mm]ax-[Aa]ge=0/);
  });
});
```

- [ ] **Step 4: 实现 schemas + routes + test-helpers（绿）**

`apps/api/src/auth/schemas.ts`（zod；若 Step 1 选了 zod-openapi 就用其 z 并加 .openapi() 注解）：
```ts
/** /auth 请求/响应 schema —— 镜像 auth_schemas.py 的字段与边界 */
import { z } from "zod";

export const LoginRequestSchema = z.object({
  username: z.string().min(3).max(255),
  password: z.string().min(8).max(128),
  remember_me: z.boolean().default(false),
});

export const UpdatePreferredModelSchema = z.object({
  model_id: z.string().min(1).max(64),
});

/** MeResponse 字段顺序无所谓，字段集必须恰为六个（snake_case 对齐 pydantic 输出） */
export type MeResponse = {
  id: number; email: string; username: string; is_admin: boolean;
  created_at: string; preferred_model: string | null;
};
```

`apps/api/src/auth/routes.ts`（行为逐条镜像「行为规范速查」；锁定常量 `LOCK_DURATION_MIN=15`、`MAX_FAILED=5`；依赖注入 `{db repo 函数集, env, now()}`，便于测试控制时间）：
```ts
/**
 * 5 条 /auth/* 路由 —— 镜像老仓 auth_router.py。
 * 注入式依赖：repo（users 仓储）、env（JWT/cookie 配置）、nowFn（测试可控时间）。
 */
import { Hono } from "hono";
import { deleteCookie, getCookie, setCookie } from "hono/cookie";
import { toMysqlDatetime } from "@ke/store";
import { isSupportedModel } from "../models.js";
import { authMiddleware, type AuthEnv, type AuthedUser } from "./middleware.js";
import { LoginRequestSchema, UpdatePreferredModelSchema, type MeResponse } from "./schemas.js";
import {
  cookieSettings, createAccessToken, createRefreshToken, decodeToken, verifyPassword,
} from "./security.js";

const MAX_FAILED = 5;
const LOCK_DURATION_MIN = 15;

/** users repo 注入面（与 @ke/store/users.ts 函数对齐；测试用内存 fake） */
export type AuthRepo = {
  findUserByLogin(login: string): Promise<AuthRepoRow | undefined>;
  findUserById(id: number): Promise<AuthRepoRow | undefined>;
  recordLoginFailure(id: number, v: { failedAttempts: number; lockedUntil: string | null }): Promise<void>;
  recordLoginSuccess(id: number): Promise<void>;
  updatePreferredModel(id: number, modelId: string): Promise<void>;
};
export type AuthRepoRow = AuthedUser & {
  hashedPassword: string; failedAttempts: number;
  lockedUntil: string | null; createdAt: string | Date;
};

export type AuthDeps = {
  repo: AuthRepo;
  env: {
    KE_JWT_SECRET: string; KE_JWT_ALGORITHM: string;
    KE_JWT_ACCESS_TTL_MIN: number; KE_JWT_REFRESH_TTL_DAYS: number;
    KE_JWT_REFRESH_TTL_REMEMBER_DAYS: number;
    KE_COOKIE_SECURE: string; KE_COOKIE_SAMESITE: string;
    KE_COOKIE_DOMAIN?: string; KE_COOKIE_PATH: string;
  };
  nowFn?: () => Date;
};

function toMe(u: AuthRepoRow): MeResponse {
  const created = u.createdAt instanceof Date
    ? u.createdAt.toISOString() : String(u.createdAt);
  return {
    id: u.id, email: u.email, username: u.username, is_admin: u.isAdmin,
    created_at: created, preferred_model: u.preferredModel,
  };
}

export function createAuthRoutes(deps: AuthDeps): Hono<AuthEnv> {
  const { repo, env } = deps;
  const now = deps.nowFn ?? (() => new Date());
  const accessTtlSec = env.KE_JWT_ACCESS_TTL_MIN * 60;
  const refreshTtlSec = (remember: boolean) =>
    (remember ? env.KE_JWT_REFRESH_TTL_REMEMBER_DAYS : env.KE_JWT_REFRESH_TTL_DAYS) * 86400;
  const sign = { secret: env.KE_JWT_SECRET, alg: env.KE_JWT_ALGORITHM };

  const app = new Hono<AuthEnv>();
  const auth = authMiddleware({ ...sign, findUserById: (id) => repo.findUserById(id) });

  app.post("/auth/login", async (c) => {
    const parsed = LoginRequestSchema.safeParse(await c.req.json().catch(() => ({})));
    if (!parsed.success) return c.json({ detail: parsed.error.issues }, 400);
    const body = parsed.data;

    const invalid = () => c.json({ detail: "用户名或密码不正确" }, 401); // 防枚举：三种失败同文案
    const user = await repo.findUserByLogin(body.username);
    if (!user) return invalid();

    // 锁定检查：MySQL datetime 是 naive UTC 字符串 → 转毫秒比较
    const nowMs = now().getTime();
    if (user.lockedUntil) {
      const lockMs = Date.parse(`${String(user.lockedUntil).replace(" ", "T")}Z`);
      if (lockMs > nowMs) {
        const remainMin = Math.floor((lockMs - nowMs) / 60000) + 1; // 镜像 //60 + 1
        return c.json({ detail: `账号已锁定，请 ${remainMin} 分钟后重试` }, 423);
      }
    }
    if (!user.isActive) return invalid();

    if (!(await verifyPassword(body.password, user.hashedPassword))) {
      const failed = user.failedAttempts + 1;
      // 镜像「先落库再 401」：达到阈值同时写锁
      await repo.recordLoginFailure(user.id, {
        failedAttempts: failed,
        lockedUntil: failed >= MAX_FAILED
          ? toMysqlDatetime(new Date(nowMs + LOCK_DURATION_MIN * 60000)) : null,
      });
      return invalid();
    }

    await repo.recordLoginSuccess(user.id);
    const access = await createAccessToken({ userId: user.id, username: user.username, ttlSec: accessTtlSec, ...sign });
    const refresh = await createRefreshToken({ userId: user.id, ttlSec: refreshTtlSec(body.remember_me), ...sign });
    setCookie(c, "refresh_token", refresh, cookieSettings(env, refreshTtlSec(body.remember_me)));
    return c.json({ access_token: access, token_type: "bearer", expires_in: accessTtlSec });
  });

  app.post("/auth/refresh", async (c) => {
    // CSRF：host 不是 origin 子串 → 403（镜像 host not in origin）
    const origin = c.req.header("origin") ?? "";
    const host = c.req.header("host") ?? "";
    if (origin && host && !origin.includes(host)) {
      return c.json({ detail: "Cross-origin refresh forbidden" }, 403);
    }
    const token = getCookie(c, "refresh_token");
    if (!token) return c.json({ detail: "Missing refresh cookie" }, 401);
    const payload = await decodeToken(token, sign.secret, sign.alg);
    if (!payload || payload.type !== "refresh") return c.json({ detail: "Invalid refresh token" }, 401);
    const id = Number.parseInt(payload.sub ?? "", 10);
    if (Number.isNaN(id)) return c.json({ detail: "Invalid token payload" }, 401);
    const user = await repo.findUserById(id);
    if (!user || !user.isActive) return c.json({ detail: "User not found or inactive" }, 401);
    const access = await createAccessToken({ userId: user.id, username: user.username, ttlSec: accessTtlSec, ...sign });
    return c.json({ access_token: access, expires_in: accessTtlSec });
  });

  app.get("/auth/me", auth, async (c) => {
    const row = await repo.findUserById(c.get("user").id);
    return c.json(toMe(row!));
  });

  app.patch("/auth/me/model", auth, async (c) => {
    const parsed = UpdatePreferredModelSchema.safeParse(await c.req.json().catch(() => ({})));
    if (!parsed.success) return c.json({ detail: parsed.error.issues }, 400);
    const modelId = parsed.data.model_id;
    if (!isSupportedModel(modelId)) {
      return c.json({ detail: `不支持的模型 id: '${modelId}'` }, 400); // Python repr 单引号
    }
    await repo.updatePreferredModel(c.get("user").id, modelId);
    const row = await repo.findUserById(c.get("user").id);
    return c.json(toMe(row!));
  });

  app.post("/auth/logout", (c) => {
    deleteCookie(c, "refresh_token", { path: env.KE_COOKIE_PATH, sameSite: "Strict" });
    return c.json({ ok: true });
  });

  return app;
}
```

`apps/api/src/auth/test-helpers.ts`（内存 fake repo + buildTestApp + seedUser，给 routes.test 用；hashPassword 真算（cost 12 慢则在 helper 里用 cost 4 并注释「仅测试」））。完整实现执行时按测试需求落，**导出**：`buildTestApp(users: SeededUser[]) => { app, security: { access(id): Promise<string> } }`、`seedUser({username, password, email?, isActive?, isAdmin?})`。

- [ ] **Step 5: 跑绿 + 三门禁 + commit**

```bash
pnpm vitest run apps/api && pnpm typecheck && pnpm lint
git add apps/api
git commit -m "feat(auth): 5 条 /auth 路由 — 防枚举/锁定/CSRF/cookie 语义逐项镜像 auth_router

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: 装配进 ke-api + （隧道下）真库 E2E

**Files:**
- Modify: `apps/api/src/index.ts`、`apps/api/src/app.ts`
- Test: `apps/api/src/auth/e2e.test.ts`（gated：`KE_DB_IT=1`）

- [ ] **Step 1: app.ts 挂载 auth（保持注入式）**

`createApp` 增加可选 `auth?: { repo: AuthRepo; env: AuthDeps["env"] }`，有则 `app.route("/", createAuthRoutes(...))`。/health 维持原样。

- [ ] **Step 2: index.ts 装配**

- `loadEnv()` 后：若 `KE_JWT_SECRET` 存在但 `<32` 字符 → 启动抛错（镜像 `_jwt_secret()` fail-fast）；不存在 → 控制台警告「auth 路由未启用（KE_JWT_SECRET 未配置）」且只起 /health（保持 Phase 1 行为可独立跑）
- `createDb(env.KE_DB_URL)` 单例 + `AuthRepo` 实装 = `@ke/store` users 函数的薄绑定（`findUserByLogin: (l) => findUserByLogin(db, l)` …）

- [ ] **Step 3: gated 真库 E2E（红→绿）**

`apps/api/src/auth/e2e.test.ts`：gate 同 Task 1（`KE_DB_IT=1`+`KE_DB_URL`+隧道）。流程：tsx 内 `insertUser`（密码 `hashPassword("E2e#Pass123!")` 真算）→ login（断言 200 + cookie）→ me（Bearer）→ refresh（带 cookie）→ 错密码 ×5 → 423 → logout → `deleteUserById` 清理。secret 用 `.env.local` 的真实 KE_JWT_SECRET（E2E 本来就要真环境）。

- [ ] **Step 4: 全门禁 + 启动冒烟 + commit**

```bash
pnpm test && pnpm typecheck && pnpm lint
# 起服冒烟：8700 端口先清（lsof -t | xargs kill），起后 curl /health 与 POST /auth/login（错密码 → 401 JSON）再杀干净
git add apps/api && git commit -m "feat(api): auth 装配 — KE_JWT_SECRET fail-fast + users repo 绑定 + gated 真库 E2E

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: P2a 验收 + 收口

- [ ] **Step 1: 隧道 + 真实 env 下全量**：`KE_DB_IT=1` 跑全套（integration + E2E 全不 skip），三门禁，记录测试总数
- [ ] **Step 2: push**：`git push origin main`
- [ ] **Step 3: Obsidian 收口**（commit+push obsidian 仓）：
  - `TS重构-总体路线-设计.md` §五 Phase 2 标题后加：`> ◐ Phase 2 进行中：P2a auth 已完成（<日期>，<commit>）——5 路由 + Bearer 中间件 + 跨语言 golden（passlib/python-jose ↔ bcryptjs/jose 双向）+ 真库 E2E。子计划拆分：P2a auth ✅ → P2b 管理面 CRUD → P2c sessions → P2d 召回链路 → P2e agent+SSE → P2f 记忆/code/docx。`
  - `_overview.md` 开放问题 TS 重构条目同步「Phase 2 进行中（P2a ✅）」
- [ ] **Step 4: 汇报**：测试矩阵、golden 双向结果、已知差异清单（422/400、created_at 格式）、依赖版本

---

## 自审记录（写计划时已跑）

1. **Spec 覆盖**：spec §五 Phase 2 第 1 项「auth（JWT/bcrypt 兼容验证用真实存量 hash/token 测）」→ Task 3 golden 双向 + Task 6 真库 E2E；路由面 5/5（含 routes-summary 漏列的 PATCH /auth/me/model）。Phase 2 其余模块明确划出（P2b-P2f），本计划单一子系统。
2. **占位符**：Task 5 test-helpers 给了完整契约（导出签名 + 行为），实现留给执行者按测试驱动补——不算空心（测试代码已全文给出）。
3. **类型一致性**：`AuthRepo` 函数签名 = @ke/store users.ts 导出集的 curry 绑定；`AuthedUser` 字段 = middleware/test 共用；`toMysqlDatetime` 在 Task 1 定义、Task 5 routes import；`cookieSettings(env, maxAgeSec)` 与 Task 5 调用一致；security 函数签名在 Task 2/3/4/5 全程一致（带 secret/alg 注入，不读全局 env——与 Python 读 os.getenv 的差异是有意的注入式改造，行为等价）。
4. **已知风险**：① drizzle introspect 列属性命名（snake vs camel）——Task 1 Step 1 先验后写；② @hono/zod-openapi 与 zod 4 兼容——Task 5 Step 1 先验，有降级方案；③ bcryptjs cost 12 验证 ~100-200ms/次——登录路径可接受（与 Python 同级），测试 helper 可用 cost 4；④ 共享生产库的写测试——双闸门 + 专名前缀 + afterAll 清理。
