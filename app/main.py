from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import auth, disks

BASE_DIR = os.path.dirname(__file__)

app = FastAPI(title="NAS Manager")

secret_key = os.environ.get("SESSION_SECRET_KEY")
if not secret_key:
    raise RuntimeError(
        "SESSION_SECRET_KEY manquant. Ce fichier doit etre genere par "
        "install.sh dans /opt/nas-manager/.env"
    )
app.add_middleware(SessionMiddleware, secret_key=secret_key, https_only=False)

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def require_login(request: Request) -> str:
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return username


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if request.session.get("username"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if auth.authenticate(username, password):
        request.session["username"] = username
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Identifiants invalides ou compte non autorise."},
        status_code=401,
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, username: str = Depends(require_login)):
    disk_list = disks.list_disks()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "username": username, "disks": disk_list},
    )


@app.get("/api/disks")
def api_disks(username: str = Depends(require_login)):
    return [asdict(d) for d in disks.list_disks()]
