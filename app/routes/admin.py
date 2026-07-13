import csv
import io
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from ..schema import load_schema
from ..settings import settings
from ..storage import (
    authenticate,
    create_participant,
    dashboard_stats,
    delete_session,
    export_rows,
    list_participants,
)
from .deps import csrf_token, redirect, render, require_admin, valid_csrf


router = APIRouter(prefix="/admin")


@router.get("")
def dashboard(request: Request, question_type: str = "1", filter_field: str = "nofilter"):
    admin = require_admin(request)
    if not admin:
        return redirect("/admin/login")
    stats = dashboard_stats(question_type, filter_field)
    return render(
        request,
        "admin_dashboard.html",
        {"title": "Admin Dashboard", "stats": stats},
    )


@router.get("/login")
def login_form(request: Request):
    if require_admin(request):
        return redirect("/admin")
    token = secrets.token_urlsafe(32)
    response = render(request, "admin_login.html", {"title": "Admin Login", "login_csrf": token})
    response.set_cookie(
        settings.login_csrf_cookie,
        token,
        httponly=True,
        samesite="strict",
        secure=settings.cookie_secure,
        max_age=600,
    )
    return response


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    login_csrf: str = Form(...),
):
    expected_csrf = request.cookies.get(settings.login_csrf_cookie, "")
    if not expected_csrf or not secrets.compare_digest(expected_csrf, login_csrf):
        return render(
            request,
            "admin_login.html",
            {"title": "Admin Login", "flash": "Invalid form token. Please reload and try again.", "login_csrf": ""},
            status_code=403,
        )
    token = authenticate(username, password)
    if not token:
        return render(
            request,
            "admin_login.html",
            {
                "title": "Admin Login",
                "flash": "Invalid username or password.",
                "login_csrf": login_csrf,
            },
        )
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        settings.session_cookie,
        token,
        httponly=True,
        samesite="strict",
        secure=settings.cookie_secure,
        max_age=settings.session_ttl_seconds,
    )
    response.delete_cookie(settings.login_csrf_cookie)
    return response


@router.post("/logout")
def logout(request: Request, csrf: str = Form(...)):
    if not require_admin(request) or not valid_csrf(request, csrf):
        return redirect("/admin/login")
    token = request.cookies.get(settings.session_cookie)
    delete_session(token)
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(settings.session_cookie)
    return response


@router.get("/participants")
def participants(request: Request):
    admin = require_admin(request)
    if not admin:
        return redirect("/admin/login")
    return render(
        request,
        "admin_participants.html",
        {"title": "Participants", "participants": list_participants()},
    )


@router.post("/participants")
def add_participant(
    request: Request,
    language: str = Form("en"),
    record_id: str = Form(""),
    csrf: str = Form(...),
):
    admin = require_admin(request)
    if not admin:
        return redirect("/admin/login")
    if not valid_csrf(request, csrf):
        return render(
            request,
            "admin_participants.html",
            {"title": "Participants", "participants": list_participants(), "flash": "Invalid form token. Please try again."},
        )
    if language not in ("en", "es"):
        language = "en"
    try:
        create_participant(language=language, record_id=record_id.strip() or None)
    except Exception as exc:
        return render(
            request,
            "admin_participants.html",
            {
                "title": "Participants",
                "participants": list_participants(),
                "flash": f"Could not create participant: {exc}",
            },
        )
    return redirect("/admin/participants")


@router.get("/export.csv")
def export_csv(request: Request):
    admin = require_admin(request)
    if not admin:
        return redirect("/admin/login")
    schema = load_schema()
    rows = export_rows()
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=schema.export_columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="rpps_export.csv"'},
    )
