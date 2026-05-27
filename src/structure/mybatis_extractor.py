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

# 从已有的结构模型导入需要的类型
# EntityType / RelationType 是枚举（str, Enum），StructureEntity/StructureRelation 是 Pydantic BaseModel
from src.models.structure import (
    EntityType,
    RelationType,
    StructureEntity,
    StructureRelation,
)


# ─── regex（与 CodeGraph mybatis-extractor.ts 一致）──────────────────────

# <mapper namespace="X"> 根节点识别 + namespace 属性
# re.IGNORECASE：允许 <Mapper namespace=...> 等大小写变体（实际 XML 极少，但防御性保留）
_MAPPER_OPEN_RE = re.compile(r"<mapper\b([^>]*)>", re.IGNORECASE)

# 从标签属性串里提取 namespace="X"
_NAMESPACE_ATTR_RE = re.compile(r'\bnamespace\s*=\s*"([^"]+)"')

# 顶层 statement 元素（select/insert/update/delete/sql）
# [\s\S]*? 非贪婪匹配标签体，确保遇到第一个 </同名标签> 就停止
# 注意：内部嵌套的 <if>/<foreach>/<trim> 等动态 SQL 标签不影响外层正则识别
# 局限：如果同一个 mapper 里嵌套了同名标签（极罕见），正则会提前截断 — 可接受
_STATEMENT_RE = re.compile(
    r"<(select|insert|update|delete|sql)\b([^>]*)>([\s\S]*?)</\1>",
    re.IGNORECASE,
)

# 各属性提取（从标签 <tag attr1="v1" attr2="v2"> 的属性串里捞字段）
_ID_ATTR_RE = re.compile(r'\bid\s*=\s*"([^"]+)"')
_RESULT_TYPE_ATTR_RE = re.compile(r'\bresultType\s*=\s*"([^"]+)"')
_PARAMETER_TYPE_ATTR_RE = re.compile(r'\bparameterType\s*=\s*"([^"]+)"')

# <include refid="X"/>  — MyBatis 的 SQL 片段引用
_INCLUDE_REFID_RE = re.compile(r'<include\b[^>]*\brefid\s*=\s*"([^"]+)"')

# SQL preview 长度上限（字符），与 CodeGraph 一致
_SQL_PREVIEW_LIMIT = 256


@dataclass
class ExtractionResult:
    """单个 XML 文件的提取结果。

    entities/relations 直接 splice 进 StructureFacts；
    errors 是非致命警告（解析异常但能继续），不抛出。

    Python dataclass 说明：
    - @dataclass 装饰器自动生成 __init__、__repr__ 等方法
    - field(default_factory=list) 表示每个实例用独立的空列表初始化
      （如果写 entities: list = [] 会造成所有实例共享同一个列表 — 经典 Python 陷阱）
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
        # file_path：相对仓库根的路径字符串，用于 entity id 哈希 + location 字段
        self.file_path = file_path
        # source：XML 文件的完整文本内容
        self.source = source
        # 预计算每行起始偏移，用于 offset → 行号转换（见 _compute_line_starts）
        self._line_starts = self._compute_line_starts()

    # ── 公共入口 ─────────────────────────────────────────────────────────

    def extract(self) -> ExtractionResult:
        """主入口：扫 self.source，返回 ExtractionResult。不抛异常。

        设计上"永不抛"：XML 解析错误会记到 result.errors，让 pipeline 继续处理其他文件。
        """
        # time.time() 返回 Unix 浮点秒；用于记录解析耗时（监控 slow XML）
        t0 = time.time()
        result = ExtractionResult()

        # 1. 总是 emit file entity（即使不是 mapper XML，也要记录文件存在）
        file_entity = self._create_file_entity()
        result.entities.append(file_entity)

        # 2/3. 找 mapper 根 + 扫 statements
        try:
            # _find_mapper_root 返回 None（非 mapper）或 (namespace, body_start, body_end)
            mapper = self._find_mapper_root()
            if mapper is not None:
                # Python 的元组解包：一行同时赋值三个变量
                namespace, body_start, body_end = mapper
                self._extract_mapper(
                    file_entity.id, namespace, body_start, body_end, result
                )
        except Exception as e:
            # 宽泛 except：保证任何奇怪的 XML（BOM、编码错误等）不崩 pipeline
            # type(e).__name__ 获取异常类名（如 "ValueError"）
            result.errors.append({
                "message": f"MyBatis extraction error: {type(e).__name__}: {e}",
                "severity": "error",
                "code": "parse_error",
            })

        # int(...) 截断小数；* 1000 把秒转毫秒
        result.duration_ms = int((time.time() - t0) * 1000)
        return result

    # ── 内部 helper ──────────────────────────────────────────────────────

    def _create_file_entity(self) -> StructureEntity:
        """单个 file entity，id 用稳定 hash（与 canonical_v1 风格对齐）。"""
        # rsplit("/", 1)[-1]：从右侧按 "/" 分割一次，取最后一段 = 文件名
        name = self.file_path.rsplit("/", 1)[-1]
        # hashlib.sha256：确定性哈希，相同路径 → 相同 id，跨 pipeline 运行稳定
        # .encode()：str → bytes（sha256 只接受 bytes）
        # .hexdigest()[:16]：取 16 位十六进制字符（64 位熵足够避免碰撞）
        sha = hashlib.sha256(self.file_path.encode()).hexdigest()[:16]
        return StructureEntity(
            id=f"file//{sha}",           # file// 前缀标识文件类型 entity
            type=EntityType.FILE,
            name=name,
            location=f"{self.file_path}:1",  # 文件 entity 固定指向第 1 行
            language="xml",
            attributes={"path": self.file_path},
        )

    def _find_mapper_root(self) -> tuple[str, int, int] | None:
        """找 <mapper namespace="X"> 开标签，返回 (namespace, body_start, body_end)。

        不是 mapper（或没 namespace 属性）→ None。

        返回值类型 tuple[str, int, int] | None 的含义：
        - str：namespace 字符串（如 "com.macro.mall.dao.UmsRoleDao"）
        - int body_start：<mapper> 标签关闭后的字符偏移（标签体起始）
        - int body_end：</mapper> 开始位置（或文件末尾）
        """
        # re.search 扫整个字符串，找第一处匹配；不像 re.match 只看开头
        m = _MAPPER_OPEN_RE.search(self.source)
        if not m:
            return None  # 没找到 <mapper ...> → 不是 mybatis mapper XML
        # m.group(1) 是第一个捕获组：<mapper> 的属性串（如 ' namespace="com.x.Foo"'）
        ns_match = _NAMESPACE_ATTR_RE.search(m.group(1) or "")
        if not ns_match:
            return None  # 有 <mapper> 但没 namespace 属性 → 跳过
        # m.end()：<mapper ...> 标签整体结束后的偏移，即标签体起始位置
        body_start = m.end()
        # str.find() 返回子串首次出现的偏移，找不到返回 -1
        close = self.source.find("</mapper>", body_start)
        body_end = close if close >= 0 else len(self.source)
        # ns_match.group(1)：namespace 属性值（如 "com.macro.mall.dao.UmsRoleDao"）
        return ns_match.group(1), body_start, body_end

    def _extract_mapper(
        self,
        file_node_id: str,
        namespace: str,
        body_start: int,
        body_end: int,
        result: ExtractionResult,
    ) -> None:
        """扫 mapper body 内每个 statement，emit method entity + contains relation。

        :param file_node_id: 父文件 entity 的 id，用于 CONTAINS 关系的 source
        :param namespace:    mapper 的 namespace（如 "com.macro.mall.dao.UmsRoleDao"）
        :param body_start:   <mapper> 标签后的字符偏移
        :param body_end:     </mapper> 前的字符偏移
        :param result:       就地追加，不返回值
        """
        # 切片提取 mapper body：只在这段文本里跑 statement regex，避免误匹配文件头
        body = self.source[body_start:body_end]

        # re.finditer 返回迭代器，每次 yield 一个 Match 对象
        for m in _STATEMENT_RE.finditer(body):
            # group(1)：捕获组 1 = 标签名（select/insert/update/delete/sql）
            # .lower()：统一小写，处理 <SELECT> 这类奇葩写法
            elem_type = m.group(1).lower()
            # group(2)：捕获组 2 = 标签的属性串（如 ' id="findAll" resultType="..."'）
            attrs = m.group(2) or ""
            # group(3)：捕获组 3 = 标签体内的 SQL 文本
            elem_body = m.group(3) or ""

            # 从属性串里提取 id="X"
            id_match = _ID_ATTR_RE.search(attrs)
            if not id_match:
                continue  # <select> 没 id 跳过（语义上无效的 statement）
            statement_id = id_match.group(1)

            # 布尔标记：是否是 <sql> 片段（与 select/insert/update/delete 处理略有不同）
            is_sql_fragment = (elem_type == "sql")

            # ── 行号计算 ──────────────────────────────────────────────
            # m.start() 是相对 body 的偏移，加上 body_start 换算回原始 source 的绝对偏移
            absolute_idx = body_start + m.start()
            start_line = self._get_line_number(absolute_idx)
            end_line = self._get_line_number(absolute_idx + len(m.group(0)))

            # qualified name = "namespace::id"（与 CodeGraph 约定一致）
            qualified = f"{namespace}::{statement_id}"

            # entity id：sha256(file_path + qualified)[:16]
            # 用 file_path 参与哈希，避免不同文件里 id 相同时碰撞
            id_hash = hashlib.sha256(
                f"{self.file_path}//{qualified}".encode()
            ).hexdigest()[:16]
            entity_id = f"method//{id_hash}"

            # 构建 signature、preview、include refids
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
                    "sql_kind": elem_type,             # select/insert/update/delete/sql
                    "signature": signature,
                    "sql_preview": sql_preview,
                    "include_refids": include_refids,   # list[str]，留给 link resolver
                    "namespace": namespace,
                },
            )
            result.entities.append(method_entity)

            # CONTAINS 关系：file entity → method entity
            # 含义：这个 SQL statement 被包含在这个 XML 文件里
            result.relations.append(StructureRelation(
                type=RelationType.CONTAINS,
                source_id=file_node_id,
                target_id=entity_id,
            ))

    @staticmethod
    def _build_signature(elem_type: str, attrs: str, is_sql_fragment: bool) -> str:
        """构造 signature 字符串（给 LLM / UI 展示用的简短说明）。

        @staticmethod 说明：这个方法不用访问 self，所以声明为静态方法。
        等效写法：在类外定义一个普通函数 _build_signature(elem_type, attrs, ...)。

        Examples:
            <sql id="X">                     → "<sql>"
            <select id="X" resultType="Y">   → "SELECT result=Y"
            <insert id="X" parameterType="A"> → "INSERT param=A"
            <update id="X">                  → "UPDATE"
        """
        if is_sql_fragment:
            return "<sql>"
        # str.upper()：把字符串全部转大写
        verb = elem_type.upper()
        # re.search 返回 Match 对象或 None；利用短路逻辑只在匹配时取 .group(1)
        rt = _RESULT_TYPE_ATTR_RE.search(attrs)
        pt = _PARAMETER_TYPE_ATTR_RE.search(attrs)
        # 用 list 收集 signature 的各部分，最后 " ".join 拼接
        parts = [verb]
        if rt:
            parts.append(f"result={rt.group(1)}")
        if pt:
            parts.append(f"param={pt.group(1)}")
        return " ".join(parts)

    @staticmethod
    def _preview_sql(body: str) -> str:
        """SQL preview：strip XML 注释、压空白，截到 256 字符。

        目的：给图谱里的 sql_preview 属性存可读 SQL 摘要，方便 LLM 问答时快速理解。
        """
        # re.sub(pattern, repl, string)：把所有匹配项替换为 repl
        # <!--[\s\S]*?-->：XML 注释，[\s\S] 匹配含换行的任意字符，*? 非贪婪
        cleaned = re.sub(r"<!--[\s\S]*?-->", "", body)
        # \s+ 匹配一个或多个空白字符（包括 \n \t 等），全部压成单个空格
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        # 字符串切片 [start:end]：end 不含，超出长度自动截断不报错
        return cleaned[:_SQL_PREVIEW_LIMIT]

    @staticmethod
    def _extract_include_refids(body: str, namespace: str) -> list[str]:
        """扫 <include refid="X"/>，返回限定后的 refid 列表。

        MyBatis <include> 规则：
        - 同 mapper 内 refid（如 "cols"）→ 限定为 "<namespace>::cols"
        - 跨 mapper refid（如 "other.namespace.Dao.cols"，含 "."）→ 限定为 "<other>...<Dao>::cols"

        为什么要限定：下游 link resolver 需要全限定名来跨文件查 entity。
        """
        out: list[str] = []
        for m in _INCLUDE_REFID_RE.finditer(body):
            # m.group(1)：refid 属性值，如 "cols" 或 "com.x.OtherDao.cols"
            refid = m.group(1)
            if "." in refid:
                # 跨 mapper 引用：如 "com.x.OtherDao.someCol" → 替换最后一个 "." 为 "::"
                # 简化策略：把所有 "." 替换为 "::"（resolver 会做二次匹配）
                out.append(refid.replace(".", "::"))
            else:
                # 同 mapper 内引用：补全为 namespace::refid
                out.append(f"{namespace}::{refid}")
        return out

    def _compute_line_starts(self) -> list[int]:
        """预算每行起始偏移，加速 _get_line_number 的行号查找。

        结果示例：source = "ab\ncd\nef"
        → starts = [0, 3, 6]
          第 1 行从 0 开始，第 2 行从 3（\n 后）开始，第 3 行从 6 开始。
        """
        # 第 1 行总从 offset=0 开始
        starts = [0]
        # enumerate(iterable) 同时产出 (索引, 值)
        for i, ch in enumerate(self.source):
            if ch == "\n":
                # 换行符之后的 offset 是下一行的起始
                starts.append(i + 1)
        return starts

    def _get_line_number(self, offset: int) -> int:
        """二分查 offset 所在行号（1-based）。

        bisect 模块说明：标准库，对有序列表做二分搜索，时间复杂度 O(log n)。
        bisect_right(a, x)：返回 x 在列表 a 中"右侧插入位置"，
        即 a[i-1] <= x < a[i] 时返回 i。
        由此：offset 落在第 idx 行（1-based）。
        """
        import bisect  # 标准库，放在函数内 import 是合法 Python 习惯（lazy import）
        # bisect_right 返回"应插入的位置"，正好等于行号（因为 _line_starts[0]=0 总存在）
        idx = bisect.bisect_right(self._line_starts, offset)
        # max(1, idx) 防御：offset=0 时 bisect_right 返回 1，正常；但极端输入用 max 保底
        return max(1, idx)
