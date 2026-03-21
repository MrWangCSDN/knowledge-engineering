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
