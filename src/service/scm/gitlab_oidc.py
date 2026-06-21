"""GitLab OIDC 登录 provider：discovery + 授权 URL + code 交换 + id_token 完整校验 + 登录身份。设计 §4.2/§6 B3。"""
# 允许在类型注解里使用 `X | Y` 语法（Python 3.9 兼容写法）
from __future__ import annotations

# time：读取当前 Unix 时间戳，用于 exp/iat 校验
import time
# Optional：表示"可能为 None"的类型注解
from typing import Optional
# urlencode：把 dict 转成 URL query string（如 key=val&key2=val2）
from urllib.parse import urlencode

# httpx：异步 HTTP 客户端，用于 discovery/JWKS/token exchange 网络请求
import httpx
# jjwt：joserfc 的 JWT 模块，提供 encode/decode（验签+claims 读取）
from joserfc import jwt as jjwt
# KeySet：JWKS 公钥集合，import_key_set 从 JSON 构建
from joserfc.jwk import KeySet

# ScmIdentity：登录身份数据类（provider/scm_user_id/login）
from src.service.scm.base import ScmIdentity
# GitLabOidcConfig：GitLab OIDC 配置（issuer/client_id/client_secret）
from src.service.scm.config import GitLabOidcConfig

# 允许的时钟偏差（秒）：处理服务器时钟不完全同步的场景
# exp 允许往过去延伸 120s，iat 允许往未来偏移 120s
_CLOCK_SKEW = 120  # 秒


class GitLabOidcProvider:
    """GitLab OIDC 登录 provider。

    职责链：
      1. _discovery() → 拉取 OpenID Configuration（缓存在实例上）
      2. build_authorize_url() → 构造 OAuth 授权跳转 URL
      3. exchange_code() → 用 authorization_code 换 token（含 id_token）
      4. _jwks() → 拉取 JWKS 公钥集合
      5. validate_id_token() → 完整校验 id_token（签名+iss/aud/exp/iat/nonce）
      6. get_login_identity() → 从已校验 claims 提取登录身份
    """

    def __init__(self, cfg: GitLabOidcConfig):
        """初始化，注入配置；_disc 延迟加载（首次调用 _discovery 时拉取）。"""
        # 保存 OIDC 配置（issuer/client_id/client_secret）
        self._cfg = cfg
        # discovery 文档缓存：None 表示尚未拉取；拉取后赋值为 dict，避免重复请求
        self._disc: Optional[dict] = None

    async def _discovery(self) -> dict:
        """拉 OIDC discovery 文档（含 authorization/token/jwks 端点 + 支持的 alg）。

        路径：{issuer}/.well-known/openid-configuration（OIDC 标准端点）。
        首次调用时发 HTTP GET，之后直接返回缓存（同实例生命周期内只拉一次）。
        """
        # `is None` 而非 `not self._disc`：防止 discovery 返回空 dict 时误判
        if self._disc is None:
            # `async with` + AsyncClient：异步上下文管理器，自动关闭 HTTP 连接
            async with httpx.AsyncClient(timeout=15) as client:
                # f-string 拼接 discovery 端点；raise_for_status 遇 4xx/5xx 直接抛异常
                r = await client.get(f"{self._cfg.issuer}/.well-known/openid-configuration")
                r.raise_for_status()
                # r.json()：解析响应体为 Python dict
                self._disc = r.json()
        return self._disc

    async def build_authorize_url(self, *, redirect_uri: str, state: str, nonce: str) -> str:
        """构造 GitLab OIDC 授权跳转 URL。

        Args:
            redirect_uri: 授权成功后 GitLab 回调的 URL
            state:        随机不可预测字符串（防 CSRF）
            nonce:        随机不可预测字符串（防 id_token 重放，写入 id_token claims）
        Returns:
            完整授权 URL，前端 redirect 到此地址
        """
        # 等待 discovery 拿到 authorization_endpoint
        disc = await self._discovery()
        # urlencode：将参数 dict 转为 URL query string（自动 percent-encode 特殊字符）
        q = urlencode({
            "response_type": "code",             # 授权码模式（OIDC 标准）
            "client_id": self._cfg.client_id,    # OAuth App 的 client_id
            "redirect_uri": redirect_uri,         # 回调地址（必须与 GitLab 配置一致）
            "scope": "openid profile",            # openid 必选（OIDC）；profile 取 preferred_username
            "state": state,                       # CSRF 防护令牌
            "nonce": nonce,                       # id_token 防重放令牌
        })
        # 拼接 authorization_endpoint 和 query string
        return f"{disc['authorization_endpoint']}?{q}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        """用 authorization_code 换 token（access_token + id_token 等）。

        Args:
            code:         GitLab 回调携带的 authorization_code（一次性）
            redirect_uri: 必须与授权请求时完全一致（GitLab 会校验）
        Returns:
            token 响应 dict，含 access_token / refresh_token / id_token / expires_in
        """
        disc = await self._discovery()
        async with httpx.AsyncClient(timeout=15) as client:
            # POST 到 token_endpoint：data= 发送 application/x-www-form-urlencoded
            r = await client.post(disc["token_endpoint"], data={
                "grant_type": "authorization_code",  # 固定值，代表授权码换 token
                "code": code,                        # 从 callback URL 取到的一次性授权码
                "redirect_uri": redirect_uri,        # 必须与授权请求完全匹配
                "client_id": self._cfg.client_id,    # App 标识
                "client_secret": self._cfg.client_secret,  # App 密钥（机密，走 server-side）
            }, headers={"Accept": "application/json"})
            r.raise_for_status()
            # 返回含 id_token 的 token 响应
            return r.json()

    async def _jwks(self) -> KeySet:
        """拉取 GitLab JWKS 公钥集合，构建 joserfc KeySet 用于验签。

        Returns:
            joserfc.jwk.KeySet：可直接传给 jjwt.decode 作为验签密钥集合
        """
        disc = await self._discovery()
        async with httpx.AsyncClient(timeout=15) as client:
            # jwks_uri 来自 discovery 文档（如 {issuer}/oauth/discovery/keys）
            r = await client.get(disc["jwks_uri"])
            r.raise_for_status()
            # KeySet.import_key_set：把 {"keys": [...]} 格式的 JWKS JSON 转为 KeySet 对象
            return KeySet.import_key_set(r.json())

    async def validate_id_token(self, id_token: str, *, expected_nonce: Optional[str]) -> dict:
        """完整校验 id_token：JWKS 验签 + alg 锚定（拒 none/对称）+ iss/aud/azp/exp/iat/nonce。

        校验顺序（任一失败立即 raise）：
          1. alg allowlist 非空守卫（防 algorithms=[] 绕过验签）
          2. JWKS 签名验证 + alg 锚定到 discovery 声明的非对称算法集
          3. iss 精确匹配
          4. aud 包含 client_id（multi-aud 场景还要求 azp == client_id）
          5. exp 未过期（含 CLOCK_SKEW）
          6. iat 存在且未来偏移不超过 CLOCK_SKEW
          7. nonce 与期望值精确匹配

        Args:
            id_token:       从 token endpoint 取到的 JWT 字符串
            expected_nonce: 授权请求时生成的 nonce（None 表示跳过检查，不推荐）
        Returns:
            通过全部校验的 claims dict
        Raises:
            ValueError:           iss/aud/azp/exp/iat/nonce 校验失败
            BadSignatureError:    签名验证失败（含 alg=none 抹签名场景）
            DecodeError / ...:    token 格式非法
        """
        disc = await self._discovery()

        # ── 校验 1：alg allowlist 非空守卫 ─────────────────────────────────────
        # 从 discovery 取支持的签名算法，只保留非对称算法（RS*/ES*/PS*）
        # 杜绝 HS*（对称，需共享密钥）和 none（无签名）混入
        allowed = [
            a for a in disc.get("id_token_signing_alg_values_supported", ["RS256"])
            if a.startswith(("RS", "ES", "PS"))  # 白名单：仅非对称算法族
        ]
        # ⚠️ 关键安全守卫：joserfc 的 decode(..., algorithms=[]) 含义是"不约束算法"
        # → 等价于关闭 alg 校验，任意 alg 的 token 都会通过。
        # 因此 allowed 为空时必须直接拒绝，不能把空列表传给 decode。
        if not allowed:
            raise ValueError("id_token: discovery 未提供可信的非对称签名算法")

        # ── 校验 2：JWKS 签名验证 + alg 锚定 ────────────────────────────────────
        # _jwks() 拉取 GitLab 公钥集合；algorithms=allowed 同时做：
        #   a) 验签（签名与公钥不匹配 → BadSignatureError）
        #   b) alg 锚定（token header 的 alg 不在 allowed 里 → 报错，拒绝 none/HS*）
        keyset = await self._jwks()
        # jjwt.decode 是同步调用；若验签失败直接抛异常，不返回 token 对象
        token = jjwt.decode(id_token, keyset, algorithms=allowed)
        # token.claims 是解码后的 claims dict（只有验签通过才能走到这里）
        claims = token.claims

        # 读取当前 Unix 时间戳，用于 exp/iat 边界计算
        now = int(time.time())

        # ── 校验 3：iss 精确匹配 ─────────────────────────────────────────────────
        # iss 必须与配置的 issuer 完全一致（字符串精确比较，不做前缀/域名匹配）
        if claims.get("iss") != self._cfg.issuer:
            raise ValueError("id_token iss 不符")

        # ── 校验 4：aud 包含 client_id ───────────────────────────────────────────
        # aud 可以是单个字符串或字符串列表（OIDC 规范 5.1）
        aud = claims.get("aud")
        # `isinstance(aud, list)`：判断是否为列表类型（多受众场景）
        aud_ok = (aud == self._cfg.client_id) or (isinstance(aud, list) and self._cfg.client_id in aud)
        if not aud_ok:
            raise ValueError("id_token aud 不符")

        # ── 校验 4b：multi-aud 时 azp 必须等于 client_id ────────────────────────
        # OIDC 规范：当 aud 包含多个受众时，azp（Authorized Party）必须是当前 client_id
        # 防止 token 被其他 client 窃用（confused deputy 攻击）
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self._cfg.client_id:
            raise ValueError("多 aud 但 azp 不符")

        # ── 校验 5：exp 未过期 ───────────────────────────────────────────────────
        # exp（expiration time）：token 过期时间；加 CLOCK_SKEW 容忍时钟偏差
        # 若 exp 缺失，claims.get("exp", 0) 返回 0 → 视为已过期（安全 fail-closed）
        if int(claims.get("exp", 0)) < now - _CLOCK_SKEW:
            raise ValueError("id_token 已过期")

        # ── 校验 6：iat 存在且不在未来 ──────────────────────────────────────────
        # iat（issued at）：签发时间；必须存在且不超过 now+CLOCK_SKEW
        # iat > now+skew 说明 token 是"未来签发的"，可能是时间篡改攻击
        if "iat" not in claims or int(claims["iat"]) > now + _CLOCK_SKEW:
            raise ValueError("id_token iat 非法")

        # ── 校验 7：nonce 精确匹配 ───────────────────────────────────────────────
        # nonce 防止 id_token 重放攻击：授权请求时生成随机值，回调时验证 token 里的 nonce
        # expected_nonce=None 表示调用方明确不校验（仅用于无状态场景，通常不推荐）
        if expected_nonce is not None and claims.get("nonce") != expected_nonce:
            raise ValueError("id_token nonce 不符")

        # 所有校验通过，返回 claims dict（dict() 构造副本，防止外部修改影响内部状态）
        return dict(claims)

    async def get_login_identity(self, token: dict) -> ScmIdentity:
        """从已通过 validate_id_token 校验的 claims 提取登录身份。

        Args:
            token: 含 "id_token_claims" 键的 dict，值为 validate_id_token 返回的 claims
        Returns:
            ScmIdentity(provider="gitlab", scm_user_id=sub, login=preferred_username)
        """
        # 取出已校验的 claims dict（由调用方在 validate_id_token 后存入）
        claims = token["id_token_claims"]
        # sub：OIDC 标准字段，GitLab 用户唯一标识（stable across rename）
        # preferred_username：GitLab 用户名（可能随用户改名变化，展示用）
        return ScmIdentity(
            provider="gitlab",
            scm_user_id=str(claims["sub"]),              # 强转 str 以防 sub 为数字类型
            login=claims.get("preferred_username"),       # .get()：字段不存在返回 None
        )
