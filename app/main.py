from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import auth, disks, zfs

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
    warning = None
    if not disk_list:
        warning = (
            "Aucun disque detecte. Soit ce serveur n'a reellement aucun "
            "disque visible, soit la commande 'lsblk' a echoue cote "
            "systeme. Verifie les journaux avec : "
            "journalctl -u nas-manager -n 50 --no-pager"
        )
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "username": username, "disks": disk_list, "warning": warning},
    )


@app.get("/api/disks")
def api_disks(username: str = Depends(require_login)):
    return [asdict(d) for d in disks.list_disks()]


# ---------------------------------------------------------------------------
# Pools ZFS
# ---------------------------------------------------------------------------

@app.get("/pools", response_class=HTMLResponse)
def pools_list(request: Request, username: str = Depends(require_login)):
    pool_list = zfs.list_pools()
    return templates.TemplateResponse(
        "pools.html",
        {"request": request, "username": username, "pools": pool_list},
    )


@app.get("/pools/new", response_class=HTMLResponse)
def pool_new_form(request: Request, username: str = Depends(require_login)):
    available = disks.get_available_disks()
    return templates.TemplateResponse(
        "pool_new.html",
        {
            "request": request,
            "username": username,
            "available_disks": [asdict(d) for d in available],
            "vdev_min": zfs.VDEV_MIN_DISKS,
            "vdev_labels": zfs.VDEV_LABELS,
        },
    )


def _parse_disk_list(raw: str) -> list[str]:
    """Le formulaire envoie les disques choisis sous forme de chaine
    separee par des virgules (rempli en JS a partir des cases cochees)."""
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


@app.post("/api/zfs/plan")
async def api_zfs_plan(request: Request, username: str = Depends(require_login)):
    """Validation en direct (appelee en AJAX pendant que l'utilisateur
    remplit le formulaire) : renvoie erreurs/avertissements/commande sans
    rien creer. Revalide integralement cote serveur, comme la creation
    reelle - les memes regles s'appliquent partout."""
    body = await request.json()
    check = zfs.validate_pool_plan(
        name=body.get("name", ""),
        vdev_type=body.get("vdev_type", ""),
        main_disks=body.get("main_disks", []),
        special_disks=body.get("special_disks", []),
        log_disks=body.get("log_disks", []),
        cache_disks=body.get("cache_disks", []),
    )
    return JSONResponse({
        "errors": check.errors,
        "warnings": check.warnings,
        "command_preview": check.command_preview,
        "can_create": check.can_create,
    })


@app.post("/pools", response_class=HTMLResponse)
def pool_create(
    request: Request,
    username: str = Depends(require_login),
    name: str = Form(...),
    confirm_name: str = Form(...),
    vdev_type: str = Form(...),
    main_disks: str = Form(""),
    special_disks: str = Form(""),
    log_disks: str = Form(""),
    cache_disks: str = Form(""),
):
    main_list = _parse_disk_list(main_disks)
    special_list = _parse_disk_list(special_disks)
    log_list = _parse_disk_list(log_disks)
    cache_list = _parse_disk_list(cache_disks)

    if confirm_name.strip() != name.strip():
        check = zfs.PoolPlanCheck(errors=[
            "Le nom tape pour confirmer ne correspond pas au nom du pool - rien n'a ete cree."
        ])
        return templates.TemplateResponse(
            "pool_result.html",
            {"request": request, "username": username, "success": False, "check": check, "pool_name": name},
            status_code=400,
        )

    check = zfs.validate_pool_plan(name, vdev_type, main_list, special_list, log_list, cache_list)
    if not check.can_create:
        return templates.TemplateResponse(
            "pool_result.html",
            {"request": request, "username": username, "success": False, "check": check, "pool_name": name},
            status_code=400,
        )

    try:
        output = zfs.create_pool(name, vdev_type, main_list, special_list, log_list, cache_list)
    except zfs.PoolCreationError as exc:
        check.errors.append(str(exc))
        return templates.TemplateResponse(
            "pool_result.html",
            {"request": request, "username": username, "success": False, "check": check, "pool_name": name},
            status_code=500,
        )

    return templates.TemplateResponse(
        "pool_result.html",
        {"request": request, "username": username, "success": True, "check": check, "pool_name": name, "output": output},
    )


@app.get("/pools/{name}/delete", response_class=HTMLResponse)
def pool_delete_form(request: Request, name: str, username: str = Depends(require_login)):
    pool = zfs.get_pool(name)
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Pool '{name}' introuvable.")
    return templates.TemplateResponse(
        "pool_delete.html",
        {"request": request, "username": username, "pool": pool, "error": None},
    )


@app.post("/pools/{name}/delete", response_class=HTMLResponse)
def pool_delete_submit(
    request: Request,
    name: str,
    username: str = Depends(require_login),
    confirm_name: str = Form(...),
):
    pool = zfs.get_pool(name)
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Pool '{name}' introuvable.")

    if confirm_name.strip() != name.strip():
        return templates.TemplateResponse(
            "pool_delete.html",
            {
                "request": request, "username": username, "pool": pool,
                "error": "Le nom tape ne correspond pas au nom du pool - rien n'a ete supprime.",
            },
            status_code=400,
        )

    try:
        zfs.destroy_pool(name)
    except zfs.PoolDestructionError as exc:
        return templates.TemplateResponse(
            "pool_delete.html",
            {"request": request, "username": username, "pool": pool, "error": str(exc)},
            status_code=500,
        )

    return RedirectResponse("/pools", status_code=302)
