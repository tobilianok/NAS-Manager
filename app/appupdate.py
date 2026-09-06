"""
Mise a jour de NAS Manager lui-meme, depuis GitHub (Phase 11b).

Le probleme central : le processus qui applique la mise a jour est celui
qu'il faut redemarrer. Si le worker web lancait `install.sh` directement,
il se tuerait au milieu de son propre travail et personne ne saurait dire
si la mise a jour s'est terminee. La mise a jour est donc confiee a un
script DETACHE (`scripts/self-update.sh`), lance via `systemd-run` dans
son propre service transitoire : il survit au redemarrage de NAS Manager,
et ecrit sa progression dans un fichier d'etat que l'interface relit.

Filet de securite : apres redemarrage, le script interroge la page de
sante de l'interface. Si elle ne repond pas dans le delai imparti, il
revient TOUT SEUL a la version precedente et relance l'installation -
meme principe que le `netplan try` de la Phase 7b. Une interface web qui
se met a jour elle-meme peut se couper l'acces ; sans retour arriere
automatique, il faudrait un clavier ou du SSH pour s'en sortir.

Ce qui rend le retour arriere sur : l'etat applicatif (registres des
partages et des stacks, avatars, icones) vit dans /var/lib/nas-manager,
HORS du depot. Revenir a un commit precedent ne touche donc jamais aux
donnees, seulement au code.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from app import gitauth, version as version_module

logger = logging.getLogger("nas_manager.appupdate")

REPO_DIR = version_module.REPO_DIR
STATE_DIR = Path(os.environ.get("NAS_MANAGER_STATE_DIR", "/var/lib/nas-manager"))
STATE_FILE = STATE_DIR / "self_update.json"
UPDATER_SCRIPT = os.path.join(REPO_DIR, "scripts", "self-update.sh")
SERVICE_UNIT = "nas-manager-selfupdate"

# Une mise a jour (git + pip + redemarrage + controle de sante) depasse
# rarement quelques minutes. Au-dela, on considere l'etat comme perime
# plutot que de bloquer indefiniment l'interface sur "en cours".
STALE_AFTER_SECONDS = 1800

# Au-dela, un compte rendu de mise a jour n'est plus une nouvelle : il
# decrit un evenement passe, avec des consignes qui ont pu cesser d'etre
# valables. Un jour laisse largement le temps de le lire.
REPORT_TTL_SECONDS = 24 * 3600

# Un libelle de cible qui commence par vX est un tag ; « main @ abc1234 »
# n'en est pas un et ne se compare pas a un numero de version.
_TAG_LABEL = re.compile(r"^v\d")

STABLE = "stable"
DEV = "dev"


class AppUpdateError(RuntimeError):
    pass


@dataclass
class UpdateTarget:
    kind: str                # "stable" ou "dev"
    label: str               # "v1.1.0" ou "main @ a1b2c3d"
    ref: str                 # reference git resolue (tag ou sha)
    commit: str              # sha court
    available: bool          # vrai si cette version apporte quelque chose de nouveau
    warning: str = ""        # avertissement affiche a cote du bouton
    # Vrai quand la version est deja contenue dans l'historique deploye : la
    # proposer ferait RECULER la branche, pas avancer.
    already_included: bool = False


@dataclass
class AppUpdateStatus:
    current_commit: str | None = None
    current_tag: str | None = None
    current_version: str = version_module.VERSION
    dirty: bool = False
    targets: list[UpdateTarget] = field(default_factory=list)
    new_commits: list[str] = field(default_factory=list)   # sujets, du plus recent au plus ancien
    fetch_error: str = ""
    git_available: bool = True
    # Vrai quand l'echec vient des identifiants et non du reseau : la page
    # met alors en avant l'enregistrement d'un jeton GitHub.
    auth_required: bool = False
    remote_url: str = ""
    # Branche courante, ou "" si le depot est en HEAD detache. Cet etat
    # merite d'etre signale : un `git pull` y annonce "Fast-forward" et fait
    # avancer HEAD, mais laisse la BRANCHE en arriere - le `git push` suivant
    # ne pousse alors rien d'autre que les tags, sans le dire.
    branch: str = ""
    # Ecart entre la branche locale et GitHub. Un retard signifie que le
    # prochain `git push` sera refuse (non-fast-forward) : autant le dire
    # ici plutot que de le laisser decouvrir au moment de livrer.
    behind_origin: int = 0
    ahead_origin: int = 0
    # Dernier tag trouve, quand le code qui tourne porte deja un numero plus
    # haut : le tag de la version installee n'a pas ete pousse. Chaine vide
    # quand tout concorde ; peut valoir "" aussi lorsqu'aucun tag n'existe -
    # `untagged` distingue les deux cas.
    untagged_version: str | None = None
    # `push.followTags` est-il pose sur le depot ? S'il l'est, l'oubli du
    # `--tags` ne peut plus se reproduire, et l'ecran peut le dire au lieu
    # de repeter une consigne que personne n'appliquera.
    follow_tags: bool = False

    @property
    def untagged(self) -> bool:
        return self.untagged_version is not None

    @property
    def detached(self) -> bool:
        return self.git_available and not self.branch

    @property
    def diverged(self) -> bool:
        return self.behind_origin > 0

    def target(self, kind: str) -> UpdateTarget | None:
        for candidate in self.targets:
            if candidate.kind == kind:
                return candidate
        return None


# ---------------------------------------------------------------------------
# Lecture git
# ---------------------------------------------------------------------------

def _git(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    """Appel git dans le depot deploye.

    `safe.directory` est force : le depot appartient a l'utilisateur admin
    alors que le service tourne en root, ce que git refuse par defaut
    (« dubious ownership »).

    L'environnement vient de `gitauth` : il interdit toute invite au
    clavier (il n'y a pas de terminal ici) et fournit, si un jeton est
    enregistre, de quoi s'authentifier aupres de GitHub."""
    cmd = [
        "git", "-C", REPO_DIR,
        "-c", f"safe.directory={REPO_DIR}",
        *gitauth.git_config_args(),
        *args,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **gitauth.git_env()},
        )
    except FileNotFoundError:
        return 127, "", "git n'est pas installe."
    except subprocess.SubprocessError as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _git_out(*args: str) -> str | None:
    code, out, _ = _git(*args)
    return out if code == 0 and out else None


# Un tag annote qui reste sur le serveur alors que les commits sont partis :
# c'est ce qui s'est produit a chaque livraison de la v1.11.0 a la v1.14.0.
# `git push origin main` ne pousse PAS les tags par defaut, et l'ecran des
# mises a jour nommait alors une version stable plus ancienne que celle qui
# tourne. Le rappel dans la documentation n'a pas suffi : quatre fois de
# suite, le `--tags` a ete oublie.
#
# `push.followTags` supprime l'etape humaine : git joint de lui-meme les tags
# annotes accessibles depuis ce qui est pousse - y compris quand la branche
# est deja a jour et qu'il n'y a rien d'autre a envoyer.
PUSH_FOLLOW_TAGS = "push.followTags"


def ensure_push_follow_tags() -> bool:
    """Pose `push.followTags` sur le depot deploye s'il ne l'est pas.

    Appelee au demarrage du service : `install.sh` le pose aussi, mais une
    installation mise a jour depuis l'interface ne repasse jamais par lui.
    Reglage local au depot, sans effet ailleurs, et sans risque : il ne
    fait qu'ajouter des tags a un push que l'on a demande."""
    current = _git_out("config", "--local", "--get", PUSH_FOLLOW_TAGS)
    if (current or "").strip().lower() == "true":
        return False
    code, _, err = _git("config", "--local", PUSH_FOLLOW_TAGS, "true")
    if code != 0:
        logger.warning("Reglage %s non applique : %s", PUSH_FOLLOW_TAGS, err)
        return False
    logger.info("Reglage git %s active sur %s", PUSH_FOLLOW_TAGS, REPO_DIR)
    return True


def push_follow_tags_enabled() -> bool:
    return (_git_out("config", "--local", "--get", PUSH_FOLLOW_TAGS) or "").strip().lower() == "true"


def version_tuple(label: str) -> tuple[int, ...]:
    """« v1.10.0 » -> (1, 10, 0), pour comparer des numeros et non des
    chaines : « v1.10.0 » est APRES « v1.9.0 », ce qu'un tri alphabetique
    inverserait. Une etiquette illisible vaut (0,) : elle ne peut alors
    jamais passer pour la plus recente."""
    match = re.match(r"^v?(\d+(?:\.\d+)*)", (label or "").strip())
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def _is_ancestor(commit: str, of: str) -> bool:
    """Vrai si `commit` est deja contenu dans l'historique de `of`."""
    return _git("merge-base", "--is-ancestor", commit, of)[0] == 0


def get_status(fetch: bool = True) -> AppUpdateStatus:
    """Etat des mises a jour de NAS Manager. `fetch=False` evite l'acces
    reseau (utile pour un affichage rapide, ou hors connexion)."""
    status = AppUpdateStatus()

    if _git_out("rev-parse", "--git-dir") is None:
        status.git_available = False
        status.fetch_error = (
            "Ce dossier n'est pas un depot git : la mise a jour automatique "
            "n'est possible que sur une installation faite avec 'git clone'."
        )
        return status

    status.current_commit = _git_out("rev-parse", "--short", "HEAD")
    status.current_tag = _git_out("describe", "--tags", "--exact-match", "HEAD")
    status.dirty = bool(_git_out("status", "--porcelain"))

    status.remote_url = _git_out("remote", "get-url", "origin") or ""
    # --show-current renvoie une chaine vide en HEAD detache : c'est
    # exactement ce qu'on veut detecter.
    status.branch = _git_out("branch", "--show-current") or ""

    if fetch:
        # --prune --tags : sans ca, un tag supprime en amont resterait
        # proposable indefiniment.
        code, _, err = _git("fetch", "--prune", "--tags", "origin", timeout=120)
        if code != 0:
            raw = err or "Impossible de contacter GitHub."
            status.auth_required = gitauth.looks_like_auth_failure(raw)
            status.fetch_error = gitauth.explain_failure(raw, status.remote_url)

    head = _git_out("rev-parse", "HEAD")

    # Ecart avec GitHub. `rev-list --count A..B` compte ce que B a en plus.
    counts = _git_out("rev-list", "--left-right", "--count", "HEAD...origin/main")
    if counts:
        parts = counts.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            status.ahead_origin, status.behind_origin = int(parts[0]), int(parts[1])

    # --- Version stable : le tag de plus haut numero accessible depuis
    # origin/main. `describe --abbrev=0` donnerait le tag le plus PROCHE dans
    # le graphe, pas le plus RECENT : apres une fusion, l'ordre des parents
    # peut mettre un ancien tag a portee plus courte et faire annoncer une
    # version depassee comme « derniere version publiee ». Le tri `-v:refname`
    # compare les numeros (v1.10.0 apres v1.9.0, ce qu'un tri alphabetique
    # rate), et `--merged` garantit qu'on ne propose que du deja accessible.
    tags = _git_out("tag", "--sort=-v:refname", "--merged", "origin/main")
    latest_tag = tags.splitlines()[0].strip() if tags else None

    # Le code qui tourne annonce un numero plus haut que le dernier tag
    # trouve : c'est que le tag n'a pas ete pousse (un `git push origin main`
    # sans `--tags`). L'ecran serait sinon coherent mais incomprehensible - il
    # nommerait une version plus ancienne que celle affichee juste au-dessus,
    # sans dire pourquoi (v1.7.1).
    if version_tuple(version_module.VERSION) > version_tuple(latest_tag or ""):
        status.untagged_version = latest_tag or ""
    status.follow_tags = push_follow_tags_enabled()

    if latest_tag:
        tag_commit = _git_out("rev-list", "-n", "1", latest_tag)
        # Une version DEJA contenue dans l'historique deploye ne doit pas
        # etre proposee : l'installer ferait reculer la branche au lieu de
        # l'avancer. C'est le cas courant quand la livraison a ete integree
        # par une fusion, le tag se retrouvant alors sous la pointe.
        included = bool(tag_commit and head and _is_ancestor(tag_commit, head))
        status.targets.append(UpdateTarget(
            kind=STABLE, label=latest_tag, ref=latest_tag,
            commit=(tag_commit or "")[:7],
            available=bool(tag_commit and tag_commit != head and not included),
            already_included=included and tag_commit != head,
        ))

    # --- Version de developpement : le dernier commit de origin/main ---
    dev_commit = _git_out("rev-parse", "origin/main")
    if dev_commit:
        dev_included = bool(head and _is_ancestor(dev_commit, head))
        status.targets.append(UpdateTarget(
            kind=DEV, label=f"main @ {dev_commit[:7]}", ref="origin/main",
            commit=dev_commit[:7],
            available=dev_commit != head and not dev_included,
            already_included=dev_included and dev_commit != head,
            warning=(
                "Version de developpement : ce commit n'a pas ete publie comme "
                "version stable, il peut contenir du travail en cours."
            ),
        ))
        if dev_commit != head:
            log = _git_out("log", "--pretty=%s", "--no-merges", "-20", f"HEAD..{dev_commit}")
            status.new_commits = log.splitlines() if log else []

    return status


def test_connection() -> str:
    """Verifie que le depot distant est joignable ET lisible, sans rien
    modifier. `ls-remote` suffit : il interroge GitHub sans ecrire dans le
    depot local, donc on peut le lancer autant de fois qu'on veut."""
    remote = _git_out("remote", "get-url", "origin") or ""
    code, out, err = _git("ls-remote", "--heads", "origin", timeout=60)
    if code != 0:
        raise AppUpdateError(gitauth.explain_failure(err or "Echec inconnu.", remote))
    branches = len(out.splitlines())
    return f"Connexion a GitHub reussie ({branches} branche(s) visible(s))."


# ---------------------------------------------------------------------------
# Etat d'une mise a jour en cours / terminee
# ---------------------------------------------------------------------------

@dataclass
class UpdateProgress:
    status: str = "idle"        # idle|running|success|rolled_back|failed
    step: str = ""
    target_label: str = ""
    previous_commit: str = ""
    started_epoch: float = 0.0
    finished_epoch: float = 0.0
    message: str = ""
    log_tail: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def stale(self) -> bool:
        """Une mise a jour 'en cours' depuis trop longtemps : le script a
        probablement ete tue (coupure de courant, OOM). On ne laisse pas
        l'interface bloquee dessus."""
        return self.running and (time.time() - self.started_epoch) > STALE_AFTER_SECONDS

    @property
    def obsolete(self) -> bool:
        """Le compte rendu ne decrit plus la situation presente (v1.5.3).

        Le fichier d'etat n'est jamais efface : sans ca, un « Mise a jour
        terminee — v1.4.3 » restait affiche en tete de page des semaines plus
        tard, avec ses consignes d'alors, alors que la machine tournait deja
        trois versions plus loin. Un bandeau qui ne peut pas disparaitre finit
        par etre lu comme l'etat courant.

        Deux facons d'etre depasse :
        - un succes qui annonce une version qui n'est plus celle qui tourne ;
        - n'importe quel compte rendu vieux de plus d'un jour.

        Un ECHEC, lui, n'est jamais masque par le premier critere : il annonce
        justement une version qui n'a pas ete installee, et c'est precisement
        ce qu'il faut continuer a voir."""
        if self.status in ("idle", "running"):
            return False
        if self.finished_epoch and (time.time() - self.finished_epoch) > REPORT_TTL_SECONDS:
            return True
        if self.status == "success" and _TAG_LABEL.match(self.target_label or ""):
            return self.target_label != f"v{version_module.VERSION}"
        return False


def read_progress() -> UpdateProgress:
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return UpdateProgress()
    known = {f for f in UpdateProgress.__dataclass_fields__}
    return UpdateProgress(**{k: v for k, v in raw.items() if k in known})


def write_progress(progress: UpdateProgress) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(asdict(progress), indent=2))
    os.chmod(STATE_FILE, 0o600)


def clear_progress() -> None:
    try:
        STATE_FILE.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------

def _spawn_detached(cmd: list[str]) -> subprocess.CompletedProcess:
    """Isole pour les tests : la vraie execution lance un service
    transitoire systemd, qui survit au redemarrage de NAS Manager."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def start_update(kind: str) -> UpdateTarget:
    """Verifie que la mise a jour est possible, puis lance le script
    detache. Toutes les verifications sont refaites ICI, au moment du
    clic : la page affichee peut dater de plusieurs minutes."""
    if kind not in (STABLE, DEV):
        raise AppUpdateError(f"Type de mise a jour inconnu : '{kind}'.")

    progress = read_progress()
    if progress.running and not progress.stale:
        raise AppUpdateError(
            "Une mise a jour est deja en cours. Attends qu'elle se termine."
        )

    status = get_status(fetch=True)
    if not status.git_available:
        raise AppUpdateError(status.fetch_error)
    if status.fetch_error:
        # Le message d'authentification est deja explicite et actionnable :
        # le prefixer d'un "Impossible de recuperer..." le noierait.
        raise AppUpdateError(
            status.fetch_error if status.auth_required
            else f"Impossible de recuperer les versions depuis GitHub : {status.fetch_error}"
        )
    if status.dirty:
        raise AppUpdateError(
            "Des fichiers du serveur ont ete modifies a la main : la mise a "
            "jour les ecraserait. Annule ces modifications (git checkout .) "
            "ou sauvegarde-les avant de recommencer."
        )

    target = status.target(kind)
    if target is None:
        raise AppUpdateError("Aucune version de ce type n'est disponible.")
    if not target.available:
        raise AppUpdateError(f"NAS Manager est deja sur {target.label}.")

    if not os.path.exists(UPDATER_SCRIPT):
        raise AppUpdateError(
            "Le script de mise a jour est introuvable. Relance './install.sh' "
            "puis reessaie."
        )

    write_progress(UpdateProgress(
        status="running", step="Lancement de la mise a jour",
        target_label=target.label,
        previous_commit=status.current_commit or "",
        started_epoch=time.time(),
    ))

    cmd = _build_launch_command(target.ref, target.label)
    result = _spawn_detached(cmd)
    if result.returncode != 0:
        failure = (result.stderr or result.stdout or "").strip()
        write_progress(UpdateProgress(
            status="failed", step="Lancement",
            target_label=target.label,
            started_epoch=time.time(), finished_epoch=time.time(),
            message=failure or "Le script de mise a jour n'a pas pu demarrer.",
        ))
        raise AppUpdateError(
            failure or "Le script de mise a jour n'a pas pu demarrer."
        )
    logger.info("Mise a jour de NAS Manager lancee vers %s", target.label)
    return target


def _build_launch_command(ref: str, label: str) -> list[str]:
    """systemd-run place le script dans son propre service transitoire :
    il n'est donc pas tue quand nas-manager.service redemarre. Sans
    systemd-run (environnement de test, conteneur), on retombe sur
    `setsid` qui detache au moins le processus du worker web."""
    if shutil.which("systemd-run"):
        return [
            "systemd-run",
            f"--unit={SERVICE_UNIT}",
            "--collect",              # nettoie l'unite une fois terminee
            "--property=Type=oneshot",
            "--property=TimeoutStartSec=1800",
            "/bin/bash", UPDATER_SCRIPT, ref, label,
        ]
    return ["setsid", "/bin/bash", UPDATER_SCRIPT, ref, label]


def resync_with_origin() -> str:
    """Fusionne ce qui est sur GitHub dans la branche locale (v1.5.1).

    Pourquoi ce bouton existe : une mise a jour installee par une version
    ANTERIEURE a la v1.4.3 deplacait la branche de force et la laissait en
    retard sur origin/main - le push suivant etait rejete. Le correctif ne
    peut pas s'appliquer a sa propre installation (le script execute est
    celui present AVANT la mise a jour), donc la reparation doit se faire
    une fois a la main. Autant qu'elle se fasse d'ici plutot qu'en SSH.

    Volontairement une fusion et RIEN D'AUTRE : pas de push. Le jeton
    recommande est en lecture seule, et pousser depuis une interface web
    sur l'historique d'un depot demande une intention explicite.

    En cas de conflit, la fusion est ANNULEE : mieux vaut un depot intact
    et un message clair qu'un depot laisse au milieu d'une fusion, ou plus
    rien ne fonctionne."""
    if _git_out("rev-parse", "--git-dir") is None:
        raise AppUpdateError("Ce dossier n'est pas un depot git.")

    if _git_out("status", "--porcelain"):
        raise AppUpdateError(
            "Des fichiers du serveur ont ete modifies a la main : la fusion "
            "les melangerait aux changements distants. Annule-les d'abord."
        )

    branch = _git_out("branch", "--show-current")
    if not branch:
        raise AppUpdateError(
            "Le depot n'est sur aucune branche (HEAD detache). Place-toi "
            "d'abord sur main : git checkout main"
        )

    code, _, err = _git("fetch", "--prune", "--tags", "origin", timeout=120)
    if code != 0:
        raise AppUpdateError(gitauth.explain_failure(err or "GitHub injoignable.",
                                                     _git_out("remote", "get-url", "origin") or ""))

    before = _git_out("rev-parse", "HEAD")
    code, out, err = _git("merge", "--no-edit", "origin/main", timeout=120)
    if code != 0:
        # Ne jamais laisser le depot au milieu d'une fusion : le service
        # tournerait alors sur des fichiers contenant des marqueurs de
        # conflit.
        _git("merge", "--abort")
        raise AppUpdateError(
            "La fusion s'est heurtee a un conflit et a ete annulee : le depot "
            "est intact. Resous-le en SSH avec "
            "'git pull --no-rebase origin main'. Detail : "
            + (err or out or "")[:300]
        )

    after = _git_out("rev-parse", "HEAD")
    if before == after:
        return "La branche etait deja a jour avec GitHub : rien a fusionner."
    logger.warning("Branche resynchronisee avec origin/main (%s -> %s)",
                   (before or "")[:7], (after or "")[:7])
    return (
        "Branche resynchronisee avec GitHub. Ton prochain "
        "'git push origin main --tags' sera accepte."
    )


def start_rollback() -> str:
    """Retour manuel a la version precedente, a partir du commit note lors
    de la derniere mise a jour."""
    progress = read_progress()
    previous = (progress.previous_commit or "").strip()
    if not previous:
        raise AppUpdateError(
            "Aucune version precedente connue : il n'y a pas eu de mise a "
            "jour depuis cette interface."
        )
    if progress.running and not progress.stale:
        raise AppUpdateError("Une mise a jour est en cours. Attends qu'elle se termine.")
    # `cat-file -e` ne produit aucune sortie : c'est le code de retour qui
    # dit si l'objet existe.
    if _git("cat-file", "-e", f"{previous}^{{commit}}")[0] != 0:
        raise AppUpdateError(
            f"Le commit precedent ({previous}) est introuvable dans le depot."
        )

    write_progress(UpdateProgress(
        status="running", step="Retour a la version precedente",
        target_label=previous, previous_commit="",
        started_epoch=time.time(),
    ))
    result = _spawn_detached(_build_launch_command(previous, previous))
    if result.returncode != 0:
        raise AppUpdateError(
            (result.stderr or result.stdout or "").strip()
            or "Le retour arriere n'a pas pu demarrer."
        )
    return previous
