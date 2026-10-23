from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.auth import require_page_auth


router = APIRouter(include_in_schema=False, dependencies=[Depends(require_page_auth)])
templates = Jinja2Templates(directory="app/templates")


@router.get("/pricing", response_class=HTMLResponse)
def pricing_workbench(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "pricing.html", {"request": request})
