# 不移植清单（py-final-baseline → ke-server）

> 终态依据：Obsidian spec《TS重构-总体路线-设计》§八。本文档记录 Phase 0 验证结论与证据。

| 项 | 验证结论 | 证据 |
|---|---|---|
| `src/ir/`（整层） | 运行时零引用（src 其余模块 + scripts + tests 均无 import）；`src/ir/` 本身在基线中从未入库（`.gitignore` 排除，py-final-baseline 及 main 树中均无此路径） | 本文档 §验证记录 |
| 6 段式 synthesizer 全链路 | 决策 #6 agent-only；main 独有 3 commit（daef206/f80c16f/a21d9d4）内容未进基线 | Task 1 合并记录（merge commit 7e930f4） |
| `stub_retriever.py` | dev 桩，TS 侧按需重建 | — |
| Streamlit UI / OWL 推理 | 基线树中已不存在 | Task 1 验证（ls-tree 零命中） |
| `KE_QA_USE_REACT` 开关 | TS 版恒为 agent 路径，开关消失 | 决策 #6 |

## src/ir 规模（本地工作树，仅供参考）

- Python 文件数：**17 个**（含 `engine/` 子包 5 个）
- Python 代码总行数：**3052 行**
- 模块列表：`__init__`, `container`, `edges`, `enums`, `errors`, `layer`, `metadata`, `nodes`, `profile`, `quality`, `spi`, `engine/{__init__, adapter_generator, code_scanner, pipeline, prompt_templates, registry}`

> 即便从未入库，以上数字代表 TS 侧**不需要移植的工作量下界**：3 k 行 IR 层全部跳过。

## 验证记录（2026-06-11）

### 命令 1：git grep 运行时引用（基线标签，排除 src/ir 和 tests）

```
$ git grep -nE "from src\.ir|import src\.ir|from src import ir" py-final-baseline -- ':!src/ir' ':!tests'
（空输出）
runtime refs exit=1
```

### 命令 2：git grep tests 引用（基线标签）

```
$ git grep -lE "from src\.ir|import src\.ir" py-final-baseline -- 'tests'
（空输出）
test refs exit=1
```

> 注：py-final-baseline 树中 `src/ir/` 路径不存在（`.gitignore` 排除，从未入库），因此 git grep 在任何范围均无命中，包括 tests。

### 命令 3：动态引用防检（importlib）

```
$ git grep -nE "importlib.*['\"](src\.ir)|['\"]src\.ir['\"]" py-final-baseline -- ':!src/ir' ':!tests'
（空输出）
dynamic refs exit=1
```

### 附加：工作树双重验证

```
$ grep -rn "from src\.ir|import src\.ir|from src import ir" src/ --include="*.py" --exclude-dir=ir
（空输出）
working-tree runtime refs exit=1

$ grep -rn "from src\.ir|import src\.ir" tests/ --include="*.py"
（空输出）
working-tree test refs exit=1
```

**结论：`src/ir/` 整层在基线快照中从未入库，在工作树中亦无任何运行时或测试引用，Phase 0 终验通过。**
