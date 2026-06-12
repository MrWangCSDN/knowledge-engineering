# P5a structure 层行为规范（py-final-baseline 提取，经对抗核验）

> TS 移植权威。2026-06-12 提取+对抗核验。
> **门禁③ entity_id 逐字节**：Java AST 实体 entity_id 由 javaparser-bridge jar（Java 侧不动）生成，spawn 同 jar 天然一致；MyBatis XML 实体 entity_id 在 Python 侧 sha256 生成，TS 必须逐字节复现——file 实体 +sha256(file_path)[:16]、statement 实体 +sha256(file_path+"//"+namespace+"::"+statement_id)[:16]，裸 .encode()=UTF-8，[:16]=16 hex=8 字节。jar 路径 javaparser-bridge/target/javaparser-bridge-1.0.0-shaded.jar，CLI --repo-path/--modules-json/--output，extract_cross_service 只经 modules.json 传。



---

# 【javaparser-bridge 子进程接口 + StructureFacts 模型】

All files read. The spec is largely accurate. Now let me verify a few subtle points the spec makes claims about that I want to confirm against source: the `_create_file_entity` name derivation (spec doesn't mention `name` for file entity), the `.encode()` default-vs-explicit-utf8 detail, and the line number computation. Let me cross-check the byte-level claims carefully.

Key verification of the highest-priority claims (entity_id byte construction):

- **File entity** (line 149): `hashlib.sha256(self.file_path.encode()).hexdigest()[:16]` → `f"file//{sha}"`. Spec §7.2 says `file_path.encode("utf-8")`. The source uses bare `.encode()` — which defaults to UTF-8 in Python, so byte-equivalent. ✓ (worth a note)
- **Statement entity** (lines 234-237): `hashlib.sha256(f"{self.file_path}//{qualified}".encode()).hexdigest()[:16]` → `f"method//{id_hash}"`, where `qualified = f"{namespace}::{statement_id}"`. Spec §7.3 matches exactly. ✓
- `[:16]` = 16 hex chars = 8 bytes. Spec §10 says "前 16 位（8 字节）". ✓

Now the discrepancies I found.

## 修正清单

1. **§7.2 文件实体缺少 `name` 字段说明（遗漏）**。`_create_file_entity`（行 145）通过 `self.file_path.rsplit("/", 1)[-1]` 取文件名作为 `name`（即相对路径最后一段，basename）。spec §7.2 完全没提 file entity 的 `name`，TS 复现时会漏。已补入。

2. **§7.2 / §10 编码细节措辞需精确**。源码 `_create_file_entity` 用的是**裸 `.encode()`**（行 149），statement 也是裸 `.encode()`（行 235），**没有显式传 `"utf-8"`**。Python `str.encode()` 默认即 UTF-8，故字节等价，spec 写 `encode("utf-8")` 结论正确但需注明源码是默认编码——TS 侧用 `Buffer.from(s, "utf8")` 即可。已加注。

3. **§7.5 SQL Preview 步骤顺序修正（轻微但 load-bearing）**。源码 `_preview_sql`（行 305-309）顺序是：先 `re.sub(r"<!--[\s\S]*?-->", "", body)` 去注释 → 再 `re.sub(r"\s+", " ", cleaned).strip()` 压空白+strip → 再 `[:256]` 截断。spec §7.5 顺序正确，但把 strip 归到第 2 步、截断为第 3 步，与源码一致。✓ 仅确认。

4. **§7.6 include_refids 限定规则措辞误导**。spec 写 "refid 含 `.` → 将所有 `.` 替换为 `::`"，源码（行 328）确为 `refid.replace(".", "::")`（全部替换）。但 spec §7.3 的设计 docstring（行 316-317）说的是"替换最后一个 `.`"，**实际实现是全部替换**。spec §7.6 描述的是真实行为（全部替换），正确。✓ 仅确认，并标注 docstring 与实现不符这一怪癖。

5. **§7.3 statement 行号计算细节补全**。`end_line` 用的是 `absolute_idx + len(m.group(0))`（行 227），即匹配**整体**（含结束标签 `</tag>`）的末尾偏移，不是标签体末尾。`_get_line_number` 用 `bisect.bisect_right(self._line_starts, offset)` 并 `max(1, idx)` 保底。spec §7.3 说"由 bisect 二分 `_line_starts` 得出"基本正确，补全 `bisect_right` + `max(1, …)` 细节。

6. **§5.4 attributes 已知键 — file entity 的 `name` 与 statement 的键集合需对齐源码**。statement entity 的 `attributes`（行 250-257）恰好是 6 个键：`qualified_name`、`sql_kind`、`signature`、`sql_preview`、`include_refids`、`namespace`。spec §5.4 列出这 6 个 + `path`，但 `path` 只属于 **file entity**（行 156 `attributes={"path": self.file_path}`），statement entity **没有** `path`。spec 把 `path` 放在 MyBatis 列表末尾并注明"file entity 专用"，措辞正确但易误读，已明确分组。

7. **§3.3 退出码非 0 时 stderr 来源修正（重要怪癖）**。spec §3.3 写错误消息为 `{stderr[-500:]}`，暗示用启动时捕获的全部 stderr。**实际源码**（行 194-198）：退出码非 0 时执行 `remaining = proc.stderr.read()`，即**重新读取管道中剩余未被进度线程消费的内容**，取其 `[-500:]`。由于进度线程（daemon）已 join(timeout=5) 并大概率读空了 stderr，`remaining` 往往为空或只剩尾部残留。TS 复现时需注意：错误消息里的 stderr 是"进度线程消费后剩余的部分"，不是完整 stderr。已修正描述。

8. **§3.4 `done` 日志计数键确认**。`_stream_progress`（行 98-104）`done` 分支读 `data.get("entities",0)`、`data.get("relations",0)`、`data.get("errors",0)`。spec §3.4 表格正确。✓

9. **§3.4 `progress` 分支前置条件补全**。源码（行 86）条件是 `if msg_type == "progress" and progress_callback`——即 `progress_callback` 为 None 时即使收到 progress 也**不调用**。spec 未提此短路，已补。

10. **§8 步骤 1 措辞修正（非 java 返回的 meta）**。源码（行 36）返回 `StructureFacts(meta={"language": language, "message": "仅支持 java，其余返回空"})`，其中 `language` 是**已 lower() 的值**。spec §8 步骤 1 描述一致。✓ 仅确认。

11. **§8 步骤 3 `repo_path` 取值有 fallback（怪癖补全）**。`_extract_mybatis_xml`（行 90）用 `getattr(source, "repo_path", None) or getattr(source, "path", None)`——优先 `repo_path`，回退 `path`，二者都无则 warning 跳过返回 0。spec §8 未提此 fallback，已补。

---

以下为**修正后的完整规范**。除上述编号修正外，其余条目经回源抽查与源码逐字一致（命令行参数、Popen 参数、超时/退出码、枚举值、StructureFacts 查询方法、临时目录前缀均已逐字核对无误）。

---

# javaparser-bridge 子进程接口 + StructureFacts 数据模型 — 行为规范（修正版）

## 1. JAR 文件定位

**模块**：`src/structure/javaparser_bridge.py` → `_find_bridge_jar()`

**相对路径常量**（逐字，模块级常量 `_JAR_RELATIVE_PATH`）：
```
javaparser-bridge/target/javaparser-bridge-1.0.0-shaded.jar
```

**查找顺序**（两步，第一个 `.exists()` 为真即返回）：
1. `Path.cwd() / _JAR_RELATIVE_PATH`
2. `Path(__file__).resolve().parents[2] / _JAR_RELATIVE_PATH`（向上两级 = `knowledge-engineering/`）

两步都不存在返回 `None`。`run_javaparser_bridge` 拿到 `None` 时抛 `FileNotFoundError`，消息固定（注意源码用了 f-string 但无插值）：
```
JavaParser Bridge JAR not found. Run: cd javaparser-bridge && mvn clean package -DskipTests
```

另有 `is_javaparser_available()`：`shutil.which("java")` 存在 **且** jar 存在才返回 `True`。

**绝对路径**（本机）：`/Users/java/knowledge-engineering/javaparser-bridge/target/javaparser-bridge-1.0.0-shaded.jar`

---

## 2. modules.json 生成

**函数**：`_write_modules_json(source: CodeInputSource, tmp_dir: Path) -> Path`

写入路径：`{tmp_dir}/modules.json`，`json.dumps(..., ensure_ascii=False)`，`encoding="utf-8"`。

**JSON schema**（逐字段）：
```json
{
  "repo_path": "<source.repo_path>",
  "modules": [
    {
      "id": "<m.id>",
      "name": "<m.name or m.id>",
      "path": "<m.path or m.id>",
      "business_domains": "<m.business_domains or []>"
    }
  ],
  "file_module_map": {
    "<f.path>": "<f.module_id>"
  },
  "extract_cross_service": true
}
```

- `modules` 由 `source.modules` 推导；`name`/`path` 为 falsy 时回退到 `m.id`；`business_domains` 为 falsy 时为 `[]`。
- `file_module_map` 由 `source.files` 推导：`{f.path: f.module_id}`。
- `extract_cross_service` **硬编码 `True`**，不受 `run_javaparser_bridge` 的 `extract_cross_service` 参数控制（该参数当前不进入 JSON，也不进入 CLI）。

---

## 3. subprocess 调用规范

**函数**：
```python
run_javaparser_bridge(
    source: CodeInputSource,
    extract_cross_service: bool = True,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    java_cmd: str = "java",
    jvm_args: Optional[list[str]] = None,
    timeout_seconds: int = 600,
) -> StructureFacts
```

`jvm_args is None` 时在函数内赋默认 `["-Xmx2g"]`（默认参数用 `None` 哨兵，避免可变默认参数陷阱）。

### 3.1 命令行参数（完整顺序，逐字）
```
[java_cmd, *jvm_args, "-jar", str(jar_path),
 "--repo-path", source.repo_path,
 "--modules-json", str(modules_json),
 "--output", str(output_file)]
```

| 位置 | 值 | 备注 |
|------|-----|------|
| `java_cmd` | 默认 `"java"` | 可覆盖为绝对路径 |
| `jvm_args` | 默认 `["-Xmx2g"]` | 单元素数组；调用方可传空列表或多项 |
| `--repo-path` | `source.repo_path` | 仓库根路径 |
| `--modules-json` | `{tmp_dir}/modules.json` 路径 | 由 `_write_modules_json` 生成 |
| `--output` | `{tmp_dir}/structure_facts.json` | jar 输出到此文件 |

**无 `--extract-cross-service` CLI 参数**；该语义只经 `modules.json` 内 `extract_cross_service: true` 传递。CLI 参数均用连字符（`--repo-path`/`--modules-json`/`--output`），非下划线。

### 3.2 Popen 参数（逐字）
```python
subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    encoding="utf-8",
    errors="replace",
)
```

### 3.3 等待、超时、退出码
- `proc.wait(timeout=timeout_seconds)`，默认 `timeout_seconds=600`。
- 捕获 `subprocess.TimeoutExpired` → `proc.kill()` → 抛 `RuntimeError(f"JavaParser Bridge timed out after {timeout_seconds}s")`。
- wait 之后 `progress_thread.join(timeout=5)`。
- **退出码非 0**：先 `remaining = proc.stderr.read() if proc.stderr else ""`，再抛 `RuntimeError(f"JavaParser Bridge failed (exit code {proc.returncode}): {remaining[-500:]}")`。
  - **怪癖**：`remaining` 是**进度线程消费 stderr 之后管道里剩余的内容**（不是启动以来的完整 stderr）。因进度 daemon 线程通常已读空 stderr，`remaining` 往往为空或仅含尾部残留。TS 复现需自行决定是否缓存完整 stderr 以构造同样的尾部 500 字符（若要严格对齐，须复现"进度线程先消费、剩余再取尾 500"的行为；通常实现为缓冲所有 stderr 行后取 `slice(-500)` 已足够等价，差异仅在极端缓冲竞态）。

### 3.4 stderr 进度流（NDJSON）

后台线程 `_stream_progress(stderr_pipe, progress_callback)`，`daemon=True`，逐行 `line.strip()`，空行跳过，每行尝试 `json.loads`：

| `type` 字段 | 行为 | 前置条件 |
|-------------|------|----------|
| `"progress"` | `progress_callback(data.get("current",0), data.get("total",0), data.get("message",""))` | **仅当 `progress_callback` 非 None** 才调用（短路） |
| `"file_error"` | `_LOG.warning("JavaParser file error: %s: %s", data.get("file",""), data.get("error",""))` | — |
| `"done"` | `_LOG.info("JavaParser done: %d entities, %d relations, %d errors", data.get("entities",0), data.get("relations",0), data.get("errors",0))` | — |
| 其它 `type` | 无操作 | — |
| `json.JSONDecodeError`（非 JSON 行） | `_LOG.debug("JavaParser stderr (non-JSON): %s", line)` | — |

整个循环包在 `try/except Exception` 中，异常仅 `_LOG.debug` 记录，不抛。`stderr_pipe is None` 直接 return。

### 3.5 stdout
打开为 `PIPE` 但**从不读取**（防 pipe 满阻塞），进程结束随临时资源丢弃。

### 3.6 输出读取
- 若 `not output_file.exists()` → 抛 `RuntimeError("JavaParser Bridge did not produce output file")`。
- 否则 `json_text = output_file.read_text(encoding="utf-8")`，`facts = StructureFacts.model_validate_json(json_text)`。

---

## 4. 临时目录

`tempfile.TemporaryDirectory(prefix="javaparser_bridge_")`，整个调用包在 `with` 块内，结束自动清理（含 `modules.json` 与 `structure_facts.json`）。

---

## 5. structure_facts.json Schema

jar 输出 JSON，由 `StructureFacts.model_validate_json` 反序列化。

### 5.1 顶层结构（Pydantic `StructureFacts`）
```typescript
{
  entities: StructureEntity[],   // default []
  relations: StructureRelation[], // default []
  meta: Record<string, any>      // default {}；如 repo_version, parsed_at
}
```

### 5.2 StructureEntity
| 字段 | 类型 | 必须 | 默认 | 说明 |
|------|------|------|------|------|
| `id` | `string` | 是 | — | entity_id，canonical_v1（见 §7） |
| `type` | `EntityType` | 是 | — | 枚举值字符串 |
| `name` | `string` | 是 | — | 简短名称 |
| `location` | `string \| null` | 否 | `None` | `"文件:行"` 或 `"文件:行-行"` |
| `module_id` | `string \| null` | 否 | `None` | 所属模块/服务 id |
| `language` | `string \| null` | 否 | `None` | 如 `"java"`、`"xml"` |
| `attributes` | `Record<string, any>` | 否 | `{}` | 见 §5.4 |

### 5.3 StructureRelation
| 字段 | 类型 | 必须 | 默认 |
|------|------|------|------|
| `type` | `RelationType` | 是 | — |
| `source_id` | `string` | 是 | — |
| `target_id` | `string` | 是 | — |
| `attributes` | `Record<string, any>` | 否 | `{}` |

### 5.4 attributes 已知键

**Java AST 实体**（jar 产出，字段由 Java 侧决定，TS 只需能解析、不需复现）：
- `signature`、`visibility`（`"public"`/`"private"`/`"protected"`/`"package"`）、`path`（`api_endpoint` 路径）等。

**MyBatis XML — file entity**（`_create_file_entity` 产出，TS 必须逐字复现，见 §7.2）：
- `path`：`file_path`（相对路径字符串）。**file entity 仅此一个 attribute 键。**

**MyBatis XML — statement (method) entity**（`_extract_mapper` 产出，TS 必须逐字复现，见 §7.3）。`attributes` 恰好 6 键，顺序如下：
- `qualified_name`：`"<namespace>::<statement_id>"`
- `sql_kind`：`"select"`/`"insert"`/`"update"`/`"delete"`/`"sql"`（已 `.lower()`）
- `signature`：见 §7.4
- `sql_preview`：见 §7.5
- `include_refids`：`string[]`，见 §7.6
- `namespace`：mapper namespace 字符串

> statement entity **不含** `path` 键；`path` 是 file entity 专属。

---

## 6. EntityType 与 RelationType 枚举

### EntityType（`str, Enum`）
`"file"`, `"module"`, `"package"`, `"class"`, `"interface"`, `"enum"`, `"annotation_type"`, `"method"`（含 MyBatis XML statement）, `"field"`, `"parameter"`, `"service"`, `"api_endpoint"`。

### RelationType（`str, Enum`）
`"contains"`, `"calls"`, `"extends"`, `"implements"`, `"depends_on"`, `"belongs_to"`, `"relates_to"`, `"annotated_by"`, `"service_calls"`, `"service_exposes"`, `"binds_to_service"`。

---

## 7. entity_id 生成规范（canonical_v1）

### 7.1 Java AST 实体（jar 侧生成，TS 无需复现）
jar 生成，Python/TS spawn 同一 jar → 天然一致。前缀模式（观察值）：`file//…`、`class//…`、`method//…`。

### 7.2 MyBatis XML 文件实体（`_create_file_entity`，TS 必须逐字节复现）
```
name       = file_path.rsplit("/", 1)[-1]            # 取相对路径最后一段 = 文件名（basename）
sha        = sha256(file_path.encode()).hexdigest()[:16]
entity_id  = f"file//{sha}"
```
- `file_path`：相对仓库根的路径字符串（`/` 分隔；上游已 `str(rel).replace("\\", "/")`，见 §8）。
- `.encode()` 为**裸调用**，等价 UTF-8（TS：`Buffer.from(file_path, "utf8")`）。
- 哈希：SHA-256，hexdigest 前 **16** 个十六进制字符（= 8 字节，小写）。
- 前缀 `file//`（双斜线）。
- 其它字段：`type=FILE`，`name`=basename（见上），`location=f"{file_path}:1"`（固定第 1 行），`language="xml"`，`attributes={"path": file_path}`。

### 7.3 MyBatis XML Statement 实体（`_extract_mapper`，TS 必须逐字节复现）
```
qualified = f"{namespace}::{statement_id}"
id_hash   = sha256(f"{file_path}//{qualified}".encode()).hexdigest()[:16]
entity_id = f"method//{id_hash}"
```
- 哈希输入字符串：`"{file_path}//{namespace}::{statement_id}"`（`file_path` 与 `qualified` 用**双斜线** `//` 连接；`namespace` 与 `statement_id` 用**双冒号** `::` 连接）。
- `.encode()` 裸调用（UTF-8）；SHA-256 hexdigest 前 **16** 位（小写）；前缀 `method//`（双斜线）。
- 其它字段：`type=METHOD`，`name=statement_id`，`language="xml"`，`location=f"{file_path}:{start_line}-{end_line}"`。
- **行号计算**：
  - `absolute_idx = body_start + m.start()`（statement 匹配在原始 source 中的绝对起始偏移）。
  - `start_line = _get_line_number(absolute_idx)`。
  - `end_line = _get_line_number(absolute_idx + len(m.group(0)))`，其中 `m.group(0)` 是**完整匹配**（含 `<tag …>…</tag>` 结束标签）。
  - `_get_line_number(offset)`：`idx = bisect.bisect_right(_line_starts, offset)`，返回 `max(1, idx)`（1-based）。
  - `_line_starts`：`[0]` 起，遍历 source 每遇 `"\n"` 追加 `i+1`。

### 7.4 签名生成规则（`_build_signature(elem_type, attrs, is_sql_fragment)`）
| 条件 | 返回值 |
|------|--------|
| `is_sql_fragment`（即 `elem_type == "sql"`） | `"<sql>"` |
| 否则基础 | `elem_type.upper()`（`"SELECT"`/`"INSERT"`/`"UPDATE"`/`"DELETE"`） |
| 有 `resultType="Y"` | 追加 ` result=Y` |
| 有 `parameterType="A"` | 追加 ` param=A` |

拼接方式：`parts=[verb]`，按序 append `f"result={...}"`、`f"param={...}"`，最后 `" ".join(parts)`。例：`<select id=X resultType=Y parameterType=A>` → `"SELECT result=Y param=A"`。属性用正则 `\bresultType\s*=\s*"([^"]+)"` / `\bparameterType\s*=\s*"([^"]+)"` 从 attrs 串提取。

### 7.5 SQL Preview 规则（`_preview_sql`，上限常量 `_SQL_PREVIEW_LIMIT = 256`）
顺序：
1. `re.sub(r"<!--[\s\S]*?-->", "", body)`——去 XML 注释（`[\s\S]` 跨行、`*?` 非贪婪）。
2. `re.sub(r"\s+", " ", cleaned).strip()`——所有连续空白压成单空格，再 strip 两端。
3. `cleaned[:256]`——截前 256 字符。

### 7.6 include_refids 限定规则（`_extract_include_refids(body, namespace)`）
正则 `<include\b[^>]*\brefid\s*=\s*"([^"]+)"` 扫 body 内每个 `<include refid="X"…`：
- refid **含 `"."`** → `refid.replace(".", "::")`（**全部** `.` 替换为 `::`，非仅最后一个）。
- refid **不含 `"."`** → `f"{namespace}::{refid}"`。
- 返回 `list[str]`。

> 怪癖：源码 docstring（行 316-317）口述"替换最后一个 `.`"，但**实际实现是全部替换**。以实现为准（全部替换）。

---

## 8. run_structure_layer 编排逻辑

**入口**：
```python
run_structure_layer(
    source: CodeInputSource,
    extract_cross_service: bool = True,
    progress_callback: Optional[Any] = None,
    layering: "LayeringConfig | None" = None,
) -> StructureFacts
```

**流程**：
1. `language = (source.language or "java").lower()`；若 `!= "java"`，立即返回 `StructureFacts(meta={"language": language, "message": "仅支持 java，其余返回空"})`（`language` 为已 lower 值）。
2. 进度 `(0,3,"正在解析 Java 代码…")`；调 `run_javaparser_bridge(source=…, extract_cross_service=…, progress_callback=…)`（注意：**未透传 `java_cmd`/`jvm_args`/`timeout_seconds`**，全用默认值）。
3. 进度 `(1,3,…)`；调 `_extract_mybatis_xml(source, facts)`：
   - `repo_path = getattr(source, "repo_path", None) or getattr(source, "path", None)`（**有 fallback**：先 `repo_path` 再 `path`；都无 → warning 返回 0）。
   - `Path(repo_path)`，不存在 → warning 返回 0。
   - `root.rglob("*.xml")`，对每个 `rel = xml_path.relative_to(root)`，若路径任一段 `parts` 命中 `{"target","build","node_modules",".git","dist"}` 则跳过。
   - 读文件 `read_text(encoding="utf-8", errors="replace")`（OSError → warning continue）。
   - `rel_str = str(rel).replace("\\", "/")`（**反斜线换正斜线**，喂给 extractor 的就是这个 `rel_str`）。
   - `MyBatisXmlExtractor(rel_str, content).extract()` → `facts.entities.extend(result.entities)`，`facts.relations.extend(result.relations)`，`added += len(result.entities)`；`result.errors` 逐条 warning。
4. 进度 `(2,3,…)`；调 `_link_java_to_mybatis_xml(facts)` → `synthesize_mybatis_java_xml_relations(facts)`，把返回的合成 CALLS 边 `facts.relations.extend(...)`。
5. `apply_layering(facts, layering)`（函数内延迟 import）：`layering=None` 或 `enabled=False` 时内部返回 `{"skipped": True}` 不改实体；否则对每个 entity 写 `attributes["layer"]`。仅当 `not layer_stats.get("skipped")` 时打日志。
6. 进度 `(3,3, f"代码结构解析完成（{len(facts.entities)} 实体, {len(facts.relations)} 关系）")`，返回 `facts`。

> 所有 `progress_callback(...)` 调用均包在 `if progress_callback:` 内。

---

## 9. StructureFacts 查询方法

| 方法 | 语义 |
|------|------|
| `entity_by_id(eid: str) -> StructureEntity \| None` | 线性扫 `entities`，返回首个 `e.id == eid` 否则 `None` |
| `relations_from(source_id: str, rel_type: RelationType \| None = None) -> list[StructureRelation]` | 过滤 `r.source_id == source_id`；`rel_type` 非 None 再过滤 `r.type == rel_type` |
| `relations_to(target_id: str, rel_type: RelationType \| None = None) -> list[StructureRelation]` | 过滤 `r.target_id == target_id`；`rel_type` 非 None 再过滤 `r.type == rel_type` |

---

## 10. TS 侧实现要点

### spawn 等价方式
```typescript
import { spawn } from "node:child_process";

const jvmArgs = ["-Xmx2g"];
const cmd = [
  "java", ...jvmArgs,
  "-jar", jarPath,
  "--repo-path", source.repoPath,
  "--modules-json", modulesJsonPath,
  "--output", outputFilePath,
];
const proc = spawn(cmd[0], cmd.slice(1), { stdio: ["ignore", "pipe", "pipe"] });
// stderr 设 utf8 + 按行切；stdout 不读（drain 即可，防阻塞）
```

### 关键一致性检查点
- `modules.json` 的 `extract_cross_service` 硬编码 `true`；`name`/`path` 回退到 `id`，`business_domains` 回退 `[]`。
- JVM 默认 `["-Xmx2g"]`（单元素数组）。
- CLI 参数连字符：`--repo-path`/`--modules-json`/`--output`；无 `--extract-cross-service`。
- 超时 600s（默认）；超时 kill 抛 `timed out after {N}s`；退出码非 0 抛 `failed (exit code {N}): {remaining[-500:]}`（remaining = 进度线程消费后剩余 stderr）。
- 输出文件不存在抛 `did not produce output file`。
- stderr 按行 NDJSON，`type` 字段分发；`progress` 仅在有回调时调用。
- **SHA-256 hexdigest 取前 16 个十六进制字符（8 字节，小写）。**
- `file_path` 在哈希前**已**完成 `\\` → `/` 替换（在 `_extract_mybatis_xml` 用 `str(rel).replace("\\", "/")` 完成）。
- file entity 前缀 `file//`，statement entity 前缀 `method//`（均双斜线）；`name` 分别为 basename / `statement_id`。
- file entity attributes 仅 `{path}`；statement entity attributes 为 6 键 `{qualified_name, sql_kind, signature, sql_preview, include_refids, namespace}`，无 `path`。
- statement 哈希输入：`"{file_path}//{namespace}::{statement_id}"`。
- `include_refids`：含 `.` 全替换为 `::`，否则 `namespace::refid`。
- `sql_preview`：去注释 → 压空白+strip → 截 256。

**源文件**（均绝对路径）：
- `/Users/java/knowledge-engineering/src/structure/javaparser_bridge.py`
- `/Users/java/knowledge-engineering/src/structure/runner.py`
- `/Users/java/knowledge-engineering/src/models/structure.py`
- `/Users/java/knowledge-engineering/src/structure/mybatis_extractor.py`（§7 entity_id 字节构成、§7.4–7.6 算法的权威源；spec 原文未列入"源码"清单但其所有 MyBatis 声明依赖此文件，已回源核对）

---

# 【MyBatis 抽取 + entity_id 生成 + link 解析（canonical_v1 门禁③）】

The spec is highly accurate. I verified every claim against source. Here is the corrected complete specification.

## 修正清单

逐条回源核验，重点核查项与结论：

1. **entity_id 字节构成（门禁③）— 全部正确，逐字节复现**
   - file entity：`hashlib.sha256(self.file_path.encode()).hexdigest()[:16]`，前缀 `"file//"`（extractor.py:149-151）✅
   - method entity：`hashlib.sha256(f"{self.file_path}//{qualified}".encode()).hexdigest()[:16]`，前缀 `"method//"`，其中 `qualified = f"{namespace}::{statement_id}"`（extractor.py:230-237）✅
   - `.encode()` 无参 = UTF-8 ✅；`.hexdigest()` 全小写 ✅；`[:16]` = 16 个十六进制字符 ✅；分隔符 `//` 双斜线 ✅

2. **【新增遗漏点】file entity 的 `name` 字段构造方式 spec 未写**：源码 `name = self.file_path.rsplit("/", 1)[-1]`（extractor.py:145）——从右按 `/` 分割一次取最后一段（文件名）。**TS 必须用 `/` 而非平台分隔符**（输入是相对仓库根的正斜线路径）。spec §2.1 仅笼统写"文件名"，已在下方 §8 补精确算法。

3. **【措辞修正】spec §3.1 称 file_path"不做任何 normalize/strip"** — 核实属实（直接 `.encode()`），但 `name` 字段确实做了 `rsplit("/",1)[-1]`。注意：参与 **hash 的是完整 `file_path`，不是 name**，二者互不影响。spec 此处无错，保留并补注。

4. **§5.1 _find_mapper_root**：核实 `ns_match = _NAMESPACE_ATTR_RE.search(m.group(1) or "")`（extractor.py:174）——spec 写"从 `m.group(1)`（属性串）"正确，但源码有 `or ""` 防御（group(1) 理论上不为 None，因 `[^>]*` 可匹配空）。无行为影响，补注。

5. **§5.2 emit CONTAINS relation**：源码 `StructureRelation(type=CONTAINS, source_id=file_node_id, target_id=entity_id)` **未传 attributes**（extractor.py:263-267），靠 Pydantic `Field(default_factory=dict)` 默认 `{}`。spec §2.2 标注"可为空 {}"正确 ✅。

6. **link resolver 全部正确**：Java 索引 key、XML 侧 `namespace.split(".")[-1]`、`rsplit("::", 1)`、`len(candidates) != 1` 保守丢弃、去重键 `f"{java.id}->{xml.id}"`、方向 Java→XML、attributes 三字段（`via` 用 `.` 点号）——逐行核对一致（resolver.py:43-110）✅

7. **正则表达式 7 条 + 魔法数字 256**：与源码逐字符比对一致（extractor.py:36-59）✅

8. **抽查 EntityType/RelationType 枚举值**：`FILE="file"`、`METHOD="method"`、`CONTAINS="contains"`、`CALLS="calls"` 与 structure.py:10-40 一致 ✅

结论：spec 行为完全正确，仅 **§2.1/§8 的 file entity `name` 字段算法缺精确定义**（修正点 2），其余为补注。下方为修正后完整规范。

---

# MyBatis 抽取 + entity_id 生成 + 链接解析 行为规范

**来源文件：**
- `/Users/java/knowledge-engineering/src/structure/mybatis_extractor.py`
- `/Users/java/knowledge-engineering/src/structure/mybatis_link_resolver.py`
- `/Users/java/knowledge-engineering/src/models/structure.py`

---

## 1. 模块总览

| 模块 | 职责 |
|------|------|
| `MyBatisXmlExtractor` | 扫单个 XML 文件，emit `StructureEntity` (file + method) 和 `StructureRelation` (CONTAINS) |
| `synthesize_mybatis_java_xml_relations` | 跨全量 `StructureFacts`，将 XML statement entity 与 Java method entity 用 CALLS 边连起来 |

---

## 2. 数据结构

### 2.1 StructureEntity（Pydantic BaseModel，structure.py:43-51）

```
id:         str                    # canonical_v1 entity id（见第3节）
type:       EntityType             # str enum；本模块用 FILE / METHOD
name:       str                    # 见下；file=文件名（见 §8），method=statement_id
location:   Optional[str] = None   # "path:line" 或 "path:startLine-endLine"
module_id:  Optional[str] = None   # 所属模块（MyBatis extractor 不填 → None）
language:   Optional[str] = None   # file entity = "xml"；method entity = "xml"
attributes: dict[str, Any]         # Field(default_factory=dict)；见各 entity 字段说明
```

### 2.2 StructureRelation（Pydantic BaseModel，structure.py:54-59）

```
type:       RelationType           # 本模块用 CONTAINS / CALLS
source_id:  str
target_id:  str
attributes: dict[str, Any]         # Field(default_factory=dict)；CONTAINS 不传 → 默认 {}
```

> 注：extractor emit CONTAINS 时**未显式传 attributes**（extractor.py:263-267），依赖 Pydantic 默认值得到 `{}`。TS 实现须让 CONTAINS relation 的 attributes 落为空对象 `{}`。

### 2.3 ExtractionResult（dataclass，extractor 内部，extractor.py:62-77）

```
entities:    list[StructureEntity]   # field(default_factory=list)，初始 []
relations:   list[StructureRelation] # field(default_factory=list)，初始 []
errors:      list[dict[str, Any]]    # field(default_factory=list)，非致命错误记录
duration_ms: int = 0                 # 解析耗时（毫秒）
```

errors 元素结构（extractor.py:130-134）：
```
{"message": "MyBatis extraction error: {ExceptionClassName}: {exception}",
 "severity": "error",
 "code": "parse_error"}
```

---

## 3. entity_id 精确字节构成（canonical_v1 门禁③ 关键）

### 3.1 file entity（extractor.py:149-151）

```
hash_input  = self.file_path                   # 原始字符串（Python str），不 normalize/strip
encoding    = UTF-8（.encode() 默认无参）
digest      = sha256(hash_input.encode()).hexdigest()   # 全小写十六进制
id          = "file//" + digest[:16]           # 取前 16 个十六进制字符（= 64 bit）
```

**示例：**
```
file_path   = "mall-swarm/src/main/resources/mapper/UmsRoleDao.xml"
id          = "file//" + sha256("mall-swarm/src/main/resources/mapper/UmsRoleDao.xml").hexdigest()[:16]
```

**关键细节：**
- 参与 hash 的是**完整 `file_path`**（调用方传入的相对仓库根路径），**不做 normalize/strip**
- `.encode()` 无参数 = UTF-8
- `hexdigest()` 全小写
- 取 `[:16]`（16 个十六进制字符，不是 16 字节）
- ⚠️ file entity 的 `name` 字段对 `file_path` 做了 `rsplit("/",1)[-1]`，但**那只影响 name，不影响 hash 输入**（见 §8）

### 3.2 method entity（XML statement，extractor.py:230-237）

```
qualified   = f"{namespace}::{statement_id}"   # 先拼 qualified name
hash_input  = f"{self.file_path}//{qualified}" # 中间是双斜线 "//"
encoding    = UTF-8（.encode() 默认）
digest      = sha256(hash_input.encode()).hexdigest()
id          = "method//" + digest[:16]
```

**示例：**
```
file_path    = "mall-swarm/src/main/resources/mapper/UmsRoleDao.xml"
namespace    = "com.macro.mall.dao.UmsRoleDao"
statement_id = "getMenuList"
qualified    = "com.macro.mall.dao.UmsRoleDao::getMenuList"
hash_input   = "mall-swarm/src/main/resources/mapper/UmsRoleDao.xml//com.macro.mall.dao.UmsRoleDao::getMenuList"
id           = "method//" + sha256(hash_input.encode()).hexdigest()[:16]
```

**关键细节：**
- `file_path` 与 `qualified` 之间分隔符是 `//`（双斜线）
- `qualified` 内部 namespace 与 id 之间分隔符是 `::`（双冒号）
- `qualified` 先构造，再整体拼入 hash 输入，三段间无空格
- 前缀 `"method//"` 直接拼接，不经过 hash

### 3.3 前缀汇总

| entity 类型 | 前缀 | hash 输入字符串 |
|------------|------|-----------------|
| FILE | `file//` | `file_path` |
| METHOD (XML statement) | `method//` | `file_path + "//" + namespace + "::" + statement_id` |

---

## 4. MyBatisXmlExtractor — 公开接口

### 4.1 构造函数（extractor.py:94-100）

```python
MyBatisXmlExtractor(file_path: str, source: str)
```

- `file_path`：相对仓库根的路径字符串（用于 entity id hash、location、name）
- `source`：XML 文件完整文本内容（字符串）
- 构造时立即调用 `self._line_starts = self._compute_line_starts()`（每行首字符偏移，供行号查找）

### 4.2 extract() -> ExtractionResult（extractor.py:104-138）

**流程：**

1. `t0 = time.time()`，`result = ExtractionResult()`
2. 构造 file entity（`_create_file_entity()`，见 §3.1/§8），`result.entities.append(file_entity)`
3. `try:` 块内调用 `_find_mapper_root()`
   - 返回 `None` → 非 mapper XML，跳过（只有 file entity 输出）
   - 返回 `(namespace, body_start, body_end)` → 元组解包后调用 `_extract_mapper(file_entity.id, namespace, body_start, body_end, result)`
4. `except Exception as e:` 捕获，append 到 `result.errors`（非致命，pipeline 继续）
5. `result.duration_ms = int((time.time() - t0) * 1000)`
6. 返回 `result`

> 注意：file entity 在 try 之外构造（步骤 2），所以即使 mapper 解析抛异常，file entity 仍已入 entities。

---

## 5. 内部 helper 算法

### 5.1 _find_mapper_root() -> tuple[str, int, int] | None（extractor.py:159-183）

**步骤：**

1. `m = _MAPPER_OPEN_RE.search(self.source)` — 找第一个 `<mapper ...>` 开标签（`re.search`，非 match，从任意位置找，支持文件头 XML 声明/DTD）
2. `if not m: return None`
3. `ns_match = _NAMESPACE_ATTR_RE.search(m.group(1) or "")` — 从属性串提取 namespace（`m.group(1)` 为捕获的属性串，`or ""` 防御性兜底）
4. `if not ns_match: return None`
5. `body_start = m.end()`（`<mapper ...>` 关闭 `>` 之后的偏移）
6. `close = self.source.find("</mapper>", body_start)`
7. `body_end = close if close >= 0 else len(self.source)`
8. `return ns_match.group(1), body_start, body_end`

### 5.2 _extract_mapper(file_node_id, namespace, body_start, body_end, result)（extractor.py:185-267）

**步骤：**

1. `body = self.source[body_start:body_end]`（切片，只在这段文本内跑 statement regex）
2. `for m in _STATEMENT_RE.finditer(body):`
   - `elem_type = m.group(1).lower()`（select/insert/update/delete/sql）
   - `attrs = m.group(2) or ""`（标签属性串）
   - `elem_body = m.group(3) or ""`（标签体 SQL 文本）
   - `id_match = _ID_ATTR_RE.search(attrs)`；`if not id_match: continue`（无 id 属性跳过）
   - `statement_id = id_match.group(1)`
   - `is_sql_fragment = (elem_type == "sql")`
   - 行号计算：
     - `absolute_idx = body_start + m.start()`
     - `start_line = self._get_line_number(absolute_idx)`
     - `end_line = self._get_line_number(absolute_idx + len(m.group(0)))`
   - `qualified = f"{namespace}::{statement_id}"`
   - entity_id 计算（见 §3.2）
   - `signature = self._build_signature(elem_type, attrs, is_sql_fragment)`
   - `sql_preview = self._preview_sql(elem_body)`
   - `include_refids = self._extract_include_refids(elem_body, namespace)`
   - emit `StructureEntity`（type=METHOD，见 §5.3）
   - emit `StructureRelation(type=CONTAINS, source_id=file_node_id, target_id=entity_id)`（无 attributes → `{}`）

### 5.3 method entity 字段（extractor.py:244-258）

```
id:         "method//" + sha256(...)[:16]   # 见 §3.2
type:       EntityType.METHOD
name:       statement_id
location:   f"{self.file_path}:{start_line}-{end_line}"
language:   "xml"
attributes:
    qualified_name:  str            # "{namespace}::{statement_id}"
    sql_kind:        str            # "select"|"insert"|"update"|"delete"|"sql"（已 lower）
    signature:       str            # 见 _build_signature
    sql_preview:     str            # 见 _preview_sql，最长 256 字符
    include_refids:  list[str]      # 见 _extract_include_refids
    namespace:       str            # 原始 namespace 值
```

> module_id 不设 → None。attributes **键顺序**为 qualified_name → sql_kind → signature → sql_preview → include_refids → namespace（如下游对序列化顺序敏感，TS 须对齐；JSON 对象语义上无序，一般无影响）。

### 5.4 _build_signature(elem_type, attrs, is_sql_fragment) -> str（@staticmethod，extractor.py:269-295）

| 条件 | 返回值 |
|------|--------|
| `is_sql_fragment == True` | `"<sql>"`（直接 return，不看 attrs） |
| 否则 | `" ".join(parts)`，见下 |

**算法（非 sql 分支）：**
- `verb = elem_type.upper()`
- `rt = _RESULT_TYPE_ATTR_RE.search(attrs)`
- `pt = _PARAMETER_TYPE_ATTR_RE.search(attrs)`
- `parts = [verb]`
- `if rt: parts.append(f"result={rt.group(1)}")`
- `if pt: parts.append(f"param={pt.group(1)}")`
- `return " ".join(parts)`

举例：`SELECT`、`SELECT result=Y`、`INSERT param=A`、`SELECT result=Y param=A`、`UPDATE`、`DELETE`。
顺序固定为 `verb [result=...] [param=...]`（result 在 param 前）。

### 5.5 _preview_sql(body) -> str（@staticmethod，extractor.py:297-309）

```
1. cleaned = re.sub(r"<!--[\s\S]*?-->", "", body)   # 去 XML 注释
2. cleaned = re.sub(r"\s+", " ", cleaned).strip()    # 压缩空白 + 去首尾空白
3. return cleaned[:_SQL_PREVIEW_LIMIT]                # 截取前 256 字符
```

`_SQL_PREVIEW_LIMIT = 256`（魔法数字，按字符计，非字节）。

### 5.6 _extract_include_refids(body, namespace) -> list[str]（@staticmethod，extractor.py:311-332）

`for m in _INCLUDE_REFID_RE.finditer(body):`，`refid = m.group(1)`

| 条件 | 输出 |
|------|------|
| refid 中**有** `"."` | `refid.replace(".", "::")`（全部 `.` 替换为 `::`） |
| refid 中**无** `"."` | `f"{namespace}::{refid}"`（同 mapper 内引用，补全 namespace） |

> 源码判断顺序是 `if "." in refid:` 先，`else` 后。返回 `list[str]`（可为空）。

### 5.7 行号计算

`_compute_line_starts() -> list[int]`（构造时运行，extractor.py:334-348）：
- `starts = [0]`
- `for i, ch in enumerate(self.source): if ch == "\n": starts.append(i + 1)`
- 结果：每行首字符偏移列表（0-indexed）

`_get_line_number(offset) -> int`（1-based，extractor.py:350-362）：
- `import bisect`（函数内 lazy import）
- `idx = bisect.bisect_right(self._line_starts, offset)`
- `return max(1, idx)`

> ⚠️ 偏移按 **Python str 字符索引**（`enumerate(self.source)` 逐字符 + 切片 `m.start()`），非字节偏移。源含多字节字符时，TS 须以 UTF-16/码点策略对齐 Python str 语义（Python str 按 Unicode 码点计数）。
> end_line 传入 `absolute_idx + len(m.group(0))`（含整个匹配的闭合标签长度），故 end_line 是闭合标签字符结束所在行。

---

## 6. 正则表达式完整列表（魔法值，extractor.py:36-59）

| 变量名 | 模式 | flags |
|--------|------|-------|
| `_MAPPER_OPEN_RE` | `r"<mapper\b([^>]*)>"` | `re.IGNORECASE` |
| `_NAMESPACE_ATTR_RE` | `r'\bnamespace\s*=\s*"([^"]+)"'` | 无 |
| `_STATEMENT_RE` | `r"<(select\|insert\|update\|delete\|sql)\b([^>]*)>([\s\S]*?)</\1>"` | `re.IGNORECASE` |
| `_ID_ATTR_RE` | `r'\bid\s*=\s*"([^"]+)"'` | 无 |
| `_RESULT_TYPE_ATTR_RE` | `r'\bresultType\s*=\s*"([^"]+)"'` | 无 |
| `_PARAMETER_TYPE_ATTR_RE` | `r'\bparameterType\s*=\s*"([^"]+)"'` | 无 |
| `_INCLUDE_REFID_RE` | `r'<include\b[^>]*\brefid\s*=\s*"([^"]+)"'` | 无 |

**魔法数字：** `_SQL_PREVIEW_LIMIT = 256`

> ⚠️ TS 复现注意：`\1` 是反向引用（`_STATEMENT_RE` 要求闭合标签同名）；`re.IGNORECASE` 仅用于 `_MAPPER_OPEN_RE` 与 `_STATEMENT_RE`，其余属性提取正则**大小写敏感**（属性名 `namespace`/`id`/`resultType`/`parameterType`/`refid` 须精确匹配大小写）。`\b` 单词边界须在 TS 正则中保留。

---

## 7. synthesize_mybatis_java_xml_relations — 公开接口（resolver.py:31-110）

### 7.1 签名

```python
synthesize_mybatis_java_xml_relations(facts: StructureFacts) -> list[StructureRelation]
```

- 入参：完整 `StructureFacts`（含 Java method entity 和 XML method entity）
- 出参：新合成的 `StructureRelation` 列表（**不**直接 append 到 facts，由调用方控制时机）

### 7.2 算法

**Step 1：建 Java method 索引（resolver.py:43-69）**

```
java_index: dict[str, list[StructureEntity]] = {}
for e in facts.entities:
    if e.type != EntityType.METHOD: continue
    if (e.language or "").lower() == "xml": continue   # 排除 XML，剩下视作 Java 候选

    qn = e.attributes.get("qualified_name", "")
    if qn and "::" in qn:
        parts = qn.split("::")
        class_fqn = parts[-2]                  # 倒数第二段，如 "com.x.UserDao"
        method_name = parts[-1]                # 最后一段，如 "findById"
        class_name = class_fqn.split(".")[-1]  # "UserDao"
    else:
        class_name = e.attributes.get("class_name", "") or ""
        method_name = e.name or ""
        if not class_name or not method_name: continue   # 任一为空则跳过

    key = f"{class_name}::{method_name}"
    java_index.setdefault(key, []).append(e)
```

> ⚠️ FQN 分支用 `qn.split("::")` 取 `parts[-2]`/`parts[-1]`（不是 rsplit）；若 qn 含多个 `::`，class_fqn 取倒数第二段。

**Step 2：遍历 XML method 匹配（resolver.py:71-110）**

```
new_edges: list[StructureRelation] = []
seen: set[str] = set()
for xml in facts.entities:
    if xml.type != EntityType.METHOD: continue
    if (xml.language or "").lower() != "xml": continue
    qn = xml.attributes.get("qualified_name", "")
    if "::" not in qn: continue
    namespace, statement_id = qn.rsplit("::", 1)        # rsplit 只切最后一个 ::
    if not namespace or not statement_id: continue
    class_name = namespace.split(".")[-1]               # namespace 最后一段
    candidates = java_index.get(f"{class_name}::{statement_id}", [])
    if len(candidates) != 1: continue                   # 0 或 >1 均跳过（保守）
    java = candidates[0]
    key = f"{java.id}->{xml.id}"
    if key in seen: continue
    seen.add(key)
    new_edges.append(StructureRelation(
        type=RelationType.CALLS,
        source_id=java.id,
        target_id=xml.id,
        attributes={
            "synthesizedBy": "mybatis-java-xml",
            "provenance":    "heuristic",
            "via":           f"{class_name}.{statement_id}",
        }))
return new_edges
```

### 7.3 匹配规则总结

| 关键点 | 值 |
|--------|-----|
| 匹配键（XML 侧） | `namespace.split(".")[-1]` + `"::"` + `statement_id` |
| 匹配键（Java 侧，FQN 分支） | `class_fqn.split(".")[-1]` + `"::"` + `method_name`（class_fqn=`parts[-2]`，method_name=`parts[-1]`） |
| 匹配键（Java 侧，fallback 分支） | `attributes["class_name"]` + `"::"` + `e.name` |
| XML 侧拆分 | `qn.rsplit("::", 1)` — 只切最后一个 `::` |
| Java FQN 侧拆分 | `qn.split("::")` — 全切，取 `[-2]`/`[-1]` |
| 多匹配处理 | 保守丢弃（`len(candidates) != 1` 均跳过，含 0 和 >1） |
| 去重键 | `f"{java.id}->{xml.id}"` |
| 关系方向 | `source_id = java.id`，`target_id = xml.id`（Java → XML） |
| 关系类型 | `RelationType.CALLS`（`"calls"`） |

### 7.4 CALLS relation attributes 字段（resolver.py:103-107）

```
synthesizedBy: "mybatis-java-xml"               # 固定字面字符串
provenance:    "heuristic"                       # 固定字面字符串
via:           f"{class_name}.{statement_id}"    # ⚠️ 点 "." 分隔，不是 "::"
```

---

## 8. file entity 字段（extractor.py:142-157）

```
id:         "file//" + sha256(self.file_path.encode()).hexdigest()[:16]   # 见 §3.1
type:       EntityType.FILE
name:       self.file_path.rsplit("/", 1)[-1]     # ⚠️ 从右按 "/" 分割一次取最后一段（文件名）
location:   f"{self.file_path}:1"                  # 固定指向第 1 行
language:   "xml"
module_id:  None（不设）
attributes: {"path": self.file_path}              # 原始传入的完整 file_path
```

> ⚠️ **name 构造（补正点）**：`rsplit("/", 1)[-1]` 用正斜线 `/` 分割（输入是相对仓库根的 POSIX 路径），**TS 不可用平台分隔符**（须固定用 `/`）。若 `file_path` 不含 `/`，`rsplit` 返回整串即文件名本身。

---

## 9. 局限性与怪癖

1. **正则不处理嵌套同名标签**：`_STATEMENT_RE` 用 `[\s\S]*?` 非贪婪 + `\1` 反向引用，若 mapper body 内嵌套同名顶层标签（极罕见），会提前截断 — 已知可接受缺陷。
2. **`</mapper>` 搜索用 `str.find()`**（精确小写 `</mapper>`），`</Mapper>` 大写变体会漏掉 body_end，退化为 `len(source)`。（注：`<mapper>` 开标签识别用了 IGNORECASE，但闭合标签查找没有，存在不对称。）
3. **Java entity 语言判断是反向的**：`javaparser-bridge` 不显式设 `e.language`，故 `(e.language or "").lower() != "xml"` 即视为 Java 候选；同理 XML 侧用 `== "xml"` 正向判断。`(e.language or "")` 处理 `language=None` 的情况。
4. **`include_refids` 跨 mapper 替换**：含 `.` 时全部 `.` → `::` 是简化处理，resolver 做二次匹配。
5. **`_get_line_number` 的 end_line**：传入 `absolute_idx + len(m.group(0))`（含闭合标签），end_line 是闭合标签结束字符所在行。
6. **`duration_ms`** 只精确到毫秒（`int(... * 1000)` 截断），精度取决于平台 `time.time()`。
7. **offset/行号按 Python str 字符（Unicode 码点）计**，非字节；多字节字符场景 TS 须对齐码点语义。
8. **java_index FQN 分支用 `split("::")` 取 `[-2]`**：若 qualified_name 形如 `a::b::c`，class_fqn=`b`、method_name=`c`，首段 `a` 被丢弃。

---

## 10. 外部依赖

| 依赖 | 用途 |
|------|------|
| `hashlib`（stdlib，顶层 import） | sha256 计算 entity_id |
| `re`（stdlib，顶层 import） | XML 结构解析 |
| `time`（stdlib，顶层 import） | 解析耗时计时 |
| `bisect`（stdlib，**函数内 lazy import**） | 行号二分查找 |
| `dataclasses`（stdlib） | `@dataclass` / `field` 定义 ExtractionResult |
| `src.models.structure` | `EntityType`, `RelationType`, `StructureEntity`, `StructureRelation`, `StructureFacts`（后者仅 resolver 用） |
| `pydantic`（间接，经 structure.py） | BaseModel / Field |

---

## 11. TS 实现对照备忘（canonical_v1 门禁③ 最小检查单）

```typescript
import { createHash } from "node:crypto";

// file entity id
const fileSha = createHash("sha256").update(filePath, "utf8").digest("hex").slice(0, 16);
const fileEntityId = `file//${fileSha}`;
const fileName = filePath.split("/").pop()!;   // 等价 rsplit("/",1)[-1]，固定用 "/"

// method entity id
const qualified = `${namespace}::${statementId}`;
const hashInput = `${filePath}//${qualified}`;
const methodSha = createHash("sha256").update(hashInput, "utf8").digest("hex").slice(0, 16);
const methodEntityId = `method//${methodSha}`;
```

**必须验证：**
- `update(input, "utf8")` 等价 Python `.encode()`（UTF-8）✅
- `digest("hex")` 等价 Python `.hexdigest()`（全小写十六进制）✅
- `.slice(0, 16)` 等价 Python `[:16]`（前 16 个十六进制字符）✅
- hash 输入串分隔符是 `//`（双斜线），qualified 内是 `::`（双冒号）✅
- file `name` 用固定 `/` 分割，不用 `path.basename` / 平台分隔符 ⚠️
- CONTAINS relation attributes 须为 `{}`（空对象），CALLS attributes 三字段中 `via` 用 `.` 点号 ⚠️
- 行号/偏移按 Unicode 码点对齐 Python str（多字节场景）⚠️

---

**核验结论**：spec 对 entity_id 字节构成、jar 无关（XML 侧 sha256 在 Python 生成，TS 须复现）、JSON schema 字段、link resolver 匹配规则的描述**全部正确**。唯一实质补正为 **file entity `name` 字段的精确算法（`rsplit("/",1)[-1]`，固定正斜线）**，原 spec §2.1/§8 仅笼统写"文件名"未给算法。其余为防御性补注（lazy import、attributes 默认 `{}`、IGNORECASE 不对称、码点 vs 字节、FQN 分支 `split` vs `rsplit`）。

---

# 【架构分层采集 layering】

I've now verified all four layering source files plus both model files. Let me check the one factual claim in the spec that I haven't yet confirmed against source: the `_entity_package_path` docstring claim about `None → "."`. I've confirmed the code (`return "." + ".".join(loc...)` with `loc = entity.location or ""`). Let me trace: if location is None, `loc = ""`, `"".split(":")[0]` = `""`, `"".replace(...).split("/")` = `[""]`, `".".join([""])` = `""`, so `"." + ""` = `"."`. Correct.

Now I have everything needed. The spec is highly accurate. Let me compile the corrected full specification with the corrections clause.

# 架构分层采集（Layering）行为规范

## 修正清单

逐条回源核验后，原 spec **整体准确度极高**，发现 **2 处需修正、1 处需补强**，其余抽查点全部一致。

1. **【修正·`LayerMatch.has_attr` 语义描述】** 原 spec 数据结构表写 has_attr 语义为「实体 `attributes` 中存在且 `bool(value) == True` 的键名」——表述本身正确，但源码字段注释（models.py:53）原文是「存在且**非空**的 attribute 键」，且实现判定为 `bool(e.attributes.get(k))`。结论：语义无误，保留。**此条为确认，非修正。**

2. **【修正·`_entity_package_path` 的 docstring 返回值描述自相矛盾】** 源码 docstring（adapter.py:39）写「若 location 为 None，返回**空字符串 '.'**」——这是源码注释里的措辞瑕疵（「空字符串」与「'.'」矛盾）。**实际行为**经字节级追踪为：location=None → `loc=""` → `"".split(":")[0]=""` → `"".split("/")=[""]` → `".".join([""])=""` → `"."+"" = "."`。原 spec 写「location 为 None 时返回 `"."`」**正确**，与实际行为一致，优于源码 docstring。保留 spec 表述。

3. **【补强·`_match` 信号判定顺序编号】** 原 spec「信号判定顺序」小节标题写「五类信号」但实际列了 6 条（源码 `_match` 确为 6 个 `if` 块）。源码 docstring（adapter.py:90）误写「五类信号」。**实际为 6 类**（name_suffix / name_prefix / language / annotation / has_attr / package_contains）。spec 正文 6 条编号正确，仅小节内若引用「五类」需更正为「六类」。已在下方修正。

4. **【确认·DAO 层 `name_suffix`】** 抽查 presets.py:43，DAO 层 `name_suffix=["Mapper", "Dao", "Repository"]`，spec 一致。

5. **【确认·attributes 键名 / `_CLASSLIKE` 集合 / `model_copy` / `setdefault` 双轮 / first-match / 大小写不敏感】** 全部回源逐字核对一致，见下方正文标注的源码行号。

6. **【entity_id 抽查】** 本模块**不生成 entity_id**，仅**读取** `entity.id`（apply.py:98 `class_layer[e.id]`、owner 索引以 `e.id`/`r.source_id`/`r.target_id` 为键）。entity_id 的 sha256 字节构成不在本模块职责内（由 javaparser-bridge jar 与 mybatis_extractor.py 生成）。本模块对 TS 重构的 entity_id 门禁③贡献为：**必须保证读取的 `e.id` 与索引 key 类型/值完全透传，不做任何规范化/trim/lower**——源码确实零加工透传，TS 必须一致。

---

## 模块概览

```
src/structure/layering/
├── __init__.py   — 仅 docstring，无导出（"架构分层采集子包：适配器 / 注册表 / 打标签。"）
├── adapter.py    — 单实体分层判定（RuleBasedAdapter + LayerAdapter Protocol + _entity_package_path）
├── apply.py      — 批量打标签入口（apply_layering，原地写入 StructureFacts；含 _method_owner_index）
├── presets.py    — 内置范式基座（ssm / three_tier 共享 _ssm_preset 工厂）
└── registry.py   — AdapterRegistry：preset 与工程覆盖合并成生效配置
```

配置路径：`project.yaml` → `structure.layering`（`StructureConfig.layering: LayeringConfig`，默认 `default_factory=LayeringConfig` 即 `enabled=False`）。

---

## 数据结构

### `LayerMatch`（`src/config/models.py:33`）

单层匹配规则，各信号之间是 **OR**（任一命中即归该层）。全部字段默认空（不启用该信号）。

| 字段 | 类型 | 默认值 | 语义 |
|---|---|---|---|
| `annotation` | `list[str]` | `Field(default_factory=list)` | 实体 `attributes["annotations"]` 中包含的注解简单名 |
| `name_suffix` | `list[str]` | `default_factory=list` | 实体 `name` 的后缀（`str.endswith`） |
| `name_prefix` | `list[str]` | `default_factory=list` | 实体 `name` 的前缀（`str.startswith`） |
| `package_contains` | `list[str]` | `default_factory=list` | location 推导出的伪包路径中包含的子串 |
| `has_attr` | `list[str]` | `default_factory=list` | 实体 `attributes` 中存在**且非空**（`bool(value) == True`）的键名 |
| `language` | `list[str]` | `default_factory=list` | 实体 `language` 字段（大小写不敏感比较），空=不限语言 |
| `xml_paired` | `bool` | `False` | v1 未实现，Plan 2 预留（跨文件配对 XML） |
| `extends` | `list[str]` | `default_factory=list` | v1 未实现，Plan 2 预留（父类简单名） |
| `implements` | `list[str]` | `default_factory=list` | v1 未实现，Plan 2 预留（接口简单名） |

> TS 注意：默认值用 `default_factory=list` 而非 `[]` 字面量，是为避免可变默认值共享（Python 经典坑）。TS 实现每个实例须独立新建空数组。

### `LayerSpec`（`src/config/models.py:67`）

| 字段 | 类型 | 默认值 | 语义 |
|---|---|---|---|
| `id` | `str` | 必填（无默认） | 层唯一标识符，如 `"entry"` |
| `name` | `str` | 必填（无默认） | 层中文展示名，如 `"入口层"` |
| `match` | `LayerMatch` | `Field(default_factory=LayerMatch)` | 该层的匹配规则 |
| `extractor` | `Optional[str]` | `None` | Plan 2 预留 |
| `extractor_enabled` | `bool` | `True` | Plan 2 预留 |

### `LayeringConfig`（`src/config/models.py:88`）

| 字段 | 类型 | 默认值 | 语义 |
|---|---|---|---|
| `enabled` | `bool` | `False` | 总开关，False 时 apply_layering 直接 no-op |
| `adapter` | `str` | `"three_tier"` | 范式基座 id |
| `fallback_layer` | `str` | `"unknown"` | 全部层规则不命中时的兜底层 id |
| `profile_on_missing` | `bool` | `False` | Plan 3 预留 |
| `layers` | `list[LayerSpec]` | `Field(default_factory=list)` | 工程覆盖层定义；非空则跳过 preset |

> 字段顺序（enabled / adapter / fallback_layer / profile_on_missing / layers）与源码一致。

---

## `adapter.py` — 单实体分层判定

### 公开接口

**`LayerAdapter`（Protocol，adapter.py:16）**

```python
class LayerAdapter(Protocol):
    def classify(self, entity: StructureEntity) -> str: ...
```

结构化接口：任何实现了 `classify(entity) -> str` 的类自动满足，无需继承。TS 对应一个 `interface { classify(entity): string }`。

---

**`RuleBasedAdapter`（adapter.py:52）**

```python
class RuleBasedAdapter:
    def __init__(self, config: LayeringConfig) -> None   # self._config = config
    def classify(self, entity: StructureEntity) -> str
```

- 构造时持有已由 `AdapterRegistry.resolve()` 合并好的生效配置（存于 `self._config`）。
- `classify` 按 `config.layers` 列表顺序遍历，**first-match 语义**（adapter.py:80-85）：第一条 `_match` 命中的层 `id` 直接返回，全不命中返回 `self._config.fallback_layer`。

---

**`_entity_package_path(entity: StructureEntity) -> str`（模块级私有函数，adapter.py:29）**

```
输入: entity.location，形如 "mall-admin/src/main/java/com/macro/mall/service/Foo.java:20"
算法:
  1. loc = entity.location or ""          # None 防御
  2. path = loc.split(":")[0]             # 去掉行号后缀（取冒号切分第一段）
  3. path.replace("\\", "/")              # 统一路径分隔符（Windows '\' → '/'）
  4. .split("/")                          # ["mall-admin",...,"service","Foo.java"]
  5. ".".join(...)                        # "mall-admin.src.main.java.com.macro.mall.service.Foo.java"
  6. "." + ...                            # 前缀 "." → ".mall-admin.....service.Foo.java"
输出: 以 "." 开头的伪包路径字符串
边界: location 为 None → loc="" → 最终返回 "."（实际行为；源码 docstring 措辞「空字符串 '.'」自相矛盾，以实际行为为准）
```

前缀 `"."` 的作用：防止子串误命中——`".service"` 不会命中 `"aservice"`。**TS 必须逐字复现这个前缀拼接**（用 `split(":")[0]`，注意是按第一个冒号切分取首段，不是 rsplit）。

> 注意：是 `loc.split(":")[0]`——若路径里出现多个冒号（如 Windows 盘符 `C:`），只取**第一个冒号之前**。本仓 location 不含盘符前缀，正常路径只有末尾一个 `:行号`，故等效。TS 用 `loc.split(":")[0]` 保持一致。

---

### `_match` 信号判定顺序（**六类**信号，全为 OR，任一 True 即返回 True；adapter.py:87-146）

> 修正：源码 docstring 误称「五类信号」，实际 `_match` 中是 **6 个独立 `if` 块**，按以下顺序短路求值。

`name = e.name or ""`（adapter.py:100，防御性）

1. **`name_suffix`**（:105）：`any(name.endswith(s) for s in m.name_suffix)`
2. **`name_prefix`**（:110）：`any(name.startswith(p) for p in m.name_prefix)`
3. **`language`**（:117）：`m.language and (e.language or "").lower() in [x.lower() for x in m.language]`（先判 `m.language` 非空，再大小写不敏感比较）
4. **`annotation`**（:124-128）：`if m.annotation:` 时 `anns = e.attributes.get("annotations") or []`；`any(a in anns for a in m.annotation)`
5. **`has_attr`**（:133）：`m.has_attr and any(bool(e.attributes.get(k)) for k in m.has_attr)`（值为 falsy 不算命中——None/""/0/[]/False 均不命中）
6. **`package_contains`**（:138-143）：`if m.package_contains:` 时先 `pkg = _entity_package_path(e)`，再 `any(sub in pkg for sub in m.package_contains)`

全不命中返回 `False`（:146）。

**v1 不实现**：`xml_paired`、`extends`、`implements`（Plan 2 预留，`_match` 中无对应分支）。

> 求值顺序怪癖：`name_suffix`/`name_prefix` 这两个块**不先判空列表**（空列表时 `any(...)` 直接为 `False`，无副作用，安全）；而 `language`/`annotation`/`has_attr`/`package_contains` 四块**显式前置判空**（`m.xxx and ...` 或 `if m.xxx:`），主要为短路避免无谓计算（尤其 package_contains 避免无谓调用 `_entity_package_path`）。结果等价，TS 可直接照搬这种「OR 任一命中」语义，前置判空仅为性能/安全，不影响结果。

---

## `apply.py` — 批量打标签入口

### 公开函数

```python
def apply_layering(facts: StructureFacts, config: Optional[LayeringConfig]) -> dict
```

**入参：**
- `facts: StructureFacts`：含 `entities: list[StructureEntity]`、`relations: list[StructureRelation]`、`meta: dict`；**原地修改** `entity.attributes`
- `config: Optional[LayeringConfig]`：`None` 或 `enabled=False` 时直接跳过

**返回值（dict）：**
- `{"applied": int, "skipped": bool}`
- `skipped=True`：`enabled=False`/None 跳过，`applied=0`，facts 完全未修改
- `skipped=False`：正常执行，`applied` = 实际打标签实体数（class-like + method 总计）

---

### 算法步骤

**前置（apply.py:59-73）：**
1. `config is None or not config.enabled` → 返回 `{"applied": 0, "skipped": True}`（短路：config 为 None 时不求值 `config.enabled`）
2. `AdapterRegistry().resolve(config)` → `effective`
3. `RuleBasedAdapter(effective)` → `adapter`
4. `name_by_id = {layer.id: layer.name for layer in effective.layers}`

**Step 1 — class-like 实体（`_CLASSLIKE = {CLASS, INTERFACE, ENUM, ANNOTATION_TYPE}`，apply.py:32-37, 82-101）：**

```
class_layer: dict[str, str] = {}
applied = 0
for e in facts.entities:
    if e.type in _CLASSLIKE:
        layer = adapter.classify(e)
        e.attributes["layer"] = layer
        e.attributes["layer_name"] = name_by_id.get(layer, layer)
        class_layer[e.id] = layer
        applied += 1
```

**Step 2 — METHOD 实体（含继承，apply.py:106-129）：**

```
owner = _method_owner_index(facts)   # methodId→classId
for e in facts.entities:
    if e.type == EntityType.METHOD:
        layer = adapter.classify(e)
        if layer == effective.fallback_layer:       # 自身落到 fallback，才尝试继承
            cid = owner.get(e.id)
            if cid is not None and cid in class_layer:
                layer = class_layer[cid]             # 继承父类层
        e.attributes["layer"] = layer
        e.attributes["layer_name"] = name_by_id.get(layer, layer)
        applied += 1
```

**关键细节：**
- 层标签写入位置：`entity.attributes["layer"]`（layer_id）和 `entity.attributes["layer_name"]`（展示名）。**无独立字段**，全部写进 `attributes` dict（StructureEntity 无 layer 字段，确认 structure.py:43-51）。
- method 继承触发条件：`layer == effective.fallback_layer`（用变量比较，**不是**硬编码 `"unknown"`，因 fallback_layer 可配置覆盖）。
- 继承双门：`cid is not None and cid in class_layer`（用 `is not None` 而非真值判断，语义更精确）。
- `name_by_id.get(layer, layer)`：fallback_layer（如 `"unknown"`）通常不在 layers 列表中，则 `layer_name` 直接等于 `layer_id`。
- METHOD 与 class-like 都计入 `applied`；只有这两类实体被处理（FILE/MODULE/PACKAGE/FIELD/PARAMETER/SERVICE/API_ENDPOINT 等**不打标签**）。
- 日志（apply.py:133）：logger 名为 `__name__` = `"src.structure.layering.apply"`，`INFO` 级，惰性格式化：`_LOG.info("[layering] adapter=%s 打标签实体数=%d", effective.adapter, applied)`。注意打印的是 `effective.adapter`（resolve 后的，工程覆盖路径下与入参 adapter 相同；preset 路径下 model_copy 保留原 adapter）。

---

### `_method_owner_index(facts: StructureFacts) -> dict[str, str]`（apply.py:139）

构建 `methodId → classId` 索引，两轮扫描：

```
owner: dict[str, str] = {}
# 第一轮：BELONGS_TO（source=methodId, target=classId）
for r in facts.relations:
    if r.type == RelationType.BELONGS_TO:
        owner[r.source_id] = r.target_id     # 直接赋值（同 method 多条 BELONGS_TO 时后者覆盖前者）
# 第二轮：CONTAINS（source=classId, target=methodId）补全
for r in facts.relations:
    if r.type == RelationType.CONTAINS:
        owner.setdefault(r.target_id, r.source_id)   # 已有则不覆盖（BELONGS_TO 优先）
return owner
```

- **`setdefault` 语义**：仅 key 不存在时插入 → 保证 BELONGS_TO 优先于 CONTAINS。TS 用 `if (!owner.has(k)) owner.set(k, v)`。
- 第一轮用普通赋值（非 setdefault），故同一 method 多条 BELONGS_TO 时**最后一条胜出**；TS 须同样用直接 `owner.set(...)`（迭代顺序按 relations 数组顺序）。
- 枚举值（供 TS 对齐字符串值）：`BELONGS_TO = "belongs_to"`，`CONTAINS = "contains"`（structure.py:34, 29）。

---

## `presets.py` — 内置范式基座

### 注册表（presets.py:54-57）

```python
PRESETS = {
    "ssm":        _ssm_preset,    # Spring Boot + MyBatis 标准 SSM
    "three_tier": _ssm_preset,    # 通用三层，与 ssm 共享同一工厂
}
```

> 源码无 `: dict[str, Callable[[], LayeringConfig]]` 显式注解（spec 原文加了类型注解，实际是裸 dict 字面量）。两 adapter_id 映射**同一函数对象**，行为完全相同。

### SSM preset（`_ssm_preset()`，presets.py:10-49）

`LayeringConfig(enabled=True, adapter="ssm", fallback_layer="unknown", layers=[...])`

#### 层定义（按 layers 列表顺序 = 优先级顺序）

**层 1：`entry`（入口层）**

| 信号 | 值 |
|---|---|
| `annotation` | `["Controller", "RestController"]` |
| `name_suffix` | `["Controller", "Resource", "Api"]` |
| `has_attr` | `["path"]` |

**层 2：`business`（业务逻辑层）**

| 信号 | 值 |
|---|---|
| `annotation` | `["Service"]` |
| `name_suffix` | `["ServiceImpl", "Service", "Manager"]`（`ServiceImpl` 在 `Service` 前） |
| `package_contains` | `[".service"]` |

**层 3：`dao`（数据访问层）**

| 信号 | 值 |
|---|---|
| `language` | `["xml"]` |
| `name_suffix` | `["Mapper", "Dao", "Repository"]` |
| `package_contains` | `[".dao", ".mapper", ".repository"]` |
| `annotation` | `["Mapper", "Repository"]` |

**fallback：`unknown`**

> 同层内信号字段在源码 `LayerMatch(...)` 构造里的书写顺序（如 dao 层 language→name_suffix→package_contains→annotation）**不影响结果**（`_match` 内部固定按 name_suffix→name_prefix→language→annotation→has_attr→package_contains 顺序求值，全 OR）。TS 复现时只需保证各信号值集合一致即可。

---

## `registry.py` — 注册表与生效配置解析

### `AdapterRegistry`（registry.py:12）

```python
class AdapterRegistry:
    def __init__(self) -> None          # self._presets = dict(PRESETS)  # 浅拷贝
    def resolve(self, config: LayeringConfig) -> LayeringConfig
```

### `resolve` 优先级规则（registry.py:25-58）

```
if config.layers:                       # 用户已写 layers（列表非空）→ 工程覆盖，原样返回原对象
    return config
factory = self._presets.get(config.adapter)
if factory is None:                     # 未知 adapter 且无 layers → 原样返回（不抛错），layers 仍空 → 全归 fallback
    return config
preset = factory()                      # 调用工厂生成基座
return config.model_copy(update={"layers": preset.layers})
#   Pydantic v2 model_copy：保留 config 的 enabled/adapter/fallback_layer/profile_on_missing
#   只把 layers 替换为 preset.layers（adapter 仍是 config 原值，可能与 preset 的 "ssm" 不同）
```

**关键怪癖：**
- `config.layers` 真值判断 = 非空列表（空列表 falsy）。
- `model_copy` 是 Pydantic v2 API，不直接改 `config.layers`。返回值身份：工程覆盖路径与未知 adapter 路径返回**原入参对象**；preset 路径返回**新对象**。
- preset 路径下 `update={"layers": preset.layers}` 把 preset 的 layers 列表对象**直接引用**进新 config（非深拷贝）。TS 视为只读即可。
- **adapter 字段不被 preset 改写**：若 config.adapter="three_tier" 走 preset 路径，effective.adapter 仍是 `"three_tier"`（而 `_ssm_preset()` 内部 adapter="ssm" 的那个值被丢弃，只取它的 layers）。这影响 apply.py 日志里打印的 adapter 值。

---

## 关键常量与魔法数字

| 值 | 位置 | 语义 |
|---|---|---|
| `"unknown"` | `LayeringConfig.fallback_layer` 默认 / SSM preset | 兜底层 id |
| `"three_tier"` | `LayeringConfig.adapter` 默认 | 默认范式基座 |
| `"ssm"` | `_ssm_preset` 内 adapter / PRESETS 键 | SSM 基座 id |
| `"entry"` / `"business"` / `"dao"` | SSM preset layer id | 三层标准 id |
| `"入口层"` / `"业务逻辑层"` / `"数据访问层"` | SSM preset layer name | 三层中文展示名 |
| `"layer"` | attributes 键名 | 写入 layer_id |
| `"layer_name"` | attributes 键名 | 写入层展示名 |
| `"annotations"` | attributes 键名 | Java 端写入的注解列表（annotation 信号来源） |
| `"path"` | entry 层 has_attr 键名 | 路由注解存在性信号 |
| `"xml"` | dao 层 language 值 | MyBatis XML 直归 dao（大小写不敏感比较） |
| `_CLASSLIKE` | apply.py:32 | `{CLASS, INTERFACE, ENUM, ANNOTATION_TYPE}` |
| `"[layering] adapter=%s 打标签实体数=%d"` | apply.py:133 | INFO 日志格式串（逐字） |
| logger 名 `"src.structure.layering.apply"` | apply.py:28（`__name__`） | 日志通道名 |
| `EntityType` 字符串值 | structure.py:10-24 | class=`"class"`, interface=`"interface"`, enum=`"enum"`, annotation_type=`"annotation_type"`, method=`"method"` |
| `RelationType` 字符串值 | structure.py:27-40 | belongs_to=`"belongs_to"`, contains=`"contains"` |

---

## 外部依赖

- `src.config.models`：`LayerMatch` / `LayerSpec` / `LayeringConfig`
- `src.models.structure`：`StructureEntity` / `StructureRelation` / `StructureFacts` / `EntityType` / `RelationType`
- Python 标准库：`logging`
- `typing`：`Protocol`（adapter）、`Optional`（apply）
- Pydantic v2（`BaseModel` / `Field` / `model_copy`）
- `from __future__ import annotations`（adapter.py / apply.py / registry.py 均有；presets.py 无）

---

## 怪癖与 TS 实现注意事项

1. **attributes 原地修改**：layer/layer_name 写入 `entity.attributes`，无独立字段。TS 操作同一 attributes map（StructureEntity.attributes，`dict[str, Any]`）。

2. **method 继承条件是 `== effective.fallback_layer`，非硬编码 `"unknown"`**：fallback_layer 可被配置覆盖，TS 必须用变量比较。

3. **`name_by_id.get(layer, layer)` 兜底**：fallback_layer 通常不在 layers 中，`layer_name` = `layer_id`。

4. **`_entity_package_path` 的 `"."` 前缀必须复现**：`split(":")[0]` 取首段、`replace("\\","/")`、`split("/")`、`".".join`、再前缀 `"."`；None → `"."`。`".service"` 不命中 `"aservice"` 是设计决策。

5. **BELONGS_TO 优先于 CONTAINS**：`_method_owner_index` 第二轮 `setdefault`，TS 用 `if (!owner.has(k))`。第一轮 BELONGS_TO 用直接赋值（同 method 多条时后者覆盖）。

6. **layers 列表顺序决定优先级（first-match）**；同层内信号顺序不影响结果（全 OR，且 `_match` 内部信号求值顺序固定，与 LayerMatch 字段书写顺序无关）。

7. **`resolve` 返回对象身份**：工程覆盖/未知 adapter 路径返回原入参；preset 路径返回 `model_copy` 新对象。两路径均按只读使用。effective.adapter 在 preset 路径下保留 config 原值（不会变成 preset 的 "ssm"）。

8. **语言匹配大小写不敏感**：`(e.language or "").toLowerCase()` vs `m.language.map(x => x.toLowerCase())`，且需先判 `m.language` 非空。

9. **`_match` 六类信号（非五类）**：源码 docstring 误称五类，实际 6 个 if 块。TS 实现勿漏 `package_contains`。

10. **只处理 class-like + METHOD 两类实体**：其余 EntityType（FILE/MODULE/PACKAGE/FIELD/PARAMETER/SERVICE/API_ENDPOINT）不打标签、不计入 applied。

11. **entity_id 本模块零加工透传**：本模块不生成 entity_id，只读 `e.id` / `r.source_id` / `r.target_id` 作 dict key，无任何 trim/normalize/lower。TS 必须同样原值透传以保门禁③（entity_id 字节稳定性由 jar 与 mybatis_extractor.py 上游保证，不在本模块）。

12. **Pydantic 默认值用 `default_factory`**（list / LayerMatch / LayeringConfig）：避免可变默认值共享。TS 每实例独立新建。

---

相关源码绝对路径（均只读核验）：
- `/Users/java/knowledge-engineering/src/structure/layering/adapter.py`
- `/Users/java/knowledge-engineering/src/structure/layering/apply.py`
- `/Users/java/knowledge-engineering/src/structure/layering/presets.py`
- `/Users/java/knowledge-engineering/src/structure/layering/registry.py`
- `/Users/java/knowledge-engineering/src/structure/layering/__init__.py`
- `/Users/java/knowledge-engineering/src/config/models.py`（LayerMatch:33 / LayerSpec:67 / LayeringConfig:88 / StructureConfig:113）
- `/Users/java/knowledge-engineering/src/models/structure.py`（EntityType:10 / RelationType:27 / StructureEntity:43 / StructureRelation:54 / StructureFacts:62）