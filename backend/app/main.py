"""智能采集质量管控平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.persistence import bootstrap
from app.routers import ROUTERS
from app.store import store

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 构建与依赖安装完成后才执行迁移；失败不阻断启动，状态可经接口查询
    status = bootstrap.bootstrap()
    logger.info("审计记录持久化迁移状态：%s", status)
    yield


app = FastAPI(title=settings.app_name, version="1.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
