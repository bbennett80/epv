import secrets

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..settings import settings
from ..storage import get_admin_by_session


templates = Jinja2Templates(directory=settings.template_dir)


def render(request: Request, template: str, context: dict, status_code: int = 200):
    admin_user = get_admin_by_session(request.cookies.get(settings.session_cookie))
    context = {
        "request": request,
        "admin_user": admin_user,
        "csrf_token": csrf_token(request),
        **context,
    }
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def require_admin(request: Request):
    admin = get_admin_by_session(request.cookies.get(settings.session_cookie))
    if not admin:
        return None
    return admin


def csrf_token(request: Request) -> str:
    return request.cookies.get(settings.session_cookie, "")


def valid_csrf(request: Request, submitted_token: str) -> bool:
    expected = csrf_token(request)
    return bool(expected and secrets.compare_digest(expected, submitted_token))


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)
