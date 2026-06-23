"""GitHubAppProvider：RS256 JWT 换 1h installation token（内存缓存），httpx 调 REST。
设计 §6/§7。token 不落库，缓存到接近过期重取。"""
from __future__ import annotations

import asyncio
import os
import shutil
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

    async def get_account_login(self, installation_id: int) -> str:
        """取该安装的账号 login（org/user 名）。GET /app/installations/{id} 需 App JWT。"""
        headers = {
            "Authorization": f"Bearer {self._app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{_API}/app/installations/{installation_id}", headers=headers)
            resp.raise_for_status()
        return resp.json().get("account", {}).get("login", "")

    async def clone(self, installation_id: int, full_name: str, ref: str,
                    subpath: Optional[str], dest: str) -> str:
        """浅克隆指定仓库分支到 dest，返回 HEAD commit sha（40 hex）。"""
        token = await self.get_installation_token(installation_id)
        url = f"https://github.com/{full_name}.git"
        return await shallow_clone(url, ref=ref, dest=dest, token=token, subpath=subpath)

    def build_authorize_url(self, *, client_id: str, redirect_uri: str, state: str) -> str:
        """GitHub App user-to-server 授权 URL（不带 scope；权限由 App 配置）。"""
        # urllib.parse.urlencode 将 dict 转为 URL 查询字符串，如 a=1&b=2
        from urllib.parse import urlencode
        # 构造三个必要查询参数：client_id（App Client ID）、redirect_uri（回调地址）、state（CSRF 防护随机串）
        q = urlencode({"client_id": client_id, "redirect_uri": redirect_uri, "state": state})
        # f-string：Python 3.6+ 的字符串格式化语法，{变量} 会被替换为其值
        return f"https://github.com/login/oauth/authorize?{q}"

    async def exchange_code(self, *, client_id: str, client_secret: str,
                            code: str, redirect_uri: str) -> dict:
        """用 code 换 user access token。返回 {"access_token":..., "scope":..., ...}。"""
        # async with：异步上下文管理器，退出时自动关闭 httpx.AsyncClient 连接
        async with httpx.AsyncClient(timeout=15) as client:
            # POST 到 GitHub OAuth token 端点；Accept: application/json 让 GitHub 返回 JSON 而非表单编码
            resp = await client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                # data= 用于 application/x-www-form-urlencoded 格式（GitHub 要求）
                data={"client_id": client_id, "client_secret": client_secret,
                      "code": code, "redirect_uri": redirect_uri},
            )
            # raise_for_status：若 HTTP 状态码 >= 400 则抛出 httpx.HTTPStatusError
            resp.raise_for_status()
            return resp.json()

    async def get_login_identity(self, token: dict) -> "ScmIdentity":
        """用 OAuth user access token 调 GET /user，返回登录身份 ScmIdentity。"""
        # 局部 import 避免循环依赖（github_app 和 base 互相不在顶层 import）
        from src.service.scm.base import ScmIdentity
        # 从 token dict 取出 access_token 字符串
        access = token["access_token"]
        # async with：异步上下文管理器，退出时自动关闭 httpx.AsyncClient 连接
        async with httpx.AsyncClient(timeout=15) as client:
            # GET /user：需要 Authorization 头携带 access_token
            resp = await client.get(
                f"{_API}/user",
                headers={"Authorization": f"token {access}",
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"},
            )
            # raise_for_status：若 HTTP 状态码 >= 400 则抛出 httpx.HTTPStatusError
            resp.raise_for_status()
            data = resp.json()
        # str(data["id"])：GitHub 用户 id 是整数，统一转为字符串作为 scm_user_id
        # data.get("login")：login 字段不一定存在，用 .get() 安全取值（不存在时返回 None）
        return ScmIdentity(provider="github", scm_user_id=str(data["id"]), login=data.get("login"))

    async def _get_user(self, user_token: str, path: str) -> httpx.Response:
        """用 user-to-server token 发 GET。A7：绝不用 installation/app token。返回原始 Response（调用方按 status 分支）。"""
        # A7 不变量：只用调用方传入的 user_token，构造 "token <user_token>" 格式的 Authorization 头
        # 此处绝不调用 get_installation_token / _get，确保 user 权限边界
        headers = {
            "Authorization": f"token {user_token}",          # GitHub user-to-server token 格式
            "Accept": "application/vnd.github+json",          # 要求返回 GitHub v3 JSON
            "X-GitHub-Api-Version": "2022-11-28",             # 固定 API 版本，避免行为漂移
        }
        # async with：异步上下文管理器，退出时自动关闭底层 TCP 连接
        async with httpx.AsyncClient(timeout=15) as client:
            # 注意：这里不调用 raise_for_status，调用方需自行按 status_code 分支处理
            return await client.get(f"{_API}{path}", headers=headers)

    @staticmethod
    def _is_rate_limited(resp: httpx.Response) -> bool:
        """区分"无权 403"与"限流 403/429"（限流应当抛→502，不可当永久 deny）。
        @staticmethod：不依赖 self，纯函数，直接通过类或实例调用。"""
        # 只关心 403 和 429 这两个可能是限流的状态码
        if resp.status_code not in (403, 429):
            return False
        # x-ratelimit-remaining: "0" 是 GitHub 主限流标志
        if resp.headers.get("x-ratelimit-remaining") == "0":
            return True
        # Retry-After 头：GitHub secondary rate limit 和 429 常见
        if resp.headers.get("retry-after"):
            return True
        # 兜底：body 文本包含限流关键词（二级限流/滥用检测）
        body = (resp.text or "").lower()
        # "in" 运算符：检查子字符串是否存在于字符串中
        return ("rate limit" in body) or ("secondary rate" in body) or ("abuse" in body)

    async def list_user_installations(self, *, user_token: str) -> list[int]:
        """该用户可见的 App 安装 id 列表（GET /user/installations，读 installations 键 + 分页）。
        *：keyword-only 参数，调用时必须写 user_token=...，不能按位置传参（防止误用）。"""
        out: list[int] = []   # list[int]：Python 3.9+ 内置泛型，不需要 from typing import List
        page = 1              # 从第 1 页开始翻页
        while True:           # 无限循环，满足退出条件后 break
            # 分页参数：per_page=100（GitHub 上限）/ page=当前页
            resp = await self._get_user(user_token, f"/user/installations?per_page=100&page={page}")
            # raise_for_status：非 2xx 状态码直接抛出异常，告知调用方 token 失效等
            resp.raise_for_status()
            # .get("installations", [])：installations 键不存在时安全返回空列表
            items = resp.json().get("installations", [])
            if not items:     # 空列表 → 没有更多数据，退出
                break
            # 列表推导式：从每个 installation dict 取出 id 字段
            # 等效于: for i in items: out.append(i["id"])
            out.extend(i["id"] for i in items)
            if len(items) < 100:   # 本页不足 100 条 → 已到最后一页
                break
            page += 1         # 翻到下一页继续
        return out

    async def list_user_visible_repos(self, *, user_token: str, installation_id: int) -> list["VisibleRepo"]:
        """该用户在指定 installation 下可见仓（user-token 视角，GET /user/installations/{id}/repositories）。
        内联 permissions 推 role_name → github_role_to_scm；NOT_VISIBLE 滤除。
        非限流 403/404（无安装访问）→ 空列表；限流 403/429 或 5xx → raise（上层 502）。"""
        from src.service.scm.base import RepoInfo, VisibleRepo, ScmRole
        from src.service.scm.scm_roles import github_role_to_scm
        out: list[VisibleRepo] = []
        page = 1
        while True:
            resp = await self._get_user(
                user_token, f"/user/installations/{installation_id}/repositories?per_page=100&page={page}")
            if resp.status_code in (403, 404) and not self._is_rate_limited(resp):
                return []                                   # 调用者对该安装无访问 → 合法空集（fail-closed）
            resp.raise_for_status()                          # 限流 403/429 或 5xx → HTTPStatusError → 上层 502
            repos = resp.json().get("repositories", [])
            if not repos:
                break
            for r in repos:
                perms = r.get("permissions") or {}
                role_name = ("admin" if perms.get("admin") else
                             "maintain" if perms.get("maintain") else
                             "write" if perms.get("push") else
                             "triage" if perms.get("triage") else
                             "read" if perms.get("pull") else None)
                role = github_role_to_scm(role_name)
                if role == ScmRole.NOT_VISIBLE:
                    continue
                out.append(VisibleRepo(
                    repo=RepoInfo(external_id=r["id"], full_name=r["full_name"],
                                  default_branch=r.get("default_branch") or "main",
                                  private=bool(r.get("private"))),
                    role=role))
            if len(repos) < 100:
                break
            page += 1
        return out

    async def resolve_repo_role(self, *, token: str, repo: str, principal) -> "ScmRole":
        """解析用户在仓 repo(=full_name) 的 ScmRole。token=user-to-server，principal=GitHub username(=scm_login)。
        状态矩阵：200→映射角色，限流403→抛，401→NOT_VISIBLE，403/404→只读回退，5xx→抛。"""
        # 局部 import：避免模块顶层循环依赖（base 不 import github_app，反方向安全）
        from src.service.scm.base import ScmRole
        from src.service.scm.scm_roles import github_role_to_scm
        # B3 安全守卫：scm_login 缺失时不拼 None 到 URL，直接返回 NOT_VISIBLE
        if not principal:
            return ScmRole.NOT_VISIBLE
        # 调用 user token 专属方法（A7：不走 installation token）
        resp = await self._get_user(token, f"/repos/{repo}/collaborators/{principal}/permission")
        sc = resp.status_code   # 取出状态码，后续多次用到
        if sc == 200:
            # 200 → 解析 role_name / permission 字段，映射到三档 ScmRole
            data = resp.json()
            return github_role_to_scm(data.get("role_name"), data.get("permission"))
        if self._is_rate_limited(resp):
            # 限流：用 raise_for_status 抛出 httpx.HTTPStatusError → 上层转 502
            resp.raise_for_status()
        if sc == 401:
            # 401：token 已吊销/失效 → fail-closed，返回 NOT_VISIBLE（不重试）
            return ScmRole.NOT_VISIBLE
        if sc in (403, 404):
            # 403：只读 collaborator 调 permission 端点可能被拒；404：不在 collaborators 列表
            # → 两种情况都用回退逻辑：直接查 /repos/{repo} 自测可见性
            return await self._repo_read_fallback(token, repo)
        # 5xx 等其余状态码 → raise_for_status 抛出（上层 502）
        resp.raise_for_status()
        return ScmRole.NOT_VISIBLE   # 理论不可达，让 mypy/type checker 满意

    async def _repo_read_fallback(self, user_token: str, repo: str) -> "ScmRole":
        """I2 只读回退：permission 端点对只读 collaborator 可能 403 → 用用户自己 token 自查 GET /repos/{repo}。
        200=能读（≥read）→ CAN_QUERY；否则 NOT_VISIBLE（含私有仓/不存在/token 失效）。"""
        from src.service.scm.base import ScmRole
        # 用相同 user token 直接查仓库元数据（公开仓/有 read+ 权限的私有仓均返回 200）
        resp = await self._get_user(user_token, f"/repos/{repo}")
        if resp.status_code == 200:
            # 能访问仓库本体 → 至少有 read 权限 → CAN_QUERY
            return ScmRole.CAN_QUERY
        # 404/403/401 → 仓库不可见 → NOT_VISIBLE
        return ScmRole.NOT_VISIBLE


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
    # 复用同一 dest 时（重试/重复索引）git clone 会因目录非空失败；先清掉旧目录保证干净克隆。
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    if subpath:
        # sparse-checkout 流程：先 no-checkout 克隆骨架，再设置稀疏路径，最后 checkout
        await _run(["git", "clone", "--depth", "1", "--branch", ref, "--single-branch",
                    "--filter=blob:none", "--no-checkout", auth_url, dest], token)
        await _run(["git", "sparse-checkout", "set", subpath], token, cwd=dest)
        await _run(["git", "checkout", ref], token, cwd=dest)
    else:
        await _run(build_clone_args(auth_url, ref, dest), token)
    return await _run(["git", "rev-parse", "HEAD"], token, cwd=dest)
