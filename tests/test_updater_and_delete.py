"""Testes do autoatualizador e da exclusão protegida de prontuários.

Cobrem as rotas ``/api/system/update/*`` com mocks (nenhum acesso à rede nem
reinício do processo) e ``POST /api/patients/{id}/delete`` exigindo a senha
mestra, preservando dados em caso de falha e removendo registros e arquivos
criptografados no sucesso.
"""

from __future__ import annotations

from pathlib import Path

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
