"""GitHubAppProvider：RS256 JWT 换 1h installation token（内存缓存），httpx 调 REST。
设计 §6/§7。token 不落库，缓存到接近过期重取。"""
from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from jose import jwt

from src.service.scm.config import GitHubAppConfig

_API = "https://api.github.com"
_JWT_TTL = 540          # App JWT 9 分钟（GitHub 上限 10min）
_TOKEN_SKEW = 60        # installation token 提前 60s 视为过期


class GitHubAppProvider:
    def __init__(self, cfg: GitHubAppConfig):
        self._cfg = cfg
        self._tok_cache: dict[int, tuple[str, float]] = {}

    def _app_jwt(self) -> str:
        """用 App 私钥签 RS256 JWT（iss=app_id），用于换 installation token。"""
        now = int(time.time())
        payload = {"iat": now - 30, "exp": now + _JWT_TTL, "iss": self._cfg.app_id}
        return jwt.encode(payload, self._cfg.private_key_pem, algorithm="RS256")

    async def get_installation_token(self, installation_id: int) -> str:
        """换 1h installation token；缓存命中且未近过期则复用。"""
        cached = self._tok_cache.get(installation_id)
        if cached and cached[1] - _TOKEN_SKEW > time.time():
            return cached[0]
        headers = {
            "Authorization": f"Bearer {self._app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_API}/app/installations/{installation_id}/access_tokens", headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
        token = data["token"]
        exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        self._tok_cache[installation_id] = (token, exp)
        return token

    async def _get(self, installation_id: int, path: str) -> httpx.Response:
        """用 installation token 发起 GET 请求，返回原始 httpx.Response。"""
        token = await self.get_installation_token(installation_id)
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{_API}{path}", headers=headers)
            resp.raise_for_status()
            return resp

    async def list_repos(self, installation_id: int) -> list["RepoInfo"]:
        """列出该 installation 下所有可见仓库，分页自动翻页（per_page=100）。"""
        from src.service.scm.base import RepoInfo
        out: list[RepoInfo] = []
        page = 1
        while True:
            resp = await self._get(installation_id, f"/installation/repositories?per_page=100&page={page}")
            repos = resp.json().get("repositories", [])
            if not repos:
                break
            for r in repos:
                out.append(RepoInfo(
                    external_id=r["id"], full_name=r["full_name"],
                    default_branch=r.get("default_branch", "main"), private=bool(r.get("private")),
                ))
            if len(repos) < 100:
                break
            page += 1
        return out

    async def list_branches(self, installation_id: int, full_name: str) -> "BranchList":
        """列出仓库所有分支并返回默认分支，分页自动翻页（per_page=100）。"""
        from src.service.scm.base import BranchList
        meta = (await self._get(installation_id, f"/repos/{full_name}")).json()
        default_branch = meta.get("default_branch", "main")
        branches: list[str] = []
        page = 1
        while True:
            resp = await self._get(installation_id, f"/repos/{full_name}/branches?per_page=100&page={page}")
            items = resp.json()
            if not items:
                break
            branches.extend(b["name"] for b in items)
            if len(items) < 100:
                break
            page += 1
        return BranchList(default_branch=default_branch, branches=branches)

    async def clone(self, installation_id: int, full_name: str, ref: str,
                    subpath: Optional[str], dest: str) -> str:
        """浅克隆指定仓库分支到 dest，返回 HEAD commit sha（40 hex）。"""
        token = await self.get_installation_token(installation_id)
        url = f"https://github.com/{full_name}.git"
        return await shallow_clone(url, ref=ref, dest=dest, token=token, subpath=subpath)


# ---------------------------------------------------------------------------
# 模块级工具函数（可单独单测，不依赖 class 实例）
# ---------------------------------------------------------------------------

def mask_token(text: str, token: Optional[str]) -> str:
    """将 token 字符串从日志/错误信息中替换为 ***，避免泄露。"""
    if not token or not text:
        return text
    return text.replace(token, "***")


def build_clone_args(clone_url: str, ref: str, dest: str) -> list[str]:
    """构造浅克隆命令（单分支）。subpath 的 sparse-checkout 在 shallow_clone 内 clone 后单独执行。"""
    return ["git", "clone", "--depth", "1", "--branch", ref, "--single-branch", clone_url, dest]


def _inject_token(clone_url_https: str, token: Optional[str]) -> str:
    """将 installation token 注入 HTTPS URL，形如 x-access-token:<token>@github.com/…。"""
    if not token:
        return clone_url_https
    return clone_url_https.replace("https://", f"https://x-access-token:{token}@", 1)


async def _run(args: list[str], token: Optional[str], cwd: Optional[str] = None) -> str:
    """运行子进程命令，返回 stdout 字符串；失败时抛出 RuntimeError（token 已掩码）。"""
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(mask_token(err.decode("utf-8", "replace"), token))
    return out.decode("utf-8", "replace").strip()


async def shallow_clone(clone_url_https: str, ref: str, dest: str,
                        token: Optional[str], subpath: Optional[str] = None) -> str:
    """浅克隆指定分支到 dest，返回 HEAD commit sha（40 hex）。subpath 非空时启用 sparse-checkout。"""
    auth_url = _inject_token(clone_url_https, token)
    if subpath:
        # sparse-checkout 流程：先 no-checkout 克隆骨架，再设置稀疏路径，最后 checkout
        await _run(["git", "clone", "--depth", "1", "--branch", ref, "--single-branch",
                    "--filter=blob:none", "--no-checkout", auth_url, dest], token)
        await _run(["git", "sparse-checkout", "set", subpath], token, cwd=dest)
        await _run(["git", "checkout", ref], token, cwd=dest)
    else:
        await _run(build_clone_args(auth_url, ref, dest), token)
    return await _run(["git", "rev-parse", "HEAD"], token, cwd=dest)
