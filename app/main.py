from __future__ import annotations

import os
import subprocess
from dataclasses import asdict

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import auth, disks, zfs, sysstats, smart as smart_module, replace_workflow, shares, nasusers, dockerstacks

BASE_DIR = os.path.dirname(__file__)


def _env_flag(name: str, default: bool = False) -> bool:
    """Lit une variable d'environnement booleenne (.env) de facon tolerante :
    '1'/'true'/'yes'/'on' (insensible a la casse) valent vrai, tout le reste
    (y compris absent) retombe sur `default`."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


app = FastAPI(title="NAS Manager")

secret_key = os.environ.get("SESSION_SECRET_KEY")
if not secret_key:
    raise RuntimeError(
        "SESSION_SECRET_KEY manquant. Ce fichier doit etre genere par "
        "install.sh dans /opt/nas-manager/.env"
    )
# En production, install.sh sert l'interface en HTTPS (certificat auto-signe)
# et positionne SESSION_HTTPS_ONLY=true dans .env : le cookie de session
# n'est alors jamais envoye en clair. Reste a false par defaut (dev local
# sans TLS via `uvicorn --reload`, cf. README).
app.add_middleware(SessionMiddleware, secret_key=secret_key, https_only=_env_flag("SESSION_HTTPS_ONLY", False))

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


FILL_WARNING_PCT = 75
FILL_CRITICAL_PCT = 90


def _fill_alert_level(percent: float) -> str:
    if percent >= FILL_CRITICAL_PCT:
        return "critical"
    if percent >= FILL_WARNING_PCT:
        return "warning"
    return "ok"


def _replacement_context() -> tuple[replace_workflow.ReplacementState | None, str | None]:
    state = replace_workflow.load_state()
    if state is None:
        return None, None
    return state, replace_workflow.STEP_LABELS.get(state.step, state.step)


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
    replacement_state, step_label = _replacement_context()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request, "username": username, "disks": disk_list, "warning": warning,
            "replacement_state": replacement_state, "step_label": step_label,
        },
    )


@app.get("/api/disks")
def api_disks(username: str = Depends(require_login)):
    return [asdict(d) for d in disks.list_disks()]


@app.get("/partials/sysstats", response_class=HTMLResponse)
def partial_sysstats(request: Request, username: str = Depends(require_login)):
    stats = sysstats.get_system_stats()
    pool_list = zfs.list_pools()
    pools_with_alerts = [
        {"pool": p, "alert": _fill_alert_level(p.used_percent)} for p in pool_list
    ]
    return templates.TemplateResponse(
        "_sysstats_partial.html",
        {
            "request": request, "stats": stats,
            "uptime_label": sysstats.format_uptime(stats.uptime_seconds),
            "pools_with_alerts": pools_with_alerts,
        },
    )


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


# IMPORTANT : cette route generique "/pools/{name}" doit toujours rester
# declaree APRES toutes les routes GET /pools/... plus specifiques
# (/pools/new en particulier) - FastAPI fait correspondre les routes dans
# leur ordre de declaration, et {name} matcherait sinon "new" comme un nom
# de pool et rendrait /pools/new inaccessible.
@app.get("/pools/{name}", response_class=HTMLResponse)
def pool_detail(request: Request, name: str, username: str = Depends(require_login)):
    pool = zfs.get_pool(name)
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Pool '{name}' introuvable.")
    replacement_state, step_label = _replacement_context()
    return templates.TemplateResponse(
        "pool_detail.html",
        {
            "request": request, "username": username, "pool": pool,
            "replacement_state": replacement_state, "step_label": step_label,
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


# ---------------------------------------------------------------------------
# Etat SMART des disques
# ---------------------------------------------------------------------------

@app.get("/disks/smart", response_class=HTMLResponse)
def disks_smart_overview(request: Request, username: str = Depends(require_login)):
    disk_list = disks.list_disks()
    reports = [{"disk": d, "report": smart_module.get_smart_report(d.path)} for d in disk_list]
    return templates.TemplateResponse(
        "disks_smart.html",
        {"request": request, "username": username, "reports": reports},
    )


@app.get("/disks/{name}/smart", response_class=HTMLResponse)
def disk_smart_detail(request: Request, name: str, username: str = Depends(require_login)):
    all_disks = {d.name: d for d in disks.list_disks()}
    disk = all_disks.get(name)
    if disk is None:
        raise HTTPException(status_code=404, detail=f"Disque '{name}' introuvable.")
    report = smart_module.get_smart_report(disk.path)
    return templates.TemplateResponse(
        "disk_smart_detail.html",
        {
            "request": request, "username": username, "disk": disk, "report": report,
            "glossary": smart_module.GLOSSARY,
        },
    )


# ---------------------------------------------------------------------------
# Remplacement guide d'un disque
# ---------------------------------------------------------------------------
#
# Regle de securite absolue (rappel general du projet) : la preservation
# des donnees prime sur tout. Chaque action ici revalide en direct l'etat
# reel du pool et des disques - jamais de confiance dans ce que le
# formulaire ou l'etat persiste racontent, ils ne servent qu'a guider
# l'utilisateur.

@app.get("/pools/{name}/disks/{disk_name}/replace", response_class=HTMLResponse)
def replace_intro(request: Request, name: str, disk_name: str, username: str = Depends(require_login)):
    pool = zfs.get_pool(name)
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Pool '{name}' introuvable.")

    existing_state = replace_workflow.load_state()
    if existing_state is not None:
        return RedirectResponse("/replacement", status_code=302)

    disk_path = f"/dev/{disk_name}"
    check = zfs.plan_disk_replacement(name, disk_path)
    if not check.can_proceed:
        raise HTTPException(status_code=404, detail=" / ".join(check.errors))

    all_disks = {d.name: d for d in disks.list_disks()}
    disk_info = all_disks.get(disk_name)

    return templates.TemplateResponse(
        "replace_intro.html",
        {
            "request": request, "username": username, "pool": pool,
            "disk_name": disk_name, "disk_path": disk_path, "disk_info": disk_info,
            "current_state": pool.disk_states.get(disk_path, "inconnu"),
            "check": check, "error": None,
        },
    )


@app.post("/pools/{name}/disks/{disk_name}/replace/start", response_class=HTMLResponse)
def replace_start(request: Request, name: str, disk_name: str, username: str = Depends(require_login)):
    pool = zfs.get_pool(name)
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Pool '{name}' introuvable.")

    if replace_workflow.load_state() is not None:
        return RedirectResponse("/replacement", status_code=302)

    disk_path = f"/dev/{disk_name}"
    check = zfs.plan_disk_replacement(name, disk_path)
    if not check.can_proceed:
        all_disks = {d.name: d for d in disks.list_disks()}
        return templates.TemplateResponse(
            "replace_intro.html",
            {
                "request": request, "username": username, "pool": pool,
                "disk_name": disk_name, "disk_path": disk_path,
                "disk_info": all_disks.get(disk_name),
                "current_state": pool.disk_states.get(disk_path, "inconnu"),
                "check": check, "error": " / ".join(check.errors),
            },
            status_code=400,
        )

    all_disks = {d.name: d for d in disks.list_disks()}
    disk_info = all_disks.get(disk_name)

    try:
        zfs.offline_disk(name, disk_path)
    except zfs.ReplacementError as exc:
        return templates.TemplateResponse(
            "replace_intro.html",
            {
                "request": request, "username": username, "pool": pool,
                "disk_name": disk_name, "disk_path": disk_path, "disk_info": disk_info,
                "current_state": pool.disk_states.get(disk_path, "inconnu"),
                "check": check, "error": str(exc),
            },
            status_code=500,
        )

    replace_workflow.start_replacement(
        pool=name, old_disk=disk_path,
        serial=disk_info.serial if disk_info else None,
        model=disk_info.model if disk_info else None,
    )
    return RedirectResponse("/replacement", status_code=302)


@app.get("/replacement", response_class=HTMLResponse)
def replacement_hub(request: Request, username: str = Depends(require_login), error: str | None = None):
    state = replace_workflow.load_state()
    if state is None:
        return RedirectResponse("/pools", status_code=302)

    available_disks = [asdict(d) for d in disks.get_available_disks()] if state.step == replace_workflow.STEP_AWAITING_NEW_DISK else []
    check_warnings: list[str] = []
    if state.step == replace_workflow.STEP_OFFLINED:
        check_warnings = zfs.plan_disk_replacement(state.pool, state.old_disk).warnings

    return templates.TemplateResponse(
        "replacement_hub.html",
        {
            "request": request, "username": username, "state": state,
            "available_disks": available_disks, "check_warnings": check_warnings,
            "error": error,
        },
    )


@app.post("/replacement/continue")
def replacement_continue(username: str = Depends(require_login)):
    state = replace_workflow.load_state()
    if state is None or state.step != replace_workflow.STEP_OFFLINED:
        return RedirectResponse("/replacement", status_code=302)
    replace_workflow.advance_to_disk_selection(state)
    return RedirectResponse("/replacement", status_code=302)


@app.post("/replacement/cancel")
def replacement_cancel(username: str = Depends(require_login)):
    state = replace_workflow.load_state()
    if state is None:
        return RedirectResponse("/pools", status_code=302)
    if state.step in (replace_workflow.STEP_OFFLINED, replace_workflow.STEP_AWAITING_NEW_DISK):
        try:
            zfs.online_disk(state.pool, state.old_disk)
        except zfs.ReplacementError:
            pass  # le disque est peut-etre physiquement absent (deja retire) - on efface quand meme l'etat
    replace_workflow.clear_state()
    return RedirectResponse(f"/pools/{state.pool}", status_code=302)


@app.get("/replacement/shutdown-confirm", response_class=HTMLResponse)
def replacement_shutdown_confirm(request: Request, username: str = Depends(require_login)):
    state = replace_workflow.load_state()
    if state is None or state.step != replace_workflow.STEP_OFFLINED:
        return RedirectResponse("/replacement", status_code=302)
    return templates.TemplateResponse(
        "replace_shutdown_confirm.html",
        {"request": request, "username": username, "state": state},
    )


@app.post("/replacement/shutdown")
def replacement_shutdown(username: str = Depends(require_login)):
    state = replace_workflow.load_state()
    if state is None or state.step != replace_workflow.STEP_OFFLINED:
        return RedirectResponse("/replacement", status_code=302)
    subprocess.run(["systemctl", "poweroff"], check=False)
    return HTMLResponse(
        "<html><body style='font-family:system-ui;background:#0f172a;color:#e2e8f0;"
        "padding:3rem;text-align:center;'><h1>Arret en cours...</h1>"
        "<p>Le serveur s'eteint. Reconnecte-toi une fois le nouveau disque installe "
        "et le serveur redemarre - l'assistant reprendra automatiquement.</p>"
        "</body></html>"
    )


@app.post("/replacement/select", response_class=HTMLResponse)
def replacement_select(request: Request, username: str = Depends(require_login), new_disk: str = Form(...)):
    state = replace_workflow.load_state()
    if state is None or state.step != replace_workflow.STEP_AWAITING_NEW_DISK:
        return RedirectResponse("/replacement", status_code=302)

    try:
        zfs.replace_disk(state.pool, state.old_disk, new_disk)
    except zfs.ReplacementError as exc:
        available_disks = [asdict(d) for d in disks.get_available_disks()]
        return templates.TemplateResponse(
            "replacement_hub.html",
            {
                "request": request, "username": username, "state": state,
                "available_disks": available_disks, "check_warnings": [],
                "error": str(exc),
            },
            status_code=400,
        )

    replace_workflow.advance_to_resilvering(state, new_disk)
    return RedirectResponse("/replacement", status_code=302)


@app.post("/replacement/finish")
def replacement_finish(username: str = Depends(require_login)):
    replace_workflow.clear_state()
    return RedirectResponse("/pools", status_code=302)


@app.get("/partials/resilver", response_class=HTMLResponse)
def partial_resilver(request: Request, username: str = Depends(require_login)):
    state = replace_workflow.load_state()
    if state is None:
        return HTMLResponse("<div>Aucun remplacement en cours.</div>")

    resilver = zfs.get_resilver_status(state.pool)

    if not resilver.in_progress and state.step == replace_workflow.STEP_RESILVERING:
        # Le resilver est termine (ou n'a jamais demarre a temps pour ce
        # sondage) - on marque l'etape comme terminee, puis on demande a
        # HTMX de recharger la page entiere (pas seulement ce fragment) afin
        # que le hub affiche proprement la section "termine".
        replace_workflow.mark_done(state)
        return HTMLResponse(
            "<div>Resilver termine, actualisation...</div>",
            headers={"HX-Refresh": "true"},
        )

    return templates.TemplateResponse(
        "_resilver_partial.html",
        {"request": request, "resilver": resilver},
    )


# ---------------------------------------------------------------------------
# Comptes de partage (SMB/NFS) - independants des comptes d'administration
# ---------------------------------------------------------------------------

@app.get("/share-users", response_class=HTMLResponse)
def share_users_list(request: Request, username: str = Depends(require_login), error: str | None = None):
    return templates.TemplateResponse(
        "share_users.html",
        {"request": request, "username": username, "users": nasusers.list_share_users(), "error": error},
    )


@app.post("/share-users", response_class=HTMLResponse)
def share_users_create(
    request: Request, username: str = Depends(require_login),
    new_username: str = Form(...), password: str = Form(...),
):
    try:
        nasusers.create_share_user(new_username, password)
    except nasusers.ShareUserError as exc:
        return templates.TemplateResponse(
            "share_users.html",
            {"request": request, "username": username, "users": nasusers.list_share_users(), "error": str(exc)},
            status_code=400,
        )
    return RedirectResponse("/share-users", status_code=302)


@app.post("/share-users/{name}/password", response_class=HTMLResponse)
def share_users_password(
    request: Request, name: str, username: str = Depends(require_login), password: str = Form(...),
):
    try:
        nasusers.set_share_user_password(name, password)
    except nasusers.ShareUserError as exc:
        return templates.TemplateResponse(
            "share_users.html",
            {"request": request, "username": username, "users": nasusers.list_share_users(), "error": str(exc)},
            status_code=400,
        )
    return RedirectResponse("/share-users", status_code=302)


@app.post("/share-users/{name}/delete", response_class=HTMLResponse)
def share_users_delete(request: Request, name: str, username: str = Depends(require_login)):
    try:
        nasusers.delete_share_user(name)
    except nasusers.ShareUserError as exc:
        return templates.TemplateResponse(
            "share_users.html",
            {"request": request, "username": username, "users": nasusers.list_share_users(), "error": str(exc)},
            status_code=400,
        )
    return RedirectResponse("/share-users", status_code=302)


# ---------------------------------------------------------------------------
# Dossiers partages (SMB/NFS)
# ---------------------------------------------------------------------------

@app.get("/shares", response_class=HTMLResponse)
def shares_list(request: Request, username: str = Depends(require_login)):
    return templates.TemplateResponse(
        "shares.html",
        {"request": request, "username": username, "shares": shares.list_shares()},
    )


@app.get("/shares/new", response_class=HTMLResponse)
def share_new_form(request: Request, username: str = Depends(require_login)):
    return templates.TemplateResponse(
        "share_new.html",
        {"request": request, "username": username, "pools": zfs.list_pools(), "error": None},
    )


@app.post("/shares", response_class=HTMLResponse)
def share_create(
    request: Request, username: str = Depends(require_login),
    name: str = Form(...), pool: str = Form(...),
    protocol_smb: str = Form(""), protocol_nfs: str = Form(""),
):
    protocols = []
    if protocol_smb:
        protocols.append("smb")
    if protocol_nfs:
        protocols.append("nfs")

    try:
        share, warnings = shares.create_share(name, pool, protocols)
    except (shares.ShareError, zfs.DatasetError) as exc:
        return templates.TemplateResponse(
            "share_new.html",
            {"request": request, "username": username, "pools": zfs.list_pools(), "error": str(exc)},
            status_code=400,
        )
    return RedirectResponse(f"/shares/{share.name}", status_code=302)


def _render_share_detail(
    request: Request, username: str, name: str,
    error: str | None = None, warnings: list[str] | None = None, status_code: int = 200,
):
    share = shares.get_share(name)
    if share is None:
        raise HTTPException(status_code=404, detail=f"Partage '{name}' introuvable.")

    used_usernames = {u.username for u in share.users}
    available_users = [u for u in nasusers.list_share_users() if u.username not in used_usernames]

    return templates.TemplateResponse(
        "share_detail.html",
        {
            "request": request, "username": username, "share": share,
            "available_users": available_users, "error": error, "warnings": warnings or [],
        },
        status_code=status_code,
    )


# IMPORTANT : declaree APRES /shares/new pour la meme raison que
# /pools/{name} apres /pools/new (voir commentaire plus haut).
@app.get("/shares/{name}", response_class=HTMLResponse)
def share_detail(request: Request, name: str, username: str = Depends(require_login)):
    return _render_share_detail(request, username, name)


@app.post("/shares/{name}/users", response_class=HTMLResponse)
def share_add_user(
    request: Request, name: str, username: str = Depends(require_login),
    share_username: str = Form(...), access: str = Form(...),
):
    try:
        warnings = shares.add_user_to_share(name, share_username, access)
    except shares.ShareError as exc:
        return _render_share_detail(request, username, name, error=str(exc), status_code=400)
    return _render_share_detail(request, username, name, warnings=warnings)


@app.post("/shares/{name}/users/{share_username}/delete", response_class=HTMLResponse)
def share_remove_user(request: Request, name: str, share_username: str, username: str = Depends(require_login)):
    try:
        warnings = shares.remove_user_from_share(name, share_username)
    except shares.ShareError as exc:
        return _render_share_detail(request, username, name, error=str(exc), status_code=400)
    return _render_share_detail(request, username, name, warnings=warnings)


@app.post("/shares/{name}/nfs-networks", response_class=HTMLResponse)
def share_update_nfs_networks(
    request: Request, name: str, username: str = Depends(require_login), networks: str = Form(...),
):
    network_list = [n.strip() for n in networks.split(",") if n.strip()]
    try:
        warnings = shares.update_nfs_networks(name, network_list)
    except shares.ShareError as exc:
        return _render_share_detail(request, username, name, error=str(exc), status_code=400)
    return _render_share_detail(request, username, name, warnings=warnings)


@app.get("/shares/{name}/delete", response_class=HTMLResponse)
def share_delete_form(request: Request, name: str, username: str = Depends(require_login)):
    share = shares.get_share(name)
    if share is None:
        raise HTTPException(status_code=404, detail=f"Partage '{name}' introuvable.")
    return templates.TemplateResponse(
        "share_delete.html",
        {"request": request, "username": username, "share": share, "error": None},
    )


@app.post("/shares/{name}/delete", response_class=HTMLResponse)
def share_delete_submit(
    request: Request, name: str, username: str = Depends(require_login), confirm_name: str = Form(...),
):
    share = shares.get_share(name)
    if share is None:
        raise HTTPException(status_code=404, detail=f"Partage '{name}' introuvable.")

    if confirm_name.strip() != name.strip():
        return templates.TemplateResponse(
            "share_delete.html",
            {
                "request": request, "username": username, "share": share,
                "error": "Le nom tape ne correspond pas au nom du partage - rien n'a ete supprime.",
            },
            status_code=400,
        )

    try:
        shares.delete_share(name)
    except (shares.ShareError, zfs.DatasetError) as exc:
        return templates.TemplateResponse(
            "share_delete.html",
            {"request": request, "username": username, "share": share, "error": str(exc)},
            status_code=500,
        )

    return RedirectResponse("/shares", status_code=302)


# ---------------------------------------------------------------------------
# Stacks Docker Compose
# ---------------------------------------------------------------------------

@app.get("/docker", response_class=HTMLResponse)
def docker_list(request: Request, username: str = Depends(require_login)):
    stacks = dockerstacks.list_stacks()
    rows = []
    for s in stacks:
        try:
            containers = dockerstacks.get_stack_containers(s.name)
        except dockerstacks.DockerStackError:
            containers = []
        rows.append({"stack": s, "containers": containers})
    return templates.TemplateResponse(
        "docker_stacks.html",
        {"request": request, "username": username, "rows": rows},
    )


@app.get("/docker/new", response_class=HTMLResponse)
def docker_new_form(request: Request, username: str = Depends(require_login)):
    return templates.TemplateResponse(
        "docker_new.html",
        {"request": request, "username": username, "pools": zfs.list_pools(), "error": None, "name": "", "compose_content": ""},
    )


@app.post("/docker", response_class=HTMLResponse)
def docker_create(
    request: Request, username: str = Depends(require_login),
    name: str = Form(...), pool: str = Form(...), compose_content: str = Form(...),
):
    try:
        stack, output = dockerstacks.create_stack(name, pool, compose_content)
    except (dockerstacks.DockerStackError, zfs.DatasetError) as exc:
        return templates.TemplateResponse(
            "docker_new.html",
            {
                "request": request, "username": username, "pools": zfs.list_pools(),
                "error": str(exc), "name": name, "compose_content": compose_content,
            },
            status_code=400,
        )
    return RedirectResponse(f"/docker/{stack.name}", status_code=302)


def _render_docker_detail(
    request: Request, username: str, name: str,
    error: str | None = None, message: str | None = None,
    updates: dict[str, str] | None = None, status_code: int = 200,
):
    stack = dockerstacks.get_stack(name)
    if stack is None:
        raise HTTPException(status_code=404, detail=f"Stack '{name}' introuvable.")
    try:
        containers = dockerstacks.get_stack_containers(name)
    except dockerstacks.DockerStackError as exc:
        containers = []
        error = error or str(exc)
    compose_content = dockerstacks.get_compose_content(name)

    return templates.TemplateResponse(
        "docker_detail.html",
        {
            "request": request, "username": username, "stack": stack,
            "containers": containers, "compose_content": compose_content,
            "error": error, "message": message, "updates": updates or {},
        },
        status_code=status_code,
    )


# IMPORTANT : declaree APRES /docker/new (voir explication plus haut pour
# le meme cas avec /pools/{name} et /shares/{name}).
@app.get("/docker/{name}", response_class=HTMLResponse)
def docker_detail(request: Request, name: str, username: str = Depends(require_login)):
    return _render_docker_detail(request, username, name)


@app.post("/docker/{name}/start", response_class=HTMLResponse)
def docker_start(request: Request, name: str, username: str = Depends(require_login)):
    try:
        dockerstacks.start_stack(name)
    except dockerstacks.DockerStackError as exc:
        return _render_docker_detail(request, username, name, error=str(exc), status_code=400)
    return _render_docker_detail(request, username, name, message="Stack demarree.")


@app.post("/docker/{name}/stop", response_class=HTMLResponse)
def docker_stop(request: Request, name: str, username: str = Depends(require_login)):
    try:
        dockerstacks.stop_stack(name)
    except dockerstacks.DockerStackError as exc:
        return _render_docker_detail(request, username, name, error=str(exc), status_code=400)
    return _render_docker_detail(request, username, name, message="Stack arretee.")


@app.post("/docker/{name}/restart", response_class=HTMLResponse)
def docker_restart(request: Request, name: str, username: str = Depends(require_login)):
    try:
        dockerstacks.restart_stack(name)
    except dockerstacks.DockerStackError as exc:
        return _render_docker_detail(request, username, name, error=str(exc), status_code=400)
    return _render_docker_detail(request, username, name, message="Stack redemarree.")


@app.post("/docker/{name}/compose", response_class=HTMLResponse)
def docker_update_compose(
    request: Request, name: str, username: str = Depends(require_login), compose_content: str = Form(...),
):
    try:
        dockerstacks.update_compose_file(name, compose_content)
    except dockerstacks.DockerStackError as exc:
        return _render_docker_detail(request, username, name, error=str(exc), status_code=400)
    return _render_docker_detail(request, username, name, message="Configuration mise a jour et appliquee.")


@app.post("/docker/{name}/check-updates", response_class=HTMLResponse)
def docker_check_updates(request: Request, name: str, username: str = Depends(require_login)):
    try:
        updates = dockerstacks.check_stack_updates(name)
    except dockerstacks.DockerStackError as exc:
        return _render_docker_detail(request, username, name, error=str(exc), status_code=400)
    return _render_docker_detail(request, username, name, updates=updates)


@app.post("/docker/{name}/update", response_class=HTMLResponse)
def docker_apply_update(request: Request, name: str, username: str = Depends(require_login)):
    try:
        dockerstacks.pull_and_recreate(name)
    except dockerstacks.DockerStackError as exc:
        return _render_docker_detail(request, username, name, error=str(exc), status_code=400)
    return _render_docker_detail(request, username, name, message="Images mises a jour et containers recrees.")


@app.get("/docker/{name}/logs/{service}", response_class=HTMLResponse)
def docker_logs(request: Request, name: str, service: str, username: str = Depends(require_login)):
    stack = dockerstacks.get_stack(name)
    if stack is None:
        raise HTTPException(status_code=404, detail=f"Stack '{name}' introuvable.")
    try:
        logs = dockerstacks.get_logs(name, service)
    except dockerstacks.DockerStackError as exc:
        logs = str(exc)
    return templates.TemplateResponse(
        "docker_logs.html",
        {"request": request, "username": username, "stack": stack, "service": service, "logs": logs},
    )


@app.get("/docker/{name}/delete", response_class=HTMLResponse)
def docker_delete_form(request: Request, name: str, username: str = Depends(require_login)):
    stack = dockerstacks.get_stack(name)
    if stack is None:
        raise HTTPException(status_code=404, detail=f"Stack '{name}' introuvable.")
    return templates.TemplateResponse(
        "docker_delete.html",
        {"request": request, "username": username, "stack": stack, "error": None},
    )


@app.post("/docker/{name}/delete", response_class=HTMLResponse)
def docker_delete_submit(
    request: Request, name: str, username: str = Depends(require_login), confirm_name: str = Form(...),
):
    stack = dockerstacks.get_stack(name)
    if stack is None:
        raise HTTPException(status_code=404, detail=f"Stack '{name}' introuvable.")

    if confirm_name.strip() != name.strip():
        return templates.TemplateResponse(
            "docker_delete.html",
            {
                "request": request, "username": username, "stack": stack,
                "error": "Le nom tape ne correspond pas au nom de la stack - rien n'a ete supprime.",
            },
            status_code=400,
        )

    try:
        dockerstacks.delete_stack(name)
    except (dockerstacks.DockerStackError, zfs.DatasetError) as exc:
        return templates.TemplateResponse(
            "docker_delete.html",
            {"request": request, "username": username, "stack": stack, "error": str(exc)},
            status_code=500,
        )

    return RedirectResponse("/docker", status_code=302)
