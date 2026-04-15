# 代码知识工程（knowledge-engineering）

一个面向“代码仓库 → 结构事实 → 语义增强 → 知识图谱 →（可选）解读与推理 → 可检索/可解释 UI”的端到端工程。

工程以 **流水线（pipeline）** 为核心，并通过 **Streamlit（UI）** 与 **FastAPI（API）** 两种入口消费同一套产物：知识图谱（memory/Neo4j）与向量/解读库（memory/Weaviate）。

---

## 主要能力概览

1. **结构抽取（Structure）**
   - 从代码仓库解析 AST，抽取文件/包/类/接口/方法等实体与它们的关系，形成稳定的结构事实（`StructureFacts`）。
2. **语义增强（Semantic）**
   - 基于 `project.yaml` 的领域词表与能力 path_pattern，对结构实体做术语匹配与业务关联，形成 `SemanticFacts`。
3. **知识图谱构建（Knowledge）**
   - 将结构事实 + 语义增强事实构建为内存图（NetworkX `MultiDiGraph`），并可选同步到 **Neo4j**。
   - 可选把“带方法源码片段”的方法向量写入向量库（memory 或 Weaviate）。
4. **解读（Interpretation，可选）**
   - **技术解读（method_interpretation）**：为方法写入 LLM 解读文本，并入 Weaviate 的 `vectordb-interpret` 集合。
   - **业务解读（business_interpretation）**：为类/API/模块写入三层业务综述，并入 Weaviate 的 `vectordb-business` 集合。
   - 两类解读均支持增量续跑（跳过已存在的键）。
5. **OWL 推理（Ontology，可选）**
   - 基于图谱执行本体推理，可选将推断边写回图谱。
6. **可检索/可解释消费**
   - Streamlit 提供“步骤化探索 UI”（构建、统计、检索、影响分析、解读专区、场景可视化）。
   - FastAPI 提供检索与影响分析 API，必要时走 Neo4j 查询直接调用关系。

---

## 模式 A / 模式 B 解读方案

### 1）两种模式总览

项目当前把“代码解读”拆成两种互补方式，而不是只依赖一种 LLM 调用模式：

- **模式 A：方法级代码解读（向量召回 / 预解读碎片库）**
  - 先离线为方法生成技术解读，再写入 Weaviate `MethodInterpretation`。
  - 适合“先找候选代码”“先快速看懂单个方法”“先做检索和缩圈”。
- **模式 B：实时链路解读（调用链展开 / 一次性深度解读）**
  - 从一个入口方法出发，沿 `calls` 实时展开调用链，拼接链路代码与 SQL，一次性交给 LLM 输出完整业务流程分析。
  - 适合“解释一条完整业务链路”“做深度分析”“看上下游影响和数据变更”。

这两种方式不是替代关系，而是典型的“**先 A 后 B**”组合：先用模式 A 做候选定位与快速浏览，再对关键入口使用模式 B 做链路级深度解读。

### 2）模式 A：方法级代码解读（向量召回 / 预解读碎片库）

**目标定位**

- 面向“方法”这一最小可消费单元生成技术解读。
- 把原始代码转换成更适合检索和阅读的“解读碎片”，支撑后续 UI 浏览、向量检索和候选归集。

**核心流程**

1. 结构层产出 `StructureFacts`，筛出有 `code_snippet` 的非简单 getter/setter 方法。
2. `src/knowledge/method_interpretation_runner.py` 为每个方法构造上下文：
   - 所属类 / 签名 / 模块
   - 直接上游调用者与下游被调方法摘要
3. 调用 LLM 生成两段式结果：
   - `[摘要]`：关键词密集的短摘要
   - `[详情]`：完整技术解读
4. 结果写入 Weaviate `MethodInterpretation` collection，作为可复用的“预解读碎片库”。
5. 上层 UI / 检索逻辑按 `method_entity_id` 回查解读文本、上下文摘要和关联实体。

**输入 / 产出**

- 输入：方法源码片段、方法签名、所属类信息、直接调用关系。
- 产出：方法级技术解读文本、摘要、上下文摘要，以及可被向量检索消费的结构化记录。

**依赖组件**

- 结构事实：`StructureFacts`
- LLM 解读生成：`run_method_interpretations(...)`
- 存储后端：Weaviate `MethodInterpretation`

**典型入口**

- Streamlit “解读给定的 method” 场景：先看源码，再看该方法已有技术解读。
- “业务问题找代码”“反向从代码看意图”等向量检索场景：先召回候选方法，再展开查看方法级解读。
- `WeaviateDataService.fetch_method_interpretation(...)` 一类按 `method_entity_id` 的读取链路。

**优点 / 约束**

- 优点：预计算、响应快、适合搜索 / 浏览 / 候选定位，可被多个 UI 场景复用。
- 约束：粒度是单方法；能解释局部职责，但默认不直接给出完整业务链路。

### 3）模式 B：实时链路解读（调用链展开 / 一次性深度解读）

**目标定位**

- 面向“入口方法对应的一整条业务调用链”生成一次性的深度解读。
- 把零散的方法级代码拼成“按执行路径组织”的业务流程说明。

**核心流程**

1. 以入口方法为起点，沿 `calls` 做 BFS 展开，可按 `down / up / both` 控制方向。
2. `src/knowledge/callchain_interpreter.py` 在展开过程中补充链路上下文：
   - 接口方法自动跳到实现类方法
   - 可选注入 DAO / MyBatis SQL
   - 跳过 getter/setter 等低价值噪音调用
3. 将整条链路中的方法代码、深度、类名、方法名等拼成一个大 prompt。
4. 一次性调用 LLM，输出面向业务场景的结构化分析报告。
5. 返回结果包括：链路节点列表、链路规模、总代码字符数、LLM 解读正文和耗时信息。

**输入 / 产出**

- 输入：入口方法 ID、图谱中的调用边、方法源码片段、可选 SQL 片段。
- 产出：一份链路级业务解读报告，覆盖业务场景、前置条件、主流程、业务规则、数据变更、异常场景、上下游影响与风险建议。

**依赖组件**

- 图谱 / 调用关系：`KnowledgeGraph` 中的 `calls`
- 链路解释引擎：`CallChainInterpreter`
- LLM 实时推理能力

**典型入口**

- 深度分析某个入口方法对应的完整业务流程。
- 需求覆盖率分析 demo：`run_requirement_analysis.py` 先用模式 A 召回候选方法，再选主入口并用模式 B 展开完整链路。

**优点 / 约束**

- 优点：上下文完整，适合链路说明、业务流程还原、复杂场景分析。
- 约束：实时计算，延迟更高、成本更高，更依赖图谱完整性与入口选择质量。

### 4）模式 A / 模式 B 对比

| 维度 | 模式 A：方法级代码解读 | 模式 B：实时链路解读 |
| --- | --- | --- |
| 解读粒度 | 单个方法 | 一条调用链 / 一个业务流程 |
| 输入 | 方法源码 + 方法上下文 | 入口方法 + 调用图 + 链路代码 + 可选 SQL |
| 产出 | 方法级技术解读、摘要、可检索碎片 | 链路级业务解读报告 |
| 延迟 | 预构建后读取快，适合毫秒级到秒级浏览 | 实时展开 + 实时调 LLM，通常更慢 |
| 成本 | 主要发生在离线构建阶段 | 主要发生在每次实时分析时 |
| 是否依赖预构建 | 依赖。需先写入 Weaviate 解读库 | 可不依赖已有解读库，但依赖图谱 / 结构事实 / 调用边 |
| 适合场景 | 搜索、浏览、候选定位、方法速读 | 链路说明、业务流程分析、需求覆盖率分析 |
| 当前接入状态 | 已接入 Streamlit 场景与 Weaviate 消费链路 | 解释引擎已实现，当前主要体现在核心代码 / 脚本能力，尚未像模式 A 那样完整接入 UI/API 主流程 |

### 5）当前项目中的落地状态

- **模式 A 已是主路径能力**
  - 已有完整的“生成 → 落库 → UI 消费”闭环。
  - 当前 Streamlit 的方法详情、向量检索、业务问题找代码等场景，都是围绕这套方法级解读库展开。
- **模式 B 已有能力内核，但仍偏引擎 / demo 形态**
  - `CallChainInterpreter` 已实现实时链路展开、接口到实现跳转、SQL 注入和结构化 prompt 生成。
  - `run_requirement_analysis.py` 已把模式 B 用在需求覆盖率分析 demo 中。
  - 但它目前还没有像模式 A 那样，全面接入现有 Streamlit / FastAPI 主流程，不能简单视为“已上线的统一主入口能力”。

### 6）推荐使用策略

- **先 A 后 B**
  - 第一步：用模式 A 从业务问题、关键词或方法列表中快速定位候选代码。
  - 第二步：看候选方法的技术解读、源码和邻居关系，确定哪个入口最值得深挖。
  - 第三步：对关键入口运行模式 B，得到完整链路级业务解读。
- **需求覆盖率分析**
  - 当前 demo 的标准套路就是：
    - 先用模式 A 做“向量召回 + 候选碎片归集”
    - 再用模式 B 做“主入口链路展开 + 完整分析”
- **日常推荐**
  - 日常排查、代码导览、候选定位：优先模式 A。
  - 业务流程讲解、需求核对、链路级问题分析：再切到模式 B。

---

## 运行方式（快速开始）

### 1）启动 Streamlit UI

项目入口脚本为仓库根的 `main.py`：

```bash
python main.py
```

该脚本会调用 `streamlit run src/app/streamlit_app.py`，并设置服务为 `http://localhost:8501`。

### 2）运行流水线（结构/语义/知识/解读）

命令行入口在 `src/pipeline/cli.py`（`run` 风格子命令目前简化为单入口脚本）。

示例：

```bash
python -m src.pipeline.cli --config config/project.yaml --until knowledge
```

主要参数：
- `--config, -c`：配置文件路径（默认 `config/project.yaml`）
- `--until`：执行到 `structure | semantic | knowledge` 后停止（默认执行到 knowledge）
- `--output-dir, -o`：中间结果输出目录（例如 `semantic_facts.json` 等）
- `--with-interpretation / --without-interpretation`：是否清空并重建技术解读
- `--with-business-interpretation / --without-business-interpretation`：是否执行业务解读

> 注意：解读部分通常依赖 LLM 与 Weaviate，可能会显著增加耗时。

---

## 配置说明（config/project.yaml）

配置文件是系统行为的单一来源，关键段包括：

- `repo`：仓库路径、语言、模块列表（决定输入范围与 module_id 推断）
- `domain`：
  - `business_domains`：业务域定义（并绑定 capability_ids）
  - `capabilities`：能力与 `path_pattern`（用于语义层业务关联）
  - `terms`：领域术语与同义词（用于语义层术语匹配）
  - `service_domain_mappings`：服务/模块与业务域的权重映射（用于图谱建模）
- `structure`：
  - `extract_cross_service`：是否做跨服务结构抽取
- `schema`：
  - `ddl_path` 与 `mapper_glob`：供方法-表访问模块加载 SQL/Mapper 模板索引
- `knowledge`：
  - `pipeline.include_method_interpretation_build` / `pipeline.include_business_interpretation_build`：解读是否在流水线中执行（增量续跑，不清空默认解读库）
  - `semantic_embedding`：语义向量模型（当前默认本地 Ollama）
  - `graph`：图后端（`memory | neo4j`）
  - `vectordb-code / vectordb-interpret / vectordb-business`：三个向量库集合的启用、后端与 Weaviate 连接参数
  - `method_interpretation / business_interpretation`：LLM 选择、timeout 与 max_* 的分批增量策略
  - `ontology`：OWL 推理开关与写回策略

---

## 整体架构设计

### 1）分层与依赖方向（读者视角）

- **`src/app/`（UI / presentation）**：Streamlit 页面、组件与步骤化渲染
- **`src/pipeline/`（orchestration）**：加载配置、构建段（stage）与执行顺序表
- **`src/structure/` & `src/semantic/`（计算层）**：结构抽取与语义增强
- **`src/knowledge/`（图谱与解读 domain logic）**：
  - 图谱构建/同步、向量库适配、技术/业务解读 runner、OWL 集成
- **`src/persistence/`（storage 抽象）**：结构事实缓存、知识快照等持久化接口
- **`src/service/`（FastAPI API）**：检索/影响分析/子图数据等对外接口
- **`src/core/`（横切能力）**：上下文、枚举、路径与默认值（单一来源）

设计要点：UI/API 不需要理解流水线内部实现细节，优先通过 `src/pipeline/gateways.py` 的窄接口加载配置与解读进度。

### 2）主数据流（pipeline）

当用户点击 UI 的“运行流水线”（或在命令行触发）时，系统执行：

1. `StructureStage`
   - `load_code_source` 构建输入源
   - `run_structure_layer` 解析 AST，产出 `StructureFacts`
2. `SemanticStage`
   - `run_semantic_layer` 依据 `domain` 做术语/能力匹配，产出 `SemanticFacts`
3. `KnowledgeStage`
   - `KnowledgeGraph.build_from` 构建内存图
   - 依据配置可选同步到 Neo4j，并可选写入向量库
4. `InterpretationStage`（可选）
   - `run_method_interpretations`（技术解读）
   - `run_business_interpretations`（业务解读）
5. `OntologyStage`（可选）
6. `FinalizeStage`
   - 统计图谱规模、生成返回消息与快照/缓存

这套顺序由 `src/pipeline/full_pipeline_orchestrator.py` 的“段表（table）”显式定义，保证系统行为稳定、可测试、可扩展。

---

## 模块划分（按目录）

### `src/app/`：Streamlit 前端与场景

- `streamlit_app.py`：入口（缓存 `AppServices`，注入默认 `project.yaml`，渲染 Sidebar/MainContent）
- `facades/`：侧边栏/主内容/影响分析等“页面编排器”
- `components/`：进度条、表格、解读专区等可复用组件
- `views/scene_template_room/`：场景模板（方法解读、调用关系展开、能力实现概览等）
- `services/`：Weaviate 拉取服务 `WeaviateDataService`

### `src/pipeline/`：流水线编排与入口

- `gateways.py`：窄接口（加载 `ProjectConfig`、查询解读进度）
- `run.py`：面向外部的 `run_pipeline` 入口（加载配置 + 构建 scope + 调用执行器）
- `full_pipeline_orchestrator.py`：段表编排（structure/semantic/knowledge/interpretation/ontology/finalize）
- `stage_runtime.py`：Stage 上下文与 Stage.execute 实现
- `config_bootstrap.py`：YAML -> 强类型领域模型（`ProjectConfig`、`DomainKnowledge` 等）
- `interpretation_standalone.py`：仅解读流程（基于已缓存结构事实）
- `cli.py`：命令行入口

### `src/structure/`：结构抽取（当前实现聚焦 Java）

- `runner.py`：`run_structure_layer`（输出 `StructureFacts`）
- `java_parser.py`：Java AST 抽取实现（javalang）
- 产物：
  - 实体（file/package/class/interface/method/…）
  - 关系（belongs_to/calls/…）
  - stable entity_id（canonical_v1）

### `src/semantic/`：语义增强

- `runner.py`：`run_semantic_layer`
- 根据领域配置进行：
  - 术语匹配（含驼峰拆分与同义词）
  - 能力路径匹配（path_pattern）
  - embed_text 拼接（供向量化）

### `src/knowledge/`：图谱构建、向量适配、解读与 OWL

- `graph.py`：`KnowledgeGraph`（内存图 + 向量后端统一视图）
- `graph_neo4j.py`：Neo4j 后端封装（影响闭包、calls/pred/succ 查询等）
- `vector_store.py` / `vector_store_weaviate.py`：向量检索与 entity_id -> code_snippet 回取
- `factories.py`：GraphBackendFactory / VectorStoreFactory（按配置 backend 字符串创建实例）
- `method_interpretation_runner.py` / `business_interpretation_runner.py`：技术/业务解读写入 Weaviate
- `method_table_access_service.py`：方法-表访问（用于工程化影响分析/SQL 映射）

### `src/service/`：FastAPI API

- `api.py`：
  - `/search`：名称或语义检索
  - `/impact`：影响分析闭包
  - `/calls/callees`、`/calls/callers`：直接调用关系（走 Neo4j）
  - 子图接口与知识附加能力（ontology run / load snapshot）

### `src/persistence/`：缓存与快照

- 结构事实缓存（用于仅解读或断点续跑）
- 图谱快照（用于 UI/命令行之间快速加载）

关键约定见 `src/core/paths.py`：
- 默认缓存文件：`out_ui/structure_facts_for_interpret.json`
- 解读进度汇总：`out_ui/interpretation_progress.json`
- UI 快照目录：`out_ui/knowledge_snapshot/`（图快照使用 `graph.json`）

---

## 环境依赖（可选能力）

在 `pyproject.toml` 中已经按能力分组了可选依赖：
- `neo4j`：Neo4j 驱动
- `vector`：如果扩展本地向量相关实现
- `owl`：OWL/推理依赖（rdflib）
- `llm-openai` / `llm-anthropic` / `llm`：云端 LLM provider

本地默认可先以 `memory` 图后端 + `weaviate` 向量后端 + `ollama` embedding/LLM 的组合跑通主链路。

---

## 产物与目录（你关心的“文件会落在哪里”）

默认路径约定（由 `src/core/paths.py` 统一管理）：
- `out_ui/structure_facts_for_interpret.json`：完整流水线写入/仅解读读取的结构事实缓存
- `out_ui/interpretation_progress.json`：UI 展示用解读进度汇总
- `out_ui/knowledge_snapshot/graph.json`：图谱快照（用于加载/替换当前图）

当你用命令行提供 `--output-dir` 时，流水线会把中间结果（例如 `semantic_facts.json`）与快照写入指定目录下的对应子目录。

---

## 接下来怎么扩展

- 新增图后端：实现 GraphBackendProtocol 并在 `knowledge/factories.py` 注册
- 新增向量后端：实现 VectorStoreProtocol 并在 `knowledge/factories.py` 注册
- 新增 pipeline stage：在 `full_pipeline_orchestrator.py` 的段表中插入一个新的段函数，并补齐 stage_runtime 中的上下文与 Stage.execute
- 新增解读策略：复用 `BaseInterpretationRunner + prompt + store adapter` 的组织方式，确保断点续跑键稳定

# 代码解读知识工程 (Knowledge Engineering)

基于《代码掘金：用AI打造企业级代码知识工程》整体架构设计的 Python 实现，实现「数据与触发层 → 结构层 → 语义层 → 知识层 → 服务层」五层流水线。

## 架构对应

| 层级           | 包名           | 职责概要                         |
|----------------|----------------|----------------------------------|
| 数据与触发层   | `data_trigger` | 代码库接入、全量/增量/按需触发   |
| 结构层         | `structure`    | AST 解析、结构抽取、统一结构表示 |
| 语义层         | `semantic`     | 领域知识库、术语识别、意图关联   |
| 知识层         | `knowledge`    | 代码/业务本体、图存储、版本快照  |
| 服务层         | `service`      | 检索、问答、影响分析、REST API   |

## 安装

```bash
cd knowledge-engineering
pip install -e ".[neo4j]"   # 可选: 使用 Neo4j 持久化
pip install -e ".[vector]"  # 可选: 向量检索
pip install -e ".[owl]"     # 可选: OWL 本体导出与推理机（传递闭包等）
pip install -e ".[llm]"     # 可选: 技术/业务解读使用 OpenAI 或 Anthropic（见下）
# 或仅其一: pip install -e ".[llm-openai]" / pip install -e ".[llm-anthropic]"
```

## 配置

编辑 `config/project.yaml`：

- `repo.path`: 目标代码库本地路径（或 Git 克隆路径）
- `repo.modules`: 模块/服务列表（如 Maven 子模块名）
- `domain`: 领域词表与「服务—业务域」映射
- `knowledge.ontology`: 可选 OWL 推理——`enabled: true` 时在构建后导出 OWL、运行内置传递闭包推理并将推断边写回图
- `knowledge.vectordb-code`: 源代码向量库。`backend: weaviate` 且 `enabled: true` 时，流水线会把**每个方法的代码片段**写入 Weaviate，`entity_id` 与知识图谱中的方法节点一一对应，便于按代码语义检索并关联回图谱。Weaviate 连接信息（含 API Key）见 `config/project.yaml` 与你的 `docker-compose.yaml`。
- **`knowledge.pipeline.include_method_interpretation_build`**：`false`（默认）时每次流水线**只**重建图谱 + 代码向量，**不清空、不重算**技术解读库（适合日常迭代）。`true` 或与 Streamlit 勾选「包含技术解读」、命令行 `--with-interpretation` 时，会清空解读库并调用 LLM 全量重建（极慢）。
- **稳定实体 ID（canonical_v1）**：`file://` + 仓库相对路径；`class//`、`method//` 为对「路径 + 类型名 + 签名」的确定性 SHA256 短哈希。同一方法路径与签名不变则 `method_id` 不变，解读库可与多次「仅图谱+代码」构建对齐。**文件移动或方法改签会换新 ID**，旧解读可能残留，需再跑一轮带解读的构建或后续做增量清理。
- **`knowledge.method_interpretation` + `knowledge.vectordb-interpret`**：调用 LLM 生成技术解读；`language: zh` / `en`。解读写入独立 Weaviate collection。CLI：`python -m src.pipeline.cli --with-interpretation` / `--without-interpretation` 覆盖配置。
  - **`llm_backend`**：`ollama`（默认，本地）、`openai`（官方或兼容 API）、`anthropic`。OpenAI 需安装 `.[llm-openai]`，配置 `openai_api_key` 或环境变量 `OPENAI_API_KEY`；可选 `openai_base_url`（转发网关、Azure 等）。Anthropic 需 `.[llm-anthropic]` 与 `anthropic_api_key` / `ANTHROPIC_API_KEY`。
  - **`llm_allow_fallback_to_ollama`**：默认 `false`。为 `true` 时，若选了 `openai`/`anthropic` 但未安装对应 Python 包，会回退到本地 Ollama；为 `false` 则直接报错（fail-fast）。
- **`knowledge.business_interpretation`**：同上，独立 `llm_backend` 与各提供商字段。
- **`knowledge.vectordb-code` 等向量库**：**`allow_fallback_to_memory`** 默认 `false`。Weaviate 创建失败时是否回退内存向量库；生产环境建议保持 `false`，避免误以为数据已进 Weaviate。

## 启动 Web 应用（推荐）

```bash
python main.py
```

浏览器访问 http://localhost:8501。在侧栏选择配置文件并点击「运行流水线」构建知识图谱后，可使用检索、影响分析、图谱子图、统计等能力。

## 运行流水线（命令行）

```bash
# 全量构建（从代码库到知识图谱）
python -m src --config config/project.yaml

# 仅结构层（输出结构事实到 JSON）
python -m src --config config/project.yaml --until structure --output-dir out
```

## 启动服务层 API（FastAPI）

```bash
uvicorn src.service.api:app --reload --host 0.0.0.0 --port 8000
```

API 文档: http://localhost:8000/docs

- **OWL 推理**：安装 `.[owl]` 后，配置 `knowledge.ontology.enabled: true` 可在流水线结束后自动执行；或调用 `POST /knowledge/ontology/run` 按需执行导出与推理。

## 项目结构

```
knowledge-engineering/
├── config/           # 项目配置与领域词表
├── src/
│   ├── models/       # 共享数据模型（CodeInputSource, StructureFacts 等）
│   ├── data_trigger/ # 数据与触发层
│   ├── structure/    # 结构层（AST、Java 解析器）
│   ├── semantic/     # 语义层（领域知识库、术语识别）
│   ├── knowledge/    # 知识层（图存储、本体映射）
│   ├── service/      # 服务层（FastAPI、检索、影响分析）
│   └── pipeline/     # 流水线编排与 CLI
├── pyproject.toml
└── README.md
```

## 查看 Weaviate 向量库内容

Weaviate 无官方桌面 UI，可用以下方式查看/浏览数据：

- **REST/GraphQL**：`GET http://localhost:8080/v1/objects`、`/v1/schema` 等（若启用 API Key，需在请求头加 `Authorization: Bearer <key>`）。
- **开源社区 UI**：[weaviate-browser](https://github.com/gagin/weaviate-browser)（Flask 小工具，可列集合、按属性筛选）、[weaviate-ui](https://github.com/naaive/weaviate-ui) 等，连接同一 Weaviate 地址即可。

## 适用代码库

架构为通用设计，默认示例为 mall-swarm（Java 微服务）。换用其他代码库时，仅需修改配置中的仓库路径、模块列表与领域词表；若为单体或多模块非微服务，可不配置跨服务调用相关规则。
