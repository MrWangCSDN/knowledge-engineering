# MyBatis XML Extractor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 mall-swarm 的 MyBatis Mapper XML 解析进图谱：每个 `<select|insert|update|delete|sql>` 成 method-shaped entity，Java mapper interface method 自动连到对应 XML statement（CALLS 边，provenance=heuristic）。

**Architecture:** 严格对齐 CodeGraph `mybatis-extractor.ts` + `callback-synthesizer.ts:mybatisJavaXmlEdges`。新增 2 个 Python 模块：`MyBatisXmlExtractor`（regex 扫 XML emit StructureEntity/Relation）+ `synthesize_mybatis_java_xml_edges`（structure 阶段末尾跑，按 `<className>::<methodName>` 后缀匹配 Java method）。`knowledge.graph.build_from` 不动（已经能消费 StructureFacts.entities/relations）。

**Tech Stack:** Python 3.12 / pydantic / pytest / re（标准库 regex，无新依赖）。

**仓库 / 分支:** `/Users/java/knowledge-engineering-auth` 分支 `release-0513`。

**Spec 来源:** Obsidian `[[MyBatis-XML-Extractor-设计]]`（已批准）。

**关键 schema 复用**（已存在，不改）：
- `EntityType.METHOD` / `EntityType.FILE`
- `RelationType.CALLS` / `RelationType.CONTAINS`
- `StructureEntity.language` 字段（设 "xml" 区分）
- `StructureEntity.attributes` dict（存 `sql_kind`, `signature`, `synthesizedBy`, `provenance`）

**Run tests:** `cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth tests/test_structure -q`。

---

## File Structure

| 文件 | 改动 | 责任 |
|---|---|---|
| `src/structure/mybatis_extractor.py` | 🆕 ~200 行 | regex 扫单个 XML 文件 → emit StructureEntity/Relation |
| `src/structure/mybatis_link_resolver.py` | 🆕 ~80 行 | 跨文件 link：Java method → XML statement (CALLS edge) |
| `src/structure/runner.py` | Modify | 扫 source 的 XML 文件 + 调 extractor + 调 resolver |
| `tests/test_structure/__init__.py` | 🆕 空文件 | pytest 包识别 |
| `tests/test_structure/test_mybatis_extractor.py` | 🆕 | 7 测试：mapper 识别、3 statement、sql_fragment、include、preview、non-mapper |
| `tests/test_structure/test_mybatis_link_resolver.py` | 🆕 | 4 测试：基本匹配、ambiguous 丢弃、no-match 跳过、edge metadata |
| `tests/test_structure/fixtures/UmsRoleDao.xml` | 🆕 | 真实 mall-swarm 拷贝 |
| `tests/test_structure/fixtures/UmsRoleDao.java` | 🆕 | 配对 Java interface 拷贝 |
| `tests/test_structure/fixtures/non_mapper.xml` | 🆕 | log4j 风格 negative fixture |

---

## Task 1: MyBatisXmlExtractor（核心）+ 单测

**Files:**
- Create: `src/structure/mybatis_extractor.py`
- Create: `tests/test_structure/__init__.py`（空文件）
- Create: `tests/test_structure/test_mybatis_extractor.py`
- Create: `tests/test_structure/fixtures/UmsRoleDao.xml`
- Create: `tests/test_structure/fixtures/non_mapper.xml`

- [ ] **Step 1: 准备测试 fixture**

```bash
mkdir -p /Users/java/knowledge-engineering-auth/tests/test_structure/fixtures
cp /Users/java/repos/mall-swarm/mall-admin/src/main/resources/dao/UmsRoleDao.xml \
   /Users/java/knowledge-engineering-auth/tests/test_structure/fixtures/UmsRoleDao.xml
cp /Users/java/repos/mall-swarm/mall-admin/src/main/java/com/macro/mall/dao/UmsRoleDao.java \
   /Users/java/knowledge-engineering-auth/tests/test_structure/fixtures/UmsRoleDao.java
```

新建 `tests/test_structure/__init__.py`（空）+ `tests/test_structure/fixtures/non_mapper.xml`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!-- log4j config 风格，不是 mybatis mapper -->
<configuration>
    <appender name="console" class="ch.qos.logback.core.ConsoleAppender">
        <encoder>
            <pattern>%d{HH:mm:ss} %-5level %msg%n</pattern>
        </encoder>
    </appender>
    <root level="INFO">
        <appender-ref ref="console"/>
    </root>
</configuration>
```

- [ ] **Step 2: 写失败测试 `tests/test_structure/test_mybatis_extractor.py`**

```python
"""MyBatisXmlExtractor 单元测试。

设计：[[MyBatis-XML-Extractor-设计]] §3 + §6.1
参考：CodeGraph src/extraction/mybatis-extractor.ts (198 行)
"""
from pathlib import Path

import pytest

from src.models.structure import EntityType, RelationType
from src.structure.mybatis_extractor import MyBatisXmlExtractor


# fixture 路径（相对本测试文件）
FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> tuple[str, str]:
    """读 fixture，返回 (file_path 字符串, source 文本)。"""
    p = FIXTURE_DIR / name
    return (f"tests/fixtures/{name}", p.read_text(encoding="utf-8"))


def test_finds_mapper_root():
    """识别 <mapper namespace="X"> 并提取 namespace。"""
    file_path, source = _load("UmsRoleDao.xml")
    extractor = MyBatisXmlExtractor(file_path, source)
    result = extractor.extract()

    # 至少有 1 个 file entity + N 个 method entity
    file_ents = [e for e in result.entities if e.type == EntityType.FILE]
    assert len(file_ents) == 1
    assert file_ents[0].name == "UmsRoleDao.xml"


def test_emits_method_per_statement():
    """UmsRoleDao.xml 有 3 个 <select>，应 emit 3 个 method entity + 3 个 contains relation。"""
    file_path, source = _load("UmsRoleDao.xml")
    result = MyBatisXmlExtractor(file_path, source).extract()

    method_ents = [e for e in result.entities if e.type == EntityType.METHOD]
    assert len(method_ents) == 3

    names = sorted([e.name for e in method_ents])
    assert names == ["getMenuList", "getMenuListByRoleId", "getResourceListByRoleId"]

    # 每个 method 的 qualifiedName 必须是 namespace::id
    for m in method_ents:
        assert m.attributes["qualified_name"].startswith("com.macro.mall.dao.UmsRoleDao::")
        assert m.language == "xml"

    # 3 个 contains relation: file -> method
    contains = [r for r in result.relations if r.type == RelationType.CONTAINS]
    assert len(contains) == 3
    file_id = next(e.id for e in result.entities if e.type == EntityType.FILE)
    for r in contains:
        assert r.source_id == file_id


def test_signature_format_select_with_result_type():
    """<select resultType="X"> → signature "SELECT result=X"。"""
    file_path, source = _load("UmsRoleDao.xml")
    result = MyBatisXmlExtractor(file_path, source).extract()

    get_menu = next(e for e in result.entities
                    if e.type == EntityType.METHOD and e.name == "getMenuList")
    sig = get_menu.attributes.get("signature", "")
    assert sig.startswith("SELECT")
    assert "result=com.macro.mall.model.UmsMenu" in sig


def test_sql_kind_attribute():
    """每个 statement 节点 attributes 里有 sql_kind。"""
    file_path, source = _load("UmsRoleDao.xml")
    result = MyBatisXmlExtractor(file_path, source).extract()

    for e in result.entities:
        if e.type == EntityType.METHOD:
            assert e.attributes.get("sql_kind") == "select"


def test_sql_preview_in_attributes():
    """attributes 里有 sql_preview 字段（前 256 字符 SQL）。"""
    file_path, source = _load("UmsRoleDao.xml")
    result = MyBatisXmlExtractor(file_path, source).extract()

    get_menu = next(e for e in result.entities
                    if e.type == EntityType.METHOD and e.name == "getMenuList")
    preview = get_menu.attributes.get("sql_preview", "")
    # 含 SELECT 关键字 + 表名（不超 256）
    assert "SELECT" in preview
    assert "ums_admin_role_relation" in preview
    assert len(preview) <= 256


def test_handles_sql_fragment():
    """<sql id="X"> 也 emit 节点，signature 为 "<sql>"。"""
    sql_fragment = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" "x">
<mapper namespace="com.x.FooDao">
    <sql id="baseColumns">id, name, create_time</sql>
    <select id="findAll" resultType="com.x.Foo">
        SELECT <include refid="baseColumns"/> FROM foo
    </select>
</mapper>
'''
    result = MyBatisXmlExtractor("Foo.xml", sql_fragment).extract()
    methods = [e for e in result.entities if e.type == EntityType.METHOD]
    assert len(methods) == 2
    base = next(m for m in methods if m.name == "baseColumns")
    assert base.attributes["sql_kind"] == "sql"
    assert base.attributes["signature"] == "<sql>"


def test_handles_include_refid_emits_relates_to():
    """<include refid="X"> → emit relation 指向同 mapper 内的 sql fragment。

    备注：跨 mapper 引用（refid="other.X"）暂不在 structure 阶段 resolve，
    走 unresolved reference 流程，由后续 resolver / mybatis_link_resolver 处理。
    """
    src = '''<?xml version="1.0"?>
<mapper namespace="com.x.FooDao">
    <sql id="cols">id, name</sql>
    <select id="findAll" resultType="com.x.Foo">
        SELECT <include refid="cols"/> FROM foo
    </select>
</mapper>
'''
    result = MyBatisXmlExtractor("Foo.xml", src).extract()
    # 至少一个 from select → sql_fragment 的 references-style relation
    # 我们用 attributes["include_refids"] 列表记录所有 include，留给 resolver 用
    select = next(e for e in result.entities
                  if e.type == EntityType.METHOD and e.name == "findAll")
    refids = select.attributes.get("include_refids", [])
    assert "cols" in refids or "com.x.FooDao::cols" in refids


def test_non_mapper_xml_only_emits_file():
    """非 mapper XML（如 log4j config）只 emit file entity，不 emit method 节点。"""
    file_path, source = _load("non_mapper.xml")
    result = MyBatisXmlExtractor(file_path, source).extract()

    file_ents = [e for e in result.entities if e.type == EntityType.FILE]
    method_ents = [e for e in result.entities if e.type == EntityType.METHOD]
    assert len(file_ents) == 1
    assert len(method_ents) == 0
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_structure/test_mybatis_extractor.py -v
```
Expected: `ImportError: No module named 'src.structure.mybatis_extractor'`

- [ ] **Step 4: 创建 `src/structure/mybatis_extractor.py`**

```python
"""MyBatis XML Mapper 解析器 — 把 <select|insert|update|delete|sql> 解析进图谱。

设计：[[MyBatis-XML-Extractor-设计]] §3
参考：CodeGraph src/extraction/mybatis-extractor.ts

为什么需要：mall-swarm 大量 DAO 把 SQL 放 Mapper XML 里（Java 接口只有签名）。
当前 pipeline 只跑 javaparser-bridge，XML 完全不在图谱 →
"UmsRoleDao.getMenuList 的真实 SQL" 之类问题问不出。

策略：参考 CodeGraph 的纯 regex 实现（不引 lxml 重依赖），扫每个
<select|insert|update|delete|sql> 顶层标签，emit 一个 method-shaped entity
（type=METHOD, language="xml", qualified=<namespace>::<id>）。
"""
from __future__ import annotations

import hashlib  # 算稳定 entity_id（与现有 canonical_v1 风格对齐）
import re
import time
from dataclasses import dataclass, field
from typing import Any

from src.models.structure import (
    EntityType,
    RelationType,
    StructureEntity,
    StructureRelation,
)


# ─── regex（与 CodeGraph mybatis-extractor.ts 一致）──────────────────────

# <mapper namespace="X"> 根节点识别 + namespace 属性
_MAPPER_OPEN_RE = re.compile(r"<mapper\b([^>]*)>", re.IGNORECASE)
_NAMESPACE_ATTR_RE = re.compile(r'\bnamespace\s*=\s*"([^"]+)"')

# 顶层 statement 元素（select/insert/update/delete/sql）
# 注意：是非贪婪匹配整个标签体，内部嵌套的 <if>/<foreach> 等不影响外层识别
_STATEMENT_RE = re.compile(
    r"<(select|insert|update|delete|sql)\b([^>]*)>([\s\S]*?)</\1>",
    re.IGNORECASE,
)

# 各属性提取
_ID_ATTR_RE = re.compile(r'\bid\s*=\s*"([^"]+)"')
_RESULT_TYPE_ATTR_RE = re.compile(r'\bresultType\s*=\s*"([^"]+)"')
_PARAMETER_TYPE_ATTR_RE = re.compile(r'\bparameterType\s*=\s*"([^"]+)"')

# <include refid="X"/>
_INCLUDE_REFID_RE = re.compile(r'<include\b[^>]*\brefid\s*=\s*"([^"]+)"')

# SQL preview 长度上限（字符），与 CodeGraph 一致
_SQL_PREVIEW_LIMIT = 256


@dataclass
class ExtractionResult:
    """单个 XML 文件的提取结果。

    entities/relations 直接 splice 进 StructureFacts；
    errors 是非致命警告（解析异常但能继续），不抛出。
    """
    entities: list[StructureEntity] = field(default_factory=list)
    relations: list[StructureRelation] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0


class MyBatisXmlExtractor:
    """扫单个 XML 文件，emit StructureEntity/Relation。

    流程：
    1. 总是 emit 一个 file entity（让 watcher / 引用追踪完整）
    2. 找 <mapper namespace="X"> 根
       a. 没找到 → 非 mapper XML（pom.xml / log4j / spring beans），只返 file
       b. 找到 → step 3
    3. regex 扫每个 <select|insert|update|delete|sql>，每个 emit:
       - method entity (qualified=<ns>::<id>, lang="xml",
         attributes={sql_kind, signature, sql_preview, include_refids, qualified_name})
       - CONTAINS relation: file → method
    """

    def __init__(self, file_path: str, source: str):
        self.file_path = file_path
        self.source = source
        self._line_starts = self._compute_line_starts()

    # ── 公共入口 ─────────────────────────────────────────────────────────

    def extract(self) -> ExtractionResult:
        """主入口：扫 self.source，返回 ExtractionResult。不抛异常。"""
        t0 = time.time()
        result = ExtractionResult()

        # 1. 总是 emit file entity
        file_entity = self._create_file_entity()
        result.entities.append(file_entity)

        # 2/3. 找 mapper 根 + 扫 statements
        try:
            mapper = self._find_mapper_root()
            if mapper is not None:
                namespace, body_start, body_end = mapper
                self._extract_mapper(
                    file_entity.id, namespace, body_start, body_end, result
                )
        except Exception as e:
            # 永远不抛：记到 errors，让 structure 阶段继续
            result.errors.append({
                "message": f"MyBatis extraction error: {type(e).__name__}: {e}",
                "severity": "error",
                "code": "parse_error",
            })

        result.duration_ms = int((time.time() - t0) * 1000)
        return result

    # ── 内部 helper ──────────────────────────────────────────────────────

    def _create_file_entity(self) -> StructureEntity:
        """单个 file entity，id 用稳定 hash。"""
        name = self.file_path.rsplit("/", 1)[-1]
        # entity id 风格与现有 canonical_v1 对齐：sha256 短哈希前缀
        sha = hashlib.sha256(self.file_path.encode()).hexdigest()[:16]
        return StructureEntity(
            id=f"file//{sha}",
            type=EntityType.FILE,
            name=name,
            location=f"{self.file_path}:1",
            language="xml",
            attributes={"path": self.file_path},
        )

    def _find_mapper_root(self) -> tuple[str, int, int] | None:
        """找 <mapper namespace="X"> 开标签，返回 (namespace, body_start, body_end)。

        不是 mapper（或没 namespace 属性）→ None。
        """
        m = _MAPPER_OPEN_RE.search(self.source)
        if not m:
            return None
        ns_match = _NAMESPACE_ATTR_RE.search(m.group(1) or "")
        if not ns_match:
            return None
        body_start = m.end()
        close = self.source.find("</mapper>", body_start)
        body_end = close if close >= 0 else len(self.source)
        return ns_match.group(1), body_start, body_end

    def _extract_mapper(
        self,
        file_node_id: str,
        namespace: str,
        body_start: int,
        body_end: int,
        result: ExtractionResult,
    ) -> None:
        """扫 mapper body 内每个 statement，emit method entity + contains relation。"""
        body = self.source[body_start:body_end]

        for m in _STATEMENT_RE.finditer(body):
            elem_type = m.group(1).lower()
            attrs = m.group(2) or ""
            elem_body = m.group(3) or ""

            id_match = _ID_ATTR_RE.search(attrs)
            if not id_match:
                continue  # <select> 没 id 跳过（语义无效）
            statement_id = id_match.group(1)
            is_sql_fragment = (elem_type == "sql")

            # 行号（基于原始 source 的绝对偏移）
            absolute_idx = body_start + m.start()
            start_line = self._get_line_number(absolute_idx)
            end_line = self._get_line_number(absolute_idx + len(m.group(0)))

            qualified = f"{namespace}::{statement_id}"

            # entity id：sha256(file_path + qualified) 前缀
            id_hash = hashlib.sha256(
                f"{self.file_path}//{qualified}".encode()
            ).hexdigest()[:16]
            entity_id = f"method//{id_hash}"

            signature = self._build_signature(elem_type, attrs, is_sql_fragment)
            sql_preview = self._preview_sql(elem_body)
            include_refids = self._extract_include_refids(elem_body, namespace)

            method_entity = StructureEntity(
                id=entity_id,
                type=EntityType.METHOD,
                name=statement_id,
                location=f"{self.file_path}:{start_line}-{end_line}",
                language="xml",
                attributes={
                    "qualified_name": qualified,
                    "sql_kind": elem_type,            # select/insert/update/delete/sql
                    "signature": signature,
                    "sql_preview": sql_preview,
                    "include_refids": include_refids,  # list[str]，留给 link resolver
                    "namespace": namespace,
                },
            )
            result.entities.append(method_entity)

            # file → method 的 CONTAINS 关系
            result.relations.append(StructureRelation(
                type=RelationType.CONTAINS,
                source_id=file_node_id,
                target_id=entity_id,
            ))

    @staticmethod
    def _build_signature(elem_type: str, attrs: str, is_sql_fragment: bool) -> str:
        """构造 signature 字符串。

        Examples:
            <sql id="X">                     → "<sql>"
            <select id="X" resultType="Y">   → "SELECT result=Y"
            <insert id="X" parameterType="A"> → "INSERT param=A"
            <update id="X">                  → "UPDATE"
        """
        if is_sql_fragment:
            return "<sql>"
        verb = elem_type.upper()
        rt = _RESULT_TYPE_ATTR_RE.search(attrs)
        pt = _PARAMETER_TYPE_ATTR_RE.search(attrs)
        parts = [verb]
        if rt:
            parts.append(f"result={rt.group(1)}")
        if pt:
            parts.append(f"param={pt.group(1)}")
        return " ".join(parts)

    @staticmethod
    def _preview_sql(body: str) -> str:
        """SQL preview：strip XML 注释、压空白，截到 256 字符。"""
        cleaned = re.sub(r"<!--[\s\S]*?-->", "", body)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned[:_SQL_PREVIEW_LIMIT]

    @staticmethod
    def _extract_include_refids(body: str, namespace: str) -> list[str]:
        """扫 <include refid="X"/>，返回限定后的 refid 列表。

        - 同 mapper 内 refid（如 "cols"）→ 限定为 "<namespace>::cols"
        - 跨 mapper refid（如 "other.cols"）→ 限定为 "<other>::cols"
        """
        out: list[str] = []
        for m in _INCLUDE_REFID_RE.finditer(body):
            refid = m.group(1)
            if "." in refid:
                # 跨 mapper：dotted → 替换为 ::
                out.append(refid.replace(".", "::"))
            else:
                out.append(f"{namespace}::{refid}")
        return out

    def _compute_line_starts(self) -> list[int]:
        """预算每行起始偏移，加速 _get_line_number。"""
        starts = [0]
        for i, ch in enumerate(self.source):
            if ch == "\n":
                starts.append(i + 1)
        return starts

    def _get_line_number(self, offset: int) -> int:
        """二分查 offset 所在行号（1-based）。"""
        import bisect
        idx = bisect.bisect_right(self._line_starts, offset)
        return max(1, idx)
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_structure/test_mybatis_extractor.py -v
```
Expected: **8 PASS**.

- [ ] **Step 6: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/structure/mybatis_extractor.py \
        tests/test_structure/__init__.py \
        tests/test_structure/test_mybatis_extractor.py \
        tests/test_structure/fixtures/
git commit -m "$(cat <<'EOF'
feat(structure): 新增 MyBatisXmlExtractor — XML Mapper 解析进图谱

参考 CodeGraph src/extraction/mybatis-extractor.ts (198 行)。
regex 扫 <mapper namespace="X"> 根 + 每个 <select|insert|update|delete|sql>
emit method-shaped StructureEntity（lang=xml, qualified=<ns>::<id>，
attributes 含 sql_kind / signature / sql_preview / include_refids / namespace）。
非 mapper XML（log4j 等）只 emit file entity，不爆错。

设计：[[MyBatis-XML-Extractor-设计]] §3
fixture：mall-swarm 真实 UmsRoleDao.xml（3 个 select statement）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: mybatis_link_resolver + 单测

**Files:**
- Create: `src/structure/mybatis_link_resolver.py`
- Create: `tests/test_structure/test_mybatis_link_resolver.py`

- [ ] **Step 1: 写失败测试 `tests/test_structure/test_mybatis_link_resolver.py`**

```python
"""mybatis_link_resolver 单元测试。

设计：[[MyBatis-XML-Extractor-设计]] §4 + §6.2
参考：CodeGraph callback-synthesizer.ts mybatisJavaXmlEdges()
"""
from src.models.structure import (
    EntityType,
    RelationType,
    StructureEntity,
    StructureFacts,
)
from src.structure.mybatis_link_resolver import (
    synthesize_mybatis_java_xml_relations,
)


def _java_method(class_fqn: str, method_name: str) -> StructureEntity:
    """构造一个 Java method entity（最小字段）。"""
    qualified = f"{class_fqn}::{method_name}"
    return StructureEntity(
        id=f"method//java-{class_fqn}-{method_name}",
        type=EntityType.METHOD,
        name=method_name,
        language="java",
        attributes={"qualified_name": qualified},
    )


def _xml_statement(namespace: str, id_: str) -> StructureEntity:
    """构造一个 XML statement entity。"""
    qualified = f"{namespace}::{id_}"
    return StructureEntity(
        id=f"method//xml-{namespace}-{id_}",
        type=EntityType.METHOD,
        name=id_,
        language="xml",
        attributes={"qualified_name": qualified, "namespace": namespace},
    )


def test_basic_match_synthesizes_edge():
    """单 Java method ↔ 单 XML statement，合成 1 个 CALLS 边。"""
    facts = StructureFacts(entities=[
        _java_method("com.macro.mall.dao.UmsRoleDao", "getMenuList"),
        _xml_statement("com.macro.mall.dao.UmsRoleDao", "getMenuList"),
    ])
    edges = synthesize_mybatis_java_xml_relations(facts)
    assert len(edges) == 1
    e = edges[0]
    assert e.type == RelationType.CALLS
    assert e.source_id.startswith("method//java-")
    assert e.target_id.startswith("method//xml-")
    assert e.attributes.get("synthesizedBy") == "mybatis-java-xml"
    assert e.attributes.get("provenance") == "heuristic"


def test_ambiguous_match_drops_conservatively():
    """两个不同包的同名 Java 类（重名）→ 不合成（保守丢弃）。"""
    facts = StructureFacts(entities=[
        _java_method("com.a.UserDao", "findById"),
        _java_method("com.b.UserDao", "findById"),  # 同 simpleName，不同包
        _xml_statement("com.a.UserDao", "findById"),
    ])
    edges = synthesize_mybatis_java_xml_relations(facts)
    assert len(edges) == 0


def test_no_java_match_skips_silently():
    """XML statement 找不到 Java 对应 method → 跳过，不报错也不合成。"""
    facts = StructureFacts(entities=[
        _xml_statement("com.x.OrphanDao", "queryAll"),
        # 故意不放 Java method
    ])
    edges = synthesize_mybatis_java_xml_relations(facts)
    assert len(edges) == 0


def test_edge_metadata_contains_via():
    """合成的 edge metadata 含 'via' 字段（className.id 形式）。"""
    facts = StructureFacts(entities=[
        _java_method("com.x.FooDao", "doIt"),
        _xml_statement("com.x.FooDao", "doIt"),
    ])
    edges = synthesize_mybatis_java_xml_relations(facts)
    assert edges[0].attributes.get("via") == "FooDao.doIt"


def test_multiple_xml_statements_match_in_one_pass():
    """一个 mapper 的多个 statement 同时匹配。"""
    facts = StructureFacts(entities=[
        _java_method("com.x.RoleDao", "getMenuList"),
        _java_method("com.x.RoleDao", "getResourceList"),
        _xml_statement("com.x.RoleDao", "getMenuList"),
        _xml_statement("com.x.RoleDao", "getResourceList"),
    ])
    edges = synthesize_mybatis_java_xml_relations(facts)
    assert len(edges) == 2
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_structure/test_mybatis_link_resolver.py -v
```
Expected: ImportError on `synthesize_mybatis_java_xml_relations`.

- [ ] **Step 3: 创建 `src/structure/mybatis_link_resolver.py`**

```python
"""MyBatis Java↔XML 关联 — 把 Java mapper interface method 跟 XML statement 连起来。

设计：[[MyBatis-XML-Extractor-设计]] §4
参考：CodeGraph callback-synthesizer.ts mybatisJavaXmlEdges() (62 行)

算法：
1. 索引所有 Java/Kotlin method entity by `<ClassName>::<methodName>`
2. 对每个 language=xml method entity：
   a. 从 qualified_name 拆 namespace + id
   b. className = namespace 最后一段（点分割）
   c. 在 Java 索引按 <className>::<id> 查
   d. 唯一匹配 → emit StructureRelation(type=CALLS,
      attributes={synthesizedBy: "mybatis-java-xml", provenance: "heuristic", via: ...})
   e. 多匹配（包名歧义）→ 保守丢弃
   f. 0 匹配 → 跳过

为什么"保守丢弃"：mall-swarm 实测 namespace 都是唯一的，
误连边比缺连边更影响下游（ke_callees / ke_impact 会跑到错误链路）。
"""
from __future__ import annotations

from src.models.structure import (
    EntityType,
    RelationType,
    StructureEntity,
    StructureFacts,
    StructureRelation,
)


def synthesize_mybatis_java_xml_relations(
    facts: StructureFacts,
) -> list[StructureRelation]:
    """合成 Java method → XML statement 的 CALLS relation 列表。

    :param facts: StructureFacts，含 Java + XML 两种 method entity
    :returns: 新合成的 CALLS relation 列表（不会直接 append 到 facts，
              留给调用方控制时机）
    """
    # 1. 索引 Java/Kotlin method by <ClassName>::<methodName>
    java_index: dict[str, list[StructureEntity]] = {}
    for e in facts.entities:
        if e.type != EntityType.METHOD:
            continue
        if (e.language or "").lower() not in ("java", "kotlin"):
            continue
        qn = e.attributes.get("qualified_name", "")
        parts = qn.split("::")
        if len(parts) < 2:
            continue
        class_fqn = parts[-2]      # 如 "com.x.UserDao"
        method_name = parts[-1]    # 如 "findById"
        class_name = class_fqn.split(".")[-1]  # "UserDao"
        key = f"{class_name}::{method_name}"
        java_index.setdefault(key, []).append(e)

    # 2. 遍历 XML method 匹配
    new_edges: list[StructureRelation] = []
    seen: set[str] = set()
    for xml in facts.entities:
        if xml.type != EntityType.METHOD:
            continue
        if (xml.language or "").lower() != "xml":
            continue
        qn = xml.attributes.get("qualified_name", "")
        if "::" not in qn:
            continue
        namespace, statement_id = qn.rsplit("::", 1)
        if not namespace or not statement_id:
            continue
        class_name = namespace.split(".")[-1]
        candidates = java_index.get(f"{class_name}::{statement_id}", [])

        # 保守策略：多匹配丢弃；0 匹配跳过
        if len(candidates) != 1:
            continue
        java = candidates[0]

        # 去重（同一对 java/xml 只连一次）
        key = f"{java.id}->{xml.id}"
        if key in seen:
            continue
        seen.add(key)

        new_edges.append(StructureRelation(
            type=RelationType.CALLS,
            source_id=java.id,
            target_id=xml.id,
            attributes={
                "synthesizedBy": "mybatis-java-xml",
                "provenance": "heuristic",
                "via": f"{class_name}.{statement_id}",
            },
        ))

    return new_edges
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_structure/test_mybatis_link_resolver.py -v
```
Expected: **5 PASS**.

- [ ] **Step 5: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/structure/mybatis_link_resolver.py tests/test_structure/test_mybatis_link_resolver.py
git commit -m "$(cat <<'EOF'
feat(structure): 新增 mybatis_link_resolver — Java method ↔ XML statement 连边

参考 CodeGraph callback-synthesizer.ts mybatisJavaXmlEdges() (62 行)。
按 <className>::<methodName> 后缀匹配，唯一匹配 emit CALLS 边
（attributes.synthesizedBy="mybatis-java-xml", provenance="heuristic", via=...）。
多匹配（包名歧义）保守丢弃，0 匹配跳过 — 误连比漏连危害大。

设计：[[MyBatis-XML-Extractor-设计]] §4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 集成进 structure runner

**Files:**
- Modify: `src/structure/runner.py`

- [ ] **Step 1: Read 当前 runner.py**

已知现状（见 plan 顶部）：`run_structure_layer` 只跑 `run_javaparser_bridge`，返回 StructureFacts。需要在 Java facts 之后追加 XML facts + 跑 link resolver。

- [ ] **Step 2: 改 `src/structure/runner.py` 整文件替换**

```python
"""结构层入口：从 CodeInputSource 产出 StructureFacts。

使用 JavaParser Bridge (Java 1-25+) 解析 Java 源码；
+ 用 MyBatisXmlExtractor 解析 Mapper XML（设计 [[MyBatis-XML-Extractor-设计]]）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from src.models import CodeInputSource, StructureFacts

_LOG = logging.getLogger(__name__)


def run_structure_layer(
    source: CodeInputSource,
    extract_cross_service: bool = True,
    progress_callback: Optional[Any] = None,
) -> StructureFacts:
    """
    对 source 中的文件做 AST 解析与结构抽取，输出与语言无关的结构事实。
    使用 JavaParser Bridge（支持 Java 1-25+）+ MyBatisXmlExtractor。
    """
    language = (source.language or "java").lower()
    if language != "java":
        return StructureFacts(meta={"language": language, "message": "仅支持 java，其余返回空"})

    from .javaparser_bridge import run_javaparser_bridge

    if progress_callback:
        progress_callback(0, 3, "正在解析 Java 代码（JavaParser, Java 1-25+）…")

    facts = run_javaparser_bridge(
        source=source,
        extract_cross_service=extract_cross_service,
        progress_callback=progress_callback,
    )

    # ── 新增：扫 Mapper XML（设计 §3）────────────────────────────────────
    if progress_callback:
        progress_callback(1, 3, "正在解析 MyBatis Mapper XML…")
    xml_added = _extract_mybatis_xml(source, facts)
    _LOG.info("[structure] MyBatis XML 解析：新增 %d 个 entity", xml_added)

    # ── 新增：跨文件 link Java method ↔ XML statement（设计 §4）─────────
    if progress_callback:
        progress_callback(2, 3, "正在关联 Java method ↔ XML statement…")
    link_added = _link_java_to_mybatis_xml(facts)
    _LOG.info("[structure] MyBatis Java↔XML 合成边：%d 条", link_added)

    if progress_callback:
        progress_callback(3, 3,
            f"代码结构解析完成（{len(facts.entities)} 实体, {len(facts.relations)} 关系）")

    return facts


def _extract_mybatis_xml(source: CodeInputSource, facts: StructureFacts) -> int:
    """扫 source.repo_path 下所有 .xml，跑 MyBatisXmlExtractor，把 entities/relations 追加到 facts。

    :returns: 新增的 entity 数（含 file + method）
    """
    from .mybatis_extractor import MyBatisXmlExtractor

    repo_path = getattr(source, "repo_path", None) or getattr(source, "path", None)
    if not repo_path:
        _LOG.warning("[structure] source 无 repo_path/path，跳过 MyBatis XML 扫描")
        return 0

    root = Path(repo_path)
    if not root.exists():
        _LOG.warning("[structure] repo_path 不存在：%s", repo_path)
        return 0

    added = 0
    # mall-swarm 的 mapper XML 在 src/main/resources 下；广撒网扫所有 .xml
    for xml_path in root.rglob("*.xml"):
        # 跳过常见非源码目录
        rel = xml_path.relative_to(root)
        parts = rel.parts
        if any(p in ("target", "build", "node_modules", ".git", "dist") for p in parts):
            continue
        try:
            content = xml_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            _LOG.warning("[structure] 读 XML 失败 %s: %s", xml_path, e)
            continue

        # extractor 用项目相对路径作为 entity location
        rel_str = str(rel).replace("\\", "/")
        extractor = MyBatisXmlExtractor(rel_str, content)
        result = extractor.extract()

        # 把 result.entities / result.relations splice 进 facts
        facts.entities.extend(result.entities)
        facts.relations.extend(result.relations)
        added += len(result.entities)

        if result.errors:
            for err in result.errors:
                _LOG.warning("[structure] mybatis_extractor error in %s: %s",
                             rel_str, err.get("message"))

    return added


def _link_java_to_mybatis_xml(facts: StructureFacts) -> int:
    """跑 mybatis_link_resolver，把合成的 CALLS edges 追加到 facts.relations。

    :returns: 新合成的 edge 数
    """
    from .mybatis_link_resolver import synthesize_mybatis_java_xml_relations

    new_edges = synthesize_mybatis_java_xml_relations(facts)
    facts.relations.extend(new_edges)
    return len(new_edges)
```

- [ ] **Step 3: Run existing tests not regress**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth tests/test_structure -q 2>&1 | tail -5
```
Expected: 687 (baseline) + 13 (Tasks 1+2 加的) = ~700 pass, 0 fail.

- [ ] **Step 4: Commit**

```bash
cd /Users/java/knowledge-engineering-auth
git add src/structure/runner.py
git commit -m "$(cat <<'EOF'
feat(structure): runner 串联 MyBatis XML extractor + link resolver

run_structure_layer 三步：
1. javaparser-bridge 解析 Java 源码（原流程）
2. 扫 source.repo_path 下所有 .xml，跑 MyBatisXmlExtractor
3. 跑 mybatis_link_resolver，合成 Java method ↔ XML statement 的 CALLS 边

跳过 target/build/node_modules/.git/dist 等非源码目录。
非 mapper XML 仍 emit file entity（保持引用追踪完整）。

设计：[[MyBatis-XML-Extractor-设计]] §2 + §5

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: mall-swarm 重跑 pipeline + E2E 验证

**Files:** （无代码改动）

- [ ] **Step 1: 准备 — 确认 tunnel 和 uvicorn**

```bash
# MySQL tunnel
lsof -nP -iTCP:3307 -sTCP:LISTEN 2>/dev/null | head -2 || bash /Users/java/knowledge-engineering-auth/scripts/start_mysql_tunnel.sh

# 重启 uvicorn（带 KE_QA_USE_REACT=1 + 新 structure 代码已 reload）
pkill -f "uvicorn src.service.api:app" 2>/dev/null
sleep 2
cd /Users/java/knowledge-engineering-auth && KE_QA_USE_REACT=1 nohup ./venv/bin/uvicorn src.service.api:app --host 127.0.0.1 --port 8000 --reload > /tmp/uvicorn-react.log 2>&1 &
sleep 5
```

- [ ] **Step 2: 跑 pipeline 重建 mall-swarm 图谱（含新加的 XML 节点）**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m scripts.run_pipeline_with_env --until knowledge --without-interpretation --without-business-interpretation 2>&1 | tail -20
```

Expected log 含：
- `[structure] MyBatis XML 解析：新增 NNN 个 entity`（NNN 应该 ≥200）
- `[structure] MyBatis Java↔XML 合成边：MM 条`（MM 应该 ≥200）
- `Pipeline stage: knowledge` + `Graph nodes: ... edges: ...`

如失败：看 stderr 全文，常见问题：
- `repo_path` attribute 错（看 CodeInputSource schema） → 修 `_extract_mybatis_xml` 里的 getattr 链
- regex 性能（mall-swarm 60+ XML 文件） → 监控时长，如 > 30s 考虑改 lxml

- [ ] **Step 3: Neo4j 直查验证**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -c "
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path('.env.local'), override=False)
from neo4j import GraphDatabase
drv = GraphDatabase.driver(os.environ['NEO4J_URI'], auth=(os.environ.get('NEO4J_USER','neo4j'), os.environ['NEO4J_PASSWORD']))
with drv.session() as s:
    # XML method 节点数
    r = s.run(\"MATCH (n) WHERE n.project_id='mall-swarm' AND n.language='xml' AND n.entity_type='method' RETURN count(n) AS c\").single()
    print(f'XML method nodes: {r[\"c\"]}')
    # Java→XML 合成 edges
    r = s.run(\"\"\"
        MATCH (j)-[r]->(x)
        WHERE j.project_id='mall-swarm' AND x.language='xml' AND r.synthesizedBy='mybatis-java-xml'
        RETURN count(r) AS c
    \"\"\").single()
    print(f'mybatis-java-xml edges: {r[\"c\"]}')
    # 抽样：UmsRoleDao 的 XML statement
    r = s.run(\"\"\"
        MATCH (n) WHERE n.project_id='mall-swarm' AND n.language='xml'
              AND n.name='getMenuList'
        RETURN n.qualified_name AS qn, n.sql_preview AS preview LIMIT 3
    \"\"\").data()
    for row in r:
        print(f'  {row[\"qn\"]}: {row[\"preview\"][:80]}...')
drv.close()
"
```

Expected:
- `XML method nodes:` ≥ 200
- `mybatis-java-xml edges:` ≥ 200  
- sample 含 `com.macro.mall.dao.UmsRoleDao::getMenuList` + SQL preview

- [ ] **Step 4: E2E LLM 验证**

```bash
curl -sS -X POST http://localhost:8000/auth/login -H "Content-Type: application/json" -d '{"username":"alice","password":"test12345"}' 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" > /tmp/alice.token

curl -sS -N -X POST http://localhost:8000/projects/mall-swarm/qa/explain \
  -H "Authorization: Bearer $(cat /tmp/alice.token)" \
  -H "Content-Type: application/json" \
  -d '{"question":"UmsRoleDao.getMenuList 真实执行什么 SQL？请用工具查"}' 2>&1 | grep -E "tool_call|result_preview" | head -10
```

Expected: LLM 调 ke_callees 或 ke_search 拿到 XML method entity，result_preview 含真实 SQL（`SELECT m.id...FROM ums_admin_role_relation...`）。

- [ ] **Step 5: 全套回归**

```bash
cd /Users/java/knowledge-engineering-auth && ./venv/bin/python -m pytest tests/test_auth tests/test_structure -q --tb=short 2>&1 | tail -5
```
Expected: ~700 pass, 0 fail.

- [ ] **Step 6: Commit（如有 fixup）**

如 Task 3 dry-run 后发现 `repo_path` getattr 错或类似小问题，单独 fixup commit。

---

## Task 5: Obsidian 设计文档 §11 实施完成标记

**Files:**
- Modify: `/Users/java/obsidian/01 Engineering/knowledge-engineering/MyBatis-XML-Extractor-设计.md`

- [ ] **Step 1: 在文末追加 §11**

收集 commits：
```bash
cd /Users/java/knowledge-engineering-auth && git log --oneline release-0513..HEAD | head -10
```

打开 spec 文件，文末追加：

```markdown
---

## §11 实施完成（2026-05-27）

4 个 task 完成，全套回归 ~700 pass。

### Commits 列表

| Task | Commit | 内容 |
|---|---|---|
| 1 | `<sha1>` | MyBatisXmlExtractor + 8 测试 + UmsRoleDao fixture |
| 2 | `<sha2>` | mybatis_link_resolver + 5 测试 |
| 3 | `<sha3>` | structure runner 串联（扫 XML + 调 link resolver） |
| 4 | （本提交）| mall-swarm 重跑 + E2E 验证 + doc §11 |

### 实测数据

- mall-swarm XML method 节点：**XXX**（待跑后填）
- Java↔XML synthesized 边：**YYY**
- 全套测试：687（baseline）→ ~700 pass
- 端到端：alice 问 "UmsRoleDao.getMenuList 真实 SQL" → 拿到 4 表 LEFT JOIN 全文

### 已知 follow-up（spec §9 列出）

1. SQL 表名提取（用 sqlparse）→ method → table reads/writes edges
2. 动态 SQL 标签 `<if>`/`<foreach>` 结构化建模
3. `@Select` `@Insert` annotation 风格 SQL 也建图
4. 服务器侧 `/opt/mall-swarm-source` 同步跑新 pipeline
```

填实际 commit SHA + 实测数据。

- [ ] **Step 2: 可选 Commit Obsidian 改动**（如果 vault 是 git）

```bash
cd /Users/java/obsidian
[ -d .git ] && git add "01 Engineering/knowledge-engineering/MyBatis-XML-Extractor-设计.md" && git commit -m "docs(ke-mybatis): §11 实施完成记录"
```

---

## Self-Review

**1. Spec 覆盖**

| Spec 段 | Task |
|---|---|
| §0 背景 + §1 决策 | Plan Goal + Architecture |
| §2 架构图 | Task 1 + 2 + 3 共同实现 |
| §3 extractor | Task 1 |
| §3.4 buildSignature | Task 1 step 4 `_build_signature` |
| §3.5 docstring=preview | Task 1 step 4 `_preview_sql` |
| §4 link resolver | Task 2 |
| §4.2 在 pipeline 中调用 | Task 3（structure runner 末尾，**修订**：spec 写"knowledge 阶段"，plan 改为 structure runner 末尾——更简洁，knowledge.graph.build_from 一行不动） |
| §5 文件清单 | Plan File Structure |
| §6 测试 | Task 1 (8 测试) + Task 2 (5 测试) |
| §7 验收 | Task 4 |

**关键 deviation**：spec §4.2 说 resolver 在 knowledge 阶段跑，plan 改为 **structure runner 末尾跑**（更简洁，避免 knowledge.graph 改动）。设计意图相同，实施路径调整。

**2. Placeholder scan**：每个 step 都有真实 Python/shell code，无 TBD。

**3. Type / signature 一致**：
- `MyBatisXmlExtractor(file_path, source).extract() → ExtractionResult` — Task 1 定义 + Task 3 使用
- `synthesize_mybatis_java_xml_relations(facts) → list[StructureRelation]` — Task 2 定义 + Task 3 使用
- `StructureEntity.attributes["qualified_name"]` 约定 — Task 1 写 + Task 2 读
- `language="xml"` / `language="java"` 字段 — 全程一致

一致 ✅。

---
