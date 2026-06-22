"""SCM 仓权限缓存：仅缓存正向 grant 的 can_query 结果（TTL）。设计 §4.6。
can_bind 不经此（裸调 resolve_repo_role）；deny 不缓存（吊销后不长期放行）。per-process。"""
from __future__ import annotations

# time 是标准库模块，用于获取当前时间戳（Unix epoch 浮点秒）
import time
# Optional 表示类型可能为 None（Python 3.10+ 可用 X | None，此处保持兼容写法）
from typing import Optional

# 导入业务层角色枚举，缓存逻辑只认 ScmRole，不依赖具体 provider 实现
from src.service.scm.base import ScmRole

# 正向缓存 TTL（秒）：5 分钟内同一用户+连接+仓库不重复打 SCM API
_TTL = 300

# 进程内缓存字典（模块级单例，非线程安全，但 async 事件循环单线程调度足够）
# key=(user_id, connection_id, repo_external_id) → (ScmRole, expires_at_epoch_float)
# connection_id 全局唯一，已隐含 provider 类型，无需额外字段
_CACHE: dict[tuple, tuple] = {}


def _now() -> float:
    """返回当前 Unix 时间戳（浮点秒）。提取为独立函数便于测试时 monkeypatch 替换。"""
    # time.time() 返回自 1970-01-01 00:00:00 UTC 以来的秒数（浮点）
    return time.time()


def cache_clear() -> None:
    """清空全部缓存条目。测试 autouse fixture 用，确保各用例隔离。"""
    # dict.clear() 原位清空，保持对象引用不变（比重新赋值 _CACHE={} 更安全）
    _CACHE.clear()


def cache_invalidate(connection_id: str) -> None:
    """清除某连接的全部缓存项（token 轮换 / 连接删除 / re-link 时由上层调）。

    Args:
        connection_id: ScmConnection.id，全局唯一，匹配缓存 key[1]。
    """
    # 先收集要删除的 key 列表，再删除——不能在迭代 dict 时原位删（RuntimeError）
    # 列表推导式：[表达式 for 变量 in 可迭代 if 条件]
    keys_to_remove = [k for k in _CACHE if k[1] == connection_id]
    for k in keys_to_remove:
        # pop(k, None)：若 key 不存在也不抛异常（防并发竞态）
        _CACHE.pop(k, None)


def _gc(now: float) -> None:
    """机会式清理过期条目，限制无界增长。每次 resolve 时顺带执行。

    Args:
        now: 当前时间戳（由调用方传入，避免重复调用 _now()）。
    """
    # 先快照过期 key，再删除——同样不能边迭代边删
    # _CACHE.items() 返回 (key, value) 视图；这里 value 解包为 (_, exp)
    expired = [k for k, (_, exp) in _CACHE.items() if exp < now]
    for k in expired:
        _CACHE.pop(k, None)


async def resolve_repo_role_cached(
    provider_obj,
    *,
    user_id: int,
    connection_id: str,
    repo_external_id,
    token: str,
    repo,
    principal,
) -> ScmRole:
    """带缓存的仓权限解析（can_query 路径用）。

    仅缓存正向 CAN_QUERY：
    - CAN_BIND 永不缓存（A3：bind 门用裸调，避免绑定权限被缓存放行低权用户）
    - NOT_VISIBLE 不缓存（吊销后不长期放行）

    Args:
        provider_obj: 具体 SCM provider 实例（需有 async resolve_repo_role 方法）。
        user_id:          KE 内部用户 ID（缓存 key 组成部分）。
        connection_id:    ScmConnection.id，全局唯一，隐含 provider 类型。
        repo_external_id: 仓库 numeric id（GitHub: repo.id / GitLab: project.id）。
        token:            用户 SCM access token，透传给 provider。
        repo:             仓库 full_name（owner/repo），透传给 provider。
        principal:        SCM login / user id，透传给 provider。

    Returns:
        ScmRole 枚举值（CAN_BIND / CAN_QUERY / NOT_VISIBLE）。
    """
    # 获取当前时间（单次调用，后续复用）
    now = _now()

    # 顺带清理已过期条目（机会式 GC，不阻塞主逻辑）
    _gc(now)

    # 构造缓存 key：三元组唯一标识一个「用户 × 连接 × 仓库」的权限查询
    key = (user_id, connection_id, repo_external_id)

    # 尝试从缓存读取：_CACHE.get(key) 不存在返回 None
    hit = _CACHE.get(key)

    # 命中且未过期（expires_at >= now）则直接返回缓存结果，不打 SCM API
    if hit is not None and hit[1] >= now:
        return hit[0]  # hit = (ScmRole, expires_at)

    # 缓存未命中或已过期：调用 provider 的异步权限解析方法
    # await 挂起当前协程，等 provider 完成 HTTP 请求后恢复
    role = await provider_obj.resolve_repo_role(token=token, repo=repo, principal=principal)

    # 仅在角色为 CAN_QUERY 时写入缓存
    # CAN_BIND 永不缓存（A3）：bind 门总是裸调，确保每次严格核验
    # NOT_VISIBLE 不缓存：权限被撤销后应立即生效，不因缓存继续放行
    if role == ScmRole.CAN_QUERY:
        # 写入缓存：value = (role, 过期时间戳)
        _CACHE[key] = (role, now + _TTL)

    return role
