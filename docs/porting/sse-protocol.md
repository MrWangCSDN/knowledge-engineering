# QA SSE 事件协议（py-final-baseline 提取）

> **TS 重构门禁①：TS 版 emitter 输出必须与本文档逐字段一致，前端零改动。**
>
> - 提取基线：tag `py-final-baseline`（与写作时 HEAD 的 `src/` 完全一致，已用 `git diff py-final-baseline..HEAD -- src/` 验证为空）。
> - 提取方法：**未抓真实流，基于后端代码 + 前端代码双向静态提取**（本机无 MySQL/Weaviate/LLM key，按计划跳过抓包；拿不准的行为集中登记在 §7）。
> - 后端真相源：`src/service/qa_router.py`、`src/service/qa_engine/sse_emitter.py`、`src/service/qa_engine/react_synthesizer.py`、`src/service/qa_engine/token_batcher.py`、`src/service/qa_engine/think_splitter.py`、`src/service/qa_engine/tools/render_call_graph.py`、`src/service/qa_engine/tools/todo_write.py`、`src/service/api.py`。
> - 前端真相源（只读，仓库 `/Users/java/knowledge-engineering-web`）：`src/store/chat.ts`（**唯一线上 SSE 客户端**）、`src/types/chat.ts`、`src/components/chat/AssistantMessage.tsx`、`ToolCallCard.tsx`、`buildAnswerSegments.ts`、`ContextWindowBar.tsx`。
>   注意：`src/hooks/useSSEStream.ts` 是**死代码**（除自身测试外无任何 import），不构成消费约束，但其 done/error 收尾逻辑可作参考。
> - 范围：**agent 路径**（`KE_QA_USE_REACT=1`，线上默认）。6 段式老路径（QASynthesizer 非 agent 分支）不在移植范围，但**两路径共用同一事件外壳**（同一个 `stream_qa_answer` 生成器）：`meta / step / route / token / section_* / done / error / session_title` 对两路径通用；`thinking / tool_call / todo` 仅 agent 路径产生（sse_emitter.py:351-353 只在 `is_react` 时注入回调）。

---

## 1. 传输层

### 1.1 端点

| 项 | 值 | 出处 |
|---|---|---|
| Method/Path | `POST /projects/{project_id}/qa/explain` | qa_router.py:57-62, 253-262 |
| 前端实际 URL | `{baseURL=/api}/projects/{pid}/qa/explain`（`/api` 前缀是 nginx 反代层，FastAPI app 内无此前缀） | chat.ts:372-373 |
| 鉴权 | `Authorization: Bearer <accessToken>` header + `credentials: 'include'`（cookie 走 refresh） | chat.ts:374-389 |
| 权限门禁 | 工程成员 reporter 及以上（`require_project_role("reporter")`） | qa_router.py:261 |
| 基础设施门禁 | router 级 `require_infra_healthy`，不健康直接 503 | qa_router.py:60-62 |

### 1.2 请求体（`ExplainRequest`，qa_router.py:236-248）

| 字段 | 类型 | 必选 | 约束/语义 |
|---|---|---|---|
| `question` | string | ✅ | min_length=1, max_length=2000（违反 → 422） |
| `session_id` | string \| null | ❌ | 空 → 新建会话（`"sess_" + uuid4.hex[:12]`，qa_router.py:301）；非空 → 追问已有会话 |
| `history` | `[{role, content}]` \| null | ❌ | 前端取最近 20 条（chat.ts:357-365）；后端按 token 预算再裁（qa_router.py:462-483） |
| `model` | string \| null | ❌ | max_length=64；优先级 body.model > user.preferred_model > 默认；未知 id 静默回退默认（qa_router.py:350-370） |

### 1.3 响应头（qa_router.py:490-526）

```
HTTP/1.1 200
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
Connection: keep-alive
```

实现为 Starlette `StreamingResponse(stream_qa_answer(...), media_type="text/event-stream", headers={...})`。

### 1.4 SSE 帧格式（`format_sse`，sse_emitter.py:87-96）

每条事件**恰好**为：

```
event: {event_type}\n
data: {json}\n
\n
```

- `data` 是**单行 JSON**：`json.dumps(data, ensure_ascii=False, separators=(",", ":"))` —— **紧凑分隔符（无空格）、中文不转义（UTF-8 原文）**。TS 版 `JSON.stringify` 默认行为即等价（无空格、不转义非 ASCII），但需保证**不输出多行 JSON**。
- 没有 `id:` 行、没有 `retry:` 行、没有 SSE 注释行（`: ping`）。
- **没有心跳/keepalive 机制**。流静默期（工具执行中、LLM 思考首 token 前）连接零字节；反代层超时必须 ≥ 总护栏 75s（见 §4.3）。
- 字段名示例帧（meta）：

```
event: meta
data: {"session_id":"sess_ab12cd34ef56","message_id":"msg_1234abcd5678","plan_steps":["searching","chain_extraction","synthesizing"],"context_usage":{"used_tokens":1234,"window_tokens":1000000,"pct":0.1,"history_trimmed":false}}

```

### 1.5 流前 HTTP 错误 vs 流内 error 事件

流**开始前**（headers 未发出）用 HTTP 状态码；流**开始后**一律 200 + 流内 `error` 事件（headers 已发，状态码不可改）。

| HTTP | 触发 | detail | 出处 |
|---|---|---|---|
| 401 | 未登录/token 失效 | — | auth 依赖 |
| 403 | 非工程成员 / 角色不足 | — | qa_router.py:261 |
| 404 | 工程不存在 | `"工程不存在"` | qa_router.py:278-280 |
| 409 | 工程 indexing 中 | `"工程正在索引，暂时无法问答；完成后会自动通知"` | qa_router.py:281-285 |
| 409 | 会话已归档 | `"该会话已归档，请先恢复后继续提问"` | qa_router.py:292-298 |
| 422 | question 校验失败 | Pydantic 默认结构 | qa_router.py:236-248 |
| 503 | infra 不健康 | `{"code":"INFRA_UNHEALTHY","message":"系统暂时不可用，请联系管理员"[,"deps":{...}admin 才有]}` | deps_infra.py:34-74 |
| 503 | QA 引擎未就绪（weaviate_interp_store 缺 / per-request retriever 构造失败 / synthesizer 缺） | 文案见 qa_router.py:330-348 | qa_router.py:329-348 |

前端对非 2xx 的处理（chat.ts:392-409）：读 body 文本；**特判 503 且 `body.detail.code === 'INFRA_UNHEALTHY'`** → `useInfraStore.markUnhealthy()` 弹横幅。**TS 版 503 的 detail.code 字符串必须保持 `INFRA_UNHEALTHY`**。

### 1.6 客户端断开

- 前端 abort（用户点 ⏹）：`AbortController.abort()`（chat.ts:245-305）。前端保留已累积内容并落地为消息（无 metadata）。
- 后端视角：Starlette 在 client disconnect 时取消响应迭代任务，`stream_qa_answer` 在当前 `yield`/`await`（多为 `asyncio.sleep(0.005)`）处收到 CancelledError 而终止。**断开点之后的所有步骤不再执行**——含 fold、sections dump、`on_complete` 持久化、`done`、`session_title`、`on_memory`。即：**中途断开 = 本轮 user+assistant 消息不落库，reopen 看不到这一轮**（仅新会话的 QASession 行已先 commit，qa_router.py:300-310，会留下一个 message_count=0 的空会话）。
- 后台 `asyncio.create_task(synthesize_stream)`（sse_emitter.py:355-357）**没有人显式 cancel**——最佳推断：断开后该 task 继续跑完 LLM 轮次后被丢弃（浪费 token、无副作用）。见 §7。

### 1.7 前端客户端实现要点（行为约束的另一半）

- 解析器：`eventsource-parser` `createParser().feed(decoder.decode(value, {stream:true}))`（chat.ts:433-682）。无 `event:` 或无 `data:` 的帧被跳过（chat.ts:435）。
- `data` 先 `JSON.parse`，失败则当 string（chat.ts:54-56）——后端永远发合法 JSON，此为防御。
- **switch 无 default 分支**：未知事件名静默忽略（向后兼容性的来源；`route` 事件即靠此存活）。
- **`done` 不终止读流**（chat.ts:593-645 没有 `stopped = true`）：前端在 done 之后继续读，以接收 `session_title`；直到服务端关闭连接 `reader.read()` 返回 done 才退出。**TS 版必须维持「done 之后连接仍可发事件」**。
- `error` 事件设 `stopped = true`（chat.ts:657），其后帧不再处理。
- 流自然结束但没收到 done：前端兜底清理 streaming 状态（chat.ts:684-696）。

---

## 2. 事件类型总表

**13 种事件**。「前端消费」列指 `store/chat.ts` 的 switch（chat.ts:438-672）及下游组件实际读取。

| # | event | 触发时机 | payload 字段（名:类型，●=必发） | 顺序约束 | 后端出处 | 前端消费点 |
|---|---|---|---|---|---|---|
| 1 | `meta` | 流开头，恰一次 | ●`session_id:str` ●`message_id:str` ●`plan_steps:str[]` ○`context_usage:obj` | 永远第一条 | sse_emitter.py:205-222 | chat.ts:439-511 |
| 2 | `step` | 阶段切换，3 次 | ●`phase:str` ●`desc:str` | searching 在 meta 后；chain_extraction、synthesizing 在 route 后 | sse_emitter.py:225,250,253 | chat.ts:569-570（**显式 no-op**） |
| 3 | `error` | 检索失败 / LLM 失败 | ●`code:str` ●`message:str` ●`recoverable:bool` | 替代后续所有事件，发完即流结束 | sse_emitter.py:233-238,400-405 | chat.ts:656-671（仅读 `message`） |
| 4 | `route` | 召回门控决策后，一次 | ●`skill_id:"architecture"\|"chit-chat"` ●`recall_score:float(4 位小数)` | 在 step(searching) 之后、step(chain_extraction) 之前 | sse_emitter.py:244-247 | **前端未消费**（switch 无此 case；TS 版仍须保留，防其它消费方/未来 UI） |
| 5 | `thinking` | agent 思考增量（推理模型 reasoning / 工具轮旁白） | ●`delta:str` | 仅 agent 路径；与 tool_call/todo/token 交错 | sse_emitter.py:361-363,374-376 | chat.ts:554-559 → ThinkingBlock |
| 6 | `tool_call` | 每次工具调用前后各一条（todo_write 除外） | ●`phase:"starting"\|"complete"` ●`id:str` ●`name:str` ●`at:int`；starting：●`arguments:obj`；complete：●`result_preview:str(≤600)` ○`render:{kind,data}` | starting 先于同 id 的 complete；仅 agent 路径 | sse_emitter.py:267-302,364-367,377-379,394-395 | chat.ts:513-545；ToolCallCard.tsx:51,66-74,78-106；buildAnswerSegments.ts:54-77 |
| 7 | `todo` | LLM 调 `todo_write` 时（仅 starting 阶段，complete 不发） | ●`items:[{content:str,status:"pending"\|"in_progress"\|"completed"}]` | 全量快照覆盖（非增量）；仅 agent 路径 | sse_emitter.py:276-284 | chat.ts:562-566 → TodoList |
| 8 | `token` | 最终答案轮正文增量 | ●`delta:str` | 仅最终答案轮（工具轮旁白不进 token，进 thinking）；在所有 tool_call 之后（见 §3.2） | sse_emitter.py:369-371,380-386 | chat.ts:547-551（累计到 raw_stream） |
| 9 | `section_start` | 流末按段 dump，每段 1 条 | ●`section:str` ●`title:str` | 全部 token 之后；同段三连 start→content→done | sse_emitter.py:423-426 | chat.ts:572-577 |
| 10 | `content` | 每段 1 条（**整段一次性 dump，非增量**） | ●`section:str` ●`delta:str(整段内容)` | 紧随同段 section_start | sse_emitter.py:427-430 | chat.ts:579-584 |
| 11 | `section_done` | 每段 1 条 | ●`section:str` ●`references:[]`（agent 路径恒空数组） | 紧随同段 content | sse_emitter.py:431-434 | chat.ts:586-591 |
| 12 | `done` | 正常完成，恰一次 | ●`session_id:str` ●`message_id:str` ●`total_tokens:int` ●`cost_yuan:float` ●`latency_ms:int` ●`cited_entities:str[]` | 所有 section 事件之后；**持久化（on_complete）已在 done 发出前完成** | sse_emitter.py:455-464 | chat.ts:593-645（读 `cited_entities`/`total_tokens`/`latency_ms`；**不读** session_id/message_id/cost_yuan） |
| 13 | `session_title` | done 之后；仅新会话 && 标题未被手动改 && LLM 总结成功 | ●`session_id:str` ●`title:str(≤30 字)` | 流的最后一条（之后是 on_memory 静默执行 + 连接关闭） | sse_emitter.py:470-480 | chat.ts:647-654 → sessions store 改 sidebar 标题 |

### 2.1 事件全序（agent 正常路径）

```
meta
step{phase:"searching"}
   ├─ 检索失败 → error{RETRIEVE_FAILED} → 流结束
route{skill_id, recall_score}
step{phase:"chain_extraction"}
step{phase:"synthesizing"}
(thinking | tool_call | todo)*        ← ReAct 工具轮（0..N 轮）交错出现
   ├─ LLM 异常 → error{LLM_FAILED} → 流结束
token*                                 ← 最终答案轮伪流式（20 字符/25ms 节奏）
(section_start → content → section_done)+   ← fold 后的 sections 逐段 dump
[on_complete 持久化，无事件]
done
[session_title]                        ← 可选；中间夹一次 LLM 标题总结调用（秒级静默）
[on_memory，无事件]
→ 服务端关闭连接
```

主循环 flush 优先级（同一 5ms tick 内）：thinking → tool_call/todo → token（sse_emitter.py:359-372）。

### 2.2 各事件 payload 字段细则

#### `meta`（sse_emitter.py:205-222）

| 字段 | 类型 | 必选 | 说明 | 前端读取 |
|---|---|---|---|---|
| `session_id` | str | ✅ | 新会话时为后端刚生成的真实 sid；前端用它做临时 sid → 真实 sid 迁移 | chat.ts:440 |
| `message_id` | str | ✅ | `"msg_" + uuid4.hex[:12]`（assistant 流式消息 id） | chat.ts:441 |
| `plan_steps` | str[] | ✅ | 恒为 `["searching","chain_extraction","synthesizing"]` | 未消费（types/chat.ts:263 已声明） |
| `context_usage` | obj | ❌（计算失败时缺省） | `{used_tokens:int, window_tokens:int, pct:float(0-100,1 位小数), history_trimmed:bool}`（qa_router.py:462-483） | chat.ts:444-450 **四字段全量类型校验**，任一缺/类型错则整个忽略 |

#### `step`

`{phase, desc}` 三组固定值：`("searching","检索相关代码实体")`、`("chain_extraction","提取调用链路")`、`("synthesizing","合成业务文档")`。前端显式忽略（chat.ts:569-570），desc 文案改动不影响前端，但 TS 版照搬。

#### `route`（sse_emitter.py:244-247）

`skill_id` 来自召回门控（retriever.py:196,202：top1 相似度 < `KE_QA_RECALL_THRESHOLD`(默认 0.45) → `"chit-chat"`，否则 `"architecture"`）；`recall_score = round(top1, 4)`，retriever 无该属性时 getattr 兜底 0.0。前端完全不消费（连 `SSEEventType` 枚举都没有它，types/chat.ts:227-238），靠 switch 无 default 而被忽略。**TS 版仍须保留**。

#### `thinking`

`{delta:str}`。两个来源（在 provider 层归一为 `StreamThinkingDelta`）：
1. DashScope/qwen 推理模型的 `delta.reasoning_content` 字段（llm_dashscope.py:303-308）；
2. MiniMax-M2 content 内联 `<think>...</think>`，由 `ThinkSplitter` 状态机跨 chunk 切分（llm_minimax.py:110-125, think_splitter.py）；
3. **工具轮旁白**：有 tool_calls 的轮次其正文文本不进 token，整段转投 `on_thinking`（react_synthesizer.py:325-330）——这是「工具轮旁白进灰字思考、仅最终答案进正文」机制的实现点。

`thinking` 不参与持久化（消息落库无 thinking 字段），reopen 后灰字消失——前端 `Message.thinking` 仅流式期间存在。

#### `tool_call`（sse_emitter.py:285-302）

| 字段 | 类型 | starting | complete | 说明 |
|---|---|---|---|---|
| `phase` | `"starting"`\|`"complete"` | ✅ | ✅ | |
| `id` | str | ✅ | ✅ | LLM（OpenAI 协议）生成的 tool_call id；前端用作配对 key（chat.ts:517） |
| `name` | str | ✅ | ✅ | 工具名。**`"render_call_graph"` 是前端硬编码识别的（chat.ts:526：starting 即插 loading 占位）—— TS 版工具名一个字符不能改** |
| `at` | int | ✅ | ✅ | 调工具时刻已通过 token 事件发出的正文字符数快照（sse_emitter.py:287, 322-326）。⚠️ 注意：agent 路径里 on_token 只在最终答案轮触发（react_synthesizer.py:319-324），而工具调用都发生在最终轮之前，**因此现行实现里 at 实际恒为 0**（图折叠到答案头部）。机制保留是为将来文本/工具交错时仍正确。见 §7-1 |
| `arguments` | obj | ✅ | — | LLM 的入参 dict（已 JSON 解码）。ToolCallCard 全量 k:v 展示（ToolCallCard.tsx:66-74） |
| `result_preview` | str | — | ✅ | `json.dumps(result, ensure_ascii=False)[:600]`。工具失败时是 `{"error": "..."}` 的序列化（工具错误**不**产生 error 事件，见 §4） |
| `render` | `{kind:str, data:obj}` | — | ❌（仅渲染类工具出图成功时） | `kind` 现仅 `"call_graph"`（前端 AssistantMessage.tsx:549 硬匹配此串）；`data = {nodes:[...], edges:[...]}`（结构见 §3.4）。**render=None/缺省时该键不出现在 payload**（sse_emitter.py:296-297 条件挂键） |

前端配对逻辑（chat.ts:513-545）：`tool_calls[id] = {starting, complete?, render?, at?}`；starting 且 name==='render_call_graph' → 本地造 `render:{kind:'loading',data:null}` 占位；complete 带 render → 原位换真图；complete 无 render 且占位是 loading → 删占位。**带 render 的工具调用不进「调查过程」折叠区**（AssistantMessage.tsx:372 过滤 `tc.render == null`）。

`todo_write` 调用**不会**产生 tool_call 事件（starting 转 `todo` 事件，complete 直接吞掉，sse_emitter.py:276-284）。

#### `todo`

`{items: [{content, status}]}`。items 经后端归一化保证是数组（非 list → `[]`）。全量快照语义：前端整体覆盖 `message.todos`（chat.ts:562-566）。todos 不持久化，reopen 后消失。

#### `token`

`{delta:str}`。链路：LLM chunk → `TokenBatcher(min_chars=1, max_ms=10)`（实质禁用攒批，sse_emitter.py:317-320）→ pending 队列 → 主循环 5ms tick flush。agent 最终答案轮是**伪流式**：完整轮文本按 20 字符/块、块间 25ms 重放（react_synthesizer.py:232-234, 385-403）。流末有 `token_batcher.flush()` 残留补发（sse_emitter.py:383-386）。前端把 delta 累计进 `raw_stream` 渲染打字机（chat.ts:547-551）。

#### `section_start` / `content` / `section_done`

整答案完成、fold 之后逐段三连 dump（sse_emitter.py:421-434）。注意：
- `content.delta` 是**整段全文**（一次性，不切 token）——字段叫 delta 是历史命名。
- `section` 值 = `section.get("type", "unknown")`；agent 路径典型为 `overview` 与 `call_chain`（fold 后），chit-chat（6 段路径）为 `chit-chat`。
- `title` = `section.get("title", "")`；fold 出的段无 title 键 → `""`。**`headerless` 标志不在 SSE 事件里传输**（只进持久化 sections）——流式视图与 reopen 视图因此存在已知渲染差异，见 §3.5。
- `references`：agent 路径 `_parse_sections` 兜底段恒 `[]`；6 段路径可能非空（`[{entity_id, display_text, kind}]`）。前端把它写回段（chat.ts:586-591）并渲 EntityChip（AssistantMessage.tsx:475-485）。

#### `done`

| 字段 | 类型 | 说明 | 前端 |
|---|---|---|---|
| `session_id` / `message_id` | str | 与 meta 相同值 | 不读（前端用 meta 的） |
| `total_tokens` | int | `answer.token_usage`。**agent 路径恒 0**：ReActSynthesizer 构造 `SynthesizedAnswer` 时不传 token_usage（react_synthesizer.py:324,383），取 dataclass 默认 0；6 段路径为粗算 word count。前端仅 >0 才显示（AssistantMessage.tsx:592-593），所以 agent 路径页脚不出现 tokens 文案——TS 版**不要**顺手填真实值，会改变 UI | chat.ts:598 → metadata.token_usage |
| `cost_yuan` | float | 恒 `0.0`（v1 未接价格表，synthesizer.py:99-100） | 不读 |
| `latency_ms` | int | `int((monotonic()-start)*1000)`，从 meta 前计时 | chat.ts:599 |
| `cited_entities` | str[] | **agent 工具调用轨迹**：每次 tool_call arguments 里的 `entity_id` 去重保序（react_synthesizer.py:350-354）。≠ 持久化 metadata.cited_entities（那是 sections.references 抽取，见 §5） | chat.ts:596 → 「本答案引用」EntityChip 行（AssistantMessage.tsx:577-584） |

#### `session_title`

仅当 `on_title` 回调返回非空：新会话 && `title_custom == False` && LLM 总结成功（qa_router.py:691-727）。标题先 UPDATE+commit DB 再 emit（DB 是真相源，emit 失败无妨）。标题处理链：`strip_think` → strip 引号/「」 → 截 30 字。失败/非新会话静默无事件。

#### `error`

`{code, message, recoverable}`。仅两个产生点：

| code | message 模板 | recoverable | 触发 | 出处 |
|---|---|---|---|---|
| `RETRIEVE_FAILED` | `检索失败：{exc}` | `true` | `retriever.retrieve` 抛异常 | sse_emitter.py:232-238 |
| `LLM_FAILED` | `LLM 调用失败：{exc}` | `true` | synthesize_stream task 抛异常（`task.result()` 重抛） | sse_emitter.py:399-405 |

error 后生成器 `return`：无 sections dump、无持久化、无 done。前端只读 `message` 展示，`code`/`recoverable` 已声明类型（types/chat.ts:346-351）但未消费——TS 版三字段都保留。

---

## 3. section 与 fold 语义

### 3.1 sections 数据结构

```jsonc
// 一段（后端 dict / 前端 Section 类型，types/chat.ts:47-66）
{
  "type": "overview" | "entry_point" | "call_chain" | "db_ops" | "rules" | "sources" | "chit-chat",
  "title": "",                  // fold 段恒空；6 段路径有中文标题
  "content": "markdown 或 call_chain JSON 字符串",
  "references": [],             // 可缺省；[{entity_id, display_text, kind}]
  "headerless": true            // 可缺省；仅 fold 产生的段携带（持久化有、SSE 无）
}
```

agent 自由输出的解析（`QASynthesizer._parse_sections`，synthesizer.py:334-420）：尝试 ```json fence → 严格 `json.loads` → `json_repair` 兜底 → 全失败则包成单段 `{type:"overview", title:"回答", content:raw, references:[]}`。**agent 路径正常情况就是这个单段兜底**（自由 markdown 非 JSON），随后被 fold 切开。

### 3.2 at 偏移含义

`at` = 调工具那一刻 `_on_token` 已累计的原始 delta 字符数（sse_emitter.py:259-265, 322-326）——与前端 `raw_stream` 累计长度同口径。**现行 agent 实现中工具调用全部发生在任何 token 发出之前，故 at 恒 0**（详见 §2.2 tool_call 与 §7-1）。前端兜底：`at` 非 number 时回退本地 `raw_stream.length`（chat.ts:520）。

### 3.3 fold_render_sections 折叠规则（sse_emitter.py:99-148）

输入：synthesizer 的 sections + 流式期间收集的 `rendered_graphs = [{at:int, data:{nodes,edges}}]`（仅 `render.kind=="call_graph"` 且 data 非空的 complete 才收集，sse_emitter.py:296-301）。

规则（逐条，TS 必须等价）：
1. `renders` 空或 `sections` 空 → 原样返回。
2. 取 `sections[0]` 为折叠载体：`text = base.content or ""`，`base_type = base.type or "overview"`。
3. 各 render 的 `at` 夹到 `[0, len(text)]`，按 at 升序**稳定**排序。
4. 游标切片：每个 render 前的 `text[cursor:at]` **非空才** push 为 `{type: base_type, headerless: true, content: chunk}`；随后 push 图段 `{type:"call_chain", title:"", headerless:true, content: JSON.stringify(data, 中文不转义)}`；`cursor = at`。
5. 尾段 `text[cursor:]` 非空才 push。
6. `sections[1:]` 原样拼后面。
7. **任何异常 → 返回原 sections**（fail-soft，不阻断持久化）。

at 恒 0 的现实效果：`out = [call_chain(图1), call_chain(图2)…, {base_type, headerless, content: 全文}]`——图折叠在答案头部。

### 3.4 call_chain 段 content（图 JSON）结构

`content = JSON.stringify({nodes, edges})`，与 tool_call.render.data 同构、与前端 `tryParseCallChain`（types/chat.ts:131-166）/ `CallChainFlow` 对齐：

```jsonc
{
  "nodes": [{
    "id": "OmsPortalOrderServiceImpl::generateOrder#(OrderParam)",  // 模式A=实体id；模式B=agent 自定义短id
    "label": "生成订单",            // 中文业务名（2b 解读首短语）或方法短名兜底
    "method": "OmsPortalOrderServiceImpl.generateOrder",  // 英文 短类名.方法
    "classOf": "com.macro.mall.portal.service.impl.OmsPortalOrderServiceImpl",  // 模式A才有
    "kind": "controller" | "service" | "mapper" | "method",
    "entityId": "method://OmsPortalOrderServiceImpl::generateOrder#(OrderParam)"  // 可点击跳源码；模式B无 entityId 的抽象节点可缺省
  }],
  "edges": [{ "from": "<node id>", "to": "<node id>", "label": "可选边文案" }]
}
```

（模式 A：synthesizer.py:599-682 `_build_call_chain_section_from_edges`；模式 B：render_call_graph.py:241-326 `_build_freeform_graph`，**边统一 from/to 而非 source/target**。）前端校验：nodes/edges 必须是数组、node.id 与 edge.from/to 必须是 string，否则整段降级 markdown 渲染。

### 3.5 dump、流式渲染与 reopen 回放的三态语义

1. **流式期间**：前端用 `raw_stream`（token 累计）+ `tool_calls`（at 偏移）经 `buildAnswerSegments` 交错渲染（AssistantMessage.tsx:542-568）——图内联、文本剥手画 ```reactflow/```mermaid graph 块。
2. **done 后（同会话不刷新）**：`raw_stream` 被删，消息切到 sections 渲染。⚠️ 已知前端 quirk：流内 section 事件按 **type 去重/合并**（chat.ts:95-98 `startSection` 同 type 直接忽略、chat.ts:87-93 `applyContentDelta` 追加到首个同 type 段），fold 产生的多个同 type 文本段会被合并，且 SSE 不传 `headerless` → done 后的实时视图与 reopen 视图存在轻微差异（合并后图相对文本的次序、出现段头）。这是基线现状，**TS 版照旧即可（不要"修复"dump 顺序或给 SSE 加 headerless 字段）**。
3. **reopen 回放**：`GET /projects/{pid}/qa/sessions/{sid}`（qa_router.py:594-642）返回 fs 持久化的 sections **原样数组**（多段、含 headerless），前端 `AssistantMessage` 按序渲染：`headerless===true || type==='chit-chat' || sections.length===1` 不出段头（AssistantMessage.tsx:401）；`type==='call_chain'` 先整段 `tryParseCallChain` → ReactFlow（AssistantMessage.tsx:415-417）；其余段剥手画图 fence 后 markdown。**图随 sections 持久化，reopen 不丢、位置稳定**——这就是 fold 治「图跳末尾/reopen 丢图」的最终形态。

### 3.6 防 narrate-tool 退化剥离

fold 之后、dump/持久化之前，对**每段** content 执行 ```` ```render_call_graph\n...\n``` ```` 代码块整块剥除（sse_emitter.py:34-57, 411-418，正则 DOTALL 非贪婪）。TS 版需等价正则：`/```render_call_graph[ \t]*\n[\s\S]*?\n```\n?/g`。

---

## 4. 边界行为

### 4.1 护栏参数（生产实际值）

| 护栏 | 值 | 来源 | 触发后果 |
|---|---|---|---|
| ReAct 轮数上限 | **12**（`KE_QA_REACT_MAX_ITER` 默认 "12"，api.py:222；构造器签名默认 8 仅在未传参时生效，生产 api.py 总是显式传） | api.py:216-231 | 不发 error。用累计正文兜底解析 sections；累计为空则单段 `{type:"overview", title:"未完成", content:"ReAct 循环达到 {N} 轮上限或总超时仍未收敛，请简化问题或拆分。", references:[]}`，随后正常 dump + done（react_synthesizer.py:372-383） |
| 总超时 | **75.0s**（构造器默认，api.py 未覆盖） | react_synthesizer.py:78,94 | 同上（轮间检查 `monotonic() > deadline` 则 break，**不打断进行中的轮**——实际墙钟可超 75s） |
| 单工具超时 | **20.0s**（构造器默认） | react_synthesizer.py:79,95,487-492 | 工具结果 = `{"error": "tool timeout after 20.0s: '{name}'"}` → 回灌 LLM + tool_call complete 的 result_preview；**不发 error 事件、不中断流** |
| 工具未注册 | — | react_synthesizer.py:493-494 | `{"error": "tool not registered: '{name}'"}` 同上 |
| 工具内部异常 | — | react_synthesizer.py:495-497 | `{"error": "tool execution failed: {e}"}` 同上 |

### 4.2 LLM/检索失败

- 检索异常 → `error{RETRIEVE_FAILED}`，流终止（已发 meta + step(searching)）。
- LLM 流式任务异常（含 provider 网络错、tool-calling 协议错）→ `error{LLM_FAILED}`，流终止（此前的 thinking/tool_call/token 已发出的不收回）。
- 回调三兄弟失败全部静默不影响流：`on_tool_call` 异常被吞（react_synthesizer.py:355-365）、`on_complete` 持久化失败吞（sse_emitter.py:447-452）、`on_title`/`on_memory` 失败吞（sse_emitter.py:470-494）。

### 4.3 时序/节流参数（体感复刻需要）

| 参数 | 值 | 出处 |
|---|---|---|
| 主循环 flush tick | 5ms（`asyncio.sleep(0.005)`） | sse_emitter.py:372 |
| TokenBatcher | `min_chars=1, max_ms=10`（等效直通） | sse_emitter.py:320 |
| 伪流式 chunk | 20 字符 / 25ms | react_synthesizer.py:232-234 |
| 心跳 | 无 | — |

### 4.4 客户端断开

见 §1.6。要点重申：断开 → 本轮不持久化；后台 synthesize task 无人 cancel（最佳推断继续跑完）；新会话的空 QASession 行残留。

---

## 5. 持久化映射（流内事件 ↔ 落库）

流结束前的第 6 步 `on_complete(question, answer.sections, metadata)`（sse_emitter.py:436-452 → qa_router.py:374-422 `persist_messages`），**在 done 事件之前执行完毕**——前端收到 done 时 reopen 必能读到本轮。

| 落库物 | 介质 | 内容 | 与流内事件的对应 |
|---|---|---|---|
| user 消息 | fs `ke://u/{uid}/session/{sid}/messages/{msg_id}.md` frontmatter | `role:"user"`, `content=question`, `created_at` | = 请求体 question |
| assistant 消息 | 同上 | `role:"assistant"`, `content=None`, **`sections` = fold+strip 后的数组（含 headerless / call_chain 段）**, `msg_metadata`, `created_at` | sections 与 section_start/content/section_done 三连逐段一一对应（dump 用的就是同一数组）；**msg_id 是 persist 时新生成的，≠ meta.message_id**（qa_router.py:383-384） |
| `msg_metadata` | 同上 | `{token_usage, cost_yuan, latency_ms, entry_points: ctx.entry_candidates 前 3 个 entity_id, cited_entities: sections.references 抽取去重}` | token_usage/cost_yuan/latency_ms 与 done 同值；**cited_entities ≠ done.cited_entities**（前者=引用抽取，agent 路径通常空；后者=工具轨迹）。done 多出的工具轨迹 cited_entities **不持久化**，reopen 后「本答案引用」chips 消失（前端从 metadata.cited_entities 读，agent 路径为空） |
| `qa_sessions.message_count` | MySQL | +2 | 强一致：fs 写失败 → ERROR 日志 + 不更新 DB（qa_router.py:388-413） |
| `qa_sessions.title` | MySQL | session_title 事件的 title（先 commit 后 emit） | = session_title.title |
| 记忆/会话摘要 | fs + Weaviate | on_memory 产物 | 无对应事件（纯静默副作用） |

reopen 读取（`GET …/sessions/{sid}`，qa_router.py:594-642）响应 message 形态：
`{id: null, session_id, role, content, sections, metadata, created_at}` —— 注意 `id` 恒 null（fs 文件名即 msg_id 但响应不回填）、键名是 `metadata`（不是 msg_metadata）。前端 `loadSession` 全量替换 `messagesBySession[sid]`（chat.ts:188-243）。

不在持久化里的流内信息（reopen 必然丢，属预期）：`thinking`、`todos`、`tool_calls`（含调查过程卡片）、`raw_stream`、done 的工具轨迹 cited_entities、route、step、context_usage。

---

## 6. 前端零改动核对清单

TS 版自测时逐项打勾（「读取点」均为 `/Users/java/knowledge-engineering-web` 内路径:行）：

**传输层**
- [ ] `POST {base}/projects/{pid}/qa/explain` 接受 `{question, session_id, history, model}` — chat.ts:373-388
- [ ] 200 + `text/event-stream`；帧 `event:`+`data:` 单行 JSON — chat.ts:433-435（无 event/data 即丢帧）
- [ ] 503 时 body `detail.code === 'INFRA_UNHEALTHY'` — chat.ts:397-407
- [ ] done 之后连接不立刻断，session_title 仍可达 — chat.ts:593-654（done 不置 stopped）

**meta**
- [ ] `data.session_id` string — chat.ts:440（临时 sid 迁移依赖它）
- [ ] `data.message_id` string — chat.ts:441
- [ ] `data.context_usage.{pct:number, used_tokens:number, window_tokens:number, history_trimmed:boolean}` 四字段类型 — chat.ts:444-450；ContextWindowBar.tsx:44
- [ ] `plan_steps` 前端未消费（types/chat.ts:263 声明）——仍发

**step** — 前端 no-op（chat.ts:569-570）；仍按 3 条发，phase/desc 照旧

**route** — 前端未消费（SSEEventType 无此项，types/chat.ts:227-238）——**仍发**，`{skill_id, recall_score}`

**thinking**
- [ ] `data.delta` string 累计 — chat.ts:554-559；ThinkingBlock.tsx:5

**tool_call**
- [ ] `data.phase` 'starting'/'complete' — chat.ts:521,531
- [ ] `data.id` 配对 key — chat.ts:517
- [ ] `data.name`；字面量 `'render_call_graph'` 触发 loading 占位 — chat.ts:526
- [ ] `data.at` number（现行恒 0；缺省回退 raw_stream.length） — chat.ts:520
- [ ] starting `data.arguments` 对象逐 k:v 展示 — ToolCallCard.tsx:66-74
- [ ] complete `data.result_preview` ≤600 字符串 — ToolCallCard.tsx:78-106
- [ ] complete `data.render.{kind,data}`；`kind==='call_graph'` 走 CallChainFlow — chat.ts:533-535；AssistantMessage.tsx:549-551；render.data 过 buildAnswerSegments.ts:54-77
- [ ] todo_write 不出现在 tool_call 流里 — sse_emitter.py:276-284（后端职责）

**todo**
- [ ] `data.items` 数组全量覆盖；item `{content, status∈pending|in_progress|completed}` — chat.ts:562-566；types/chat.ts:311-314

**token**
- [ ] `data.delta` string 累计为 raw_stream — chat.ts:547-551

**section 三连**
- [ ] `section_start.{section, title}` — chat.ts:572-577（同 type 二次 start 被忽略——保持现行 dump 顺序即可）
- [ ] `content.{section, delta=整段}` — chat.ts:579-584
- [ ] `section_done.{section, references[]}`，reference `{entity_id, display_text, kind}` — chat.ts:586-591；AssistantMessage.tsx:475-485
- [ ] call_chain 段 content 可被 tryParseCallChain 解析（nodes[].id:string、edges[].from/to:string） — types/chat.ts:131-166

**done**
- [ ] `data.cited_entities` string[] — chat.ts:596
- [ ] `data.total_tokens` number — chat.ts:598
- [ ] `data.latency_ms` number — chat.ts:599
- [ ] `session_id/message_id/cost_yuan` 未消费——仍发

**session_title**
- [ ] `data.session_id` + `data.title` 双非空才生效 — chat.ts:647-654；sessions.ts:116

**error**
- [ ] `data.message` string — chat.ts:658；`code`/`recoverable` 未消费（types/chat.ts:346-351 声明）——仍发

**reopen（GET sessions/{sid}）**
- [ ] messages[].sections 原样多段、`headerless:true` 透传 — AssistantMessage.tsx:401
- [ ] call_chain 段整段 JSON 渲 ReactFlow — AssistantMessage.tsx:415-417
- [ ] messages[].metadata.{cited_entities, token_usage, latency_ms} — AssistantMessage.tsx:577-594

---

## 7. ⚠️ 待 TS 实现时实测确认

静态提取拿不准、或与设计文档表述有出入的点。每条给出当前最佳推断；TS 联调时各抓一条真实流核对，确认后回写本文档。

1. **`tool_call.at` 是否恒 0**。代码推断：`_on_token` 只在最终答案轮（无 tool_calls 的轮）被 `_pseudo_stream` 调用（react_synthesizer.py:319-324），而工具调用都发生在此前轮次 → `_offset` 快照恒 0，所有图 fold 到答案头部。设计文档语言「图在模型说到这里的位置」描述的是机制能力而非现状。**最佳推断：恒 0；TS 版照搬「快照 token 偏移」机制即可自然等价**。验证法：抓流看 tool_call 事件的 at 值 + reopen 后图段是否在 sections 头部。
2. **客户端断开后，后台 synthesize task 是否继续跑完**。推断：StreamingResponse 取消的是生成器迭代，`asyncio.create_task` 的子任务无人 cancel → 继续执行至完成后丢弃（不触发 on_complete——那在生成器体内）。风险仅是 token 浪费。验证法：断开后看后端日志 LLM 调用是否照常完成。
3. **断开时点与持久化的竞态**。推断：CancelledError 命中 §5 持久化步骤**之前**的任何 yield → 本轮完全不落库；命中 on_complete 执行中（await 点）→ 可能写一半（fs user 写成、assistant 未写：read 侧按文件粒度容忍）。验证法：在 section dump 期间断开，查 fs 目录。
4. **done 与 session_title 之间的间隔**（中间夹一次 LLM 标题总结同步调用）会有数秒静默。推断：前端持续 read 不超时（fetch 无 read timeout），nginx `proxy_read_timeout` 须覆盖。TS 版若改为后台异步出标题，**事件顺序语义（done 后仍在同一连接发 session_title）不能变**。
5. **chit-chat 在 agent 模式下的形态**。代码推断：ReActSynthesizer 不分支 `ctx.skill_id`（react_synthesizer.py 全文无 chit-chat 逻辑），闲聊问题同样走 agent 自由输出 + `route{skill_id:"chit-chat"}` 事件；单段 `type:"chit-chat"` 的 section 只出现在 QASynthesizer 路径。验证法：对线上发一句"你好"，看 sections type 是 overview 还是 chit-chat。
6. **多图同 at 的折叠顺序**：`sorted(key=at)` 是稳定排序，同 at（=0）按调用先后保序。TS `Array.prototype.sort` 在现代引擎也是稳定的，但实现时显式保证（带次序号）更稳妥。
7. **`json.dumps(separators=(",",":"))` 与 `JSON.stringify` 的逐字节一致性**：浮点格式（`recall_score` round 4 位、`pct` 1 位、`cost_yuan` 0.0）Python 输出 `0.0`/`0.4523`，JS `JSON.stringify(0.0)` 输出 `0`。前端全部 `JSON.parse` 后按 number 消费，**数值文本差异不影响前端**；但若有人 diff 原始帧（如回归脚本逐字节比对 sse-sample），需注意此差异属可接受范畴。
8. **Starlette `StreamingResponse` 对 async generator 的逐 yield flush 粒度**（无显式 flush 调用，依赖 ASGI server 不缓冲）。TS（如 Fastify/Express/Hono）需确认每次 write 即 flush、关闭 Nagle 或等效配置，否则打字机退化成卡顿块。

---

*生成于 2026-06-11，Phase 0 Task 5（对照物③）。后端 src/ 基线 = tag `py-final-baseline`。*
