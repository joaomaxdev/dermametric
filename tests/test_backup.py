"""Testes do backup proprietário cifrado ``.dmbak``.

Cobrem:

* exportação completa e individual com o magic header ``DMBAK\\x01\\x00\\x00``;
* rejeição de arquivo adulterado, arquivo comum renomeado e senha incorreta;
* restauração em banco limpo (``replace``) e em modo ``merge`` preservando
  prontuários, sessões, medições e a descriptografia das imagens;
* snapshot automático ``pre_restore_*.dmbak`` antes de gravar.

Cada teste roda em um banco/storage isolado (tmp_path), mesmo quando a suíte
completa já importou os módulos com outro diretório de teste.
"""

from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

# Isolamento antecipado quando este módulo é o primeiro a importar o backend.
TEST_ROOT = Path(__file__).resolve().parent / "tmp_backup"
if "backend.database" not in sys.modules:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    import os

    os.environ["DERMAMETRIC_STORAGE_DIR"] = str(TEST_ROOT / "storage")
    os.environ["DERMAMETRIC_DB_PATH"] = str(TEST_ROOT / "storage" / "dermametric.db")

from fastapi.testclient import TestClient  # noqa: E402

from backend import backup, database as db, security  # noqa: E402
from backend.main import app  # noqa: E402

PASSWORD = "senha-mestra-backup-1"
BACKUP_MAGIC = b"DMBAK\x01\x00\x00"
MEDIA_TYPE = "application/vnd.dermametric.backup"


# ---------------------------------------------------------------------------
# Fixtures e utilitários
# ---------------------------------------------------------------------------
@pytest.fixture()
def station(tmp_path, monkeypatch):
    """Estação isolada com banco, storage, DEK e sessão autenticada próprios."""
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
    image = np.full((240, 240, 3), (145, 170, 208), dtype=np.uint8)
    cv2.ellipse(image, (120, 120), (60, 14), 0, 0, 360, (222, 228, 238), -1)
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    return buffer.tobytes()


def measured_case(client, headers, code: str, name: str) -> dict:
    """Cria paciente com duas sessões, imagens, calibração e uma medição."""
    response = client.post(
        "/api/patients",
        json={"code": code, "name": name, "notes": "Caso de teste", "sessions": 2},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    patient = response.json()
    session_id = patient["sessions"][0]["id"]
    png = synthetic_png()
    upload = client.post(
        f"/api/sessions/{session_id}/upload",
        files={
            "clean_image": ("clean.png", png, "image/png"),
            "ruler_image": ("ruler.png", png, "image/png"),
        },
        data={"label": "Baseline", "captured_at": "2026-01-10"},
    )
    assert upload.status_code == 200, upload.text
    calibrate = client.post(
        f"/api/sessions/{session_id}/calibrate",
        json={"p1": [30, 210], "p2": [130, 210], "known_mm": 10.0},
    )
    assert calibrate.status_code == 200, calibrate.text
    register = client.post(
        f"/api/sessions/{session_id}/register",
        json={
            "landmarks": [[20, 20], [220, 20], [20, 220], [220, 220]],
            "healthy_skin_roi": [150, 150, 60, 60],
        },
    )
    assert register.status_code == 200, register.text
    measured = client.post(
        f"/api/sessions/{session_id}/measure",
        json={
            "stria_code": "L1",
            "stria_label": "Abdome inferior",
            "polygon": [[60, 100], [180, 100], [180, 140], [60, 140]],
        },
    )
    assert measured.status_code == 200, measured.text
    return patient


def export_file(client, headers, patient_id: str | None = None):
    response = client.post(
        "/api/backup/export",
        json={"password": PASSWORD, "patient_id": patient_id},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response


def inspect_file(client, headers, content: bytes, password: str = PASSWORD):
    return client.post(
        "/api/backup/inspect",
        files={"file": ("backup.dmbak", content, MEDIA_TYPE)},
        data={"password": password},
        headers=headers,
    )


def restore_file(client, headers, content: bytes, mode: str, password: str = PASSWORD):
    return client.post(
        "/api/backup/restore",
        files={"file": ("backup.dmbak", content, MEDIA_TYPE)},
        data={"password": password, "mode": mode},
        headers=headers,
    )


def wipe_clinical_tables() -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM striae_measurements")
        conn.execute("DELETE FROM stria_roi")
        conn.execute("DELETE FROM patient_pins")
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM patients")


# ---------------------------------------------------------------------------
# Exportação
# ---------------------------------------------------------------------------
def test_full_backup_format_manifest_and_inspect(station):
    client, headers = station
    first = measured_case(client, headers, "PAC-BKUP01", "Marina Alves de Souza")
    second = measured_case(client, headers, "PAC-BKUP02", "Clara Nogueira")

    response = export_file(client, headers)
    content = response.content
    assert content.startswith(BACKUP_MAGIC)
    assert response.headers["content-type"] == MEDIA_TYPE
    filename = response.headers["content-disposition"]
    assert "DermaMetric-Backup-" in filename and filename.endswith('.dmbak"')

    parsed = backup.read_backup(content, PASSWORD)
    manifest = parsed["manifest"]
    assert manifest["format_version"] == 1
    assert manifest["mode"] == "full"
    assert manifest["counts"]["patients"] == 2
    assert manifest["counts"]["sessions"] == 4
    assert manifest["counts"]["measurements"] == 2
    assert manifest["counts"]["images"] >= 4
    assert set(manifest["patient_codes"]) == {"PAC-BKUP01", "PAC-BKUP02"}
    assert manifest["professional"]["name"] == ""
    assert "records.json" in manifest["checksums"]

    records = parsed["records"]
    names = {p["code"]: p["name"] for p in records["patients"]}
    assert names["PAC-BKUP01"] == "Marina Alves de Souza"
    session = records["patients"][0]["sessions"][0]
    assert session["measurements"][0]["stria_code"] == "L1"
    assert session["measurements"][0]["polygon"]
    assert session["landmarks"]
    assert parsed["blobs"], "as imagens devem estar dentro do envelope"

    summary = inspect_file(client, headers, content)
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["mode"] == "full"
    assert body["counts"]["patients"] == 2
    assert set(body["patient_codes"]) == {"PAC-BKUP01", "PAC-BKUP02"}
    # Inspeção no mesmo banco sinaliza os códigos já existentes (conflito).
    assert set(body["conflicts"]) == {"PAC-BKUP01", "PAC-BKUP02"}
    assert body["has_conflicts"] is True
    assert first["code"] in body["patient_codes"]
    assert second["code"] in body["patient_codes"]


def test_individual_patient_backup(station):
    client, headers = station
    target = measured_case(client, headers, "PAC-INDIV1", "Paciente Individual")
    measured_case(client, headers, "PAC-INDIV2", "Outro Paciente")

    response = export_file(client, headers, patient_id=target["id"])
    assert response.content.startswith(BACKUP_MAGIC)
    assert 'filename="PAC-INDIV1.dmbak"' in response.headers["content-disposition"]

    parsed = backup.read_backup(response.content, PASSWORD)
    assert parsed["manifest"]["mode"] == "patient"
    assert parsed["manifest"]["patient_codes"] == ["PAC-INDIV1"]
    assert parsed["manifest"]["counts"]["patients"] == 1
    assert len(parsed["records"]["patients"]) == 1

    summary = inspect_file(client, headers, response.content).json()
    assert summary["mode"] == "patient"
    assert summary["patient_codes"] == ["PAC-INDIV1"]


def test_rejects_tampered_fake_and_wrong_password(station):
    client, headers = station
    measured_case(client, headers, "PAC-SECURE", "Paciente Seguro")
    content = export_file(client, headers).content

    # Arquivo comum renomeado para .dmbak.
    fake = b"%PDF-1.7\nnao sou um backup\n"
    response = inspect_file(client, headers, fake)
    assert response.status_code == 400
    assert "DMBAK" in response.json()["detail"] or ".dmbak" in response.json()["detail"]

    # Um único byte alterado invalida o HMAC-SHA256.
    tampered = bytearray(content)
    tampered[-1] ^= 0x01
    response = inspect_file(client, headers, bytes(tampered))
    assert response.status_code == 400
    assert "HMAC" in response.json()["detail"] or "corrompido" in response.json()["detail"]

    # Truncamento também é rejeitado.
    response = inspect_file(client, headers, content[:-40])
    assert response.status_code == 400

    # Senha mestra incorreta não decifra e não altera nada.
    response = inspect_file(client, headers, content, password="senha-errada")
    assert response.status_code == 401
    assert "Senha mestra incorreta" in response.json()["detail"]

    before = len(db.list_patients())
    restore = restore_file(client, headers, content, mode="replace", password="senha-errada")
    assert restore.status_code == 401
    assert len(db.list_patients()) == before


# ---------------------------------------------------------------------------
# Restauração
# ---------------------------------------------------------------------------
def test_restore_replace_into_clean_database(station):
    client, headers = station
    patient = measured_case(client, headers, "PAC-REST01", "Helena Costa Ferreira")
    content = export_file(client, headers).content
    assert content.startswith(BACKUP_MAGIC)

    wipe_clinical_tables()
    assert db.list_patients() == []

    restored = restore_file(client, headers, content, mode="replace")
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["mode"] == "replace"
    assert body["patients"] == 1
    assert body["sessions"] == 2
    assert body["measurements"] == 1
    assert body["images"] >= 2
    assert body["snapshot"].startswith("pre_restore_") and body["snapshot"].endswith(".dmbak")

    # Snapshot automático existe, é um .dmbak válido e legível.
    snapshot_path = Path(db.DATA_DIR) / "backups" / body["snapshot"]
    assert snapshot_path.exists()
    snapshot = snapshot_path.read_bytes()
    assert snapshot.startswith(BACKUP_MAGIC)
    backup.read_backup(snapshot, PASSWORD)

    patients = {p["code"]: p for p in db.list_patients()}
    assert set(patients) == {"PAC-REST01"}
    restored_patient = patients["PAC-REST01"]
    assert restored_patient["name"] == "Helena Costa Ferreira"
    assert restored_patient["notes"] == "Caso de teste"
    assert restored_patient["sessions_total"] == 2
    session = restored_patient["sessions"][0]
    assert session["label"] == "Baseline"
    assert session["landmarks"]
    assert session["healthy_skin_roi"]
    assert session["px_per_mm"] == pytest.approx(10.0)
    assert len(session["measurements"]) == 1
    assert session["measurements"][0]["area_mm2"] > 0
    assert session["measurements"][0]["polygon"]

    # Dados pessoais permanecem cifrados em repouso no SQLite.
    with db.connection() as conn:
        row = conn.execute(
            "SELECT name, notes FROM patients WHERE id = ?", (patient["id"],)
        ).fetchone()
    assert row["name"].startswith("enc:v1:")
    assert row["notes"].startswith("enc:v1:")
    assert "Helena" not in row["name"]

    # Imagem restaurada: cifrada no disco e descriptografada pela API.
    clean_url = session["clean_image_url"]
    assert clean_url
    relative = session["clean_image_path"]
    encrypted_path = db.storage_absolute(relative)
    assert encrypted_path is not None and encrypted_path.exists()
    raw = encrypted_path.read_bytes()
    assert not raw.startswith(b"\x89PNG")
    served = client.get(clean_url)
    assert served.status_code == 200
    assert served.content.startswith(b"\x89PNG")


def test_merge_preserves_local_and_imports_missing(station):
    client, headers = station
    first = measured_case(client, headers, "PAC-MERGE1", "Paciente Um")
    second = measured_case(client, headers, "PAC-MERGE2", "Paciente Dois")
    content = export_file(client, headers).content

    # Remove o segundo paciente localmente e cria um terceiro só local.
    with db.connection() as conn:
        conn.execute(
            "DELETE FROM striae_measurements WHERE session_id IN"
            " (SELECT id FROM sessions WHERE patient_id = ?)",
            (second["id"],),
        )
        conn.execute(
            "DELETE FROM stria_roi WHERE session_id IN"
            " (SELECT id FROM sessions WHERE patient_id = ?)",
            (second["id"],),
        )
        conn.execute("DELETE FROM sessions WHERE patient_id = ?", (second["id"],))
        conn.execute("DELETE FROM patient_pins WHERE patient_id = ?", (second["id"],))
        conn.execute("DELETE FROM patients WHERE id = ?", (second["id"],))

    local_only = measured_case(client, headers, "PAC-LOCAL", "Paciente Local")
    before_first = {
        p["code"]: p
        for p in db.list_patients()
    }
    assert set(before_first) == {"PAC-MERGE1", "PAC-LOCAL"}
    existing_measurements = len(before_first["PAC-MERGE1"]["sessions"][0]["measurements"])

    restored = restore_file(client, headers, content, mode="merge")
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["mode"] == "merge"
    assert body["patients_merged"] >= 1
    assert body["patients"] >= 1
    assert body["snapshot"].startswith("pre_restore_")

    after = {p["code"]: p for p in db.list_patients()}
    assert set(after) == {"PAC-MERGE1", "PAC-MERGE2", "PAC-LOCAL"}
    assert after["PAC-MERGE1"]["name"] == "Paciente Um"
    assert len(after["PAC-MERGE1"]["sessions"][0]["measurements"]) == existing_measurements
    assert len(after["PAC-MERGE2"]["sessions"]) == 2
    assert len(after["PAC-MERGE2"]["sessions"][0]["measurements"]) == 1
    assert after["PAC-LOCAL"]["id"] == local_only["id"]
    assert len(after["PAC-LOCAL"]["sessions"]) == 2

    # Nenhum código duplicado após o merge.
    codes = [p["code"] for p in db.list_patients()]
    assert len(codes) == len(set(codes))

    # O paciente original continua com as duas sessões e a imagem servida.
    session = after["PAC-MERGE1"]["sessions"][0]
    served = client.get(session["clean_image_url"])
    assert served.status_code == 200
    assert served.content.startswith(b"\x89PNG")
    assert first["code"] == "PAC-MERGE1"


def test_export_requires_master_password(station):
    client, headers = station
    measured_case(client, headers, "PAC-PWD001", "Paciente Senha")

    response = client.post(
        "/api/backup/export",
        json={"password": "senha-errada", "patient_id": None},
        headers=headers,
    )
    assert response.status_code == 401

    empty = client.post(
        "/api/backup/export",
        json={"password": "", "patient_id": None},
        headers=headers,
    )
    assert empty.status_code == 400


def test_backup_requires_station_token_when_configured(station):
    client, _ = station
    measured_case(client, {"Authorization": "Bearer invalido"}, "PAC-TOKEN1", "Token")
    response = client.post(
        "/api/backup/export",
        json={"password": PASSWORD, "patient_id": None},
    )
    assert response.status_code == 401
