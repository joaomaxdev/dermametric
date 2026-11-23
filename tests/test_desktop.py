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


def test_server_starts_without_std_streams(monkeypatch):
    """Simula PyInstaller --windowed: sys.stdout/stderr = None.

    O Uvicorn não pode tentar ``sys.stderr.isatty()`` nem reconfigurar o
    formatador padrão (AttributeError/ValueError).
    """
    from fastapi import FastAPI

    import desktop_launcher as launcher

    app = FastAPI()

    @app.get("/api/health")
    def _health() -> dict[str, str]:
        return {"status": "ok"}

    port = launcher.find_free_port()
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    server = launcher.LocalServer(app, port=port)
    server.start()
    try:
        health = server.wait_until_ready(timeout=30)
        assert health == {"status": "ok"}
        # O guard instalou streams graváveis (devnull/log) no lugar dos None.
        assert sys.stdout is not None
        assert sys.stderr is not None
        assert hasattr(sys.stdout, "isatty")
        assert hasattr(sys.stderr, "isatty")
    finally:
        server.stop()
        launcher._stream_fallback = None
        launcher._missing_streams.clear()



def test_desktop_launch_token_blocks_external_browser_and_restricts_cors():
    import urllib.error
    import urllib.request
    from http.cookiejar import CookieJar
    from backend.main import app
    from desktop_launcher import LocalServer, find_free_port

    token = "tok-secreto-janela-nativa-123"
    os.environ["DERMAMETRIC_LAUNCH_TOKEN"] = token
    port = find_free_port("127.0.0.1", 0)
    server = LocalServer(app, host="127.0.0.1", port=port).start()
    try:
        server.wait_until_ready(timeout=20.0)

        # 1. Acesso direto sem token (ex.: navegador aberto manualmente) deve receber 403
        try:
            urllib.request.urlopen(f"{server.base_url}/api/auth/status", timeout=5.0)
            raise AssertionError("Era esperado 403 sem launch_token")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403

        # 2. CORS não permite origem externa
        req = urllib.request.Request(
            f"{server.base_url}/api/health",
            headers={"Origin": "https://evil.example.com"},
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            assert resp.status == 200
            assert resp.headers.get("access-control-allow-origin") is None

        # 3. Janela nativa entra com ?launch_token=..., recebe cookie HttpOnly e navega na API
        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        with opener.open(f"{server.base_url}/?launch_token={token}", timeout=5.0) as resp:
            assert resp.status == 200
            set_cookie = resp.headers.get("Set-Cookie") or ""
            assert "dm_desktop_session=" in set_cookie
            assert "HttpOnly" in set_cookie

        with opener.open(f"{server.base_url}/api/auth/status", timeout=5.0) as resp:
            assert resp.status == 200
    finally:
        os.environ.pop("DERMAMETRIC_LAUNCH_TOKEN", None)
        server.stop()
