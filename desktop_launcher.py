#!/usr/bin/env python3
"""Launcher desktop do DermaMetric (duplo clique, sem terminal).

Fluxo:

1. escolhe uma porta livre em ``127.0.0.1`` (preferindo 8765);
2. sobe o FastAPI/Uvicorn em uma thread daemon;
3. espera ``/api/health`` responder 200;
4. abre a interface em janela nativa com ``pywebview`` (fallback:
   ``webbrowser.open``);
5. encerra o servidor de forma limpa quando a janela é fechada.

Uso normal (duplo clique)::

    python desktop_launcher.py

Uso para automação/CI (não abre janela)::

    DERMAMETRIC_DATA_DIR=/tmp/derma python desktop_launcher.py --smoke-test
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

APP_TITLE = "DermaMetric — Fotogrametria Clínica"
DEFAULT_HOST = "127.0.0.1"
PREFERRED_PORT = 8765
HEALTH_PATH = "/api/health"

# Em desenvolvimento, garante que ``backend`` seja importável a partir da raiz.
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Porta e servidor
# ---------------------------------------------------------------------------
def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(host: str = DEFAULT_HOST, preferred: int = PREFERRED_PORT) -> int:
    """Usa a porta preferida se estiver livre; senão, uma porta efêmera."""
    if preferred and _port_is_free(host, preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


class LocalServer:
    """Uvicorn em thread daemon com espera ativa por ``/api/health``."""

    def __init__(self, app, host: str = DEFAULT_HOST, port: int = PREFERRED_PORT) -> None:
        self.app = app
        self.host = host
        self.port = port
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "LocalServer":
        import uvicorn

        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run,
            name="dermametric-uvicorn",
            daemon=True,
        )
        self._thread.start()
        return self

    def health(self, timeout: float = 1.0) -> dict | None:
        url = f"{self.base_url}{HEALTH_PATH}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                if response.status != 200:
                    return None
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def wait_until_ready(self, timeout: float = 45.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            payload = self.health()
            if payload is not None:
                return payload
            if self._thread is not None and not self._thread.is_alive():
                raise RuntimeError("O servidor DermaMetric encerrou durante a inicialização.")
            time.sleep(0.15)
        raise TimeoutError(
            f"O servidor DermaMetric não respondeu em {timeout:.0f}s "
            f"({self.base_url}{HEALTH_PATH})."
        )

    def stop(self, timeout: float = 10.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Janela nativa
# ---------------------------------------------------------------------------
def open_desktop_window(url: str) -> bool:
    """Abre a janela ``pywebview``; devolve ``False`` se indisponível."""
    try:
        import webview
    except Exception:  # noqa: BLE001 - dependência opcional
        return False

    # Permite baixar PDF, CSV, .dmkey e .dmbak a partir da interface.
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:  # noqa: BLE001 - versões antigas do pywebview
        pass

    try:
        webview.create_window(
            APP_TITLE,
            url,
            width=1360,
            height=920,
            min_size=(1024, 720),
            confirm_close=False,
        )
        webview.start()
        return True
    except Exception:  # noqa: BLE001 - backend gráfico indisponível
        return False
    finally:
        try:
            webview.windows.clear()
        except Exception:  # noqa: BLE001
            pass


def _prepare_data_dir(data_dir: str | None) -> Path:
    """Aplica ``--data-dir`` antes de importar o backend (caminhos congelados)."""
    explicit = data_dir or os.environ.get("DERMAMETRIC_DATA_DIR")
    if not explicit:
        # Sem override: deixe ``backend.paths`` decidir (dev x empacotado).
        from backend import paths

        return paths.DATA_DIR
    target = Path(explicit).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    os.environ["DERMAMETRIC_DATA_DIR"] = str(target)
    if not os.environ.get("DERMAMETRIC_STORAGE_DIR"):
        os.environ["DERMAMETRIC_STORAGE_DIR"] = str(target / "storage")
    if not os.environ.get("DERMAMETRIC_DB_PATH"):
        os.environ["DERMAMETRIC_DB_PATH"] = str(target / "dermametric.db")
    return target


def _load_app():
    from backend.main import app

    return app


def run_smoke_test(data_dir: Path, host: str, port: int, timeout: float) -> int:
    """Sobe, valida saúde e isolamento de dados, e encerra sem abrir janela."""
    server = LocalServer(_load_app(), host=host, port=port).start()
    try:
        health = server.wait_until_ready(timeout=timeout)
        db_path = Path(str(health.get("db") or "")).resolve()
        expected_root = data_dir.resolve()
        if expected_root not in db_path.parents and db_path != expected_root:
            raise RuntimeError(
                "Isolamento de DATA_DIR falhou: "
                f"banco em {db_path}, esperado dentro de {expected_root}."
            )
        storages = {
            "data_dir": str(expected_root),
            "db": str(db_path),
            "storage_dir": os.environ.get("DERMAMETRIC_STORAGE_DIR", ""),
        }
        print(
            json.dumps(
                {
                    "ok": True,
                    "url": server.base_url,
                    "health": health,
                    **storages,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launcher desktop do DermaMetric")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=0, help="0 = escolher automaticamente")
    parser.add_argument("--data-dir", default=None, help="diretório persistente de dados")
    parser.add_argument("--no-window", action="store_true", help="não abre janela (servidor só)")
    parser.add_argument("--smoke-test", action="store_true", help="valida e encerra")
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args(argv)

    data_dir = _prepare_data_dir(args.data_dir)
    port = args.port or find_free_port(args.host)

    if args.smoke_test:
        return run_smoke_test(data_dir, args.host, port, args.timeout)

    server = LocalServer(_load_app(), host=args.host, port=port).start()
    try:
        health = server.wait_until_ready(timeout=args.timeout)
        if not args.no_window:
            if not open_desktop_window(server.base_url):
                webbrowser.open(server.base_url)
                # Sem janela nativa, mantém o serviço vivo até o processo encerrar.
                try:
                    while server._thread is not None and server._thread.is_alive():
                        time.sleep(0.5)
                except KeyboardInterrupt:
                    pass
        else:
            try:
                while server._thread is not None and server._thread.is_alive():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
        return 0 if health else 1
    finally:
        server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
