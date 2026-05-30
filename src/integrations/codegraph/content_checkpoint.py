# src/integrations/codegraph/content_checkpoint.py
"""内容感知 checkpoint：记 {durable_key: code_snippet 的 hash}。

与现有 id-only EmbeddingCheckpoint 的区别：它能发现"key 没变但代码改了"，
从而正确触发重 embed；还能算出孤儿 key（CodeGraph 里已没有的）用于删库。
"""
from __future__ import annotations  # PEP 563：允许注解里引用未定义名称（如 set[str]）

import hashlib  # 标准库：提供 sha256 等哈希算法，用于生成代码片段的指纹
import json     # 标准库：JSON 序列化/反序列化，用于把 dict 持久化到磁盘
import os       # 标准库：文件系统操作（路径判断 / 原子重命名）


def code_hash(snippet: str) -> str:
    """计算代码片段的 sha256 指纹（截断 16 位），用于快速比对内容是否变化。

    Args:
        snippet: 源码文本字符串
    Returns:
        16 字符的十六进制 hash 字符串（足够区分，比存全文节省磁盘）
    """
    # encode("utf-8")：str → bytes（hashlib 只接受 bytes）
    # hexdigest()：把 bytes 哈希值转成十六进制字符串（64 位）
    # [:16]：截取前 16 位；碰撞概率约 1/(16^16) ≈ 1e-19，实际场景可接受
    return hashlib.sha256(snippet.encode("utf-8")).hexdigest()[:16]


class ContentCheckpoint:
    """{durable_key: code_hash} 的本地持久化。

    职责：
    1. changed()  — 判断某个方法的代码是否自上次 embed 后变化了
    2. mark()     — 记录某方法当前内容 hash（embed 成功后调）
    3. orphans()  — 返回已记录但当前 CodeGraph 里不存在的 key（方法删/改名）
    4. drop()     — 从 checkpoint 移除某 key（删库后调）
    5. flush()    — 原子写盘（先写临时文件再 rename，防断电损坏）
    """

    def __init__(self, path: str) -> None:
        """初始化 checkpoint，若文件已存在则加载旧记录。

        Args:
            path: checkpoint JSON 文件路径（不存在会在 flush() 时创建）
        """
        self._path = path                           # 持久化文件路径
        self._map: dict[str, str] = {}              # {durable_key: code_hash} 内存字典
        # os.path.exists：判断文件是否存在；有旧文件才加载
        if os.path.exists(path):
            try:
                # open(...) 打开文件；with 语句确保文件句柄在离开块时自动关闭（上下文管理器）
                with open(path, "r", encoding="utf-8") as fh:
                    # json.load(fh) 从文件对象读取 JSON → Python dict
                    self._map = json.load(fh)
            except (OSError, ValueError):
                # OSError：文件权限/IO 错；ValueError：JSON 格式损坏
                # 任何加载失败都当作空 checkpoint，全量重来，不阻断启动
                self._map = {}

    def changed(self, key: str, snippet: str) -> bool:
        """判断该 key 的代码是否变了（或全新）→ 需要重 embed。

        Args:
            key:     durable_key（如 "com.example.A::method#(String)"）
            snippet: 该方法当前源码片段
        Returns:
            True = 需要重 embed；False = 内容未变，可跳过
        """
        # dict.get(key) 返回值或 None；与新 hash 比较
        # 若 key 不在 map 中（新方法），get 返回 None，必然 != hash，正确触发 embed
        return self._map.get(key) != code_hash(snippet)

    def mark(self, key: str, snippet: str) -> None:
        """记下该 key 当前内容 hash（embed 成功后调）。

        Args:
            key:     durable_key
            snippet: 当前源码片段（用于计算 hash 后存入）
        """
        # 赋值到内存 dict；需在调用 flush() 后才落盘
        self._map[key] = code_hash(snippet)

    def orphans(self, current_keys: set[str]) -> set[str]:
        """返回已记录但当前 CodeGraph 里不存在的 key（方法删/改名 → 旧向量该删）。

        Args:
            current_keys: 当前 CodeGraph 中所有方法的 durable_key 集合
        Returns:
            孤儿 key 集合（set 差运算：已记录 - 当前存在）
        """
        # set(self._map.keys()) 把 dict_keys 转成集合
        # - 运算符 = 集合差：左边有、右边没有的元素
        return set(self._map.keys()) - current_keys

    def drop(self, key: str) -> None:
        """从 checkpoint 移除某 key（删库后调，避免下次又算成孤儿）。

        Args:
            key: 要移除的 durable_key
        """
        # dict.pop(key, None)：存在则删，不存在则返回 None（不抛 KeyError）
        self._map.pop(key, None)

    def flush(self) -> None:
        """将内存 dict 原子写入磁盘（先写临时文件，再 os.replace 重命名）。

        原子替换原理：os.replace 在 POSIX 系统上是原子操作，
        即使在写入过程中断电，也不会出现半写文件——旧文件要么完整保留，要么被新文件完整替换。
        """
        tmp = self._path + ".tmp"                   # 临时文件路径（同目录，保证在同一文件系统）
        # open(..., "w") 写模式：文件不存在时创建，存在时清空
        with open(tmp, "w", encoding="utf-8") as fh:
            # json.dump：把 dict 序列化成 JSON 字符串写入文件
            # ensure_ascii=False：允许中文等非 ASCII 字符直接写入，而非转成 \uXXXX
            json.dump(self._map, fh, ensure_ascii=False)
        # os.replace：原子地将 tmp 重命名为 self._path；POSIX 保证原子性
        os.replace(tmp, self._path)
