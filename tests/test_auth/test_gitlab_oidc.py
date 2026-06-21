"""GitLabOidcProvider 单元测试：mock discovery + JWKS，自签 RS256 id_token。

测试覆盖：
  - test_validate_id_token_ok：正常路径（验签+claims+身份提取）
  - test_reject_bad_nonce：nonce 不匹配应拒绝
  - test_reject_bad_iss：iss 不匹配应拒绝
  - test_reject_expired：exp 已过期应拒绝
  - test_reject_alg_none：抹掉签名段（模拟 alg=none）应拒绝
"""
# json：构造 mock 响应体（如有需要）
import json
# time：生成 exp/iat 时间戳
import time

# pytest：测试框架；asyncio mark 用于异步测试
import pytest
# joserfc jwt 模块：encode 生成自签 id_token，decode 验证（implementation 内部用）
from joserfc import jwt as jjwt
# RSAKey：生成 RSA 密钥对；generate_key 返回含公私钥的 RSAKey 对象
from joserfc.jwk import RSAKey

# 被测目标
from src.service.scm.gitlab_oidc import GitLabOidcProvider
# GitLabOidcConfig：注入 issuer/client_id/client_secret
from src.service.scm.config import GitLabOidcConfig
# ScmIdentity：身份数据类，用于断言 get_login_identity 返回值
from src.service.scm.base import ScmIdentity

# 固定测试常量：issuer 域名和 audience（与 _provider 构造的 config 保持一致）
ISS = "https://gitlab.example.com"
AUD = "gl-cid"


def _key():
    """生成 2048-bit RSA 密钥对，kid=k1（测试用）。

    RSAKey.generate_key(bits, parameters):
      - bits=2048：RSA 密钥长度（安全下限）
      - parameters={"kid": ...}：附加到公私钥的 JWK 头部元数据
    返回同时包含公钥+私钥的 RSAKey 对象。
    """
    # parameters 字典里的 kid 会写入 JWK，让 KeySet 在 decode 时按 kid 路由到正确公钥
    return RSAKey.generate_key(2048, parameters={"kid": "k1"})


def _id_token(key, *, iss=ISS, aud=AUD, nonce="N1", exp_delta=600, alg="RS256", sub="sub-9", azp=None):
    """构造并签名一个 id_token JWT，供各测试用例使用。

    Args:
        key:       RSAKey 对象（含私钥，用于签名）
        iss:       Issuer claim（默认 ISS 常量）
        aud:       Audience claim（默认 AUD 常量）
        nonce:     nonce claim（防重放）
        exp_delta: exp = now + exp_delta（负值=已过期）
        alg:       JWT header 里的签名算法（默认 RS256）
        sub:       Subject claim（用户唯一 ID）
        azp:       Authorized Party（multi-aud 场景才传）
    Returns:
        JWT 字符串（header.payload.signature）
    """
    # int(time.time())：当前 Unix 时间戳（整数秒）
    now = int(time.time())
    # claims dict：OIDC 标准字段
    claims = {
        "iss": iss,                  # 签发方
        "aud": aud,                  # 受众
        "sub": sub,                  # 用户唯一标识
        "exp": now + exp_delta,      # 过期时间（exp_delta < 0 → 已过期）
        "iat": now,                  # 签发时间
        "nonce": nonce,              # 防重放令牌
        "preferred_username": "gluser",  # GitLab 用户名（get_login_identity 读取）
    }
    # azp（Authorized Party）仅 multi-aud 场景需要，不传则不加入 claims
    if azp:
        claims["azp"] = azp
    # JWT header：alg 声明签名算法，kid 匹配 JWKS 里的公钥
    header = {"alg": alg, "kid": "k1"}
    # jjwt.encode(header, claims, key)：用私钥签名，返回 JWT 字符串
    return jjwt.encode(header, claims, key)


def _provider(httpx_mock, key):
    """构造 GitLabOidcProvider，同时 mock discovery + JWKS HTTP 响应。

    pytest-httpx 的 httpx_mock fixture：拦截 AsyncClient 发出的 HTTP 请求，
    add_response(url=..., json=...) 按 URL 精确匹配返回预设响应。

    Args:
        httpx_mock: pytest-httpx fixture（自动注入）
        key:        RSAKey 对象（as_dict(private=False) 导出公钥 JWK）
    Returns:
        GitLabOidcProvider 实例（含已 mock 的网络层）
    """
    # key.as_dict(private=False)：导出公钥部分的 JWK dict（不含私钥 d/p/q/...）
    jwks = {"keys": [key.as_dict(private=False)]}

    # mock discovery 端点：返回 OIDC 元数据
    httpx_mock.add_response(
        url=f"{ISS}/.well-known/openid-configuration",
        json={
            "issuer": ISS,
            "authorization_endpoint": f"{ISS}/oauth/authorize",
            "token_endpoint": f"{ISS}/oauth/token",
            "jwks_uri": f"{ISS}/oauth/discovery/keys",
            # id_token_signing_alg_values_supported：implementation 据此构建 alg allowlist
            "id_token_signing_alg_values_supported": ["RS256"],
        },
    )
    # mock JWKS 端点：返回公钥集合（implementation 用于验签）
    httpx_mock.add_response(url=f"{ISS}/oauth/discovery/keys", json=jwks)

    # 构造 provider，注入配置（issuer/client_id 与 token claims 里的 iss/aud 一致）
    return GitLabOidcProvider(GitLabOidcConfig(issuer=ISS, client_id=AUD, client_secret="s"))


# ─── 测试用例 ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_validate_id_token_ok(httpx_mock):
    """正常路径：RS256 签名 + 正确 nonce → 校验通过，身份提取正确。"""
    key = _key()
    # _provider 返回含 mock 网络的 provider 实例
    p = _provider(httpx_mock, key)
    # validate_id_token 应返回通过全部校验的 claims dict
    claims = await p.validate_id_token(_id_token(key), expected_nonce="N1")
    # 断言 sub 字段（用户唯一 ID）
    assert claims["sub"] == "sub-9"
    # get_login_identity 接收含 id_token_claims 键的 dict（模拟 oauth callback 流程）
    ident = await p.get_login_identity({"id_token_claims": claims})
    # 断言返回的 ScmIdentity 数据类各字段
    assert ident == ScmIdentity(provider="gitlab", scm_user_id="sub-9", login="gluser")


@pytest.mark.asyncio
async def test_reject_bad_nonce(httpx_mock):
    """nonce 不匹配：token 里是 'OTHER'，期望 'N1' → 必须拒绝（防重放）。"""
    key = _key()
    p = _provider(httpx_mock, key)
    # pytest.raises(Exception)：断言代码块内必须抛出异常（任意类型）
    # `as` 语法可以捕获异常对象，但此处只需验证"有异常"即可
    with pytest.raises(Exception):
        # nonce="OTHER" 与 expected_nonce="N1" 不匹配 → validate_id_token 应 raise ValueError
        await p.validate_id_token(_id_token(key, nonce="OTHER"), expected_nonce="N1")


@pytest.mark.asyncio
async def test_reject_bad_iss(httpx_mock):
    """iss 不匹配：token 里是 'https://evil'，配置是 ISS → 必须拒绝（防跨站 token 注入）。"""
    key = _key()
    p = _provider(httpx_mock, key)
    with pytest.raises(Exception):
        # iss="https://evil" 与 cfg.issuer=ISS 不匹配 → raise ValueError
        await p.validate_id_token(_id_token(key, iss="https://evil"), expected_nonce="N1")


@pytest.mark.asyncio
async def test_reject_expired(httpx_mock):
    """exp 已过期：exp_delta=-10 让 exp = now-10 → 必须拒绝（超出 CLOCK_SKEW=120 才过期）。

    注意：CLOCK_SKEW=120，但 exp_delta=-10 → exp = now-10，now-exp = 10 < 120，
    实际上按当前实现这个 token 还在宽限期内……
    修正：用 exp_delta=-(120+60)=-180 确保 now - exp = 180 > 120（CLOCK_SKEW）→ 过期。
    """
    key = _key()
    p = _provider(httpx_mock, key)
    with pytest.raises(Exception):
        # exp_delta=-300：exp = now-300，远超 CLOCK_SKEW=120，肯定过期
        await p.validate_id_token(_id_token(key, exp_delta=-300), expected_nonce="N1")


@pytest.mark.asyncio
async def test_reject_alg_none(httpx_mock):
    """alg=none（抹掉签名）：把 JWT 第三段（签名）替换为空 → 签名验证失败，必须拒绝。

    攻击场景：攻击者把 alg 改成 none，抹掉签名段，伪造任意 claims。
    实现防御：algorithms=allowed 只含 RS*/ES*/PS*，none 不在其中；
              即使 alg=none 能绕过 allowlist，BadSignatureError 也会在验签时触发。

    构造方式：token.rsplit('.', 1)[0] + '.' 把签名段替换为空字符串，
    等效于 "header.payload."（signature 为空，三段 JWT 结构保持，但签名无效）。
    """
    key = _key()
    p = _provider(httpx_mock, key)
    # 取合法 token，抹掉最后一段（签名）→ "header.payload."
    # rsplit('.', 1)：从右边按 '.' 分割一次，得到 ["header.payload", "signature"]
    bad = _id_token(key).rsplit(".", 1)[0] + "."
    with pytest.raises(Exception):
        # decode 应因签名验证失败（BadSignatureError）或 alg 不匹配而 raise
        await p.validate_id_token(bad, expected_nonce="N1")
