# src/service/webhook_router.py
"""GitHub webhook 接收：验签→解析→（绑定分支）去重入队→快速 200。设计 §4.4/§9。"""
from __future__ import annotations

import json  # Fix 3: 提到模块顶层，与 stdlib 同层，避免函数内反复 import
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from src.service.db_models_homepage import Project
from src.service.indexing.queue import enqueue_index_job
from src.service.scm.webhook_verify import verify_signature, parse_push


def create_webhook_routes(*, get_db: Callable, webhook_secret: str) -> APIRouter:
    router = APIRouter(tags=["webhook"])

    @router.post("/webhooks/github")
    async def github_webhook(request: Request, db=Depends(get_db)) -> dict:
        body = await request.body()
        sig = request.headers.get("X-Hub-Signature-256")
        if not verify_signature(webhook_secret, body, sig):
            raise HTTPException(status_code=401, detail="签名校验失败")
        event = request.headers.get("X-GitHub-Event", "")
        delivery = request.headers.get("X-GitHub-Delivery")
        if event != "push":
            return {"ok": True, "ignored": event}     # 仅处理 push（create/installation 后续）
        # Fix 2: 恶意/截断 body 会抛 JSONDecodeError；GitHub 对 4xx 不重试，对 5xx 会轮询重试（retry storm）
        try:
            ev = parse_push(json.loads(body))
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="无效 JSON body")
        # 找绑定了该 repo + 该分支的工程
        projects = (await db.execute(
            select(Project).where(Project.repo_external_id == ev.repo_external_id, Project.ref == ev.ref)
        )).scalars().all()
        enqueued = 0
        for p in projects:
            # Fix 1: dedup_key 必须含 project_id，否则同一 delivery 发给多工程时
            # 第 2 个工程的 job 会被错误去重（enqueue_index_job 仅以 dedup_key 做唯一键）
            await enqueue_index_job(db, project_id=p.id, type_="incremental",
                                    trigger="webhook",
                                    dedup_key=f"{delivery}:{p.id}" if delivery else None,
                                    commit_sha=ev.after_sha)
            enqueued += 1
        await db.commit()
        return {"ok": True, "enqueued": enqueued}      # 快速 200；重活在 worker

    return router
