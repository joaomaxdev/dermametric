"""Smoke tests do launcher desktop e da resolução central de caminhos."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "DERMAMETRIC_DATA_DIR",
        "DERMAMETRIC_STORAGE_DIR",
        "DERMAMETRIC_DB_PATH",
        "DERMAMETRIC_MASTER_KEY",
    ):
        env.pop(key, None)
    return env


def test_paths_dev_defaults_and_env_override(tmp_path):
    env = _clean_env()
    dev = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path;"
            "from backend import paths;"
            "print(paths.FROZEN, paths.DATA_DIR, paths.STORAGE_DIR, paths.DB_PATH)",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert dev.returncode == 0, dev.stderr
    frozen, data_dir, storage_dir, db_path = dev.stdout.split()
    assert frozen == "False"
    assert Path(data_dir) == ROOT / "data"
    assert Path(storage_dir) == ROOT / "storage"
    assert Path(db_path) == ROOT / "data" / "dermametric.db"

    override = tmp_path / "dados"
    env["DERMAMETRIC_DATA_DIR"] = str(override)
    custom = subprocess.run(
        [
            sys.executable,
            "-c",
            "from backend import paths;"
            "print(paths.DATA_DIR, paths.STORAGE_DIR, paths.DB_PATH)",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert custom.returncode == 0, custom.stderr
    data_dir, storage_dir, db_path = custom.stdout.split()
    assert Path(data_dir) == override.resolve()
    # Em desenvolvimento o storage histórico é preservado mesmo com override.
    assert Path(storage_dir) == ROOT / "storage"
    assert Path(db_path) == override.resolve() / "dermametric.db"


def test_desktop_launcher_smoke_health_and_data_isolation(tmp_path):
    data_dir = tmp_path / "dermametric-data"
    env = _clean_env()
    env["DERMAMETRIC_DATA_DIR"] = str(data_dir)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "desktop_launcher.py"),
            "--smoke-test",
            "--port",
            "0",
            "--timeout",
            "60",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    payload = json.loads([line for line in result.stdout.splitlines() if line.startswith("{")][-1])

    assert payload["ok"] is True
    assert payload["health"]["status"] == "ok"
    assert payload["url"].startswith("http://127.0.0.1:")

    expected = data_dir.resolve()
    assert Path(payload["db"]).resolve().is_relative_to(expected)
    assert Path(payload["health"]["db"]).resolve().is_relative_to(expected)
    assert Path(payload["storage_dir"]).resolve().is_relative_to(expected)
    assert (expected / "dermametric.db").exists()
    assert (expected / "storage" / ".secret.key").exists()
    # Nada foi gravado no storage real do repositório durante o smoke test.
    assert (ROOT / "storage").resolve() != (expected / "storage").resolve()
