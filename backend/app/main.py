from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import admin_stats, deploy, logs
from app.core.config import settings
from app.core.schema import ensure_schema
from app.models import deploy as deploy_models  # noqa: F401
from app.models import log as log_models  # noqa: F401
from app.models import stats as stats_models  # noqa: F401


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_schema()
    yield


app = FastAPI(
    title="Quick Build WP",
    description="一键搭建 WordPress 环境",
    version="0.1.7",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(deploy.router)
app.include_router(logs.router)
app.include_router(admin_stats.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
