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
ARCHIVE_PASSWORD = "senha-do-pacote-selecionado-1"
CASE_PIN = "4821"
OTHER_PIN = "741963"
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


def export_file(client, headers, patient_id: str | None = None, pin: str | None = None):
    body = {"password": PASSWORD}
    if patient_id is not None:
        body["patient_id"] = patient_id
        body["pin"] = pin if pin is not None else CASE_PIN
    response = client.post(
        "/api/backup/export",
        json=body,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response


def inspect_file(client, headers, content: bytes, password: str = PASSWORD, case_pin: str = ""):
    data = {"password": password} if password else {}
    if case_pin:
        data = {"case_pin": case_pin}
    return client.post(
        "/api/backup/inspect",
        files={"file": ("backup.dmbak", content, MEDIA_TYPE)},
        data=data,
        headers=headers,
    )


def restore_file(
    client,
    headers,
    content: bytes,
    mode: str,
    password: str = PASSWORD,
    import_as_new: bool = False,
    case_pin: str = "",
):
    data = {"mode": mode}
    if case_pin:
        data["case_pin"] = case_pin
    else:
        data["password"] = password
    if import_as_new:
        data["import_as_new"] = "true"
    return client.post(
        "/api/backup/restore",
        files={"file": ("backup.dmbak", content, MEDIA_TYPE)},
        data=data,
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
    disposition = response.headers["content-disposition"]
    assert 'filename="DermaMetric-Caso-PAC-INDIV1-' in disposition
    assert disposition.endswith('.dmbak"')

    parsed = backup.read_case_backup(response.content, CASE_PIN)
    assert parsed["manifest"]["mode"] == "patient"
    assert parsed["manifest"]["patient_code"] == "PAC-INDIV1"
    assert parsed["manifest"]["patient_codes"] == ["PAC-INDIV1"]
    assert parsed["manifest"]["counts"]["patients"] == 1
    assert parsed["manifest"]["protection"] == "case_pin"
    assert parsed["manifest"]["encryption"]["case_pin"] is True
    assert len(parsed["records"]["patients"]) == 1

    # A senha mestra da estação não abre o pacote protegido pelo PIN.
    with pytest.raises(backup.BackupPasswordError):
        backup.read_backup(response.content, PASSWORD)
    with pytest.raises(backup.BackupPINError):
        backup.read_case_backup(response.content, "0000")

    summary = inspect_file(client, headers, response.content, password="", case_pin=CASE_PIN).json()
    assert summary["mode"] == "patient"
    assert summary["patient_code"] == "PAC-INDIV1"
    assert summary["patient_codes"] == ["PAC-INDIV1"]
    assert summary["protection"] == "case_pin"


def test_single_case_backup_is_encrypted_with_the_case_pin(station):
    """Caso único sai cifrado pelo PIN, não pela senha mestra da estação."""
    client, headers = station
    target = measured_case(client, headers, "PAC-CASE01", "Helena Duarte")
    measured_case(client, headers, "PAC-CASE02", "Não vai no arquivo")
    assert target["has_pin"] is False

    # Sem PIN informado, a exportação de caso é recusada.
    missing = client.post(
        "/api/backup/export",
        json={"password": PASSWORD, "patient_id": target["id"]},
        headers=headers,
    )
    assert missing.status_code == 400
    assert "PIN do caso" in missing.json()["detail"]

    # Definir o PIN no ato da exportação também protege o prontuário local.
    response = client.post(
        "/api/backup/export",
        json={"password": PASSWORD, "patient_id": target["id"], "pin": CASE_PIN},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    content = response.content
    disposition = response.headers["content-disposition"]
    assert 'filename="DermaMetric-Caso-PAC-CASE01-' in disposition

    locked = client.get(f"/api/patients/{target['id']}", headers=headers).json()
    assert locked["has_pin"] is True

    # Exportar novamente exige confirmar o PIN já cadastrado.
    refused = client.post(
        "/api/backup/export",
        json={"password": PASSWORD, "patient_id": target["id"], "pin": OTHER_PIN},
        headers=headers,
    )
    assert refused.status_code == 401
    assert refused.json()["detail"] == "PIN do caso incorreto."
    again = client.post(
        "/api/backup/export",
        json={"password": PASSWORD, "patient_id": target["id"], "pin": CASE_PIN},
        headers=headers,
    )
    assert again.status_code == 200, again.text

    parsed = backup.read_case_backup(content, CASE_PIN)
    manifest = parsed["manifest"]
    assert manifest["mode"] == "patient"
    assert manifest["patient_code"] == "PAC-CASE01"
    assert manifest["patient_codes"] == ["PAC-CASE01"]
    assert manifest["counts"]["patients"] == 1
    assert manifest["protection"] == "case_pin"
    assert parsed["records"]["patients"][0]["pin"]["pin_hash"]
    assert [p["code"] for p in parsed["records"]["patients"]] == ["PAC-CASE01"]

    # Nem a senha mestra nem um PIN errado abrem o envelope.
    with pytest.raises(backup.BackupPasswordError):
        backup.read_backup(content, PASSWORD)
    with pytest.raises(backup.BackupPINError):
        backup.read_case_backup(content, OTHER_PIN)

    wrong = client.post(
        "/api/backup/inspect",
        files={"file": ("caso.dmbak", content, MEDIA_TYPE)},
        data={"case_pin": OTHER_PIN},
        headers=headers,
    )
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "PIN do caso incorreto."

    inspected = client.post(
        "/api/backup/inspect",
        files={"file": ("caso.dmbak", content, MEDIA_TYPE)},
        data={"case_pin": CASE_PIN},
        headers=headers,
    )
    assert inspected.status_code == 200, inspected.text
    body = inspected.json()
    assert body["mode"] == "patient"
    assert body["patient_code"] == "PAC-CASE01"
    assert body["conflicts"] == ["PAC-CASE01"]

    # Importando como novo, o caso de outro profissional não sobrescreve o local.
    restored = restore_file(
        client,
        headers,
        content,
        "merge",
        case_pin=CASE_PIN,
        import_as_new=True,
    )
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["source_mode"] == "patient"
    assert body["source_patient_code"] == "PAC-CASE01"
    assert body["case_pin"] is True
    assert body["import_as_new"] is True
    assert body["patients"] == 1
    assert body["patients_merged"] == 0
    assert body["imported_patients"][0]["status"] == "created_copy"
    new_code = body["imported_patients"][0]["patient_code"]
    assert new_code.startswith("PAC-") and new_code != "PAC-CASE01"

    listed = client.get("/api/patients").json()["patients"]
    codes = {patient["code"] for patient in listed}
    assert {"PAC-CASE01", "PAC-CASE02", new_code} <= codes
    # Prontuário local preservado; a cópia recebeu um código novo.
    original = next(patient for patient in listed if patient["code"] == "PAC-CASE01")
    assert original["sessions_total"] == 2
    copy = next(patient for patient in listed if patient["code"] == new_code)
    assert copy["sessions_total"] == 2


def test_merge_single_case_without_import_as_new_merges_local(station):
    client, headers = station
    target = measured_case(client, headers, "PAC-MERGE1", "Paciente Local")
    response = export_file(client, headers, patient_id=target["id"])

    restored = restore_file(client, headers, response.content, "merge", case_pin=CASE_PIN)
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["patients_merged"] == 1
    assert body["patients"] == 0
    assert body["imported_patients"][0]["status"] == "merged"
    assert len(client.get("/api/patients").json()["patients"]) == 1


def test_case_pin_import_on_another_station_uses_only_the_pin(
    station, tmp_path, monkeypatch
):
    """Caso exportado numa estação importa em outra só com o PIN do caso."""
    client, headers = station
    target = measured_case(client, headers, "PAC-XSTN01", "Paciente Inter")
    content = export_file(client, headers, patient_id=target["id"]).content
    parsed = backup.read_case_backup(content, CASE_PIN)
    patient_record = parsed["records"]["patients"][0]
    image_relative = patient_record["sessions"][0]["clean_image_path"]
    blob_name = f"{backup.BLOB_PREFIX}{image_relative}"
    plain_image = parsed["blobs"][blob_name]

    # Segunda estação: storage/DEK/senha mestra completamente diferentes.
    stor2 = tmp_path / "station2" / "storage"
    data2 = tmp_path / "station2" / "data"
    for directory in (stor2, data2, stor2 / "uploads", stor2 / "processed"):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(db, "DB_PATH", stor2 / "dermametric.db")
    monkeypatch.setattr(db, "DATA_DIR", data2)
    monkeypatch.setattr(db, "STORAGE_DIR", stor2)
    monkeypatch.setattr(db, "UPLOAD_DIR", stor2 / "uploads")
    monkeypatch.setattr(db, "PROCESSED_DIR", stor2 / "processed")
    monkeypatch.setattr(db, "WORK_DIR", stor2 / "work")
    monkeypatch.setattr(security, "STORAGE_DIR", stor2)
    monkeypatch.setattr(security, "KEY_PATH", stor2 / ".secret.key")
    monkeypatch.setattr(security, "_fernet", None)
    monkeypatch.setattr(security, "_cached_key", None)
    db.init_db()

    with TestClient(app) as second:
        setup = second.post(
            "/api/auth/setup", params={"password": "outra-senha-mestra-2026"}
        )
        assert setup.status_code == 200, setup.text
        h2 = {"Authorization": f"Bearer {setup.json()['access_token']}"}

        wrong = second.post(
            "/api/backup/restore",
            files={"file": ("caso.dmbak", content, MEDIA_TYPE)},
            data={"case_pin": "0000", "mode": "merge"},
            headers=h2,
        )
        assert wrong.status_code == 401
        assert wrong.json()["detail"] == "PIN do caso incorreto."

        # A senha mestra da estação de destino não decifra o pacote.
        with_master = second.post(
            "/api/backup/restore",
            files={"file": ("caso.dmbak", content, MEDIA_TYPE)},
            data={"password": "outra-senha-mestra-2026", "mode": "merge"},
            headers=h2,
        )
        assert with_master.status_code == 401

        restored = second.post(
            "/api/backup/restore",
            files={"file": ("caso.dmbak", content, MEDIA_TYPE)},
            data={"case_pin": CASE_PIN, "mode": "merge", "import_as_new": "true"},
            headers=h2,
        )
        assert restored.status_code == 200, restored.text
        body = restored.json()
        assert body["patients"] == 1
        assert body["case_pin"] is True
        assert body["imported_patients"][0]["patient_code"] == "PAC-XSTN01"

        # O prontuário importado abre com os dados originais na nova estação.
        listed = second.get("/api/patients", headers=h2).json()["patients"]
        assert [p["code"] for p in listed] == ["PAC-XSTN01"]
        detail = second.get(f"/api/patients/{listed[0]['id']}", headers=h2).json()
        session = detail["sessions"][0]
        assert session["measurements"][0]["stria_code"] == "L1"
        served = second.get(session["clean_image_url"])
        assert served.status_code == 200
        assert served.content.startswith(b"\x89PNG")

        # A imagem foi recifrada com a DEK local (bytes diferentes do envelope).
        stored_path = db.storage_absolute(image_relative)
        assert stored_path is not None and stored_path.exists()
        stored = stored_path.read_bytes()
        assert stored != plain_image
        assert security.decrypt_bytes(stored) == plain_image

        # Dados sensíveis também ficaram cifrados com a chave da nova estação.
        with db.connection() as conn:
            row = conn.execute(
                "SELECT name FROM patients WHERE code = 'PAC-XSTN01'"
            ).fetchone()
        assert row["name"].startswith("enc:v1:")


def test_selected_patients_backup_with_dedicated_password_and_merge_restore(station):
    """Seleção de p1+p3, envelope com senha própria e restauração em merge."""
    client, headers = station
    first = measured_case(client, headers, "PAC-SEL001", "Ana Paula Souza")
    second = measured_case(client, headers, "PAC-SEL002", "Bruno Lima")
    third = measured_case(client, headers, "PAC-SEL003", "Carla Mendes")

    response = client.post(
        "/api/backup/export",
        json={
            "password": PASSWORD,
            "patient_ids": [first["id"], third["id"]],
            "archive_password": ARCHIVE_PASSWORD,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    content = response.content
    assert content.startswith(BACKUP_MAGIC)
    disposition = response.headers["content-disposition"]
    assert "DermaMetric-Selecao-2pacs-" in disposition and disposition.endswith('.dmbak"')

    parsed = backup.read_backup(content, ARCHIVE_PASSWORD)
    manifest = parsed["manifest"]
    assert manifest["mode"] == "selected"
    assert manifest["selected_count"] == 2
    assert manifest["counts"]["patients"] == 2
    assert manifest["counts"]["sessions"] == 4
    assert set(manifest["patient_codes"]) == {"PAC-SEL001", "PAC-SEL003"}
    records = parsed["records"]
    assert {p["code"] for p in records["patients"]} == {"PAC-SEL001", "PAC-SEL003"}
    assert second["code"] not in {p["code"] for p in records["patients"]}
    session = records["patients"][0]["sessions"][0]
    assert session["measurements"] and session["landmarks"] and session["healthy_skin_roi"]
    assert parsed["blobs"], "imagens dos pacientes selecionados devem ir no envelope"

    # A senha mestra não abre um envelope cifrado com a senha do pacote.
    with pytest.raises(backup.BackupPasswordError):
        backup.read_backup(content, PASSWORD)

    inspected = inspect_file(client, headers, content, password=ARCHIVE_PASSWORD)
    assert inspected.status_code == 200, inspected.text
    body = inspected.json()
    assert body["mode"] == "selected"
    assert body["selected_count"] == 2
    assert set(body["patient_codes"]) == {"PAC-SEL001", "PAC-SEL003"}

    # Restaura somente os dois pacientes selecionados, com a senha do pacote.
    wipe_clinical_tables()
    restored = restore_file(
        client, headers, content, "merge", password=ARCHIVE_PASSWORD
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["source_mode"] == "selected"
    listed = client.get("/api/patients").json()
    codes = {patient["code"] for patient in listed["patients"]}
    assert codes == {"PAC-SEL001", "PAC-SEL003"}
    assert len(listed["patients"]) == 2


def test_selected_single_patient_uses_master_password_and_clear_filename(station):
    client, headers = station
    target = measured_case(client, headers, "PAC-ONE001", "Paciente Único")
    measured_case(client, headers, "PAC-ONE002", "Não selecionado")

    response = client.post(
        "/api/backup/export",
        json={"password": PASSWORD, "patient_ids": [target["id"]]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    assert 'filename="DermaMetric-PAC-ONE001-' in disposition
    assert disposition.endswith('.dmbak"')

    parsed = backup.read_backup(response.content, PASSWORD)
    assert parsed["manifest"]["mode"] == "selected"
    assert parsed["manifest"]["selected_count"] == 1
    assert parsed["manifest"]["patient_codes"] == ["PAC-ONE001"]


def test_selected_export_validates_ids_and_empty_selection(station):
    client, headers = station
    target = measured_case(client, headers, "PAC-VALID1", "Paciente Válido")

    empty = client.post(
        "/api/backup/export",
        json={"password": PASSWORD, "patient_ids": []},
        headers=headers,
    )
    assert empty.status_code == 400

    missing = client.post(
        "/api/backup/export",
        json={"password": PASSWORD, "patient_ids": [target["id"], "paciente-inexistente"]},
        headers=headers,
    )
    assert missing.status_code == 404
    assert "paciente-inexistente" in missing.json()["detail"]


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
    assert body["ok"] is True
    assert body["mode"] == "replace"
    assert body["patients"] == 1
    assert body["sessions"] == 2
    assert body["measurements"] == 1
    assert body["images"] >= 2
    assert body["snapshot"].startswith("pre_restore_") and body["snapshot"].endswith(".dmbak")
    assert body["backup_path"].endswith(".dmbak")
    assert Path(body["backup_path"]).exists()
    assert body["total_patients"] == 1
    assert body["total_sessions"] == 2
    assert body["total_measurements"] == 1
    assert body["total_images"] >= 2
    assert isinstance(body["logs"], list) and len(body["logs"]) >= 5
    logs = " | ".join(body["logs"])
    assert "Assinatura DMBAK v1 validada" in logs
    assert "Snapshot preventivo salvo em" in logs
    assert "Integridade do banco verificada" in logs
    assert len(body["imported_patients"]) == 1
    imported = body["imported_patients"][0]
    assert imported["status"] == "created"
    assert imported["patient_code"] == "PAC-REST01"
    assert imported["masked_name"] == "H. C. F."
    assert imported["sessions_count"] == 2
    assert imported["images_count"] >= 2

    # GET /api/patients e /api/patients/{id} respondem imediatamente após o
    # replace, com a sessão autenticada da estação ainda válida (token/DEK).
    listing = client.get("/api/patients", headers=headers)
    assert listing.status_code == 200, listing.text
    listed = {p["code"]: p for p in listing.json()["patients"]}
    assert set(listed) == {"PAC-REST01"}
    assert listed["PAC-REST01"]["sessions_total"] == 2
    detail = client.get(f"/api/patients/{patient['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    detail_patient = detail.json()
    assert detail_patient["code"] == "PAC-REST01"
    assert len(detail_patient["sessions"]) == 2
    assert len(detail_patient["sessions"][0]["measurements"]) == 1
    for session in detail_patient["sessions"]:
        if not session["clean_image_url"]:
            continue
        image = client.get(session["clean_image_url"])
        assert image.status_code == 200
        assert image.content.startswith(b"\x89PNG")

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
    assert body["ok"] is True
    assert body["mode"] == "merge"
    assert body["patients_merged"] >= 1
    assert body["patients"] >= 1
    assert body["snapshot"].startswith("pre_restore_")
    assert body["backup_path"].endswith(".dmbak") and Path(body["backup_path"]).exists()
    assert body["total_sessions"] == 4
    assert body["total_measurements"] == 2
    assert body["total_images"] >= 4
    assert isinstance(body["logs"], list) and len(body["logs"]) >= 5
    logs = " | ".join(body["logs"])
    assert "Assinatura DMBAK v1 validada" in logs
    assert "Integridade do banco verificada" in logs
    statuses = {p["patient_code"]: p["status"] for p in body["imported_patients"]}
    assert statuses == {"PAC-MERGE1": "merged", "PAC-MERGE2": "created"}
    imported_by_code = {p["patient_code"]: p for p in body["imported_patients"]}
    assert imported_by_code["PAC-MERGE2"]["sessions_count"] == 2
    assert imported_by_code["PAC-MERGE2"]["images_count"] >= 2

    # Leitura imediata pela API após o merge, usando o token da estação.
    listing = client.get("/api/patients", headers=headers)
    assert listing.status_code == 200, listing.text
    listed = {p["code"]: p for p in listing.json()["patients"]}
    assert set(listed) == {"PAC-MERGE1", "PAC-MERGE2", "PAC-LOCAL"}
    detail = client.get(
        f"/api/patients/{imported_by_code['PAC-MERGE2']['id']}", headers=headers
    )
    assert detail.status_code == 200, detail.text
    detail_patient = detail.json()
    assert detail_patient["code"] == "PAC-MERGE2"
    assert len(detail_patient["sessions"]) == 2
    assert len(detail_patient["sessions"][0]["measurements"]) == 1
    image = client.get(detail_patient["sessions"][0]["clean_image_url"])
    assert image.status_code == 200
    assert image.content.startswith(b"\x89PNG")

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
