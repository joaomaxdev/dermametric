"""SQLite database for DermaMetric - with patient and PIN management."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote
import uuid

try:  # importação como pacote (uvicorn backend.main:app / pytest)
    from . import paths as _paths
except ImportError:  # pragma: no cover - execução direta de dentro de backend/
    import paths as _paths  # type: ignore[no-redef]

# Caminhos centralizados (dev x empacotado). Mantemos os nomes públicos para
# compatibilidade com testes e com o restante do backend.
BASE_DIR = _paths.ASSETS_DIR
DATA_DIR = _paths.DATA_DIR
DB_PATH = _paths.DB_PATH
STORAGE_DIR = _paths.STORAGE_DIR
UPLOAD_DIR = _paths.UPLOAD_DIR
PROCESSED_DIR = _paths.PROCESSED_DIR
WORK_DIR = _paths.WORK_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

SESSION_IDS = ["T0", "T2", "T3", "T4"]

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Schemas
_SETTINGS_SCHEMA = """CREATE TABLE IF NOT EXISTS settings (
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    scope   TEXT NOT NULL DEFAULT 'global',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (key, scope)
);"""

_ACCESS_TOKEN_SCHEMA = """CREATE TABLE IF NOT EXISTS access_token (
    token TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);"""

_PATIENT_SCHEMA = """CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    notes TEXT,
    birth_date TEXT,
    phone TEXT,
    locked BOOLEAN NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);"""

# Código público do paciente: aleatório, não sequencial e sem caracteres
# ambíguos (0/O, 1/I/L) para digitação e leitura em voz alta.
PATIENT_CODE_PREFIX = "PAC-"
PATIENT_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PATIENT_CODE_LENGTH = 6
PATIENT_CODE_MAX_ATTEMPTS = 64
PATIENT_PERSONAL_FIELDS = ("name", "birth_date", "phone", "notes")

_PATIENT_PINS_SCHEMA = """CREATE TABLE IF NOT EXISTS patient_pins (
    patient_id TEXT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    pin_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (patient_id)
);"""

_SESSION_SCHEMA = """CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    label TEXT,
    captured_at TEXT,
    px_per_mm REAL DEFAULT 0,
    ruler_points_json TEXT,
    landmarks_json TEXT,
    healthy_skin_roi_json TEXT,
    aligned_image_path TEXT,
    enhanced_image_path TEXT,
    ruler_image_path TEXT,
    clean_image_path TEXT,
    lesion_labels_json TEXT,
    clinical_scales_json TEXT,
    updated_at TEXT NOT NULL
);"""

_MEASUREMENT_SCHEMA = """CREATE TABLE IF NOT EXISTS striae_measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    stria_code TEXT NOT NULL,
    polygon_json TEXT NOT NULL,
    metrics_json TEXT,
    stria_label TEXT,
    delta_e_00 REAL,
    created_at TEXT NOT NULL
);"""

_STRIA_ROI_SCHEMA = """CREATE TABLE IF NOT EXISTS stria_roi (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    roi_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);"""

_SETUP_SCHEMA = """CREATE TABLE IF NOT EXISTS setup_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);"""

_SAVE_LOG_SCHEMA = """CREATE TABLE IF NOT EXISTS save_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    step TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);"""


def _ensure_all_schemas(conn: sqlite3.Connection) -> None:
    conn.executescript(_SETTINGS_SCHEMA)
    conn.executescript(_ACCESS_TOKEN_SCHEMA)
    conn.executescript(_PATIENT_SCHEMA)
    conn.executescript(_PATIENT_PINS_SCHEMA)
    conn.executescript(_SESSION_SCHEMA)
    conn.executescript(_MEASUREMENT_SCHEMA)
    conn.executescript(_STRIA_ROI_SCHEMA)
    conn.executescript(_SETUP_SCHEMA)
    conn.executescript(_SAVE_LOG_SCHEMA)


def _get_conn_path() -> Path:
    return str(DB_PATH)


@contextmanager
def connection():
    """Context manager that yields a new sqlite3 connection, auto-committing on success."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def db_utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def checkpoint() -> None:
    """Sincroniza o SQLite após operações grandes (WAL e transações pendentes).

    Cada chamada de ``connection()`` abre e fecha a sua própria conexão; este
    helper garante que nenhuma transação antiga permaneça visível antes de a
    API voltar a listar pacientes após uma restauração ``replace``.
    """
    with connection() as conn:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:  # pragma: no cover - journal não-WAL
            pass


def local_now_string() -> str:
    return db_utc_now()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Migração aditiva: adiciona a coluna apenas quando ela ainda não existe."""
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def generate_patient_code(conn: sqlite3.Connection) -> str:
    """Gera um código ``PAC-XXXXXX`` aleatório e verifica unicidade na tabela.

    Usa apenas caracteres alfanuméricos maiúsculos sem ambiguidade
    (``ABCDEFGHJKLMNPQRSTUVWXYZ23456789``), com 32^6 ≈ 1,07 bilhão de
    combinações. Nenhuma ordem sequencial é exposta no código público.
    """
    for _ in range(PATIENT_CODE_MAX_ATTEMPTS):
        suffix = "".join(
            secrets.choice(PATIENT_CODE_ALPHABET) for _ in range(PATIENT_CODE_LENGTH)
        )
        code = f"{PATIENT_CODE_PREFIX}{suffix}"
        exists = conn.execute(
            "SELECT 1 FROM patients WHERE code = ?", (code,)
        ).fetchone()
        if not exists:
            return code
    raise RuntimeError(
        "Não foi possível gerar um código único de paciente. Tente novamente."
    )


def _migrate_settings_pk(conn: sqlite3.Connection) -> None:
    """Garante PK composta (key, scope) — necessário para PINs por paciente."""
    info = [dict(row) for row in conn.execute("PRAGMA table_info(settings)")]
    pk_columns = [row["name"] for row in info if row["pk"]]
    if pk_columns == ["key"]:
        conn.execute("ALTER TABLE settings RENAME TO settings_legacy")
        conn.executescript(_SETTINGS_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value, scope, updated_at)"
            " SELECT key, value, scope, updated_at FROM settings_legacy"
        )
        conn.execute("DROP TABLE settings_legacy")


def init_db() -> None:
    with connection() as conn:
        _ensure_all_schemas(conn)
        _migrate_settings_pk(conn)
        # Migração multi-paciente (compatível com bancos antigos).
        _ensure_column(conn, "sessions", "patient_id", "TEXT")
        _ensure_column(conn, "sessions", "session_code", "TEXT")
        _ensure_column(conn, "patients", "birth_date", "TEXT")
        _ensure_column(conn, "patients", "phone", "TEXT")
        _ensure_column(conn, "striae_measurements", "delta_e_00", "REAL")
        conn.execute(
            "UPDATE sessions SET session_code = id"
            " WHERE session_code IS NULL OR session_code = ''"
        )
        # Popula as sessões padrão (T0, T2, T3, T4) caso ainda estejam vazias.
        sessions = [
            ("T0", "Sessão T0 — baseline"),
            ("T2", "Sessão T2 — acompanhamento"),
            ("T3", "Sessão T3 — acompanhamento"),
            ("T4", "Sessão T4 — acompanhamento"),
        ]
        existing = {
            row["id"]
            for row in conn.execute("SELECT id FROM sessions").fetchall()
        }
        for sid, label in sessions:
            if sid not in existing:
                conn.execute(
                    "INSERT OR IGNORE INTO sessions (id, label, captured_at, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    (sid, label, db_utc_now(), db_utc_now()),
                )


# ---------------------------------------------------------------------------
# Global settings (access password)
# ---------------------------------------------------------------------------

def get_setting(key: str, *, scope: str = "global", default: str | None = None) -> str | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ? AND scope = ?",
            (key, scope),
        ).fetchone()
    return row["value"] if row else default


def save_setting(key: str, value: str, *, scope: str = "global") -> None:
    with connection() as conn:
        now = db_utc_now()
        conn.execute(
            """
            INSERT INTO settings (key, value, scope, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key, scope) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, scope, now),
        )


def set_access_token(token: str) -> None:
    with connection() as conn:
        now = db_utc_now()
        conn.execute(
            """
            INSERT INTO access_token (token, expires_at)
            VALUES (?, ?)
            ON CONFLICT(token) DO UPDATE SET expires_at = excluded.expires_at
            """,
            (token, now),
        )


def get_access_token() -> str | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT token FROM access_token ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    return row["token"] if row else None


def get_access_expires_at() -> str | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT expires_at FROM access_token ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    return row["expires_at"] if row else None


def clear_access_token() -> None:
    with connection() as conn:
        conn.execute("DELETE FROM access_token")


def normalize_storage_path(value: str | Path | None) -> str | None:
    """Normaliza separadores de caminho do Windows (``\\``) para ``/``."""
    if value is None:
        return None
    normalized = str(value).replace("\\", "/").strip()
    return normalized or None


def storage_relative(path: Path | str) -> str:
    """Retorna o caminho relativo ao storage (sempre com separador ``/``)."""
    relative = Path(path).resolve().relative_to(Path(STORAGE_DIR).resolve())
    return normalize_storage_path(relative) or str(relative)


def storage_absolute(relative_path: str | None) -> Path | None:
    """Resolve um caminho do storage aceitando separadores Windows/Unix."""
    cleaned = normalize_storage_path(relative_path)
    if not cleaned:
        return None
    if len(cleaned) > 1 and cleaned[1] == ":":
        return None  # drive absoluto do Windows (não pertence ao storage)
    root = Path(STORAGE_DIR).resolve()
    raw = Path(cleaned)
    candidate = raw.resolve() if raw.is_absolute() else (root / cleaned).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def list_sessions() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM sessions").fetchall()
        return [_serialize_session(dict(row)) for row in rows]


def _serialize_session(row: dict[str, Any]) -> dict[str, Any]:
    """Converte uma linha bruta de ``sessions`` em payload pronto para a API."""
    session = dict(row)
    session_id = session.get("id")
    label = _decrypt_text(session.get("label"))
    landmarks = _json_or_none(_decrypt_text(session.get("landmarks_json")))
    lesion_labels = _json_or_none(_decrypt_text(session.get("lesion_labels_json")))
    clinical_scales = _json_or_none(_decrypt_text(session.get("clinical_scales_json")))
    ruler_points = _json_or_none(_decrypt_text(session.get("ruler_points_json")))
    roi = _json_or_none(_decrypt_text(session.get("healthy_skin_roi_json")))

    session["code"] = session.get("session_code") or session_id
    session["patient_id"] = session.get("patient_id")
    session["label"] = label
    session["landmarks"] = landmarks
    session["lesion_labels"] = lesion_labels
    session["clinical_scales"] = clinical_scales
    session["ruler_points"] = ruler_points
    session["healthy_skin_roi"] = roi

    clean_path = session.get("clean_image_path")
    ruler_path = session.get("ruler_image_path")
    aligned_path = session.get("aligned_image_path")
    enhanced_path = session.get("enhanced_image_path")

    def _to_url(kind: str, path: str | None) -> str | None:
        if not path:
            return None
        relative = normalize_storage_path(path) or ""
        prefix = f"{kind}/"
        while relative.startswith(prefix):
            relative = relative[len(prefix):]
        url = f"/api/files/{kind}/{quote(relative, safe='')}"
        if kind == "processed" and session_id:
            # Imagens processadas são servidas com marca d'água por padrão;
            # o arquivo em disco permanece limpo para o motor de cálculo.
            url += "?watermark=1&session=" + quote(str(session_id), safe="")
        return url

    session["clean_image_url"] = _to_url("uploads", clean_path)
    session["ruler_image_url"] = _to_url("uploads", ruler_path)
    session["aligned_image_url"] = _to_url("processed", aligned_path)
    session["enhanced_image_url"] = _to_url("processed", enhanced_path)

    measurements = db_load_measurements(session_id) if session_id else []
    session["measurements"] = measurements

    px_per_mm = session.get("px_per_mm")
    session["px_per_mm"] = float(px_per_mm) if px_per_mm is not None else 0.0
    return session


def db_load_measurements(session_id: str) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM striae_measurements WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        measurements = []
        for row in rows:
            m = dict(row)
            polygon = _json_or_none(_decrypt_text(m.get("polygon_json")))
            metrics = _json_or_none(_decrypt_text(m.get("metrics_json"))) or {}
            measurements.append(
                {
                    "stria_code": m.get("stria_code"),
                    "stria_label": m.get("stria_label"),
                    "polygon": polygon if polygon is not None else [],
                    "metrics": metrics,
                    "measurement": {
                        "id": m.get("id"),
                        "session_id": m.get("session_id"),
                        "stria_code": m.get("stria_code"),
                        "polygon": polygon if polygon is not None else [],
                        "metrics": metrics,
                        "stria_label": m.get("stria_label"),
                        "delta_e_00": metrics.get("delta_e_00") if metrics.get("delta_e_00") is not None else metrics.get("delta_e"),
                        "area_mm2": metrics.get("area_mm2"),
                        "mean_width_mm": metrics.get("mean_width_mm"),
                        "max_width_mm": metrics.get("max_width_mm"),
                        "length_mm": metrics.get("length_mm"),
                        "created_at": m.get("created_at"),
                    },
                    "delta_e_00": metrics.get("delta_e_00") if metrics.get("delta_e_00") is not None else metrics.get("delta_e"),
                    "area_mm2": metrics.get("area_mm2"),
                    "mean_width_mm": metrics.get("mean_width_mm"),
                    "max_width_mm": metrics.get("max_width_mm"),
                    "length_mm": metrics.get("length_mm"),
                }
            )
        return measurements


def get_session(session_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return None
        session = dict(row)
    return _serialize_session(session)


def get_session_row(session_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return row


def watermark_context(session_id: str) -> dict[str, Any]:
    """Contexto mínimo (código da sessão, paciente e data) para a tarja."""
    with connection() as conn:
        row = conn.execute(
            """SELECT s.id, s.session_code, s.captured_at, p.code AS patient_code
                 FROM sessions s
                 LEFT JOIN patients p ON p.id = s.patient_id
                WHERE s.id = ?""",
            (session_id,),
        ).fetchone()
    if row is None:
        return {"session_code": session_id, "patient_code": None, "captured_at": None}
    return {
        "session_code": row["session_code"] or row["id"],
        "patient_code": row["patient_code"],
        "captured_at": row["captured_at"],
    }


from . import security as _security


def _encrypt_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if _security.is_encrypted(text):
        return text
    return _security.encrypt_text(text)


def _decrypt_text(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    return _security.decrypt_text(text)


PROFILE_FIELDS = ("professional_name", "professional_registry", "institution")
_PROFILE_PREFIX = "dermametric.profile."


def get_profile() -> dict[str, str]:
    """Perfil do profissional/pesquisador responsável (settings cifrados)."""
    profile = {field: "" for field in PROFILE_FIELDS}
    with connection() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE scope = 'global' AND key LIKE ?",
            (f"{_PROFILE_PREFIX}%",),
        ).fetchall()
    for row in rows:
        field = str(row["key"])[len(_PROFILE_PREFIX):]
        if field not in profile:
            continue
        try:
            profile[field] = str(_decrypt_text(row["value"]) or "").strip()
        except Exception:  # noqa: BLE001 - valor ilegível: mantém vazio
            profile[field] = ""
    return profile


def save_profile(values: dict[str, Any]) -> dict[str, str]:
    """Persiste apenas os campos informados do perfil (Fernet em repouso)."""
    now = db_utc_now()
    with connection() as conn:
        for field in PROFILE_FIELDS:
            if field not in values:
                continue
            text = str(values.get(field) or "").strip()
            conn.execute(
                """
                INSERT INTO settings (key, value, scope, updated_at)
                VALUES (?, ?, 'global', ?)
                ON CONFLICT(key, scope) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (f"{_PROFILE_PREFIX}{field}", _encrypt_text(text), now),
            )
    return get_profile()


def _json_or_none(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    if text == "None" or text == "":
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def update_session(session_id: str, **updates) -> dict[str, Any] | None:
    allowed = {"label", "captured_at", "px_per_mm", "ruler_points_json", "landmarks_json",
               "healthy_skin_roi_json", "aligned_image_path", "enhanced_image_path",
               "ruler_image_path", "clean_image_path", "lesion_labels_json", "clinical_scales_json"}
    encrypted_json_fields = {"ruler_points_json", "landmarks_json", "healthy_skin_roi_json",
                             "lesion_labels_json", "clinical_scales_json"}
    with connection() as conn:
        sets = []
        args = []
        for k, v in updates.items():
            if k not in allowed:
                continue
            if k in encrypted_json_fields and v is not None:
                if not isinstance(v, str):
                    v = json.dumps(v)
                v = _encrypt_text(v)
            elif k == "label" and v is not None:
                v = _encrypt_text(v)
            sets.append(f"{k} = ?")
            args.append(v)
        if not sets:
            return get_session(session_id)
        args.append(db_utc_now())
        args.append(session_id)
        conn.execute(
            f"UPDATE sessions SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
            args,
        )
    return get_session(session_id)


def update_session_measurements(session_id: str, measurements: list[dict[str, Any]]) -> dict[str, Any]:
    with connection() as conn:
        for m in measurements:
            conn.execute(
                """INSERT INTO striae_measurements (session_id, stria_code, polygon_json, metrics_json, stria_label, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    m.get("stria_code", ""),
                    json.dumps(m.get("polygon", [])),
                    json.dumps(m.get("metrics", {})),
                    m.get("stria_label", ""),
                    db_utc_now(),
                ),
            )
        return {"session_id": session_id, "updated_measurements": len(measurements)}


def upsert_measurement(
    session_id: str,
    stria_code: str,
    polygon: list[dict[str, Any]],
    metrics: dict[str, Any],
    label: str | None,
) -> dict[str, Any]:
    with connection() as conn:
        existing = conn.execute(
            "SELECT id FROM striae_measurements WHERE session_id = ? AND stria_code = ?",
            (session_id, stria_code),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE striae_measurements
                   SET polygon_json = ?, metrics_json = ?, stria_label = ?,
                       delta_e_00 = ?, created_at = ?
                 WHERE session_id = ? AND stria_code = ?""",
                (
                    _encrypt_text(json.dumps(polygon)),
                    _encrypt_text(json.dumps(metrics)),
                    label,
                    metrics.get("delta_e_00"),
                    db_utc_now(),
                    session_id,
                    stria_code,
                ),
            )
            row = conn.execute(
                "SELECT * FROM striae_measurements WHERE id = ?", (existing[0],)
            ).fetchone()
        else:
            conn.execute(
                """INSERT INTO striae_measurements
                   (session_id, stria_code, polygon_json, metrics_json, stria_label, delta_e_00, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    stria_code,
                    _encrypt_text(json.dumps(polygon)),
                    _encrypt_text(json.dumps(metrics)),
                    label,
                    metrics.get("delta_e_00"),
                    db_utc_now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM striae_measurements WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        result = dict(row)
        result["polygon"] = _json_or_none(_decrypt_text(result.get("polygon_json"))) or []
        result["metrics"] = _json_or_none(_decrypt_text(result.get("metrics_json"))) or {}
        return result


def save_lesion_labels(session_id: str, labels: dict[str, Any]) -> dict[str, Any]:
    """Persiste rótulos de lesão associados a uma sessão."""
    encrypted = _encrypt_text(json.dumps(labels))
    with connection() as conn:
        conn.execute(
            """UPDATE sessions SET lesion_labels_json = ?, updated_at = ?
               WHERE id = ?""",
            (encrypted, db_utc_now(), session_id),
        )
        session = get_session(session_id)
        return dict(session) if session else {"session_id": session_id}


def delete_measurement(session_id: str, stria_code: str) -> int:
    with connection() as conn:
        cur = conn.execute(
            "DELETE FROM striae_measurements WHERE session_id = ? AND stria_code = ?",
            (session_id, stria_code),
        )
        return cur.rowcount


def save_clinical_scales(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    encrypted = _encrypt_text(json.dumps(payload))
    with connection() as conn:
        conn.execute(
            """UPDATE sessions SET clinical_scales_json = ?, updated_at = ?
               WHERE id = ?""",
            (encrypted, db_utc_now(), session_id),
        )
        session = get_session(session_id)
        return dict(session) if session else {"session_id": session_id}


def upsert_stria_roi(session_id: str, roi_json: str) -> dict[str, Any]:
    with connection() as conn:
        existing = conn.execute(
            "SELECT id FROM stria_roi WHERE session_id = ?", (session_id,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE stria_roi SET roi_json = ?, created_at = ?
                   WHERE session_id = ?""",
                (roi_json, db_utc_now(), session_id),
            )
        else:
            conn.execute(
                """INSERT INTO stria_roi (session_id, roi_json, created_at)
                   VALUES (?, ?, ?)""",
                (session_id, roi_json, db_utc_now()),
            )
        row = conn.execute("SELECT * FROM stria_roi WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row else {"session_id": session_id, "roi_json": roi_json}


def delete_all_stria_roi(session_id: str) -> int:
    with connection() as conn:
        cur = conn.execute(
            "DELETE FROM stria_roi WHERE session_id = ?", (session_id,)
        )
        return cur.rowcount


def delete_all_measurements(session_id: str) -> int:
    with connection() as conn:
        cur = conn.execute(
            "DELETE FROM striae_measurements WHERE session_id = ?", (session_id,)
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Patient and PIN management
# ---------------------------------------------------------------------------

def _session_sort_key(session: dict[str, Any]) -> tuple[int, str]:
    code = str(session.get("code") or session.get("id") or "")
    digits = "".join(ch for ch in code if ch.isdigit())
    return (int(digits) if digits else 999, code)


def _patient_sessions(conn: sqlite3.Connection, patient_id: str) -> list[dict[str, Any]]:
    """Sessões do paciente, ordenadas por T0, T1, T2…."""
    rows = conn.execute(
        "SELECT * FROM sessions WHERE patient_id = ?", (patient_id,)
    ).fetchall()
    sessions = [_serialize_session(dict(row)) for row in rows]
    sessions.sort(key=_session_sort_key)
    return sessions


def _parse_timestamp(value: Any) -> datetime | None:
    """Interpreta datas ISO/SQLite de forma tolerante (sempre em UTC)."""
    text = str(value or "").strip()
    if not text:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _effective_updated_at(
    patient: dict[str, Any], sessions: list[dict[str, Any]]
) -> str:
    """Data de atividade mais recente: paciente, sessões ou criação."""
    candidates: list[Any] = [patient.get("updated_at"), patient.get("created_at")]
    for session in sessions:
        candidates.append(session.get("updated_at"))
        candidates.append(session.get("captured_at"))
    newest: datetime | None = None
    for candidate in candidates:
        parsed = _parse_timestamp(candidate)
        if parsed is not None and (newest is None or parsed > newest):
            newest = parsed
    if newest is not None:
        return newest.isoformat(timespec="seconds")
    return (
        str(patient.get("updated_at") or patient.get("created_at") or "").strip()
        or db_utc_now()
    )


def _decorate_patient(
    conn: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]
) -> dict[str, Any]:
    patient = dict(row)
    # Campos pessoais são gravados cifrados (Fernet); leituras legadas em
    # texto puro continuam funcionando por conta do prefixo ``enc:v1:``.
    for field in PATIENT_PERSONAL_FIELDS:
        if patient.get(field):
            patient[field] = _decrypt_text(patient[field])
    pin_row = conn.execute(
        "SELECT pin_hash FROM patient_pins WHERE patient_id = ?",
        (patient["id"],),
    ).fetchone()
    has_pin = pin_row is not None
    sessions = _patient_sessions(conn, patient["id"])
    done = sum(
        1 for session in sessions
        if session.get("measurements") or session.get("aligned_image_url")
    )
    patient["has_pin"] = has_pin
    patient["locked"] = has_pin
    patient["status"] = "locked" if has_pin else "active"
    patient["sessions"] = sessions
    patient["sessions_total"] = len(sessions)
    patient["sessions_done"] = done
    patient["session_count"] = len(sessions)
    patient["updated_at"] = _effective_updated_at(patient, sessions)
    return patient


def list_patients() -> list[dict[str, Any]]:
    """Lista todos os pacientes cadastrados com contadores de sessão e status."""
    with connection() as conn:
        rows = conn.execute("SELECT * FROM patients ORDER BY code").fetchall()
        return [_decorate_patient(conn, row) for row in rows]


def get_patient(patient_id: str) -> dict[str, Any] | None:
    """Obtém um paciente por ID, incluindo suas sessões."""
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        if row is None:
            return None
        return _decorate_patient(conn, row)


def get_patient_session_by_code(
    patient_id: str, session_code: str
) -> dict[str, Any] | None:
    """Localiza a sessão de código T0/T1/... de um paciente."""
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE patient_id = ? AND session_code = ?",
            (patient_id, session_code),
        ).fetchone()
        if row is None:
            return None
        return _serialize_session(dict(row))


def _insert_patient_session(
    conn: sqlite3.Connection, patient_id: str, code: str
) -> str:
    session_id = f"{patient_id}:{code}"
    now = db_utc_now()
    conn.execute(
        "INSERT INTO sessions"
        " (id, patient_id, session_code, label, captured_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            session_id,
            patient_id,
            code,
            "Sessão T0 — baseline" if code == "T0" else f"Sessão {code} — acompanhamento",
            None,
            now,
        ),
    )
    return session_id


def create_patient(
    code: str | None,
    name: str,
    notes: str,
    sessions_count: int,
    *,
    birth_date: str = "",
    phone: str = "",
) -> dict[str, Any]:
    """Cria paciente com sessões iniciais (T0 baseline + T1..T(n-1)).

    Quando ``code`` é omitido/vazio, gera um identificador público único
    ``PAC-XXXXXX`` não sequencial. Códigos explícitos são preservados para
    compatibilidade com importações e prontuários legados (``PAC-01``…).
    Os dados pessoais são gravados cifrados (Fernet/AES) no SQLite.
    """
    patient_id = str(uuid.uuid4())
    now = db_utc_now()
    with connection() as conn:
        resolved = str(code or "").strip().upper()
        if resolved:
            existing = conn.execute(
                "SELECT id FROM patients WHERE code = ?", (resolved,)
            ).fetchone()
            if existing:
                raise ValueError(f"Já existe um paciente com o código {resolved}.")
        else:
            resolved = generate_patient_code(conn)
        conn.execute(
            "INSERT INTO patients"
            " (id, code, name, notes, birth_date, phone, locked, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                patient_id,
                resolved,
                _encrypt_text(name),
                _encrypt_text(notes or ""),
                _encrypt_text(birth_date or ""),
                _encrypt_text(phone or ""),
                now,
                now,
            ),
        )
        for i in range(0, sessions_count):
            _insert_patient_session(conn, patient_id, f"T{i}")
    patient = get_patient(patient_id)
    assert patient is not None
    return patient


def update_patient_personal_data(
    patient_id: str, values: dict[str, Any]
) -> dict[str, Any] | None:
    """Atualiza e re-cifra os dados pessoais do paciente.

    Apenas campos presentes em ``values`` são alterados. Retorna o paciente
    decorado (já descriptografado) ou ``None`` quando o ID não existe.
    """
    assignments: list[str] = []
    params: list[Any] = []
    for field in PATIENT_PERSONAL_FIELDS:
        if field not in values:
            continue
        text = str(values.get(field) or "").strip()
        assignments.append(f"{field} = ?")
        params.append(_encrypt_text(text))
    now = db_utc_now()
    assignments.append("updated_at = ?")
    params.append(now)
    with connection() as conn:
        row = conn.execute(
            "SELECT id FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        if row is None:
            return None
        params.append(patient_id)
        conn.execute(
            f"UPDATE patients SET {', '.join(assignments)} WHERE id = ?", params
        )
    return get_patient(patient_id)


def add_patient_session(patient_id: str) -> dict[str, Any] | None:
    """Adiciona a próxima sessão de acompanhamento (T(n))."""
    patient = get_patient(patient_id)
    if patient is None:
        return None
    codes = [s["code"] if s.get("code") else s["id"] for s in patient["sessions"]]
    highest = 0
    for code in codes:
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        if digits:
            highest = max(highest, int(digits))
    with connection() as conn:
        _insert_patient_session(conn, patient_id, f"T{highest + 1}")
        conn.execute(
            "UPDATE patients SET updated_at = ? WHERE id = ?", (db_utc_now(), patient_id)
        )
    return get_patient(patient_id)


def delete_patient_session(patient_id: str, session_id: str) -> dict[str, Any]:
    """Remove uma sessão vazia (T0 nunca pode ser removido)."""
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND patient_id = ?",
            (session_id, patient_id),
        ).fetchone()
        if row is None:
            raise ValueError("Sessão do paciente não encontrada.")
        code = row["session_code"] or row["id"]
        if code == "T0":
            raise ValueError("A sessão T0 é o baseline e não pode ser removida.")
        measurements = conn.execute(
            "SELECT COUNT(*) AS total FROM striae_measurements WHERE session_id = ?",
            (session_id,),
        ).fetchone()["total"]
        has_content = any(
            row[field]
            for field in (
                "clean_image_path",
                "ruler_image_path",
                "aligned_image_path",
                "enhanced_image_path",
            )
        )
        if measurements or has_content:
            raise ValueError("Apenas sessões vazias podem ser removidas.")
        conn.execute("DELETE FROM striae_measurements WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM stria_roi WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    patient = get_patient(patient_id)
    assert patient is not None
    return patient


def delete_patient(patient_id: str) -> dict[str, Any] | None:
    """Exclui permanentemente um prontuário, suas sessões, medições e arquivos."""
    image_fields = (
        "clean_image_path",
        "ruler_image_path",
        "aligned_image_path",
        "enhanced_image_path",
    )
    candidate_paths: set[str] = set()
    still_referenced: set[str] = set()

    with connection() as conn:
        patient_row = conn.execute(
            "SELECT id, code FROM patients WHERE id = ?",
            (patient_id,),
        ).fetchone()
        if patient_row is None:
            return None
        summary = {
            "id": str(patient_row["id"]),
            "code": str(patient_row["code"]),
        }
        session_rows = conn.execute(
            "SELECT * FROM sessions WHERE patient_id = ?",
            (patient_id,),
        ).fetchall()
        session_ids = [str(r["id"]) for r in session_rows]
        for srow in session_rows:
            for field in image_fields:
                val = srow[field]
                if val:
                    candidate_paths.add(str(val))

        for sid in session_ids:
            conn.execute("DELETE FROM striae_measurements WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM stria_roi WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE patient_id = ?", (patient_id,))
        conn.execute("DELETE FROM patient_pins WHERE patient_id = ?", (patient_id,))
        conn.execute("DELETE FROM patients WHERE id = ?", (patient_id,))

        if candidate_paths:
            remaining = conn.execute(
                "SELECT clean_image_path, ruler_image_path, aligned_image_path, enhanced_image_path FROM sessions"
            ).fetchall()
            for r in remaining:
                for field in image_fields:
                    val = r[field]
                    if val:
                        still_referenced.add(str(val))

    removed_files = 0
    for rel in candidate_paths - still_referenced:
        abs_path = storage_absolute(rel)
        if abs_path is not None:
            try:
                if abs_path.is_file():
                    abs_path.unlink()
                    removed_files += 1
            except OSError:
                pass
    summary["removed_files"] = removed_files
    return summary


def verify_patient_pin(patient_id: str, pin: str) -> bool:
    """Verifica se o PIN fornecido corresponde ao paciente."""
    with connection() as conn:
        row = conn.execute(
            "SELECT pin_hash FROM patient_pins WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()
    if not row:
        return False
    return hashlib.sha256(pin.encode("utf-8")).hexdigest() == row[0]


def set_patient_pin(patient_id: str, pin: str | None) -> None:
    """Define ou remove o PIN individual do paciente."""
    now = db_utc_now()
    with connection() as conn:
        if pin and pin.strip():
            conn.execute(
                "INSERT OR REPLACE INTO patient_pins (patient_id, pin_hash, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (patient_id, hashlib.sha256(pin.strip().encode("utf-8")).hexdigest(), now, now),
            )
            conn.execute(
                "UPDATE patients SET locked = 1, updated_at = ? WHERE id = ?",
                (now, patient_id),
            )
        else:
            conn.execute(
                "DELETE FROM patient_pins WHERE patient_id = ?",
                (patient_id,),
            )
            conn.execute(
                "UPDATE patients SET locked = 0, updated_at = ? WHERE id = ?",
                (now, patient_id),
            )