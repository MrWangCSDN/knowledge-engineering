# src/service/scm/scm_refresh.py
"""per-provider token 刷新闭包（get_valid_scm_token 的 refresh_fn）。设计 §4.5。
按 body error 判 invalid_grant（GitHub 200+error / GitLab 400+error）；expires_in→expires_at datetime。"""
from __future__ import annotations

# datetime：用于构造 aware datetime（UTC）；timedelta：将 expires_in 秒数转成偏移量；timezone：UTC 时区
from datetime import datetime, timedelta, timezone
# Optional：表示函数返回值可能为 None（无 refresh 能力时）
from typing import Optional

# httpx：异步 HTTP 客户端，用于向 GitHub / GitLab token 端点发 POST 请求
import httpx

# RefreshFn：类型别名 Callable[[str], Awaitable[dict]]，表示 async refresh 闭包签名
# ScmTokenInvalid：token 失效异常，invalid_grant 时抛出，通知上层删 DB 行
from src.service.scm.scm_token_store import RefreshFn, ScmTokenInvalid

# GitHub token 端点：固定 URL，不依赖 discovery（GitHub 无 OIDC）
_GH_TOKEN_URL = "https://github.com/login/oauth/access_token"
# GitHub / GitLab 均以这两个 error code 表示 refresh_token 无效/已过期
_INVALID = {"invalid_grant", "bad_refresh_token"}


def _to_token_dict(body: dict) -> dict:
    """token 端点响应 → get_valid_scm_token 契约 dict（expires_in 秒 → expires_at aware datetime）。

    Args:
        body: token 端点 JSON 响应体（已解析为 dict）
    Returns:
        包含 access_token / refresh_token / expires_at 的 dict；
        expires_at 为 UTC aware datetime（有 expires_in 时），或 None（无 expires_in 时）
    """
    # 初始化 expires_at 为 None：若 body 无 expires_in，调用方无需处理过期
    exp = None
    # body.get("expires_in") 取不到或为 0/None 时，exp 保持 None
    if body.get("expires_in"):
        # datetime.now(timezone.utc)：当前 UTC 时间（aware datetime，带时区信息）
        # timedelta(seconds=int(...))：将秒数转成时间偏移
        exp = datetime.now(timezone.utc) + timedelta(seconds=int(body["expires_in"]))
    # 返回契约 dict：access_token 必有；refresh_token 可选（rotation 场景才有）；expires_at 可为 None
    return {"access_token": body["access_token"], "refresh_token": body.get("refresh_token"), "expires_at": exp}


def build_refresh_fn(provider: str, *, gitlab_provider=None, oauth_cfg=None) -> Optional[RefreshFn]:
    """构造 provider 的 async refresh 闭包。无凭证/无 refresh 能力 → None。

    Args:
        provider:        "github" 或 "gitlab"（其余 provider 返回 None）
        gitlab_provider: GitLabOidcProvider 实例（gitlab 路径必填）
        oauth_cfg:       OAuthConfig 实例（github 路径读 github.client_id/secret）
    Returns:
        async (refresh_token: str) -> dict 闭包；或 None（不支持 refresh）
    """
    # ── GitHub 路径 ──────────────────────────────────────────────────────────
    if provider == "github":
        # getattr(..., "github", None)：安全读 oauth_cfg.github；oauth_cfg 为 None 时也返回 None
        gh = getattr(oauth_cfg, "github", None) if oauth_cfg else None
        if gh is None:
            # 无 GitHub OAuth App 凭证 → 不支持 refresh（普通 PAT 也走此分支）
            return None

        # 闭包捕获 gh（含 client_id/client_secret），不依赖外部状态
        async def _gh_refresh(refresh_token: str) -> dict:
            """向 GitHub token 端点用 refresh_token 换新 access_token。

            GitHub invalid_grant 特殊性：HTTP 200 + body.error（不是 4xx），
            必须先 raise_for_status 排除运维配错（4xx/5xx），再读 body.error。
            """
            # async with httpx.AsyncClient：异步上下文管理器，请求结束自动关闭连接池
            async with httpx.AsyncClient(timeout=15) as client:
                # headers Accept: application/json → GitHub 返 JSON 而非 form-encoded
                # data={}：以 application/x-www-form-urlencoded 发送（OAuth 标准）
                r = await client.post(_GH_TOKEN_URL, headers={"Accept": "application/json"},
                                      data={"client_id": gh.client_id, "client_secret": gh.client_secret,
                                            "grant_type": "refresh_token", "refresh_token": refresh_token})
            # GitHub 坏凭证等运维错误 → HTTP 4xx/5xx；raise_for_status 抛 httpx.HTTPStatusError
            # 这类错误不是 invalid_grant，不应删用户关联行，所以必须抛非 ScmTokenInvalid
            r.raise_for_status()
            # r.json()：解析响应体为 dict；GitHub 成功和 invalid_grant 都是 HTTP 200，区别在 body.error
            body = r.json()
            # body.get("error") in _INVALID：检测 GitHub 的 invalid_grant / bad_refresh_token
            if body.get("error") in _INVALID:
                # 抛 ScmTokenInvalid → 上层 get_valid_scm_token 会删 DB 行并告知用户重新关联
                raise ScmTokenInvalid(f"github refresh: {body['error']}")
            if "access_token" not in body:
                # 未知 error 或其他异常响应 → 抛 RuntimeError（非 ScmTokenInvalid，保住关联行）
                raise RuntimeError(f"github refresh 无 access_token：{body.get('error')}")
            return _to_token_dict(body)

        # 返回闭包本身（函数对象），不调用它
        return _gh_refresh

    # ── GitLab 路径 ──────────────────────────────────────────────────────────
    if provider == "gitlab":
        if gitlab_provider is None:
            # 无 GitLabOidcProvider → 不支持 refresh
            return None

        async def _gl_refresh(refresh_token: str) -> dict:
            """向 GitLab token 端点（经 OIDC discovery）用 refresh_token 换新 token。

            GitLab invalid_grant 特殊性：HTTP 400 + body.error；
            必须先读 body.error 判 invalid_grant，再 raise_for_status 处理其他非 2xx。
            顺序与 GitHub 相反：GitHub 先 raise_for_status（因为 invalid_grant 是 200），
            GitLab 先读 body（因为 invalid_grant 是 400，raise_for_status 会吞掉 error code）。
            """
            # await gitlab_provider._discovery()：异步拉取（或返回缓存）OIDC discovery 文档
            # disc["token_endpoint"] 是 GitLab 自管实例的 token 端点（非固定 URL，需 discovery）
            disc = await gitlab_provider._discovery()
            # gitlab_provider._cfg：GitLabOidcConfig（含 client_id/client_secret/issuer）
            cfg = gitlab_provider._cfg
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(disc["token_endpoint"], headers={"Accept": "application/json"},
                                      data={"client_id": cfg.client_id, "client_secret": cfg.client_secret,
                                            "grant_type": "refresh_token", "refresh_token": refresh_token})
            # 先尝试解析 body（即使是 4xx 也可能有 error 字段）
            body = {}
            try:
                # r.json() 可能在非 JSON 响应（如 502 纯文本）时抛异常
                body = r.json()
            except Exception:      # noqa: BLE001 — 非 JSON 响应时 body 保持空 dict
                body = {}
            # GitLab invalid_grant 走 HTTP 400 + body.error → 先于 raise_for_status 检测
            if body.get("error") in _INVALID:
                raise ScmTokenInvalid(f"gitlab refresh: {body['error']}")
            # 其他非 2xx（坏凭证/5xx 等）→ raise_for_status 抛 httpx.HTTPStatusError（非 ScmTokenInvalid）
            r.raise_for_status()
            if "access_token" not in body:
                raise RuntimeError("gitlab refresh 无 access_token")
            return _to_token_dict(body)

        return _gl_refresh

    # 未知 provider → 不支持 refresh
    return None
