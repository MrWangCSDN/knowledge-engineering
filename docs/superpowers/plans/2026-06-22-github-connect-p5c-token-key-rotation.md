# GitHub 连接 P5c — token 密钥轮换 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `token_crypto` 改用 `MultiFernet` 支持**零停机密钥轮换**（encrypt 用主 key、decrypt 试所有 key），加离线 `token_reencrypt` 工具把旧密文迁到主 key——使密钥可轮换且最终可弃旧 key。

**Architecture:** `cryptography.fernet.MultiFernet`：`KE_TOKEN_ENC_KEYS`（逗号分隔，主 key 在前）+ `KE_TOKEN_ENC_KEY` 单 key 向后兼容回退。**无 schema 变更、无外部依赖、对外 encrypt_token/decrypt_token 签名不变、调用方零改动**。

**Tech Stack:** cryptography（已装，47.0.0）/ SQLAlchemy async / pytest / sqlite :memory:。

**分支:** `feat/token-key-rotation-p5c`（栈式，base=`feat/github-repo-connect`，与 P5b 独立各自 PR；PR #2 冻结在 P5a）。完成后开栈式 PR（base=feat/github-repo-connect）。

**设计依据:** Obsidian `GitHub仓库连接-P5c-token密钥轮换-设计.md`（已过对抗评审实证：MultiFernet 47.0.0 语义/向后兼容 36+1102 测试绿/ORM 类名确认；补 3 IMPORTANT = token_reencrypt 顶部 logging+_log/全 import/CLI 仅对生产库）。

**测试运行约定（必带 env）:**
```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest <路径> -v
```

---

## 文件结构

| 文件 | 责任 | 动作 |
|---|---|---|
| `src/service/token_crypto.py` | MultiFernet + `_load_keys` + `rotate_token` + reset 缓存 | 改 |
| `src/service/token_reencrypt.py` | `reencrypt_all_tokens` + `_main` CLI | 建 |
| `tests/test_auth/test_token_rotation.py` | 轮换/回退/rotate/reencrypt 测试 | 建 |

**关键既有件（已核实 worktree）：**
- `token_crypto.py`：`_get_fernet()`(lru_cache, 内部专用, 评审确认外部零引用)、`encrypt_token`/`decrypt_token`(InvalidToken→ValueError)/`token_hint`/`reset_fernet_cache()`。
- `MultiFernet`（cryptography 47.0.0）：`MultiFernet([Fernet(k1),...])`；`.encrypt` 用 k1（主）；`.decrypt` 试全部；`.rotate(t)` 任一解→k1 重加密；全失败抛 `InvalidToken`。
- `GitCredential`（db_models_homepage.py:137，表 git_credentials）：`id:String(64)`、`name:str`、`encrypted_token:Text not null`、`type` 默认 "pat"、`created_by` 可空。
- `UserScmToken`（:208，表 user_scm_token）：`id:String(64)`、`user_id:int`、`provider:str`、`access_token:Text not null`、`refresh_token:Text nullable`、`linked_at:DateTime not null`。
- `get_session_maker`（src.service.db）；CLI 范式见 indexer.py `_main`。
- 测试范式：`monkeypatch.setenv("KE_TOKEN_ENC_KEY", key)` + `token_crypto.reset_fernet_cache()`（test_token_crypto.py autouse fixture）。

---

## Task 1: `token_crypto` 改 MultiFernet + `rotate_token`

**Files:**
- Modify: `src/service/token_crypto.py`
- Test: `tests/test_auth/test_token_rotation.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth/test_token_rotation.py`：
```python
"""P5c token 密钥轮换：MultiFernet 多 key + rotate_token + reencrypt。"""
import pytest
from cryptography.fernet import Fernet
from src.service import token_crypto


@pytest.fixture(autouse=True)
def _reset():
    token_crypto.reset_fernet_cache()
    yield
    token_crypto.reset_fernet_cache()


def test_backward_compat_single_key(monkeypatch):
    monkeypatch.delenv("KE_TOKEN_ENC_KEYS", raising=False)
    monkeypatch.setenv("KE_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    token_crypto.reset_fernet_cache()
    c = token_crypto.encrypt_token("ghp_secret")
    assert token_crypto.decrypt_token(c) == "ghp_secret"


def test_multi_key_encrypt_uses_primary(monkeypatch):
    k_new = Fernet.generate_key().decode()
    k_old = Fernet.generate_key().decode()
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", f"{k_new},{k_old}")
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    c = token_crypto.encrypt_token("s")
    # 密文只用主 key k_new 即可解 → 证明 encrypt 用列表第一个
    assert Fernet(k_new.encode()).decrypt(c.encode()).decode() == "s"


def test_multi_key_decrypt_fallback_and_missing(monkeypatch):
    k_new = Fernet.generate_key().decode()
    k_old = Fernet.generate_key().decode()
    old_cipher = Fernet(k_old.encode()).encrypt(b"s").decode()
    # 有 k_old 在列表 → 能解
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", f"{k_new},{k_old}")
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    assert token_crypto.decrypt_token(old_cipher) == "s"
    # 去掉 k_old → 解不开抛 ValueError
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", k_new)
    token_crypto.reset_fernet_cache()
    with pytest.raises(ValueError):
        token_crypto.decrypt_token(old_cipher)


def test_rotate_token_migrates_to_primary(monkeypatch):
    k_new = Fernet.generate_key().decode()
    k_old = Fernet.generate_key().decode()
    old_cipher = Fernet(k_old.encode()).encrypt(b"s").decode()
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", f"{k_new},{k_old}")
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    rotated = token_crypto.rotate_token(old_cipher)
    # rotate 后单独主 key 即可解
    assert Fernet(k_new.encode()).decrypt(rotated.encode()).decode() == "s"


def test_empty_keys_raises(monkeypatch):
    monkeypatch.delenv("KE_TOKEN_ENC_KEYS", raising=False)
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    with pytest.raises(RuntimeError):
        token_crypto.encrypt_token("x")


def test_whitespace_keys_filtered(monkeypatch):
    k1 = Fernet.generate_key().decode()
    k2 = Fernet.generate_key().decode()
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", f"{k2},, ,{k1},")   # 空逗号项过滤 → [k2,k1]
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    c = token_crypto.encrypt_token("s")
    assert Fernet(k2.encode()).decrypt(c.encode()).decode() == "s"   # 主 key=k2
```

- [ ] **Step 2: 跑红**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_token_rotation.py -v
```
预期：FAIL（`rotate_token` 不存在；多 key 路径未实现）。

- [ ] **Step 3: 实现（`src/service/token_crypto.py`）**

把 import 行改为 `from cryptography.fernet import Fernet, MultiFernet, InvalidToken`。
新增 `_load_keys`、把 `_get_fernet` 替换为 `_get_multifernet`、`encrypt_token`/`decrypt_token` 走 MultiFernet、新增 `rotate_token`、`reset_fernet_cache` 清新缓存（`token_hint` 不动）：
```python
def _load_keys() -> list[str]:
    """读密钥列表：优先 KE_TOKEN_ENC_KEYS（逗号分隔，主 key 在前），回退单 KE_TOKEN_ENC_KEY。"""
    multi = os.getenv("KE_TOKEN_ENC_KEYS", "")
    if multi.strip():
        keys = [k.strip() for k in multi.split(",") if k.strip()]
    else:
        single = os.getenv("KE_TOKEN_ENC_KEY", "").strip()
        keys = [single] if single else []
    if not keys:
        raise RuntimeError(
            "KE_TOKEN_ENC_KEYS / KE_TOKEN_ENC_KEY 未设置（至少一个 Fernet key）。"
            "生成：python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
    return keys


@lru_cache(maxsize=1)
def _get_multifernet() -> MultiFernet:
    """单例 MultiFernet。列表第一个=主 key（encrypt 用它）；decrypt 依次试所有 key。"""
    try:
        return MultiFernet([Fernet(k.encode("utf-8")) for k in _load_keys()])
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"密钥格式不对（须 44 字节 base64 编码 Fernet key）：{e}") from e


def encrypt_token(plain: str) -> str:
    if not plain:
        raise ValueError("token 不能为空")
    return _get_multifernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_token(cipher: str) -> str:
    if not cipher:
        raise ValueError("密文不能为空")
    try:
        return _get_multifernet().decrypt(cipher.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("密文损坏或密钥不匹配") from e


def rotate_token(cipher: str) -> str:
    """用任一 key 解 → 主 key 重加密（MultiFernet.rotate）。re-encrypt 工具用。"""
    if not cipher:
        raise ValueError("密文不能为空")
    try:
        return _get_multifernet().rotate(cipher.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("密文损坏或密钥不匹配") from e


def reset_fernet_cache() -> None:
    """单测专用：重置 lru_cache，让 monkeypatch 后的 env 生效。"""
    _get_multifernet.cache_clear()
```
> 删除旧 `_get_fernet`（评审确认外部零引用）。`token_hint`/模块 docstring 顶部可更新 env 说明（提 KE_TOKEN_ENC_KEYS），非必须。

- [ ] **Step 4: 跑绿（含既有 token 测试不回归）**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_token_rotation.py tests/test_auth/test_token_crypto.py tests/test_auth/test_scm_token_store.py -v
```
预期：新 7 例 + 既有 token_crypto/scm_token_store 全绿（向后兼容：既有测试设单 KE_TOKEN_ENC_KEY 走回退路径）。

- [ ] **Step 5: Commit**

```bash
git add src/service/token_crypto.py tests/test_auth/test_token_rotation.py
git commit -m "feat(crypto): token_crypto 改 MultiFernet 密钥轮换 + rotate_token（向后兼容单 key）（P5c T1）"
```

---

## Task 2: `token_reencrypt` 离线工具 + 全回归

**Files:**
- Create: `src/service/token_reencrypt.py`
- Test: `tests/test_auth/test_token_rotation.py`（追加 reencrypt 测试）

- [ ] **Step 1: 追加失败测试**

```python
import pytest_asyncio
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from src.service.db_models_homepage import Base, GitCredential, UserScmToken


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_reencrypt_all_tokens_migrates_to_primary(maker, monkeypatch):
    from src.service.token_reencrypt import reencrypt_all_tokens
    k_new = Fernet.generate_key().decode()
    k_old = Fernet.generate_key().decode()
    f_old = Fernet(k_old.encode())
    # seed：用 k_old 加密
    async with maker() as s:
        s.add(GitCredential(id="g1", name="c", encrypted_token=f_old.encrypt(b"pat1").decode()))
        s.add(UserScmToken(id="t1", user_id=1, provider="github",
                           access_token=f_old.encrypt(b"at1").decode(),
                           refresh_token=f_old.encrypt(b"rt1").decode(),
                           linked_at=datetime.now(timezone.utc)))
        s.add(UserScmToken(id="t2", user_id=2, provider="github",
                           access_token=f_old.encrypt(b"at2").decode(),
                           refresh_token=None, linked_at=datetime.now(timezone.utc)))
        # 一条损坏密文（任何 key 都解不开）
        s.add(GitCredential(id="bad", name="x", encrypted_token="not-a-valid-fernet-token"))
        await s.commit()
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", f"{k_new},{k_old}")
    monkeypatch.delenv("KE_TOKEN_ENC_KEY", raising=False)
    token_crypto.reset_fernet_cache()
    async with maker() as s:
        counts = await reencrypt_all_tokens(s)
    assert counts == {"git_credentials": 1, "user_scm_token": 2, "errors": 1}
    # 弃旧 key：仅留 k_new，迁移过的行应仍可解；bad 行原密文未动
    monkeypatch.setenv("KE_TOKEN_ENC_KEYS", k_new)
    token_crypto.reset_fernet_cache()
    async with maker() as s:
        g1 = (await s.execute(select(GitCredential).where(GitCredential.id == "g1"))).scalar_one()
        t1 = (await s.execute(select(UserScmToken).where(UserScmToken.id == "t1"))).scalar_one()
        t2 = (await s.execute(select(UserScmToken).where(UserScmToken.id == "t2"))).scalar_one()
        assert token_crypto.decrypt_token(g1.encrypted_token) == "pat1"
        assert token_crypto.decrypt_token(t1.access_token) == "at1"
        assert token_crypto.decrypt_token(t1.refresh_token) == "rt1"
        assert token_crypto.decrypt_token(t2.access_token) == "at2"
        assert t2.refresh_token is None
```

- [ ] **Step 2: 跑红**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_token_rotation.py -k reencrypt -v
```
预期：FAIL（`No module named token_reencrypt`）。

- [ ] **Step 3: 实现（新建 `src/service/token_reencrypt.py`）**

```python
"""离线 re-encrypt：把所有加密 token 迁到当前主 key（MultiFernet.rotate）。
入口 python -m src.service.token_reencrypt。轮换流程见 P5c spec §4.3。
注意：对已部署生产库运行（KE_DB_URL 指向生产）；本 CLI 不建表/不跑迁移。"""
from __future__ import annotations

import logging

from sqlalchemy import select

from src.service.db_models_homepage import GitCredential, UserScmToken
from src.service.token_crypto import rotate_token

_log = logging.getLogger(__name__)


async def reencrypt_all_tokens(session) -> dict:
    """遍历 git_credentials + user_scm_token，逐条 rotate 到主 key。
    解不开的行 log+skip+计 error，不崩不丢行。返回计数 dict。"""
    counts = {"git_credentials": 0, "user_scm_token": 0, "errors": 0}
    for cred in (await session.execute(select(GitCredential))).scalars().all():
        try:
            cred.encrypted_token = rotate_token(cred.encrypted_token)
            counts["git_credentials"] += 1
        except ValueError:
            counts["errors"] += 1
            _log.warning("reencrypt skip git_credential id=%s（密文解不开）", cred.id)
    for tok in (await session.execute(select(UserScmToken))).scalars().all():
        try:
            tok.access_token = rotate_token(tok.access_token)
            if tok.refresh_token:
                tok.refresh_token = rotate_token(tok.refresh_token)
            counts["user_scm_token"] += 1
        except ValueError:
            counts["errors"] += 1
            _log.warning("reencrypt skip user_scm_token id=%s（密文解不开）", tok.id)
    await session.commit()
    return counts


def _main() -> None:  # pragma: no cover — 进程入口
    import asyncio
    from src.service.db import get_session_maker
    maker = get_session_maker()
    async def _run():
        async with maker() as s:
            print(await reencrypt_all_tokens(s))
    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    _main()
```

- [ ] **Step 4: 跑绿**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth/test_token_rotation.py -v
```
预期：T1(6/7) + T2(1) 全绿。

- [ ] **Step 5: import 冒烟 + 全量回归**

```bash
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -c "import src.service.token_reencrypt; import src.service.api; print('import ok')"
KE_JWT_SECRET=test KE_COOKIE_SECURE=false KE_TOKEN_ENC_KEY=qQLkZNq52TbxWwHJ4Vf6soeJ-wBmmGDY53WnJLUZaC4= ./venv/bin/python -m pytest tests/test_auth -q
```
预期：import ok；全量绿（基线 1095[P5a，本栈式分支不含 P5b] passed + 本片新增 8 = 约 1103）。**贴最终 passed 数**；既有 token_crypto/scm_token_store/credentials/admin 测试不回归（向后兼容单 key 路径）。

- [ ] **Step 6: Commit**

```bash
git add src/service/token_reencrypt.py tests/test_auth/test_token_rotation.py
git commit -m "feat(crypto): token_reencrypt 离线密钥迁移工具（rotate 到主 key）（P5c T2）"
```

---

## 验收标准（P5c Done）
1. 向后兼容：仅 KE_TOKEN_ENC_KEY → round-trip OK，既有 token 测试绿。
2. MultiFernet：encrypt 用主 key；decrypt 试所有；key 缺失→ValueError；空逗号项过滤；全空→RuntimeError。
3. `rotate_token` 把旧 key 密文迁到主 key（单独主 key 可解）。
4. `reencrypt_all_tokens` 迁 git_credentials + user_scm_token（access+refresh），计数正确，损坏行 skip+error 不崩不丢行。
5. CLI `python -m src.service.token_reencrypt` import 冒烟。
6. 对外 encrypt_token/decrypt_token 签名不变、调用方零改动；全量 test_auth 绿。

## 自审记录（writing-plans self-review）
- **Spec 覆盖**：MultiFernet+rotate_token→T1；reencrypt 工具+CLI→T2；§6 全部用例（向后兼容/主key encrypt/多key decrypt+缺失/rotate/空→RuntimeError/空白过滤/reencrypt+损坏行）映射到 T1/T2。✅
- **类型一致**：token_crypto API（encrypt/decrypt 签名不变）、GitCredential/UserScmToken 类名+字段（encrypted_token/access_token/refresh_token[nullable]/linked_at[not null 须 seed]）、MultiFernet 47.0.0 语义、reset_fernet_cache 名保留清新缓存——均按 worktree 实际 + 评审实证。✅
- **占位符**：无 TBD。✅
- **风险点**：(a) I1/I2 已写全 token_reencrypt 的 logging+_log+import（评审：否则损坏行 except NameError 崩）；(b) 删 _get_fernet 安全（外部零引用，评审实证）；(c) 测试 reset_fernet_cache 必跟在每次 setenv 后（autouse fixture + 各用例显式）；(d) UserScmToken seed 须给 linked_at（not null 无默认）。
