"""Criptografia em repouso do DermaMetric (LGPD).

Usa ``cryptography.fernet.Fernet`` (AES-128-CBC + HMAC-SHA256, chave de 256
bits). A chave mestra é lida de ``DERMAMETRIC_MASTER_KEY`` ou gerada uma única
vez em ``storage/.secret.key`` com permissão ``0600``.

Campos de texto do SQLite são gravados com o prefixo ``enc:v1:``. Valores
legados sem o prefixo continuam legíveis (migração transparente). Imagens são
gravadas cifradas em ``uploads/*.enc`` e ``processed/*.enc`` e servidas
descriptografadas pela API.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

try:  # importação como pacote
    from . import paths as _paths
except ImportError:  # pragma: no cover - execução direta de dentro de backend/
    import paths as _paths  # type: ignore[no-redef]

BASE_DIR = _paths.ASSETS_DIR
STORAGE_DIR = _paths.STORAGE_DIR
KEY_PATH = _paths.KEY_PATH

PREFIX = "enc:v1:"

_fernet: Fernet | None = None
_cached_key: bytes | None = None


def _normalize_key(raw: bytes | str) -> bytes:
    """Aceita chave Fernet válida ou deriva uma chave estável a partir de texto."""
    if isinstance(raw, str):
        raw = raw.strip().encode("utf-8")
    raw = raw.strip()
    try:
        candidate = base64.urlsafe_b64decode(raw)
        if len(candidate) == 32:
            return raw
    except Exception:  # noqa: BLE001 - entrada arbitrária do ambiente
        pass
    digest = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(digest)


def _load_key() -> bytes:
    env_value = os.environ.get("DERMAMETRIC_MASTER_KEY")
    if env_value:
        return _normalize_key(env_value)

    if KEY_PATH.exists():
        key = KEY_PATH.read_bytes().strip()
        if key:
            return _normalize_key(key)

    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(str(KEY_PATH), flags, 0o600)
    try:
        os.write(descriptor, key)
    finally:
        os.close(descriptor)
    try:
        os.chmod(KEY_PATH, 0o600)
    except OSError:  # pragma: no cover - sistemas sem suporte a chmod
        pass
    return key


def get_fernet() -> Fernet:
    global _fernet, _cached_key
    if _fernet is None:
        _cached_key = _load_key()
        _fernet = Fernet(_cached_key)
    return _fernet


def dek_bytes() -> bytes:
    """Chave Fernet efetiva (DEK) usada para cifrar dados e imagens."""
    return _cached_key if _cached_key is not None else _load_key()


def adopt_dek(raw_key: bytes | str) -> str:
    """Passa a usar em memória uma DEK desbloqueada por outro segredo.

    Usado após a recuperação por chave: a DEK obtida do wrapper substitui a
    chave em cache, garantindo que a sessão leia exatamente os dados
    cifrados com aquela chave. Devolve o fingerprint curto da nova chave.
    """
    global _fernet, _cached_key
    key = _normalize_key(raw_key)
    _cached_key = key
    _fernet = Fernet(key)
    return hashlib.sha256(key).hexdigest()[:12]


def key_fingerprint() -> str:
    """Identificador curto e não reversível da chave ativa (para auditoria)."""
    key = _cached_key if _cached_key is not None else _load_key()
    return hashlib.sha256(key).hexdigest()[:12]


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    if is_encrypted(text):
        return text
    token = get_fernet().encrypt(text.encode("utf-8")).decode("ascii")
    return f"{PREFIX}{token}"


def decrypt_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not is_encrypted(text):
        return text
    try:
        return get_fernet().decrypt(text[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # Chave trocada ou dado corrompido: mantém o valor bruto em vez de
        # derrubar a sessão inteira. A interface sinaliza o campo ilegível.
        return text


def encrypt_bytes(data: bytes) -> bytes:
    return get_fernet().encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    return get_fernet().decrypt(data)


def encrypt_file(source: Path | str, destination: Path | str) -> Path:
    """Cifra ``source`` em ``destination`` (adiciona ``.enc`` se ausente)."""
    source_path = Path(source)
    destination_path = Path(destination)
    if destination_path.suffix != ".enc":
        destination_path = destination_path.with_suffix(destination_path.suffix + ".enc")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_bytes(encrypt_bytes(source_path.read_bytes()))
    return destination_path


def read_encrypted_file(path: Path | str) -> bytes:
    """Lê um arquivo cifrado ou legado em texto puro."""
    data = Path(path).read_bytes()
    if str(path).endswith(".enc"):
        return decrypt_bytes(data)
    return data
