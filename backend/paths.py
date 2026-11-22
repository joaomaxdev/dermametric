"""Resolução central de caminhos do DermaMetric (dev e empacotado).

Quando o aplicativo roda a partir do PyInstaller (``sys.frozen``), os arquivos
estáticos (``frontend/``, ``favicon.svg``) ficam dentro de ``sys._MEIPASS``
(pasta temporária e somente-leitura) e os dados persistentes precisam ir para o
perfil do usuário. Em desenvolvimento mantemos o comportamento histórico do
repositório (``./data`` e ``./storage``), preservando os testes.

Variáveis de ambiente respeitadas (úteis para testes e instalações portáteis):

* ``DERMAMETRIC_DATA_DIR`` — raiz dos dados persistentes;
* ``DERMAMETRIC_DB_PATH`` — caminho explícito do SQLite;
* ``DERMAMETRIC_STORAGE_DIR`` — raiz das imagens/arquivo-chave.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "DermaMetric"
LINUX_DIR_NAME = "dermametric"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _expand(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


#: ``True`` quando executado a partir do bundle do PyInstaller.
FROZEN = _is_frozen()

#: Raiz do código-fonte (pai de ``backend/``), usada apenas em desenvolvimento.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Onde ficam os assets somente-leitura (``frontend/``, ``favicon.svg``).
ASSETS_DIR = _expand(getattr(sys, "_MEIPASS", REPO_ROOT)) if FROZEN else REPO_ROOT

#: Compatibilidade com o nome antigo usado por ``backend.main``.
BASE_DIR = ASSETS_DIR


def _default_user_data_dir() -> Path:
    """Diretório persistente por usuário quando não há override explícito."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / APP_DIR_NAME / "data"
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / LINUX_DIR_NAME / "data"


def _resolve_data_dir() -> Path:
    override = os.environ.get("DERMAMETRIC_DATA_DIR")
    if override:
        return _expand(override)
    if FROZEN:
        return _expand(_default_user_data_dir())
    return _expand(REPO_ROOT / "data")


def _resolve_storage_dir(data_dir: Path) -> Path:
    override = os.environ.get("DERMAMETRIC_STORAGE_DIR")
    if override:
        return _expand(override)
    if FROZEN:
        # O bundle é somente-leitura: imagens e chave mestra vão para os dados.
        return _expand(data_dir / "storage")
    return _expand(REPO_ROOT / "storage")


def _resolve_db_path(data_dir: Path) -> Path:
    override = os.environ.get("DERMAMETRIC_DB_PATH")
    if override:
        return _expand(override)
    return _expand(data_dir / "dermametric.db")


#: Raiz dos dados persistentes (SQLite, snapshots de backup).
DATA_DIR = _resolve_data_dir()

#: Raiz das imagens cifradas e da chave mestra local.
STORAGE_DIR = _resolve_storage_dir(DATA_DIR)

#: Caminho do banco SQLite.
DB_PATH = _resolve_db_path(DATA_DIR)

#: Subdiretórios derivados.
UPLOAD_DIR = STORAGE_DIR / "uploads"
PROCESSED_DIR = STORAGE_DIR / "processed"
WORK_DIR = STORAGE_DIR / "work"
BACKUP_DIR = DATA_DIR / "backups"
KEY_PATH = STORAGE_DIR / ".secret.key"


def ensure_directories() -> None:
    """Cria a árvore de diretórios graváveis (idempotente)."""
    for directory in (
        DATA_DIR,
        STORAGE_DIR,
        UPLOAD_DIR,
        PROCESSED_DIR,
        WORK_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


__all__ = [
    "APP_DIR_NAME",
    "ASSETS_DIR",
    "BACKUP_DIR",
    "BASE_DIR",
    "DATA_DIR",
    "DB_PATH",
    "FROZEN",
    "KEY_PATH",
    "PROCESSED_DIR",
    "REPO_ROOT",
    "STORAGE_DIR",
    "UPLOAD_DIR",
    "WORK_DIR",
    "ensure_directories",
]
