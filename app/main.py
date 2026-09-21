from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.api.router import router
from app.api.auth import router as auth_router
from app.core.config import get_settings
from app.db.session import get_session_factory
from app.core.auth import ensure_bootstrap_admin
from app.scheduler import create_scheduler
from app.dashboard.router import router as dashboard_router

settings = get_settings()
# Keep the first-party local and Pages entry points available even when a
# deployment still has an older FRONTEND_ALLOWED_ORIGINS secret configured.
configured_frontend_origins = [
    origin.strip() for origin in settings.frontend_allowed_origins.split(",") if origin.strip()
]
allowed_frontend_origins = list(
    dict.fromkeys(
        configured_frontend_origins
        + [
            "https://galoremw.github.io",
            "http://127.0.0.1:3000",
            "http://localhost:3000",
        ]
    )
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    with get_session_factory()() as db:
        ensure_bootstrap_admin(db)
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
templates = Jinja2Templates(directory="app/templates")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_frontend_origins + ["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_origin_regex=settings.extension_allowed_origin_regex,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
)
app.include_router(router)
app.include_router(auth_router)
app.include_router(dashboard_router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html")


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
