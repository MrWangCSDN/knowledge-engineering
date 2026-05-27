"""EmbeddingCheckpoint 单测 — 双层 cache + 项目目录持久化 + model 失效。

设计：[[DashScope-Embedding-替换-Ollama-设计]] §4.3 + §5.1bis
"""
# json：标准库，序列化 / 反序列化 checkpoint 文件
import json
# Path：pathlib 提供的面向对象文件路径 API，比 os.path 更现代
from pathlib import Path
# MagicMock：unittest.mock 提供的"什么属性都能调"的假对象，用于 stub Weaviate store
from unittest.mock import MagicMock

import pytest

# 待测模块：第一次跑会 ImportError（RED 阶段）
from src.semantic.embedding_checkpoint import EmbeddingCheckpoint, _resolve_checkpoint_dir


def test_resolve_checkpoint_dir_creates_data_checkpoints(tmp_path, monkeypatch):
    """_resolve_checkpoint_dir 自动创建 data/checkpoints/ 目录。

    pytest 内置 fixtures:
      - tmp_path：每个用例独立的临时目录（Path 对象）
      - monkeypatch：测试结束自动还原 env / chdir / attr
    """
    # 模拟 cwd 在含 src/ 的目录（_resolve_checkpoint_dir 用 src/ 作锚点判断 auth 根）
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    target = _resolve_checkpoint_dir()
    assert target == tmp_path / "data/checkpoints"
    # `Path.is_dir()` — 目录存在且确实是目录
    assert target.is_dir()


def test_load_no_file_returns_empty(tmp_path, monkeypatch):
    """文件不存在 → 返空 checkpoint。"""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    ckpt = EmbeddingCheckpoint.load("proj-a")
    # `has(any)` 在空 checkpoint 上应该返 False
    assert ckpt.has("any_id") is False


def test_load_with_force_full_returns_empty_and_deletes_file(tmp_path, monkeypatch):
    """force_full=True：即便有文件也返空，且删 disk 文件。"""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)

    # 先建一个 checkpoint 文件（模拟上次跑留下的）
    ckpt_dir = tmp_path / "data/checkpoints"
    # `parents=True` 等价于 `mkdir -p`，缺中间目录自动建
    ckpt_dir.mkdir(parents=True)
    ckpt_path = ckpt_dir / "proj-a_embedding_checkpoint.json"
    # `Path.write_text` 一次性写字符串（自动 utf-8 encode）
    ckpt_path.write_text(json.dumps({
        "project_id": "proj-a",
        "model": "text-embedding-v4",
        "completed_entity_ids": ["x"],
    }))

    ckpt = EmbeddingCheckpoint.load("proj-a", force_full=True)
    assert ckpt.has("x") is False  # 内存为空
    assert not ckpt_path.exists()  # 文件被删


def test_load_existing_file(tmp_path, monkeypatch):
    """文件已存在且 model 匹配 → 加载 completed_entity_ids。"""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)

    ckpt_dir = tmp_path / "data/checkpoints"
    ckpt_dir.mkdir(parents=True)
    (ckpt_dir / "proj-a_embedding_checkpoint.json").write_text(json.dumps({
        "project_id": "proj-a",
        "model": "text-embedding-v4",
        "completed_entity_ids": ["a", "b", "c"],
    }))

    ckpt = EmbeddingCheckpoint.load("proj-a")
    # 3 个 id 命中，1 个未命中
    assert ckpt.has("a") is True
    assert ckpt.has("b") is True
    assert ckpt.has("c") is True
    assert ckpt.has("d") is False


def test_load_model_mismatch_invalidates(tmp_path, monkeypatch):
    """checkpoint 里 model 不匹配（旧 ollama）→ 视作 invalid 返空。"""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)

    ckpt_dir = tmp_path / "data/checkpoints"
    ckpt_dir.mkdir(parents=True)
    (ckpt_dir / "proj-a_embedding_checkpoint.json").write_text(json.dumps({
        "project_id": "proj-a",
        "model": "bge-m3",   # 旧的，不匹配 v4
        "completed_entity_ids": ["a", "b"],
    }))

    ckpt = EmbeddingCheckpoint.load("proj-a", model="text-embedding-v4")
    assert ckpt.has("a") is False  # 旧的不算


def test_load_corrupted_file_returns_empty(tmp_path, monkeypatch):
    """文件 JSON 损坏 → 视作不存在，返空（不抛）。"""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)

    ckpt_dir = tmp_path / "data/checkpoints"
    ckpt_dir.mkdir(parents=True)
    # 写入非法 JSON：json.load 会抛 JSONDecodeError；load() 应吃掉返空
    (ckpt_dir / "proj-a_embedding_checkpoint.json").write_text("{ invalid json")

    ckpt = EmbeddingCheckpoint.load("proj-a")
    assert ckpt.has("any") is False


def test_has_set_hit_after_mark_done(tmp_path, monkeypatch):
    """mark_done(X) → has(X) 立即 True。"""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)

    ckpt = EmbeddingCheckpoint.load("proj-a")
    ckpt.mark_done("entity-x")
    assert ckpt.has("entity-x") is True


def test_has_weaviate_fallback_hit(tmp_path, monkeypatch):
    """文件 / 内存没 X，weaviate_store.exists 返 True → has(X) 回填 set + 返 True。"""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)

    # MagicMock 默认所有方法返 MagicMock；用 return_value 显式控制
    fake_store = MagicMock()
    fake_store.exists = MagicMock(return_value=True)

    ckpt = EmbeddingCheckpoint.load("proj-a", weaviate_store=fake_store)
    assert ckpt.has("xx") is True
    # `assert_called_once_with(args...)` 校验 mock 被调用次数 + 参数
    fake_store.exists.assert_called_once_with("proj-a", "xx")
    # 第二次 has 走内存 cache，不再调 Weaviate
    fake_store.exists.reset_mock()
    assert ckpt.has("xx") is True
    fake_store.exists.assert_not_called()


def test_has_many_batch_fallback(tmp_path, monkeypatch):
    """5 个 entity_id，2 个内存命中 3 个查 Weaviate；调用 exists_many 仅 3 个 unknown。"""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)

    fake_store = MagicMock()
    # exists_many 返回 unknown 中真存在的子集（仅 "c"）
    fake_store.exists_many = MagicMock(return_value={"c"})

    ckpt = EmbeddingCheckpoint.load("proj-a", weaviate_store=fake_store)
    ckpt.mark_done("a")
    ckpt.mark_done("b")

    result = ckpt.has_many(["a", "b", "c", "d", "e"])
    # a/b 内存命中；c 是 Weaviate fallback 命中；d/e 双层都没
    assert result == {"a": True, "b": True, "c": True, "d": False, "e": False}
    # exists_many 只查 unknown 3 个
    fake_store.exists_many.assert_called_once_with("proj-a", ["c", "d", "e"])


def test_flush_writes_atomically(tmp_path, monkeypatch):
    """flush → .tmp 写 + rename；JSON 含 4 个字段。"""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)

    ckpt = EmbeddingCheckpoint.load("proj-a")
    ckpt.mark_done("a")
    ckpt.mark_done("b")
    ckpt.flush()

    path = tmp_path / "data/checkpoints/proj-a_embedding_checkpoint.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["project_id"] == "proj-a"
    assert data["model"] == "text-embedding-v4"
    # `sorted(...)` 保证比较稳定（set 转 list 顺序不定）
    assert sorted(data["completed_entity_ids"]) == ["a", "b"]
    # ISO-8601 timestamp 由 datetime.now(...).isoformat() 生成
    assert "updated_at" in data


def test_flush_empty_no_op(tmp_path, monkeypatch):
    """没有 pending 时 flush 不写盘（节省 I/O）。"""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)

    ckpt = EmbeddingCheckpoint.load("proj-a")
    ckpt.flush()  # 没 mark_done 过
    assert not (tmp_path / "data/checkpoints/proj-a_embedding_checkpoint.json").exists()
