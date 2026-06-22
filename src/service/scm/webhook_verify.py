# src/service/scm/webhook_verify.py
"""GitHub webhook HMAC 验签 + push 解析。设计 §4.4。"""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from src.service.scm.base import WebhookEvent


def verify_signature(secret: str, body: bytes, header_sig: Optional[str]) -> bool:
    """常量时间比较 X-Hub-Signature-256（'sha256=...'）。secret 空 / header 缺 → False。"""
    if not secret or not header_sig or not header_sig.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_sig)


def parse_push(payload: dict) -> WebhookEvent:
    """push 事件 → 归一化 WebhookEvent。ref 去 refs/heads/ 前缀。"""
    ref = payload.get("ref", "")
    branch = ref.split("refs/heads/", 1)[-1] if ref.startswith("refs/heads/") else ref
    return WebhookEvent(
        event_type="push", ref=branch, after_sha=payload.get("after"),
        repo_external_id=(payload.get("repository") or {}).get("id"),
    )
