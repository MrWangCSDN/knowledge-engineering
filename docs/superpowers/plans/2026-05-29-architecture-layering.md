# 架构分层采集 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给结构层增加「架构角色分层」维度——每个 class/method 实体获得 `layer` 标签（入口/业务/Dao 等），规则按工程范式可配、可覆盖。

**Architecture:** 三段式「画像 → 适配 → 采集」中的**适配段**先落地。`LayeringConfig`（project.yaml）声明范式与规则；`RuleBasedAdapter` 按 match 规则把单个实体判定到某层；`AdapterRegistry` 提供范式基座（SSM 等）并合并工程覆盖；`apply_layering` 在结构层抽取完成后给实体打标签（method 缺省继承所属类）。Java 子工程补抽 applied 注解名作为匹配信号。

**Tech Stack:** Python 3 + Pydantic v2（config/模型）、pytest（测试）、JavaParser（Java 子工程，Maven）。

> **本设计拆成 3 个独立计划**（每个都能独立产出可工作软件）：
> - **Plan 1（本文件）— 分层骨架**：数据模型 + 配置 + 适配器 + 注册表 + 打标签 + Java 注解持久化 + 流水线接线。产出：mall-swarm 全实体带 `layer`。
> - **Plan 2 — 按层增强采集**：`LayerExtractor` + `ExtractorRegistry` + entry_rest/business_orchestration/dao_table 三个抽取器 + 新关系 `EXPOSES_ROUTE`/`ACCESSES_TABLE` + ctx-backed 信号（xml_paired/extends/implements）。依赖 Plan 1。
> - **Plan 3 — 工程画像器 ProjectProfiler**：扫指纹自动产出「建议分层映射」草稿供人确认。依赖 Plan 1 的配置 schema。
>
> Plan 2 / Plan 3 的完整任务分解待 Plan 1 落地后再写（它们依赖 Plan 1 真实接口）。本文件末尾给出范围与 seam 约定。

**设计 spec（单一来源）：** `/Users/java/obsidian/01 Engineering/knowledge-engineering/架构分层采集-设计.md`

**用户偏好：** 所有 Python 代码必须含**中文逐行注释**（用户为 Python 学习者）；下方代码块已带注释，实现时**保留并按需补充**，勿删。

---

## File Structure（Plan 1）

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/config/models.py` | 新增 `LayerMatch / LayerSpec / LayeringConfig`，`StructureConfig` 加 `layering` 字段 | Modify |
| `src/structure/layering/__init__.py` | 子包导出 | Create |
| `src/structure/layering/adapter.py` | `LayerAdapter` 协议 + `RuleBasedAdapter`（单实体 classify） | Create |
| `src/structure/layering/presets.py` | 内置范式基座（SSM / three_tier 的默认 layers） | Create |
| `src/structure/layering/registry.py` | `AdapterRegistry`：基座 + 工程覆盖合并 | Create |
| `src/structure/layering/apply.py` | `apply_layering(facts, config)`：打标签 + method 继承 | Create |
| `src/structure/runner.py` | `run_structure_layer` 加 `layering` 参数，调用 `apply_layering` | Modify |
| `src/pipeline/stage_runtime.py` | `StructureStageContext` 加 `layering` 字段；`StructureStage.execute` 透传 | Modify |
| `src/pipeline/context_builders.py` | `_build_structure_ctx` 加 `layering` 参数并透传 | Modify |
| `src/pipeline/full_pipeline_orchestrator.py` | `_build_structure_ctx(...)` 调用处加 `layering=scope.struct_cfg.layering` | Modify |
| `javaparser-bridge/.../extract/JavaFileProcessor.java` | 持久化 applied 注解名 `attr("annotations", ...)` | Modify |
| `config/project.yaml` | 加 `structure.layering`（SSM） | Modify |
| `tests/test_structure/test_layering_*.py` | 单测（config/adapter/registry/apply/runner） | Create |

> 注：分层**配置模型**放 `src/config/models.py`（与 `StructureConfig` 同处、随其自动解析）；**运行时**逻辑放 `src/structure/layering/`。两侧无循环依赖（config 不 import structure）。

---

## Task 1: 配置模型 LayeringConfig + StructureConfig.layering

**Files:**
- Modify: `src/config/models.py`（在 `class StructureConfig`（约 L33）之前插入 3 个模型，并给 `StructureConfig` 加字段）
- Test: `tests/test_structure/test_layering_config.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_structure/test_layering_config.py
"""LayeringConfig 解析单测：验证 StructureConfig 能从 YAML dict 解析出分层配置。"""
# 从 config 模型导入待测类（实现后才存在，故此刻应 ImportError）
from src.config.models import StructureConfig


def test_layering_defaults_disabled():
    """默认：未配置 layering 时，enabled=False、adapter=three_tier、layers 为空。"""
    sc = StructureConfig()  # 不传任何字段，走默认
    assert sc.layering.enabled is False          # 默认关闭，向后兼容
    assert sc.layering.adapter == "three_tier"   # 默认范式基座
    assert sc.layering.fallback_layer == "unknown"
    assert sc.layering.layers == []              # 默认无层定义


def test_layering_parsed_from_yaml_dict():
    """model_validate 能把嵌套 dict 解析成强类型的 LayeringConfig/LayerSpec/LayerMatch。"""
    raw = {                                       # 模拟 project.yaml 的 structure 段
        "extract_cross_service": True,
        "layering": {
            "enabled": True,
            "adapter": "ssm",
            "fallback_layer": "unknown",
            "layers": [
                {
                    "id": "entry",
                    "name": "入口层",
                    "match": {"name_suffix": ["Controller"], "has_attr": ["path"]},
                }
            ],
        },
    }
    sc = StructureConfig.model_validate(raw)      # Pydantic v2 校验+构造
    assert sc.layering.enabled is True
    assert sc.layering.adapter == "ssm"
    assert sc.layering.layers[0].id == "entry"
    assert sc.layering.layers[0].match.name_suffix == ["Controller"]
    assert sc.layering.layers[0].match.has_attr == ["path"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_structure/test_layering_config.py -v`
Expected: FAIL — `ImportError` 或 `AttributeError: 'StructureConfig' object has no attribute 'layering'`

- [ ] **Step 3: 写最小实现**

在 `src/config/models.py` 中 `class StructureConfig` 之前插入：

```python
class LayerMatch(BaseModel):
    """单层匹配规则：各信号之间是 OR（任一命中即归该层）。"""
    annotation: list[str] = Field(default_factory=list)        # applied 注解简单名，如 "RestController"
    name_suffix: list[str] = Field(default_factory=list)       # 类/方法名后缀
    name_prefix: list[str] = Field(default_factory=list)       # 类/方法名前缀
    package_contains: list[str] = Field(default_factory=list)  # 包路径含子串，如 ".service"
    has_attr: list[str] = Field(default_factory=list)          # 存在且非空的 attribute 键，如 "path"
    language: list[str] = Field(default_factory=list)          # java / xml
    # 以下信号 Plan 2（ctx-backed）才实现，v1 classifier 暂忽略：
    xml_paired: bool = False                                   # 在 mapper/dao 包且有配对 XML
    extends: list[str] = Field(default_factory=list)           # 父类简单名
    implements: list[str] = Field(default_factory=list)        # 接口简单名


class LayerSpec(BaseModel):
    """一层的定义。"""
    id: str                                                    # 层 id，如 "entry"
    name: str                                                  # 层中文名
    match: LayerMatch = Field(default_factory=LayerMatch)      # 匹配规则
    extractor: Optional[str] = None                            # Plan 2：本层挂的增强 extractor id
    extractor_enabled: bool = True                             # Plan 2：是否启用该 extractor


class LayeringConfig(BaseModel):
    """架构分层采集配置（project.yaml: structure.layering）。"""
    enabled: bool = False                                      # 总开关，默认关，向后兼容
    adapter: str = "three_tier"                                # 范式基座 id
    fallback_layer: str = "unknown"                            # classify 全不命中归这层
    profile_on_missing: bool = False                           # Plan 3：无 layers 时是否自动画像
    layers: list[LayerSpec] = Field(default_factory=list)      # 层定义（工程覆盖）
```

并给 `StructureConfig` 加字段（`Optional` 已在文件顶部导入；`Field`/`BaseModel` 同）：

```python
class StructureConfig(BaseModel):
    """structure 配置。"""
    extract_cross_service: bool = True
    java_source_extensions: list[str] = Field(default_factory=lambda: [".java"])
    layering: LayeringConfig = Field(default_factory=LayeringConfig)  # 新增：架构分层采集配置
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_structure/test_layering_config.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/config/models.py tests/test_structure/test_layering_config.py
git commit -m "feat(structure): add LayeringConfig models + StructureConfig.layering"
```

---

## Task 2: RuleBasedAdapter.classify（单实体分层判定）

**Files:**
- Create: `src/structure/layering/__init__.py`
- Create: `src/structure/layering/adapter.py`
- Test: `tests/test_structure/test_layering_adapter.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_structure/test_layering_adapter.py
"""RuleBasedAdapter.classify 单测：验证各 match 信号 + 优先级 + fallback。"""
from src.config.models import LayeringConfig, LayerMatch, LayerSpec
from src.models.structure import EntityType, StructureEntity
from src.structure.layering.adapter import RuleBasedAdapter


def _cfg() -> LayeringConfig:
    """构造三层配置：entry 在前优先级最高，dao 用 name_suffix+language。"""
    return LayeringConfig(
        enabled=True, adapter="ssm", fallback_layer="unknown",
        layers=[
            LayerSpec(id="entry", name="入口层", match=LayerMatch(
                annotation=["Controller"], name_suffix=["Controller"], has_attr=["path"])),
            LayerSpec(id="business", name="业务层", match=LayerMatch(
                annotation=["Service"], name_suffix=["ServiceImpl", "Service"],
                package_contains=[".service"])),
            LayerSpec(id="dao", name="数据访问层", match=LayerMatch(
                language=["xml"], name_suffix=["Mapper", "Dao"])),
        ],
    )


def _cls(name, *, location="", language="java", attrs=None) -> StructureEntity:
    """构造一个 class 实体（最小字段）。"""
    return StructureEntity(id=f"class//{name}", type=EntityType.CLASS, name=name,
                           location=location, language=language, attributes=attrs or {})


def test_name_suffix_controller_is_entry():
    a = RuleBasedAdapter(_cfg())
    assert a.classify(_cls("PmsBrandController")) == "entry"


def test_annotation_service_is_business():
    a = RuleBasedAdapter(_cfg())
    # 名字不含 Service 后缀，但 annotations 含 Service → 命中 business
    e = _cls("OrderManager", attrs={"annotations": ["Service"]})
    assert a.classify(e) == "business"


def test_package_contains_service_is_business():
    a = RuleBasedAdapter(_cfg())
    e = _cls("Foo", location="mall/src/main/java/com/macro/mall/service/Foo.java:1")
    assert a.classify(e) == "business"


def test_language_xml_is_dao():
    a = RuleBasedAdapter(_cfg())
    e = _cls("getMenuList", language="xml")
    assert a.classify(e) == "dao"


def test_priority_first_layer_wins():
    """同时像 entry 和 business 时，layers 中靠前的 entry 优先。"""
    a = RuleBasedAdapter(_cfg())
    e = _cls("WeirdControllerService", attrs={"path": "/x"})  # 后缀 Service + 有 path
    assert a.classify(e) == "entry"


def test_fallback_when_no_match():
    a = RuleBasedAdapter(_cfg())
    assert a.classify(_cls("RandomUtil")) == "unknown"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_structure/test_layering_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.structure.layering'`

- [ ] **Step 3: 写最小实现**

```python
# src/structure/layering/__init__.py
"""架构分层采集子包：适配器 / 注册表 / 打标签。"""
```

```python
# src/structure/layering/adapter.py
"""分层适配器：按 LayeringConfig 的 match 规则把单个实体判定到某一层。"""
from __future__ import annotations

from typing import Optional, Protocol  # Protocol：结构化类型（鸭子类型的静态版），定义"接口"

from src.config.models import LayerMatch, LayeringConfig
from src.models.structure import StructureEntity


class LayerAdapter(Protocol):
    """适配器协议：给一个实体判定 layer_id。任何实现了 classify 的对象都算一个 adapter。"""
    def classify(self, entity: StructureEntity) -> str: ...


def _entity_package_path(entity: StructureEntity) -> str:
    """从 location（文件路径）推导包路径近似，用于 package_contains 子串匹配。

    location 形如 'mall-admin/src/main/java/com/macro/mall/service/Foo.java:20'，
    去掉 ':行号' 后把目录用 '.' 连接，便于用 '.service' 这种子串匹配。
    """
    loc = entity.location or ""              # location 可能为 None
    path = loc.split(":")[0]                 # 去掉结尾的 ':行号'
    # replace 统一斜杠；split('/') 切目录；'.'.join 拼成 '.a.b.c' 形式
    return "." + ".".join(path.replace("\\", "/").split("/"))


class RuleBasedAdapter:
    """纯规则驱动的适配器：按 config.layers 顺序，第一条 match 命中的层即归属。"""

    def __init__(self, config: LayeringConfig) -> None:
        self._config = config                # 持有生效配置（已被 registry 解析过）

    def classify(self, entity: StructureEntity) -> str:
        """返回实体所属 layer_id；全不命中返回 fallback_layer。"""
        for layer in self._config.layers:    # 按列表顺序遍历 → 靠前的层优先级更高
            if self._match(entity, layer.match):
                return layer.id
        return self._config.fallback_layer

    def _match(self, e: StructureEntity, m: LayerMatch) -> bool:
        """OR 语义：任一信号命中即算该层。v1 实现实体自带的信号子集。"""
        name = e.name or ""
        # 1) 名字后缀 / 前缀
        if any(name.endswith(s) for s in m.name_suffix):
            return True
        if any(name.startswith(p) for p in m.name_prefix):
            return True
        # 2) 语言（java/xml）
        if m.language and (e.language or "").lower() in [x.lower() for x in m.language]:
            return True
        # 3) applied 注解名（依赖 Java 端持久化的 attributes["annotations"]，缺省当空）
        if m.annotation:
            anns = e.attributes.get("annotations") or []
            if any(a in anns for a in m.annotation):
                return True
        # 4) 存在且非空的 attribute（如 path 表示 @*Mapping 入口）
        if m.has_attr and any(bool(e.attributes.get(k)) for k in m.has_attr):
            return True
        # 5) 包路径子串
        if m.package_contains:
            pkg = _entity_package_path(e)
            if any(sub in pkg for sub in m.package_contains):
                return True
        # v1 暂不实现 xml_paired / extends / implements（Plan 2 ctx-backed）
        return False
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_structure/test_layering_adapter.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/structure/layering/__init__.py src/structure/layering/adapter.py tests/test_structure/test_layering_adapter.py
git commit -m "feat(structure): RuleBasedAdapter classify with OR-match signals"
```

---

## Task 3: AdapterRegistry + SSM 范式基座

**Files:**
- Create: `src/structure/layering/presets.py`
- Create: `src/structure/layering/registry.py`
- Test: `tests/test_structure/test_layering_registry.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_structure/test_layering_registry.py
"""AdapterRegistry 单测：基座解析 + 工程覆盖优先 + 未知 adapter 容错。"""
from src.config.models import LayeringConfig, LayerMatch, LayerSpec
from src.structure.layering.registry import AdapterRegistry


def test_ssm_preset_used_when_no_user_layers():
    """adapter=ssm 且用户没写 layers → 借用 SSM 基座的 3 层。"""
    cfg = LayeringConfig(enabled=True, adapter="ssm")  # layers 为空
    eff = AdapterRegistry().resolve(cfg)
    ids = [l.id for l in eff.layers]
    assert ids == ["entry", "business", "dao"]
    assert eff.adapter == "ssm"          # 其它字段保留


def test_user_layers_override_preset():
    """用户写了 layers → 工程覆盖优先，忽略基座。"""
    cfg = LayeringConfig(enabled=True, adapter="ssm", layers=[
        LayerSpec(id="api", name="API", match=LayerMatch(name_suffix=["Endpoint"])),
    ])
    eff = AdapterRegistry().resolve(cfg)
    assert [l.id for l in eff.layers] == ["api"]


def test_unknown_adapter_no_layers_is_empty():
    """未知 adapter 且无覆盖 → 空 layers（不抛错，全部归 fallback）。"""
    cfg = LayeringConfig(enabled=True, adapter="nonexistent")
    eff = AdapterRegistry().resolve(cfg)
    assert eff.layers == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_structure/test_layering_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.structure.layering.registry'`

- [ ] **Step 3: 写最小实现**

```python
# src/structure/layering/presets.py
"""内置范式基座：每个 adapter_id 对应一套默认 LayeringConfig.layers。

新增范式 = 在此加一个 _xxx_preset() 并注册进 PRESETS（扩展点）。
"""
from src.config.models import LayeringConfig, LayerMatch, LayerSpec


def _ssm_preset() -> LayeringConfig:
    """SSM / Spring Boot + MyBatis 三层基座（mall-swarm 实测指纹驱动）。"""
    return LayeringConfig(
        enabled=True, adapter="ssm", fallback_layer="unknown",
        layers=[
            LayerSpec(id="entry", name="入口层", match=LayerMatch(
                annotation=["Controller", "RestController"],   # mall-swarm 主用 @Controller
                name_suffix=["Controller", "Resource", "Api"],
                has_attr=["path"])),                           # @*Mapping 的 path 属性
            LayerSpec(id="business", name="业务逻辑层", match=LayerMatch(
                annotation=["Service"],
                name_suffix=["ServiceImpl", "Service", "Manager"],  # Impl 在前，先匹配更具体的
                package_contains=[".service"])),
            LayerSpec(id="dao", name="数据访问层", match=LayerMatch(
                language=["xml"],                              # MyBatis XML statement 直接归 Dao
                name_suffix=["Mapper", "Dao", "Repository"],
                package_contains=[".dao", ".mapper", ".repository"],
                annotation=["Mapper", "Repository"])),         # 命中率低，作辅助
        ],
    )


# adapter_id → 生成基座配置的工厂函数。three_tier 与 ssm v1 用同一套默认规则。
PRESETS = {
    "ssm": _ssm_preset,
    "three_tier": _ssm_preset,
}
```

```python
# src/structure/layering/registry.py
"""适配器注册表：adapter_id → 范式基座；并把工程覆盖合并成生效配置。"""
from __future__ import annotations

from src.config.models import LayeringConfig
from src.structure.layering.presets import PRESETS


class AdapterRegistry:
    """范式基座注册表 + 生效配置解析。"""

    def __init__(self) -> None:
        self._presets = dict(PRESETS)        # 拷贝一份，避免外部改全局

    def resolve(self, config: LayeringConfig) -> LayeringConfig:
        """计算生效配置：
        - 用户写了 layers → 工程覆盖优先，原样返回；
        - 否则按 adapter_id 取基座 layers，套到用户的 enabled/fallback 上；
        - 未知 adapter 且无覆盖 → 原样返回（layers 为空 → 全部归 fallback，便于渐进）。
        """
        if config.layers:                    # 工程覆盖优先
            return config
        factory = self._presets.get(config.adapter)
        if factory is None:                  # 未知 adapter，不抛错
            return config
        preset = factory()
        # model_copy(update=...)：Pydantic v2 的不可变更新，仅替换 layers 字段
        return config.model_copy(update={"layers": preset.layers})
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_structure/test_layering_registry.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/structure/layering/presets.py src/structure/layering/registry.py tests/test_structure/test_layering_registry.py
git commit -m "feat(structure): AdapterRegistry + SSM preset (paradigm base + project override)"
```

---

## Task 4: apply_layering（打标签 + method 继承类）

**Files:**
- Create: `src/structure/layering/apply.py`
- Test: `tests/test_structure/test_layering_apply.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_structure/test_layering_apply.py
"""apply_layering 单测：class 打标签、method 缺省继承类、禁用时跳过。"""
from src.config.models import LayeringConfig
from src.models.structure import (
    EntityType, RelationType, StructureEntity, StructureFacts, StructureRelation,
)
from src.structure.layering.apply import apply_layering


def _facts() -> StructureFacts:
    """一个 Controller 类 + 它的一个方法（无分层信号，应继承类）+ 一个 Mapper XML。"""
    ctrl = StructureEntity(id="class//Ctrl", type=EntityType.CLASS, name="PmsBrandController",
                           language="java", attributes={"path": "/brand"})
    m = StructureEntity(id="method//getList", type=EntityType.METHOD, name="getList",
                        language="java", attributes={"class_name": "PmsBrandController"})
    xml = StructureEntity(id="method//xml-stmt", type=EntityType.METHOD, name="selectAll",
                          language="xml", attributes={})
    rels = [StructureRelation(type=RelationType.CONTAINS, source_id="class//Ctrl",
                              target_id="method//getList")]
    return StructureFacts(entities=[ctrl, m, xml], relations=rels)


def test_apply_tags_class_and_method_inherits():
    cfg = LayeringConfig(enabled=True, adapter="ssm")  # 用 SSM 基座
    stats = apply_layering(_facts() if False else _f(), cfg)  # 见下 _f 复用
    # 占位：实际断言在 test_full 中


def _f() -> StructureFacts:
    return _facts()


def test_full():
    """Controller→entry；其方法继承 entry；xml→dao。"""
    facts = _facts()
    cfg = LayeringConfig(enabled=True, adapter="ssm")
    stats = apply_layering(facts, cfg)
    by_id = {e.id: e for e in facts.entities}
    assert by_id["class//Ctrl"].attributes["layer"] == "entry"
    assert by_id["method//getList"].attributes["layer"] == "entry"   # 继承自类
    assert by_id["method//xml-stmt"].attributes["layer"] == "dao"    # language=xml
    assert stats["skipped"] is False
    assert stats["applied"] == 3


def test_disabled_skips():
    facts = _facts()
    stats = apply_layering(facts, LayeringConfig(enabled=False))
    assert stats["skipped"] is True
    assert "layer" not in facts.entities[0].attributes
```

> 实现 Step 3 后删掉占位的 `test_apply_tags_class_and_method_inherits` / `_f`（仅 `test_full` + `test_disabled_skips` 即可）。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_structure/test_layering_apply.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.structure.layering.apply'`

- [ ] **Step 3: 写最小实现**

```python
# src/structure/layering/apply.py
"""把架构分层标签写进 StructureFacts：classify 每个 class/method，method 缺省继承类。"""
from __future__ import annotations

import logging
from typing import Optional

from src.config.models import LayeringConfig
from src.models.structure import EntityType, RelationType, StructureFacts
from src.structure.layering.adapter import RuleBasedAdapter
from src.structure.layering.registry import AdapterRegistry

_LOG = logging.getLogger(__name__)

# class 一类的实体类型（都参与分层）
_CLASSLIKE = {
    EntityType.CLASS, EntityType.INTERFACE, EntityType.ENUM, EntityType.ANNOTATION_TYPE,
}


def apply_layering(facts: StructureFacts, config: Optional[LayeringConfig]) -> dict:
    """给 facts 中的 class/method 实体打 layer 标签。

    :returns: 统计 dict，如 {"applied": 12, "skipped": False}
    """
    # 总开关：未配置或关闭 → 直接跳过（向后兼容，老流水线零影响）
    if config is None or not config.enabled:
        return {"applied": 0, "skipped": True}

    effective = AdapterRegistry().resolve(config)        # 基座 + 工程覆盖
    adapter = RuleBasedAdapter(effective)
    name_by_id = {l.id: l.name for l in effective.layers}  # layer_id → 中文名

    # 1) 先分类 class-like 实体，建立 classId → layer 映射（供 method 继承）
    class_layer: dict[str, str] = {}
    applied = 0
    for e in facts.entities:
        if e.type in _CLASSLIKE:
            layer = adapter.classify(e)
            e.attributes["layer"] = layer
            e.attributes["layer_name"] = name_by_id.get(layer, layer)
            class_layer[e.id] = layer
            applied += 1

    # 2) method：先自身 classify；若落到 fallback，则继承所属类的层
    owner = _method_owner_index(facts)                   # methodId → classId
    for e in facts.entities:
        if e.type == EntityType.METHOD:
            layer = adapter.classify(e)
            if layer == effective.fallback_layer:        # 自身无信号 → 继承类
                cid = owner.get(e.id)
                if cid and cid in class_layer:
                    layer = class_layer[cid]
            e.attributes["layer"] = layer
            e.attributes["layer_name"] = name_by_id.get(layer, layer)
            applied += 1

    _LOG.info("[layering] adapter=%s 打标签实体数=%d", effective.adapter, applied)
    return {"applied": applied, "skipped": False}


def _method_owner_index(facts: StructureFacts) -> dict[str, str]:
    """methodId → classId：优先 BELONGS_TO(method→class)，回退 CONTAINS(class→method)。"""
    owner: dict[str, str] = {}
    for r in facts.relations:                            # 先用 BELONGS_TO（方法→类）
        if r.type == RelationType.BELONGS_TO:
            owner[r.source_id] = r.target_id
    for r in facts.relations:                            # 再用 CONTAINS（类→方法）补
        if r.type == RelationType.CONTAINS:
            owner.setdefault(r.target_id, r.source_id)   # setdefault：已有则不覆盖
    return owner
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_structure/test_layering_apply.py -v`
Expected: PASS（2 passed，删掉占位用例后）

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/structure/layering/apply.py tests/test_structure/test_layering_apply.py
git commit -m "feat(structure): apply_layering tags entities, method inherits class layer"
```

---

## Task 5: Java 子工程持久化 applied 注解名

**Files:**
- Modify: `javaparser-bridge/src/main/java/com/knowledgeeng/bridge/extract/JavaFileProcessor.java`
- Test: `javaparser-bridge/src/test/java/com/knowledgeeng/bridge/extract/AnnotationCollectTest.java`（Create）

> 跨语言：此任务为「JUnit 红绿 + 重新打包」。改完必须 `mvn package` 重出 shaded jar，否则 Python 端跑的还是旧 jar。

- [ ] **Step 1: 写失败 JUnit 测试**

```java
// javaparser-bridge/src/test/java/com/knowledgeeng/bridge/extract/AnnotationCollectTest.java
package com.knowledgeeng.bridge.extract;

import static org.junit.jupiter.api.Assertions.assertTrue;

import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import java.util.List;
import org.junit.jupiter.api.Test;

/** 验证 collectAnnotationNames 能取出类上 applied 的注解简单名。 */
class AnnotationCollectTest {
    @Test
    void collectsClassAnnotationSimpleNames() {
        // 解析一段带注解的类源码
        CompilationUnit cu = StaticJavaParser.parse(
            "@Controller @RequestMapping(\"/x\") class Foo {}");
        ClassOrInterfaceDeclaration cls =
            cu.findFirst(ClassOrInterfaceDeclaration.class).orElseThrow();
        // 待实现的静态包级方法
        List<String> names = JavaFileProcessor.collectAnnotationNames(cls);
        assertTrue(names.contains("Controller"));
        assertTrue(names.contains("RequestMapping"));
    }
}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth/javaparser-bridge && mvn -q -Dtest=AnnotationCollectTest test`
Expected: FAIL — 编译错误：`cannot find symbol: method collectAnnotationNames`

- [ ] **Step 3: 写最小实现**

在 `JavaFileProcessor.java` 顶部确保有 import（缺则补）：

```java
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.ast.expr.AnnotationExpr;
import java.util.ArrayList;
import java.util.List;
```

新增包级静态 helper（放在类内，靠近其它注解相关方法）：

```java
    /** 取出节点上 applied 注解的简单名列表（如 ["Controller","RequestMapping"]）。
     *  包级静态以便单测直接调用。 */
    static List<String> collectAnnotationNames(NodeWithAnnotations<?> node) {
        List<String> names = new ArrayList<>();
        for (AnnotationExpr ann : node.getAnnotations()) {
            names.add(ann.getNameAsString());   // 简单名，不含包前缀
        }
        return names;
    }
```

在**类实体**创建处（约 L259-264），`.attr("path", ...)` 之后追加一行：

```java
        StructureEntity entity = new StructureEntity(classId, entityType, name)
                .location(location)
                .moduleId(moduleId)
                .language("java")
                .attr("visibility", modifiers)
                .attr("path", mappingPath != null ? mappingPath : "")
                .attr("annotations", collectAnnotationNames(typeDecl));   // 新增：applied 注解名
```

在**方法实体**创建处（约 L398-405），`.attr("is_setter", isSetter)` 之后追加一行：

```java
        StructureEntity entity = new StructureEntity(methodId, EntityType.METHOD, method.getNameAsString())
                .location(location)
                .moduleId(moduleId)
                .language("java")
                .attr("signature", sig)
                .attr("class_name", className)
                .attr("is_getter", isGetter)
                .attr("is_setter", isSetter)
                .attr("annotations", collectAnnotationNames(method));     // 新增：applied 注解名
```

- [ ] **Step 4: 运行测试确认通过 + 重新打包**

```bash
cd /Users/java/knowledge-engineering-auth/javaparser-bridge
mvn -q -Dtest=AnnotationCollectTest test        # 期望：BUILD SUCCESS
mvn -q clean package -DskipTests                 # 重出 target/javaparser-bridge-1.0.0-shaded.jar
ls -la target/javaparser-bridge-1.0.0-shaded.jar # 确认 jar 时间戳已更新
```
Expected: 测试 BUILD SUCCESS；jar 重新生成。

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add javaparser-bridge/src/main/java/com/knowledgeeng/bridge/extract/JavaFileProcessor.java \
        javaparser-bridge/src/test/java/com/knowledgeeng/bridge/extract/AnnotationCollectTest.java
git commit -m "feat(bridge): persist applied annotation names on class/method entities"
```

---

## Task 6: 接线——layering 配置贯通流水线

**Files:**
- Modify: `src/structure/runner.py`（`run_structure_layer` 加 `layering` 参数 + 调 `apply_layering`）
- Modify: `src/pipeline/stage_runtime.py`（`StructureStageContext` 加字段；`StructureStage.execute` 透传）
- Modify: `src/pipeline/context_builders.py`（`_build_structure_ctx` 加参数 + 透传）
- Modify: `src/pipeline/full_pipeline_orchestrator.py`（调用处加 `layering=scope.struct_cfg.layering`）
- Test: `tests/test_structure/test_layering_runner.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_structure/test_layering_runner.py
"""run_structure_layer 透传 layering 单测：monkeypatch 掉 Java bridge，只验证打标签接线。"""
from src.config.models import LayeringConfig
from src.models import CodeInputSource
from src.models.structure import EntityType, StructureEntity, StructureFacts
import src.structure.runner as runner_mod


def test_run_structure_layer_applies_layering(monkeypatch, tmp_path):
    """伪造 javaparser bridge 返回一个 Controller 类，验证 run 后带 layer。"""
    fake_facts = StructureFacts(entities=[
        StructureEntity(id="class//C", type=EntityType.CLASS, name="FooController",
                        language="java", attributes={}),
    ])

    # 用假的 bridge 替换真实子进程调用（避免依赖 jar）
    def _fake_bridge(*, source, extract_cross_service, progress_callback):
        return fake_facts
    monkeypatch.setattr(runner_mod, "run_javaparser_bridge", _fake_bridge, raising=False)
    # 也可能是延迟 import；同时打掉 javaparser_bridge 模块内的符号
    import src.structure.javaparser_bridge as jb
    monkeypatch.setattr(jb, "run_javaparser_bridge", _fake_bridge, raising=False)

    # repo_path 指向空临时目录 → MyBatis XML 扫描自然返回 0，不报错
    source = CodeInputSource(language="java", repo_path=str(tmp_path))

    cfg = LayeringConfig(enabled=True, adapter="ssm")
    facts = runner_mod.run_structure_layer(source, layering=cfg)
    assert facts.entities[0].attributes.get("layer") == "entry"


def test_run_structure_layer_no_layering_is_noop(monkeypatch, tmp_path):
    """不传 layering → 实体无 layer，老行为不变。"""
    fake_facts = StructureFacts(entities=[
        StructureEntity(id="class//C", type=EntityType.CLASS, name="FooController",
                        language="java", attributes={}),
    ])
    def _fake_bridge(*, source, extract_cross_service, progress_callback):
        return fake_facts
    import src.structure.javaparser_bridge as jb
    monkeypatch.setattr(jb, "run_javaparser_bridge", _fake_bridge, raising=False)
    source = CodeInputSource(language="java", repo_path=str(tmp_path))
    facts = runner_mod.run_structure_layer(source)
    assert "layer" not in facts.entities[0].attributes
```

> 实现者注意：`CodeInputSource` 的字段名以实际 schema 为准（runner 用 `getattr(source, "repo_path", None) or getattr(source, "path", None)`）。若构造签名不同，按真实模型调整测试里的构造。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/java/knowledge-engineering-auth && python -m pytest tests/test_structure/test_layering_runner.py -v`
Expected: FAIL — `TypeError: run_structure_layer() got an unexpected keyword argument 'layering'`

- [ ] **Step 3: 写最小实现**

`src/structure/runner.py` —— 改签名 + 末尾调用：

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:                                   # 仅类型检查期 import，避免运行时循环依赖
    from src.config.models import LayeringConfig


def run_structure_layer(
    source: CodeInputSource,
    extract_cross_service: bool = True,
    progress_callback: Optional[Any] = None,
    layering: "LayeringConfig | None" = None,       # 新增：架构分层配置（None=跳过）
) -> StructureFacts:
```

在 `_link_java_to_mybatis_xml(facts)` 之后、最终 `return facts` 之前插入：

```python
    # ── 新增：架构分层打标签（设计 [[架构分层采集-设计]]）──────────────────
    from src.structure.layering.apply import apply_layering   # 延迟 import，避免顶层循环
    layer_stats = apply_layering(facts, layering)
    if not layer_stats.get("skipped"):
        _LOG.info("[structure] 架构分层：打标签 %d 个实体", layer_stats.get("applied", 0))
```

`src/pipeline/stage_runtime.py` —— `StructureStageContext` 加字段（在 `source: Any = None` 旁）：

```python
    layering: Any = None        # 新增：架构分层配置（LayeringConfig | None）
```

同文件 `StructureStage.execute` 中 `run_structure_layer(...)` 调用（约 L118）加一个 kwarg：

```python
        ctx.structure_facts = run_structure_layer(
            ctx.source,
            extract_cross_service=ctx.extract_cross_service,
            progress_callback=_wrap_struct_progress(ctx.progress_callback),
            layering=ctx.layering,                  # 新增：透传分层配置
        )
```

`src/pipeline/context_builders.py` —— `_build_structure_ctx` 加参数（在 `extract_cross_service` 旁）并透传：

```python
def _build_structure_ctx(
    *,
    repo_path: str,
    repo_version: Optional[str],
    modules: list[Any],
    repo_language: Optional[str],
    extract_cross_service: bool,
    layering: Any = None,                           # 新增
    interpret_enabled: bool,
    progress_callback: Optional[Any],
    step_callback: Callable[[str], None],
    structure_repo: StructureFactsRepository,
    config_path: str | Path,
    out_dir: Optional[Path],
) -> StructureStageContext:
    return StructureStageContext(
        repo_path=repo_path,
        repo_version=repo_version,
        modules=modules,
        repo_language=repo_language,
        extract_cross_service=extract_cross_service,
        layering=layering,                          # 新增
        interpret_enabled=interpret_enabled,
        progress_callback=progress_callback,
        step_callback=step_callback,
        structure_repo=structure_repo,
        config_path=config_path,
        out_dir=out_dir,
    )
```

`src/pipeline/full_pipeline_orchestrator.py` —— `_build_structure_ctx(...)` 调用处（约 L104-109）加一行：

```python
    structure_ctx = _build_structure_ctx(
        ...                                         # 其余参数保持原样
        extract_cross_service=scope.struct_cfg.extract_cross_service,
        layering=scope.struct_cfg.layering,         # 新增：从 StructureConfig 取分层配置
        ...
    )
```

- [ ] **Step 4: 运行测试确认通过 + 跑全量结构测试防回归**

```bash
cd /Users/java/knowledge-engineering-auth
python -m pytest tests/test_structure/ -v
```
Expected: 全部 PASS（含既有 mybatis 用例 + 新增 layering 用例）。

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/structure/runner.py src/pipeline/stage_runtime.py \
        src/pipeline/context_builders.py src/pipeline/full_pipeline_orchestrator.py \
        tests/test_structure/test_layering_runner.py
git commit -m "feat(pipeline): thread layering config through structure stage"
```

---

## Task 7: project.yaml 启用 SSM 分层 + mall-swarm 实测

**Files:**
- Modify: `config/project.yaml`（`structure` 段加 `layering`）

- [ ] **Step 1: 改配置**

在 `config/project.yaml` 的 `structure:` 段下追加（与 `extract_cross_service` 同级）：

```yaml
structure:
  extract_cross_service: true
  java_source_extensions: [".java"]
  # 架构分层采集（设计：架构分层采集-设计.md）。adapter=ssm 用内置基座，无需写 layers。
  layering:
    enabled: true
    adapter: ssm
    fallback_layer: unknown
```

- [ ] **Step 2: 跑结构层（仅 structure 段，避免触发解读/向量）**

```bash
cd /Users/java/knowledge-engineering-auth
python -m src.pipeline.cli --config config/project.yaml --until structure 2>&1 | tail -30
```
Expected: 结构层完成，日志含 `[structure] 架构分层：打标签 N 个实体`。

> 注意：`--until structure` 截断语义对 structure 段是生效的（见 orchestrator 段表）。若环境缺 jar，先 `cd javaparser-bridge && mvn clean package -DskipTests`。

- [ ] **Step 3: 校验分层覆盖率（读结构事实缓存）**

```bash
cd /Users/java/knowledge-engineering-auth
python - <<'PY'
# 读最近一次结构事实缓存，统计各 layer 的实体数与 unknown 占比
import json, glob, os, collections
# 结构事实缓存路径按 StructureFactsRepository 实际位置调整；先在 out / .cache 下找
cands = glob.glob("**/structure_facts*.json", recursive=True)
assert cands, "未找到 structure_facts 缓存，确认 Task6 的 structure_repo.save 路径"
path = max(cands, key=os.path.getmtime)
data = json.load(open(path, encoding="utf-8"))
ents = data.get("entities", [])
c = collections.Counter((e.get("attributes") or {}).get("layer", "<none>")
                        for e in ents if e.get("type") in ("class","interface","method"))
total = sum(c.values())
print("结构事实缓存:", path)
for k, v in c.most_common():
    print(f"  {k:10s} {v:6d}  {v*100/total:5.1f}%")
print("总计:", total)
PY
```
Expected：entry/business/dao 三层均有可观数量；`unknown` 占比偏高时按下面「人工校准」调规则。期望 mall-swarm（实测指纹：`*Controller`×50、`*Service/*ServiceImpl`×100+、`*Mapper`×76 + xml×76）下三层覆盖明显，`unknown` 主要落在 common/util/config/model。

- [ ] **Step 4: 人工校准（按需）**

若某层覆盖异常（如 entry 远少于 50），在 `config/project.yaml` 的 `structure.layering` 下显式写 `layers:`（覆盖基座），按实测调 `name_suffix/package_contains`，重跑 Step 2-3。记录最终 `layers` 到设计文档的「实施完成标记」。

- [ ] **Step 5: 提交**

```bash
cd /Users/java/knowledge-engineering-auth
git add config/project.yaml
git commit -m "chore(config): enable SSM architecture layering for mall-swarm"
```

---

## Self-Review（写完即查，已修内联）

**1. Spec coverage（对照设计文档）：**
- 「画像 Profiler」→ Plan 3（本计划不含，已在头部声明）✓
- 「适配 LayerAdapter（范式基座+工程覆盖）」→ Task 2（classify）+ Task 3（registry/preset）✓
- 「固化配置 LayeringConfig」→ Task 1 + Task 7 ✓
- 「采集 LayerExtractor + 新关系」→ Plan 2（本计划不含，已声明）✓
- 「数据模型：layer/layer_name/annotations」→ Task 4（layer/layer_name）+ Task 5（annotations）✓
- 「Java 端注解名持久化」→ Task 5 ✓
- 「接入 run_structure_layer」→ Task 6 ✓
- 「正交：layer ⊥ L0/L1」→ 仅写 attributes["layer"]，不碰拓扑层级 ✓

**2. Placeholder scan：** Task 4 测试里有占位用例，已显式标注 Step 3 后删除；无其它 TODO/TBD。✓

**3. Type consistency：**
- `LayeringConfig/LayerSpec/LayerMatch` 字段在 Task1 定义，Task2/3/4 使用一致 ✓
- `apply_layering(facts, config) -> dict`、`RuleBasedAdapter(config).classify(entity) -> str`、`AdapterRegistry().resolve(config) -> LayeringConfig` 全计划一致 ✓
- `run_structure_layer(..., layering=)` 在 Task6 定义并被 stage 调用一致 ✓
- `collectAnnotationNames(NodeWithAnnotations<?>)` Task5 定义并被两处 + JUnit 调用一致 ✓

**4. v1 已知缩减（设计文档已记）：** classifier 仅实现实体自带信号（annotation/name_suffix/name_prefix/package_contains/has_attr/language）；`xml_paired/extends/implements` 字段保留但留给 Plan 2 ctx-backed 实现。

---

## Plan 2 / Plan 3 范围与 seam 约定（待 Plan 1 落地后展开完整任务）

**Plan 2 — 按层增强采集（依赖 Plan 1）**
- 新增 `RelationType.EXPOSES_ROUTE / ACCESSES_TABLE`（Python `src/models/structure.py` + Java `model/RelationType.java` **同步**）。
- `src/structure/layering/extractors/`：`base.py`（`LayerExtractor` 协议 `extract(entities_of_layer, facts, ctx)`）、`entry_rest.py`、`business_orchestration.py`、`dao_table.py`。
- `dao_table` **复用** `src/knowledge/mapper_access_index.py::MapperAccessIndex`（SQL→表 解析）+ `ddl_parser.py`，**不重造**（见 [[方法-表访问与SQL映射-设计]]）；产出 `ACCESSES_TABLE`。
- `ExtractorRegistry` 按 `(paradigm, layer)` 注册；`apply_layering` 末尾加「按层跑 extractor」循环（受 `LayerSpec.extractor_enabled` 控制）。
- classifier 补 ctx-backed 信号：`xml_paired`（复用 MyBatis link resolver 结果）、`extends/implements`（用 EXTENDS/IMPLEMENTS 关系）。
- `EXPOSES_ROUTE` 与既有 `API_ENDPOINT`/`SERVICE_EXPOSES` 对齐：**优先复用 api_endpoint**，路由表作其属性增强，避免双写（设计文档「已知限制」末条）。

**Plan 3 — 工程画像器 ProjectProfiler（依赖 Plan 1 配置 schema）**
- `src/structure/layering/profiler.py`：扫指纹（依赖/注解分布/包结构/命名/父类/XML 配对）→ 范式打分 → 产出 `ProjectProfile`（paradigm+confidence+suggested_layers+evidence+warnings）。
- 草稿序列化为可人审的 yaml；CLI `--reprofile`；`profile_on_missing` 生效逻辑。
- 范式指纹库 v1 内置 SSM/three_tier 打分规则。
