"""Git 命令辅助 — v1.0 只用 ls-remote 做连接测试。

设计文档：[[仓库管理-设计]] §6

为啥不用 GitPython / dulwich：
  - ls-remote 是只读操作，无需完整 Git 客户端
  - subprocess git 最简单可靠，全平台兼容
  - 实际 clone（v1.1 才做）也用 subprocess git，保持一致

安全：
  - 拼好的 auth_url 永远不进 log
  - stderr 解析后 mask 任何可能含 token 的串
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse, quote


@dataclass
class LsRemoteResult:
    ok: bool
    default_branch: Optional[str] = None
    last_commit: Optional[str] = None
    error: Optional[str] = None


def inject_token_to_https(url: str, token: str | None) -> str:
    """把 PAT 注入 HTTPS URL：https://x-token-auth:TOKEN@host/path

    GitHub / GitLab / Gitea 都支持这种 basic auth 格式。
    user 部分用 'x-token-auth' 是常见约定（也可以用 'oauth2'），实际被 forge 忽略。

    Args:
        url: 原始 URL，可能是 https://github.com/x/y 或 git@host:x/y.git
        token: 明文 PAT；为空时不修改 URL

    Returns:
        带 auth 的 URL；若是 SSH URL（git@...）原样返回（v1.0 不支持 SSH 凭证）
    """
    if not token:
        return url

    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        # SSH url 或非标准协议：v1.0 不注入凭证
        return url

    if parsed.username:
        # 已经带 auth，不重复注入
        return url

    # netloc 形如 'host:port'
    auth = f"x-token-auth:{quote(token, safe='')}"
    new_netloc = f"{auth}@{parsed.netloc}"
    return urlunparse(parsed._replace(netloc=new_netloc))


def mask_token_in_text(text: str, token: str | None) -> str:
    """从输出（stderr）里抹掉明文 token 防止日志泄露。"""
    if not token or not text:
        return text
    return text.replace(token, "***").replace(quote(token, safe=""), "***")


# 解析 ls-remote --symref 输出：
#   ref: refs/heads/main\tHEAD
#   abc123def456...   HEAD
_REF_LINE = re.compile(r"^ref:\s+refs/heads/(\S+)\s+HEAD")
_HEAD_HASH_LINE = re.compile(r"^([a-f0-9]{40})\s+HEAD")


def parse_ls_remote(output: str) -> tuple[Optional[str], Optional[str]]:
    """从 git ls-remote --symref <url> HEAD 的输出中解析默认分支 + HEAD commit。

    Returns:
        (default_branch, head_commit)；任一无法解析则返 None。
    """
    default_branch: Optional[str] = None
    head_commit: Optional[str] = None
    for line in output.splitlines():
        if m := _REF_LINE.match(line):
            default_branch = m.group(1)
        elif m := _HEAD_HASH_LINE.match(line):
            head_commit = m.group(1)
    return default_branch, head_commit


async def ls_remote(
    url: str,
    *,
    token: str | None = None,
    timeout: float = 15.0,
) -> LsRemoteResult:
    """异步执行 git ls-remote --symref <url> HEAD。

    成功条件：returncode == 0 且能解析出 commit hash。

    超时控制：默认 15 秒。慢仓库可能要更久但作为"测试连接"15s 足够。
    """
    auth_url = inject_token_to_https(url, token)

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "ls-remote", "--symref", auth_url, "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # 防止 git 弹出交互密码框
            env={"GIT_TERMINAL_PROMPT": "0", "PATH": _safe_path()},
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        return LsRemoteResult(ok=False, error=f"连接超时（>{timeout}s），请检查 URL 和网络")
    except FileNotFoundError:
        return LsRemoteResult(ok=False, error="服务器未安装 git 命令")

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = mask_token_in_text(
        stderr_bytes.decode("utf-8", errors="replace"), token
    )

    if proc.returncode != 0:
        # 常见错误：无权限 / 仓库不存在 / 域名不通
        # 截断到 500 字防止前端被巨长错误轰炸
        return LsRemoteResult(ok=False, error=stderr.strip()[:500] or "git 失败")

    branch, commit = parse_ls_remote(stdout)
    if commit is None:
        return LsRemoteResult(
            ok=False,
            error="git 返回成功但解析不到 HEAD commit；仓库可能为空"
        )

    return LsRemoteResult(ok=True, default_branch=branch, last_commit=commit)


def _safe_path() -> str:
    """保留必要的 PATH 让 git 可执行，但不暴露完整 env。"""
    import os
    return os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
