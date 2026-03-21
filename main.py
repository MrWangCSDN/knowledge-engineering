#!/usr/bin/env python3
# 代码知识工程 — 统一入口：运行 Streamlit Web 应用
"""执行方式：python main.py"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, module="altair")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="jsonschema")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="altair.utils.schemapi")

# Pydantic v2 弃用 class-based config 的噪音警告（不影响运行）
try:
    from pydantic.warnings import PydanticDeprecatedSince20  # type: ignore

    warnings.filterwarnings("ignore", category=PydanticDeprecatedSince20)
except Exception:
    pass

import sys
import asyncio
from pathlib import Path

def main() -> None:
    def _install_asyncio_exception_filter() -> None:
        """忽略浏览器断连导致的 websocket 关闭噪音异常。"""
        try:
            import tornado.websocket  # type: ignore
            import tornado.iostream  # type: ignore
        except Exception:
            return

        loop = asyncio.get_event_loop()
        prev_handler = loop.get_exception_handler()

        def _handler(loop_obj, context):  # type: ignore[no-untyped-def]
            exc = context.get("exception")
            if isinstance(
                exc,
                (
                    tornado.websocket.WebSocketClosedError,
                    tornado.iostream.StreamClosedError,
                ),
            ):
                return
            if prev_handler is not None:
                prev_handler(loop_obj, context)
            else:
                loop_obj.default_exception_handler(context)

        loop.set_exception_handler(_handler)

    _install_asyncio_exception_filter()

    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    app_path = root / "src" / "app" / "streamlit_app.py"
    if not app_path.exists():
        print(f"未找到应用文件: {app_path}")
        sys.exit(1)

    import streamlit.web.cli as stcli
    sys.argv = [
        "streamlit", "run",
        str(app_path),
        "--server.port=8501",
        "--server.address=localhost",
        "--browser.gatherUsageStats=false",
    ]
    stcli.main()


if __name__ == "__main__":
    main()
