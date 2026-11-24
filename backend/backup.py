"""Backup e restauração proprietários do DermaMetric (``.dmbak``).

Formato binário ``DMBAK\\x01\\x00\\x00``::

    magic (8) | salt (16) | iterations (4, BE) | password-check (8)
              | payload-len (8, BE) | HMAC-SHA256 (32) | payload

O payload é um ZIP cifrado com Fernet/AES (chave derivada da senha mestra via
PBKDF2-SHA256 e salt aleatório do cabeçalho). O HMAC-SHA256 autentica
cabeçalho + ciphertext, detectando qualquer adulteração. Dentro do ZIP:

* ``manifest.json`` — versão, data UTC, modo, profissional emissor, contagens
  e checksums SHA-256 de todos os itens;
* ``records.json`` — pacientes, dados pessoais, PIN (hash), sessões, marcos,
  ROIs, medições e escalas clínicas (já descriptografados no envelope);
* ``blobs/<rel_path>`` — imagens originais referenciadas pelas sessões.

As imagens são guardadas descriptografadas *dentro* do envelope cifrado e, na
restauração, são recifradas com a DEK local (``STORAGE_KEY``), permanecendo
sempre cifradas em repouso no storage da instalação.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import re
import secrets
import struct
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import database as db
from . import security

MAGIC = b"DMBAK\x01\x00\x00"
FORMAT_VERSION = 1
APP_NAME = "DermaMetric"
PAYLOAD_FORMAT = "dmbak"

SALT_LEN = 16
CHECK_LEN = 8
TAG_LEN = 32
PBKDF2_ITERATIONS = 200_000
# PINs de 4 a 8 dígitos têm entropia baixa: o envelope do caso usa mais
# iterações e um prefixo de domínio para não reutilizar a derivação da senha
# mestra em outro contexto.
CASE_PBKDF2_ITERATIONS = 600_000
CASE_PIN_RE = re.compile(r"^\d{4,8}$")
CASE_KEY_PREFIX = "dmbak-case-pin:v1:"

# magic + salt + iterations + checker + payload_len
_HEADER_PREFIX_LEN = 8 + SALT_LEN + 4 + CHECK_LEN + 8
_MIN_FILE_LEN = _HEADER_PREFIX_LEN + TAG_LEN

MANIFEST_NAME = "manifest.json"
RECORDS_NAME = "records.json"
BLOB_PREFIX = "blobs/"

_IMAGE_FIELDS = (
    "clean_image_path",
    "ruler_image_path",
    "aligned_image_path",
    "enhanced_image_path",
)

_NAME_PARTICLES = {
    "de", "da", "do", "das", "dos", "e", "di", "du", "del", "della", "van", "von",
}


def mask_patient_name(full_name: str) -> str:
    """Iniciais seguras para o relatório de importação (sem nome completo)."""
    parts = [
        part for part in str(full_name or "").split()
        if part and part.lower() not in _NAME_PARTICLES
    ]
    return " ".join(f"{part[0].upper()}." for part in parts if part)


class BackupError(ValueError):
    """Arquivo inválido, adulterado ou incompatível."""


class BackupPasswordError(PermissionError):
    """Senha mestra incorreta para o arquivo informado."""


class BackupPINError(PermissionError):
    """PIN do caso ausente, fora do formato ou incorreto."""


def normalize_case_pin(pin: Any) -> str:
    """Valida o PIN do caso (4 a 8 dígitos numéricos)."""
    value = str(pin or "").strip()
    if not CASE_PIN_RE.match(value):
        raise BackupPINError("O PIN do caso deve ter de 4 a 8 dígitos numéricos.")
    return value


# ---------------------------------------------------------------------------
# Criptografia do envelope
# ---------------------------------------------------------------------------
def _derive_material(password: str, salt: bytes, iterations: int) -> tuple[bytes, bytes]:
    """Deriva (chave Fernet, chave HMAC) da senha mestra e do salt."""
    material = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=64
    )
    fernet_key = base64.urlsafe_b64encode(material[:32])
    return fernet_key, material[32:]


def _password_check(mac_key: bytes) -> bytes:
    return hashlib.sha256(mac_key + b"dmbak-password-check").digest()[:CHECK_LEN]


def encrypt_backup_bytes(
    payload: bytes, password: str, iterations: int = PBKDF2_ITERATIONS
) -> bytes:
    """Encapsula o ZIP ``payload`` no envelope ``.dmbak`` autenticado."""
    if not password:
        raise BackupPasswordError("Informe a senha mestra para cifrar o backup.")
    salt = secrets.token_bytes(SALT_LEN)
    fernet_key, mac_key = _derive_material(password, salt, iterations)
    cipher = Fernet(fernet_key).encrypt(payload)
    prefix = (
        MAGIC
        + salt
        + struct.pack(">I", iterations)
        + _password_check(mac_key)
        + struct.pack(">Q", len(cipher))
    )
    tag = hmac.new(mac_key, prefix + cipher, hashlib.sha256).digest()
    return prefix + tag + cipher


def decrypt_backup_bytes(data: bytes, password: str) -> bytes:
    """Valida magic, senha e HMAC; devolve o ZIP interno do backup."""
    if len(data) < _MIN_FILE_LEN or not data.startswith(MAGIC):
        raise BackupError(
            "Arquivo inválido: não é um backup DermaMetric (.dmbak)."
        )
    offset = 8
    salt = data[offset : offset + SALT_LEN]
    offset += SALT_LEN
    (iterations,) = struct.unpack(">I", data[offset : offset + 4])
    offset += 4
    check = data[offset : offset + CHECK_LEN]
    offset += CHECK_LEN
    (payload_len,) = struct.unpack(">Q", data[offset : offset + 8])
    offset += 8
    tag = data[offset : offset + TAG_LEN]
    offset += TAG_LEN
    cipher = data[offset:]
    if iterations <= 0 or iterations > 10_000_000:
        raise BackupError("Cabeçalho .dmbak inválido (iterações PBKDF2).")
    if payload_len != len(cipher):
        raise BackupError("Backup truncado ou com tamanho inconsistente.")

    fernet_key, mac_key = _derive_material(password, salt, iterations)
    if not hmac.compare_digest(check, _password_check(mac_key)):
        raise BackupPasswordError("Senha mestra incorreta para este backup.")

    expected_tag = hmac.new(
        mac_key, data[:_HEADER_PREFIX_LEN] + cipher, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise BackupError(
            "Backup corrompido ou adulterado: falha na verificação HMAC-SHA256."
        )
    try:
        return Fernet(fernet_key).decrypt(cipher)
    except InvalidToken as exc:  # pragma: no cover - HMAC já cobre este caso
        raise BackupError("Não foi possível decifrar o backup.") from exc


def _case_envelope_password(pin: str) -> str:
    """Deriva o segredo do envelope a partir do PIN, com domínio próprio."""
    return f"{CASE_KEY_PREFIX}{pin}"


def encrypt_case_bytes(payload: bytes, pin: str) -> bytes:
    """Cifra o pacote de um caso com chave derivada do PIN (PBKDF2 + Fernet)."""
    value = normalize_case_pin(pin)
    return encrypt_backup_bytes(
        payload, _case_envelope_password(value), iterations=CASE_PBKDF2_ITERATIONS
    )


def decrypt_case_bytes(data: bytes, pin: str) -> bytes:
    """Decifra um pacote de caso; PIN errado vira ``BackupPINError``."""
    value = normalize_case_pin(pin)
    try:
        return decrypt_backup_bytes(data, _case_envelope_password(value))
    except BackupPasswordError as exc:
        raise BackupPINError("PIN do caso incorreto.") from exc


# ---------------------------------------------------------------------------
# Coleta dos registros
# ---------------------------------------------------------------------------
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _decrypt(value: Any) -> Any:
    return db._decrypt_text(value)


def _clean_json(value: Any) -> Any:
    """Normaliza um campo JSON cifrado para estrutura serializável."""
    parsed = db._json_or_none(_decrypt(value))
    if isinstance(parsed, (dict, list)) or parsed is None:
        return parsed
    return parsed


def _measurement_record(entry: dict[str, Any]) -> dict[str, Any]:
    nested = entry.get("measurement") or {}
    return {
        "stria_code": entry.get("stria_code") or nested.get("stria_code"),
        "stria_label": entry.get("stria_label") or nested.get("stria_label"),
        "polygon": entry.get("polygon") or nested.get("polygon") or [],
        "metrics": entry.get("metrics") or nested.get("metrics") or {},
        "delta_e_00": (
            entry.get("delta_e_00")
            if entry.get("delta_e_00") is not None
            else nested.get("delta_e_00")
        ),
        "created_at": nested.get("created_at"),
    }


def _session_record(conn, session: dict[str, Any]) -> dict[str, Any]:
    session_id = session.get("id")
    rois = []
    if session_id:
        for row in conn.execute(
            "SELECT roi_json, created_at FROM stria_roi WHERE session_id = ? ORDER BY id",
            (session_id,),
        ):
            rois.append(
                {
                    "roi": _clean_json(row["roi_json"]),
                    "created_at": row["created_at"],
                }
            )
    return {
        "id": session_id,
        "patient_id": session.get("patient_id"),
        "session_code": session.get("session_code") or session.get("code"),
        "label": session.get("label"),
        "captured_at": session.get("captured_at"),
        "px_per_mm": session.get("px_per_mm"),
        "ruler_points": session.get("ruler_points"),
        "landmarks": session.get("landmarks"),
        "healthy_skin_roi": session.get("healthy_skin_roi"),
        "lesion_labels": session.get("lesion_labels"),
        "clinical_scales": session.get("clinical_scales"),
        "clean_image_path": db.normalize_storage_path(session.get("clean_image_path")),
        "ruler_image_path": db.normalize_storage_path(session.get("ruler_image_path")),
        "aligned_image_path": db.normalize_storage_path(session.get("aligned_image_path")),
        "enhanced_image_path": db.normalize_storage_path(session.get("enhanced_image_path")),
        "updated_at": session.get("updated_at"),
        "measurements": [
            _measurement_record(m) for m in (session.get("measurements") or [])
        ],
        "stria_roi": rois,
    }


def _patient_record(conn, row) -> dict[str, Any]:
    patient = dict(row)
    patient_id = patient["id"]
    pin_row = conn.execute(
        "SELECT pin_hash, created_at, updated_at FROM patient_pins WHERE patient_id = ?",
        (patient_id,),
    ).fetchone()
    sessions = db._patient_sessions(conn, patient_id)
    return {
        "id": patient_id,
        "code": patient.get("code"),
        "name": _decrypt(patient.get("name")),
        "notes": _decrypt(patient.get("notes")),
        "birth_date": _decrypt(patient.get("birth_date")),
        "phone": _decrypt(patient.get("phone")),
        "locked": bool(patient.get("locked")),
        "created_at": patient.get("created_at"),
        "updated_at": patient.get("updated_at"),
        "pin": dict(pin_row) if pin_row else None,
        "sessions": [_session_record(conn, session) for session in sessions],
    }


def collect_records(
    patient_id: str | None = None,
    patient_ids: list[str | int] | None = None,
) -> dict[str, Any]:
    """Monta o ``records.json`` a partir do banco (dados já descriptografados).

    ``patient_id`` mantém a compatibilidade com a exportação individual antiga.
    ``patient_ids`` exporta uma seleção arbitrária de pacientes e grava
    ``mode: "selected"`` no manifesto. Todos os IDs precisam existir.
    """
    selected_ids: list[str] | None = None
    if patient_ids is not None:
        if isinstance(patient_ids, (str, int)):
            patient_ids = [patient_ids]
        selected_ids = []
        for raw in patient_ids:
            value = str(raw).strip()
            if value and value not in selected_ids:
                selected_ids.append(value)
        if not selected_ids:
            raise ValueError("Selecione ao menos um paciente para exportar.")

    with db.connection() as conn:
        if selected_ids is not None:
            placeholders = ",".join("?" for _ in selected_ids)
            rows = conn.execute(
                f"SELECT * FROM patients WHERE id IN ({placeholders})",
                selected_ids,
            ).fetchall()
            found = {str(row["id"]) for row in rows}
            missing = [value for value in selected_ids if value not in found]
            if missing:
                raise LookupError(
                    "Paciente(s) não encontrado(s) para exportação: "
                    + ", ".join(missing)
                )
            rows = sorted(rows, key=lambda row: str(row["code"] or ""))
            mode = "selected"
        elif patient_id:
            rows = conn.execute(
                "SELECT * FROM patients WHERE id = ?", (patient_id,)
            ).fetchall()
            if not rows:
                raise LookupError("Paciente não encontrado para o backup.")
            mode = "patient"
        else:
            rows = conn.execute("SELECT * FROM patients ORDER BY code").fetchall()
            mode = "full"
        patients = [_patient_record(conn, row) for row in rows]
    return {
        "format_version": FORMAT_VERSION,
        "generated_at": _utc_now_iso(),
        "mode": mode,
        "patients": patients,
    }


def _collect_blobs(records: dict[str, Any]) -> dict[str, bytes]:
    """Lê e descriptografa as imagens referenciadas (para o envelope)."""
    blobs: dict[str, bytes] = {}
    for patient in records.get("patients") or []:
        for session in patient.get("sessions") or []:
            for field in _IMAGE_FIELDS:
                relative = db.normalize_storage_path(session.get(field))
                if not relative or relative in blobs:
                    continue
                absolute = db.storage_absolute(relative)
                if absolute is None or not absolute.exists():
                    continue
                try:
                    blobs[relative] = security.read_encrypted_file(absolute)
                except Exception as exc:  # noqa: BLE001 - blob ilegível
                    raise BackupError(
                        f"Não foi possível ler a imagem {relative} para o backup."
                    ) from exc
    return blobs


def _build_payload(
    records: dict[str, Any],
    blobs: dict[str, bytes],
    professional: dict[str, str],
    protection: str = "master_password",
    iterations: int = PBKDF2_ITERATIONS,
) -> tuple[bytes, dict[str, Any]]:
    records_bytes = json.dumps(
        records, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    checksums = {RECORDS_NAME: hashlib.sha256(records_bytes).hexdigest()}
    blob_items: list[tuple[str, bytes]] = []
    for relative, data in sorted(blobs.items()):
        safe = _safe_relative_blob(relative)
        name = f"{BLOB_PREFIX}{safe}"
        checksums[name] = hashlib.sha256(data).hexdigest()
        blob_items.append((name, data))

    session_count = sum(
        len(patient.get("sessions") or []) for patient in records.get("patients") or []
    )
    measurement_count = sum(
        len(session.get("measurements") or [])
        for patient in records.get("patients") or []
        for session in patient.get("sessions") or []
    )
    manifest = {
        "app": APP_NAME,
        "format": PAYLOAD_FORMAT,
        "format_version": FORMAT_VERSION,
        "created_at": records["generated_at"],
        "mode": records["mode"],
        "professional": {
            "name": str(professional.get("professional_name") or ""),
            "registry": str(professional.get("professional_registry") or ""),
            "institution": str(professional.get("institution") or ""),
        },
        "patient_codes": [
            str(patient.get("code") or "") for patient in records.get("patients") or []
        ],
        "counts": {
            "patients": len(records.get("patients") or []),
            "sessions": session_count,
            "measurements": measurement_count,
            "images": len(blob_items),
            "blobs": len(blob_items),
        },
        "checksums": checksums,
        "protection": protection,
        "encryption": {
            "payload": "fernet-aes256",
            "kdf": "pbkdf2_sha256",
            "iterations": iterations,
            "integrity": "hmac_sha256",
            "case_pin": protection == "case_pin",
        },
    }
    if records["mode"] == "selected":
        manifest["selected_count"] = len(records.get("patients") or [])
    elif records["mode"] == "patient" and manifest["patient_codes"]:
        manifest["patient_code"] = manifest["patient_codes"][0]
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME, manifest_bytes)
        archive.writestr(RECORDS_NAME, records_bytes)
        for name, data in blob_items:
            archive.writestr(name, data)
    return buffer.getvalue(), manifest


def _safe_relative_blob(relative: str) -> str:
    path = PurePosixPath(str(relative).replace("\\", "/"))
    if path.is_absolute() or any(part in {"..", ""} for part in path.parts):
        raise BackupError(f"Caminho de imagem inválido no backup: {relative!r}")
    return "/".join(path.parts)


# ---------------------------------------------------------------------------
# Exportação
# ---------------------------------------------------------------------------
def export_backup(
    password: str,
    patient_id: str | None = None,
    patient_ids: list[str | int] | None = None,
    archive_password: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Gera o arquivo ``.dmbak`` completo, individual ou de uma seleção.

    ``password`` valida a estação no endpoint; o envelope pode ser cifrado com
    uma ``archive_password`` dedicada (quando informada). Sem ela, usa a
    própria senha mestra.
    """
    records = collect_records(patient_id, patient_ids=patient_ids)
    blobs = _collect_blobs(records)
    protection = (
        "archive_password"
        if str(archive_password or "").strip()
        else "master_password"
    )
    payload, manifest = _build_payload(
        records,
        blobs,
        db.get_profile(),
        protection=protection,
        iterations=PBKDF2_ITERATIONS,
    )
    envelope_password = str(archive_password or "").strip() or password
    data = encrypt_backup_bytes(payload, envelope_password)
    summary = {
        "mode": records["mode"],
        "created_at": manifest["created_at"],
        "patient_codes": manifest["patient_codes"],
        "counts": manifest["counts"],
        "size_bytes": len(data),
        "protection": protection,
        "has_archive_password": bool(
            str(archive_password or "").strip()
            and str(archive_password).strip() != password
        ),
    }
    if records["mode"] == "selected":
        summary["selected_count"] = manifest.get("selected_count", len(records["patients"]))
    return data, summary


def export_case_backup(patient_id: str, pin: str) -> tuple[bytes, dict[str, Any]]:
    """Exporta um caso único cifrado com chave derivada do PIN do prontuário.

    Se o paciente já possui PIN, ele é apenas *confirmado*; se ainda não
    possui, o PIN informado (4 a 8 dígitos) passa a proteger o prontuário e é
    gravado localmente, mantendo o caso consistente entre estações.
    """
    pin_value = normalize_case_pin(pin)
    patient = db.get_patient(patient_id)
    if patient is None:
        raise LookupError("Paciente não encontrado para o backup.")
    pin_defined = not bool(patient.get("has_pin"))
    if not pin_defined and not db.verify_patient_pin(patient_id, pin_value):
        raise BackupPINError("PIN do caso incorreto.")
    if pin_defined:
        db.set_patient_pin(patient_id, pin_value)

    records = collect_records(patient_id=patient_id)
    blobs = _collect_blobs(records)
    payload, manifest = _build_payload(
        records,
        blobs,
        db.get_profile(),
        protection="case_pin",
        iterations=CASE_PBKDF2_ITERATIONS,
    )
    data = encrypt_case_bytes(payload, pin_value)
    summary = {
        "mode": records["mode"],
        "created_at": manifest["created_at"],
        "patient_codes": manifest["patient_codes"],
        "counts": manifest["counts"],
        "size_bytes": len(data),
        "protection": "case_pin",
        "case_pin": True,
        "pin_defined_now": pin_defined,
    }
    return data, summary


def backup_filename(summary: dict[str, Any]) -> str:
    """Nome de download claro conforme modo e quantidade de pacientes."""
    codes = [
        "".join(ch for ch in str(code) if ch.isalnum() or ch in "-_")
        for code in (summary.get("patient_codes") or [])
        if str(code).strip()
    ]
    mode = summary.get("mode")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    day = datetime.now(timezone.utc).strftime("%Y%m%d")

    if mode == "patient" and codes:
        return f"DermaMetric-Caso-{codes[0] or 'Paciente'}-{day}.dmbak"
    if mode == "selected":
        if len(codes) == 1:
            return f"DermaMetric-{codes[0]}-{day}.dmbak"
        return f"DermaMetric-Selecao-{len(codes)}pacs-{stamp}.dmbak"
    return f"DermaMetric-Backup-Completo-{stamp}.dmbak"


# ---------------------------------------------------------------------------
# Leitura, inspeção e restauração
# ---------------------------------------------------------------------------
def read_backup(data: bytes, password: str) -> dict[str, Any]:
    """Decifra e valida integralmente um ``.dmbak`` sem tocar no banco."""
    return _parse_payload(decrypt_backup_bytes(data, password))


def read_case_backup(data: bytes, pin: str) -> dict[str, Any]:
    """Decifra e valida um pacote de caso protegido por PIN."""
    return _parse_payload(decrypt_case_bytes(data, pin))


def _parse_payload(payload: bytes) -> dict[str, Any]:
    """Valida checksums/manifesto do ZIP interno já decifrado."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise BackupError("Conteúdo interno do backup está corrompido.") from exc

    with archive:
        names = set(archive.namelist())
        if MANIFEST_NAME not in names or RECORDS_NAME not in names:
            raise BackupError("Backup incompleto: manifest.json/records.json ausentes.")
        try:
            manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
            records = json.loads(archive.read(RECORDS_NAME).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError("Metadados do backup ilegíveis.") from exc
        if not isinstance(manifest, dict) or not isinstance(records, dict):
            raise BackupError("Estrutura do backup inválida.")
        if int(manifest.get("format_version") or 0) != FORMAT_VERSION:
            raise BackupError(
                "Versão de backup incompatível com esta instalação do DermaMetric."
            )
        checksums = manifest.get("checksums") or {}
        if not isinstance(checksums, dict):
            raise BackupError("Manifesto sem checksums válidos.")
        for name, expected in checksums.items():
            if name not in names:
                raise BackupError(f"Item ausente no backup: {name}")
            actual = hashlib.sha256(archive.read(name)).hexdigest()
            if not hmac.compare_digest(str(expected), actual):
                raise BackupError(f"Checksum divergente no backup: {name}")
        for name in names:
            if name in {MANIFEST_NAME, RECORDS_NAME}:
                continue
            if name not in checksums:
                raise BackupError(f"Item não autenticado no backup: {name}")
        blobs = {
            name: archive.read(name)
            for name in names
            if name.startswith(BLOB_PREFIX)
        }
    return {"manifest": manifest, "records": records, "blobs": blobs}


def _local_codes() -> set[str]:
    with db.connection() as conn:
        return {
            str(row["code"])
            for row in conn.execute("SELECT code FROM patients")
            if row["code"]
        }


def inspect_backup(data: bytes, password: str) -> dict[str, Any]:
    """Resumo do conteúdo do arquivo, sem qualquer alteração no banco."""
    return _inspect_parsed(read_backup(data, password), len(data))


def inspect_case_backup(data: bytes, pin: str) -> dict[str, Any]:
    """Resumo de um pacote de caso protegido por PIN."""
    return _inspect_parsed(read_case_backup(data, pin), len(data))


def _inspect_parsed(parsed: dict[str, Any], size_bytes: int) -> dict[str, Any]:
    manifest = parsed["manifest"]
    records = parsed["records"]
    codes = [
        str(patient.get("code") or "")
        for patient in records.get("patients") or []
        if patient.get("code")
    ]
    existing = _local_codes()
    conflicts = [code for code in codes if code in existing]
    counts = dict(manifest.get("counts") or {})
    counts.setdefault("patients", len(codes))
    result = {
        "format_version": manifest.get("format_version"),
        "created_at": manifest.get("created_at"),
        "mode": manifest.get("mode"),
        "professional": manifest.get("professional") or {},
        "counts": counts,
        "patient_codes": codes,
        "conflicts": conflicts,
        "has_conflicts": bool(conflicts),
        "encryption": manifest.get("encryption") or {},
        "protection": manifest.get("protection") or "master_password",
        "case_pin": bool((manifest.get("encryption") or {}).get("case_pin")),
        "size_bytes": size_bytes,
    }
    if manifest.get("mode") == "selected":
        result["selected_count"] = manifest.get("selected_count", len(codes))
    elif manifest.get("mode") == "patient":
        result["patient_code"] = manifest.get("patient_code") or (codes[0] if codes else "")
    return result


def _encrypt_optional(value: Any) -> str | None:
    if value is None:
        return None
    return security.encrypt_text(str(value))


def _encrypt_json(value: Any) -> str | None:
    if value is None:
        return None
    return security.encrypt_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    )


def _insert_patient(conn, patient: dict[str, Any]) -> str:
    patient_id = str(patient.get("id") or secrets.token_hex(16))
    pin = patient.get("pin") or {}
    locked = 1 if pin.get("pin_hash") else 0
    now = db.db_utc_now()
    conn.execute(
        """INSERT INTO patients
             (id, code, name, notes, birth_date, phone, locked, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            patient_id,
            str(patient.get("code") or patient_id),
            _encrypt_optional(patient.get("name") or ""),
            _encrypt_optional(patient.get("notes") or ""),
            _encrypt_optional(patient.get("birth_date") or ""),
            _encrypt_optional(patient.get("phone") or ""),
            locked,
            patient.get("created_at") or now,
            patient.get("updated_at") or now,
        ),
    )
    if pin.get("pin_hash"):
        conn.execute(
            """INSERT OR REPLACE INTO patient_pins
                 (patient_id, pin_hash, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (
                patient_id,
                pin["pin_hash"],
                pin.get("created_at") or now,
                pin.get("updated_at") or now,
            ),
        )
    return patient_id


def _insert_session(conn, session: dict[str, Any], patient_id: str) -> None:
    code = str(session.get("session_code") or "T0")
    session_id = str(session.get("id") or f"{patient_id}:{code}")
    now = db.db_utc_now()
    conn.execute(
        """INSERT INTO sessions
             (id, patient_id, session_code, label, captured_at, px_per_mm,
              ruler_points_json, landmarks_json, healthy_skin_roi_json,
              aligned_image_path, enhanced_image_path, ruler_image_path,
              clean_image_path, lesion_labels_json, clinical_scales_json,
              updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            patient_id,
            code,
            _encrypt_optional(session.get("label") or ""),
            session.get("captured_at"),
            float(session.get("px_per_mm") or 0.0),
            _encrypt_json(session.get("ruler_points")),
            _encrypt_json(session.get("landmarks")),
            _encrypt_json(session.get("healthy_skin_roi")),
            db.normalize_storage_path(session.get("aligned_image_path")),
            db.normalize_storage_path(session.get("enhanced_image_path")),
            db.normalize_storage_path(session.get("ruler_image_path")),
            db.normalize_storage_path(session.get("clean_image_path")),
            _encrypt_json(session.get("lesion_labels")),
            _encrypt_json(session.get("clinical_scales")),
            session.get("updated_at") or now,
        ),
    )
    existing = {
        str(row["stria_code"])
        for row in conn.execute(
            "SELECT stria_code FROM striae_measurements WHERE session_id = ?",
            (session_id,),
        )
    }
    for measurement in session.get("measurements") or []:
        code_m = str(measurement.get("stria_code") or "")
        if not code_m or code_m in existing:
            continue
        metrics = measurement.get("metrics") or {}
        conn.execute(
            """INSERT INTO striae_measurements
                 (session_id, stria_code, polygon_json, metrics_json,
                  stria_label, delta_e_00, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                code_m,
                _encrypt_json(measurement.get("polygon") or []),
                _encrypt_json(metrics),
                measurement.get("stria_label"),
                measurement.get("delta_e_00"),
                measurement.get("created_at") or now,
            ),
        )
        existing.add(code_m)
    roi_count = conn.execute(
        "SELECT COUNT(*) AS total FROM stria_roi WHERE session_id = ?",
        (session_id,),
    ).fetchone()["total"]
    if not roi_count:
        for roi in session.get("stria_roi") or []:
            conn.execute(
                "INSERT INTO stria_roi (session_id, roi_json, created_at)"
                " VALUES (?, ?, ?)",
                (
                    session_id,
                    _encrypt_json(roi.get("roi")),
                    roi.get("created_at") or now,
                ),
            )


def _merge_session(
    conn, existing_row: dict[str, Any], session: dict[str, Any], patient_id: str
) -> None:
    """Completa uma sessão existente sem apagar dados locais."""
    session_id = existing_row["id"]
    updates: list[tuple[str, Any]] = []
    field_map = {
        "label": "label",
        "captured_at": "captured_at",
        "ruler_points": "ruler_points_json",
        "landmarks": "landmarks_json",
        "healthy_skin_roi": "healthy_skin_roi_json",
        "lesion_labels": "lesion_labels_json",
        "clinical_scales": "clinical_scales_json",
    }
    for source, column in field_map.items():
        if existing_row.get(column):
            continue
        value = session.get(source)
        if value in (None, "", {}, []):
            continue
        updates.append((column, _encrypt_json(value) if source not in {"label", "captured_at"} else _encrypt_optional(value)))
    for field in _IMAGE_FIELDS:
        if existing_row.get(field) or not session.get(field):
            continue
        updates.append((field, db.normalize_storage_path(session.get(field))))
    if not existing_row.get("px_per_mm") and session.get("px_per_mm"):
        updates.append(("px_per_mm", float(session.get("px_per_mm"))))
    if updates:
        sets = ", ".join(f"{column} = ?" for column, _ in updates)
        conn.execute(
            f"UPDATE sessions SET {sets}, updated_at = ? WHERE id = ?",
            [value for _, value in updates] + [db.db_utc_now(), session_id],
        )
    existing = {
        str(row["stria_code"])
        for row in conn.execute(
            "SELECT stria_code FROM striae_measurements WHERE session_id = ?",
            (session_id,),
        )
    }
    now = db.db_utc_now()
    for measurement in session.get("measurements") or []:
        code_m = str(measurement.get("stria_code") or "")
        if not code_m or code_m in existing:
            continue
        conn.execute(
            """INSERT INTO striae_measurements
                 (session_id, stria_code, polygon_json, metrics_json,
                  stria_label, delta_e_00, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                code_m,
                _encrypt_json(measurement.get("polygon") or []),
                _encrypt_json(measurement.get("metrics") or {}),
                measurement.get("stria_label"),
                measurement.get("delta_e_00"),
                measurement.get("created_at") or now,
            ),
        )
        existing.add(code_m)
    roi_count = conn.execute(
        "SELECT COUNT(*) AS total FROM stria_roi WHERE session_id = ?",
        (session_id,),
    ).fetchone()["total"]
    if not roi_count:
        for roi in session.get("stria_roi") or []:
            conn.execute(
                "INSERT INTO stria_roi (session_id, roi_json, created_at)"
                " VALUES (?, ?, ?)",
                (session_id, _encrypt_json(roi.get("roi")), roi.get("created_at") or now),
            )


def _patient_blob_count(patient: dict[str, Any], blob_relatives: set[str]) -> int:
    """Conta imagens do paciente cujos blobs existem no envelope."""
    count = 0
    seen: set[str] = set()
    for session in patient.get("sessions") or []:
        for field in _IMAGE_FIELDS:
            relative = db.normalize_storage_path(session.get(field))
            if not relative or relative in seen:
                continue
            seen.add(str(relative))
            if str(relative) in blob_relatives:
                count += 1
    return count


def _apply_records(
    records: dict[str, Any],
    mode: str,
    blob_relatives: set[str],
    import_as_new: bool = False,
) -> dict[str, Any]:
    """Aplica pacientes/sessões e devolve estatísticas + relatório por paciente.

    Em ``merge`` de um caso único, ``import_as_new`` cria uma cópia do
    prontuário com um novo código público (``PAC-XXXXXX``) quando o código de
    origem já existe na estação, em vez de mesclar no prontuário local.
    """
    stats = {
        "patients": 0,
        "patients_merged": 0,
        "sessions": 0,
        "measurements": 0,
    }
    imported: list[dict[str, Any]] = []
    with db.connection() as conn:
        if mode == "replace":
            conn.execute("DELETE FROM striae_measurements")
            conn.execute("DELETE FROM stria_roi")
            conn.execute("DELETE FROM patient_pins")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM patients")
        for patient in records.get("patients") or []:
            code = str(patient.get("code") or "")
            sessions = patient.get("sessions") or []
            images_count = _patient_blob_count(patient, blob_relatives)
            masked = mask_patient_name(str(patient.get("name") or ""))
            existing_patient = None
            if mode == "merge" and code:
                existing_patient = conn.execute(
                    "SELECT * FROM patients WHERE code = ?", (code,)
                ).fetchone()
            if existing_patient is not None and import_as_new:
                # Cópia independente do caso recebido: novo código público,
                # novas sessões e nenhuma escrita no prontuário local.
                copy_patient = dict(patient)
                copy_patient["id"] = secrets.token_hex(16)
                copy_patient["code"] = db.generate_patient_code(conn)
                new_id = _insert_patient(conn, copy_patient)
                stats["patients"] += 1
                for session in sessions:
                    session_code = str(session.get("session_code") or "T0")
                    incoming = dict(session)
                    incoming["id"] = f"{new_id}:{session_code}"
                    _insert_session(conn, incoming, new_id)
                    stats["sessions"] += 1
                    stats["measurements"] += len(session.get("measurements") or [])
                imported.append(
                    {
                        "id": new_id,
                        "patient_code": copy_patient["code"],
                        "source_patient_code": code or str(patient.get("id") or ""),
                        "masked_name": masked,
                        "sessions_count": len(sessions),
                        "images_count": images_count,
                        "status": "created_copy",
                    }
                )
                continue

            if existing_patient is not None:
                local_id = existing_patient["id"]
                stats["patients_merged"] += 1
                local_sessions = {
                    str(row["session_code"] or row["id"]): dict(row)
                    for row in conn.execute(
                        "SELECT * FROM sessions WHERE patient_id = ?", (local_id,)
                    )
                }
                for session in sessions:
                    session_code = str(session.get("session_code") or "T0")
                    existing_session = local_sessions.get(session_code)
                    if existing_session is not None:
                        _merge_session(conn, existing_session, session, local_id)
                    else:
                        incoming = dict(session)
                        incoming["id"] = f"{local_id}:{session_code}"
                        _insert_session(conn, incoming, local_id)
                        stats["sessions"] += 1
                    stats["measurements"] += len(session.get("measurements") or [])
                imported.append(
                    {
                        "id": local_id,
                        "patient_code": code or local_id,
                        "masked_name": masked,
                        "sessions_count": len(sessions),
                        "images_count": images_count,
                        "status": "merged",
                    }
                )
                continue

            patient_id = _insert_patient(conn, patient)
            stats["patients"] += 1
            for session in sessions:
                _insert_session(conn, session, patient_id)
                stats["sessions"] += 1
                stats["measurements"] += len(session.get("measurements") or [])
            imported.append(
                {
                    "id": patient_id,
                    "patient_code": code or patient_id,
                    "masked_name": masked,
                    "sessions_count": len(sessions),
                    "images_count": images_count,
                    "status": "created",
                }
            )
    return {"stats": stats, "imported_patients": imported}


def _verify_integrity(imported: list[dict[str, Any]]) -> None:
    """Confirma que os pacientes importados estão legíveis e o SQLite íntegro."""
    with db.connection() as conn:
        for item in imported:
            row = conn.execute(
                "SELECT code FROM patients WHERE id = ?", (item["id"],)
            ).fetchone()
            if row is None:
                raise BackupError(
                    f"Integridade comprometida: paciente {item['patient_code']} ausente."
                )
            total = conn.execute(
                "SELECT COUNT(*) AS total FROM sessions WHERE patient_id = ?",
                (item["id"],),
            ).fetchone()["total"]
            if int(total) < int(item["sessions_count"]):
                raise BackupError(
                    f"Integridade comprometida: sessões ausentes para "
                    f"{item['patient_code']}."
                )
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        if str(result).lower() != "ok":
            raise BackupError(f"Falha na verificação de integridade do SQLite: {result}")



def _write_blobs(blobs: dict[str, bytes]) -> int:
    written = 0
    for name, data in blobs.items():
        if not name.startswith(BLOB_PREFIX):
            continue
        relative = _safe_relative_blob(name[len(BLOB_PREFIX):])
        # ``db.STORAGE_DIR`` espelha ``backend.paths.STORAGE_DIR`` (dev x
        # empacotado) e é o ponto único já utilizado pelo restante do backend.
        destination = (db.STORAGE_DIR / relative).resolve()
        storage_root = Path(db.STORAGE_DIR).resolve()
        if storage_root not in destination.parents and destination != storage_root:
            raise BackupError(f"Destino de imagem fora do storage: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = security.encrypt_bytes(data) if relative.endswith(".enc") else data
        destination.write_bytes(payload)
        try:
            destination.chmod(0o600)
        except OSError:  # pragma: no cover - plataformas sem chmod
            pass
        written += 1
    return written


def restore_backup(
    data: bytes,
    password: str,
    mode: str = "merge",
    import_as_new: bool = False,
) -> dict[str, Any]:
    """Restaura o backup, criando antes um snapshot de segurança local.

    Devolve um relatório estruturado com ``logs`` cronológicos e
    ``imported_patients`` para a interface acompanhar cada etapa.
    ``import_as_new`` vale para casos únicos em ``merge``: quando o código de
    origem já existe, cria um prontuário novo (novo ``PAC-XXXXXX``) em vez de
    mesclar no prontuário local.
    """
    payload = decrypt_backup_bytes(data, password)
    return _restore_payload(
        payload,
        password,
        mode,
        import_as_new,
        protection_label="Senha mestra",
        protection="master_password",
    )


def restore_case_backup(
    data: bytes,
    pin: str,
    mode: str = "merge",
    import_as_new: bool = False,
) -> dict[str, Any]:
    """Importa um caso protegido por PIN, recifrando com a DEK local."""
    pin_value = normalize_case_pin(pin)
    payload = decrypt_case_bytes(data, pin_value)
    return _restore_payload(
        payload,
        pin_value,
        mode,
        import_as_new,
        protection_label="PIN do caso",
        protection="case_pin",
    )


def _restore_payload(
    payload: bytes,
    envelope_password: str,
    mode: str,
    import_as_new: bool,
    *,
    protection_label: str,
    protection: str,
) -> dict[str, Any]:
    if mode not in {"merge", "replace"}:
        raise BackupError("Modo de restauração inválido. Use 'merge' ou 'replace'.")
    parsed = _parse_payload(payload)
    records = parsed["records"]
    manifest = parsed["manifest"]
    version = manifest.get("format_version") or FORMAT_VERSION
    source_counts = manifest.get("counts") or {}
    logs: list[str] = [
        f"Assinatura DMBAK v{version} validada",
        f"{protection_label} e integridade HMAC-SHA256 confirmadas",
        (
            f"Backup contém {source_counts.get('patients', 0)} paciente(s), "
            f"{source_counts.get('sessions', 0)} sessão(ões) e "
            f"{source_counts.get('images', source_counts.get('blobs', 0))} imagem(ns)"
        ),
    ]
    if mode == "replace":
        logs.append(
            "Modo substituir: dados clínicos anteriores serão removidos; "
            "credenciais da estação e DEK local permanecem intactas"
        )
    elif import_as_new and manifest.get("mode") == "patient":
        logs.append(
            "Modo mesclar como novo: códigos repetidos gerarão um novo "
            "prontuário, sem alterar o caso local"
        )

    # Snapshot automático do estado atual antes de qualquer gravação.
    logs.append("Criando snapshot preventivo do banco atual…")
    snapshot_bytes, _ = export_backup(envelope_password)
    # ``db.DATA_DIR`` deriva de ``backend.paths.DATA_DIR`` (perfil do usuário
    # quando empacotado), mantendo os snapshots junto aos dados persistentes.
    backup_dir = db.DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    snapshot_path = backup_dir / f"pre_restore_{stamp}.dmbak"
    counter = 1
    while snapshot_path.exists():
        snapshot_path = backup_dir / f"pre_restore_{stamp}-{counter}.dmbak"
        counter += 1
    snapshot_path.write_bytes(snapshot_bytes)
    try:
        snapshot_path.chmod(0o600)
    except OSError:  # pragma: no cover
        pass
    logs.append(f"Snapshot preventivo salvo em {snapshot_path}")

    blob_relatives = {
        name[len(BLOB_PREFIX):]
        for name in parsed["blobs"]
        if name.startswith(BLOB_PREFIX)
    }
    applied = _apply_records(
        records, mode, blob_relatives, import_as_new=import_as_new
    )
    stats = applied["stats"]
    imported = applied["imported_patients"]

    for item in imported:
        verb = "importado"
        if item["status"] == "merged":
            verb = "mesclado"
        elif item["status"] == "created_copy":
            verb = (
                "importado como novo prontuário "
                f"(origem {item.get('source_patient_code') or '—'})"
            )
        images_label = (
            f"{item['images_count']} imagem(ns) recifrada(s)"
            if item["images_count"]
            else "sem imagens no envelope"
        )
        logs.append(
            f"Paciente {item['patient_code']} {verb} "
            f"({item['sessions_count']} sessão(ões), {images_label})"
        )

    images = _write_blobs(parsed["blobs"])
    logs.append(f"{images} imagem(ns) gravada(s) e recifrada(s) no storage local")

    # Fecha qualquer transação pendente e sincroniza o SQLite antes de servir
    # novas requisições (o token/DEK da estação não são tocados).
    db.checkpoint()
    _verify_integrity(imported)
    total_sessions = sum(int(item["sessions_count"]) for item in imported)
    total_patients = len(imported)
    logs.append(
        f"Integridade do banco verificada ({total_patients} paciente(s), "
        f"{total_sessions} sessão(ões) legíveis)"
    )
    logs.append(f"Restauração em modo '{mode}' concluída com sucesso")

    return {
        "ok": True,
        "restored": True,
        "mode": mode,
        "backup_path": str(snapshot_path),
        "source_created_at": manifest.get("created_at"),
        "source_mode": manifest.get("mode"),
        "source_patient_code": manifest.get("patient_code"),
        "protection": protection,
        "case_pin": protection == "case_pin",
        "import_as_new": bool(import_as_new),
        "patient_codes": manifest.get("patient_codes") or [],
        "imported_patients": imported,
        "patients": stats["patients"],
        "patients_merged": stats["patients_merged"],
        "sessions": total_sessions,
        "measurements": stats["measurements"],
        "images": images,
        "total_patients": total_patients,
        "total_sessions": total_sessions,
        "total_measurements": stats["measurements"],
        "total_images": images,
        "snapshot": snapshot_path.name,
        "logs": logs,
    }
