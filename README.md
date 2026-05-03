# 代码知识工程（knowledge-engineering）

面向「代码仓库 → 结构事实 → 语义增强 → 知识图谱 →（可选）解读 → 可检索/可解释 UI」的端到端工程。

工程以 **流水线（pipeline）** 为核心，并通过 **Streamlit（UI）** 与 **FastAPI（API）** 两种入口消费同一套产物：知识图谱（memory/Neo4j）与向量/解读库（memory/Weaviate）。

> 详细架构、数据模型、各子系统设计：见内部设计文档。本 README 只覆盖安装、启动与配置等运维信息。

---

## 主要能力

1. **结构抽取**：解析 Java AST 抽取实体/关系，产出 `StructureFacts`
2. **语义增强**：术语匹配 + 能力 path_pattern + embed_text 拼接 + 向量化
3. **知识图谱构建**：内存图（NetworkX `MultiDiGraph`）或同步到 Neo4j；可选写入代码向量库
4. **解读（可选）**：4 种模式
   - 方法级技术解读（向量召回 / 预解读碎片库）
   - 链路级业务解读（实时调用链展开）
   - 业务三层综述（类 / API / 模块）
   - 拓扑解读（按调用拓扑序批量）
5. **OWL 推理（可选）**：本体推理 + 推断边写回图谱
6. **检索 / 可视化**：Streamlit 步骤化探索 + FastAPI 检索/影响分析接口

---

## 安装

```bash
pip install -e .

# 可选依赖（按需）：
pip install -e ".[neo4j]"          # Neo4j 持久化
pip install -e ".[vector]"         # 向量检索扩展
pip install -e ".[owl]"            # OWL 本体推理（rdflib）
pip install -e ".[llm-openai]"     # OpenAI 解读
pip install -e ".[llm-anthropic]"  # Anthropic 解读
pip install -e ".[llm]"            # 同时装 OpenAI + Anthropic
```

本地默认可先以 `memory` 图后端 + `weaviate` 向量后端 + `ollama` embedding/LLM 的组合跑通主链路。

---

## 配置

编辑 `config/project.yaml`，关键段：

| 段 | 用途 |
|---|---|
| `repo` | 目标代码库路径、语言、模块列表 |
| `domain` | 业务域、能力 `path_pattern`、术语词表与同义词 |
| `structure` | 跨服务结构抽取开关（`extract_cross_service`） |
| `schema` | DDL 路径、Mapper glob（用于方法-表访问索引） |
| `knowledge` | 图后端、4 个 Weaviate collection、4 种解读各自的 LLM 与续跑策略、OWL 推理开关 |

详见配置文件中的注释。

---

## 启动 Streamlit UI（推荐）

```bash
python main.py
```

浏览器访问 <http://localhost:8501>。在侧栏选择配置文件 →「运行流水线」构建知识图谱 → 使用检索、影响分析、解读专区、场景模板。

---

## 启动 FastAPI

```bash
uvicorn src.service.api:app --reload --host 0.0.0.0 --port 8000
```

API 文档：<http://localhost:8000/docs>

---

## 命令行流水线

```bash
# 全量构建
python -m src.pipeline.cli --config config/project.yaml

# 仅跑到结构层
python -m src.pipeline.cli --config config/project.yaml --until structure --output-dir out
```

主要参数：

| 参数 | 说明 |
|---|---|
| `--config, -c` | 配置文件路径（默认 `config/project.yaml`） |
| `--until` | 执行到 `structure \| semantic \| knowledge` 后停止（默认到 knowledge） |
| `--output-dir, -o` | 中间产物输出目录（如 `semantic_facts.json`、图快照） |
| `--with-interpretation / --without-interpretation` | 是否清空并重建技术解读 |
| `--with-business-interpretation / --without-business-interpretation` | 是否执行业务解读 |

> 解读部分依赖 LLM 与 Weaviate，会显著增加耗时；首次构建可先 `--without-interpretation` 跑通主链路。

---

## 项目结构

```
knowledge-engineering/
├── config/                # 项目配置 + 领域词表
├── javaparser-bridge/     # Java AST 抽取子工程（独立 Maven 项目）
├── src/
│   ├── data_trigger/      # 数据触发与代码源加载
│   ├── structure/         # AST 解析、StructureFacts、稳定 entity_id
│   ├── semantic/          # 术语匹配、能力归属、embedding
│   ├── knowledge/         # 图谱、向量库、4 种解读、模式识别、OWL、LLM 抽象
│   ├── pipeline/          # 流水线编排、CLI、配置加载
│   ├── service/           # FastAPI
│   ├── app/               # Streamlit UI
│   ├── persistence/       # 缓存与快照
│   ├── ir/                # 中间表示（探索性，未接入主流水线）
│   ├── core/              # 横切：路径、枚举、默认值
│   ├── plugins/           # DAO SQL 插件（MyBatis 等）
│   └── models/            # 共享数据模型
├── pyproject.toml
└── README.md
```

默认输出目录见 `src/core/paths.py`（`out_ui/structure_facts_for_interpret.json` / `out_ui/interpretation_progress.json` / `out_ui/knowledge_snapshot/graph.json`）。

---

## 适用代码库

默认目标为 mall-swarm（Java Spring Cloud 微服务示例）。换用其他代码库：修改 `config/project.yaml` 中的 `repo.path / repo.modules / domain` 即可；若为单体或多模块非微服务，可不配置跨服务调用相关规则。

---

## 查看 Weaviate 数据

Weaviate 无官方桌面 UI，可用：

- **REST / GraphQL**：`GET http://localhost:8080/v1/objects`、`/v1/schema`（启用 API Key 时加 `Authorization: Bearer <key>`）
- **第三方 UI**：[weaviate-browser](https://github.com/gagin/weaviate-browser)、[weaviate-ui](https://github.com/naaive/weaviate-ui)
