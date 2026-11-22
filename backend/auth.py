"""Autenticação do DermaMetric (login de acesso e proteção por PIN).

Armazena o hash da senha de acesso e o hash do PIN por paciente. Todos os
valores são persistidos com hash SHA-256 + salt (via hashlib pbkdf2) — não há
texto puro em disco. A sessão de acesso em memória carrega o token atual.

A senha mestra também embrulha a DEK da estação. A mesma DEK pode ser
desbloqueada por uma Chave de Recuperação (``DMK-XXXX-XXXX-XXXX-XXXX``)
guardada no arquivo ``.dmkey``, permitindo redefinir a senha sem perder os
prontuários (ver ``backend/recovery.py``).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from typing import Any

from . import database as db
from . import recovery
from . import security


ACCESS_TOKEN_TTL_SECONDS = 12 * 60 * 60  # 12h de sessão de acesso
PIN_TTL_SECONDS = 5 * 60  # 5min para desbloqueio de paciente

ACCESS_HASH_KEY = "dermametric.access.hash"
PIN_HASH_KEY = "dermametric.patients.pin_hash"
ACCESS_SETUP_KEY = "dermametric.access.setup"
PBKDF2_ITERATIONS = 120_000

PIN_RE = re.compile(r"^\d{4,6}$")
MIN_PASSWORD_LENGTH = 6


def _hash(text: str) -> str:
    """Hash PBKDF2-SHA256 com salt aleatório embutido no próprio valor."""
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", text.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def _verify(plain: str, stored: str | None) -> bool:
    if not plain or not stored:
        return False
    expected = str(stored)
    if expected.startswith("pbkdf2_sha256$"):
        try:
            _, iterations, salt, digest = expected.split("$", 3)
            candidate = hashlib.pbkdf2_hmac(
                "sha256",
                plain.encode("utf-8"),
                bytes.fromhex(salt),
                int(iterations),
            ).hex()
        except (ValueError, TypeError):
            return False
        return secrets.compare_digest(candidate, digest)
    if expected.startswith("sha256$"):
        expected = expected[len("sha256$"):]
    legacy = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    return secrets.compare_digest(legacy, expected)


def _access_hash() -> str | None:
    return db.get_setting(ACCESS_HASH_KEY)


def _save_access_hash(value: str) -> None:
    db.save_setting(ACCESS_HASH_KEY, value)


def _pin_hash(patient_id: str) -> str | None:
    return db.get_setting(PIN_HASH_KEY, default=f"pins:{patient_id}")


def _save_pin_hash(patient_id: str, value: str | None) -> None:
    db.save_setting(PIN_HASH_KEY, value, scope=f"pins:{patient_id}")


# ---------------------------------------------------------------------------
# Status / setup
# ---------------------------------------------------------------------------

def _institution() -> str:
    try:
        return str(db.get_profile().get("institution") or "")
    except Exception:  # noqa: BLE001 - perfil é opcional
        return ""


def status() -> dict[str, Any]:
    """Relógio de estado para a tela de bloqueio/login."""
    configured = _access_hash() is not None
    return {
        "configured": bool(configured),
        "access_token": None,
        "access_token_expires_at": None,
        "patient_id": None,
        "patient_status": None,
        "setup_hint": (
            "Defina uma senha de acesso para a estação clínica." if not configured
            else None
        ),
        "recovery": recovery.status(),
    }


def setup(password: str) -> dict[str, Any]:
    """Cria a primeira senha de acesso e a chave de recuperação da estação."""
    if _access_hash() is not None:
        raise ValueError("Já existe uma senha de acesso configurada.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"A senha de acesso precisa ter pelo menos {MIN_PASSWORD_LENGTH} caracteres."
        )
    _save_access_hash(_hash(password))
    recovery.wrap_password(password)
    created = recovery.create_recovery_key(_institution())
    token = create_access_token()
    return {
        "configured": True,
        "access_token": token,
        "access_token_expires_at": _token_expires_at(),
        "recovery_key": created["recovery_key"],
        "key_id": created["key_id"],
        "created_at": created["created_at"],
        "key_file": created["key_file"],
    }


def _new_token() -> str:
    token = secrets.token_urlsafe(32)
    return token


def _token_expires_at() -> str | None:
    return None


def verify_access(password: str) -> bool:
    stored = _access_hash()
    return stored is not None and _verify(password, stored)


def create_access_token() -> str:
    token = _new_token()
    # Persiste em memória para a API responder autenticado na mesma requisição.
    db.set_access_token(token)
    return token


def get_access_token() -> str | None:
    return db.get_access_token()


def get_access_expires_at() -> str | None:
    return db.get_access_expires_at()


def destroy_access_token() -> None:
    db.clear_access_token()


def login(password: str) -> dict[str, Any] | None:
    """Valida a senha, garante os wrappers de recuperação e abre a sessão.

    Em bancos antigos (sem wrapper de recuperação) a chave ``.dmkey`` é
    criada neste primeiro login e devolvida em ``pending_recovery`` para
    download imediato.
    """
    if not verify_access(password):
        return None
    pending = recovery.ensure_initialized(password, _institution())
    token = create_access_token()
    return {
        "token": token,
        "recovery": recovery.status(),
        "pending_recovery": pending,
    }


def change_password(current_password: str, new_password: str) -> dict[str, Any]:
    """Troca a senha mestra mantendo a mesma DEK (dados continuam legíveis)."""
    if _access_hash() is None:
        raise ValueError("A estação ainda não possui senha mestra configurada.")
    if not verify_access(current_password):
        raise ValueError("Senha mestra atual incorreta.")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"A nova senha precisa ter pelo menos {MIN_PASSWORD_LENGTH} caracteres."
        )
    _save_access_hash(_hash(new_password))
    recovery.wrap_password(new_password)
    pending = recovery.ensure_initialized(new_password, _institution())
    return {"changed": True, "recovery": recovery.status(), "pending_recovery": pending}


def regenerate_recovery_key(master_password: str) -> dict[str, Any]:
    """Gera (rotaciona) um novo arquivo-chave ``.dmkey`` para a estação."""
    if not verify_access(master_password):
        raise ValueError("Senha mestra incorreta.")
    created = recovery.create_recovery_key(_institution())
    return {**created, "recovery": recovery.status()}


def recover(recovery_key: str, new_password: str) -> dict[str, Any]:
    """Redefine a senha mestra usando a Chave de Recuperação.

    A chave é validada contra o wrapper ativo da DEK. Em seguida a DEK é
    reembrulhada com a nova senha e uma nova chave de recuperação é gerada
    (rotação automática), invalidando o arquivo ``.dmkey`` anterior.
    """
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"A nova senha precisa ter pelo menos {MIN_PASSWORD_LENGTH} caracteres."
        )
    recovery.unwrap_dek_with_recovery(recovery_key)
    _save_access_hash(_hash(new_password))
    recovery.wrap_password(new_password)
    rotated = recovery.create_recovery_key(_institution())
    token = create_access_token()
    return {
        "token": token,
        **rotated,
        "recovery": recovery.status(),
    }


def reset_patient_pin(
    patient_id: str,
    master_password: str,
    *,
    new_pin: str | None = None,
    remove_pin: bool = False,
) -> dict[str, Any]:
    """Redefine/remove o PIN de um prontuário após confirmar a senha mestra."""
    if not verify_access(master_password):
        raise ValueError("Senha mestra incorreta.")
    patient = db.get_patient(patient_id)
    if patient is None:
        raise LookupError("Paciente não encontrado.")
    if remove_pin:
        db.set_patient_pin(patient_id, None)
        return {"patient_id": patient_id, "locked": False, "patient": db.get_patient(patient_id)}
    pin = str(new_pin or "").strip()
    if not PIN_RE.match(pin):
        raise ValueError("O novo PIN deve ter de 4 a 6 dígitos numéricos.")
    db.set_patient_pin(patient_id, pin)
    return {"patient_id": patient_id, "locked": True, "patient": db.get_patient(patient_id)}


def logout() -> None:
    destroy_access_token()
