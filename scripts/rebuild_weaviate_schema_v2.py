"""v2.0：删 + 重建 Weaviate 3 个 collection 启用 Multi-Tenancy。

此脚本会 **永久删除** 现有的 CodeEntity / MethodInterpretation / BusinessInterpretation
三个 collection 及其全部数据，然后以启用了 Multi-Tenancy 的配置重新创建。

使用方法::

    python scripts/rebuild_weaviate_schema_v2.py --confirm

环境变量（均有默认值）::

    WEAVIATE_HOST          Weaviate 服务 IP / 域名（默认 43.228.76.163）
    WEAVIATE_PORT          HTTP 端口（默认 8080）
    WEAVIATE_GRPC_PORT     gRPC 端口（默认 50051）
    WEAVIATE_API_KEY       API Key（可选）

.. warning::
    此操作 **不可逆**。请在执行前备份数据或确认已无需保留旧数据。
"""
from __future__ import annotations

import os
import sys


# ---------------------------------------------------------------------------
# 属性定义（与 weaviate_business_store._schema_properties 对齐）
# ---------------------------------------------------------------------------

def _code_entity_props():
    """CodeEntity collection 属性定义，与 WeaviateVectorStore 内部一致。"""
    from weaviate.classes.config import Property, DataType
    return [
        Property(name="entity_id",    data_type=DataType.TEXT),   # 实体唯一 ID
        Property(name="name",         data_type=DataType.TEXT),   # 实体名称（方法/类名等）
        Property(name="entity_type",  data_type=DataType.TEXT),   # method / class / interface 等
        Property(name="location",     data_type=DataType.TEXT),   # 源文件路径
        Property(name="module_id",    data_type=DataType.TEXT),   # 所属模块/服务
        Property(name="class_name",   data_type=DataType.TEXT),   # 所属类名（方法专用）
        Property(name="code_snippet", data_type=DataType.TEXT),   # 代码片段（向量化原文）
    ]


def _method_interp_props():
    """MethodInterpretation collection 属性定义，与 WeaviateMethodInterpretStore 对齐。"""
    from weaviate.classes.config import Property, DataType
    return [
        Property(name="entity_id",      data_type=DataType.TEXT),  # 方法实体 ID
        Property(name="entity_type",    data_type=DataType.TEXT),
        Property(name="level",          data_type=DataType.TEXT),  # 解读层级
        Property(name="summary_text",   data_type=DataType.TEXT),  # LLM 生成的解读文本
        Property(name="language",       data_type=DataType.TEXT),  # zh / en
        Property(name="context_json",   data_type=DataType.TEXT),  # JSON 格式上下文
    ]


def _biz_interp_props():
    """BusinessInterpretation collection 属性定义，与 WeaviateBusinessInterpretStore 对齐。"""
    from weaviate.classes.config import Property, DataType
    return [
        Property(name="entity_id",                data_type=DataType.TEXT),
        Property(name="entity_type",              data_type=DataType.TEXT),
        Property(name="level",                    data_type=DataType.TEXT),   # class | api | module
        Property(name="business_domain",          data_type=DataType.TEXT),
        Property(name="business_capabilities",    data_type=DataType.TEXT),
        Property(name="summary_text",             data_type=DataType.TEXT),
        Property(name="language",                 data_type=DataType.TEXT),
        Property(name="context_json",             data_type=DataType.TEXT),
        Property(name="related_entity_ids_json",  data_type=DataType.TEXT),
    ]


# ---------------------------------------------------------------------------
# 重建逻辑
# ---------------------------------------------------------------------------

_COLLECTIONS = [
    ("CodeEntity",             _code_entity_props),
    ("MethodInterpretation",   _method_interp_props),
    ("BusinessInterpretation", _biz_interp_props),
]


def main() -> None:  # noqa: C901
    """主入口：解析 --confirm 参数，连接 Weaviate，删除并重建 3 个 collection。"""
    # --confirm 保护：防止误运行
    if "--confirm" not in sys.argv:
        print("WARNING: 此脚本将永久删除并重建 Weaviate collection！")
        print("  被删除的 collection：CodeEntity / MethodInterpretation / BusinessInterpretation")
        print("  所有数据将丢失，无法恢复。")
        print("")
        print("若已确认并希望继续，请加上 --confirm 参数重新运行：")
        print("  python scripts/rebuild_weaviate_schema_v2.py --confirm")
        sys.exit(1)

    import weaviate
    from weaviate.classes.init import Auth
    from weaviate.classes.config import Configure

    # 从环境变量读取连接参数（有默认值，方便本地调试）
    host     = os.environ.get("WEAVIATE_HOST",      "43.228.76.163")
    port     = int(os.environ.get("WEAVIATE_PORT",  "8080"))
    grpc_port = int(os.environ.get("WEAVIATE_GRPC_PORT", "50051"))
    api_key  = os.environ.get("WEAVIATE_API_KEY",   "")

    print(f"连接 Weaviate: http://{host}:{port}  gRPC port={grpc_port}")

    # 根据是否有 API Key 选择鉴权方式
    auth = Auth.api_key(api_key) if api_key else None

    # 使用 weaviate-client v4 的 connect_to_custom 接口
    client = weaviate.connect_to_custom(
        http_host=host,
        http_port=port,
        http_secure=False,
        grpc_host=host,
        grpc_port=grpc_port,
        grpc_secure=False,
        auth_credentials=auth,
    )

    try:
        # 1. 删除旧 collection
        for name, _ in _COLLECTIONS:
            if client.collections.exists(name):
                print(f"  正在删除 collection: {name} …")
                client.collections.delete(name)
                print(f"  已删除: {name}")
            else:
                print(f"  collection 不存在，跳过删除: {name}")

        # 2. 重建带 Multi-Tenancy 的 collection
        for name, props_fn in _COLLECTIONS:
            print(f"  正在创建 collection: {name}（Multi-Tenancy 已启用）…")
            client.collections.create(
                name=name,
                # v2.0 Multi-Tenancy 配置：
                #   auto_tenant_creation  —— 写入时自动创建不存在的 tenant（无需预先 create_tenant）
                #   auto_tenant_activation —— 自动激活非 ACTIVE 的 tenant（如 COLD 状态）
                multi_tenancy_config=Configure.multi_tenancy(
                    enabled=True,
                    auto_tenant_creation=True,
                    auto_tenant_activation=True,
                ),
                properties=props_fn(),
            )
            print(f"  已创建: {name}")

        print("")
        print("Schema 重建完成。现在每个 collection 均已启用 Multi-Tenancy。")
        print("后续写入请通过 .with_tenant(<project_id>) 访问对应租户视图。")
    finally:
        # 确保连接被关闭，避免 gRPC 通道泄漏
        client.close()


if __name__ == "__main__":
    main()
