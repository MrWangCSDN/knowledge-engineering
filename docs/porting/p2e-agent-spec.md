# P2e agent 引擎行为规范（py-final-baseline 提取，经对抗核验）

> TS 移植权威。2026-06-12 5 路并行提取+对抗核验。配合已有 docs/porting/sse-protocol.md（13 事件）+ agent-tools-schema.json（13 工具 I/O）。
> 护栏：max_iterations 构造默认 8 / 生产 env KE_QA_REACT_MAX_ITER=12 / total_timeout 75s / tool_timeout 20s；伪流 20字/25ms。ReActSynthesizer 路径 token_usage 恒 0。



---

# 【ReAct 主循环 + 护栏 + 模型层】

All verified. I now have everything needed to produce the corrected spec. The original spec is highly accurate; I found a small number of precise corrections.

---

## 修正清单

经逐字回源核验，原 spec 总体精确。发现以下修正/补强（按严重性排序）：

1. **【文件路径错误，影响溯源】** §4 表格与「生产值覆盖路径」均称来源为 `api.py:222`，暗示 `qa_engine/api.py`。实际位于 **`src/service/api.py:222`**（不是 `qa_engine/` 子目录下的 api.py）。生产实例化在 `src/service/api.py:227-231`，构造时**只传 3 个具名参数**（`llm_provider` / `tool_registry` / `max_iterations=max_iter`），`total_timeout_sec` 与 `tool_timeout_sec` **不传**，沿用构造默认 75.0 / 20.0。spec 正文已正确表述「构造默认」，仅文件路径需修正。

2. **【构造默认值的代码注释佐证】** §4 标注 `max_iterations` 构造默认 = `8`，正确（源 line 77 `max_iterations: int = 8`）。但 spec §4 表格脚注「`12→8` 收紧」方向需澄清：源码 line 90 注释原文是「`8 轮安全阀（12→8 收紧）`」——即**构造默认从历史的 12 收紧到 8**；而生产 env 默认仍是 `"12"`（line 222）。两者并存：构造签名默认 8，生产 env 覆盖回 12。spec 已正确反映两值，此处仅补注释出处。

3. **【SynthesizedAnswer 字段默认值补全】** §2 spec 把 `sections` 标为「`list[dict]`」无默认，但源码 line 93 是 **`sections: list[dict] = field(default_factory=list)`**（有默认空列表）；`cited_entities` 同为 `field(default_factory=list)`（line 105）。`@dataclass` **非** frozen/slots（裸 `@dataclass`，line 89）。字段顺序与默认值：`sections=[] / token_usage=0 / cost_yuan=0.0 / raw_output="" / cited_entities=[]`，全部有默认值，spec 数值正确，补上 `sections`/`cited_entities` 的 `default_factory` 即完整。

4. **【非 agent 路径 token_usage 措辞】** §2「关键怪癖」称 QASynthesizer 路径用 `len(reply.split())`。核实：在 stub/error 兜底路径（line 139/178）确为 `len(reply.split())`；但主路径（line 250/330）用变量 `approx_tokens`（其计算在 `_ctx_to_dict` 附近，本次范围外）。spec 表述「词数粗算」方向正确，但精确实现因路径而异。**ReActSynthesizer 路径恒为 0 这一核心结论完全正确**（react_synthesizer.py 三处 `return SynthesizedAnswer(...)` 均不传 `token_usage`，见 line 160/225/324/383）。

5. **【ToolCall/LLMToolResponse 装饰器】** §1.1/§1.2 标注 `@dataclass(frozen=True, slots=True)`，与源码 line 13/30 **逐字一致**，正确。`StreamTextDelta`/`StreamThinkingDelta` 同为 `frozen=True, slots=True`（line 52/62），正确。

以下为**抽查确认无误**的关键点（逐字核对通过）：

- **护栏数字**：`max_iterations=8`（默认）/ 生产 `12` / `total_timeout_sec=75.0` / `tool_timeout_sec=20.0` — 全部精确。
- **伪流常量**：`_PSEUDO_STREAM_CHUNK_SIZE=20` / `_PSEUDO_STREAM_INTERVAL_SEC=0.025` — 精确（line 232/234）。
- **`_execute_tool_call` 三类错误文案**：`f"tool timeout after {self.tool_timeout_sec}s: {tc.name!r}"`、`f"tool not registered: {tc.name!r}"`、`f"tool execution failed: {e}"` — 逐字核对通过（注意 `{tc.name!r}` 用 `!r` repr 带引号，spec 用 `'<name>'` 表意正确）。
- **`_tool_message_content` 判定**：`isinstance(tool_output, dict) and tool_output.get("render") is not None` + 渲染类回 `{"ok": True, "summary": ...}`（缺省「已渲染」）、`ensure_ascii=False` — 精确。spec §8 漏标渲染类分支也带 `ensure_ascii=False`（line 38），补上。
- **`_build_tool_usage_hint` prompt 正文**：逐字核对全段（line 423-459）通过。spec §9 用省略号摘录，与原文一致；TS 移植须照搬 line 423-459 **完整原文**（含 6 条规则的全部子句，规则 4 含 `render_call_graph(entity_id, direction)`、`direction 拿不准就用 down`、`level="code_entity"` 至少调 1 次 `ke_callees / ke_read_entity` 等关键句）。注意 f-string 中 `{{"error": "..."}}` 在规则 5 输出为字面 `{"error": "..."}`。
- **`_parse_sections` 解析顺序**：````json` fence → `split("```json",1)[1]` + `rsplit("```",1)[0]`（含「无闭合 fence 则取 after_open 全部」分支，spec 未提及此 else 分支，补上）；`startswith("```")` 同理分支；严格 `json.loads` → `repair_json(candidate, return_objects=True)`（捕获 `(json.JSONDecodeError, ValueError)`）→ data=None 兜底；过滤条件 `isinstance(s, dict) and "type" in s and "content" in s and isinstance(s["content"], str)`；每段浅拷贝 `{**s, "content": _fix_gfm_table_cells(...)}`；兜底段 `{type:"overview", title:"回答", content:_fix_gfm_table_cells(raw), references:[]}` — 全部精确。spec §11 漏标兜底段 content 也过 `_fix_gfm_table_cells(raw)`（line 417），补上。
- **OpenAI wire 格式**（§16）：assistant 轮 `content`（synthesize 用 `response.content` 可能 None；synthesize_stream 真流用 `round_content or None`）、`tool_calls[].function.arguments = json.dumps(tc.arguments, ensure_ascii=False)`、tool 轮 `tool_call_id` + `content` — 精确，spec §18 怪癖 5 对两路径 content 差异的描述正确。
- **`has_real_stream` duck-typing**：`hasattr(...) and callable(getattr(self.llm, "complete_stream_with_tools", None))` — 精确。
- **真流分流**：`StreamTextDelta`→缓冲、`StreamThinkingDelta`→`on_thinking`、`ToolCall`→`round_tool_calls`；最终答案轮 `_pseudo_stream` + `last_raw_output += round_content`；工具轮 `round_content`→`on_thinking` 不进正文 — 精确。
- **cited_entities 收集**：`eid = tc.arguments.get("entity_id")`、`isinstance(eid, str) and eid and eid not in cited_entities` → append，在 `on_tool_call("starting")` **之前**（line 184-186 / 352-354）— 精确。
- **兜底段文案**：`f"ReAct 循环达到 {self.max_iterations} 轮上限或总超时仍未收敛，请简化问题或拆分。"`，title `"未完成"`，type `"overview"`，`references:[]` — 逐字精确（line 219-224 / 377-382）。
- **llm_factory**：`SUPPORTED_MODELS`（qwen-plus/Qwen-Plus/DashScope，MiniMax-M2/MiniMax-M2/MiniMax，顺序正确）、`DEFAULT_MODEL_ID = os.getenv("KE_DEFAULT_MODEL", SUPPORTED_MODELS[0]["id"])`（spec 写死 `"qwen-plus"` 等价但源码用 `SUPPORTED_MODELS[0]["id"]` 引用，TS 移植建议保持引用关系而非硬编码）、双检锁「first write wins」、锁外构造、前缀路由 `qwen`/`minimax`/`abab`（小写）+ fallback DashScope、`reset_cache` — 全部精确。补：spec 未列 `is_supported_model(model_id)` 公开函数（line 57，`not model_id → False`，`any(m["id"]==model_id ...)`），TS 移植需一并提供。
- **env 汇总**（§14）：`KE_QA_USE_REACT` 判定为 `.strip() in {"1","true","yes"}`（line 216，spec 写 `1/true/yes` 正确）、`KE_QA_REACT_MAX_ITER` 默认 `"12"`、`KE_DEFAULT_MODEL` 默认 `"qwen-plus"` — 精确。`KE_CALLCHAIN_AUTO_REPAIR` 属 `synthesizer.py` 的 call_chain 自修路径（本次范围外，未逐字核验其默认值，spec 标注待 TS 移植 call_chain 段时单独核验）。

---

相关源码绝对路径：
- `/Users/java/knowledge-engineering/src/service/qa_engine/react_synthesizer.py`（497 行，主循环+护栏）
- `/Users/java/knowledge-engineering/src/service/qa_engine/llm_factory.py`（138 行，provider 工厂）
- `/Users/java/knowledge-engineering/src/service/qa_engine/llm_types.py`（73 行，数据契约）
- `/Users/java/knowledge-engineering/src/service/qa_engine/synthesizer.py`（`SynthesizedAnswer` line 89-106，`_parse_sections` line 335-420）
- `/Users/java/knowledge-engineering/src/service/api.py`（生产实例化 line 214-238，`max_iter` line 222）

(以下为修正后的完整规范，纳入上述修正)

# ReAct Agent 主循环 + 模型层规范（TS 移植权威参考）

## 文件对应关系

| Python 源文件 | 行数 | TS 对应模块 |
|---|---|---|
| `src/service/qa_engine/react_synthesizer.py` | 497 | `@ke/agent/reactSynthesizer` |
| `src/service/qa_engine/llm_factory.py` | 138 | `@ke/llm/factory` |
| `src/service/qa_engine/llm_types.py` | 73 | `@ke/agent/types` |
| `src/service/qa_engine/synthesizer.py`（部分） | — | `SynthesizedAnswer` + `_parse_sections` 共用 |
| `src/service/api.py`（部分） | — | 生产实例化点（护栏值注入） |

---

## 1. llm_types — 数据契约层

### 1.1 ToolCall

```
@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str          # LLM 自生成；工具结果回灌时匹配用
    name: str        # 必须在 ToolRegistry 注册；未注册 → error 不抛异常
    arguments: dict[str, Any]   # 已解码的 dict（provider 负责 json.loads）
```
无默认值，两字段+一字段全必填。

### 1.2 LLMToolResponse

```
@dataclass(frozen=True, slots=True)
class LLMToolResponse:
    content: str | None                     # 文本；只有 tool_calls 时为 None
    tool_calls: list[ToolCall] = field(default_factory=list)  # 空列表 = 最终答案轮
    has_tool_calls() -> bool                # return bool(self.tool_calls)
```

### 1.3 流式增量类型（v1.8，真流路径）

```
@dataclass(frozen=True, slots=True)
class StreamTextDelta:
    text: str    # 答案正文 delta（对应 delta.content）

@dataclass(frozen=True, slots=True)
class StreamThinkingDelta:
    text: str    # 思考过程 delta（DashScope delta.reasoning_content / MiniMax <think>…</think>）
```

`complete_stream_with_tools` 的 async generator 产出 `StreamTextDelta | StreamThinkingDelta | ToolCall` 三选一（按 isinstance 分流）。

---

## 2. SynthesizedAnswer — 答案结构

定义在 `synthesizer.py` line 89-106，**裸 `@dataclass`（非 frozen/非 slots）**，agent 与非 agent 路径共用。

```
@dataclass
class SynthesizedAnswer:
    sections: list[dict] = field(default_factory=list)   # type/title/content/references
    token_usage: int = 0       # agent 路径恒为 0（ReActSynthesizer 不填）
    cost_yuan: float = 0.0     # 全路径均为 0.0（留 W8 接价格表）
    raw_output: str = ""       # 原始 LLM 输出（debug 用）
    cited_entities: list[str] = field(default_factory=list)  # agent 查过的 entity_id，去重按首现序
```

**关键怪癖**：`token_usage` 在 QASynthesizer 主路径用 `approx_tokens`（错误/stub 兜底路径用 `len(reply.split())`）；ReActSynthesizer 路径**恒为 0**（三处 return 均不传该参数）。`cost_yuan` 全路径 0.0。TS 保留两字段及默认值，agent 路径不做任何估算。

---

## 3. ToolCallingLLMProto — Provider 接口

```
interface ToolCallingLLMProto {
  complete_with_tools(params: { messages, tools }): Promise<LLMToolResponse>;
  // 可选；有此方法且 callable → 走真流式（v1.8）
  complete_stream_with_tools?(params: { messages, tools }):
    AsyncIterable<StreamTextDelta | StreamThinkingDelta | ToolCall>;
}
```
判定：`hasattr(self.llm, "complete_stream_with_tools") and callable(getattr(self.llm, "complete_stream_with_tools", None))`。

---

## 4. ReActSynthesizer — 构造参数与护栏

```python
ReActSynthesizer(
    *,                              # 强制具名传参
    llm_provider: ToolCallingLLMProto,
    tool_registry: ToolRegistry,
    max_iterations: int = 8,        # 构造默认；生产 api.py 传入 12
    total_timeout_sec: float = 75.0,
    tool_timeout_sec: float = 20.0,
)
```

| 参数 | 构造默认 | 生产值 | 来源 |
|---|---|---|---|
| `max_iterations` | `8` | `12`（`int(os.environ.get("KE_QA_REACT_MAX_ITER", "12"))`） | `src/service/api.py:222`，line 230 注入 |
| `total_timeout_sec` | `75.0` | `75.0`（不传，沿用默认） | 构造默认 |
| `tool_timeout_sec` | `20.0` | `20.0`（不传，沿用默认） | 构造默认 |

生产实例化（`src/service/api.py:227-231`）仅传 `llm_provider`/`tool_registry`/`max_iterations`；另两护栏沿用构造默认。构造后不可变。

### 伪流式常量
| 常量 | 值 |
|---|---|
| `_PSEUDO_STREAM_CHUNK_SIZE` | `20`（字符） |
| `_PSEUDO_STREAM_INTERVAL_SEC` | `0.025`（秒） |

---

## 5. synthesize() — 非流式主循环

签名同原 spec。核心流程：
1. Prompt 组装：`build_user_prompt(ctx.question, _ctx_to_dict(ctx), free_format=True)`；`with_memory_block(AGENT_SYSTEM_PROMPT, memory_block)`（None=identity）；`_build_tool_usage_hint()` 非空时用 `"\n\n"` 追加；初始 messages `[system, user]`。
2. `_deadline = time.monotonic() + 75.0`。
3. `for _iteration in range(self.max_iterations)`：先查 `time.monotonic() > _deadline` → break；调 `complete_with_tools`；`acc_text += (response.content or "")`；无 tool_calls → `raw=acc_text` → `QASynthesizer._parse_sections(raw)` → return；有 tool_calls → 追加 assistant 轮（`content` 可能 None）→ 逐 tc 收集 cited_entities + `on_tool_call("starting")` + `_execute_tool_call` + `on_tool_call("complete")` + 追加 tool 轮。
4. 循环后兜底：`raw = acc_text or ((last_response.content if last_response else "") or "")`；非空→parse；空→单段「未完成」overview。

---

## 6. synthesize_stream() — 流式主循环（v1.7/v1.8）

签名、Prompt 组装、护栏同 synthesize()。`has_real_stream` duck-typing 判定。真流路径 `StreamTextDelta`→`round_text_buf` 缓冲、`StreamThinkingDelta`→`on_thinking`（吞异常）、`ToolCall`→`round_tool_calls`；伪流路径 `round_content=response.content or ""`、`round_tool_calls=list(response.tool_calls)`。统一分流：无 tool_calls→`_pseudo_stream(round_content, on_token)`（仅有内容时）+ `last_raw_output += round_content` + parse + return；有 tool_calls→`round_content`→`on_thinking`（不进正文）+ assistant 轮 `content = round_content or None` + 工具执行。兜底 `raw = last_raw_output`，空→单段「未完成」。

---

## 7. _execute_tool_call() — 永不抛异常

| 情况 | 返回 |
|---|---|
| 正常 | `asyncio.wait_for(tool_registry.call(tc.name, tc.arguments), timeout=20.0)` 返回值 |
| `asyncio.TimeoutError` | `{"error": f"tool timeout after {self.tool_timeout_sec}s: {tc.name!r}"}` |
| `ToolNotFound` | `{"error": f"tool not registered: {tc.name!r}"}` |
| 其他 `Exception as e` | `{"error": f"tool execution failed: {e}"}` |

`{tc.name!r}` 用 repr（带单引号）。

---

## 8. _tool_message_content() — 回灌策略

判定 `isinstance(tool_output, dict) and tool_output.get("render") is not None`。
- 渲染类：`json.dumps({"ok": True, "summary": tool_output.get("summary", "已渲染")}, ensure_ascii=False)`
- 其余：`json.dumps(tool_output, ensure_ascii=False)`

两分支均 `ensure_ascii=False`。

---

## 9. _build_tool_usage_hint() — system prompt 工具指引

空 registry → `""`（不拼接）。非空时返回 `react_synthesizer.py:423-459` **完整原文**（TS 必须逐字照搬），结构：分隔线 + `【可调用工具（v1.3 ReAct）】` + 分隔线 + 「你**可以调用以下工具**做主动查询；每次只在确实需要补充信息时调用，能省则省：」+ `{tool_lines}`（每行 `  - {t.name}: {t.description}`）+「工具使用规则（**严格遵守**）：」+ 6 条规则。规则 5 输出字面 `{"error": "..."}`（f-string `{{...}}` 转义）。

---

## 10. _tools_to_openai_schema()
```
[{"type":"function","function":{"name":tool.name,"description":tool.description,"parameters":tool.input_schema}} for tool in tool_registry.list_tools()]
```
`input_schema` 直接透传（已是 JSON Schema）。

---

## 11. _parse_sections() — 静态方法（QASynthesizer 定义，ReActSynthesizer 直调）

1. `candidate = raw.strip()`；含 `"```json"` → `split("```json",1)[1]`，若结果含 ``` 则 `rsplit("```",1)[0].strip()`，**否则取 after_open 全部**（无闭合 fence 容错）；`IndexError` pass。
2. elif `startswith("```")` → 同上逻辑（`split("```",1)[1]` + rsplit/else）。
3. 严格 `json.loads(candidate)`，捕获 `(json.JSONDecodeError, ValueError)`。
4. 失败→`repair_json(candidate, return_objects=True)`（json-repair，2026-06-02 加入），其 Exception→`data=None`。
5. `isinstance(data, dict)` 且 `data.get("sections")` 为 list → 过滤 `isinstance(s,dict) and "type" in s and "content" in s and isinstance(s["content"],str)`；有效则返回 `[{**s, "content": _fix_gfm_table_cells(s["content"])} for s in valid]`（浅拷贝防御）。
6. 兜底单段：`{"type":"overview","title":"回答","content":_fix_gfm_table_cells(raw),"references":[]}`（兜底 content **也过** `_fix_gfm_table_cells`）。

---

## 12. _pseudo_stream() — classmethod

按 20 字符 `range(0, len(text), 20)` 切分，每块 `await on_token(chunk)`（吞异常）后 `await asyncio.sleep(0.025)`。

---

## 13. llm_factory — Provider 工厂

### 公开函数
- `is_supported_model(model_id: str | None) -> bool`：`not model_id → False`；`any(m["id"]==model_id for m in SUPPORTED_MODELS)`。
- `get_llm_provider(model_id: str | None = None) -> Any`：规范化（None/空/未知→`DEFAULT_MODEL_ID`，未知且非空记 debug）；双检锁缓存（`threading.Lock`，构造在锁外，first write wins）；默认构造失败→`RuntimeError`。
- `reset_cache() -> None`：仅测试/热更。

### SUPPORTED_MODELS（顺序即 UI 下拉）
```
[
  {"id":"qwen-plus","label":"Qwen-Plus","vendor":"DashScope"},
  {"id":"MiniMax-M2","label":"MiniMax-M2","vendor":"MiniMax"},
]
```
### DEFAULT_MODEL_ID
`os.getenv("KE_DEFAULT_MODEL", SUPPORTED_MODELS[0]["id"])`（即 qwen-plus；TS 保持「引用 [0].id」而非硬编码）。

### _build_provider(model_id) — 前缀路由（`model_id.lower()`）
| 前缀 | Provider |
|---|---|
| `qwen*` | `DashScopeProvider(model=model_id)` |
| `minimax*` / `abab*` | `MiniMaxProvider(model=model_id)` |
| 其他 | `DashScopeProvider(model=model_id)`（fallback，debug log） |

---

## 14. 环境变量汇总
| 变量 | 默认 | 作用 |
|---|---|---|
| `KE_QA_USE_REACT` | `""` | `.strip() in {"1","true","yes"}` 启用 ReAct |
| `KE_QA_REACT_MAX_ITER` | `"12"` | 生产 `max_iterations` |
| `KE_DEFAULT_MODEL` | `"qwen-plus"` | 默认 model id |
| `KE_CALLCHAIN_AUTO_REPAIR` | （call_chain 路径，本次范围外，移植时单独核验） | call_chain JSON 自修开关 |

---

## 15. cited_entities 收集
每 tc 执行前：`eid = tc.arguments.get("entity_id")`；`isinstance(eid,str) and eid and eid not in cited_entities` → append。时机在 `on_tool_call("starting")` **之前**。有序去重按首现序。

---

## 16. OpenAI Tool Calling wire 格式
Assistant 轮 `content`（synthesize 用 `response.content`；stream 真流用 `round_content or None`），`tool_calls[].function.arguments = json.dumps(tc.arguments, ensure_ascii=False)`（wire 上为 JSON string）。Tool 轮 `{"role":"tool","tool_call_id":tc.id,"content":_tool_message_content(...)}`，content 必为 string。

---

## 17. 外部依赖
`asyncio.wait_for`（单工具超时）、`time.monotonic`（总超时）、`json_repair.repair_json`（解析兜底）、`QASynthesizer._parse_sections` / `._fix_gfm_table_cells`（静态/内部）、`ToolRegistry.list_tools()`/`.call()`、`ToolNotFound`（`tools.base`）、`AGENT_SYSTEM_PROMPT`/`build_user_prompt`/`with_memory_block`（`prompts`）、`_ctx_to_dict`（`synthesizer`）、`DashScopeProvider`/`MiniMaxProvider`（factory）。

---

## 18. 关键怪癖与降级边界
1. `acc_text`（synthesize 累加每轮含旁白）vs `last_raw_output`（stream 仅累加最终答案轮）——故意不同。
2. 工具轮正文双路：流式→`on_thinking`；非流式→无条件累进 `acc_text`。
3. `history` 参数两方法均接受但从不消费。
4. `on_tool_call` starting/complete 两次回调均 `try/except: pass`。
5. stream 真流工具轮 assistant `content = round_content or None`；非流为 `response.content`（可能本就 None）。
6. provider 缓存锁外构造，双检 first write wins。
7. token_usage agent 路径恒 0；非 agent 主路径 `approx_tokens`、兜底路径 `len(reply.split())`；TS agent 路径保持 0。

---

# 【prompt 模板（逐字）】

Both call sites confirmed (lines 124 and 263), concatenation `f"{base_system}\n\n{tool_hint}"` confirmed. I have everything needed to verify the spec.

Let me do a final precise check on a few items the spec is specific about: the `callees` header text and the `node_summaries` trigger condition wording.

The spec is overwhelmingly accurate. Let me cross-check the exact callees header and a couple of subtle points against what I read.

---

## 修正清单

逐字核对了 `AGENT_SYSTEM_PROMPT` 全文、`_build_tool_usage_hint` 全文、`build_user_prompt` 所有分支文案、4 个 `_SKILL_HINTS`、候选区渲染、`with_memory_block`、`_format_history`、所有固定 system prompt。spec 整体高度准确，发现以下需修正/补充点：

1. **§2 步骤 4「调用关系」header 文案错误**。spec 写「拼 `callees_by_entry`（top-3 候选向下展开，`→` 符号）」未给原文；实际 prompt 行是 `调用关系（top-3 候选向下展开）:`（注意是「调用关系」不是「调用链路」，且每个 entry 行**不带前缀**、callee 行用 `      → {d}`，6 空格）。

2. **§2 步骤 7「被谁调用」header 文案缺失**。实际原文 header 为 `被谁调用（caller，了解使用场景）:`，caller 行 `      ← {u}`。spec 只写了「`←` 符号」，未给逐字 header。

3. **§2 步骤 8「数据库访问」header 文案缺失**。实际原文 header 为 `数据库访问:`，行格式 `      {op}  {tid}`（op 与 tid 间两空格；缺失时各自兜底为 `"?"`）。spec 只写「`{op}  {tid}` 格式」，未给 header。

4. **§2 步骤 6 触发条件措辞需精确**。spec 写「只有当 `node_summaries` 非空才触发」正确（`if node_summaries:`），但应补：清单 header 逐字为 `调用链方法清单（含业务解读；引用 / 画图只能锚定这些真实方法的 entityId，严禁虚构）:`。

5. **§5 扁平路径「业务说明」兜底缺失**。`summary = c.get("summary_text") or "(无业务说明)"`——summary 为空时填 `(无业务说明)`，spec 未提。同样 `level` 兜底默认 `"method"`、`entity_id` 兜底 `"?"`。

6. **§5 扁平路径每条格式行号顺序**。spec 写「`业务说明: {summary[:300]}`」，实际是先输出 `{i}. {entity_id}  [level={level}]{mod_str}` 行，再 `     业务说明: ...` 行（5 空格缩进），snippet 块在其后。基本对，但注意截断逻辑是 `if len(summary) > 300: summary = summary[:300] + "…"`（先判断再截，用单字符省略号 `…` U+2026，非 `...`）。

7. **§8 外部依赖：节点属性名**。spec 称节点有 `.code_snippet` 属性——正确，但仅**子树根 `root.code_snippet`** 被渲染；子节点的 `code_snippet` 在 `_render_tree_children` 中**不渲染**（只渲染 entity_id/module/summary）。需注明这一不对称。

8. **§3 `_build_tool_usage_hint` 文件位置**。spec 头部写「实际定义在 react_synthesizer.py 第 405 行」——确认（`def _build_tool_usage_hint` 在第 405 行）。`{{"error": "..."}}` 在源码中是双花括号（f-string 转义），渲染输出为单花括号 `{"error": "..."}`，spec §3 第 5 条已正确写成单花括号（输出态），无误。

其余抽查点（AGENT_SYSTEM_PROMPT 全文逐字、各 `free_format` 分叉文案、`_SKILL_HINTS` 4 条逐字、`with_memory_block` 分隔线/模板、候选树三条触发条件、`TOP_CANDIDATES_FOR_PROMPT=10`、300 截断、孤儿/notes 块、`_format_history` 的 `[-10:]`+`None` 兜底、`build_chitchat/with_history` 无历史短路）**全部与源码一致**。

---

# prompts.py 提取文档（修正后完整规范）

**文件路径**: `/Users/java/knowledge-engineering/src/service/qa_engine/prompts.py`（790 行）
`_build_tool_usage_hint` 定义在 `/Users/java/knowledge-engineering/src/service/qa_engine/react_synthesizer.py`（第 405 行），逻辑上属同一 prompt 模块。

---

## 1. AGENT_SYSTEM_PROMPT（逐字全文）

chat/agent（ReAct）路径专用。v1.4 / Plan C4 §7。**不**强制 6 段式 JSON，用自然 markdown 作答。结构化 6 段能力保留在 `SYSTEM_PROMPT` 供非 chat 场景。原始字符串内部 `\\n`、`\\n\\n` 为字面双反斜杠（在 prompt 文本里就是字面 `\n`，给 LLM 看的转义示例，**不是**真正换行）。

```
你是企业代码知识分析师。你的任务是把代码翻译成业务方/新人能读懂的业务说明，并直接回答用户的问题。

【作答风格】
- 用**自然的 markdown** 作答：按需用标题、列表、表格、代码块（```lang）组织，不必套固定结构。
- 简洁专业、中文；篇幅与问题复杂度匹配，不啰嗦也不凑字数。
- 调用链/架构/数据流类问题，**需要画图时自己调 `render_call_graph` 工具**（图会内联在你说到的位置，可说"见下方调用图"）——绝不手画 mermaid/reactflow。
- **不要给小节顺序编号**（别用「一、二、三」或「1. 2. 3.」当章节号）——用**描述性小标题**（如「## 业务流程」「## 涉及的表」）。调用图由工具内联渲染、不算一个编号小节；否则正文常出现「二、」却没有「一、」的断号。

【严格规则】
1. **不允许编造**：所有方法名、类名、表名必须出自我提供的 context 或工具返回结果，不能从你的知识里"想当然"；宁可说"未找到"也不要虚构 entity_id / 代码内容。
2. **引用标记**：提到方法/类/表时，用 `[entity_id|显示文本]` 格式（前端会转成可点击链接），例：`[DepositController::openAccount#()|DepositController.openAccount()]`。
3. **视角**（可选锚定）：先想清楚用户要的是"整体架构 / 请求流程 / 数据流 / 依赖关系 / 业务规则 / 外部集成"哪一类，据此组织重点，但不必显式声明视角。

【探索流程（重要：判断 context 是否充足）】

判断 context 充足的标准：
  - candidates 数量 ≥ 3 且至少有一个 level 不是 "code_entity" → 充足
  - candidates 全部是 level="code_entity" → 仅代码层数据，**拓扑解读缺失**
  - candidates 为空 → context 严重不足

context 不足时**不要直接放弃**，先用工具探索：

1. **第一步：扩大候选**
   - 不知道叫什么 → ke_search 用问题里的【关键词 / 类名 / 方法名 / 业务词】查
   - 关键词模糊 → 用 ke_glob 找文件名 / ke_grep 找代码常量

2. **第二步：理解候选**
   - 拿到 entity_id → ke_callees / ke_callers 看依赖
   - 想看代码 → ke_read_entity 看 attrs + code_snippet
   - 想看拓扑解读 → ke_method_interp（无解读也 ok，至少有 signature）

3. **第三步：判定是否真的没有**
   - 探索 2-3 轮后仍无有用结果 → 输出"我尝试了 ke_search('xxx') / ke_callees(yyy) 等工具，未能找到符合的 entity。建议补充：1) 完整类全限定名 2) 业务关键词 3) entity_id"
   - **不要无尝试就投降**

特殊情况：candidates 全是 level="code_entity"（拓扑解读缺失）：
  - 说明此工程只跑了代码索引，没跑拓扑解读
  - 你能基于代码本身解读：方法签名、调用关系、SQL preview（MyBatis）等
  - **不要**因 summary_text 为空就说"未找到"——代码层数据已经足够给出有意义的回答

【画图约定 —— 唯一规则，必须遵守】

任何"节点-边"类图（调用链 / 业务流程 / 模块依赖 / 架构总览 / 数据流 / 状态流转）**一律调
`render_call_graph` 工具**出图——它是本系统唯一的画图出口，自动生成可缩放/全屏/PNG 导出、
节点可点击跳源码的 ReactFlow 图，内联渲染在你说到的位置。工具有两种用法（按需二选一）：

  · **代码调用关系**（"谁调谁 / 调用链 / 时序"）→ 传 `entity_id`（真实方法）+ `direction`，
    工具自动 BFS 出调用图。entity_id 用 context 候选或「调用链方法清单」里的真实值
    （带 method:// 前缀照搬）；direction 拿不准用 down，工具会自动回退到有边的一侧。
  · **业务逻辑 / 流程 / 架构图**（没有现成代码调用边可锚定时）→ 直接传 `nodes` + `edges`：
    nodes 每项 {id, label(中文业务名), code(英文 类.方法，可选), kind(controller/service/dao/method，可选)}；
    edges 每项 {source, target, label(可选)}。由你构思节点与连线，工具负责渲染成同款 ReactFlow 图。

⚠️ **严禁自己手画任何"节点-边"图**：不要输出 mermaid 的 graph/flowchart 代码块、不要输出 reactflow
代码块、不要用 ASCII 画框线图。手画的边常臆造、且前端无法稳定渲染（会"解析失败"显示裸代码）。
（例外：时序图 / ER 图 / 状态机这类 ReactFlow 画不了的，才可用 mermaid 的 sequenceDiagram / erDiagram。）

🔴 **严禁把 render_call_graph 工具参数写成 markdown 代码块**（2026-06-08 实测教训）：
不要在正文里写 ```render_call_graph\nentity_id: ...\ndirection: ...\n``` 这种代码块——
这只会**显示成一段无用的 YAML/JSON 文本，根本不会渲染成图**。要画图就**真正调用工具**
（tool_use / function_call），让后端 BFS 出图、SSE 推送 call_chain 段。后端会自动剥掉
误写的 ```render_call_graph 代码块，但用户会看到图缺失——所以**第一时间就要 invoke 工具**。

- 出图时机：用户问"怎么实现 / 流程 / 调用链 / 架构 / 依赖 / 数据流"等，**先调 render_call_graph 画图，
  再用文字解释**；正文里只说"见下方调用图"，不要逐节点复述。
- **绝不**用 markdown 图片语法 `![描述](...)` 去"引用"图，也不要写"调用图已渲染 / 如上图所示"这类旁白——
  图由工具**自动内联**显示在你说到的位置（不是一张图片、也不是链接）。直接自然地说"见下方调用图"即可。
- 代码调用图（模式一）返回空（该入口无调用边）→ 改用模式二（nodes/edges）画业务逻辑图，
  或用文字/表格说明，**绝不退回手画**。

🔴 **模式 B（freeform）节点-边的硬约束**（2026-06-08 实测教训）：
  1. **候选树里给的 entity 都要出现在图里**：候选区里"子树 N"展开的所有节点（含接口
     + 实现 + Dao + 工具方法）都应该在图里出现，**不要"折叠"** —— 即使你觉得接口和
     实现"是同一个东西"。接口节点 + 实现节点都画，加一条 `实现` 标签的边连起来。
  2. **不允许编纯 calls 边**：节点-边图里默认 label（如 `调用` / 空 label）的边，应是
     candidates / call_chain 段里能找到的真实 calls 关系。Dao 方法之间通常不互相调用、
     Dao 不会调 Service（反向）—— 这种边一律不画。后端会做 calls 边核验、CodeGraph
     无支撑的纯 calls 边会被丢弃。
  3. **异步/配置/事件 关系仍要画——但必须用语义 label**：MQ 路由 / @Scheduled / @Listener /
     Spring @Bean 注入 / AOP 拦截 等 CodeGraph 抓不到的真实业务关系，**不要让节点变孤儿**，
     大方画出来。label 里必须含明确语义关键词让用户一眼区分非直接调用：
       - `异步触发` / `MQ路由` / `延迟消息` / `事件监听` / `事件订阅`
       - `配置注入` / `Bean配置` / `@RabbitListener触发` / `定时调度`
     例：`CancelOrderSender::sendMessage → RabbitMqConfig::orderTtlQueue [MQ路由]`，
        `RabbitMqConfig::orderTtlQueue → CancelOrderReceiver::handle [异步触发]`。
     后端会识别 label 含"异步/MQ/触发/监听/配置/路由/事件"等关键词的边为"语义边"跳过
     calls 校验保留——你正常画即可，不会被冤枉删。
  4. **dotted vs scoped notation**：node.method 字段统一用 `Class::method` 形态
     （与 CodeGraph qualified_name 一致），不要用 `Class.method`。后端做了归一化但
     `::` 是首选。
```

---

## 2. build_user_prompt

**签名**: `build_user_prompt(question: str, context: dict[str, Any], free_format: bool = False) -> str`

`context` 键（来自 `RetrievedContext.to_dict()`）:
- `entry_candidates`: `list[{entity_id, summary_text, level, module?, ...}]`
- `candidate_code_snippets`: `dict[entity_id, str]` — 源码优先接地 P1
- `candidate_tree`: `CandidateTree | None` — None 表示走旧扁平路径
- `callees_by_entry`: `dict[entity_id, list[str]]`
- `callers_by_entry`: `dict[entity_id, list[str]]`
- `call_edges_by_entry`: `dict[entity_id, list[tuple[str, str]]]` — 多跳 from→to 边
- `callchain_node_summaries`: `dict[entity_id, str]` — 中文业务解读
- `table_access_by_entry`: `dict[entity_id, list[{table_id, operation}]]`
- `skill_id`: `"business" | "dependency" | "data-flow" | "architecture"`（缺失时默认 `"architecture"`）

### 核心流程步骤（含逐字 header）

1. 拼 `【用户问题】{question}` + 空行
2. 按 `skill_id` 查 `_SKILL_HINTS` 表，非空则插 `【路由提示】` + hint 文案 + 空行
3. 插 `【可用 context】` header，随后候选区：
   - **候选区分支**（关键判断，三条全真走树形）：
     ```python
     if (candidate_tree is not None
         and len(candidate_tree.subtrees) >= 2
         and not candidate_tree.fallback_to_flat):
         _render_tree_candidates(tree, code_snippets)   # 树形
     else:
         _render_flat_candidates(candidates, code_snippets)  # 扁平（旧行为）
     ```
   - candidates 为空时插 `（向量库未命中任何候选实体）`
4. 拼 `callees_by_entry`（`any(callees.values())` 触发）。header 逐字：`调用关系（top-3 候选向下展开）:`。每 entry：`  {entry}`，每 callee：`      → {d}`（6 空格 + `→`）。空 downs 跳过。
5. 拼 `call_edges_by_entry`（`any(call_edges.values())` 触发，多跳 from→to 边）。header 按 `free_format` 分叉：
   - `free_format=True`：`调用链路（入口向下多跳展开，from → to 边）—— 需要调用图时调 render_call_graph 工具（传入口 entity_id），不要手画；以下边供你理解调用结构:`（源码两段字面量拼接）
   - `free_format=False`：`调用链路（入口向下多跳展开，from → to 边）—— 画调用图/流程图时请据此输出 call_chain 段:`
   - 每 entry：`  入口 {entry}:`，每边：`      {frm}  →  {to}`（6 空格 + `frm` + 2 空格 + `→` + 2 空格 + `to`）
6. 拼 `callchain_node_summaries`（A1 中文化；**仅当 `node_summaries` 非空（`if node_summaries:`）才触发**）：
   - 收集 `call_edges` 全部端点（frm, to）**按出现顺序去重**为 `recalled_methods`（含无解读方法）
   - header 逐字：`调用链方法清单（含业务解读；引用 / 画图只能锚定这些真实方法的 entityId，严禁虚构）:`
   - 每行：有解读 → `  - entityId: method://{m} | 方法: {m} | 业务解读: {s}`；无解读 → `  - entityId: method://{m} | 方法: {m} | （无解读，按方法名+签名理解）`
   - 画图指令按路径分叉：
     - `free_format=True`：`【需要画图时】一律调 \`render_call_graph\` 工具（唯一画图出口，绝不手画 mermaid/reactflow）：代码调用图传真实 entityId（上面清单里照搬）作入口；业务逻辑/架构图无现成调用边时直接传 nodes+edges。`（两段拼接）
     - `free_format=False`：5 条 A1 锚定式手绘 call_chain JSON 指令：
       ```
       【画 call_chain 业务流程图时（A1 锚定式）】
         1. 把上述调用链重写成中文业务步骤流：可把连续的几个方法合并成一个业务步骤；
         2. 每个节点必须锚定到上面清单里的某个真实方法，entityId 照搬其 method:// 值（点击可跳源码）；
         3. label 用中文业务动作（≤12 字）；有「业务解读」的据其提炼，无解读的按方法名/签名理解；
         4. edge.label 用中文衔接（如「校验通过后」「下单成功触发」）；
         5. 只能用上面清单里的方法，严禁虚构代码里没有的步骤/方法。
       ```
7. 拼 `callers_by_entry`（`any(callers.values())` 触发）。header 逐字：`被谁调用（caller，了解使用场景）:`。每 entry：`  {entry}`，每 caller：`      ← {u}`。空 ups 跳过。
8. 拼 `table_access_by_entry`（`any(table_access.values())` 触发）。header 逐字：`数据库访问:`。每 entry：`  {entry}`，每表：`      {op}  {tid}`（op 缺失兜底 `"?"`、tid 缺失兜底 `"?"`，op 与 tid 间两空格）。空 tables 跳过。
9. 拼 `【任务】` 块（固定首行 `基于以上 context 回答用户问题。`），随后按 `free_format` 分叉：
   - `free_format=True`（4 行）：
     ```
     用自然的 markdown 作答（标题/列表/代码块按需），不必套固定结构。
     提到方法/类/表时用 `[entity_id|显示文本]` 标注；只能基于 context/工具返回的真实实体，不得编造 entity_id。
     如本题适合调用图/流程图/架构图，**自己调 `render_call_graph` 工具**出图（图会内联在你说到的位置），不要手画 mermaid/reactflow。
     如果 candidates 不足，按 system prompt 里的「探索流程」主动调用工具补齐再回答；探索仍无果时再说明并建议补充。
     ```
   - `free_format=False`（4 行）：
     ```
     先按 system prompt 里的 Step 1 选 1 个主视角，再按该视角侧重组织 6 段式答案。
     严格按 JSON 输出，缺信息段跳过；overview 段无论如何都要出（注明视角）。
     如果 context 不足以回答（比如候选都不相关），
     仍要给一个 overview 段说明：视角：overall-architecture\n未找到相关业务逻辑，建议换个说法。
     ```

最终 `return "\n".join(parts)`。

### 框架句「代码细节以源码为准」（扁平与树形分支头部，仅 `code_snippets` 非空时插）

```
  （注：以下候选凡附【真实源码片段】的，代码细节——SQL/表名/字段/存储技术/方法调用/状态码——一律以源码为准，2b 业务说明仅作业务提示、不可当代码事实；引用仍用 entity_id。）
```

### 护栏数字

| 常量/逻辑 | 值 | 用途 |
|---|---|---|
| `TOP_CANDIDATES_FOR_PROMPT` | `10` | 扁平路径候选截取上限（`candidates[:10]`）|
| `summary` 截断 | `if len(summary) > 300: summary[:300] + "…"` | 单条 summary 上限 300 字符 + 单字符省略号 `…`（U+2026，非 `...`）|
| `candidate_tree.subtrees` 阈值 | `>= 2` | 走树形分支的必要条件之一 |
| 历史窗口 | `history[-10:]` | `_format_history` 冗余硬上限 |

### 字段兜底（扁平路径）

- `entity_id = c.get("entity_id", "?")`
- `level = c.get("level", "method")`
- `module = c.get("module")`（None 时不输出 `(模块: …)`）
- `summary = c.get("summary_text") or "(无业务说明)"`（空时填 `(无业务说明)`）

### with_memory_block

**签名**: `with_memory_block(system: str, memory_block: str | None) -> str`

`memory_block` 为 `None` 或 `.strip()` 后为空 → 原样返回 `system`（零开销）。非空时用 `_MEMORY_BLOCK_TEMPLATE.format(block=memory_block.strip())` 前置（block 先 strip）：

```
═══════ 记忆（关于本用户 / 本次会话的已知事实，优先参考）═══════
{block}
═══════════════════════════════════════════════════════════════

{原 system}
```

（模板字面量为 `_MEMORY_BLOCK_TEMPLATE`，结尾含 `\n\n`，再拼 system。）

---

## 3. _build_tool_usage_hint（react_synthesizer.py 第 405 行）

**签名**: `self._build_tool_usage_hint() -> str`（`ReactSynthesizer` 方法）

**逻辑**：`tools = self.tool_registry.list_tools()`；空 → 返回 `""`（调用方不拼接）。否则 `tool_lines = "\n".join(f"  - {t.name}: {t.description}" for t in tools)`，拼入以下固定文案块。注意源码中 `{{"error": "..."}}` 是 f-string 双花括号转义，**渲染输出为单花括号** `{"error": "..."}`（下方已是输出态）：

```
═════════════════════════════════════════════════════════════
【可调用工具（v1.3 ReAct）】
═════════════════════════════════════════════════════════════

你**可以调用以下工具**做主动查询；每次只在确实需要补充信息时调用，能省则省：

{tool_lines}

工具使用规则（**严格遵守**）：

1. **优先用【可用 context】里已经检索好的 candidates**。我（系统）在你看到的 prompt 里已经做过初步语义检索，
   candidates 里的 entity_id 是**真实存在**的；别自己编不存在的符号，那样工具会查不到。

2. **entity_id 要照搬**。candidates 里给你的形如 `OmsPortalOrderServiceImpl::generateOrder#(OrderParam)`
   （`类名::方法名#(参数)` 形态，可能含注解/泛型文本），**原样照抄**、一个字符都别改。

3. **不要在 tool_call 输入里指定 project_id**。工具已经由后端绑定到当前会话的工程，
   你提供 project_id 会被忽略（schema 也不再包含该字段）。

4. **能直接答就别再调工具**，且**用自由、自然的 markdown 作答（不要套固定模板 / 结构化 JSON）**：
   结构与篇幅随问题深浅自适应——简单问题简短直答、复杂问题再展开；引用代码实体照抄 candidates 的 entity_id
   （前端可点击跳源码）；检索不到就如实说"未检索到 X"，不要编。
   **涉及调用关系/流程/"它调了谁、谁调它"时，调 render_call_graph(entity_id, direction)** 内联一张可点击调用图：
   它确定性构图、带中文业务标签；图直接展示给用户——你只需文字里提"见下方调用图"，**不要逐节点复述、
   也绝不要再自己写 ```reactflow 画同一张图（会重复出两张）**。direction 拿不准就用 down，
   工具查不到该方向时会自动回退到有调用关系的另一侧。
   **但**如果 candidates 全是 level="code_entity"（业务解读缺失），即使候选齐全也至少调 1 次
   ke_callees / ke_read_entity 补齐代码细节。

5. **错就早错**。tool 返回 `{"error": "..."}` 时不要重复同一调用；改 entity_id 或换工具。

6. **新增：文件层探索工具**（ke_grep / ke_glob / ke_read_file / ke_ls）适合
   "图谱里没的东西"——配置文件（yml/properties）、Mapper XML 原文、注释、字符串
   常量、目录结构、import 关系。流程参考：先 ke_glob 找文件 → ke_grep 定位行 →
   ke_read_file 看完整内容。`path` 参数都是项目相对路径，禁用 `..`/绝对路径，
   越界会被拒。
```

**调用位置**：`react_synthesizer.py` 第 124 行、263 行（两处 synthesize 入口）。两处一致：
```python
base_system = with_memory_block(AGENT_SYSTEM_PROMPT, memory_block)
system_text = base_system
tool_hint = self._build_tool_usage_hint()
if tool_hint:
    system_text = f"{base_system}\n\n{tool_hint}"   # 空 hint 时 system_text 保持 base_system
# messages: {"role": "system", "content": system_text}
```
即注入顺序为：`记忆块 + AGENT_SYSTEM_PROMPT`（= base_system）`\n\n` + tool_hint。

---

## 4. _SKILL_HINTS（视角偏置路由提示，逐字）

| skill_id | 文案 |
|---|---|
| `"business"` | `本题已被分类为 business（业务规则）。请优先采用 business-rule 视角；rules 段务必充实，db_ops 段可省略。` |
| `"dependency"` | `本题已被分类为 dependency（调用 / 依赖）。请优先采用 dependency-map 或 request-lifecycle 视角；务必给出双向调用图（机制按各自路径：agent 调 render_call_graph 工具，6 段出 call_chain 段）。` |
| `"data-flow"` | `本题已被分类为 data-flow（数据流 / 持久化）。请优先采用 data-flow 视角；db_ops 段务必列出所有涉及的表 + 读写操作。` |
| `"architecture"` | `本题已被分类为 architecture（整体架构）。请优先采用 overall-architecture 视角；entry_point + call_chain 都要写。` |

`skill_id = context.get("skill_id") or "architecture"`（缺失/falsy 默认 `"architecture"`）。

---

## 5. 候选区渲染逻辑

### 扁平 `_render_flat_candidates(candidates, code_snippets)`

- 首行：`候选入口方法（按相关度倒序）:`
- `code_snippets` 非空 → 插框架句（见 §2）
- `top_candidates = candidates[:10]`
- 每条（`enumerate(..., 1)`）：
  ```
    {i}. {entity_id}  [level={level}]{mod_str}
       业务说明: {summary}
  ```
  - `mod_str = "  (模块: {module})"` 仅当 `module` 真值，否则空串
  - `summary` 先 `or "(无业务说明)"` 兜底，再 `len>300 → [:300]+"…"`
  - `snippet = code_snippets.get(entity_id)` 非空时追加（snippet 每行前缀 5 空格）：
    ```
       【真实源码片段】(代码细节以此为准):
       ```
       {snippet 每行缩进 5 空格}
       ```
    ```
- `any(c.get("module") for c in top_candidates)` 为真 → 追加模块说明：
  ```
    （模块说明：mall-portal=前台门户、mall-admin=后台管理、mall-search=搜索、mall-auth=认证、mall-gateway=网关。判断前台/后台等归属请按上面标注的【模块】，不要仅凭类名/方法名臆断；若要对比的另一侧模块不在候选里，如实说"未检索到 X 模块的相关实体"。）
  ```

### 树形 `_render_tree_candidates(tree, code_snippets)`

**触发条件**（三条同时满足，见 §2 步骤 3）：`candidate_tree is not None` && `len(subtrees) >= 2` && `not fallback_to_flat`。

- 首行：`候选入口方法（按调用子树分组，每个子树按调用顺序）:`
- `code_snippets` 非空 → 插同款框架句
- 每棵子树（`enumerate(tree.subtrees, 1)`，前置空行）：
  ```
  【子树 {i}】入口: {root.entity_id}{mod_str}
    业务说明: {root.summary}        ← 仅 root.summary 真值时
    {_render_tree_children 缩进 ├─/└─}
    【真实源码片段】(代码细节以此为准):   ← 仅 root.code_snippet 真值时
    ```
    {root.code_snippet 每行缩进 2 空格}
    ```
  ```
  - **注意不对称**：子节点（`_render_tree_children`）只渲染 `entity_id` / `module` / `summary`，**不渲染**子节点自身的 `code_snippet`；只有子树根 `root.code_snippet` 被渲染。
- 孤儿附录（`tree.orphans` 非空）：
  ```
  【其他相关实体（未连入主路径）】
    - {entity_id}{mod_str}
      业务说明: {o.summary}        ← 仅 o.summary 真值时
  ```
- 元信息（`tree.notes` 非空）：每条 `【说明】{note}`

### `_render_tree_children(parts, children, prefix)`（递归缩进）

- 末项用 `└─`，中间项 `├─`
- 节点行：`  {prefix}{branch} {child.entity_id}{mod_str}`
- 业务说明（仅 summary 真值）：`  {prefix}{indent}业务说明: {child.summary}`，`indent` 末项 `"   "`（3 空格）、中间项 `"│  "`
- 递归 `new_prefix = prefix + ("   " if is_last else "│  ")`

---

## 6. 其他固定 system prompt

| 变量 | 用途 |
|---|---|
| `SYSTEM_PROMPT` | 6 段式 JSON 结构化输出（非 chat / QASynthesizer）。含 Step 1 视角表（13 行 view ID）、Step 2 严格规则 6 条、Step 3 六段（overview/entry_point/call_chain/db_ops/rules/sources）、Step 4 call_chain（v1.11 起首选 JSON nodes/edges schema + 兼容 Mermaid 4 类 classDef：`external`#585b70 / `entry`#89b4fa / `store`#a6e3a1 / `concern`#f38ba8）、Step 5 输出格式（合法 JSON、```json fenced）、缺信息处理（仍出 overview）|
| `_CHIT_CHAT_SYSTEM` | 闲聊路径，4 条回复原则（问候不延续话题 / 产品问询介绍 4 能力 / 通用编程直接答 / 无关问题轻松答）；身份信源优先级：第一信源 system 顶部记忆块、第二信源本轮历史用户声明、都没有才说「你还没告诉我」|
| `_TITLE_SUMMARY_SYSTEM` | 会话标题：≤15 汉字，直接输出标题本身（不解释/不引号/不标点结尾/不前缀），寒暄输出「日常问候」|
| `_SESSION_COMPACT_SYSTEM` | 会话级记忆压缩：≤300 字、中文、直接输出摘要，忠实保留既有事实并融合新信息（有变化标注演变）|
| `_MEM_L0_SYSTEM` | 文件式记忆 L0（可嵌入摘要）：≤约 100 token，聚焦用户稳定事实，严禁虚构（只复述输入已现事实）|
| `_MEM_L1_SYSTEM` | 文件式记忆 L1（导航图）：≤约 1500 字，聚成目录索引，严禁虚构 |
| `_MEM_EXTRACT_SYSTEM` | ReAct 记忆抽取：输出严格 JSON `{"memories":[{"kind","content","supersedes_kind"}]}`；只抽用户明确声明（绝不取助理回复内容）；疑问/澄清/引用返回空；kind=identity 时 supersedes_kind 必填 `'identity'`（禁 null）；无可记 `{"memories":[]}` |
| `HISTORY_SUMMARIZE_PROMPT` | 历史压缩（v1 暂未启用）：模板 `{history}` → 1-2 句概括 |
| `_MEMORY_BLOCK_TEMPLATE` | 记忆块包裹模板：`═══════ 记忆...═══════\n{block}\n═══...═══\n\n` |

---

## 7. 多轮历史相关函数

**`_format_history(history)`**: `history` 非 list/None/空 → `""`。否则取 `history[-10:]`，非 dict 项跳过，每条 `[{role}] {content[:200]}`；`role` 用 `m.get('role') or '?'` 兜底、`content` 用 `(m.get('content') or '')[:200]` 兜底（防 `None[:200]` TypeError）。`"\n".join(...)`。

**`build_chitchat_user_prompt(question, history=None)`**: `_format_history` 空 → 原样返回 `question`；非空 → `"【对话历史】\n{h}\n\n{question}"`。

**`build_user_prompt_with_history(question, context, history=None)`**: `not history` → 等价 `build_user_prompt(question, context)`（无 free_format 参数，默认 False）；有历史 → `"【对话历史】\n{_format_history(history)}\n\n{base}"`，`base = build_user_prompt(question, context)`。历史裁剪由 qa_router 按 token 预算预处理，`[-10:]` 仅冗余硬上限。

**`dump_user_prompt(question, context)`**（调试用）: 拼 prompt + `─── debug: raw context (truncated) ───` + `json.dumps(context, ensure_ascii=False, indent=2)[:500] + "…"`。

---

## 8. 外部依赖

- `CandidateTree`（`candidate_tree`）：需有 `.subtrees`（list）、`.fallback_to_flat`（bool）、`.orphans`（list）、`.notes`（list）。每个节点需有 `.entity_id`、`.module`、`.summary`、`.children`（list）、`.code_snippet`。**子树根的 `.code_snippet` 会被渲染，子节点的 `.code_snippet` 不渲染。**
- `ToolRegistry.list_tools()`（`react_synthesizer.py`）：返回 `list[Tool]`，每个 `Tool` 有 `.name`（str）、`.description`（str）；另 `_tools_to_openai_schema` 用到 `tool.input_schema`（jsonschema）。
- `with_memory_block`、`AGENT_SYSTEM_PROMPT` 由 `react_synthesizer.py` 导入。

---

## 9. 降级与边界

- **空 registry**：`_build_tool_usage_hint` 返回 `""`，调用方 `if tool_hint:` 跳过拼接，system 仅 base_system（无工具提示）。
- **memory_block 空/全空白**：`with_memory_block` 原样返回，零注入。
- **candidates 为空**：候选区插 `（向量库未命中任何候选实体）`，不抛错。
- **候选树未达阈值**（None / 单子树 / fallback_to_flat=True）：回退扁平渲染（零回归保旧行为）。
- **node_summaries 空**：跳过整个「调用链方法清单」段（信号太弱，交确定性兜底图）。
- **history 含 content=None 项**：`(... or '')[:200]` 兜底，不抛 TypeError。
- **table_access op/tid 缺失**：各自兜底 `"?"`。
- **summary 超 300 字**：截断 + `…`。

## 10. 怪癖

- `AGENT_SYSTEM_PROMPT` 内 `\\n`（源码双反斜杠）= 给 LLM 看的字面 `\n`，**非** Python 换行；移植 TS 时若用模板字符串需保留字面 `\n`（写成 `\\n`）。
- `free_format` 在 prompt 文本中造成三处分叉（call_edges header、画图指令、任务块），TS 实现须按布尔严格切两套文案。
- 源码框架句、模块说明、模块判断指引在扁平/树形两个函数中**各写一份**（树形未渲染模块说明指引，只有扁平末尾追加「模块说明」段）——这是不对称：树形分支**不输出**「（模块说明：mall-portal=…）」那段。
- `recalled_methods` 去重保序，刻意列**全部**召回方法（含无解读），防 LLM 见数量矛盾而不产 call_chain。
- `_build_tool_usage_hint` 物理上在 `react_synthesizer.py` 而非 `prompts.py`，但属同一 prompt 资产，移植时归一处。

---

# 【图类+store 类工具 handler】

All 7 schemas match both the source and the artifact. I've now verified every claim against source. Let me note the discrepancies I found:

1. The spec's schema JSON blocks **omit the `description: "返回结果上限"` / `"BFS 最大跳数"` fields** on `limit`/`max_nodes`/`max_depth` and the `description` on `direction`. The source schemas DO contain these. This is a minor omission in the spec's reproduced schema blocks (the prose claims "与 agent-tools-schema.json 一致" which is technically true at the structural level, but the reproduced JSON drops description fields). For a "唯一行为权威" doc, descriptions matter for the LLM tool-calling, so I'll correct.

2. The spec's `ToolHandler` type and signatures are accurate.

Let me also verify there's no `module_of` usage in these tools (spec lists it in GraphProto but says graph tools use successors/predecessors). And verify the `ke_search` description string is reproduced. Now I have everything. Producing the corrected spec.

---

## 修正清单

回源逐字核对后，发现 **2 处需修正**、其余抽查点全部命中。修正项：

1. **【schema 漏字段 — 已补全】** 原 spec 的 7 个 `input_schema` JSON 代码块**全部漏掉了数值字段的 `description`**。源码中 `limit`/`max_nodes` 带 `"description": "返回结果上限"`，`ke_impact.max_depth` 带 `"description": "BFS 最大跳数"`，`ke_impact.direction` 带 `"description": "down=下游影响闭包（改它影响谁）；up=上游依赖闭包（谁依赖它）"`。`docs/porting/agent-tools-schema.json` 里这些 description **存在**，所以原 spec「与 agent-tools-schema.json 一致 ✓」的结论本身正确，但 spec 自己复刻的 JSON 块漏抄了 description——对 TS 实现而言这些 description 是要逐字喂给 LLM 的工具定义，已在下方补回。

2. **【ke_read_entity / ke_method_interp 成功分支用 `.get()` 而非下标 — 已修正】** 原 spec 写「成功 → `record["name"]`」「`record["entity_type"]`」「`record["code_snippet"]`」(下标取值)。源码实际用 `record.get("name")` / `record.get("entity_type")` / `record.get("code_snippet")`——**字段缺失返回 `None` 而非抛 KeyError**。这是 fail-soft 语义差异，TS 须用可选取值（`record.name ?? null`）而非强制断言。已修正。

其余逐字抽查全部命中（无修正）：

- base.py 两处报错文案逐字一致：`f"工具重名: {tool.name!r} 已注册"` / `f"未注册的工具: {name!r}"`（注意 `!r` = repr，TS 须用单引号包裹 name）
- `Tool` 为 `@dataclass(frozen=True, slots=True)`；`ToolNotFound(LookupError)`；`call()` 用 `is None` 判空后 `await tool.handler(input)` 无包装
- 三个 Protocol（GraphProto/InterpretationStoreProto + _CodeStoreProto/_MethodInterpStoreProto）签名逐字一致
- `_TABLE_MENTION_RE = re.compile(r"([A-Za-z_]\w*)\s*表")`，`dict.fromkeys` 去重保序——逐字命中
- ke_impact 三个魔法数 `5 / 20 / 200` + BFS 双重 break + `visited` 不含起点 + `sorted(visited)`——逐行命中
- ke_search 工厂 `ValueError("build_ke_search_tool: project_id 不能为空")`——原 spec 只写「ValueError 拦截空字符串」，文案补全见下
- build_default_registry 注册顺序 13 工具逐条命中

---

# Tool Framework + Graph/Store Tool Handler Spec（修正版）

## 1. base.py — Tool 框架核心

### 组件用途
MCP 兼容工具抽象层。`Tool` 是不可变元数据+handler 容器；`ToolRegistry` 是 O(1) dispatch 的有序工具集；`ToolNotFound` 是 dispatch miss 异常。三者合计构成工具框架，不含任何业务逻辑。设计注释明确：Registry **不做缓存、不做重试、不做指标**，横切关注点故意外置（"v1.2 暂不上"）。

### 公开接口
```typescript
// Python 原型
type ToolHandler = (input: Record<string, any>) => Promise<Record<string, any>>
// Python 别名: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]   // JSONSchema
    handler: ToolHandler

class ToolNotFound(LookupError): pass

class ToolRegistry:
    __init__()                                  // self._tools: dict[str, Tool] = {}（插入有序）
    def list_tools() -> list[Tool]
    def register(tool: Tool) -> None
    def get(name: str) -> Tool | None
    async def call(name: str, input: dict) -> dict
```

### dispatch 语义（逐字）
- `register()`: 重名 → `raise ValueError(f"工具重名: {tool.name!r} 已注册")`。注意 `{name!r}` 是 Python repr，对 str 会加单引号（如 `工具重名: 'ke_search' 已注册`）。编程错误，早爆不吞。
- `call()`: `tool = self.get(name)`；`if tool is None: raise ToolNotFound(f"未注册的工具: {name!r}")`（同样 repr 加引号）；否则 `return await tool.handler(input)`，**不做任何包装/try-catch**。
- `get()`: `return self._tools.get(name)`，miss 返 `None`（仿 dict.get）。
- `list_tools()`: `return list(self._tools.values())`——按注册顺序的**拷贝**列表（外部改不影响内部）。Python 3.7+ dict 有序保证顺序 = 注册顺序，TS 须用 `Map` 或保序结构。

### 降级与边界
ToolRegistry 本身不捕获任何异常；handler 内部负责自己的容错。TS 实现须保持相同语义：`call()` 不 try/catch，让上层 (agent engine) 感知到 `ToolNotFound`（须继承自一个 lookup-error 语义的基类）。

---

## 2. build_default_registry — 装配工厂

**文件**: `tools/__init__.py`

**签名**:
```python
def build_default_registry(
    *,
    graph: GraphProto,
    interpretation_store: InterpretationStoreProto,
    project_id: str,
    code_store: Any | None = None,
    method_interp_store: Any | None = None,
    repo_local_path: str | None = None,
) -> ToolRegistry
```

**注册顺序**（固定，影响 `list_tools()` 给 LLM 的工具序）：

1. `ke_search` — `build_ke_search_tool(interpretation_store, project_id)`，闭包绑 `project_id` + `interpretation_store`
2. `ke_callees` — `build_ke_callees_tool(graph)`
3. `ke_callers` — `build_ke_callers_tool(graph)`
4. `ke_table_access` — `build_ke_table_access_tool(graph)`
5. `ke_impact` — `build_ke_impact_tool(graph)`
6. `render_call_graph` — `build_render_call_graph_tool(graph, summary_lookup=_summary_lookup)`
7. `todo_write` — `build_todo_write_tool()`，无依赖，始终注册
8. `ke_read_entity` — **仅当 `code_store is not None`**
9. `ke_method_interp` — **仅当 `method_interp_store is not None`**
10. `ke_grep` — `build_ke_grep_tool(repo_local_path)`（None 时 handler 自返 "source path not configured" error，**仍注册**）
11. `ke_glob` — 同上
12. `ke_read_file` — 同上
13. `ke_ls` — 同上

注意条件注册位置：`ke_read_entity` / `ke_method_interp` 在 `todo_write` **之后**、4 个文件类工具**之前**插入。当 store 为 None 时这两个工具不出现，注册序号顺移——`list_tools()` 给 LLM 的工具数量随之变化（11 或 12 或 13 个）。

**project_id 注入规则**: 只有 `ke_search` 由 `build_default_registry` 显式传入 `project_id`；其余 graph 类工具的 tenant 隔离由调用方在实例化 `GraphProto` adapter 时绑定（如 `Neo4jGraphAdapter(..., project_id=...)`），不由本工厂经手。

**`_summary_lookup` 内联闭包**:
```python
def _summary_lookup(entity_id: str) -> str
```
逻辑：`try: hit = interpretation_store.get_by_entity(entity_id)` → `except Exception: return ""` → `if not hit: return ""` → `return hit.get("interpretation_text") or ""`。任何异常 / None / 字段缺均返空串（fail-soft）。供 `render_call_graph` 中文化节点 label。注意取的字段是 **`interpretation_text`**，且 `get_by_entity` 只传 1 个位置参数（不传 level）。

---

## 3. 依赖 Protocol 接口（逐字命中）

定义在 `src/service/qa_engine/retriever.py`。

### GraphProto
```python
class GraphProto(Protocol):
    def successors(self, entity_id: str, rel_type: str | None = None) -> list[str]: ...
    def predecessors(self, entity_id: str, rel_type: str | None = None) -> list[str]: ...
    def module_of(self, entity_id: str) -> str | None: ...
```
- `successors(entity_id)` — 无 rel_type 时返回所有出边邻居
- `successors(entity_id, rel_type="accesses_table")` — 过滤特定关系类型（仅 `ke_table_access` 用）
- `predecessors` — 上游入边邻居，同 rel_type 语义
- `module_of` — 本批 graph/store 工具 handler **均不调用**，仅 Protocol 契约保留

### InterpretationStoreProto
```python
class InterpretationStoreProto(Protocol):
    def search_method_hits_by_text(
        self, *, text: str, project_id: str, limit: int = 5
    ) -> list[dict[str, Any]]: ...

    def get_by_entity(
        self, entity_id: str, level: str | None = None
    ) -> dict[str, Any] | None: ...
```
注意 `search_method_hits_by_text` 三个参数**全部 keyword-only**（`*` 之后），TS 实现入参须是命名对象。

### _CodeStoreProto (ke_read_entity 专用，定义在 ke_read_entity.py)
```python
class _CodeStoreProto(Protocol):
    def get_by_entity_id(self, entity_id: str) -> Optional[dict[str, Any]]: ...
```

### _MethodInterpStoreProto (ke_method_interp 专用，定义在 ke_method_interp.py)
```python
class _MethodInterpStoreProto(Protocol):
    def get_by_method_id(self, method_entity_id: str) -> Optional[dict[str, Any]]: ...
```

---

## 4. ke_search

**用途**: 在拓扑解读库（TopologicalInterpretation）向量语义检索代码实体候选列表。

**工厂**: `build_ke_search_tool(store: InterpretationStoreProto, project_id: str) -> Tool`

**关键设计怪癖**: `project_id` 从 schema 中移除，由工厂闭包注入。即使 LLM 传入 `project_id` 字段也被完全忽略，强制使用闭包值。v1.3（2026-05-26）修复原因：mall-swarm 实测 LLM 猜 project_id 时产生 "pms"/"pms-product" 等错误 tenant → 0 结果。

**工厂校验（逐字）**: `if not project_id or not project_id.strip(): raise ValueError("build_ke_search_tool: project_id 不能为空")`，随后 `bound_project_id = project_id.strip()`（绑定值已 trim）。

**input_schema**（源码 = 工件，逐字）:
```json
{
  "type": "object",
  "properties": {
    "query": { "type": "string", "description": "自然语言查询（中文优先）" },
    "limit": { "type": "integer", "description": "返回结果上限", "default": 5, "minimum": 1, "maximum": 50 }
  },
  "required": ["query"]
}
```

**description（工具级，逐字）**: `"在拓扑解读库（TopologicalInterpretation）语义检索代码实体（method / class / module / api）；project_id 已由后端绑定，无需提供。"`

**handler 逻辑**:
1. `query = (input.get("query") or "").strip()` — 空/None/空白均视为缺失
2. 空 query → `{ query, results: [], error: "missing required field: query" }`（注意此处返回的 `query` 是 strip 后的空串 `""`，非 None）
3. `limit = int(input.get("limit", 5))`，`except (TypeError, ValueError): limit = 5` — 类型容错
4. `store.search_method_hits_by_text(text=query, project_id=bound_project_id, limit=limit)` — project_id 强制用闭包值（keyword 调用）
5. 后端 `except Exception as e` → `{ query, results: [], error: f"search backend error: {e}" }`
6. 成功 → `{ query, results: list(results) }` — results 原样透传，**不做 reranking/过滤**

---

## 5. ke_read_entity

**用途**: 按 entity_id 从 Weaviate CodeEntity store 读 `{name, entity_type, code_snippet}`。

**工厂**: `build_ke_read_entity_tool(code_store: _CodeStoreProto) -> Tool`

**input_schema**（逐字）:
```json
{
  "type": "object",
  "properties": {
    "entity_id": { "type": "string", "description": "实体 ID，形如 OmsPortalOrderServiceImpl::generateOrder#(OrderParam)（qualified_name 形态）" }
  },
  "required": ["entity_id"]
}
```

**description（工具级，逐字）**: `"读取某个代码实体（method/class）的源代码片段、名称与类型。"`

**handler 逻辑**:
1. `entity_id = input.get("entity_id")` — falsy → `{ entity_id: null, name: null, entity_type: null, code_snippet: null, error: "missing required field: entity_id" }`
2. `code_store.get_by_entity_id(entity_id)`
3. 后端 `except Exception as e` → `{ entity_id, name: null, entity_type: null, code_snippet: null, error: f"code store error: {e}" }`
4. `record is None` → `{ entity_id, name: null, entity_type: null, code_snippet: null, error: "entity not found in code store" }`
5. **【修正】** 成功 → `{ entity_id, name: record.get("name"), entity_type: record.get("entity_type"), code_snippet: record.get("code_snippet") }` — 用 **`.get()`**，字段缺失返 `None`（非 KeyError）。TS 须用 `record.name ?? null` 等可选取值，不要强断言。

**怪癖**: 设计注释逐字 `"⚠️ 多租户：CodeEntity store 全局按 entity_id 查，无 project_id 隔离（见设计 §4.2 缺口）。"`。TS 实现须保持此行为（缺口待修，当前逻辑无 tenant 过滤）。

**返回 schema**（4 字段固定出现，无论成败）:
```
{ entity_id: string|null, name: string|null, entity_type: string|null, code_snippet: string|null, error?: string }
```

---

## 6. ke_callers

**用途**: 图谱上游 1 跳 — 查询谁调用了指定实体。

**工厂**: `build_ke_callers_tool(graph: GraphProto) -> Tool`

**input_schema**（逐字，含 max_nodes description）:
```json
{
  "type": "object",
  "properties": {
    "entity_id": { "type": "string", "description": "实体 ID，形如 OmsPortalOrderServiceImpl::generateOrder#(OrderParam)（qualified_name 形态）" },
    "max_nodes": { "type": "integer", "description": "返回结果上限", "default": 10, "minimum": 1, "maximum": 100 }
  },
  "required": ["entity_id"]
}
```

**description（工具级，逐字）**: `"查询某个代码实体（method / class）的上游调用 —— 谁调用了它。"`

**handler 逻辑**:
1. entity_id falsy → `{ entity_id: null, callers: [], error: "missing required field: entity_id" }`
2. `max_nodes = int(input.get("max_nodes", 10))`，`except (TypeError, ValueError): max_nodes = 10`
3. `callers = list(graph.predecessors(entity_id))[:max_nodes]` — 上游用 predecessors（不传 rel_type），截到 max_nodes
4. 后端 `except Exception as e` → `{ entity_id, callers: [], error: f"graph backend error: {e}" }`
5. 成功 → `{ entity_id, callers: string[] }`

**注意**: 切片 `[:max_nodes]` 只取图层 1 跳邻居，不做 BFS。

---

## 7. ke_callees

**用途**: 图谱下游 1 跳 — 查询指定实体调用了哪些方法/类。

**工厂**: `build_ke_callees_tool(graph: GraphProto) -> Tool`

**input_schema**: 与 ke_callers 完全对称（含 `max_nodes` 的 `"description": "返回结果上限"`、default 10 / min 1 / max 100），字段名 `callees`。

**description（工具级，逐字）**: `"查询某个代码实体（method / class）的下游调用 —— 它调用了哪些方法 / 类。"`

**handler 逻辑**: 与 ke_callers 完全对称，调 `graph.successors(entity_id)`（不传 rel_type）替代 `predecessors`，返回字段 `callees`。
```
成功: { entity_id: string, callees: string[] }
失败: { entity_id: string|null, callees: [], error: string }
```

---

## 8. ke_impact

**用途**: 多跳 BFS 影响闭包分析。`direction=down` 沿 successors 求下游影响面；`direction=up` 沿 predecessors 求上游依赖面。

**工厂**: `build_ke_impact_tool(graph: GraphProto) -> Tool`

**魔法数字/护栏**（模块级常量，逐字）:
```python
_DEFAULT_MAX_DEPTH = 5
_MAX_DEPTH_CAP = 20
_DEFAULT_MAX_NODES = 200   # BFS visited 节点总数上限（硬编码，不暴露给 LLM）
```

**input_schema**（逐字，含 direction/max_depth description）:
```json
{
  "type": "object",
  "properties": {
    "entity_id": { "type": "string", "description": "起始实体 ID，形如 OmsPortalOrderServiceImpl::generateOrder#(OrderParam)（qualified_name 形态）" },
    "direction": { "type": "string", "description": "down=下游影响闭包（改它影响谁）；up=上游依赖闭包（谁依赖它）", "enum": ["down","up"], "default": "down" },
    "max_depth": { "type": "integer", "description": "BFS 最大跳数", "default": 5, "minimum": 1, "maximum": 20 }
  },
  "required": ["entity_id"]
}
```

**description（工具级，逐字）**: `"影响分析：给一个代码实体（method/class），沿调用关系做多跳 BFS 闭包。direction=down 求下游影响面（改它会波及谁）；up 求上游依赖面（谁依赖它）。"`

**handler 逻辑**:
1. entity_id falsy → `{ entity_id: null, direction: "down", count: 0, nodes: [], error: "missing required field: entity_id" }`
2. `direction = input.get("direction")`；`if direction != "up": direction = "down"` — 非严格枚举容错（任何非 "up" 值归 down）
3. `max_depth = int(input.get("max_depth", 5))`，`except (TypeError, ValueError): max_depth = 5`；再 `max_depth = max(1, min(max_depth, 20))` 夹到 `[1, 20]`
4. `neighbors = graph.successors if direction == "down" else graph.predecessors`
5. **BFS 实现细节**（在 try 块内）:
   - `visited: set[str] = set()` — **不含起点**
   - `queue: deque[(str,int)] = deque([(entity_id, 0)])`；起点 depth=0
   - `seen = {entity_id}` — 起点只进 `seen`，**不进 `visited`**
   - 循环 `node, depth = queue.popleft()`；`if depth >= max_depth: continue`（不展开该节点）
   - 对每个 `nxt in neighbors(node)`：`if nxt in seen: continue`；否则 `seen.add(nxt)` + `visited.add(nxt)`；`if len(visited) >= 200: break`（内层 for break，**break 之前已 add，不 append 队列**）；否则 `queue.append((nxt, depth+1))`
   - 内层 for 结束后再判 `if len(visited) >= 200: break`（外层 while break）
   - 结果 `nodes = sorted(visited)` — 字典序
6. 后端 `except Exception as e` → `{ entity_id, direction, count: 0, nodes: [], error: f"graph backend error: {e}" }`
7. 成功 → `{ entity_id, direction, count: len(nodes), nodes: string[] }`

**怪癖**: `_DEFAULT_MAX_NODES=200` 不在 schema 中暴露，LLM 只能控制 depth，不能控制总节点数。起点本身不在 `nodes` 输出中；`count == len(nodes) == len(visited)`。截断细节：触发 200 上限的那个节点已计入 visited 但不再入队（其下游不再展开）。

---

## 9. ke_method_interp

**用途**: 按 method entity_id 从 Weaviate MethodInterpretation store 读完整技术解读记录（Mode A）。

**工厂**: `build_ke_method_interp_tool(interp_store: _MethodInterpStoreProto) -> Tool`

**input_schema**（逐字）:
```json
{
  "type": "object",
  "properties": {
    "entity_id": { "type": "string", "description": "方法实体 ID，形如 OmsPortalOrderServiceImpl::generateOrder#(OrderParam)（qualified_name 形态）" }
  },
  "required": ["entity_id"]
}
```

**description（工具级，逐字）**: `"读取某个方法（qualified_name 形如 Cls::method#(params)）的技术解读：它做什么、关键逻辑、上下文。"`

**handler 逻辑**:
1. entity_id falsy → `{ entity_id: null, interpretation: null, error: "missing required field: entity_id" }`
2. `interp_store.get_by_method_id(entity_id)`
3. 后端 `except Exception as e` → `{ entity_id, interpretation: null, error: f"method interp store error: {e}" }`
4. `record is None` → `{ entity_id, interpretation: null, error: "method interpretation not found" }`
5. 成功 → `{ entity_id, interpretation: record }` — record 完整 dict 原样透传

**怪癖**: 设计注释逐字 `"⚠️ 多租户：store 全局按 method_entity_id 查，无 project_id 隔离（见设计 §4.2 缺口）。"`

---

## 10. ke_table_access

**用途**: 查询代码实体访问的数据库表。双源合并去重：①图边 `accesses_table` ②`summary_text` 正则提取。

**工厂**: `build_ke_table_access_tool(graph: GraphProto) -> Tool`

**input_schema**（逐字）:
```json
{
  "type": "object",
  "properties": {
    "entity_id": { "type": "string", "description": "实体 ID（一般是 method 级，类 / 模块也支持）" },
    "summary_text": { "type": "string", "description": "可选；如果传入，会从中提取『<table> 表』模式作为补充来源" }
  },
  "required": ["entity_id"]
}
```

**description（工具级，逐字）**: `"查询某个代码实体访问的数据库表（图边 + summary_text 双源合并去重）。"`

**handler 逻辑**:
1. `entity_id = (input.get("entity_id") or "").strip()` — 空/None/空白 → `{ tables: [], error: "missing required field: entity_id" }`（**错误返回中无 `entity_id` 字段**，与其他工具不同）
2. **来源 1 — 图边**:
   - `tables: list = []`，`seen: set = set()`
   - `for tid in graph.successors(entity_id, rel_type="accesses_table"):`，`if tid and tid not in seen:` → `tables.append({"table_id": tid, "operation": "unknown"})` + `seen.add(tid)`
   - `except Exception: pass` — 图后端异常**静默跳过**，**不写 error 字段**，继续走来源 2
3. **来源 2 — summary_text**:
   - `summary_text = input.get("summary_text") or ""`；仅当非空
   - `for tid in QARetriever._extract_tables_from_text(summary_text):`，`if tid not in seen:` → `tables.append({"table_id": tid, "operation": "mentioned"})` + `seen.add(tid)`
4. 成功 → `{ entity_id, tables: list[{ table_id: string, operation: "unknown"|"mentioned" }] }`

**依赖**: 直接引用 `QARetriever._extract_tables_from_text`（`@classmethod`，定义于 retriever.py:349）。TS 实现须内联此逻辑，不依赖 retriever 模块。

**正则规则（逐字）**: `_TABLE_MENTION_RE = re.compile(r"([A-Za-z_]\w*)\s*表")`。`_extract_tables_from_text` 逻辑：`if not text: return []`；`raw = cls._TABLE_MENTION_RE.findall(text)`（findall 返回捕获组列表）；`return list(dict.fromkeys(raw))`（去重保留首次出现顺序）。TS 等价：`/([A-Za-z_]\w*)\s*表/g`，提取捕获组 1，按首次出现去重。`\w` 在 Python 默认含 Unicode，但首字符限定 `[A-Za-z_]`、捕获组仅 ASCII 标识符场景；TS 用 `\w`（ASCII）即可对齐 ASCII 标识符，保险起见可用 `[A-Za-z_][A-Za-z0-9_]*`。

**操作字段语义**:
- `operation: "unknown"` — 来自图边（有访问关系，但操作类型图中未记录）
- `operation: "mentioned"` — 来自 summary_text 文本提取（更弱信号）

---

## schema 与 agent-tools-schema.json 差异核查

实跑对比脚本：源码 7 个工具 input_schema **逐字段（含 description / default / minimum / maximum / enum / required）与 `docs/porting/agent-tools-schema.json` 完全一致**。

| 工具 | schema 一致 | 备注 |
|------|------------|------|
| ke_search | ✓ | limit description="返回结果上限"，5/1/50 |
| ke_read_entity | ✓ | |
| ke_callers | ✓ | max_nodes description="返回结果上限"，10/1/100 |
| ke_callees | ✓ | 同 callers，对称 |
| ke_impact | ✓ | direction/max_depth description 齐全；default 5 / cap 20 |
| ke_method_interp | ✓ | |
| ke_table_access | ✓ | |

（原 spec 漏抄的 description 字段已在上方各节补回。）

---

## 全局怪癖与 TS 移植注意

1. **project_id 双轨制**: `ke_search` 由 registry 工厂传入并闭包绑定（工厂对空串 `raise ValueError`）；graph 类工具（ke_callees/ke_callers/ke_impact/ke_table_access）的 tenant 由 GraphProto 适配器**实例级**绑定，不经工具 handler。TS 须保持此分工。

2. **`ke_read_entity` / `ke_method_interp` 条件注册**: 两个 store 均 Optional，`None` 时工具**不注册**到 registry（影响 list_tools 工具数量与序号）。TS 须保持按需注册，不要空实现占位。

3. **所有 graph/store 工具 handler 均不抛异常**: 任何错误通过返回 dict 的 `error` 字段告知 LLM。TS handler 签名应返回 `Promise<Record<string, unknown>>`，内部全部 try/catch。唯一抛异常的是工厂层 `build_ke_search_tool` 的空 project_id `ValueError`（构造期，非运行期）。

4. **graph.successors 的 rel_type 参数**: 仅 `ke_table_access` 用 `rel_type="accesses_table"`；其余工具不传 rel_type（取所有出边）。TS GraphProto 接口须支持可选 `relType` 参数（缺省 = 全部出边）。

5. **ke_impact BFS 起点不在输出 nodes 中**: `visited` 初始化空，起点只进 `seen`。`count == len(nodes) == len(visited)`。200 节点上限触发时，触发节点已计入但其下游不再展开。

6. **ke_table_access 图后端异常静默**: 与其他工具不同，图边来源 `except: pass`，**不写 error 字段**，让 summary_text 兜底。TS 须保持此差异（不要统一为返回 error）。此外 ke_table_access 的 missing-field 错误返回**不含** entity_id 字段（仅 `{tables:[], error}`），也与其他工具不同。

7. **`QARetriever._extract_tables_from_text`**: ke_table_access 直接调 retriever 私有 `@classmethod`。TS 实现应将正则逻辑内联到 ke_table_access handler，不依赖 retriever 模块。正则 `/([A-Za-z_]\w*)\s*表/g`，提取捕获组 1，`dict.fromkeys` 去重保序。空 text 返 `[]`。

8. **`!r`（repr）报错文案**: base.py 两条报错均用 `{name!r}`，对字符串会**加单引号**（`'ke_x'`）。TS 实现报错文案须复刻引号包裹以保持逐字一致。

9. **成功分支取值用 `.get()`**: ke_read_entity 三字段、_summary_lookup 的 interpretation_text 均用 `.get()` 软取（缺失返 None/空串）。TS 须用可选取值 `??`，不要强制断言/下标。

---

源码路径（全部只读）：
- 框架: `/Users/java/knowledge-engineering/src/service/qa_engine/tools/base.py`、`/Users/java/knowledge-engineering/src/service/qa_engine/tools/__init__.py`
- 工具: `ke_search.py` / `ke_read_entity.py` / `ke_callers.py` / `ke_callees.py` / `ke_impact.py` / `ke_method_interp.py` / `ke_table_access.py`（同目录）
- Protocol + 正则: `/Users/java/knowledge-engineering/src/service/qa_engine/retriever.py`（L59-82 Protocol，L346 `_TABLE_MENTION_RE`，L349-364 `_extract_tables_from_text`）
- 对照工件: `/Users/java/knowledge-engineering/docs/porting/agent-tools-schema.json`（13 工具，7 个本批工具 schema 逐字符核对一致）

---

# 【文件类+render+todo 工具】

I now have all the information needed. I have verified every claim against source. Here is the corrected, complete specification.

---

## 修正清单

逐条回源核验，发现以下需修正/补强点（其余条目抽查无误）：

1. **【重要·§六 §八】`render_call_graph` 的导入边界**：spec 未明确，源码只从 `synthesizer` import 三个符号 `_build_call_chain_section_from_edges` / `_cc_label` / `_CALLCHAIN_MAX_NODES`。`_cc_kind`、节点的 `classOf`/`method`/`entityId` 字段**不是** render_call_graph 自己写的，而是模式 A 完全委托给 `_build_call_chain_section_from_edges` 内部生成。spec 把 `_cc_kind` 当成 render_call_graph 的规则描述会误导 TS 实现——它属于 synthesizer 的调用链构建器。
2. **【重要·§六 模式 A Node 结构】**：spec 列出 `kind: "controller"|"service"|"mapper"|"method"`，但漏了模式 A 节点还带 `method`（`"ShortClass.methodName"`，去包名短类名）、`classOf`（**类全名，含包名**，由 `_cc_class_of` 产，不是短名）、`entityId`（`"method://{nid}"`，**模式 A 恒有**，非 optional）。spec 把 `classOf` 标"仅模式 A"对，但 `entityId` 在模式 A 也是恒有，spec 标 optional 不准确。
3. **【重要·§六 `_cc_kind` 规则】**：源码先 `_cc_class_of(entity_id).rsplit(".", 1)[-1]` 取**短类名（去包名）**再判后缀。spec 表格"类名后缀"未点明先去包名这一步。
4. **【§八 Tool dataclass】**：spec 说 `frozen=True`，源码是 `@dataclass(frozen=True, slots=True)`——漏了 `slots=True`（用 `__slots__` 省内存，且禁止动态加属性）。
5. **【§六 模式 B schema 字段描述逐字】**：`nodes` 描述里 kind 取值写的是 `controller/service/dao/method`（用 `dao` 不是 `mapper`）；`edges` 描述端点用 `{source,target,label}`。schema items 的 `required`：node=`["id","label"]`，edge=`["source","target"]`。spec 漏了这些 required。
6. **【§六 summary 文案逐字】**：spec 多处 summary 文案是改写，需逐字订正（见下方修正稿，含"未找到…的调用关系""…的调用关系均为框架噪声，未生成图""图后端异常，未生成调用图""nodes 为空，未渲染图"等）。spec §怪癖里写的 `"均为框架噪声，未生成图"` 与 `"图后端异常，未生成调用图"` 都不是完整原文。
7. **【§六 模式判定】**：源码用 `if input.get("nodes")`（truthy 判断，空 list `[]` 会落到模式 A），spec 写 `input.get("nodes") 有值`——需精确为"truthy"（空数组不触发模式 B）。
8. **【§六 错误信号文案】**：模式 A 既无 entity_id 又无 nodes，返回 `summary="缺少 entity_id 或 nodes，无法渲染图"`, `error="need entity_id or nodes"`。spec 只写了 error，漏了 summary 原文。
9. **【§一 沙箱抛错文案】**：核对一致，逐字无误（三条文案均原文匹配）。
10. **【§五 ke_read_file 二进制检测顺序】**：spec 说"先严格 decode 失败再 replace 降级"——但实际**先做 `_looks_binary` NUL 检测拒绝**，再 decode；decode 阶段才是两段式。spec §核心流程顺序对（5 二进制→6 解码），但 §边界怪癖表述"UTF-8 解码两阶段"易让人以为 binary 检测也在 decode 里，需澄清两者是独立两步。
11. **【§七 todo_write】**：核对一致。description 逐字订正见下。
12. **抽查无误**：所有 `max/min` 夹取区间、`GREP_TIMEOUT_SEC=5`、`--max-count 1000`、`MAX_FILE_SIZE_BYTES`、`8192`、`_SKIP_DIRS`、`_DEFAULT_DEPTH=2`/`_MAX_DEPTH=4`/`_MAX_EDGES=60`/`_CALLCHAIN_MAX_NODES=18`、`_SEMANTIC_EDGE_KEYWORDS` 全量关键词、`_normalize_to_qn` 五步规则、`_node_qn` 四字段优先级、`_filter_unsupported_edges` fail-safe 逻辑、`line_end` 空切片怪癖——均与源码一致。

---

# 文件类工具 + render_call_graph + todo_write 规范提取（修正稿）

## 〇、`Tool` 基类与工厂模式共性（先读，§八前置）

**文件** `/src/service/qa_engine/tools/base.py`

`Tool` 是 **`@dataclass(frozen=True, slots=True)`**（注意 `slots=True`，spec 漏写）：
```
Tool:
  name: str
  description: str
  input_schema: dict[str, Any]          # JSONSchema
  handler: ToolHandler
```
- `ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]`，即 `async (input: dict) -> dict`
- `frozen=True`：构造后字段不可改；`slots=True`：用 `__slots__`，省内存且禁止动态加属性
- 所有工具均为**工厂函数**（`build_xxx_tool(...) -> Tool`），用**闭包**绑定后端依赖（`bound_repo` / `graph` / `summary_lookup`）
- 错误约定：handler **返回 `{error: str}` dict，不抛异常**（工具层边界），调用方（ReAct loop）据此决策
- 注册顺序（`build_default_registry`）：search/callees/callers/table_access/impact → render_call_graph → todo_write → read_entity/method_interp（条件）→ grep/glob/read_file/ls

---

## 一、路径沙箱 `_path_sandbox.resolve_safe_path`

**文件** `/src/service/qa_engine/tools/_path_sandbox.py`

### 公开函数
```
resolve_safe_path(repo_local_path: str | None, relative_path: str) -> Path
```

**入参**
- `repo_local_path`：DB `projects.repo_local_path`，项目源码本机绝对路径；`None`/空字符串 → 未配置
- `relative_path`：LLM 传入的 project-relative 路径，如 `mall-admin/pom.xml`

**返回**：已确认在 repo 内的绝对 `Path`

**抛 `ValueError`（三种，文案逐字原文）**

| 场景 | 错误文案（逐字） |
|---|---|
| `not repo_local_path`（None/空） | `source path not configured for this project` |
| `Path(relative_path).is_absolute()` | `path must be project-relative, got absolute: {relative_path}` |
| 解析后越界（`relative_to` 失败） | `path out of repo boundary: {relative_path}` |

### 核心流程（顺序严格）
1. `if not repo_local_path` → 抛①
2. `rel = Path(relative_path)`；`if rel.is_absolute()` → 抛②
3. `root = Path(repo_local_path).resolve()`；`target = (root / rel).resolve()`（`resolve()` = `realpath()`，跟随 symlink + 解 `../`）
4. `try: target.relative_to(root) except ValueError:` → 抛③（symlink 逃逸在此被拦）
5. `return target`

---

## 二、`ke_grep` 工具

**文件** `/src/service/qa_engine/tools/ke_grep.py` → `build_ke_grep_tool(repo_local_path: str | None) -> Tool`

### Input Schema（逐字）

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `pattern` | string | 是（`required:["pattern"]`） | — | PCRE 正则；desc：`正则表达式（PCRE 风格，如 'RedisTemplate\|JedisPool'）` |
| `glob` | string | 否 | （无 schema default，handler 取 `""`） | desc：`文件名 glob 过滤（默认搜全部文件，例如 '**/*.java' 只搜 Java 文件）` |
| `case_sensitive` | boolean | 否 | `false` | — |
| `max_results` | integer | 否 | `50` | `minimum:1, maximum:200` |

> 注：`glob` 在 schema 中**无 `default`**，仅由 handler `(input.get("glob") or "").strip()` 兜底 `""`。spec 写默认 `""` 属行为正确但非 schema 声明。

### 返回结构
**成功**：`{matches: [{path, line, text}], truncated: bool, total_count: int}`
- `path`：相对 repo root（`abs_path.startswith(bound_repo)` 时切 `abs_path[len(bound_repo):].lstrip("/")`）
- `line`：1-based（`data.line_number`，缺省 `0`）
- `text`：`data.lines.text` 已 `.rstrip("\n")`

**失败**：`{error: str, matches: []}`

### 魔法数字与护栏 / 错误文案（逐字）
- `GREP_TIMEOUT_SEC = 5`
- rg 固定 `--max-count 1000`（单文件最多 1000 行）
- `max_results` 夹 `max(1, min(max_results, 200))`；非法（TypeError/ValueError）→ `50`
- 错误文案：
  - 未配置：`source path not configured for this project`（+`matches:[]`）
  - pattern 空：`missing required field: pattern`
  - 超时：`ke_grep timeout (>{GREP_TIMEOUT_SEC}s)`（即 `ke_grep timeout (>5s)`）
  - rg 未装：`ripgrep not installed (run: brew install ripgrep)`
  - 兜底：`{type(e).__name__}: {e}`

### 实现要点
- `cmd = ["rg", "--json", "-e", pattern]`；非 case_sensitive 时 `append("--ignore-case")`；有 glob 时 `extend(["-g", glob])`；再 `extend(["--max-count", "1000", "--"])`；最后 `append(bound_repo)`（`shell=False`）
- `asyncio.to_thread(_run_rg)` 包同步 `subprocess.run(cmd, capture_output=True, text=True, timeout=5)`
- 逐行 `json.loads`，跳空行 + `JSONDecodeError`；只取 `type=="match"`
- `len(matches) >= max_results` → break；`truncated = len(matches) >= max_results`
- 异常分支顺序：`TimeoutExpired` → `FileNotFoundError` → 兜底 `Exception`

### 外部依赖
- `ripgrep >= 13.0`（系统级，`brew install ripgrep`），无 pip 依赖

---

## 三、`ke_glob` 工具

**文件** `/src/service/qa_engine/tools/ke_glob.py` → `build_ke_glob_tool(repo_local_path) -> Tool`

### Input Schema

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `pattern` | string | 是（`required:["pattern"]`） | — | desc：`glob pattern，如 '**/Mapper/*.xml' 或 'mall-admin/**/*Controller.java'` |
| `head` | integer | 否 | `50` | `minimum:1, maximum:200` |

### 返回结构
**成功**：`{files: string[], truncated: bool, total_count: int}`
- `files`：相对 repo root 的 POSIX 路径（`p.relative_to(root).as_posix()`），**末尾 `files.sort()`**
- 仅 `p.is_file() == True`（过滤目录/symlink-to-dir）

**失败**：`{error: str, files: []}`

### 实现要点 / 护栏
- `Path(bound_repo).glob(pattern)` 生成器懒求值，`len(files) >= head` 即 break（不耗尽生成器，**注意：break 在 sort 之前**，故是"前 head 个磁盘遍历序"再排序，非"全量排序取前 head"）
- `head` 夹 `[1,200]`，非法 → `50`
- `truncated = len(files) >= head`
- 错误文案：未配置 `source path not configured for this project`；pattern 空 `missing required field: pattern`；兜底 `{type(e).__name__}: {e}`
- **沙箱**：`_path_sandbox` **未在此工具调用**，靠 `Path(bound_repo).glob` 作用域天然限制（glob 不跨 root；但 `**` 仍可能遇 symlink，无显式边界检查）

---

## 四、`ke_ls` 工具

**文件** `/src/service/qa_engine/tools/ke_ls.py` → `build_ke_ls_tool(repo_local_path) -> Tool`

### Input Schema（**无 `required`**，全可选）

| 字段 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `path` | string | `"."` | desc：`项目相对目录路径` |
| `depth` | integer | `1` | `minimum:1, maximum:3` |
| `include_hidden` | boolean | `false` | — |

### 返回结构
**成功**：`{path: string, entries: Entry[]}`（`path` 回显**请求的 relative path 原值**，非 resolve 后）

`Entry`：
```
{ name: string,            // child.relative_to(root).as_posix()，相对 repo root
  type: "dir" | "file",
  size?: number }          // 仅 file；child.stat().st_size，OSError 时缺省（不是仅"stat 失败缺省"——目录恒无 size）
```

**失败**：`{error: str, entries: []}`

### 跳过目录集合
```
_SKIP_DIRS = {"node_modules", "target", "dist", ".git"}
```

### 核心流程
1. `path = (input.get("path") or ".").strip() or "."`
2. `resolve_safe_path(bound_repo, path)`；`ValueError` → `{error: str(e), entries: []}`
3. `not target.exists()` → `directory not found: {path}`；`not target.is_dir()` → `not a directory: {path}`
4. `depth` 夹 `[1,3]`（非法 → `1`）；`include_hidden = bool(...)`
5. `root = Path(bound_repo)`；内层 `_walk(d, current_depth)` 递归：
   - `current_depth > depth` → return
   - `sorted(d.iterdir())`；`PermissionError` → 静默 return
   - 过滤：`not include_hidden and child.name.startswith(".")` → skip；`child.name in _SKIP_DIRS` → skip（**注意：隐藏判断在 `_SKIP_DIRS` 判断之前**，但 `.git` 两条都会命中）
   - `child.is_dir()` 才递归 `_walk(child, current_depth + 1)`
6. `_walk(target, 1)` 起始

---

## 五、`ke_read_file` 工具

**文件** `/src/service/qa_engine/tools/ke_read_file.py` → `build_ke_read_file_tool(repo_local_path) -> Tool`

### Input Schema

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `path` | string | 是（`required:["path"]`） | — | desc：`项目相对路径，如 'mall-admin/pom.xml'` |
| `offset` | integer | 否 | `0` | `minimum:0`，desc：`起始 line（0-based）` |
| `limit` | integer | 否 | `200` | `minimum:1, maximum:1000` |

### 返回结构
**成功**：
```
{ path: string,            // 请求的相对路径
  content: string,         // "\n".join(selected)
  line_start: number,      // 0-based = offset
  line_end: number,        // end_idx-1 if end_idx>offset else offset
  eof: boolean,            // end_idx >= total_lines
  total_lines: number }
```
**失败**：`{error: string}`（无 `content`）

### 魔法数字与护栏 / 错误文案（逐字）
- `MAX_FILE_SIZE_BYTES = 1024 * 1024`（1 MB）；超出：`file too large ({size} bytes > {MAX_FILE_SIZE_BYTES} max)`
- `_looks_binary(content_bytes)`：`b"\x00" in content_bytes[:8192]`（前 8 KB 含 NUL）→ `binary file not supported`
- `offset = max(0, int(...))`，非法 → `0`；`limit` 夹 `[1,1000]`，非法 → `200`
- 其余文案：path 空 `missing required field: path`；不存在 `file not found: {path}`；非普通文件 `not a regular file: {path}`；读失败 `read failed: {type(e).__name__}: {e}`；沙箱 `str(e)`

### 核心流程（顺序严格）
1. path 取值/strip，空 → 报错
2. `resolve_safe_path`（`ValueError` → `{error: str(e)}`）
3. `not exists()` → 报错；`not is_file()` → `not a regular file`
4. `stat().st_size > 1MB` → 拒绝
5. `target.read_bytes()`（异常 → `read failed: ...`）
6. **先** `_looks_binary(raw)` NUL 检测 → 拒绝（独立一步，与下方 decode 无关）
7. **再** decode：`raw.decode("utf-8")`，`UnicodeDecodeError` → `raw.decode("utf-8", errors="replace")`（U+FFFD 降级）
8. `all_lines = text.splitlines()`；`end_idx = min(offset+limit, total_lines)`；`selected = all_lines[offset:end_idx]`；`content = "\n".join(selected)`

### 边界怪癖
- **空切片时 `line_end = offset`**（而非 `offset-1`）——`offset >= total_lines` 时返回 `line_end == line_start == offset`
- 二进制检测与 UTF-8 解码是**两个独立步骤**：NUL 检测拒掉大多数二进制；无 NUL 但非 UTF-8（Latin-1/GBK）走 `errors="replace"` 软降级

---

## 六、`render_call_graph` 工具

**文件** `/src/service/qa_engine/tools/render_call_graph.py`

### 构造函数
```
build_render_call_graph_tool(graph: GraphProto, *, summary_lookup: Optional[Callable[[str], str]] = None) -> Tool
```
- `graph`：`GraphProto`（`successors(qn)` / `predecessors(qn)`）
- `summary_lookup`：可选，`entity_id → 2b 解读全文`（**返回全文，非短 label**；synthesizer 内部用 `_short_cn_label` 截首句作 label）；`None` 时 label 回退方法短名
- 生产 wiring（`tools/__init__.py`）：`_summary_lookup` 调 `interpretation_store.get_by_entity(entity_id)`，取 `hit["interpretation_text"]`，**fail-soft 任何异常返 `""`**

### 模块依赖（精确）
- 从 `synthesizer` 仅 import 三个符号：`_build_call_chain_section_from_edges`、`_cc_label`、`_CALLCHAIN_MAX_NODES`
- 模式 A 的节点 `kind`/`classOf`/`method`/`entityId` 字段**由 `_build_call_chain_section_from_edges` 内部产**（其内部调 `_cc_kind` / `_cc_class_of` / `_short_cn_label`），render_call_graph **不直接** import 或调用 `_cc_kind`
- 从 `retriever` import `GraphProto`；标准库 `deque` / `json` / `logging`

### 魔法数字与护栏

| 常量 | 值 | 来源文件 |
|---|---|---|
| `_DEFAULT_DEPTH` | `2` | render_call_graph.py |
| `_MAX_DEPTH` | `4` | render_call_graph.py |
| `_MAX_EDGES` | `60` | render_call_graph.py |
| `_CALLCHAIN_MAX_NODES` | `18` | synthesizer.py（import 来） |

### Input Schema（`_SCHEMA`，**根对象无 `required`**，handler 内做二选一）

模式 A：`entity_id`(string，desc 含示例 `OmsPortalOrderServiceImpl::generateOrder#(OrderParam)`) / `direction`(enum `["down","up"]`，default `"down"`) / `depth`(integer，default `_DEFAULT_DEPTH=2`，`minimum:1, maximum:_MAX_DEPTH=4`)

模式 B：
- `nodes`(array)，items `required:["id","label"]`，properties `{id,label,code,kind}`；desc 逐字：`【模式B】节点列表，画任意业务逻辑/架构图。每项 {id, label(中文业务名), code(英文 类.方法,可选), kind(controller/service/dao/method,可选)}`（注意 **kind 取值用 `dao` 不是 `mapper`**）
- `edges`(array)，items `required:["source","target"]`，properties `{source,target,label}`；desc：`【模式B】边列表，每项 {source, target, label(可选)}（端点用 nodes 里的 id）`

**模式判定**：`if input.get("nodes")`（**truthy**——空 list `[]` 不触发模式 B，落模式 A）。模式 A 又无 `entity_id` → `{render: None, summary: "缺少 entity_id 或 nodes，无法渲染图", error: "need entity_id or nodes"}`。

### 返回结构
```
// 成功
{ render: { kind: "call_graph", data: { nodes: Node[], edges: Edge[] } }, summary: string }
// 无图 / 错误
{ render: null, summary: string, error?: string }
```

**Node（模式 A，由 `_build_call_chain_section_from_edges` 产，五字段恒有）**：
```
{ id: string,                 // = 实体 id（去 scheme 后 BFS 的 nid）
  label: string,              // 有 2b 解读→_short_cn_label 截首句；否则 _cc_label 方法短名
  method: string,             // "{短类名(去包名)}.{方法短名}"，无类名时仅方法短名
  classOf: string,            // _cc_class_of 类全名（含包名，可空串）
  kind: "controller"|"service"|"mapper"|"method",
  entityId: string }          // "method://{id}"，模式 A 恒有（非 optional）
```
**Node（模式 B，`_build_freeform_graph` 产）**：`{id, label, method, kind}`，`entityId?`（仅当输入节点带 `entityId`/`entity_id` 时）。模式 B **无 `classOf`**。

**Edge（两模式同构）**：`{from: string, to: string, label?: string}`（统一 `from`/`to`，非 `source`/`target`）

### 模式 A 核心流程
1. `entity_id = str(entity_id).split("://", 1)[-1]`（剥 scheme，防双 `method://` 前缀 → reactflow 渲染失败）
2. `direction = "up" if input.get("direction") == "up" else "down"`（非 `"up"` 一律 `"down"`）
3. `depth = max(1, min(int(...), _MAX_DEPTH))`，非法 → `_DEFAULT_DEPTH=2`
4. `_collect(direction)` BFS：`down`→`graph.successors`，`up`→`graph.predecessors`；边统一 `(node, nxt) if down else (nxt, node)`（调用方→被调用方）；`seen_n` 初始含 `entity_id`；`d >= depth` 不再扩；`len(edges_) >= _MAX_EDGES=60` 提前返回
5. **方向自动回退**：请求方向无边 → 试反方向；反方向有边则 `edges, seen_nodes, direction = e2, n2, other`（`direction` 改写，summary 反映实际方向）；BFS 抛异常 → `{render: None, summary: "图后端异常，未生成调用图", error: "graph backend error: {e}"}`
6. 仍无边 → `{render: None, summary: f"未找到 {_cc_label(entity_id)} 的调用关系"}`
7. `summary_lookup` 逐 `seen_nodes` 节点查（异常吞为 `""`，非空才入 `summaries`）
8. `section = _build_call_chain_section_from_edges({entity_id: edges}, node_summaries=summaries)`
9. `section` 为 None（全噪声被滤光）→ `{render: None, summary: f"{_cc_label(entity_id)} 的调用关系均为框架噪声，未生成图"}`
10. `data = json.loads(section["content"])`；`flow = "下游" if direction=="down" else "上游"`；返回 `summary: f"已渲染 {_cc_label(entity_id)} 的{flow}调用图（{n} 节点）"`（`n = len(data["nodes"])`）

### 模式 B 核心流程（`_build_freeform_graph(nodes_in, edges_in, graph=None)`）
1. 节点归一：`(nodes_in or [])[:_CALLCHAIN_MAX_NODES=18]`；跳过非 dict / 缺 `id` / 重复 id；输出 `{id, label(=label or id), method(=code or method or ""), kind(=kind or "method")}`；`entityId`/`entity_id` 字段 → `ent if startswith("method://") else f"method://{ent}"`
2. 无有效节点 → `{render: None, summary: "nodes 为空，未渲染图", error: "freeform needs non-empty nodes"}`
3. 边归一：接受 `from`/`to` 或 `source`/`target`；缺端点 skip；端点不在 `valid_ids`（悬挂边）skip；去重；`len(out_edges) >= _MAX_EDGES=60` break；有 `label` 才加
4. **边核验**（`graph is not None` 时）：`out_edges, dropped_count = _filter_unsupported_edges(graph, out_nodes, out_edges)`
5. summary：`f"已渲染逻辑图（{len(out_nodes)} 节点）"`；`dropped_count > 0` 时追加逐字：`，已过滤 {dropped_count} 条 CodeGraph 无支撑的边（异步触发 / Spring @Bean / AOP 等静态分析抓不到的关系）`

### 模式 B 边核验（`_filter_unsupported_edges` → `(kept_edges, dropped_count)`）
- `qn_by_id = {n["id"]: _node_qn(n) for n in out_nodes}`
- 遍历每条边：
  1. **语义边豁免**（`_is_semantic_edge`，**最先判**）：`label` 含 `_SEMANTIC_EDGE_KEYWORDS` 任一 → 保留
  2. `src_qn = qn_by_id[edge["from"]]`、`tgt_qn = qn_by_id[edge["to"]]`；任一为 None（抽象概念节点）→ 保留
  3. 两端真实方法 + 非语义边：`succs = graph.successors(src_qn) or []`、`preds = graph.predecessors(tgt_qn) or []`；抛异常 → **fail-safe 保留**；`_strip(k) = k.split("://",1)[-1].split("#",1)[0]`；`tgt_qn in succ_qns or src_qn in pred_qns` → 保留，否则 drop（`_LOG.info`，`dropped += 1`）

**`_SEMANTIC_EDGE_KEYWORDS`（逐字全量，元组顺序）**：
```
"异步", "MQ", "消息", "触发", "监听", "配置", "注入", "路由",
"事件", "订阅", "发布", "广播", "回调", "调度", "定时",
"async", "queue", "message", "listener", "trigger", "event"
```
- `_is_semantic_edge`：`label` 空 → `False`（走严格校验）；否则 `any(kw in label for kw in ...)`（子串模糊匹配）

### `_node_qn` 抽 qualified_name（优先级）
依次试 `entityId` > `method` > `code` > `id`，对各值 `_normalize_to_qn`，归一后**含 `::` 即返**；都不含 `::` → `None`（抽象概念节点）

### `_normalize_to_qn` 归一规则（五步）
1. `s.split("://", 1)[-1]`（剥 scheme）
2. `s.split("#", 1)[0]`（剥签名）
3. 含 `::` → 直接返
4. 含 `.` → `idx = s.rfind("."); return s[:idx] + "::" + s[idx+1:]`（最右 `.` 替换为 `::`）
5. 无分隔符 → 原样返

### `_cc_kind` 节点角色推断（synthesizer.py，模式 A 经此路径）
**先 `_cc_class_of(entity_id).rsplit(".", 1)[-1]` 取短类名（去包名）**，再判后缀：

| 短类名后缀 | kind |
|---|---|
| `Controller` | `controller` |
| `ServiceImpl` 或 `Service`（先判 Impl，二者同归） | `service` |
| `Mapper` 或 `Dao` | `mapper` |
| 其他 | `method` |

### 怪癖与降级
- 模式 A `entity_id` 必须剥 scheme，否则节点 id 含 `://` → 前端 reactflow 渲染失败（实测 `sess_8e96f6f936e3`）
- 方向回退：`summary` 中"下游/上游"反映实际命中方向
- 全框架噪声被 `_build_call_chain_section_from_edges` 滤光 → `{render: None, summary: f"{_cc_label(entity_id)} 的调用关系均为框架噪声，未生成图"}`
- 图后端挂 → `{render: None, summary: "图后端异常，未生成调用图", error: "graph backend error: {e}"}`
- render 结果**不回灌 LLM**：只 `summary` 一句进 tool message（`ReActSynthesizer` 见 `render` 字段特殊处理）
- 模式 B `graph=None`（测试）跳过边核验，全保留

### description（逐字原文）
```
渲染节点-边图（ReactFlow，唯一画图出口）。两种用法二选一：① 代码调用图——传 entity_id（真实方法）+ direction(down下游/up上游)，自动 BFS 出调用关系；② 任意业务逻辑/流程/架构图——传 nodes(每项{id,label中文,code英文,kind})+edges(每项{source,target,label})，由你构思。图内联展示给用户，你只需在文字里说'见下方调用图'，不要复述节点；任何图都用我、不要手画 mermaid/reactflow。
```

---

## 七、`todo_write` 工具（元工具）

**文件** `/src/service/qa_engine/tools/todo_write.py` → `build_todo_write_tool() -> Tool`（无参，无后端依赖）

### Input Schema
```
_TODO_STATUS_ENUM = ["pending", "in_progress", "completed"]
{ type:"object",
  properties:{ items:{ type:"array",
    description:"当前 todo 列表；多步任务时自报进度，简单问题不必调用",
    items:{ type:"object",
      properties:{ content:{type:"string",description:"任务描述"},
                   status:{type:"string",description:"任务状态",enum:_TODO_STATUS_ENUM} },
      required:["content","status"] } } },
  required:["items"] }
```
`items` 为 **required**；`status` 严格枚举 `pending`/`in_progress`/`completed`。

### 返回结构
`{items: [...], count: number}` — 纯回显：`items` 原样，`count = len(items)`

### 核心设计
- **纯元工具零副作用**：handler 不查/不写任何 store
- 前端展示靠 `sse_emitter` 识别本次调用 → 发 `todo` SSE 事件（SSE 协议 §8）
- `items` 缺失或非 list → `isinstance(items, list)` 失败兜底 `[]`，**不抛**（信号哲学）
- 语义：**全量快照**替换，非增量

### description（逐字原文）
```
多步任务自追踪 checklist：把当前待办列表 items（每项 {content, status}，status ∈ pending/in_progress/completed）记录并展示给用户。每次调用均为全量快照，替换之前的列表（不要每条 item 调一次）。仅在多步复杂任务时调用；简单问题无需调用。
```

---

相关源文件（绝对路径）：
- `/Users/java/knowledge-engineering/src/service/qa_engine/tools/_path_sandbox.py`
- `/Users/java/knowledge-engineering/src/service/qa_engine/tools/ke_grep.py`
- `/Users/java/knowledge-engineering/src/service/qa_engine/tools/ke_glob.py`
- `/Users/java/knowledge-engineering/src/service/qa_engine/tools/ke_ls.py`
- `/Users/java/knowledge-engineering/src/service/qa_engine/tools/ke_read_file.py`
- `/Users/java/knowledge-engineering/src/service/qa_engine/tools/render_call_graph.py`
- `/Users/java/knowledge-engineering/src/service/qa_engine/tools/todo_write.py`
- `/Users/java/knowledge-engineering/src/service/qa_engine/tools/base.py`（Tool dataclass，`frozen=True, slots=True`）
- `/Users/java/knowledge-engineering/src/service/qa_engine/synthesizer.py:519-682`（`_build_call_chain_section_from_edges` / `_cc_label`:528 / `_cc_kind`:551 / `_cc_class_of`:535 / `_short_cn_label`:572 / `_CALLCHAIN_MAX_NODES`:519 — 模式 A 节点构造的真正出处）
- `/Users/java/knowledge-engineering/src/service/qa_engine/tools/__init__.py:94-112`（`_summary_lookup` 生产 wiring）

---

# 【SSE emitter + 流式 helper + explain 路由】

I have all the information needed. Let me verify a few remaining spec details: the `with_memory_block` template (spec mentions memory injection at "system prompt 顶部"), and confirm the spec's claim about title length (it says "15 个汉字" in prompt but "30字"截断 in code — both are correct, different things). Let me check the `format_sse` line numbers in the spec (claims 87-96, actual 87-96 ✓) and verify the `_make_title_generator`/`_make_memory_writer` line ranges.

The spec claims `_make_title_generator` at qa_router.py:691-727 (actual 691-727 ✓) and `_make_memory_writer` at 730-814 (actual 730-814 ✓). The explain handler spec claims qa_router.py:263-526 (actual 263-526 ✓).

Now let me confirm one subtle point in the spec: the title prompt. The spec says "标题最长 30 字" with `title[:30]` at qa_router.py:719 (actual 718-719 ✓), but the prompt itself says "不超过 15 个汉字". Both are accurate — the spec doesn't quote the prompt verbatim, which is a gap the task asked me to fill. Let me also double check the `recall_threshold` exception type.

The code at qa_router.py:127 catches `ValueError` only (not `TypeError`), but `os.getenv` always returns a str or the default str, so `float()` only raises `ValueError`. The spec says "非数字 → 回落 0.45" — accurate. Note `_recall_top_k` catches `(TypeError, ValueError)` which is broader but harmless.

All other details verified. Let me produce the corrected spec.

---

## 修正清单

逐条回源核验三个流式 helper（`sse_emitter.py` / `think_splitter.py` / `token_batcher.py`）+ `qa_router.py` explain 编排 + `react_synthesizer.py` / `api.py` 护栏数字。结果如下：

1. **【关键错误 — 护栏表自相矛盾】** §9 护栏表「ReAct 轮数上限」写 **12**（"生产，api.py 显式传"），但同表注释引用 `react_synthesizer.py:78`。两处串了：
   - `ReActSynthesizer.__init__` 构造器默认 **`max_iterations: int = 8`**（react_synthesizer.py:77），出处行号应是 **77** 不是 78（78 是 `total_timeout_sec`）。
   - 代码内注释明确写 **"8 轮安全阀（12→8 收紧）"**（react_synthesizer.py:90-91）——默认值已从 12 收紧到 8。
   - api.py:222 生产环境读 `KE_QA_REACT_MAX_ITER` env，**默认值 `"12"`**（`int(os.environ.get("KE_QA_REACT_MAX_ITER", "12"))`）。
   - 所以真相是：**构造器默认 8，但 api.py startup 显式传 env 默认 12 覆盖之**。生产实际跑 12（除非 env 设其他值）。spec 表的「值=12」结论正确，但「构造器默认」与「env 默认」两个 12/8 被混为一谈，且 react_synthesizer 行号错位。已在下方修正表拆成两行。

2. **【行号错误】** §6 表与 §9 表「伪流式 chunk」标注常量名为 `_PSEUDO_CHUNK_SIZE` / `_PSEUDO_DELAY_S`，出处 `react_synthesizer.py:232-234`。**常量名错**：实际是 **`_PSEUDO_STREAM_CHUNK_SIZE = 20`**（line 232）、**`_PSEUDO_STREAM_INTERVAL_SEC = 0.025`**（line 234）。`_pseudo_stream` 方法在 386-403（spec 的 385-403 偏 1 行，可接受）。

3. **【超时护栏行号】** §9 表「单工具超时 20.0s」「总超时 75.0s」出处写 react_synthesizer.py:78/79，正确应为 **`total_timeout_sec: float = 75.0`（line 78）**、**`tool_timeout_sec: float = 20.0`（line 79）**。spec 把 78 同时给了 max_iterations 和 total_timeout——78 只对应 total_timeout。

4. **【补充 — prompt 逐字】** spec 未给出 `_TITLE_SUMMARY_SYSTEM` 原文（仅引用变量名）。任务要求 prompt 逐字。已在下方 §8.6 补全逐字原文。**注意**：prompt 内要求模型「不超过 **15** 个汉字」，而代码后处理截断到 **30** 字（`title[:30]`）——两者不矛盾（prompt 软约束 15，代码硬上限 30 做兜底），TS 实现两个数字都要照搬。

5. **【次要 — 异常类型】** §8.1 步骤 6 spec 写「非数字 → 回落 0.45」准确，但补充精度：qa_router.py:127 只 `except ValueError`（非 `TypeError`），因 `os.getenv` 恒返回 str；而 `_recall_top_k`（sse_emitter.py:78）catch `(TypeError, ValueError)` 范围更宽。TS 实现 `parseFloat`/`Number.isNaN` 兜底即可，无需区分。

6. **抽查通过项**（无修正）：`format_sse` 帧格式三行 + `json.dumps(ensure_ascii=False, separators=(",",":"))`（sse_emitter.py:95-96）✓；`fold_render_sections` 7 条规则逐条 + `call_chain` 段 content 用默认 `json.dumps`（带空格）✓；narrate-tool 正则逐字 ✓；`ThinkSplitter` `_OPEN_TAG`/`_CLOSE_TAG` 长度 7/8 + flush 不重置 `_in_think` ✓；`TokenBatcher` min_chars/max_ms 校验 + 生产参数 `(1,10)`（sse_emitter.py:320）✓；主循环 5ms tick（sse_emitter.py:372）✓；`_recall_top_k` 默认 15 ✓；`recall_threshold` 默认 0.45 ✓；result_preview 截断 600（sse_emitter.py:293）✓；session_id/message_id 格式 ✓；question Field(min=1,max=2000)、model Field(max=64) ✓；memory recall top_k=5 ✓；rendered_graphs 四重门收集条件（sse_emitter.py:296-301）✓；`_collect_cited_entities`（sse_emitter.py:499-509）✓；persist_messages 强一致语义 ✓；`_make_title_generator`(691-727)/`_make_memory_writer`(730-814) 行号 ✓；title 后处理链 `.strip().strip('"').strip("「」").strip()`（line 715）✓；StreamingResponse headers 三项 ✓；cited_entities 收集（react_synthesizer.py:353-354）✓。

---

# SSE Emitter + 流式 Helper 实现提取（修正版）

> 对照物：`/Users/java/knowledge-engineering/docs/porting/sse-protocol.md`（SSE 协议门禁文档，已覆盖事件字段/顺序/payload 规范）。本文聚焦**实现细节**——算法步骤、魔法数字、调用编排、降级逻辑。
>
> 源文件基线：tag `py-final-baseline`。

---

## 1. `format_sse` — SSE 帧格式化

**文件**：`sse_emitter.py:87-96`

```
format_sse(event_type: str, data: object) -> str
```

**精确帧格式**（三行，末尾双换行）：

```
event: {event_type}\n
data: {json}\n
\n
```

**JSON 序列化参数**：`json.dumps(data, ensure_ascii=False, separators=(",", ":"))`
- `ensure_ascii=False`：中文字符不转义，UTF-8 原文
- `separators=(",", ":")`：紧凑分隔符，无空格
- 单行输出

**TS 等价**：`JSON.stringify(data)` 默认即无空格无转义 UTF-8——行为等价。注意浮点差异（Python `0.0` → `0.0`，JS → `0`，前端按 number 消费无语义差异）。

**无心跳帧**：无 `id:` / `retry:` / `: ping` 注释行。

---

## 2. `fold_render_sections` — 调用图折叠算法

**文件**：`sse_emitter.py:99-148`

**签名**：

```python
fold_render_sections(sections: list[dict], renders: list[dict]) -> list[dict]
```
（`renders` = `[{"at": int, "data": {nodes, edges}}, ...]`）

**目的**：agent 自由输出是单 `overview` 段。本函数把流式期间 `render_call_graph` 工具产出的图，按 `at` 字符偏移切入正文，折成多段（文本段 + call_chain 段交错），治「图跳末尾 / reopen 丢图」。

**7 条规则（TS 逐条等价）**：

1. `renders` 为空 **或** `sections` 为空 → 原样返回（快返）。
2. `sections[0]` 为折叠载体：`text = base.get("content") or ""`，`base_type = base.get("type", "overview")`。`sections[1:]` 原样挂后面。
3. 各 render 的 `at` 越界夹紧：`at = max(0, min(int(r["at"]), len(text)))`，按 at **升序稳定排序**（Python `sorted` 稳定；TS `Array.sort` 现代引擎稳定，同 at 保留调用先后）。
4. 游标 `cursor = 0`，遍历排序后 renders：
   - 切片 `chunk = text[cursor : at]`；**非空才 push**：`{type: base_type, headerless: true, content: chunk}`
   - 无条件 push 图段：`{type: "call_chain", title: "", headerless: true, content: <json.dumps(r["data"], ensure_ascii=False)>}`
   - `cursor = at`
5. 尾段 `tail = text[cursor:]`；**非空才 push**：`{type: base_type, headerless: true, content: tail}`
6. `sections[1:]` 追加到 `out` 末尾。
7. **任何异常 → `except Exception: return sections`**（fail-soft）。

**`at` 恒 0 的实际效果**：`text[0:0]` 为空跳过，图段先出，tail=全文 → `out = [call_chain(图1), …, {base_type, 全文}]`（图在头部）。

**call_chain 段 content 序列化**：`json.dumps(r["data"], ensure_ascii=False)`——中文不转义，**无 separators 覆盖**（默认 `", "` / `": "`，带空格）。**与 format_sse 不同**：这里是段 content 字符串，不走 format_sse。TS 用 `JSON.stringify(data, null, 0)` 无空格，**会与 Python 默认带空格输出有字节差异**——若 TS 要逐字节匹配 Python，需手动注入分隔符空格；若仅语义匹配（前端 JSON.parse 后消费）则 `JSON.stringify(data)` 即可。

**图的收集条件**（`sse_emitter.py:296-301`，`_on_tool_call` complete 阶段）四重门：

```python
if isinstance(result, dict) and result.get("render") is not None:
    render = result["render"]
    if isinstance(render, dict) and render.get("kind") == "call_graph" and render.get("data"):
        rendered_graphs.append({"at": _offset[0], "data": render["data"]})
```

---

## 3. 防 narrate-tool 退化剥离

**文件**：`sse_emitter.py:34-57`（正则 + helper），`415-418`（调用点）

**正则**（`sse_emitter.py:34-37`）：

```python
_RENDER_CALL_GRAPH_CODEBLOCK_RE = re.compile(
    r"```render_call_graph[ \t]*\n.*?\n```\n?",
    re.DOTALL,
)
```

- `[ \t]*`：语言标记后允许空格/tab
- `.*?`：非贪婪
- `re.DOTALL`：`.` 匹配换行
- `\n?`：末尾换行可选

**触发时机**：fold 之后、sections dump 之前，对**每段** content 执行（`sse_emitter.py:415-418`）。`None` / 无代码块 → 原样返。

**TS 等价**：`/```render_call_graph[ \t]*\n[\s\S]*?\n```\n?/g`

---

## 4. `ThinkSplitter` — MiniMax `<think>` 流式分流

**文件**：`think_splitter.py:1-87`

**常量**：`_OPEN_TAG = "<think>"`（长 7），`_CLOSE_TAG = "</think>"`（长 8，buf 保留尾部 8 字节防切碎）。

**接口**：`feed(chunk: str) -> list[Segment]`，`flush() -> list[Segment]`。
**`Segment`**：`@dataclass(frozen=True, slots=True)`，`{kind: "think"|"text", text: str}`。
**内部状态**：`_in_think: bool`，`_buf: str`。

**`feed` 算法**（while 循环）：
- **不在 think**：`buf.find("<think>")`
  - 找不到 → `rfind("<")`：有 `<` 则保留从 `<` 起的尾部、前部 emit `text`；无 `<` 则整 buf emit `text` 后清空；break
  - 找到 → `idx > 0` emit 前文 `text`；`buf = buf[idx+7:]`；`_in_think = True`，继续
- **在 think**：`buf.find("</think>")`
  - 找不到 → `len(buf) > 8`：emit `think(buf[:-8])`，`buf = buf[-8:]`；否则全留等下个 chunk；break
  - 找到 → `idx > 0` emit `think(buf[:idx])`；`buf = buf[idx+8:]`；`_in_think = False`，继续

**`flush`**：`buf` 非空 → 按当前 `_in_think` 归类成一个 Segment emit，`_buf = ""`（**不重置 `_in_think`**，见 §11-6）。

**消费方**：`complete_stream`（无工具）只取 `text` 丢 think；`complete_stream_with_tools`：`think`→`StreamThinkingDelta`（→ `on_thinking`），`text`→`StreamTextDelta`（→ `on_token`）。

---

## 5. `TokenBatcher` — 流式 token 攒批

**文件**：`token_batcher.py:1-91`

**接口**：`__init__(min_chars=20, max_ms=80)`，`async add(chunk) -> Optional[str]`，`async flush() -> Optional[str]`。

**内部状态**：`_buffer: list[str]`、`_buffer_chars: int`、`_last_flush_at: float`（`time.monotonic()`，构造时刻初始化）、`_max_seconds = max_ms / 1000.0`。

**`add` 触发（任一）**：① `_buffer_chars >= _min_chars`；② `monotonic() - _last_flush_at >= _max_seconds`（先字符后时间，时间是兜底）。空 chunk → 直接返 None（不入 buffer）。

**`_do_flush`**：`"".join(_buffer)` → 清 buffer + `_buffer_chars=0` + 更新 `_last_flush_at` → 返串。

**构造器校验**：`min_chars < 1` 抛 `ValueError("min_chars 必须 >= 1")`；`max_ms < 1` 抛 `ValueError("max_ms 必须 >= 1")`。

**生产实际参数**（`sse_emitter.py:320`）：`TokenBatcher(min_chars=1, max_ms=10)` — 等效**直通模式**（2026-05-22 调整，旧值 20/25 已替换）。

**TS 实现**：用 `performance.now()`（对应 `time.monotonic`，比 `Date.now()` 精度高）。

---

## 6. 伪流式参数

| 参数 | 值 | 出处 | 说明 |
|---|---|---|---|
| TokenBatcher min_chars | **1** | sse_emitter.py:320 | 等效直通 |
| TokenBatcher max_ms | **10** | sse_emitter.py:320 | 超时兜底 10ms |
| 主循环 tick | **5ms** (`asyncio.sleep(0.005)`) | sse_emitter.py:372 | 每秒最多 200 次 yield |
| 伪流式 chunk 大小 | **20 字符** | react_synthesizer.py:232 | 常量名 **`_PSEUDO_STREAM_CHUNK_SIZE`** |
| 伪流式 chunk 间隔 | **25ms** (`0.025`s) | react_synthesizer.py:234 | 常量名 **`_PSEUDO_STREAM_INTERVAL_SEC`** |

**伪流式机制**（`react_synthesizer.py:386-403`，方法 `_pseudo_stream`）：agent 最终答案轮文本整体生成后，按 20 字符切片、片间 `asyncio.sleep(0.025)` 重放。每片调 `on_token(chunk)`，**`on_token` 抛错被吞**（不打断后续 chunks）。→ batcher(min_chars=1) → 立即入 pending_tokens → 主循环 5ms tick flush → SSE `token` 事件。

---

## 7. `stream_qa_answer` 内部流程

**文件**：`sse_emitter.py:177-494`

**签名**：

```python
async def stream_qa_answer(
    *, question: str, project_id: str, session_id: str,
    retriever: QARetriever, synthesizer: QASynthesizer,
    router: SkillRouter | None = None,            # 已弃用，函数体不使用
    history: list[dict] | None = None,
    on_complete: OnCompleteCallback | None = None,  # (question, sections, metadata) -> Awaitable[None]
    on_title: OnTitleCallback | None = None,         # () -> Awaitable[str | None]
    memory_block: str | None = None,
    on_memory: OnMemoryCallback | None = None,       # (answer_text: str) -> Awaitable[None]
    context_usage: dict | None = None,
) -> AsyncIterator[str]
```

**回调别名**：`OnCompleteCallback = Callable[[str, list[dict], dict], Awaitable[None]]`；`OnTitleCallback = Callable[[], Awaitable[str | None]]`；`OnMemoryCallback = Callable[[str], Awaitable[None]]`。

**步骤顺序**：

| 步骤 | 事件 | 关键逻辑 |
|---|---|---|
| 0 | — | `message_id = "msg_" + uuid4().hex[:12]`；`start = time.monotonic()` |
| 1 | `meta` | session_id/message_id/`plan_steps=["searching","chain_extraction","synthesizing"]`；context_usage 非 None 时并入 |
| 2 | `step{searching}` | desc="检索相关代码实体" |
| 3 | `retriever.retrieve(question, project_id, top_k=_recall_top_k())` | 失败 → `error{RETRIEVE_FAILED, "检索失败：{e}", recoverable:true}` + return |
| 4 | `route` | `{skill_id: ctx.skill_id, recall_score: round(getattr(ctx,"recall_score",0.0), 4)}` |
| 5 | `step{chain_extraction}` | desc="提取调用链路" |
| 6 | `step{synthesizing}` | desc="合成业务文档" |
| 7 | synthesize（三路分支） | 失败 → `error{LLM_FAILED, "LLM 调用失败：{e}", recoverable:true}` + return |
| 8 | `fold_render_sections` | 就地替换 `answer.sections` |
| 9 | narrate-tool 剥离 | 对每段 content（非空才剥） |
| 10 | sections dump | 每段三连：`section_start{section,title}` → `content{section,delta}` → `section_done{section,references}` |
| 11 | `on_complete` 回调 | 失败静默；done 之前 |
| 12 | `done` | session_id/message_id/total_tokens/cost_yuan/latency_ms/`cited_entities=answer.cited_entities` |
| 13 | `on_title` + `session_title`（可选） | 失败静默；仅回调返非空 str 才 emit |
| 14 | `on_memory` 回调 | 失败静默；answer_text = 所有段 content `"\n\n".join` |

**`_recall_top_k`（sse_emitter.py:73-82）**：默认 **15**（`_RECALL_TOP_K_DEFAULT`）；env `KE_RECALL_TOP_K` 可覆盖；catch `(TypeError, ValueError)`，非正数 → 回落 15。

**synthesize 三路分支**（`sse_emitter.py:336-405`）：

```
supports_stream = hasattr(synthesizer,"synthesize_stream") and callable(...)
├── True（流式）：asyncio.create_task(synthesize_stream(ctx, **stream_kwargs))
│   while not task.done(): flush pending_thinking→thinking → pending_tool_events→tool_call/todo → pending_tokens→token; await sleep(0.005)
│   task done 后再 flush 三队列残留；token_batcher.flush() 补残留；answer = task.result()
├── False && is_react：answer = await synthesize(ctx, history, on_tool_call, memory_block); for ev in pending_tool_events: yield
└── False && !is_react：answer = await synthesize(ctx, history, memory_block)
```

**`synthesize_stream` kwargs**：基础 `{history, on_token, memory_block}`；ReAct 额外 `{on_tool_call, on_thinking}`。

**`_on_tool_call`（sse_emitter.py:267-302）**：
- `todo_write`：starting → push `("todo", {items})`（items 非 list 归一化 `[]`）；complete → return（不发事件）
- 其他工具 payload：`{phase, id: call.id, name: call.name, at: _offset[0]}`；starting 加 `arguments: call.arguments`；complete 加 `result_preview: json.dumps(result or {}, ensure_ascii=False)[:600]`，并条件性收集 render（§2 四重门）

**`_offset` 累加**（`_on_token`，sse_emitter.py:326）：`_offset[0] += len(delta)`（闭包用 list 容器可变）。

**metadata（done 前，sse_emitter.py:438-446）**：

```python
{
  "token_usage": answer.token_usage,
  "cost_yuan": answer.cost_yuan,
  "latency_ms": latency_ms,
  "entry_points": [c.get("entity_id") for c in ctx.entry_candidates[:3] if c.get("entity_id")],
  "cited_entities": _collect_cited_entities(answer.sections),
}
```

**`_collect_cited_entities`（sse_emitter.py:499-509）**：遍历 sections → `s.get("references", []) or []` → ref 是 dict 才取 `ref.get("entity_id")` → 去重保序（seen set + result list）。

---

## 8. `explain` Handler 完整编排

**文件**：`qa_router.py:263-526`

### 8.1 `build_retriever_for_project`（qa_router.py:68-137）

`async (project_id, request, db) -> QARetriever`
1. `app.state.weaviate_interp_store` singleton；None → `RuntimeError`
2. `db.get(Project, project_id)` 取 `repo_local_path`（不存在为 None）
3. `WeaviateTopologicalAdapter(interp_store)` 包装
4. `resolve_graph_adapter(repo_local_path)`：repo 缺失或 `.codegraph.db` 不存在 → 降级 `NullGraphAdapter`（图导航返 `[]`）
5. `CompositeKnowledgeStore(interpretation_store, code_store, project_id)`：code_store 缺失时跳过 fallback
6. `recall_threshold = float(os.getenv("KE_QA_RECALL_THRESHOLD", "0.45"))`；`except ValueError` → 0.45
7. 返 `QARetriever(interpretation_store=composite, graph=graph_adapter, recall_threshold=recall_threshold)`

### 8.2 `build_tools_for_project`（qa_router.py:140-203）

`async (project_id, request, db) -> ToolRegistry`。结构类似，额外取 `weaviate_code_store`、`weaviate_interp_store`（作 method_interp_store）；`resolve_graph_adapter` 同样降级；返 `build_default_registry(graph, composite_store, project_id, code_store, method_interp_store, repo_local_path)`。

### 8.3 `_inject_per_request_tool_registry`（qa_router.py:206-231）

`async (synthesizer, project_id, request, db) -> synthesizer`。非 `ReActSynthesizer` → 原样返回；`ReActSynthesizer` → `synthesizer.tool_registry = await build_tools_for_project(...)`；失败 → `_log.warning` + 沿用原 registry。

### 8.4 `explain` handler 步骤

1. **工程校验**：`db.get(Project, project_id)`；None → 404 `"工程不存在"`；`status == "indexing"` → 409 `"工程正在索引，暂时无法问答；完成后会自动通知"`
2. **会话**：非空 session_id 查归档 → 已归档 409 `"该会话已归档，请先恢复后继续提问"`；空 → `session_id = "sess_" + uuid4().hex[:12]`，建 `QASession(id, project_id, user_id, title=body.question[:30], message_count=0)` + commit
3. **synthesizer**：`app.state.qa_synthesizer`；None → 503 `"QA 引擎未就绪（app.state.qa_synthesizer 缺失）"`
4. **retriever**：优先 `app.state.qa_retriever`（测试 mock）；否则 per-request；`weaviate_interp_store` None → 503；构造失败 → 503
5. **多模型**：`chosen_model_id = body.model or getattr(user,"preferred_model",None)`；`get_llm_provider(chosen_model_id)`；`copy.copy(synthesizer)`（浅复制）；`synthesizer.llm = chosen_llm`；失败 → warning + 用原 synthesizer
6. **per-request 工具注入**：`await _inject_per_request_tool_registry(...)`
7. **记忆召回**（428-438）：`recall_memory_block(MemoryFS(), body.question, user_id, top_k=5)`；失败 → `""`
8. **会话摘要**（440-458）：`read_session_summary(MemoryFS(), user_id, session_id)`；失败 → `""`；拼装 `memory_block = session_block + "\n\n" + memory_block`（session 在前）
9. **上下文预算**（460-483）：`history_token_budget()` + `trim_history_to_budget(body.history, budget)` → `eff_history`；`context_usage = {used_tokens, window_tokens, pct, history_trimmed}`；失败 → `eff_history=body.history, context_usage=None`
10. **StreamingResponse**：`media_type="text/event-stream"`，headers `{"Cache-Control":"no-cache","X-Accel-Buffering":"no","Connection":"keep-alive"}`

### 8.5 `persist_messages`（qa_router.py:374-422）

**强一致（2026-05-28）**：fs 写失败 → ERROR 日志 + 早返回（**不更新 DB message_count**）。
步骤：生成 user/assistant 各一 `msg_id`（**≠ meta.message_id**）→ `write_message_to_fs`（user: content=question；assistant: content=None, sections, msg_metadata=metadata）→ fs 成功后 `sess.message_count += 2` + commit。

### 8.6 `_make_title_generator`（qa_router.py:691-727）

**触发**：`is_new_session == True` **且** `sess.title_custom == False`。
**LLM 调用**：`await llm.complete(system=_TITLE_SUMMARY_SYSTEM, user=question)`

**`_TITLE_SUMMARY_SYSTEM` 逐字原文**（prompts.py:571-575，三段拼接，TS 须逐字照搬）：

```
你是会话标题生成器。请用不超过 15 个汉字概括用户问题的主题，直接输出标题本身：不要解释、不要引号、不要标点结尾、不要前缀。若问题是寒暄（你好/在吗等），输出「日常问候」。
```
（注意 prompt 内有空格：`不超过 15 个汉字`——"不超过"与"15"、"15"与"个"之间各一个半角空格。）

**后处理链**（qa_router.py:713-719）：
1. `strip_think(raw)`（剥 `<think>...</think>`）
2. `(raw or "").strip().strip('"').strip("「」").strip()`
3. `if len(title) > 30: title = title[:30]`（**硬上限 30 字**，注意与 prompt 软约束「15 汉字」并存）
4. 空 → return None
**顺序**：`sess.title = title; await db.commit()`（DB 落库）→ 返 title → sse_emitter emit `session_title`。

### 8.7 `_make_memory_writer`（qa_router.py:730-814）

`async _writer(answer: str) -> None`（answer = 所有段 content `"\n\n".join`）。

**S4（记忆抽取）**：延迟 import（MemoryFS/MemoryGen/MemoryRecaller/MemoryL0Store/_DefaultEmbedder/MemoryExtractor）；`memgen = MemoryGen(llm)`；Weaviate env：`WEAVIATE_URL`（默认 `http://127.0.0.1:8080`）、`WEAVIATE_GRPC_PORT`（默认 `50051`）、`WEAVIATE_API_KEY`（None）；`MemoryL0Store(collection_name="memory_l0", dimension=1024)`；`turn_text = f"用户：{question}\n助理：{answer}"`；`extractor.extract_and_persist(fs, memgen, recaller, user_id, turn_text)`；异常 → `_log.debug` 静默。

**S5（会话压缩）**：`SessionCompactor(llm).compact(fs, user_id, session_id, force=force_compact)`；`force_compact` 来自 `history_trimmed`；异常 → `_log.debug` 静默。S4 的 `fs` 跨 S5 复用；S4 import 阶段异常 → S5 `NameError` 被外层 catch 静默。

---

## 9. 护栏数字汇总（修正版）

| 护栏 | 值 | 配置 | 出处 |
|---|---|---|---|
| recall top_k 默认 | **15** | `KE_RECALL_TOP_K` | sse_emitter.py:70-82 |
| recall 门控阈值默认 | **0.45** | `KE_QA_RECALL_THRESHOLD` | qa_router.py:126-129 |
| result_preview 截断 | **600** 字符 | 硬编码 | sse_emitter.py:293 |
| session_id 格式 | `"sess_" + uuid4.hex[:12]` | 硬编码 | qa_router.py:301 |
| message_id 格式 | `"msg_" + uuid4.hex[:12]` | 硬编码 | sse_emitter.py:205 |
| session title 初始值 | `body.question[:30]` | 硬编码 | qa_router.py:306 |
| title prompt 软约束 | **15 汉字** | 硬编码 prompt | prompts.py:572 |
| title 硬截断上限 | **30 字** | 硬编码 | qa_router.py:718-719 |
| question 长度 | **min 1 / max 2000** | Pydantic Field | qa_router.py:238 |
| model 最大长度 | **64** | Pydantic Field | qa_router.py:245 |
| memory recall top_k | **5** | 硬编码 | qa_router.py:436 |
| TokenBatcher min_chars / max_ms | **1 / 10** | 硬编码 | sse_emitter.py:320 |
| TokenBatcher 默认 min_chars / max_ms | **20 / 80** | 构造器默认（生产不用） | token_batcher.py:36 |
| 主循环 tick | **5ms** | 硬编码 | sse_emitter.py:372 |
| 伪流式 chunk / 间隔 | **20 字符 / 25ms** | 硬编码常量 `_PSEUDO_STREAM_CHUNK_SIZE` / `_PSEUDO_STREAM_INTERVAL_SEC` | react_synthesizer.py:232 / 234 |
| ReAct 轮数 — **构造器默认** | **8** | 构造器默认 | react_synthesizer.py:77 |
| ReAct 轮数 — **生产实际** | **12** | api.py 读 `KE_QA_REACT_MAX_ITER`（env 默认 "12"）显式传，覆盖构造器默认 | api.py:222,230 |
| 总超时 | **75.0s** | 构造器默认 `total_timeout_sec` | react_synthesizer.py:78 |
| 单工具超时 | **20.0s** | 构造器默认 `tool_timeout_sec` | react_synthesizer.py:79 |

---

## 10. 外部依赖

| 依赖 | 用途 | 降级 |
|---|---|---|
| `app.state.weaviate_interp_store` | 语义检索 | None → 503 |
| `app.state.weaviate_code_store` | ReAct 代码层兜底 | None → composite 跳过 fallback |
| `app.state.qa_synthesizer` | LLM 合成 | None → 503 |
| `app.state.qa_retriever` | 测试 mock 向后兼容 | None → per-request 构造 |
| `repo_local_path` + `.codegraph.db` | CodeGraph 图导航 | 缺失 → NullGraphAdapter（返 `[]`） |
| Weaviate（记忆 L0） | 记忆召回 / S4 写入 | 失败 → `memory_block=""`，静默 |
| MySQL（QASession） | 会话持久化 | — |
| MemoryFS | 消息/摘要持久化 | fs 写失败 → ERROR 日志 + 不更新 DB message_count |

---

## 11. 怪癖与易踩坑

1. **`persist_messages` 的 `msg_id` ≠ `meta.message_id`**：均 `"msg_" + uuid4.hex[:12]` 但独立生成，不同值。
2. **`done.cited_entities`（= `answer.cited_entities`，ReAct 工具轨迹，react_synthesizer.py:353-354）≠ `metadata.cited_entities`（= `_collect_cited_entities(sections)`，从 references 抽取）**。agent 路径 metadata.cited_entities 通常空。
3. **synthesizer 浅复制**：`copy.copy` 只改副本 `.llm` / `.tool_registry`，不污染全局 singleton。
4. **router 参数已弃用**：`stream_qa_answer` 签名保留 `router: SkillRouter | None`，函数体完全不用（门控改由 retriever 内部 top1 相似度决定）。
5. **db session 在 StreamingResponse 迭代期间保持打开**：Starlette 依赖 yield 退出后才迭代响应体，三个回调都依赖此语义。TS（Hono/Fastify）须确保 DB connection 流结束前不释放。
6. **`ThinkSplitter.flush` 后不重置 `_in_think`**：仅 `self._buf = ""`，无 `self._in_think = False`。对象复用会错误继承状态。**TS 实现保证每请求新实例，或 flush 后显式重置**。
7. **`max_iterations` 双默认陷阱**：构造器默认 8，但 api.py startup 显式传 env（默认 12）覆盖。**TS 移植以生产 12 为准，构造器签名保留 8 默认**（仅 mock/直接构造无 env 时才命中 8）。
8. **`_PSEUDO_STREAM_*` 是类属性**：`react_synthesizer.py:232/234` 定义在类体（不是 module 级），`_pseudo_stream` 内用 `cls._PSEUDO_STREAM_CHUNK_SIZE` 访问。
9. **`_pseudo_stream` 吞 `on_token` 异常**：单 chunk 回调抛错被吞，不打断后续 chunks（react_synthesizer.py:393 注释「跟 v1.6 行为一致」）。
10. **非流式 ReAct 兜底路径**（sse_emitter.py:394-395）：`pending_tool_events` 同步 for 一次性 yield（无主循环插帧），生产不走（仅 `spec=['synthesize']` mock）。