"""API FastAPI do DermaMetric.

Execução local (a partir da raiz do projeto)::

    .venv/bin/uvicorn backend.main:app --reload --port 8000

Documentação interativa em ``/docs``.
"""

from __future__ import annotations

import hmac
import io
import json
import os
import mimetypes
import secrets
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4
import uuid

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

try:  # importação como pacote (uvicorn backend.main:app / pytest)
    from . import database as db
    from . import paths, security, vision, auth, report, updater
    from . import backup as backup_mod
    from .version import APP_VERSION
    from .models import (
        CalibrationRequest,
        ClinicalScalesRequest,
        LesionLabelsRequest,
        MeasureRequest,
        RegisterRequest,
        SessionMetadataRequest,
        normalize_datetime,
    )
except ImportError:  # pragma: no cover - execução direta de dentro de backend/
    import database as db  # type: ignore[no-redef]
    import paths  # type: ignore[no-redef]
    import security  # type: ignore[no-redef]
    import vision  # type: ignore[no-redef]
    import auth  # type: ignore[no-redef]
    import report  # type: ignore[no-redef]
    import updater  # type: ignore[no-redef]
    from version import APP_VERSION  # type: ignore[no-redef]
    import backup as backup_mod  # type: ignore[no-redef]
    from models import (  # type: ignore[no-redef]
        CalibrationRequest,
        ClinicalScalesRequest,
        LesionLabelsRequest,
        MeasureRequest,
        RegisterRequest,
        SessionMetadataRequest,
        normalize_datetime,
    )

BASE_DIR = paths.ASSETS_DIR
FRONTEND_DIR = BASE_DIR / "frontend"
ALLOWED_IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff",
}
_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
for _ext, _mime in _IMAGE_MEDIA_TYPES.items():
    mimetypes.add_type(_mime, _ext)
MIN_PX_PER_MM = 1.5
MAX_PX_PER_MM = 50.0
EXIF_DATETIME_TAGS = (36867, 36868, 306)  # DateTimeOriginal, Digitized, DateTime

# Partículas que não geram inicial de sigilo: "Ana de Souza" -> "A. S."
NAME_PARTICLES = {
    "de", "da", "do", "das", "dos", "e", "di", "du", "del", "della", "van", "von",
}


def mask_patient_name(full_name: str) -> str:
    """Converte um nome completo em iniciais seguras.

    ``"Ana Julia Viana Silva"`` -> ``"A. J. V. S."``. Partículas como *de*,
    *da* e *dos* são ignoradas. Nomes vazios produzem string vazia.
    """
    parts = [
        part for part in str(full_name or "").split()
        if part and part.lower() not in NAME_PARTICLES
    ]
    return " ".join(f"{part[0].upper()}." for part in parts if part)


def patient_initials(full_name: str) -> str:
    """Iniciais compactas para o avatar (máximo de 2 letras).

    ``"Ana Julia Viana Silva"`` -> ``"AJ"``. Usado no diretório e nos cards
    para que o nome completo nunca apareça na tela inicial.
    """
    parts = [
        part for part in str(full_name or "").split()
        if part and part.lower() not in NAME_PARTICLES
    ]
    letters = "".join(part[0].upper() for part in parts if part)
    return letters[:2]


def _apply_name_privacy(patient: dict[str, Any], reveal: bool) -> dict[str, Any]:
    """Adiciona ``masked_name``/``name_initials`` e oculta o nome se preciso.

    Dados pessoais sensíveis (notas clínicas, nascimento e telefone) nunca
    saem pela listagem pública ou pelo GET comum: só são liberados pelo
    endpoint de dados pessoais, que exige a senha mestra da estação.
    """
    full_name = str(patient.get("name") or "")
    patient["masked_name"] = mask_patient_name(full_name)
    patient["name_initials"] = patient_initials(full_name)
    locked = bool(patient.get("has_pin") or patient.get("locked"))
    if locked and not reveal:
        patient["name"] = None
        patient["name_hidden"] = True
    else:
        patient["name_hidden"] = False
    patient["notes"] = None
    patient["birth_date"] = None
    patient["phone"] = None
    patient["personal_data_hidden"] = True
    return patient


def _normalize_birth_date(value: Any) -> str:
    """Valida ``YYYY-MM-DD`` e devolve a data normalizada (ou vazio)."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Data de nascimento inválida. Use o formato AAAA-MM-DD.",
        ) from exc
    if parsed > date.today() or parsed.year < 1900:
        raise HTTPException(
            status_code=400, detail="Data de nascimento fora do intervalo aceito."
        )
    return parsed.isoformat()


def _age_from_birth_date(value: Any) -> int | None:
    """Idade em anos completos a partir da data de nascimento (ou ``None``)."""
    if not value:
        return None
    try:
        born = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    today = date.today()
    return today.year - born.year - (
        (today.month, today.day) < (born.month, born.day)
    )


def _personal_data_payload(patient: dict[str, Any]) -> dict[str, Any]:
    """Dados pessoais completos para o modal autenticado pela senha mestra."""
    phone = str(patient.get("phone") or "")
    birth_date = str(patient.get("birth_date") or "")
    return {
        "id": patient.get("id"),
        "code": patient.get("code"),
        "name": patient.get("name") or "",
        "masked_name": mask_patient_name(patient.get("name") or ""),
        "name_initials": patient_initials(patient.get("name") or ""),
        "birth_date": birth_date,
        "age": _age_from_birth_date(birth_date),
        "phone": phone,
        "contact": phone,
        "notes": patient.get("notes") or "",
        "has_pin": bool(patient.get("has_pin") or patient.get("locked")),
        "created_at": patient.get("created_at"),
        "updated_at": patient.get("updated_at"),
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="DermaMetric API",
    description=(
        "Plataforma clínica de fotogrametria e morfometria para acompanhamento "
        "longitudinal de lesões e regiões cutâneas (T0, T2, T3, T4)."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

LAUNCH_TOKEN_COOKIE = "dm_desktop_session"
_LOCAL_ORIGIN_REGEX = r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_LOCAL_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def desktop_launch_guard(request: Request, call_next):
    expected_token = (os.environ.get("DERMAMETRIC_LAUNCH_TOKEN") or "").strip()
    if not expected_token:
        return await call_next(request)

    path = request.url.path
    if path == "/api/health":
        return await call_next(request)

    query_token = (request.query_params.get("launch_token") or "").strip()
    cookie_token = (request.cookies.get(LAUNCH_TOKEN_COOKIE) or "").strip()
    header_token = (request.headers.get("X-DermaMetric-Launch") or "").strip()

    authorized = any(
        candidate and hmac.compare_digest(candidate, expected_token)
        for candidate in (query_token, cookie_token, header_token)
    )
    if not authorized:
        return Response(
            content=json.dumps(
                {"detail": "Acesso restrito à janela nativa do DermaMetric."}
            ),
            status_code=403,
            media_type="application/json",
        )

    response = await call_next(request)
    if query_token and hmac.compare_digest(query_token, expected_token):
        response.set_cookie(
            key=LAUNCH_TOKEN_COOKIE,
            value=expected_token,
            httponly=True,
            samesite="lax",
            path="/",
        )
    return response

# Garante diretórios e schema antes de montar os estáticos.
db.init_db()
app.mount(
    "/static",
    StaticFiles(directory=str(FRONTEND_DIR)),
    name="static",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _session_code(row) -> str:
    """Código clínico da sessão (T0, T1, T2…) independente do id persistido."""
    try:
        return str(row["session_code"] or row["id"])
    except (KeyError, IndexError, TypeError):
        return str(row["id"])


def _reference_t0_id(row) -> str:
    """Id da sessão T0 do mesmo paciente (ou T0 legado, quando sem paciente)."""
    try:
        patient_id = row["patient_id"]
    except (KeyError, IndexError, TypeError):
        patient_id = None
    if patient_id:
        t0 = db.get_patient_session_by_code(patient_id, "T0")
        if t0 is not None:
            return t0["id"]
    return "T0"


def _require_session(session_id: str):
    row = db.get_session_row(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Sessão não encontrada: {session_id}")
    return row


@contextmanager
def _materialized(relative: str | None) -> Iterator[Path]:
    """Descriptografa um arquivo para um caminho temporário de trabalho."""
    path = db.storage_absolute(relative)
    if path is None or not path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Arquivo ausente no armazenamento: {relative}",
        )
    logical = relative[:-4] if relative and relative.endswith(".enc") else relative
    suffix = Path(logical or "image.png").suffix or ".png"
    temporary = db.WORK_DIR / f"{uuid4().hex}{suffix}"
    db.WORK_DIR.mkdir(parents=True, exist_ok=True)
    temporary.write_bytes(security.read_encrypted_file(path))
    try:
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def _vision_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _exif_datetime(content: bytes) -> str | None:
    """Extrai DateTimeOriginal/Digitized/DateTime de um JPEG/TIFF."""
    try:
        with Image.open(io.BytesIO(content)) as image:
            exif = image.getexif()
            candidates: list[Any] = []
            for tag in EXIF_DATETIME_TAGS:
                value = exif.get(tag)
                if value:
                    candidates.append(value)
            try:  # EXIF sub-IFD (DateTimeOriginal costuma viver aqui)
                sub_ifd = exif.get_ifd(0x8769)
                for tag in EXIF_DATETIME_TAGS[:2]:
                    value = sub_ifd.get(tag)
                    if value:
                        candidates.append(value)
            except Exception:  # noqa: BLE001 - EXIF malformado
                pass
            for raw in candidates:
                text = str(raw).strip()
                if not text:
                    continue
                for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                    try:
                        parsed = datetime.strptime(text[:19], fmt)
                        return parsed.strftime("%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
    except Exception:  # noqa: BLE001 - imagem sem EXIF legível
        return None
    return None


async def _store_upload(
    upload: UploadFile, session_id: str, kind: str
) -> tuple[str, bytes]:
    original = upload.filename or ""
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Formato não suportado para {kind}: '{suffix or 'sem extensão'}'. "
                f"Aceitos: {sorted(ALLOWED_IMAGE_SUFFIXES)}"
            ),
        )
    content = await upload.read()
    if not content:
        raise HTTPException(status_code=400, detail=f"Arquivo {kind} vazio")

    # Valida que a imagem é decodificável antes de cifrar e persistir.
    try:
        vision.imdecode_bgr(content)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Imagem {kind} inválida: {exc}"
        ) from exc

    safe_session = "".join(
        ch if ch.isalnum() or ch in "-_" else "_" for ch in session_id
    )
    filename = f"{safe_session}_{kind}_{uuid4().hex[:10]}{suffix}.enc"
    destination = db.UPLOAD_DIR / filename
    destination.write_bytes(security.encrypt_bytes(content))
    try:
        destination.chmod(0o600)
    except OSError:  # pragma: no cover - sistemas sem suporte
        pass
    return db.storage_relative(destination), content


def _landmarks_payload(payload: RegisterRequest) -> list[dict[str, Any]]:
    landmarks: list[dict[str, Any]] = []
    for index, point in enumerate(payload.landmarks):
        label = (point.label or "").strip() or f"Ponto {index + 1}"
        landmarks.append(
            {"label": label[:80], "x": float(point.x), "y": float(point.y)}
        )
    return landmarks


def _landmark_coords(landmarks: list[dict[str, Any]]) -> list[list[float]]:
    return [[float(p["x"]), float(p["y"])] for p in landmarks]
# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "dermametric",
        "version": APP_VERSION,
        "sessions": list(db.SESSION_IDS),
        "db": str(db.DB_PATH),
        "encryption": {
            "at_rest": True,
            "algorithm": "Fernet (AES-128-CBC + HMAC-SHA256)",
            "key_fingerprint": security.key_fingerprint(),
        },
    }


# ---------------------------------------------------------------------------
# Atualizações (GitHub Releases)
# ---------------------------------------------------------------------------
@app.get("/api/system/update/check")
def system_update_check() -> dict[str, Any]:
    """Consulta a última release publicada e compara com a versão instalada."""
    try:
        return updater.check_for_updates()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/system/update/status")
def system_update_status() -> dict[str, Any]:
    """Estado atual do download/instalação da atualização em background."""
    return updater.get_update_status()


@app.post("/api/system/update/apply")
def system_update_apply() -> dict[str, Any]:
    """Inicia o download e a instalação da atualização disponível."""
    try:
        return updater.start_update()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/auth/status")
def auth_status():
    """Status da autenticação para a tela de bloqueio/login."""
    configured = auth._access_hash() is not None
    return {
        "configured": configured,
        "access_token": auth.get_access_token(),
        "access_token_expires_at": auth.get_access_expires_at(),
        "patient_id": None,
        "patient_status": None,
        "setup_hint": "Defina uma senha de acesso para a estação clínica." if not configured else None,
        "recovery": auth.recovery.status(),
    }


@app.post("/api/auth/setup")
def auth_setup(password: str):
    """Configura a senha de acesso e devolve o arquivo-chave inicial."""
    try:
        return auth.setup(password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/login")
def auth_login(password: str):
    """Login para obter token de acesso."""
    result = auth.login(password)
    if not result:
        raise HTTPException(status_code=401, detail="Senha incorreta")
    return result


@app.post("/api/auth/recover")
def auth_recover(payload: dict[str, Any]):
    """Redefine a senha mestra com a Chave de Recuperação (rotação automática)."""
    recovery_key = str(payload.get("recovery_key") or "").strip()
    new_password = str(payload.get("new_password") or "")
    if not recovery_key:
        raise HTTPException(status_code=400, detail="Informe a chave de recuperação.")
    try:
        return auth.recover(recovery_key, new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/change-password")
def auth_change_password(payload: dict[str, Any], request: Request):
    """Troca a senha mestra (rota autenticada)."""
    _require_station_access(request)
    try:
        return auth.change_password(
            str(payload.get("current_password") or ""),
            str(payload.get("new_password") or ""),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/regenerate-recovery-key")
def auth_regenerate_recovery_key(payload: dict[str, Any], request: Request):
    """Gera (rotaciona) um novo arquivo-chave ``.dmkey`` (rota autenticada)."""
    _require_station_access(request)
    try:
        return auth.regenerate_recovery_key(str(payload.get("master_password") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/logout")
def auth_logout():
    """Desfaz o token de acesso."""
    auth.destroy_access_token()
    return {"message": "Deslogado com sucesso"}


@app.get("/api/files/{kind}/{filename}")
def get_encrypted_file(
    kind: str,
    filename: str,
    watermark: bool = True,
    session: str | None = None,
) -> Response:
    """Serve imagens descriptografadas sob demanda (sem expor o storage).

    Imagens processadas (``aligned``/``enhanced``) recebem marca d'água de
    exibição por padrão; use ``?watermark=0`` para obter os pixels originais.
    O arquivo em disco nunca é alterado.
    """
    directories = {"uploads": db.UPLOAD_DIR, "processed": db.PROCESSED_DIR}
    directory = directories.get(kind)
    if directory is None:
        raise HTTPException(status_code=404, detail="Tipo de arquivo inválido")
    if filename != Path(filename).name or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Nome de arquivo inválido")

    candidates = (directory / filename, directory / f"{filename}.enc")
    path = next((candidate for candidate in candidates if candidate.exists()), None)
    if path is None:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")

    try:
        data = security.read_encrypted_file(path)
    except Exception as exc:  # noqa: BLE001 - chave incorreta/arquivo corrompido
        raise HTTPException(status_code=500, detail="Falha ao descriptografar arquivo") from exc

    logical_name = filename[:-4] if filename.endswith(".enc") else filename
    suffix = Path(logical_name).suffix.lower()
    media_type = (
        _IMAGE_MEDIA_TYPES.get(suffix)
        or mimetypes.guess_type(logical_name)[0]
        or "application/octet-stream"
    )
    if watermark and kind == "processed":
        data = _watermarked_display_bytes(data, session, logical_name)
        media_type = "image/png"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=30"},
    )


def _watermarked_display_bytes(
    data: bytes, session_id: str | None, logical_name: str
) -> bytes:
    """Aplica a tarja clínica em uma cópia em memória (nunca no disco)."""
    try:
        image = vision.imdecode_bgr(data)
    except ValueError:
        return data
    context: dict[str, Any]
    if session_id and len(session_id) <= 200:
        context = db.watermark_context(session_id)
    else:
        # Sem contexto explícito, usa o prefixo do arquivo (ex.: ``T2_aligned``).
        context = {
            "session_code": logical_name.split("_")[0] or None,
            "patient_code": None,
            "captured_at": None,
        }
    marked = vision.apply_clinical_watermark(
        image,
        context.get("captured_at"),
        context.get("session_code") or "",
        context.get("patient_code"),
    )
    return vision.encode_image_bgr(marked)


@app.get("/api/sessions")
def get_sessions() -> dict[str, Any]:
    sessions = db.list_sessions()
    return {"count": len(sessions), "sessions": sessions}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    _require_session(session_id)
    session = db.get_session(session_id)
    assert session is not None
    return session


@app.patch("/api/sessions/{session_id}")
def update_session_metadata(
    session_id: str,
    payload: SessionMetadataRequest
) -> dict[str, Any]:
    _require_session(session_id)
    updates: dict[str, Any] = {}
    if payload.label is not None:
        updates["label"] = payload.label.strip()
    if payload.captured_at is not None:
        updates["captured_at"] = payload.captured_at
    if payload.lesion_labels is not None:
        updates["lesion_labels_json"] = json.dumps(payload.lesion_labels)
    session = db.update_session(session_id, **updates) if updates else db.get_session(session_id)
    assert session is not None
    return {"session_id": session_id, "session": session}


@app.post("/api/sessions/{session_id}/lesion-labels")
def save_lesion_labels(
    session_id: str,
    payload: LesionLabelsRequest
) -> dict[str, Any]:
    _require_session(session_id)
    session = db.save_lesion_labels(session_id, payload.labels)
    return {
        "session_id": session_id,
        "lesion_labels": payload.labels,
        "session": session,
    }


@app.post("/api/sessions/{session_id}/upload")
async def upload_images(
    session_id: str,
    label: str | None = Form(default=None),
    captured_at: str | None = Form(default=None),
    ruler_image: UploadFile | None = File(default=None),
    clean_image: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    _require_session(session_id)
    if ruler_image is None and clean_image is None:
        raise HTTPException(
            status_code=400,
            detail="Envie ao menos uma imagem para enviar.",
        )

    manual_date: str | None = None
    if captured_at and captured_at.strip():
        try:
            manual_date = normalize_datetime(captured_at)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    updates: dict[str, Any] = {}
    clean_content: bytes | None = None
    ruler_content: bytes | None = None

    if ruler_image is not None:
        updates["ruler_image_path"], ruler_content = await _store_upload(
            ruler_image, session_id, "ruler"
        )
    if clean_image is not None:
        updates["clean_image_path"], clean_content = await _store_upload(
            clean_image, session_id, "clean"
        )
    if label is not None and label.strip():
        updates["label"] = label.strip()

    detected = (
        (_exif_datetime(clean_content) if clean_content else None)
        or (_exif_datetime(ruler_content) if ruler_content else None)
    )
    if manual_date:
        updates["captured_at"] = manual_date
    elif detected:
        updates["captured_at"] = detected
    elif clean_content or ruler_content:
        updates["captured_at"] = db.local_now_string()

    session = db.update_session(session_id, **updates)
    clean_image_url = None
    ruler_image_url = None

    def _file_url(kind: str, relative: str | None) -> str | None:
        if not relative:
            return None
        prefix = f"{kind}/"
        name = relative[len(prefix):] if relative.startswith(prefix) else relative
        return f"/api/files/{kind}/{name}"

    if "clean_image_path" in updates:
        clean_image_url = _file_url("uploads", updates["clean_image_path"])
    if "ruler_image_path" in updates:
        ruler_image_url = _file_url("uploads", updates["ruler_image_path"])
    return {
        "session_id": session_id,
        "captured_at": session["captured_at"],
        "session": {
            "clean_image_url": clean_image_url,
            "ruler_image_url": ruler_image_url,
            "label": session.get("label", ""),
            "captured_at": session["captured_at"],
        },
    }


@app.post("/api/sessions/{session_id}/calibrate")
def calibrate(session_id: str, payload: CalibrationRequest) -> dict[str, Any]:
    _require_session(session_id)
    px_per_mm = _vision_call(
        vision.compute_px_per_mm, payload.p1, payload.p2, payload.known_mm
    )
    if not (MIN_PX_PER_MM <= px_per_mm <= MAX_PX_PER_MM):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Escala implausível: {px_per_mm:.2f} px/mm. "
                f"Verifique a régua e a distância real "
                f"(faixa esperada: {MIN_PX_PER_MM}–{MAX_PX_PER_MM} px/mm)."
            ),
        )
    ruler_points = {
        "p1": payload.p1,
        "p2": payload.p2,
        "known_mm": payload.known_mm,
    }
    session = db.update_session(
        session_id,
        px_per_mm=float(px_per_mm),
        ruler_points_json=ruler_points,
    )
    return {
        "session_id": session_id,
        "px_per_mm": float(px_per_mm),
        "ruler_points": ruler_points,
        "session": session,
    }


@app.post("/api/sessions/{session_id}/register")
def register(session_id: str, payload: RegisterRequest) -> dict[str, Any]:
    row = _require_session(session_id)
    landmarks = _landmarks_payload(payload)
    coords = _landmark_coords(landmarks)

    raw_path = db.WORK_DIR / f"{uuid4().hex}_aligned_raw.png"
    aligned_path = db.WORK_DIR / f"{uuid4().hex}_aligned.png"
    enhanced_path = db.WORK_DIR / f"{uuid4().hex}_enhanced.png"
    is_reference_session = _session_code(row) == "T0"
    reference_roi = payload.healthy_skin_roi

    try:
        with _materialized(row["clean_image_path"]) as clean_path:
            if is_reference_session:
                reference_image = _vision_call(vision.read_image_bgr, clean_path)
                output_shape = reference_image.shape[:2]
                ref_coords = coords
                ref_lab_means = None
            else:
                t0_row = _require_session(_reference_t0_id(row))
                t0_session = db.get_session(t0_row["id"])
                assert t0_session is not None
                t0_landmarks = t0_session.get("landmarks")
                if not t0_landmarks or len(t0_landmarks) != 4:
                    raise HTTPException(
                        status_code=400,
                        detail="A sessão T0 precisa ser registrada antes das demais",
                    )
                t0_roi = t0_session.get("healthy_skin_roi")
                if not t0_roi:
                    raise HTTPException(
                        status_code=400,
                        detail="A sessão T0 não possui ROI de pele saudável registrada",
                    )
                reference_roi = t0_roi
                ref_coords = _landmark_coords(t0_landmarks)
                with _materialized(t0_row["aligned_image_path"]) as t0_path:
                    t0_image = _vision_call(vision.read_image_bgr, t0_path)
                    output_shape = t0_image.shape[:2]
                    ref_lab_means = _vision_call(
                        vision.compute_region_lab_means, str(t0_path), t0_roi
                    )

            _, homography = _vision_call(
                vision.align_to_reference,
                str(clean_path),
                coords,
                ref_coords,
                output_shape,
                str(raw_path),
                is_reference=is_reference_session,
                return_homography=True,
            )

            # A ROI é marcada nas coordenadas da sessão atual; para medir a
            # pele saudável na imagem já alinhada, ela precisa ser projetada
            # pela homografia. Se a projeção sair da imagem, usa-se a ROI de
            # referência de T0 (que já está nesse espaço).
            normalization_roi = list(payload.healthy_skin_roi)
            if homography is not None:
                projected_roi = _vision_call(
                    vision.project_roi, payload.healthy_skin_roi, homography, output_shape
                )
                if projected_roi is not None:
                    normalization_roi = projected_roi
                else:
                    normalization_roi = list(reference_roi)

            _, measured_means = _vision_call(
                vision.normalize_and_enhance_cielab,
                str(raw_path),
                normalization_roi,
                ref_lab_means,
                str(enhanced_path),
                normalized_output_path=str(aligned_path),
            )

            safe_session = "".join(
                ch if ch.isalnum() or ch in "-_" else "_" for ch in session_id
            )
            aligned_enc = security.encrypt_file(
                aligned_path, db.PROCESSED_DIR / f"{safe_session}_aligned.png"
            )
            enhanced_enc = security.encrypt_file(
                enhanced_path, db.PROCESSED_DIR / f"{safe_session}_enhanced.png"
            )

            session = db.update_session(
                session_id,
                landmarks_json=landmarks,
                healthy_skin_roi_json=payload.healthy_skin_roi,
                aligned_image_path=db.storage_relative(aligned_enc),
                enhanced_image_path=db.storage_relative(enhanced_enc),
            )
    finally:
        for temporary in (raw_path, aligned_path, enhanced_path):
            temporary.unlink(missing_ok=True)

    return {
        "session_id": session_id,
        "aligned_url": session["aligned_image_url"],
        "enhanced_url": session["enhanced_image_url"],
        "lab_means": {"input": measured_means, "reference": ref_lab_means},
        "roi": {
            "input": list(payload.healthy_skin_roi),
            "normalization": normalization_roi,
            "reference": list(reference_roi),
        },
        "session": session,
    }


@app.post("/api/sessions/{session_id}/measure")
def measure(session_id: str, payload: MeasureRequest) -> dict[str, Any]:
    row = _require_session(session_id)
    if not row["px_per_mm"]:
        raise HTTPException(
            status_code=400,
            detail="Calibre a régua (px_per_mm) antes de medir",
        )

    with _materialized(row["aligned_image_path"]) as aligned_path:
        metrics = _vision_call(
            vision.measure_stria_polygon,
            str(aligned_path),
            payload.polygon,
            float(row["px_per_mm"]),
        )

    session = db.get_session(session_id)
    labels = (session or {}).get("lesion_labels") or {}
    label = (payload.stria_label or labels.get(payload.stria_code) or "").strip() or None

    saved = db.upsert_measurement(
        session_id, payload.stria_code, payload.polygon, metrics, label
    )
    return {
        "session_id": session_id,
        "stria_code": payload.stria_code,
        "stria_label": saved.get("stria_label"),
        "measurement_id": saved["id"],
        "created_at": saved["created_at"],
        **metrics,
        "measurement": saved,
    }


@app.delete("/api/sessions/{session_id}/measurements")
def clear_measurements(session_id: str) -> dict[str, Any]:
    _require_session(session_id)
    removed = db.delete_all_measurements(session_id)
    return {
        "session_id": session_id,
        "deleted": removed,
        "session": db.get_session(session_id),
    }


@app.delete("/api/sessions/{session_id}/measurements/{stria_code}")
def delete_measurement(session_id: str, stria_code: str) -> dict[str, Any]:
    _require_session(session_id)
    removed = db.delete_measurement(session_id, stria_code)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"Nenhuma medição de {stria_code} encontrada nesta sessão",
        )
    return {
        "session_id": session_id,
        "stria_code": stria_code,
        "deleted": True,
        "session": db.get_session(session_id),
    }


@app.post("/api/sessions/{session_id}/clinical-scales")
def save_clinical_scales(
    session_id: str,
    payload: ClinicalScalesRequest
) -> dict[str, Any]:
    _require_session(session_id)
    session = db.save_clinical_scales(session_id, payload.model_dump())
    return {
        "session_id": session_id,
        "clinical_scales": payload.model_dump(),
        "session": session,
    }


@app.get("/api/patients")
def list_patients() -> dict[str, Any]:
    """Lista pacientes cadastrados com sessões e status de acompanhamento.

    Sempre inclui ``masked_name`` (iniciais) para o modo sigilo do diretório.
    Pacientes protegidos por PIN têm o nome completo omitido (``name: None``);
    o nome é liberado pelo desbloqueio via ``POST .../unlock``.
    """
    patients = db.list_patients()
    return {
        "count": len(patients),
        "patients": [_apply_name_privacy(patient, reveal=False) for patient in patients],
    }


@app.post("/api/patients")
def create_patient(payload: dict[str, Any]) -> dict[str, Any]:
    """Cria paciente com sessões iniciais (T0 baseline + T1..Tn).

    O código público é gerado automaticamente no formato ``PAC-XXXXXX``
    (aleatório e não sequencial). Um ``code`` explícito é aceito apenas por
    compatibilidade com importações e prontuários legados.
    """
    code = str(payload.get("code") or "").strip().upper()
    name = str(payload.get("name") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    birth_date = _normalize_birth_date(payload.get("birth_date"))
    phone = str(payload.get("phone") or payload.get("contact") or "").strip()
    try:
        sessions_count = int(payload.get("sessions") or 3)
    except (TypeError, ValueError):
        sessions_count = 3
    if sessions_count < 2 or sessions_count > 6:
        raise HTTPException(status_code=400, detail="Número de sessões deve estar entre 2 e 6.")
    if not name:
        raise HTTPException(status_code=400, detail="O nome do paciente é obrigatório.")
    if len(name) > 120:
        raise HTTPException(status_code=400, detail="O nome excede 120 caracteres.")
    if len(phone) > 40:
        raise HTTPException(status_code=400, detail="O contato excede 40 caracteres.")
    if len(notes) > 2000:
        raise HTTPException(status_code=400, detail="As observações excedem 2000 caracteres.")
    try:
        patient = db.create_patient(
            code or None,
            name,
            notes,
            sessions_count,
            birth_date=birth_date,
            phone=phone,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _apply_name_privacy(patient, reveal=True)


@app.get("/api/patients/{patient_id}")
def get_patient(patient_id: str, request: Request) -> dict[str, Any]:
    patient = db.get_patient(patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Paciente não encontrado")
    reveal = False
    if patient.get("has_pin"):
        pin = _patient_pin_from_request(request)
        reveal = bool(pin) and db.verify_patient_pin(patient_id, pin)
    _apply_name_privacy(patient, reveal=reveal)
    patient["patient"] = {
        key: value for key, value in patient.items() if key != "sessions"
    }
    return patient


@app.post("/api/patients/{patient_id}/sessions")
def add_patient_session(patient_id: str) -> dict[str, Any]:
    """Adiciona a próxima sessão de acompanhamento (T(n+1))."""
    patient = db.add_patient_session(patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Paciente não encontrado")
    return patient


@app.delete("/api/patients/{patient_id}/sessions/{session_id}")
def delete_patient_session(patient_id: str, session_id: str) -> dict[str, Any]:
    """Remove uma sessão vazia além do T0."""
    try:
        patient = db.delete_patient_session(patient_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return patient


@app.post("/api/patients/{patient_id}/delete")
def delete_patient(patient_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Exclui definitivamente o prontuário após confirmar a senha mestra.

    Remove sessões, medições, ROIs, PIN e as imagens criptografadas que não
    sejam compartilhadas com outros prontuários. A operação é irreversível.
    """
    password = str(
        payload.get("master_password") or payload.get("password") or ""
    )
    if not password or not auth.verify_access(password):
        raise HTTPException(status_code=401, detail="Senha mestra incorreta.")
    summary = db.delete_patient(patient_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Paciente não encontrado")
    return {"deleted": True, **summary}


@app.post("/api/patients/{patient_id}/unlock")
def patient_unlock(patient_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Valida o PIN do paciente para liberar o prontuário."""
    pin = str(payload.get("pin") or "").strip()
    if not pin:
        raise HTTPException(status_code=400, detail="PIN é obrigatório")
    ok = db.verify_patient_pin(patient_id, pin)
    if not ok:
        raise HTTPException(status_code=401, detail="PIN incorreto")
    patient = db.get_patient(patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Paciente não encontrado")
    return {"unlocked": True, "patient": _apply_name_privacy(patient, reveal=True)}


@app.post("/api/patients/{patient_id}/lock")
def patient_lock(patient_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Define ou remove o PIN individual do paciente."""
    if db.get_patient(patient_id) is None:
        raise HTTPException(status_code=404, detail="Paciente não encontrado.")
    pin = str(payload.get("pin") or "").strip()
    if pin and not auth.PIN_RE.match(pin):
        raise HTTPException(
            status_code=400, detail="O PIN deve ter de 4 a 6 dígitos numéricos."
        )
    db.set_patient_pin(patient_id, pin or None)
    patient = db.get_patient(patient_id)
    return {
        "patient_id": patient_id,
        "locked": bool(pin),
        "patient": _apply_name_privacy(patient, reveal=False) if patient else None,
    }


@app.post("/api/patients/{patient_id}/reset-pin")
def patient_reset_pin(
    patient_id: str, payload: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Redefine/remove o PIN do prontuário confirmando a senha mestra.

    Usado quando o profissional esquece o PIN individual do paciente mas
    ainda tem acesso à estação.
    """
    _require_station_access(request)
    try:
        return auth.reset_patient_pin(
            patient_id,
            str(payload.get("master_password") or ""),
            new_pin=str(payload.get("new_pin") or ""),
            remove_pin=bool(payload.get("remove_pin")),
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Dados pessoais do paciente (protegidos pela senha mestra da estação)
# ---------------------------------------------------------------------------
def _require_master_password(payload: dict[str, Any]) -> None:
    """Valida a senha mestra do(a) profissional; 401 quando incorreta."""
    password = str(payload.get("master_password") or "")
    if not password or not auth.verify_access(password):
        raise HTTPException(
            status_code=401,
            detail="Senha de acesso incorreta.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _resolve_personal_updates(payload: dict[str, Any]) -> dict[str, Any]:
    """Normaliza e valida os campos editáveis de dados pessoais."""
    updates: dict[str, Any] = {}
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="O nome do paciente é obrigatório.")
        if len(name) > 120:
            raise HTTPException(status_code=400, detail="O nome excede 120 caracteres.")
        updates["name"] = name
    if "notes" in payload:
        notes = str(payload.get("notes") or "").strip()
        if len(notes) > 2000:
            raise HTTPException(
                status_code=400, detail="As observações excedem 2000 caracteres."
            )
        updates["notes"] = notes
    phone_value = payload.get("phone", payload.get("contact"))
    if phone_value is not None:
        phone = str(phone_value or "").strip()
        if len(phone) > 40:
            raise HTTPException(status_code=400, detail="O contato excede 40 caracteres.")
        updates["phone"] = phone
    if "birth_date" in payload:
        updates["birth_date"] = _normalize_birth_date(payload.get("birth_date"))
    elif payload.get("age") not in (None, ""):
        try:
            age = int(payload.get("age"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="Idade inválida. Informe um número inteiro."
            ) from exc
        if age < 0 or age > 130:
            raise HTTPException(status_code=400, detail="Idade fora do intervalo aceito.")
        approximate_birth = date(date.today().year - age, 1, 1)
        updates["birth_date"] = _normalize_birth_date(approximate_birth.isoformat())
    return updates


@app.post("/api/patients/{patient_id}/personal-data/unlock")
def patient_personal_data_unlock(
    patient_id: str, payload: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Libera os dados pessoais completos após confirmar a senha mestra."""
    _require_station_access(request)
    _require_master_password(payload)
    patient = db.get_patient(patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Paciente não encontrado")
    return {
        "patient": _personal_data_payload(patient),
        "personal_data": _personal_data_payload(patient),
    }


@app.put("/api/patients/{patient_id}/personal-data")
def patient_personal_data_update(
    patient_id: str, payload: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Atualiza e re-cifra os dados pessoais (exige a senha mestra)."""
    _require_station_access(request)
    _require_master_password(payload)
    updates = _resolve_personal_updates(payload)
    if not updates:
        raise HTTPException(
            status_code=400, detail="Nenhum campo de dados pessoais foi informado."
        )
    patient = db.update_patient_personal_data(patient_id, updates)
    if patient is None:
        raise HTTPException(status_code=404, detail="Paciente não encontrado")
    return {
        "updated": True,
        "patient": _personal_data_payload(patient),
        "personal_data": _personal_data_payload(patient),
        "summary": _apply_name_privacy(dict(patient), reveal=False),
        "updated_at": patient.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# Laudo clínico em PDF
# ---------------------------------------------------------------------------
def _station_token(request: Request) -> str:
    """Token de acesso da estação (header Bearer, X-Access-Token ou query)."""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (
        (request.headers.get("x-access-token") or "").strip()
        or (request.query_params.get("access_token") or "").strip()
    )


def _require_station_access(request: Request) -> None:
    """Exige o token da estação quando a senha de acesso já foi configurada."""
    if auth._access_hash() is None:  # pragma: no cover - estação sem senha
        return
    expected = db.get_access_token()
    provided = _station_token(request)
    if not expected or not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="Estação não autenticada. Faça login para exportar o laudo.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _patient_pin_from_request(request: Request) -> str:
    return (
        (request.headers.get("x-patient-pin") or "").strip()
        or (request.query_params.get("pin") or "").strip()
    )


_PROFILE_MAX_LENGTHS = {
    "professional_name": 120,
    "professional_registry": 80,
    "institution": 120,
}


@app.get("/api/profile")
def get_profile(request: Request) -> dict[str, str]:
    """Perfil do profissional/pesquisador responsável pela estação."""
    _require_station_access(request)
    return db.get_profile()


@app.put("/api/profile")
def update_profile(payload: dict[str, Any], request: Request) -> dict[str, str]:
    """Atualiza o perfil do profissional (campos omitidos permanecem)."""
    _require_station_access(request)
    values: dict[str, str] = {}
    for field, limit in _PROFILE_MAX_LENGTHS.items():
        if field not in payload:
            continue
        text = str(payload.get(field) or "").strip()
        if len(text) > limit:
            raise HTTPException(
                status_code=400,
                detail=f"O campo {field} excede {limit} caracteres.",
            )
        values[field] = text
    if not values:
        raise HTTPException(status_code=400, detail="Nenhum campo de perfil informado.")
    return db.save_profile(values)


# ---------------------------------------------------------------------------
# Backup e restauração proprietários (.dmbak)
# ---------------------------------------------------------------------------
BACKUP_MEDIA_TYPE = "application/vnd.dermametric.backup"
MAX_BACKUP_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


def _backup_error(exc: Exception) -> HTTPException:
    """Converte falhas do envelope .dmbak em respostas HTTP seguras."""
    if isinstance(exc, backup_mod.BackupPasswordError):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, backup_mod.BackupError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail="Falha ao processar o backup.")


async def _read_backup_upload(upload: UploadFile) -> bytes:
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail="Arquivo de backup vazio.")
    if len(data) > MAX_BACKUP_BYTES:
        raise HTTPException(status_code=413, detail="Arquivo de backup muito grande.")
    return data


@app.post("/api/backup/export")
def backup_export(payload: dict[str, Any], request: Request) -> Response:
    """Exporta um backup completo, individual ou de pacientes selecionados."""
    _require_station_access(request)
    password = str(payload.get("password") or "")
    if not password:
        raise HTTPException(status_code=400, detail="Informe a senha mestra.")
    if not auth.verify_access(password):
        raise HTTPException(status_code=401, detail="Senha mestra incorreta.")

    patient_id: str | None = None
    raw_patient_id = payload.get("patient_id")
    if raw_patient_id not in (None, "", 0):
        patient_id = str(raw_patient_id).strip()
        if not patient_id:
            patient_id = None

    patient_ids: list[str] | None = None
    raw_patient_ids = payload.get("patient_ids")
    if raw_patient_ids is not None:
        if not isinstance(raw_patient_ids, (list, tuple, set)):
            raise HTTPException(
                status_code=400,
                detail="'patient_ids' deve ser uma lista de identificadores de pacientes.",
            )
        patient_ids = []
        for raw in raw_patient_ids:
            value = str(raw or "").strip()
            if value and value not in patient_ids:
                patient_ids.append(value)
        if not patient_ids:
            raise HTTPException(
                status_code=400,
                detail="Selecione ao menos um paciente para exportar.",
            )

    archive_password: str | None = payload.get("archive_password")
    if archive_password is not None:
        archive_password = str(archive_password)
        if not archive_password.strip():
            archive_password = None

    try:
        data, summary = backup_mod.export_backup(
            password,
            patient_id=patient_id,
            patient_ids=patient_ids,
            archive_password=archive_password,
        )
    except Exception as exc:  # noqa: BLE001 - mapeado para HTTP
        raise _backup_error(exc) from exc

    filename = backup_mod.backup_filename(summary)
    return Response(
        content=data,
        media_type=BACKUP_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/backup/inspect")
async def backup_inspect(
    request: Request,
    file: UploadFile = File(...),
    password: str = Form(...),
) -> dict[str, Any]:
    """Valida e resume um ``.dmbak`` sem alterar o banco de dados."""
    _require_station_access(request)
    if not password:
        raise HTTPException(status_code=400, detail="Informe a senha mestra do arquivo.")
    data = await _read_backup_upload(file)
    try:
        return backup_mod.inspect_backup(data, password)
    except Exception as exc:  # noqa: BLE001 - mapeado para HTTP
        raise _backup_error(exc) from exc


@app.post("/api/backup/restore")
async def backup_restore(
    request: Request,
    file: UploadFile = File(...),
    password: str = Form(...),
    mode: str = Form("merge"),
    import_as_new: bool = Form(False),
) -> dict[str, Any]:
    """Restaura um ``.dmbak`` em modo ``merge`` ou ``replace``.

    ``import_as_new`` só tem efeito em ``merge`` de caso único: se o código de
    origem já existir na estação, cria um prontuário novo (``PAC-XXXXXX``) em
    vez de mesclar no prontuário local.

    Um snapshot completo do estado atual é gravado automaticamente em
    ``data/backups/pre_restore_*.dmbak`` antes de qualquer escrita.
    """
    _require_station_access(request)
    if not password:
        raise HTTPException(status_code=400, detail="Informe a senha mestra do arquivo.")
    if mode not in {"merge", "replace"}:
        raise HTTPException(
            status_code=400, detail="Modo inválido: use 'merge' ou 'replace'."
        )
    data = await _read_backup_upload(file)
    try:
        return backup_mod.restore_backup(
            data, password, mode=mode, import_as_new=import_as_new
        )
    except Exception as exc:  # noqa: BLE001 - mapeado para HTTP
        raise _backup_error(exc) from exc



def _report_images(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Imagens de exibição (com marca d'água) das sessões, para o laudo."""
    images: list[dict[str, Any]] = []
    for session in sessions:
        field = None
        key = None
        if session.get("aligned_image_path"):
            field, key = "aligned_image_path", "aligned_png"
        elif session.get("enhanced_image_path"):
            field, key = "enhanced_image_path", "enhanced_png"
        if not field:
            continue
        path = db.storage_absolute(session.get(field))
        if path is None or not path.exists():
            continue
        try:
            data = security.read_encrypted_file(path)
        except Exception:  # noqa: BLE001 - arquivo ilegível não derruba o laudo
            continue
        logical = Path(str(session.get(field))).name
        if logical.endswith(".enc"):
            logical = logical[:-4]
        display = _watermarked_display_bytes(data, session.get("id"), logical)
        images.append(
            {
                "session_code": session.get("code") or session.get("id"),
                "captured_at": session.get("captured_at"),
                "px_per_mm": session.get("px_per_mm"),
                key: display,
            }
        )
    return images


@app.get("/api/patients/{patient_id}/report.pdf")
def patient_report(
    patient_id: str, request: Request, include_name: bool = False
) -> Response:
    """Laudo longitudinal completo do paciente em PDF (autenticado).

    O laudo é **sigiloso por padrão**: identifica o paciente apenas pelo
    código ``PAC-...`` e pelas iniciais (``masked_name``). O nome completo só é
    incluído quando ``include_name=true`` é enviado explicitamente por um
    profissional com o prontuário desbloqueado (PIN válido, quando houver).
    """
    _require_station_access(request)
    patient = db.get_patient(patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Paciente não encontrado")
    if patient.get("has_pin"):
        pin = _patient_pin_from_request(request)
        if not pin or not db.verify_patient_pin(patient_id, pin):
            raise HTTPException(
                status_code=401,
                detail="Prontuário protegido por PIN. Informe o PIN para exportar o laudo.",
            )

    sessions = patient.get("sessions") or []
    report_patient = dict(patient)
    report_patient["masked_name"] = mask_patient_name(patient.get("name") or "")
    if include_name:
        report_patient["name"] = patient.get("name") or ""
        report_patient["anonymized"] = False
    else:
        # Nunca propaga o nome completo (nem observações que possam identificá-lo).
        report_patient["name"] = None
        report_patient["notes"] = ""
        report_patient["anonymized"] = True
    comparison = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "images": _report_images(sessions),
        "include_name": bool(include_name),
    }
    pdf_bytes = _vision_call(
        report.build_patient_pdf_report,
        report_patient,
        sessions,
        comparison,
        db.get_profile(),
    )
    filename = report.report_filename(patient)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/", include_in_schema=False)
def index():
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse(
        "<h1>DermaMetric</h1><p>API ativa. Consulte <a href='/docs'>/docs</a>.</p>"
    )
