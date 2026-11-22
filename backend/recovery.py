"""Chave de Recuperação e arquivo-chave ``.dmkey`` do DermaMetric.

A chave efetiva de cifragem dos dados (DEK, uma chave Fernet) é embrulhada
(``wrap``) de duas formas independentes e guardada na tabela ``settings``:

* ``dermametric.crypto.dek_password`` — DEK cifrada com uma chave derivada
  da senha mestra (PBKDF2-SHA256 + salt aleatório);
* ``dermametric.crypto.dek_recovery`` — DEK cifrada com uma chave derivada
  da Chave de Recuperação (``DMK-XXXX-XXXX-XXXX-XXXX``).

Assim, esquecer a senha não torna os prontuários irrecuperáveis: o arquivo
``.dmkey`` (ou o código digitado) abre a mesma DEK. Nem a senha nem a chave
de recuperação são persistidas em texto puro — apenas os wrappers e o
identificador curto da chave ativa.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import database as db
from . import security

APP_NAME = "DermaMetric"
KEY_FILE_TYPE = "recovery_key"
KEY_FILE_VERSION = 1

RECOVERY_PREFIX = "DMK"
RECOVERY_GROUPS = 4
RECOVERY_GROUP_SIZE = 4
# Alfabeto sem caracteres ambíguos (0/O, 1/I/L) para transcrição manual.
RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

KDF_NAME = "pbkdf2_sha256"
KDF_ITERATIONS = 200_000

PASSWORD_WRAPPER_KEY = "dermametric.crypto.dek_password"
RECOVERY_WRAPPER_KEY = "dermametric.crypto.dek_recovery"
RECOVERY_KEY_ID_KEY = "dermametric.crypto.recovery_key_id"
RECOVERY_CREATED_KEY = "dermametric.crypto.recovery_created_at"

_RECOVERY_RE = re.compile(
    rf"^{RECOVERY_PREFIX}(?:-[{RECOVERY_ALPHABET}]{{{RECOVERY_GROUP_SIZE}}})"
    rf"{{{RECOVERY_GROUPS}}}$"
)


# ---------------------------------------------------------------------------
# Chave de recuperação (formato e identificador)
# ---------------------------------------------------------------------------
def generate_recovery_key() -> str:
    """Gera uma Chave de Recuperação legível (80 bits de entropia)."""
    groups = [
        "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_GROUP_SIZE))
        for _ in range(RECOVERY_GROUPS)
    ]
    return "-".join([RECOVERY_PREFIX, *groups])


def normalize_recovery_key(value: str) -> str:
    """Normaliza ``dmk xxxx...``/``DMKXXXXXXXX`` para o formato canônico.

    Levanta ``ValueError`` quando o formato não corresponde a uma chave
    DMK válida, evitando gastar PBKDF2 em entradas inválidas.
    """
    raw = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if raw.startswith(RECOVERY_PREFIX):
        raw = raw[len(RECOVERY_PREFIX):]
    body = raw
    if len(body) != RECOVERY_GROUPS * RECOVERY_GROUP_SIZE:
        raise ValueError("Chave de recuperação incompleta. Use o formato DMK-XXXX-XXXX-XXXX-XXXX.")
    if any(char not in RECOVERY_ALPHABET for char in body):
        raise ValueError("Chave de recuperação inválida. Verifique os caracteres digitados.")
    groups = [
        body[index : index + RECOVERY_GROUP_SIZE]
        for index in range(0, len(body), RECOVERY_GROUP_SIZE)
    ]
    return "-".join([RECOVERY_PREFIX, *groups])


def key_id_for(recovery_key: str) -> str:
    """Identificador curto, estável e não reversível da chave."""
    normalized = normalize_recovery_key(recovery_key)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:12].upper()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Derivação e wrappers da DEK
# ---------------------------------------------------------------------------
def _derive_key(secret_text: str, salt_hex: str, iterations: int) -> bytes:
    digest = hashlib.pbkdf2_hmac(
        "sha256", secret_text.encode("utf-8"), bytes.fromhex(salt_hex), iterations
    )
    return base64.urlsafe_b64encode(digest)


def _wrap_dek(dek: bytes, secret_text: str) -> dict[str, Any]:
    salt = secrets.token_hex(16)
    derived = _derive_key(secret_text, salt, KDF_ITERATIONS)
    wrapped = Fernet(derived).encrypt(dek)
    return {
        "kdf": KDF_NAME,
        "iterations": KDF_ITERATIONS,
        "salt": salt,
        "wrapped": wrapped.decode("ascii"),
    }


def _unwrap_dek(wrapper: dict[str, Any], secret_text: str) -> bytes:
    try:
        derived = _derive_key(
            secret_text,
            str(wrapper["salt"]),
            int(wrapper.get("iterations") or KDF_ITERATIONS),
        )
        return Fernet(derived).decrypt(str(wrapper["wrapped"]).encode("ascii"))
    except (KeyError, ValueError, TypeError, InvalidToken) as exc:
        raise ValueError("Chave de recuperação ou senha inválida.") from exc


def _load_wrapper(key: str) -> dict[str, Any] | None:
    raw = db.get_setting(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("wrapped") else None


def _save_wrapper(key: str, wrapper: dict[str, Any]) -> None:
    db.save_setting(key, json.dumps(wrapper, separators=(",", ":")))


def has_password_wrapper() -> bool:
    return _load_wrapper(PASSWORD_WRAPPER_KEY) is not None


def has_recovery_wrapper() -> bool:
    return _load_wrapper(RECOVERY_WRAPPER_KEY) is not None


def wrap_password(password: str) -> None:
    """(Re)embrulha a DEK ativa com uma chave derivada da senha mestra."""
    _save_wrapper(PASSWORD_WRAPPER_KEY, _wrap_dek(security.dek_bytes(), password))


def _create_recovery_wrapper() -> tuple[str, str, str]:
    """Gera nova chave de recuperação e substitui o wrapper ativo."""
    recovery_key = generate_recovery_key()
    wrapped = _wrap_dek(security.dek_bytes(), recovery_key)
    key_id = key_id_for(recovery_key)
    created_at = utc_now_iso()
    wrapped["key_id"] = key_id
    wrapped["created_at"] = created_at
    _save_wrapper(RECOVERY_WRAPPER_KEY, wrapped)
    db.save_setting(RECOVERY_KEY_ID_KEY, key_id)
    db.save_setting(RECOVERY_CREATED_KEY, created_at)
    return recovery_key, key_id, created_at


def build_key_file(
    recovery_key: str,
    key_id: str,
    created_at: str,
    institution: str | None = None,
) -> dict[str, Any]:
    """Payload padronizado do arquivo ``.dmkey`` (versão 1)."""
    return {
        "app": APP_NAME,
        "type": KEY_FILE_TYPE,
        "version": KEY_FILE_VERSION,
        "key_id": key_id,
        "created_at": created_at,
        "institution": str(institution or ""),
        "recovery_key": recovery_key,
    }


def create_recovery_key(institution: str | None = None) -> dict[str, Any]:
    """Gera/rotaciona a chave de recuperação e devolve chave + arquivo."""
    recovery_key, key_id, created_at = _create_recovery_wrapper()
    return {
        "recovery_key": recovery_key,
        "key_id": key_id,
        "created_at": created_at,
        "key_file": build_key_file(recovery_key, key_id, created_at, institution),
    }


def ensure_initialized(password: str, institution: str | None = None) -> dict[str, Any] | None:
    """Garante os wrappers (retrocompatível com bancos antigos).

    Se o banco já existia sem wrapper de recuperação, cria um neste login e
    devolve o payload para download imediato do ``.dmkey``.
    """
    if not has_password_wrapper():
        wrap_password(password)
    if not has_recovery_wrapper():
        return create_recovery_key(institution)
    return None


def unwrap_dek_with_recovery(recovery_key: str) -> bytes:
    """Valida a chave de recuperação contra o wrapper ativo e devolve a DEK."""
    wrapper = _load_wrapper(RECOVERY_WRAPPER_KEY)
    if wrapper is None:
        raise ValueError(
            "A recuperação por arquivo-chave ainda não está configurada nesta estação."
        )
    normalized = normalize_recovery_key(recovery_key)
    expected_id = db.get_setting(RECOVERY_KEY_ID_KEY) or wrapper.get("key_id")
    if expected_id and key_id_for(normalized) != expected_id:
        raise ValueError(
            "Esta chave de recuperação não pertence a esta estação "
            "ou já foi substituída por uma mais recente."
        )
    dek = _unwrap_dek(wrapper, normalized)
    # Confere que o wrapper corresponde mesmo à chave Fernet ativa.
    if not secrets.compare_digest(dek, security.dek_bytes()):
        security.adopt_dek(dek)
    return dek


def status() -> dict[str, Any]:
    """Metadados públicos e não sensíveis da recuperação."""
    configured = has_recovery_wrapper()
    return {
        "configured": configured,
        "key_id": db.get_setting(RECOVERY_KEY_ID_KEY) if configured else None,
        "created_at": db.get_setting(RECOVERY_CREATED_KEY) if configured else None,
        "format": "DMK-XXXX-XXXX-XXXX-XXXX",
    }
