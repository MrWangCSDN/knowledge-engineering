"""Root conftest: ensure project root is on sys.path so `from src.service import ...` works."""
import sys
import pathlib

# 将项目根目录加入 sys.path，使 `from src.xxx import yyy` 风格的导入可以正常工作
root = str(pathlib.Path(__file__).parent)
if root not in sys.path:
    sys.path.insert(0, root)
