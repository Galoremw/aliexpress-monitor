from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.router import router
from app.core.config import get_settings
from app.scheduler import create_scheduler
from app.dashboard.router import router as dashboard_router

settings = get_settings()
allowed_frontend_origins = [
    origin.strip() for origin in settings.frontend_allowed_origins.split(",") if origin.strip()
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = create_scheduler(settings)
    app.state.scheduler = scheduler
    if settings.scheduler_enabled:
        scheduler.start()
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_frontend_origins + ["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_origin_regex=settings.extension_allowed_origin_regex,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type"],
)
app.include_router(router)
app.include_router(dashboard_router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
