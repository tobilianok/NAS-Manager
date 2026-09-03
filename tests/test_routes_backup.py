import io
import json
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main, auth, configbackup


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: p == "bonmotdepasse")
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "louis", "password": "bonmotdepasse"}, follow_redirects=False)
        assert resp.status_code == 302
        yield c


def _make_archive(tmp_path: Path, sections=("config", "accounts", "stacks")) -> Path:
    """Fabrique une archive minimale mais valide (sans toucher au systeme)."""
    root = tmp_path / "src"
    (root / "nas-manager").mkdir(parents=True)
    (root / "accounts").mkdir(parents=True)
    (root / "stacks" / "nginx").mkdir(parents=True)
    (root / "system").mkdir(parents=True)
    (root / "zfs").mkdir(parents=True)

    if "config" in sections:
        (root / "nas-manager" / "shares.json").write_text("[]")
    if "accounts" in sections:
        (root / "accounts" / "accounts.json").write_text(json.dumps({
            "share_users": [{"username": "alice", "full_name": "Alice", "extra_groups": [], "is_nasadmin": False}],
            "system_accounts": [{"username": "louis", "uid": 1000, "full_name": "Louis",
                                 "is_sudo": True, "is_nasadmin": True, "extra_groups": []}],
            "groups": [],
        }))
    if "stacks" in sections:
        (root / "stacks" / "nginx" / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx\n")

    (root / "system" / "90-nas-manager.yaml").write_text("network:\n  version: 2\n")
    (root / "zfs" / "topology.txt").write_text("# DOCUMENTATION\n")
    (root / "manifest.json").write_text(json.dumps({
        "format": configbackup.ARCHIVE_FORMAT_VERSION,
        "created_at": "2026-09-03T10:00:00", "hostname": "srv-nas",
        "contents": {"shares": 0, "stacks": 1}, "note": "secret",
    }))

    archive = tmp_path / "backup.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for item in sorted(root.iterdir()):
            tar.add(item, arcname=item.name)
    return archive


# ---------------------------------------------------------------------------
# Page et telechargement
# ---------------------------------------------------------------------------

def test_backup_page_warns_about_secrets(client):
    resp = client.get("/backup")
    assert resp.status_code == 200
    assert "secret" in resp.text.lower()
    assert "/backup/download" in resp.text


def test_backup_download_sends_archive_and_cleans_up(client, tmp_path, monkeypatch):
    archive_dir = tmp_path / "generated"
    archive_dir.mkdir()
    archive = archive_dir / "nas-manager-config-srv-nas.tar.gz"
    archive.write_bytes(b"fake-archive")
    monkeypatch.setattr(configbackup, "create_archive", lambda *a, **k: archive)

    resp = client.get("/backup/download")
    assert resp.status_code == 200
    assert resp.content == b"fake-archive"
    assert "nas-manager-config-srv-nas.tar.gz" in resp.headers["content-disposition"]
    # Le dossier temporaire est nettoye apres l'envoi : l'archive contient
    # des empreintes de mots de passe, elle ne reste pas sur le NAS.
    assert not archive_dir.exists()


def test_backup_download_requires_login():
    with TestClient(main.app) as anonymous:
        resp = anonymous.get("/backup/download", follow_redirects=False)
    assert resp.status_code in (302, 307, 401)


# ---------------------------------------------------------------------------
# Import : apercu avant toute ecriture
# ---------------------------------------------------------------------------

def test_restore_preview_shows_contents_without_writing(client, tmp_path, monkeypatch):
    archive = _make_archive(tmp_path)
    restored = []
    monkeypatch.setattr(configbackup, "restore", lambda root, sections: restored.append(sections))

    resp = client.post("/backup/restore", files={"archive": ("backup.tar.gz", archive.read_bytes())})
    assert resp.status_code == 200
    assert "srv-nas" in resp.text
    assert "ACCES ADMIN" in resp.text or "louis" in resp.text
    assert "version: 2" in resp.text          # config reseau affichee, non restauree
    assert restored == []                     # rien n'a ete applique a ce stade


def test_restore_preview_rejects_empty_file(client):
    resp = client.post("/backup/restore", files={"archive": ("vide.tar.gz", b"")})
    assert resp.status_code == 400
    assert "vide" in resp.text.lower()


def test_restore_preview_rejects_oversized_upload(client, monkeypatch):
    monkeypatch.setattr(configbackup, "MAX_ARCHIVE_BYTES", 10)
    resp = client.post("/backup/restore", files={"archive": ("gros.tar.gz", b"x" * 100)})
    assert resp.status_code == 400
    assert "volumineux" in resp.text


def test_restore_preview_rejects_foreign_archive(client, tmp_path):
    path = tmp_path / "autre.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("hello.txt")
        payload = b"hello"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    resp = client.post("/backup/restore", files={"archive": ("autre.tar.gz", path.read_bytes())})
    assert resp.status_code == 400
    assert "NAS Manager" in resp.text


# ---------------------------------------------------------------------------
# Application : mot de passe obligatoire, chemin verifie
# ---------------------------------------------------------------------------

def _extracted_root(tmp_path, monkeypatch):
    """Reproduit ce que fait la route d'apercu : une archive extraite dans un
    dossier temporaire au nom attendu."""
    workdir = tmp_path / "nas-manager-restore-abc"
    root = workdir / "content"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({
        "format": configbackup.ARCHIVE_FORMAT_VERSION, "created_at": "2026-09-03T10:00:00",
        "hostname": "srv-nas", "contents": {}, "note": "",
    }))
    return root


def test_restore_apply_requires_correct_password(client, tmp_path, monkeypatch):
    root = _extracted_root(tmp_path, monkeypatch)
    restored = []
    monkeypatch.setattr(configbackup, "restore", lambda r, s: restored.append(s) or ["ok"])

    resp = client.post("/backup/restore/apply", data={
        "archive_root": str(root), "sections": ["config"], "confirm_password": "mauvais",
    })
    assert resp.status_code == 400
    assert "Mot de passe incorrect" in resp.text
    assert restored == []


def test_restore_apply_runs_and_reports(client, tmp_path, monkeypatch):
    root = _extracted_root(tmp_path, monkeypatch)
    monkeypatch.setattr(configbackup, "restore", lambda r, s: [f"sections restaurees : {','.join(s)}"])

    resp = client.post("/backup/restore/apply", data={
        "archive_root": str(root), "sections": ["config", "accounts"],
        "confirm_password": "bonmotdepasse",
    })
    assert resp.status_code == 200
    assert "sections restaurees : config,accounts" in resp.text
    # Le dossier temporaire est nettoye apres application.
    assert not root.parent.exists()


def test_restore_apply_rejects_arbitrary_path(client, tmp_path):
    """Le chemin vient d'un champ de formulaire : il ne doit pas permettre de
    pointer n'importe ou sur le disque."""
    evil = tmp_path / "etc"
    evil.mkdir()
    resp = client.post("/backup/restore/apply", data={
        "archive_root": str(evil), "sections": ["config"], "confirm_password": "bonmotdepasse",
    })
    assert resp.status_code == 400


def test_restore_apply_rejects_expired_archive(client, tmp_path):
    root = tmp_path / "nas-manager-restore-xyz" / "content"
    root.mkdir(parents=True)   # pas de manifest.json : dossier deja nettoye
    resp = client.post("/backup/restore/apply", data={
        "archive_root": str(root), "sections": ["config"], "confirm_password": "bonmotdepasse",
    })
    assert resp.status_code == 400
    assert "expiree" in resp.text or "introuvable" in resp.text


def test_restore_apply_surfaces_module_error(client, tmp_path, monkeypatch):
    root = _extracted_root(tmp_path, monkeypatch)

    def refuse(r, s):
        raise configbackup.ConfigBackupError("Aucune section selectionnee - rien a restaurer.")

    monkeypatch.setattr(configbackup, "restore", refuse)
    resp = client.post("/backup/restore/apply", data={
        "archive_root": str(root), "sections": [], "confirm_password": "bonmotdepasse",
    })
    assert resp.status_code == 400
    assert "Aucune section" in resp.text
