"""
Weaviate 连接与 collection 名的代码层默认值（与 YAML 中 Field 默认保持一致）。

实际运行以配置文件为准；此处仅在缺省或 dict 透传时使用。
"""
from __future__ import annotations

DEFAULT_WEAVIATE_HTTP_URL = "http://localhost:8080"
DEFAULT_WEAVIATE_GRPC_PORT = 50051

DEFAULT_COLLECTION_CODE_ENTITY = "CodeEntity"
DEFAULT_COLLECTION_METHOD_INTERPRETATION = "MethodInterpretation"
DEFAULT_COLLECTION_BUSINESS_INTERPRETATION = "BusinessInterpretation"
DEFAULT_COLLECTION_PATTERN_INTERPRETATION = "PatternInterpretation"
