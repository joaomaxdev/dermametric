"""Testes do autoatualizador e da exclusão protegida de prontuários.

Cobrem as rotas ``/api/system/update/*`` com mocks (nenhum acesso à rede nem
reinício do processo) e ``POST /api/patients/{id}/delete`` exigindo a senha
mestra, preservando dados em caso de falha e removendo registros e arquivos
criptografados no sucesso.
"""

from __future__ import annotations

import socket
import ssl
import sys
import types
import urllib.error
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import database as db, security, updater
from backend.main import app
from backend.version import APP_VERSION

PASSWORD = "senha-mestra-updater-1"


# ---------------------------------------------------------------------------
# Estação isolada
# ---------------------------------------------------------------------------
@pytest.fixture()
def station(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    data_dir = tmp_path / "data"
    for directory in (storage, data_dir, storage / "uploads", storage / "processed"):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(db, "DB_PATH", storage / "dermametric.db")
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "STORAGE_DIR", storage)
    monkeypatch.setattr(db, "UPLOAD_DIR", storage / "uploads")
    monkeypatch.setattr(db, "PROCESSED_DIR", storage / "processed")
    monkeypatch.setattr(db, "WORK_DIR", storage / "work")
    monkeypatch.setattr(security, "STORAGE_DIR", storage)
    monkeypatch.setattr(security, "KEY_PATH", storage / ".secret.key")
    monkeypatch.setattr(security, "_fernet", None)
    monkeypatch.setattr(security, "_cached_key", None)

    db.init_db()
    with TestClient(app) as client:
        response = client.post("/api/auth/setup", params={"password": PASSWORD})
        assert response.status_code == 200, response.text
        token = response.json()["access_token"]
        yield client, {"Authorization": f"Bearer {token}"}


def synthetic_png() -> bytes:
    image = np.full((180, 180, 3), (150, 175, 205), dtype=np.uint8)
    cv2.circle(image, (90, 90), 34, (228, 232, 240), -1)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    return buffer.tobytes()


def patient_with_images(client) -> tuple[str, str, list[Path]]:
    """Cria um prontuário com uma sessão e duas imagens criptografadas em disco."""
    response = client.post(
        "/api/patients",
        json={"name": "Paciente Exclusão Teste", "sessions": 2},
    )
    assert response.status_code == 200, response.text
    patient = response.json()
    patient_id = patient["id"]
    session_id = patient["sessions"][0]["id"]

    png = synthetic_png()
    upload = client.post(
        f"/api/sessions/{session_id}/upload",
        files={
            "clean_image": ("clean.png", png, "image/png"),
            "ruler_image": ("ruler.png", png, "image/png"),
        },
        data={"label": "Baseline", "captured_at": "2026-02-01"},
    )
    assert upload.status_code == 200, upload.text

    with db.connection() as conn:
        rows = conn.execute(
            "SELECT clean_image_path, ruler_image_path FROM sessions WHERE patient_id = ?",
            (patient_id,),
        ).fetchall()
    files: list[Path] = []
    for row in rows:
        for field in ("clean_image_path", "ruler_image_path"):
            candidate = db.storage_absolute(row[field])
            if candidate is not None:
                files.append(candidate)
    assert files and all(path.is_file() for path in files), files
    return patient_id, patient["code"], files


# ---------------------------------------------------------------------------
# Versão e rotas de atualização
# ---------------------------------------------------------------------------
def test_app_version_matches_current_release():
    assert APP_VERSION == "1.0.5"


def test_health_reports_app_version(station):
    client, _ = station
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["version"] == APP_VERSION


def test_update_check_returns_release_info(station, monkeypatch):
    client, _ = station
    payload = {
        "current_version": APP_VERSION,
        "latest_version": "1.0.6",
        "update_available": True,
        "release_name": "v1.0.6",
        "release_notes": "- Correções clínicas\n- Melhorias",
        "html_url": "https://github.com/joaomaxdev/dermametric/releases/tag/v1.0.6",
        "asset_name": "DermaMetric-1.0.6-x86_64.AppImage",
        "asset_size": 42_000_000,
        "can_auto_update": True,
        "install_mode": "linux-appimage",
    }
    monkeypatch.setattr(updater, "check_for_updates", lambda timeout=10.0: payload)

    response = client.get("/api/system/update/check")
    assert response.status_code == 200
    assert response.json() == payload


def test_update_check_returns_502_when_github_is_offline(station, monkeypatch):
    client, _ = station

    def _offline(timeout=10.0):
        raise RuntimeError("Sem conexão com a internet para verificar atualizações.")

    monkeypatch.setattr(updater, "check_for_updates", _offline)

    response = client.get("/api/system/update/check")
    assert response.status_code == 502
    assert "Sem conexão" in response.json()["detail"]


def test_update_status_returns_worker_state(station, monkeypatch):
    client, _ = station
    state = {
        "status": "downloading",
        "progress": 37,
        "downloaded_bytes": 15_500_000,
        "total_bytes": 42_000_000,
        "message": "Baixando DermaMetric.AppImage…",
        "target_version": "1.0.6",
        "error": None,
    }
    monkeypatch.setattr(updater, "get_update_status", lambda: dict(state))

    response = client.get("/api/system/update/status")
    assert response.status_code == 200
    assert response.json() == state


def test_update_apply_starts_worker(station, monkeypatch):
    client, _ = station
    state = {"status": "downloading", "progress": 2, "message": "Iniciando download"}
    monkeypatch.setattr(updater, "start_update", lambda: dict(state))

    response = client.post("/api/system/update/apply")
    assert response.status_code == 200
    assert response.json() == state


def test_update_apply_returns_400_when_not_possible(station, monkeypatch):
    client, _ = station

    def _already_latest():
        raise ValueError("O DermaMetric já está na versão mais recente.")

    monkeypatch.setattr(updater, "start_update", _already_latest)

    response = client.post("/api/system/update/apply")
    assert response.status_code == 400
    assert "mais recente" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Exclusão de prontuário com senha mestra
# ---------------------------------------------------------------------------
def test_delete_patient_rejects_missing_or_wrong_password(station):
    client, _ = station
    patient_id, _, files = patient_with_images(client)

    missing = client.post(f"/api/patients/{patient_id}/delete", json={})
    assert missing.status_code == 401
    assert missing.json()["detail"] == "Senha mestra incorreta."

    wrong = client.post(
        f"/api/patients/{patient_id}/delete",
        json={"password": "senha-errada"},
    )
    assert wrong.status_code == 401

    # Nada pode ter sido apagado depois das tentativas inválidas.
    assert client.get(f"/api/patients/{patient_id}").status_code == 200
    assert all(path.is_file() for path in files)
    with db.connection() as conn:
        sessions = conn.execute(
            "SELECT COUNT(*) AS total FROM sessions WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()["total"]
    assert sessions > 0


def test_delete_patient_removes_records_and_encrypted_files(station):
    client, _ = station
    patient_id, code, files = patient_with_images(client)

    response = client.post(
        f"/api/patients/{patient_id}/delete",
        json={"password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deleted"] is True
    assert payload["id"] == patient_id
    assert payload["code"] == code
    assert payload["removed_files"] >= len(files)

    assert client.get(f"/api/patients/{patient_id}").status_code == 404
    assert client.get("/api/patients").json()["count"] == 0
    assert all(not path.exists() for path in files)

    with db.connection() as conn:
        patient_sessions = conn.execute(
            "SELECT COUNT(*) AS total FROM sessions WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()["total"]
        assert patient_sessions == 0
        for table in ("striae_measurements", "stria_roi", "patient_pins"):
            remaining = conn.execute(
                f"SELECT COUNT(*) AS total FROM {table}"
            ).fetchone()["total"]
            assert remaining == 0, f"{table} ainda possui {remaining} registros"


def test_delete_patient_accepts_master_password_alias(station):
    client, _ = station
    patient_id, _, _ = patient_with_images(client)

    response = client.post(
        f"/api/patients/{patient_id}/delete",
        json={"master_password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == patient_id


def test_delete_unknown_patient_returns_404(station):
    client, _ = station
    response = client.post(
        "/api/patients/paciente-inexistente/delete",
        json={"password": PASSWORD},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# SSL/certifi e diferenciação de erros do autoatualizador
# ---------------------------------------------------------------------------
def test_ssl_context_prefers_certifi_cafile(monkeypatch):
    sentinel = object()
    calls: list[str | None] = []
    fake_certifi = types.SimpleNamespace(where=lambda: "/bundle/certifi/cacert.pem")
    monkeypatch.setitem(sys.modules, "certifi", fake_certifi)

    def fake_create_default_context(*, cafile=None):
        calls.append(cafile)
        return sentinel

    monkeypatch.setattr(ssl, "create_default_context", fake_create_default_context)

    assert updater._create_ssl_context() is sentinel
    assert calls == ["/bundle/certifi/cacert.pem"]


def test_ssl_context_falls_back_without_certifi(monkeypatch):
    sentinel = object()
    calls: list[str | None] = []
    # ``None`` em sys.modules faz ``import certifi`` falhar (bundle incompleto).
    monkeypatch.setitem(sys.modules, "certifi", None)

    def fake_create_default_context(*, cafile=None):
        calls.append(cafile)
        return sentinel

    monkeypatch.setattr(ssl, "create_default_context", fake_create_default_context)

    assert updater._create_ssl_context() is sentinel
    assert calls == [None]


def test_check_for_updates_reports_http_status(monkeypatch):
    def _http(url, timeout=10.0):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(updater, "_github_get_json", _http)

    with pytest.raises(RuntimeError) as excinfo:
        updater.check_for_updates()
    message = str(excinfo.value)
    assert "HTTP 403" in message
    assert "Forbidden" in message
    assert "Sem conexão" not in message


def test_check_for_updates_reports_ssl_error_without_network_claim(monkeypatch):
    def _ssl_failure(url, timeout=10.0):
        raise urllib.error.URLError(
            ssl.SSLCertVerificationError(1, "certificate verify failed")
        )

    monkeypatch.setattr(updater, "_github_get_json", _ssl_failure)

    with pytest.raises(RuntimeError) as excinfo:
        updater.check_for_updates()
    message = str(excinfo.value)
    assert "certificado SSL" in message
    assert "certifi" in message
    assert "Sem conexão" not in message


def test_check_for_updates_reports_dns_and_timeout(monkeypatch):
    def _dns_failure(url, timeout=10.0):
        raise urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))

    monkeypatch.setattr(updater, "_github_get_json", _dns_failure)
    with pytest.raises(RuntimeError, match="DNS"):
        updater.check_for_updates()

    def _timeout(url, timeout=10.0):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(updater, "_github_get_json", _timeout)
    with pytest.raises(RuntimeError, match="Tempo esgotado"):
        updater.check_for_updates()


def test_download_reports_http_error_and_cleans_partial_file(tmp_path, monkeypatch):
    def _http(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(updater, "_urlopen", _http)
    target = tmp_path / "DermaMetric-Setup.exe"

    with pytest.raises(RuntimeError, match="HTTP 404"):
        updater._download_with_progress("https://example.test/setup.exe", target)
    assert not target.exists()


# ---------------------------------------------------------------------------
# Caminhos Windows no storage e em /api/files
# ---------------------------------------------------------------------------
def test_storage_paths_normalize_windows_separators(station):
    db.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    resolved = db.storage_absolute("uploads\\T2_clean_abc.enc")
    assert resolved == (Path(db.STORAGE_DIR) / "uploads" / "T2_clean_abc.enc").resolve()

    # Traversal e drive absoluto nunca saem da raiz do storage.
    assert db.storage_absolute("..\\..\\etc\\passwd") is None
    assert db.storage_absolute("C:\\Windows\\system32\\config") is None


def test_serialized_session_urls_use_forward_slashes():
    session = db._serialize_session(
        {
            "id": "T0",
            "clean_image_path": "uploads\\T0_clean_abc.enc",
            "ruler_image_path": "uploads\\T0_ruler_abc.enc",
        }
    )
    assert session["clean_image_url"] == "/api/files/uploads/T0_clean_abc.enc"
    assert session["ruler_image_url"] == "/api/files/uploads/T0_ruler_abc.enc"


def test_files_endpoint_accepts_windows_backslash_path(station):
    client, _ = station
    patient_id, _, _ = patient_with_images(client)
    with db.connection() as conn:
        row = conn.execute(
            "SELECT clean_image_path FROM sessions "
            "WHERE patient_id = ? AND clean_image_path IS NOT NULL LIMIT 1",
            (patient_id,),
        ).fetchone()
    assert row is not None and row["clean_image_path"]
    windows_path = str(row["clean_image_path"]).replace("/", "\\")
    encoded = quote(windows_path, safe="")

    response = client.get(f"/api/files/uploads/{encoded}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/")


def test_files_endpoint_rejects_traversal_with_backslashes(station):
    client, _ = station
    encoded = quote("uploads\\..\\..\\etc\\passwd", safe="")
    response = client.get(f"/api/files/uploads/{encoded}")
    assert response.status_code == 400
