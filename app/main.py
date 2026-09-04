from __future__ import annotations

import asyncio
import datetime
import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
import urllib.parse
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Request, Form, Depends, HTTPException, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from app import (
    auth, disks, zfs, sysstats, smart as smart_module, replace_workflow, shares,
    nasusers, dockerstacks, netstats, health, netconfig, dockerconsole, dockerops,
    sysaccounts, configbackup, poolexpand,
)

BASE_DIR = os.path.dirname(__file__)
logger = logging.getLogger("nas_manager.main")


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


def _docker_dashboard_rows() -> list[dict]:
    rows = []
    for s in dockerstacks.list_stacks():
        try:
            containers = dockerstacks.get_stack_containers(s.name)
        except dockerstacks.DockerStackError:
            containers = []
        running = sum(1 for c in containers if c.state == "running")
        rows.append({
            "stack": s, "running": running, "total": len(containers),
            "has_icon": dockerstacks.get_icon_path(s.name) is not None,
        })
    return rows


def _share_dashboard_rows() -> list[dict]:
    return [
        {"share": s, "usernames": [u.username for u in s.users]}
        for s in shares.list_shares()
    ]


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
            "docker_rows": _docker_dashboard_rows(),
            "share_rows": _share_dashboard_rows(),
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
            "boot_label": sysstats.format_boot_date(stats.boot_epoch),
            "format_frequency": sysstats.format_frequency,
            "format_bytes": sysstats.format_bytes,
            "pools_with_alerts": pools_with_alerts,
            # Le widget reseau est desormais une tuile de cette grille : il
            # est rendu par le meme fragment, avec les memes aides.
            "interfaces": netstats.list_interfaces(),
            "format_bitrate": netstats.format_bitrate,
            "sparkline_points": netstats.sparkline_points,
            "sparkline_area": netstats.sparkline_area,
            "shared_max": netstats.shared_max,
        },
    )


@app.get("/partials/network", response_class=HTMLResponse)
def partial_network(request: Request, username: str = Depends(require_login)):
    interfaces = netstats.list_interfaces()
    return templates.TemplateResponse(
        "_network_partial.html",
        {
            "request": request, "interfaces": interfaces,
            "format_bitrate": netstats.format_bitrate,
            "sparkline_points": netstats.sparkline_points,
            "sparkline_area": netstats.sparkline_area,
            "shared_max": netstats.shared_max,
        },
    )


@app.get("/partials/health", response_class=HTMLResponse)
def partial_health(request: Request, username: str = Depends(require_login)):
    return templates.TemplateResponse(
        "_health_partial.html",
        {"request": request, "report": health.get_report()},
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
            "expansion": poolexpand.get_expansion_status(name),
            "capability": poolexpand.get_capability(name),
        },
    )


# ---------------------------------------------------------------------------
# Agrandissement d'un pool existant (Phase 10)
# ---------------------------------------------------------------------------

@app.get("/pools/{name}/expand", response_class=HTMLResponse)
def pool_expand_form(request: Request, name: str, username: str = Depends(require_login)):
    try:
        options = poolexpand.get_options(name)
    except poolexpand.PoolExpandError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return templates.TemplateResponse(
        "pool_expand.html",
        {
            "request": request, "username": username, "options": options,
            "pool": options.pool, "expansion": poolexpand.get_expansion_status(name),
            "format_bytes": sysstats.format_bytes, "error": None,
        },
    )


@app.post("/pools/{name}/expand/plan", response_class=HTMLResponse)
def pool_expand_plan(
    request: Request, name: str, username: str = Depends(require_login),
    mode: str = Form(...), selected_disks: str = Form(""),
    target_vdev: str = Form(""), new_type: str = Form(""),
):
    """Etape de verification : valide tout, tente l'essai a blanc ZFS, et
    affiche le recapitulatif. Ne touche jamais au pool."""
    plan = poolexpand.plan_expansion(
        name, mode, _parse_disk_list(selected_disks),
        target_vdev=target_vdev or None, new_type=new_type or None,
    )
    if not plan.ok:
        try:
            options = poolexpand.get_options(name)
        except poolexpand.PoolExpandError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return templates.TemplateResponse(
            "pool_expand.html",
            {
                "request": request, "username": username, "options": options,
                "pool": options.pool, "expansion": poolexpand.get_expansion_status(name),
                "format_bytes": sysstats.format_bytes, "error": " ".join(plan.errors),
            },
            status_code=400,
        )
    return templates.TemplateResponse(
        "pool_expand_confirm.html",
        {"request": request, "username": username, "plan": plan, "pool_name": name},
    )


@app.post("/pools/{name}/expand/apply", response_class=HTMLResponse)
def pool_expand_apply(
    request: Request, name: str, username: str = Depends(require_login),
    mode: str = Form(...), selected_disks: str = Form(""),
    target_vdev: str = Form(""), new_type: str = Form(""),
):
    """Execution. Le plan est INTEGRALEMENT recalcule ici : on ne fait
    jamais confiance a ce que le formulaire renvoie, et la situation a pu
    changer depuis l'affichage du recapitulatif."""
    plan = poolexpand.plan_expansion(
        name, mode, _parse_disk_list(selected_disks),
        target_vdev=target_vdev or None, new_type=new_type or None,
    )
    error = None
    if not plan.ok:
        error = " ".join(plan.errors)
    else:
        try:
            poolexpand.apply_expansion(plan)
        except poolexpand.PoolExpandError as exc:
            error = str(exc)

    if error:
        try:
            options = poolexpand.get_options(name)
        except poolexpand.PoolExpandError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return templates.TemplateResponse(
            "pool_expand.html",
            {
                "request": request, "username": username, "options": options,
                "pool": options.pool, "expansion": poolexpand.get_expansion_status(name),
                "format_bytes": sysstats.format_bytes, "error": error,
            },
            status_code=400,
        )
    return RedirectResponse(f"/pools/{name}", status_code=302)


@app.get("/partials/pools/{name}/expansion", response_class=HTMLResponse)
def partial_pool_expansion(request: Request, name: str, username: str = Depends(require_login)):
    """Progression de l'extension, rafraichie par HTMX (meme principe que le
    suivi de resilver de la Phase 3)."""
    return templates.TemplateResponse(
        "_expansion_partial.html",
        {"request": request, "expansion": poolexpand.get_expansion_status(name), "pool_name": name},
    )


@app.post("/pools/{name}/upgrade", response_class=HTMLResponse)
def pool_upgrade(
    request: Request, name: str, username: str = Depends(require_login),
    confirm_name: str = Form(...),
):
    """Active les fonctionnalites ZFS en attente sur le pool (necessaire
    pour l'extension RAIDZ sur un pool cree avant). IRREVERSIBLE : retype du
    nom exige, comme pour les autres operations sans retour arriere."""
    error = None
    if confirm_name.strip() != name:
        error = "Le nom tape ne correspond pas au pool - rien n'a ete modifie."
    else:
        try:
            poolexpand.upgrade_pool(name)
        except poolexpand.PoolExpandError as exc:
            error = str(exc)
    if error:
        try:
            options = poolexpand.get_options(name)
        except poolexpand.PoolExpandError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return templates.TemplateResponse(
            "pool_expand.html",
            {
                "request": request, "username": username, "options": options,
                "pool": options.pool, "expansion": poolexpand.get_expansion_status(name),
                "format_bytes": sysstats.format_bytes, "error": error,
            },
            status_code=400,
        )
    return RedirectResponse(f"/pools/{name}/expand", status_code=302)


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


def _pool_delete_context(request: Request, username: str, pool, error: str | None = None) -> dict:
    """Ce qu'une suppression de pool emporterait avec elle. Affiche AVANT,
    jamais decouvert apres : un partage ou une stack dont le dataset a
    disparu avec le pool ne fonctionne plus, et laisse une entree morte
    dans les registres."""
    return {
        "request": request, "username": username, "pool": pool, "error": error,
        "pool_shares": shares.list_shares_on_pool(pool.name),
        "pool_stacks": dockerstacks.list_stacks_on_pool(pool.name),
    }


@app.get("/pools/{name}/delete", response_class=HTMLResponse)
def pool_delete_form(request: Request, name: str, username: str = Depends(require_login)):
    pool = zfs.get_pool(name)
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Pool '{name}' introuvable.")
    return templates.TemplateResponse("pool_delete.html", _pool_delete_context(request, username, pool))


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
            _pool_delete_context(
                request, username, pool,
                "Le nom tape ne correspond pas au nom du pool - rien n'a ete supprime.",
            ),
            status_code=400,
        )

    # Ordre important : on arrete les stacks TANT QUE le pool existe encore
    # (leur docker-compose.yml y vit). Une fois le pool detruit, il serait
    # trop tard : il resterait des containers pointant vers un chemin mort.
    try:
        dockerstacks.stop_stacks_on_pool(name)
    except Exception:  # noqa: BLE001 - best-effort, ne doit jamais bloquer la suppression
        logger.exception("Arret des stacks du pool '%s' impossible", name)

    try:
        zfs.destroy_pool(name)
    except zfs.PoolDestructionError as exc:
        return templates.TemplateResponse(
            "pool_delete.html", _pool_delete_context(request, username, pool, str(exc)),
            status_code=500,
        )

    # Le pool est detruit : ses datasets n'existent plus. On nettoie les
    # registres APRES seulement, pour ne jamais perdre des definitions si la
    # destruction avait echoue. Sans ce nettoyage, les partages restaient
    # dans smb.conf en pointant vers un chemin mort - et devenaient meme
    # impossibles a supprimer depuis l'interface.
    try:
        shares.purge_pool_shares(name)
        dockerstacks.forget_stacks_on_pool(name)
    except Exception:  # noqa: BLE001 - le pool est deja detruit, on ne bloque pas l'utilisateur
        logger.exception("Nettoyage des registres apres destruction du pool '%s' impossible", name)

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
        {
            "request": request, "username": username, "users": nasusers.list_share_users(), "error": error,
            "password_requirements": nasusers.PASSWORD_REQUIREMENTS_LABEL,
            "assignable_groups": nasusers.list_assignable_groups(),
        },
    )


def _render_share_users(request: Request, username: str, error: str, status_code: int = 400):
    return templates.TemplateResponse(
        "share_users.html",
        {
            "request": request, "username": username, "users": nasusers.list_share_users(), "error": error,
            "password_requirements": nasusers.PASSWORD_REQUIREMENTS_LABEL,
            "assignable_groups": nasusers.list_assignable_groups(),
        },
        status_code=status_code,
    )


@app.post("/share-users", response_class=HTMLResponse)
async def share_users_create(
    request: Request, username: str = Depends(require_login),
    new_username: str = Form(...), password: str = Form(...), confirm_password: str = Form(...),
    prenom: str = Form(""), nom: str = Form(""), extra_groups: list[str] = Form([]),
    avatar_emoji: str = Form(""), avatar_photo: UploadFile | None = File(None),
):
    if password != confirm_password:
        return _render_share_users(request, username, "Les deux mots de passe saisis ne correspondent pas.")
    full_name = f"{prenom.strip()} {nom.strip()}".strip()
    try:
        nasusers.create_share_user(new_username, password, full_name=full_name, extra_groups=extra_groups)
    except nasusers.ShareUserError as exc:
        return _render_share_users(request, username, str(exc))
    # L'avatar (photo ou emoji) est facultatif : un probleme dessus ne doit
    # jamais faire echouer la creation du compte, deja reussie a ce stade -
    # meme principe que l'icone Docker facultative a la creation d'une stack.
    if avatar_photo is not None and avatar_photo.filename:
        content = await avatar_photo.read()
        try:
            nasusers.set_avatar_photo(new_username, avatar_photo.filename, content)
        except nasusers.ShareUserError as exc:
            logger.warning("Avatar photo ignoree a la creation du compte de partage '%s' : %s", new_username, exc)
    elif avatar_emoji.strip():
        try:
            nasusers.set_avatar_emoji(new_username, avatar_emoji.strip())
        except nasusers.ShareUserError as exc:
            logger.warning("Avatar emoji ignore a la creation du compte de partage '%s' : %s", new_username, exc)
    return RedirectResponse("/share-users", status_code=302)


@app.post("/share-users/{name}/profile", response_class=HTMLResponse)
def share_users_update_profile(
    request: Request, name: str, username: str = Depends(require_login),
    prenom: str = Form(""), nom: str = Form(""), extra_groups: list[str] = Form([]),
):
    full_name = f"{prenom.strip()} {nom.strip()}".strip()
    try:
        nasusers.set_share_user_profile(name, full_name=full_name, extra_groups=extra_groups)
    except nasusers.ShareUserError as exc:
        return _render_share_users(request, username, str(exc))
    return RedirectResponse("/share-users", status_code=302)


@app.post("/share-users/{name}/admin/grant", response_class=HTMLResponse)
def share_users_grant_admin(
    request: Request, name: str, username: str = Depends(require_login),
    confirm_password: str = Form(...),
):
    """Donne l'acces admin a l'interface a un compte de partage (Phase 9b).
    Action sensible : exige le mot de passe de l'admin CONNECTE, jamais
    celui du compte cible (cf. app/nasusers.py)."""
    try:
        nasusers.grant_admin_access(name, username, confirm_password)
    except nasusers.ShareUserError as exc:
        return _render_share_users(request, username, str(exc))
    return RedirectResponse("/share-users", status_code=302)


@app.post("/share-users/{name}/admin/revoke", response_class=HTMLResponse)
def share_users_revoke_admin(
    request: Request, name: str, username: str = Depends(require_login),
    confirm_password: str = Form(...),
):
    try:
        nasusers.revoke_admin_access(name, username, confirm_password)
    except nasusers.ShareUserError as exc:
        return _render_share_users(request, username, str(exc))
    return RedirectResponse("/share-users", status_code=302)


@app.post("/share-users/{name}/password", response_class=HTMLResponse)
def share_users_password(
    request: Request, name: str, username: str = Depends(require_login),
    password: str = Form(...), confirm_password: str = Form(...),
):
    if password != confirm_password:
        return _render_share_users(request, username, "Les deux mots de passe saisis ne correspondent pas.")
    try:
        nasusers.set_share_user_password(name, password)
    except nasusers.ShareUserError as exc:
        return _render_share_users(request, username, str(exc))
    return RedirectResponse("/share-users", status_code=302)


@app.get("/share-users/{name}/avatar")
def share_user_avatar(name: str, username: str = Depends(require_login)):
    path = nasusers.get_avatar_photo_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail="Aucune photo d'avatar pour ce compte.")
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return Response(content=path.read_bytes(), media_type=content_type)


@app.post("/share-users/{name}/avatar", response_class=HTMLResponse)
async def share_user_avatar_upload(
    request: Request, name: str, username: str = Depends(require_login), avatar_photo: UploadFile = File(...),
):
    content = await avatar_photo.read()
    try:
        nasusers.set_avatar_photo(name, avatar_photo.filename or "avatar", content)
    except nasusers.ShareUserError as exc:
        return _render_share_users(request, username, str(exc))
    return RedirectResponse("/share-users", status_code=302)


@app.post("/share-users/{name}/avatar/emoji", response_class=HTMLResponse)
def share_user_avatar_emoji(
    request: Request, name: str, username: str = Depends(require_login), avatar_emoji: str = Form(...),
):
    try:
        nasusers.set_avatar_emoji(name, avatar_emoji)
    except nasusers.ShareUserError as exc:
        return _render_share_users(request, username, str(exc))
    return RedirectResponse("/share-users", status_code=302)


@app.post("/share-users/{name}/avatar/delete", response_class=HTMLResponse)
def share_user_avatar_delete(request: Request, name: str, username: str = Depends(require_login)):
    nasusers.delete_avatar(name)
    return RedirectResponse("/share-users", status_code=302)


@app.post("/share-users/{name}/delete", response_class=HTMLResponse)
def share_users_delete(request: Request, name: str, username: str = Depends(require_login)):
    try:
        nasusers.delete_share_user(name)
    except nasusers.ShareUserError as exc:
        return templates.TemplateResponse(
            "share_users.html",
            {
                "request": request, "username": username, "users": nasusers.list_share_users(), "error": str(exc),
                "password_requirements": nasusers.PASSWORD_REQUIREMENTS_LABEL,
                "assignable_groups": nasusers.list_assignable_groups(),
            },
            status_code=400,
        )
    return RedirectResponse("/share-users", status_code=302)


# ---------------------------------------------------------------------------
# Dossiers partages (SMB/NFS)
# ---------------------------------------------------------------------------

@app.get("/shares", response_class=HTMLResponse)
def shares_list(request: Request, username: str = Depends(require_login)):
    # On verifie en direct que le dataset de chaque partage existe encore :
    # sinon l'utilisateur voit un partage d'apparence normale qui ne
    # fonctionne plus (pool detruit, dataset supprime a la main...) sans
    # comprendre pourquoi.
    rows = [
        {"share": s, "dataset_missing": not zfs.dataset_exists(s.dataset)}
        for s in shares.list_shares()
    ]
    return templates.TemplateResponse(
        "shares.html", {"request": request, "username": username, "rows": rows},
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
    used_groups = {g.groupname for g in share.groups}
    available_groups = [g for g in nasusers.list_assignable_groups() if g not in used_groups]

    return templates.TemplateResponse(
        "share_detail.html",
        {
            "request": request, "username": username, "share": share,
            "available_users": available_users, "available_groups": available_groups,
            "error": error, "warnings": warnings or [],
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


@app.post("/shares/{name}/groups", response_class=HTMLResponse)
def share_add_group(
    request: Request, name: str, username: str = Depends(require_login),
    share_groupname: str = Form(...), access: str = Form(...),
):
    try:
        warnings = shares.add_group_to_share(name, share_groupname, access)
    except shares.ShareError as exc:
        return _render_share_detail(request, username, name, error=str(exc), status_code=400)
    return _render_share_detail(request, username, name, warnings=warnings)


@app.post("/shares/{name}/groups/{share_groupname}/delete", response_class=HTMLResponse)
def share_remove_group(request: Request, name: str, share_groupname: str, username: str = Depends(require_login)):
    try:
        warnings = shares.remove_group_from_share(name, share_groupname)
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
        rows.append({
            "stack": s, "containers": containers,
            "has_icon": dockerstacks.get_icon_path(s.name) is not None,
        })
    # Bandeau leger si des reliquats trainent sous <pool>/docker : c'est
    # invisible depuis la liste des stacks sinon, et ca bloque les noms.
    try:
        orphans, ghosts = dockerstacks.count_storage_anomalies()
    except Exception:  # noqa: BLE001 - jamais bloquer la liste pour un souci d'inventaire
        logger.exception("Inventaire du stockage Docker impossible")
        orphans, ghosts = 0, 0
    return templates.TemplateResponse(
        "docker_stacks.html",
        {"request": request, "username": username, "rows": rows, "orphans": orphans, "ghosts": ghosts},
    )


# ---------------------------------------------------------------------------
# Stockage Docker : arborescence des datasets/dossiers, nettoyage des
# orphelins (IMPORTANT : routes fixes declarees AVANT /docker/{name}).
# ---------------------------------------------------------------------------

def _render_docker_storage(
    request: Request, username: str,
    error: str | None = None, message: str | None = None, status_code: int = 200,
):
    pools = dockerstacks.list_docker_storage()
    orphans = sum(1 for ps in pools for e in ps.entries if e.status == "orphan")
    ghosts = sum(1 for ps in pools for e in ps.entries if e.status == "ghost")
    return templates.TemplateResponse(
        "docker_storage.html",
        {
            "request": request, "username": username, "pools": pools,
            "orphans": orphans, "ghosts": ghosts,
            "error": error, "message": message, "format_bytes": sysstats.format_bytes,
        },
        status_code=status_code,
    )


@app.get("/docker/storage", response_class=HTMLResponse)
def docker_storage(request: Request, username: str = Depends(require_login)):
    return _render_docker_storage(request, username)


@app.get("/docker/storage/{pool}/{name}/delete", response_class=HTMLResponse)
def docker_storage_delete_form(request: Request, pool: str, name: str, username: str = Depends(require_login)):
    entry = dockerstacks.get_storage_entry(pool, name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Entree '{name}' introuvable sous {pool}/docker.")
    if entry.status != "orphan":
        return _render_docker_storage(
            request, username,
            error=f"'{name}' n'est pas un orphelin (statut : {entry.status}) - rien a nettoyer ici.",
            status_code=400,
        )
    return templates.TemplateResponse(
        "docker_storage_delete.html",
        {"request": request, "username": username, "entry": entry, "error": None, "format_bytes": sysstats.format_bytes},
    )


@app.post("/docker/storage/{pool}/{name}/delete", response_class=HTMLResponse)
def docker_storage_delete_submit(
    request: Request, pool: str, name: str, username: str = Depends(require_login),
    confirm_name: str = Form(...),
):
    entry = dockerstacks.get_storage_entry(pool, name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Entree '{name}' introuvable sous {pool}/docker.")
    if confirm_name.strip() != name:
        return templates.TemplateResponse(
            "docker_storage_delete.html",
            {
                "request": request, "username": username, "entry": entry,
                "error": "Le nom tape ne correspond pas - rien n'a ete supprime.",
                "format_bytes": sysstats.format_bytes,
            },
            status_code=400,
        )
    try:
        message = dockerstacks.delete_orphan(pool, name)
    except (dockerstacks.DockerStackError, zfs.DatasetError) as exc:
        return templates.TemplateResponse(
            "docker_storage_delete.html",
            {"request": request, "username": username, "entry": entry, "error": str(exc), "format_bytes": sysstats.format_bytes},
            status_code=400,
        )
    return _render_docker_storage(request, username, message=message)


@app.post("/docker/storage/ghost/{name}/forget", response_class=HTMLResponse)
def docker_storage_forget_ghost(request: Request, name: str, username: str = Depends(require_login)):
    try:
        message = dockerstacks.forget_ghost_stack(name)
    except dockerstacks.DockerStackError as exc:
        return _render_docker_storage(request, username, error=str(exc), status_code=400)
    return _render_docker_storage(request, username, message=message)


@app.get("/docker/new", response_class=HTMLResponse)
def docker_new_form(request: Request, username: str = Depends(require_login)):
    return templates.TemplateResponse(
        "docker_new.html",
        {"request": request, "username": username, "pools": zfs.list_pools(), "error": None, "name": "", "compose_content": ""},
    )


@app.post("/docker", response_class=HTMLResponse)
async def docker_create(
    request: Request, username: str = Depends(require_login),
    name: str = Form(...), pool: str = Form(...), compose_content: str = Form(...),
    icon: UploadFile | None = File(None),
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
    # L'icone est facultative des la creation : un probleme dessus (format
    # invalide, fichier vide...) ne doit jamais faire echouer la creation de
    # la stack elle-meme, qui a deja reussi a ce stade - on l'ignore juste
    # silencieusement, l'utilisateur pourra toujours en ajouter/changer une
    # depuis le detail de la stack.
    if icon is not None and icon.filename:
        content = await icon.read()
        try:
            dockerstacks.save_icon(stack.name, icon.filename, content)
        except dockerstacks.DockerIconError as exc:
            logger.warning("Icone ignoree a la creation de la stack '%s' : %s", stack.name, exc)
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
    icon_url = f"/docker/{name}/icon" if dockerstacks.get_icon_path(name) is not None else None

    return templates.TemplateResponse(
        "docker_detail.html",
        {
            "request": request, "username": username, "stack": stack,
            "containers": containers, "compose_content": compose_content,
            "error": error, "message": message, "updates": updates or {},
            "icon_url": icon_url,
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


@app.get("/docker/{name}/icon")
def docker_icon(name: str, username: str = Depends(require_login)):
    path = dockerstacks.get_icon_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail="Aucune icone pour cette stack.")
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return Response(content=path.read_bytes(), media_type=content_type)


@app.post("/docker/{name}/icon", response_class=HTMLResponse)
async def docker_icon_upload(
    request: Request, name: str, username: str = Depends(require_login), icon: UploadFile = File(...),
):
    content = await icon.read()
    try:
        dockerstacks.save_icon(name, icon.filename or "icon", content)
    except dockerstacks.DockerIconError as exc:
        return _render_docker_detail(request, username, name, error=str(exc), status_code=400)
    return _render_docker_detail(request, username, name, message="Icone mise a jour.")


@app.post("/docker/{name}/icon/delete", response_class=HTMLResponse)
def docker_icon_delete(request: Request, name: str, username: str = Depends(require_login)):
    dockerstacks.delete_icon(name)
    return _render_docker_detail(request, username, name, message="Icone supprimee.")


@app.get("/docker/{name}/console/{service}", response_class=HTMLResponse)
def docker_console_page(request: Request, name: str, service: str, username: str = Depends(require_login)):
    stack = dockerstacks.get_stack(name)
    if stack is None:
        raise HTTPException(status_code=404, detail=f"Stack '{name}' introuvable.")
    try:
        dockerconsole.resolve_console_target(name, service)
    except dockerconsole.DockerConsoleError as exc:
        return _render_docker_detail(request, username, name, error=str(exc), status_code=400)
    return templates.TemplateResponse(
        "docker_console.html",
        {"request": request, "username": username, "stack": stack, "service": service},
    )


@app.websocket("/ws/docker/{name}/run/{action}")
async def docker_run_ws(websocket: WebSocket, name: str, action: str):
    """Execute une action `docker compose` sur une stack en relayant sa
    sortie EN DIRECT au navigateur (fenetre de logs). L'action est une cle
    de liste blanche (cf. app/dockerops.py) : jamais une commande libre."""
    username = websocket.session.get("username")
    if not username:
        await websocket.close(code=4401)
        return

    await websocket.accept()

    try:
        async for event in dockerops.run_action(name, action):
            await websocket.send_json(event)
    except dockerops.DockerOpsError as exc:
        await websocket.send_json({"type": "done", "ok": False, "code": -1, "text": str(exc)})
    except WebSocketDisconnect:
        # Le client a ferme la fenetre : run_action() a deja termine le
        # processus Docker en cours via son gestionnaire d'annulation.
        logger.info("Fenetre de logs Docker fermee pendant l'action '%s' sur '%s'", action, name)
        return
    except Exception:  # noqa: BLE001 - jamais laisser une exception muette cote client
        logger.exception("Action Docker '%s' sur '%s' a plante", action, name)
        try:
            await websocket.send_json({
                "type": "done", "ok": False, "code": -1,
                "text": "Erreur interne pendant l'execution - voir les journaux du service.",
            })
        except Exception:  # noqa: BLE001
            pass

    try:
        await websocket.close()
    except Exception:  # noqa: BLE001
        pass


@app.websocket("/ws/docker/{name}/console/{service}")
async def docker_console_ws(websocket: WebSocket, name: str, service: str):
    username = websocket.session.get("username")
    if not username:
        await websocket.close(code=4401)
        return

    await websocket.accept()

    try:
        container = dockerconsole.resolve_console_target(name, service)
    except dockerconsole.DockerConsoleError as exc:
        await websocket.send_text(f"\r\n[erreur] {exc}\r\n")
        await websocket.close(code=1011)
        return

    try:
        process = await dockerconsole.spawn_shell(container)
    except dockerconsole.DockerConsoleError as exc:
        await websocket.send_text(f"\r\n[erreur] {exc}\r\n")
        await websocket.close(code=1011)
        return

    await websocket.send_text(
        f"-- connecte a '{container}' (service '{service}') - tape 'exit' pour quitter --\r\n"
    )

    async def pump_output():
        assert process.stdout is not None
        try:
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                await websocket.send_text(chunk.decode(errors="replace"))
        except Exception:
            pass

    reader_task = asyncio.create_task(pump_output())
    try:
        while True:
            data = await websocket.receive_text()
            if process.stdin is None or process.stdin.is_closing():
                break
            process.stdin.write((data + "\n").encode())
            await process.stdin.drain()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        reader_task.cancel()
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except Exception:
            pass


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


# ---------------------------------------------------------------------------
# Reseau (Phase 7b) - IP/DHCP, DNS, agregats de liens, wifi.
#
# Rappel de securite (voir le commentaire d'en-tete de app/netconfig.py) :
# chaque changement passe par une page de recapitulatif/validation
# ("/network/apply") avant toute application reelle, et l'application
# elle-meme utilise 'netplan try' (confirmation ou retour arriere
# automatique sous ~90s) - jamais d'ecriture reseau definitive en un seul
# clic.
# ---------------------------------------------------------------------------

def _apply_remaining_seconds(state: netconfig.ApplyState | None) -> int | None:
    if state is None or state.status not in ("in_progress", "confirming", "cancelling"):
        return None
    started = datetime.datetime.fromisoformat(state.started_at)
    elapsed = (datetime.datetime.now() - started).total_seconds()
    return max(0, int(state.timeout - elapsed))


@app.get("/network", response_class=HTMLResponse)
def network_overview(request: Request, username: str = Depends(require_login)):
    interfaces = netconfig.list_physical_interfaces()
    managed = netconfig.read_managed_config()
    return templates.TemplateResponse(
        "network.html",
        {
            "request": request, "username": username,
            "interfaces": interfaces,
            "bonds": managed.bonds,
            "bond_available": netconfig.available_for_bonding(interfaces),
            "wifi_interfaces": [i for i in interfaces if i.is_wifi],
            "dns_servers": netconfig.get_dns_servers(),
            "apply_state": netconfig.poll_apply_status(),
        },
    )


@app.post("/network/apply/dismiss")
def network_apply_dismiss(username: str = Depends(require_login)):
    netconfig.dismiss_apply_state()
    return RedirectResponse("/network", status_code=302)


@app.get("/network/interface/{iface_name}/edit", response_class=HTMLResponse)
def network_interface_edit_form(request: Request, iface_name: str, username: str = Depends(require_login)):
    interfaces = {i.name: i for i in netconfig.list_physical_interfaces()}
    iface = interfaces.get(iface_name)
    if iface is None:
        raise HTTPException(status_code=404, detail=f"Carte reseau '{iface_name}' introuvable.")
    if iface.bond_member_of:
        raise HTTPException(
            status_code=400,
            detail=f"'{iface_name}' fait partie de l'agregat '{iface.bond_member_of}' - modifie l'agregat lui-meme.",
        )
    return templates.TemplateResponse(
        "network_interface_edit.html",
        {"request": request, "username": username, "iface": iface},
    )


@app.post("/network/interface/{iface_name}/edit", response_class=HTMLResponse)
def network_interface_edit_submit(
    request: Request, iface_name: str, username: str = Depends(require_login),
    mode: str = Form("dhcp"), address: str = Form(""), gateway4: str = Form(""),
):
    config = netconfig.read_managed_config()
    config.interfaces[iface_name] = netconfig.InterfaceConfig(
        dhcp4=(mode == "dhcp"), address=address.strip() or None, gateway4=gateway4.strip() or None,
    )
    request.session["pending_network_config"] = config.to_dict()
    return RedirectResponse("/network/apply", status_code=302)


@app.get("/network/dns", response_class=HTMLResponse)
def network_dns_form(request: Request, username: str = Depends(require_login)):
    return templates.TemplateResponse(
        "network_dns.html",
        {"request": request, "username": username, "dns_servers": netconfig.get_dns_servers()},
    )


@app.post("/network/dns", response_class=HTMLResponse)
def network_dns_submit(request: Request, username: str = Depends(require_login), dns_servers: str = Form("")):
    config = netconfig.read_managed_config()
    config.dns_servers = [s.strip() for s in dns_servers.split(",") if s.strip()]
    if config.dns_servers and not config.interfaces and not config.bonds and not config.wifis:
        # netplan rattache le DNS a chaque entree d'interface individuellement
        # (pas de section globale) : si rien n'est encore gere par NAS
        # Manager, on rattache automatiquement les serveurs DNS choisis a
        # toutes les cartes physiques detectees (en DHCP par defaut pour
        # l'adresse IP elle-meme) - sinon le DNS choisi n'aurait litteralement
        # nulle part ou s'appliquer dans le fichier genere.
        for iface in netconfig.list_physical_interfaces():
            if not iface.is_wifi and iface.bond_member_of is None:
                config.interfaces[iface.name] = netconfig.InterfaceConfig()
    request.session["pending_network_config"] = config.to_dict()
    return RedirectResponse("/network/apply", status_code=302)


@app.get("/network/bond/new", response_class=HTMLResponse)
def network_bond_new_form(request: Request, username: str = Depends(require_login)):
    return templates.TemplateResponse(
        "network_bond_new.html",
        {
            "request": request, "username": username,
            "available": netconfig.available_for_bonding(), "bond_modes": netconfig.BOND_MODES, "error": None,
        },
    )


@app.post("/network/bond/new", response_class=HTMLResponse)
def network_bond_new_submit(
    request: Request, username: str = Depends(require_login),
    bond_name: str = Form(...), members: list[str] = Form(default=[]), mode: str = Form("active-backup"),
    ip_mode: str = Form("dhcp"), address: str = Form(""), gateway4: str = Form(""),
):
    bond_name = bond_name.strip()
    member_list = [m.strip() for m in members if m.strip()]

    if not netconfig.BOND_NAME_RE.match(bond_name) or len(member_list) < 2:
        return templates.TemplateResponse(
            "network_bond_new.html",
            {
                "request": request, "username": username,
                "available": netconfig.available_for_bonding(), "bond_modes": netconfig.BOND_MODES,
                "error": (
                    "Nom d'agregat invalide (lettres minuscules/chiffres/tirets, 15 caracteres max) "
                    "ou moins de 2 cartes choisies."
                ),
            },
            status_code=400,
        )

    config = netconfig.read_managed_config()
    for member in member_list:
        config.interfaces.pop(member, None)
    config.bonds[bond_name] = netconfig.BondConfig(
        members=member_list, mode=mode, dhcp4=(ip_mode == "dhcp"),
        address=address.strip() or None, gateway4=gateway4.strip() or None,
    )
    request.session["pending_network_config"] = config.to_dict()
    return RedirectResponse("/network/apply", status_code=302)


@app.post("/network/bond/{bond_name}/delete", response_class=HTMLResponse)
def network_bond_delete(request: Request, bond_name: str, username: str = Depends(require_login)):
    config = netconfig.read_managed_config()
    bond = config.bonds.pop(bond_name, None)
    if bond is None:
        raise HTTPException(status_code=404, detail=f"Agregat '{bond_name}' introuvable.")
    for member in bond.members:
        config.interfaces.setdefault(member, netconfig.InterfaceConfig())
    request.session["pending_network_config"] = config.to_dict()
    return RedirectResponse("/network/apply", status_code=302)


@app.get("/network/wifi/{iface_name}/edit", response_class=HTMLResponse)
def network_wifi_edit_form(request: Request, iface_name: str, username: str = Depends(require_login)):
    if not netconfig.is_wifi_interface(iface_name):
        raise HTTPException(status_code=404, detail=f"'{iface_name}' n'est pas une carte wifi.")
    managed = netconfig.read_managed_config()
    return templates.TemplateResponse(
        "network_wifi_edit.html",
        {
            "request": request, "username": username, "iface_name": iface_name,
            "config": managed.wifis.get(iface_name, netconfig.WifiConfig()),
            "scanned_ssids": netconfig.scan_wifi(iface_name),
        },
    )


@app.post("/network/wifi/{iface_name}/edit", response_class=HTMLResponse)
def network_wifi_edit_submit(
    request: Request, iface_name: str, username: str = Depends(require_login),
    ssid: str = Form(...), psk: str = Form(""), mode: str = Form("dhcp"),
    address: str = Form(""), gateway4: str = Form(""),
):
    if not netconfig.is_wifi_interface(iface_name):
        raise HTTPException(status_code=404, detail=f"'{iface_name}' n'est pas une carte wifi.")
    config = netconfig.read_managed_config()
    config.wifis[iface_name] = netconfig.WifiConfig(
        ssid=ssid.strip(), psk=psk, dhcp4=(mode == "dhcp"),
        address=address.strip() or None, gateway4=gateway4.strip() or None,
    )
    request.session["pending_network_config"] = config.to_dict()
    return RedirectResponse("/network/apply", status_code=302)


@app.get("/network/apply", response_class=HTMLResponse)
def network_apply_review(request: Request, username: str = Depends(require_login)):
    apply_state = netconfig.poll_apply_status()
    if apply_state is not None:
        return templates.TemplateResponse(
            "network_apply.html",
            {
                "request": request, "username": username, "apply_state": apply_state,
                "remaining": _apply_remaining_seconds(apply_state),
                "check": None, "pending_yaml": None, "error": None,
            },
        )

    pending = request.session.get("pending_network_config")
    if pending is None:
        return RedirectResponse("/network", status_code=302)

    config = netconfig.ManagedNetworkConfig.from_dict(pending)
    return templates.TemplateResponse(
        "network_apply.html",
        {
            "request": request, "username": username, "apply_state": None,
            "check": netconfig.validate_network_plan(config),
            "pending_yaml": netconfig.build_managed_yaml(config),
            "error": None,
        },
    )


@app.post("/network/apply", response_class=HTMLResponse)
def network_apply_start(request: Request, username: str = Depends(require_login)):
    pending = request.session.get("pending_network_config")
    if pending is None:
        return RedirectResponse("/network", status_code=302)

    config = netconfig.ManagedNetworkConfig.from_dict(pending)
    try:
        netconfig.start_apply(config)
    except netconfig.NetworkApplyError as exc:
        return templates.TemplateResponse(
            "network_apply.html",
            {
                "request": request, "username": username, "apply_state": None,
                "check": netconfig.validate_network_plan(config),
                "pending_yaml": netconfig.build_managed_yaml(config),
                "error": str(exc),
            },
            status_code=400,
        )

    request.session.pop("pending_network_config", None)
    return RedirectResponse("/network/apply", status_code=302)


def _apply_status_partial(request: Request):
    apply_state = netconfig.poll_apply_status()
    return templates.TemplateResponse(
        "_network_apply_status_partial.html",
        {"request": request, "apply_state": apply_state, "remaining": _apply_remaining_seconds(apply_state)},
    )


@app.get("/partials/network-apply-status", response_class=HTMLResponse)
def partial_network_apply_status(request: Request, username: str = Depends(require_login)):
    return _apply_status_partial(request)


@app.post("/network/apply/confirm", response_class=HTMLResponse)
def network_apply_confirm(request: Request, username: str = Depends(require_login)):
    try:
        netconfig.confirm_apply()
    except netconfig.NetworkApplyError:
        pass
    return _apply_status_partial(request)


@app.post("/network/apply/cancel", response_class=HTMLResponse)
def network_apply_cancel(request: Request, username: str = Depends(require_login)):
    try:
        netconfig.cancel_apply()
    except netconfig.NetworkApplyError:
        pass
    return _apply_status_partial(request)


# ---------------------------------------------------------------------------
# Sauvegarde / restauration de la configuration (Phase 9c)
# ---------------------------------------------------------------------------

@app.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request, username: str = Depends(require_login),
                error: str | None = None, report: list[str] | None = None):
    return templates.TemplateResponse(
        "backup.html",
        {
            "request": request, "username": username, "error": error, "report": report,
            "section_labels": configbackup.SECTION_LABELS,
        },
    )


@app.get("/backup/download")
def backup_download(username: str = Depends(require_login)):
    """Genere l'archive puis la renvoie en telechargement. Le dossier
    temporaire est supprime APRES l'envoi (background task) : l'archive
    contient des empreintes de mots de passe, elle n'a rien a faire sur le
    disque du NAS une seconde de plus que necessaire."""
    try:
        archive = configbackup.create_archive()
    except configbackup.ConfigBackupError as exc:
        return RedirectResponse(f"/backup?error={urllib.parse.quote(str(exc))}", status_code=302)
    return FileResponse(
        path=str(archive), filename=archive.name, media_type="application/gzip",
        background=BackgroundTask(shutil.rmtree, archive.parent, True),
    )


@app.post("/backup/restore", response_class=HTMLResponse)
async def backup_restore_preview(
    request: Request, username: str = Depends(require_login),
    archive: UploadFile = File(...),
):
    """Etape 1 : on lit l'archive et on montre ce qu'elle contient. AUCUNE
    ecriture sur le systeme a ce stade."""
    content = await archive.read()
    if not content:
        return templates.TemplateResponse(
            "backup.html",
            {"request": request, "username": username, "error": "Fichier vide.",
             "report": None, "section_labels": configbackup.SECTION_LABELS},
            status_code=400,
        )
    if len(content) > configbackup.MAX_ARCHIVE_BYTES:
        return templates.TemplateResponse(
            "backup.html",
            {"request": request, "username": username,
             "error": "Fichier trop volumineux pour une archive de configuration.",
             "report": None, "section_labels": configbackup.SECTION_LABELS},
            status_code=400,
        )

    workdir = Path(tempfile.mkdtemp(prefix="nas-manager-restore-"))
    upload_path = workdir / "upload.tar.gz"
    upload_path.write_bytes(content)
    try:
        info = configbackup.inspect_archive(upload_path, workdir / "content")
    except configbackup.ConfigBackupError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        return templates.TemplateResponse(
            "backup.html",
            {"request": request, "username": username, "error": str(exc),
             "report": None, "section_labels": configbackup.SECTION_LABELS},
            status_code=400,
        )
    finally:
        upload_path.unlink(missing_ok=True)

    return templates.TemplateResponse(
        "backup_restore.html",
        {
            "request": request, "username": username, "info": info,
            "archive_root": info.path, "error": None,
            "section_labels": configbackup.SECTION_LABELS,
            "restorable": configbackup.RESTORABLE_SECTIONS,
        },
    )


@app.post("/backup/restore/apply", response_class=HTMLResponse)
def backup_restore_apply(
    request: Request, username: str = Depends(require_login),
    archive_root: str = Form(...), sections: list[str] = Form([]),
    confirm_password: str = Form(...),
):
    """Etape 2 : application effective, apres reconfirmation du mot de passe
    de l'admin connecte (meme regle que les autres actions sensibles)."""
    root = Path(archive_root)
    # Le chemin vient d'un champ de formulaire : on verifie qu'il pointe bien
    # vers une archive extraite par nous, et pas ailleurs sur le disque.
    if not root.name == "content" or not root.parent.name.startswith("nas-manager-restore-"):
        raise HTTPException(status_code=400, detail="Chemin d'archive invalide.")
    if not (root / configbackup.MANIFEST_NAME).is_file():
        raise HTTPException(status_code=400, detail="Archive expiree ou introuvable - recharge le fichier.")

    if not auth.authenticate(username, confirm_password):
        return templates.TemplateResponse(
            "backup_restore.html",
            {
                "request": request, "username": username,
                "info": configbackup.describe_root(root), "archive_root": archive_root,
                "error": "Mot de passe incorrect - rien n'a ete restaure.",
                "section_labels": configbackup.SECTION_LABELS,
                "restorable": configbackup.RESTORABLE_SECTIONS,
            },
            status_code=400,
        )

    try:
        report = configbackup.restore(root, sections)
    except configbackup.ConfigBackupError as exc:
        return templates.TemplateResponse(
            "backup.html",
            {"request": request, "username": username, "error": str(exc), "report": None,
             "section_labels": configbackup.SECTION_LABELS},
            status_code=400,
        )
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)

    return templates.TemplateResponse(
        "backup.html",
        {"request": request, "username": username, "error": None, "report": report,
         "section_labels": configbackup.SECTION_LABELS},
    )


# ---------------------------------------------------------------------------
# Comptes systeme / sudo & groupes Linux (Phase 8b) - zone la plus sensible
# du projet : voir les commentaires en tete de app/sysaccounts.py pour le
# detail des garde-fous (auto-verrouillage, dernier admin+sudo,
# reconfirmation de mot de passe sur les actions sensibles).
# ---------------------------------------------------------------------------

def _render_admin_accounts(
    request: Request, username: str,
    error: str | None = None, message: str | None = None, status_code: int = 200,
):
    accounts = sysaccounts.list_system_accounts()
    all_groups = sysaccounts.list_groups()
    groups = [
        {"name": g, "members": sysaccounts.group_members(g), "protected": sysaccounts.is_protected_group(g)}
        for g in all_groups
    ]
    assignable_groups = sysaccounts.list_assignable_extra_groups()
    return templates.TemplateResponse(
        "admin_accounts.html",
        {
            "request": request, "username": username, "session_username": username,
            "accounts": accounts, "groups": groups, "assignable_groups": assignable_groups,
            "password_requirements": nasusers.PASSWORD_REQUIREMENTS_LABEL,
            "error": error, "message": message,
        },
        status_code=status_code,
    )


@app.get("/admin-accounts", response_class=HTMLResponse)
def admin_accounts_page(request: Request, username: str = Depends(require_login)):
    return _render_admin_accounts(request, username)


@app.post("/admin-accounts", response_class=HTMLResponse)
def admin_accounts_create(
    request: Request, username: str = Depends(require_login),
    new_username: str = Form(...), password: str = Form(...), confirm_password: str = Form(...),
    full_name: str = Form(""), grant_sudo: str = Form(""), grant_nasadmin: str = Form(""),
):
    if password != confirm_password:
        return _render_admin_accounts(request, username, error="Les deux mots de passe saisis ne correspondent pas.", status_code=400)
    try:
        sysaccounts.create_system_account(
            new_username, password, full_name=full_name,
            grant_sudo=bool(grant_sudo), grant_nasadmin=bool(grant_nasadmin),
        )
    except sysaccounts.SysAccountError as exc:
        return _render_admin_accounts(request, username, error=str(exc), status_code=400)
    return RedirectResponse("/admin-accounts", status_code=302)


@app.post("/admin-accounts/{name}/profile", response_class=HTMLResponse)
def admin_accounts_update_profile(
    request: Request, name: str, username: str = Depends(require_login),
    full_name: str = Form(""), extra_groups: list[str] = Form([]),
):
    try:
        sysaccounts.set_full_name(name, full_name)
        sysaccounts.set_extra_groups(name, extra_groups)
    except sysaccounts.SysAccountError as exc:
        return _render_admin_accounts(request, username, error=str(exc), status_code=400)
    return RedirectResponse("/admin-accounts", status_code=302)


@app.post("/admin-accounts/{name}/password", response_class=HTMLResponse)
def admin_accounts_set_password(
    request: Request, name: str, username: str = Depends(require_login),
    password: str = Form(...), confirm_password: str = Form(...),
):
    if password != confirm_password:
        return _render_admin_accounts(request, username, error="Les deux mots de passe saisis ne correspondent pas.", status_code=400)
    try:
        sysaccounts.set_account_password(name, password)
    except sysaccounts.SysAccountError as exc:
        return _render_admin_accounts(request, username, error=str(exc), status_code=400)
    return RedirectResponse("/admin-accounts", status_code=302)


@app.post("/admin-accounts/{name}/lock", response_class=HTMLResponse)
def admin_accounts_lock(request: Request, name: str, username: str = Depends(require_login)):
    try:
        sysaccounts.lock_account(name)
    except sysaccounts.SysAccountError as exc:
        return _render_admin_accounts(request, username, error=str(exc), status_code=400)
    return RedirectResponse("/admin-accounts", status_code=302)


@app.post("/admin-accounts/{name}/unlock", response_class=HTMLResponse)
def admin_accounts_unlock(request: Request, name: str, username: str = Depends(require_login)):
    try:
        sysaccounts.unlock_account(name)
    except sysaccounts.SysAccountError as exc:
        return _render_admin_accounts(request, username, error=str(exc), status_code=400)
    return RedirectResponse("/admin-accounts", status_code=302)


@app.post("/admin-accounts/{name}/sudo/grant", response_class=HTMLResponse)
def admin_accounts_grant_sudo(request: Request, name: str, username: str = Depends(require_login)):
    try:
        sysaccounts.grant_sudo(name)
    except sysaccounts.SysAccountError as exc:
        return _render_admin_accounts(request, username, error=str(exc), status_code=400)
    return RedirectResponse("/admin-accounts", status_code=302)


@app.post("/admin-accounts/{name}/sudo/revoke", response_class=HTMLResponse)
def admin_accounts_revoke_sudo(
    request: Request, name: str, username: str = Depends(require_login), confirm_password: str = Form(...),
):
    try:
        sysaccounts.revoke_sudo(name, username, confirm_password)
    except sysaccounts.SysAccountError as exc:
        return _render_admin_accounts(request, username, error=str(exc), status_code=400)
    return RedirectResponse("/admin-accounts", status_code=302)


@app.post("/admin-accounts/{name}/nasadmin/grant", response_class=HTMLResponse)
def admin_accounts_grant_nasadmin(request: Request, name: str, username: str = Depends(require_login)):
    try:
        sysaccounts.grant_nasadmin(name)
    except sysaccounts.SysAccountError as exc:
        return _render_admin_accounts(request, username, error=str(exc), status_code=400)
    return RedirectResponse("/admin-accounts", status_code=302)


@app.post("/admin-accounts/{name}/nasadmin/revoke", response_class=HTMLResponse)
def admin_accounts_revoke_nasadmin(
    request: Request, name: str, username: str = Depends(require_login), confirm_password: str = Form(...),
):
    try:
        sysaccounts.revoke_nasadmin(name, username, confirm_password)
    except sysaccounts.SysAccountError as exc:
        return _render_admin_accounts(request, username, error=str(exc), status_code=400)
    return RedirectResponse("/admin-accounts", status_code=302)


@app.get("/admin-accounts/{name}/delete", response_class=HTMLResponse)
def admin_accounts_delete_form(request: Request, name: str, username: str = Depends(require_login)):
    account = sysaccounts.get_system_account(name)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Compte '{name}' introuvable.")
    return templates.TemplateResponse(
        "admin_account_delete.html",
        {"request": request, "username": username, "account": account, "error": None},
    )


@app.post("/admin-accounts/{name}/delete", response_class=HTMLResponse)
def admin_accounts_delete_submit(
    request: Request, name: str, username: str = Depends(require_login),
    confirm_name: str = Form(...), confirm_password: str = Form(...), remove_home: str = Form(""),
):
    account = sysaccounts.get_system_account(name)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Compte '{name}' introuvable.")

    if confirm_name.strip() != name.strip():
        return templates.TemplateResponse(
            "admin_account_delete.html",
            {
                "request": request, "username": username, "account": account,
                "error": "Le nom tape ne correspond pas au nom du compte - rien n'a ete supprime.",
            },
            status_code=400,
        )

    try:
        sysaccounts.delete_system_account(name, username, confirm_password, remove_home=bool(remove_home))
    except sysaccounts.SysAccountError as exc:
        return templates.TemplateResponse(
            "admin_account_delete.html",
            {"request": request, "username": username, "account": account, "error": str(exc)},
            status_code=400,
        )
    return RedirectResponse("/admin-accounts", status_code=302)


@app.post("/admin-accounts/groups", response_class=HTMLResponse)
def admin_groups_create(request: Request, username: str = Depends(require_login), groupname: str = Form(...)):
    try:
        sysaccounts.create_group(groupname)
    except sysaccounts.SysAccountError as exc:
        return _render_admin_accounts(request, username, error=str(exc), status_code=400)
    return RedirectResponse("/admin-accounts", status_code=302)


@app.post("/admin-accounts/groups/{groupname}/delete", response_class=HTMLResponse)
def admin_groups_delete(request: Request, groupname: str, username: str = Depends(require_login)):
    try:
        sysaccounts.delete_group(groupname)
    except sysaccounts.SysAccountError as exc:
        return _render_admin_accounts(request, username, error=str(exc), status_code=400)
    return RedirectResponse("/admin-accounts", status_code=302)
