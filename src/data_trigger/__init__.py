"""数据与触发层：代码库接入、全量/增量/按需触发，输出 CodeInputSource。"""
from .loader import load_code_source

__all__ = ["load_code_source"]
