# TS 重构 Phase 2b：管理面 CRUD 路由族移植 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移植 32 条管理面路由（projects/project_members/groups/credentials/admin/users/audit 七族）+ 三件横切设施（infra-healthy 守卫、RBAC 角色解析、审计写入器），行为逐字对齐 `docs/porting/p2b-behavior-spec.md`。

**Architecture:** 行为权威 = **`/Users/java/knowledge-engineering/docs/porting/p2b-behavior-spec.md`**（并行提取 + 对抗核验产物，174KB，按族分节；每节含守卫顺序/请求边界/响应形状/错误文案逐字/DB 语义/审计/怪癖）。本计划定义文件结构、实现模式与验收，**路由级行为细节一律以 spec 对应节为准**——实现者先读自己族的节再动手。模式沿用 P2a：packages/store 薄 repo（snake_case 原样）→ apps/api 领域层（注入式 + binding 翻译）→ 路由模块 → app.ts 挂载。

**Tech Stack:** 既有底座（Hono 4.12 / drizzle 0.45 / zod 4 / vitest）。无新增运行时依赖（test-connection 用 node:child_process spawn `git ls-remote`，以 spec admin 族节实际行为为准）。

---

## 全局约定（全部任务适用）

1. **错误体**：FastAPI 形状 `{"detail": ...}`——string 或 dict（503 的 `{code,message[,deps]}`）或 zod issues 数组（已知差异#1）。状态码与 detail **逐字** 按 spec。
2. **挂载矩阵**（spec 横切节）：projects/project_members/groups/credentials/admin/users/audit 七族全部挂 `infraHealthy` 守卫（内含认证）；auth 与 /health 不挂。
3. **事务与审计**：Python `log_audit` 只 add 不 commit、与业务同事务。TS 镜像：**凡「业务写 + 审计写」的路由用 `db.transaction(async (tx) => {...})` 包裹**，审计 insert 在同一 tx；审计失败 try/catch 吞掉仅 console.warn（镜像「审计失败不中断业务」——注意：吞掉发生在审计 insert 自身，不回滚业务）。
4. **两个 deliberate 健壮性修复**（偏离 Python、已决策、登记为已知差异）：
   - **#3 `status="configured"`**：Python pydantic Literal 缺该值 → 存量行致 500（spec projects 节怪癖）。TS 放宽为 5 态 `configured|indexing|ready|partial|failed` 透传，**不复刻 500**。
   - **#4 v1 遗留角色**：`user_project_access.role` 可能残留 `reader/writer/admin`（Python ROLE_RANK KeyError → 500）。TS `roleRank(role)` 对未知值返回 **null（视为无权限）** + console.warn，不抛。
5. **测试模式**：路由测试 = 内存 fake repo + 真守卫/真 schema（P2a 模式）；每族 gated 真库测试只动 `__ke_ts_` 前缀自建数据、双闸门 KE_DB_IT=1、afterAll 清理。fake repo 行为必须镜像真 repo 语义。
6. **每任务硬门禁**：`pnpm vitest run <范围>` + `pnpm typecheck` + `pnpm lint` 全绿才 commit；commit 尾行 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`；不 push（T9 统一推）。
7. 注释规范（用户全局 MUST）：文件 docstring / 函数 JSDoc / 关键点中文注释（解释为什么 + 引用 spec 节）。
8. **不动老仓与 obsidian**（T9 收口除外）。

## 新文件地图

```
packages/store/src/
  projects.ts groups.ts groupMembers.ts userProjectAccess.ts
  gitCredentials.ts auditLogs.ts            # 薄 repo（snake_case 原样，注入 db）
apps/api/src/
  infra/status.ts                           # infra 状态持有器（boot 写入 + /health 刷新）
  infra/middleware.ts                       # infraHealthy 守卫（503 三态）
  rbac/roles.ts                             # ROLE_RANK + roleRank()（差异#4）
  rbac/resolve.ts                           # resolveRole / resolveGroupRole / expandUserGroups（注入 repo）
  rbac/middleware.ts                        # requireProjectRole / requireGroupRole 工厂
  audit/actions.ts                          # 26 个 action 常量（spec 横切节逐字）
  audit/logger.ts                           # logAudit（tx 内 insert、失败吞）
  routes/projects.ts routes/projectMembers.ts routes/groups.ts
  routes/credentials.ts routes/adminProjects.ts routes/adminCredentials.ts
  routes/adminUsers.ts routes/audit.ts      # 每族一文件 + 同名 .test.ts
  routes/test-helpers2.ts                   # P2b 公共 fake repo 集 + buildAdminTestApp
```

---

### Task 1: infra-healthy 守卫 + 状态持有器

**Files:** Create `apps/api/src/infra/status.ts`、`infra/middleware.ts` + 测试；Modify `apps/api/src/index.ts`（boot 时写入状态）、`apps/api/src/app.ts`（/health 改为读写持有器——行为不变仍每次重 ping）

- [ ] Step 1: 读 spec「横切约定」节的 `require_infra_healthy` 与 `/health` 怪癖小节
- [ ] Step 2: TDD——测试矩阵：未初始化→503 INFRA_UNINITIALIZED 文案逐字；critical down 非 admin→503 INFRA_UNHEALTHY；admin→+deps；neo4j down→放行；健康→放行；**守卫内先认证**（无 token→401 而非 503）
- [ ] Step 3: 实现。`createInfraState()`：`{ get(), set(status) }` 模块实例由 index.ts 持有注入；middleware 工厂 `infraHealthy({ state, findUserById, secret, alg })`——复用 P2a authMiddleware 的解析逻辑（直接组合 `authMiddleware` 在前、infra 检查在后，user 从 context 取）
- [ ] Step 4: index.ts boot：起服前 `checkAllDeps` 写入 state（镜像 Python startup）；app.ts /health 每次重 ping 后 `state.set(...)`（行为不变）
- [ ] Step 5: 三门禁 + commit `feat(api): infraHealthy 守卫 — 503 三态镜像 deps_infra`

### Task 2: RBAC（角色解析 + 守卫工厂）+ 相关 repo

**Files:** Create `packages/store/src/{groups,groupMembers,userProjectAccess,projects}.ts`（repo：RBAC 所需读路径 + 各族 CRUD 写路径一次建齐，函数清单按 spec 各族 DB 节倒推）、`apps/api/src/rbac/*` + 测试

- [ ] Step 1: 读 spec 横切节 `require_project_role/require_group_role/resolve_role/resolve_group_role/_expand_user_groups/ROLE_RANK` 全部小节（含修正清单里的全角括号、默认 reporter、is_admin 短路位置）
- [ ] Step 2: TDD 解析逻辑（纯函数 + fake repo）：direct vs inherited 取高、group 链 depth≥3 截断、visited 防环、project 的 is_admin 在 resolve 内短路 owner、group 的 is_admin 在**存在性检查后**短路、未知遗留角色→null+warn（差异#4 测试）
- [ ] Step 3: TDD 守卫工厂（Hono 中间件）：404「工程不存在」/「组不存在」→ 403「无权访问此工程/组」→ 403「需要 {min} 及以上权限（当前 {role}）」**全角括号**；工厂参数非法启动期抛（镜像注册期 ValueError）；通过后 `c.set("projectRole"|"groupRole", role)`
- [ ] Step 4: 三门禁 + commit `feat(api): RBAC 角色解析与守卫 — 继承链/深度截断/遗留角色兜底`

### Task 3: 审计设施 + audit 查询路由（2 条）

**Files:** Create `packages/store/src/auditLogs.ts`、`apps/api/src/audit/{actions,logger}.ts`、`routes/audit.ts` + 测试

- [ ] Step 1: 读 spec「audit 族」+ 横切节 `log_audit` 小节（不 commit、metadata ensure_ascii=False 等价 JSON.stringify、resource_id String()、失败吞）+ audit_router 的分页双层校验怪癖（Query le=200 先 422、clamp 兜底）
- [ ] Step 2: actions.ts 26 常量逐字；logger.ts `logAudit(tx, {...})`；auditLogs repo（insert + 分页查询：LEFT JOIN users 取 actor username、ORDER BY created_at DESC、scope OR 条件按 spec）
- [ ] Step 3: TDD 两条查询路由（/admin/audit-logs 守卫 + /groups/{id}/audit-logs 的 group 守卫与 scope 过滤，行为照 spec audit 族节）：分页 422 边界 + clamp + 响应包络字段逐字
- [ ] Step 4: 三门禁 + commit `feat(api): 审计设施 + 审计查询路由`

### Task 4: projects 族（3 条）

**Files:** Create `apps/api/src/routes/projects.ts` + 测试 + `routes/test-helpers2.ts`（公共 fake 集起步）

- [ ] Step 1: 读 spec「projects 族」全节（含修正清单）
- [ ] Step 2: TDD 矩阵：list admin 全量/普通用户仅 JOIN 直接成员（**不展开 group 继承**——镜像 v2 TODO 怪癖）、created_at DESC、stats 兜底 0、indexing_progress 仅 `status==indexing 且有 phase`、pipeline_at UTC+Z 格式、POST 201/403「仅管理员可创建工程」/409「工程 ID 已存在: {id}」/id pattern、GET 详情无 RBAC、**5 态 status 透传（差异#3 测试：configured 行正常 200）**
- [ ] Step 3: 三门禁 + commit `feat(api): projects 族 — 列表/创建/详情（status 5 态放宽）`

### Task 5: credentials 族（3 条用户私有 + admin 2 条）

**Files:** Create `apps/api/src/routes/credentials.ts`、`routes/adminCredentials.ts`、`packages/store/src/gitCredentials.ts` + 测试

- [ ] Step 1: 读 spec「credentials 族」「admin 族」中 credentials 部分（注意修正清单：path 参数名 `credential_id`；admin 删除的 metadata 含 `admin_action:true, original_owner_id`；先 delete 后 audit 的顺序怪癖；凭证值响应是否脱敏按 spec）
- [ ] Step 2: TDD 全矩阵（owner 隔离、404/403 文案、审计 action credential.create/delete + metadata 差异）
- [ ] Step 3: 三门禁 + commit

### Task 6: admin projects（5 条）+ users 族（4 条）

**Files:** Create `apps/api/src/routes/adminProjects.ts`、`routes/adminUsers.ts` + 测试

- [ ] Step 1: 读 spec「admin 族」projects 部分 +「users 族」全节。关键修正项：**admin projects CRUD 不写审计**（常量存在≠被用——勿"顺手补"）；users 族 flush→audit→commit 时序、is_admin=True 双审计、PATCH 细粒度 action（user.set_admin/activate/deactivate/update）、删用户两条 422 插值文案；`require_admin` 守卫 detail 是中文「仅管理员可访问」（≠ get_current_admin 的英文「Admin only」——两个守卫并存，各族用哪个以 spec 为准）
- [ ] Step 2: test-connection 路由按 spec 实际行为实现（git 操作 spawn `git ls-remote`，凭证注入方式/超时/错误文案照 spec；fake 化 spawn 以单测）
- [ ] Step 3: TDD 全矩阵 + 三门禁 + commit

### Task 7: groups 族（10 条）

**Files:** Create `apps/api/src/routes/groups.ts` + 测试

- [ ] Step 1: 读 spec「groups 族」全节（最大族：层级深度 MAX_GROUP_DEPTH 校验与两条 500 文案、parent 环检测、RESTRICT 删除语义、member CRUD 的 last-owner 422「组必须至少 1 个 owner」、视图范围 `_expand_user_groups`、审计五连 action）
- [ ] Step 2: TDD 全矩阵（这族用例最多，覆盖 spec 每条错误分支）+ 三门禁 + commit

### Task 8: project_members 族（4 条）

**Files:** Create `apps/api/src/routes/projectMembers.ts` + 测试

- [ ] Step 1: 读 spec「project_members 族」全节（last-owner 检查条件 `role=="owner" AND body.role!="owner"`、422「项目必须至少 1 个直接 owner」、审计三 action + metadata 字段）
- [ ] Step 2: TDD 全矩阵 + 三门禁 + commit

### Task 9: 挂载装配 + 真库 E2E + 并行对抗终审 + 收口

- [ ] Step 1: app.ts/index.ts 挂载七族（挂载矩阵照 spec；prefix 对齐：audit 族无 prefix 自带全路径）；启动冒烟
- [ ] Step 2: gated 真库 E2E（KE_DB_IT=1）：自建 `__ke_ts_` 用户/组/项目/凭证走全流程（建→查→改→删→审计行验证→清理），断言审计行 action/metadata
- [ ] Step 3: **并行对抗终审（ultracode）**：Workflow 按族 fan-out 审查员——每族一个，对照 spec 节逐路由核 TS 实现（文案/状态码/守卫顺序/审计），输出违规清单；违规修复后复审
- [ ] Step 4: 全量门禁（含 KE_DB_IT=1）→ push → Obsidian（spec §五 Phase 2 进度行 P2b ✅ + _overview）→ memory → 汇报（含差异#3/#4 决策说明）

---

## 自审记录

1. **Spec 覆盖**：32 条路由 7 族全分配（T3-T8）；三件横切（T1-T3）；挂载与 E2E（T9）。行为细节外置到 behavior-spec 文档（已提交 9c8ce4f，对抗核验过）——计划内不重复 174KB 内容，每任务 Step 1 强制先读对应节，无占位。
2. **决策登记**：差异#3（status 5 态）/#4（遗留角色 null 兜底）为 deliberate 修复，T4/T2 各有专门测试钉住；admin projects 不写审计是**忠实镜像**（常量未被用），T6 明示勿补。
3. **类型一致性**：repo 注入模式、binding 翻译层、`{"detail":...}` 包络与 P2a 既有代码一致；ROLE_RANK 字面值与 spec 横切节逐字。
4. **风险**：① fake repo 与真 repo 行为漂移——E2E 兜底；② groups 族复杂度最高——单独成任务且测试矩阵要求逐分支；③ test-connection 的 git 子进程——spec 为准、可 fake。
