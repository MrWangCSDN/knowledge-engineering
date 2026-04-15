"""JavaParser Bridge: 通过 subprocess 调用 Java JAR 解析 Java 代码。

支持 Java 1-25+ 语法（record, sealed, text block, switch expression, pattern matching）。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from src.models.structure import StructureFacts
from src.models.code_source import CodeInputSource

_LOG = logging.getLogger(__name__)

# JAR 文件相对于项目根目录的路径
_JAR_RELATIVE_PATH = "javaparser-bridge/target/javaparser-bridge-1.0.0-shaded.jar"


def is_javaparser_available() -> bool:
    """检查 JavaParser Bridge 是否可用（JDK + JAR 都存在）。"""
    if not shutil.which("java"):
        return False
    jar = _find_bridge_jar()
    return jar is not None and jar.exists()


def _find_bridge_jar() -> Optional[Path]:
    """查找 Bridge JAR 文件。"""
    # 从当前工作目录查找
    cwd_jar = Path.cwd() / _JAR_RELATIVE_PATH
    if cwd_jar.exists():
        return cwd_jar

    # 从本文件位置回溯查找
    src_root = Path(__file__).resolve().parents[2]  # knowledge-engineering/
    jar = src_root / _JAR_RELATIVE_PATH
    if jar.exists():
        return jar

    return None


def _write_modules_json(source: CodeInputSource, tmp_dir: Path) -> Path:
    """将 CodeInputSource 转为 JavaParser Bridge 的模块配置 JSON。"""
    config = {
        "repo_path": source.repo_path,
        "modules": [
            {
                "id": m.id,
                "name": m.name or m.id,
                "path": m.path or m.id,
                "business_domains": m.business_domains or [],
            }
            for m in source.modules
        ],
        "file_module_map": {f.path: f.module_id for f in source.files},
        "extract_cross_service": True,
    }
    json_path = tmp_dir / "modules.json"
    json_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return json_path


def _stream_progress(
    stderr_pipe,
    progress_callback: Optional[Callable[[int, int, str], None]],
):
    """后台线程：从 Java 进程的 stderr 读取 NDJSON 进度，转发给 Python 回调。"""
    if stderr_pipe is None:
        return
    try:
        for line in stderr_pipe:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                msg_type = data.get("type", "")
                if msg_type == "progress" and progress_callback:
                    progress_callback(
                        data.get("current", 0),
                        data.get("total", 0),
                        data.get("message", ""),
                    )
                elif msg_type == "file_error":
                    _LOG.warning(
                        "JavaParser file error: %s: %s",
                        data.get("file", ""),
                        data.get("error", ""),
                    )
                elif msg_type == "done":
                    _LOG.info(
                        "JavaParser done: %d entities, %d relations, %d errors",
                        data.get("entities", 0),
                        data.get("relations", 0),
                        data.get("errors", 0),
                    )
            except json.JSONDecodeError:
                _LOG.debug("JavaParser stderr (non-JSON): %s", line)
    except Exception as e:
        _LOG.debug("JavaParser stderr reader error: %s", e)


def run_javaparser_bridge(
    source: CodeInputSource,
    extract_cross_service: bool = True,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    java_cmd: str = "java",
    jvm_args: Optional[list[str]] = None,
    timeout_seconds: int = 600,
) -> StructureFacts:
    """
    调用 JavaParser Bridge JAR 解析 Java 代码。

    Args:
        source: 代码输入源
        extract_cross_service: 是否提取跨服务边
        progress_callback: 进度回调 (current, total, message)
        java_cmd: Java 命令路径
        jvm_args: JVM 参数 (默认 ["-Xmx2g"])
        timeout_seconds: 超时秒数

    Returns:
        StructureFacts: 解析结果

    Raises:
        FileNotFoundError: JAR 文件不存在
        RuntimeError: Java 进程失败
    """
    jar_path = _find_bridge_jar()
    if not jar_path:
        raise FileNotFoundError(
            f"JavaParser Bridge JAR not found. Run: cd javaparser-bridge && mvn clean package -DskipTests"
        )

    if jvm_args is None:
        jvm_args = ["-Xmx2g"]

    with tempfile.TemporaryDirectory(prefix="javaparser_bridge_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        modules_json = _write_modules_json(source, tmp_path)
        output_file = tmp_path / "structure_facts.json"

        # 构建命令
        cmd = [
            java_cmd,
            *jvm_args,
            "-jar", str(jar_path),
            "--repo-path", source.repo_path,
            "--modules-json", str(modules_json),
            "--output", str(output_file),
        ]

        _LOG.info("Running JavaParser Bridge: %s", " ".join(cmd))

        # 启动 Java 进程
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        # 后台线程读取 stderr 进度
        progress_thread = threading.Thread(
            target=_stream_progress,
            args=(proc.stderr, progress_callback),
            daemon=True,
        )
        progress_thread.start()

        # 等待完成
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError(
                f"JavaParser Bridge timed out after {timeout_seconds}s"
            )

        progress_thread.join(timeout=5)

        # 检查退出码
        if proc.returncode != 0:
            # 读取剩余 stderr
            remaining = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(
                f"JavaParser Bridge failed (exit code {proc.returncode}): {remaining[-500:]}"
            )

        # 解析输出 JSON
        if not output_file.exists():
            raise RuntimeError("JavaParser Bridge did not produce output file")

        json_text = output_file.read_text(encoding="utf-8")
        facts = StructureFacts.model_validate_json(json_text)

        _LOG.info(
            "JavaParser Bridge: %d entities, %d relations",
            len(facts.entities),
            len(facts.relations),
        )
        return facts
