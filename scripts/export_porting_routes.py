"""导出 FastAPI 全量 OpenAPI schema + 人读路由摘要表 — TS 移植对照物①。

跑法（KE_DB_URL 只需占位，import 期不连库）：
    KE_DB_URL='mysql+asyncmy://x:x@127.0.0.1:3306/x' venv/bin/python scripts/export_porting_routes.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 将项目根目录加入 sys.path，使 `from src.xxx` 形式的导入在脚本直接跑时也能找到模块
# （pytest 由根目录 conftest.py 把项目根插入 sys.path 来支持 `from src.xxx`；脚本直接跑时
#   没有 pytest 介入，需在这里手动插入，效果与 conftest.py 的 sys.path.insert 等价）
_ROOT = Path(__file__).resolve().parent.parent   # scripts/ → 项目根
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# import app 前兜底 env：api.py 自带 dotenv 加载，但裸环境跑脚本时 KE_DB_URL 可能为空
os.environ.setdefault("KE_DB_URL", "mysql+asyncmy://x:x@127.0.0.1:3306/x")

from src.service.api import app  # noqa: E402


def main() -> None:
    """导出 FastAPI 全量路由为两份产物：

    - docs/porting/routes-openapi.json  全量 OpenAPI schema（JSON）
    - docs/porting/routes-summary.md    人读路由摘要表（Markdown 表格）

    无参数，无返回值。产物路径锚定项目根，重复运行内容确定性，git diff 为空。
    """
    # 锚定项目根，确保无论在哪个目录执行脚本，产物路径都一致
    out_dir = _ROOT / "docs" / "porting"
    out_dir.mkdir(parents=True, exist_ok=True)

    schema = app.openapi()
    (out_dir / "routes-openapi.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 人读摘要表：method / path / summary
    lines = [
        "# 路由清单（自动生成，勿手改）",
        "",
        f"来源：tag `py-final-baseline`，共 {len(schema['paths'])} 个 path。",
        "TS 移植以本表盘点覆盖率；6 段式专属路由按决策 #6 标记不迁。",
        "",
        "| Method | Path | Summary |",
        "|---|---|---|",
    ]
    for path, methods in sorted(schema["paths"].items()):
        for method, op in sorted(methods.items()):
            summary = (op.get("summary") or "").replace("|", "\\|")
            lines.append(f"| {method.upper()} | `{path}` | {summary} |")
    (out_dir / "routes-summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"✅ {len(schema['paths'])} paths → docs/porting/routes-openapi.json + routes-summary.md")


if __name__ == "__main__":
    main()
