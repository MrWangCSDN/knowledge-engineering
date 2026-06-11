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
# （pytest 由 pyproject.toml pythonpath=["src"] 处理，脚本需手动处理）
_ROOT = Path(__file__).resolve().parent.parent   # scripts/ → 项目根
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# import app 前兜底 env：api.py 自带 dotenv 加载，但裸环境跑脚本时 KE_DB_URL 可能为空
os.environ.setdefault("KE_DB_URL", "mysql+asyncmy://x:x@127.0.0.1:3306/x")

from src.service.api import app  # noqa: E402


def main() -> None:
    out_dir = Path("docs/porting")
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
