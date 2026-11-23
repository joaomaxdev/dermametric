"""Verificação e aplicação de atualizações do DermaMetric via GitHub Releases.

Suporta:
- Windows empacotado (PyInstaller + Inno Setup ``DermaMetric-Setup.exe``):
  baixa o instalador oficial, agenda instalação silenciosa após encerrar o
  processo atual e reabre o DermaMetric atualizado preservando ``%LOCALAPPDATA%``.
- Linux AppImage:
  baixa o ``DermaMetric-<arch>.AppImage``, valida o cabeçalho ELF, substitui o
  binário instalado com permissão de execução e reinicia a aplicação.
- Execução via código-fonte (Linux/macOS/Windows em desenvolvimento):
  atualiza o repositório local para a tag da release e reinicia o processo.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from . import paths
    from .version import APP_VERSION, GITHUB_RELEASES_API, GITHUB_RELEASES_URL, GITHUB_REPO
except ImportError:  # pragma: no cover
    import paths  # type: ignore[no-redef]
    from version import (  # type: ignore[no-redef]
        APP_VERSION,
        GITHUB_RELEASES_API,
        GITHUB_RELEASES_URL,
        GITHUB_REPO,
    )

_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

_update_lock = threading.Lock()
_update_state: dict[str, Any] = {
    "status": "idle",  # idle | checking | downloading | installing | restarting | error
    "progress": 0,
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "message": "",
    "target_version": None,
    "error": None,
}


def parse_version(raw: str | None) -> tuple[int, int, int]:
    """Extrai ``(major, minor, patch)`` de strings como ``'v1.0.4'`` ou ``'1.0.4'``."""
    match = _SEMVER_RE.search(str(raw or ""))
    if not match:
        return (0, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_newer_version(latest: str | None, current: str = APP_VERSION) -> bool:
    """Retorna ``True`` quando ``latest`` é estritamente maior que ``current``."""
    latest_tuple = parse_version(latest)
    current_tuple = parse_version(current)
    if latest_tuple == (0, 0, 0):
        return False
    return latest_tuple > current_tuple


def detect_install_mode() -> dict[str, Any]:
    """Identifica o sistema operacional e o modo de empacotamento em execução."""
    plat = sys.platform
    arch = platform.machine() or "x86_64"
    appimage_path = os.environ.get("APPIMAGE") or ""
    if plat.startswith("win"):
        mode = "windows-installer" if paths.FROZEN else "source"
        target_path = sys.executable
    elif plat.startswith("linux"):
        if appimage_path:
            mode = "linux-appimage"
            target_path = appimage_path
        elif paths.FROZEN:
            mode = "linux-bundle"
            target_path = sys.executable
        else:
            mode = "source"
            target_path = str(paths.REPO_ROOT)
    else:
        mode = "frozen" if paths.FROZEN else "source"
        target_path = sys.executable if paths.FROZEN else str(paths.REPO_ROOT)

    return {
        "platform": plat,
        "arch": arch,
        "frozen": paths.FROZEN,
        "mode": mode,
        "target_path": target_path,
    }


def _select_release_asset(assets: list[dict[str, Any]], info: dict[str, Any]) -> dict[str, Any] | None:
    """Escolhe o artefato mais adequado da release para o SO/arquitetura atual."""
    plat = info["platform"]
    arch = str(info["arch"]).lower()
    if not isinstance(assets, list):
        return None

    if plat.startswith("win"):
        for asset in assets:
            name = str(asset.get("name") or "")
            if name.lower().endswith(".exe") and "setup" in name.lower():
                return asset
        for asset in assets:
            name = str(asset.get("name") or "")
            if name.lower().endswith(".exe"):
                return asset
        return None

    if plat.startswith("linux"):
        # Prioriza AppImage compatível com a arquitetura
        arch_aliases = {"x86_64", "amd64"} if arch in {"x86_64", "amd64"} else {arch}
        for asset in assets:
            name = str(asset.get("name") or "")
            lower = name.lower()
            if lower.endswith(".appimage") and any(alias in lower for alias in arch_aliases):
                return asset
        for asset in assets:
            name = str(asset.get("name") or "")
            if name.lower().endswith(".appimage"):
                return asset
        for asset in assets:
            name = str(asset.get("name") or "")
            if name.lower().endswith(".tar.gz"):
                return asset
    return None


def _create_ssl_context() -> ssl.SSLContext | None:
    """Contexto SSL com as CAs do ``certifi`` (necessário no PyInstaller/Win).

    Sem ``certifi`` (execução via código-fonte ou bundle incompleto) cai para
    o contexto padrão do sistema e, em último caso, para ``None`` (o
    ``urlopen`` usa então o contexto global do Python).
    """
    try:
        import certifi  # type: ignore[import-not-found]

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - pacote/cafile ausente no bundle
        try:
            return ssl.create_default_context()
        except Exception:  # noqa: BLE001 - plataforma sem suporte a SSL
            return None


def _urlopen(request: urllib.request.Request, timeout: float):
    """Abre a requisição usando o contexto SSL com CA do ``certifi``."""
    context = _create_ssl_context()
    if context is None:
        return urllib.request.urlopen(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def _network_error_message(exc: BaseException, action: str) -> str:
    """Traduz a falha real (SSL, DNS, timeout ou HTTP) sem mascarar a causa."""
    reason = getattr(exc, "reason", None)
    cause = reason if reason is not None else exc
    text = str(cause) or cause.__class__.__name__

    if isinstance(cause, ssl.SSLCertVerificationError) or "certificate verify failed" in text.lower():
        return (
            f"Falha na verificação do certificado SSL ao {action}: {text}. "
            "Verifique se o pacote 'certifi' está instalado e empacotado "
            "(PyInstaller: --collect-data certifi)."
        )
    if isinstance(cause, ssl.SSLError):
        return f"Erro SSL ao {action}: {text}."
    if isinstance(cause, socket.gaierror):
        return f"Falha de DNS ao {action}: {text}."
    if isinstance(cause, (TimeoutError, socket.timeout)):
        return f"Tempo esgotado ao {action}: {text}."
    return f"Falha de rede ao {action}: {text}."


def _github_get_json(url: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"DermaMetric/{APP_VERSION}",
        },
    )
    with _urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_for_updates(timeout: float = 10.0) -> dict[str, Any]:
    """Consulta a última release no GitHub e compara com ``APP_VERSION``."""
    info = detect_install_mode()
    release: dict[str, Any] | None = None
    try:
        release = _github_get_json(f"{GITHUB_RELEASES_API}/latest", timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            try:
                releases = _github_get_json(
                    f"{GITHUB_RELEASES_API}?per_page=5", timeout=timeout
                )
            except urllib.error.HTTPError as list_exc:
                raise RuntimeError(
                    f"GitHub recusou a listagem de releases "
                    f"(HTTP {list_exc.code} {list_exc.reason})."
                ) from list_exc
            except (urllib.error.URLError, TimeoutError, ssl.SSLError) as list_exc:
                raise RuntimeError(
                    _network_error_message(list_exc, "listar as releases no GitHub")
                ) from list_exc
            if isinstance(releases, list):
                release = next(
                    (r for r in releases if not r.get("draft") and not r.get("prerelease")),
                    releases[0] if releases else None,
                )
        else:
            reason = f" {exc.reason}" if getattr(exc, "reason", None) else ""
            raise RuntimeError(
                f"GitHub respondeu com erro HTTP {exc.code}{reason}."
            ) from exc
    except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
        raise RuntimeError(
            _network_error_message(exc, "verificar atualizações")
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Falha de rede ao verificar atualizações: {exc}."
        ) from exc

    if not isinstance(release, dict):
        return {
            "current_version": APP_VERSION,
            "latest_version": APP_VERSION,
            "update_available": False,
            "release_name": f"v{APP_VERSION}",
            "release_notes": "",
            "published_at": None,
            "html_url": GITHUB_RELEASES_URL,
            "asset_name": None,
            "asset_size": 0,
            "download_url": None,
            "can_auto_update": False,
            "install_mode": info["mode"],
        }

    tag_name = str(release.get("tag_name") or "").strip()
    latest_clean = tag_name.lstrip("vV") or APP_VERSION
    available = is_newer_version(latest_clean, APP_VERSION)
    assets = release.get("assets") or []
    asset = _select_release_asset(assets, info)
    can_auto = bool(
        available
        and (
            asset is not None
            or info["mode"] == "source"
        )
    )

    return {
        "current_version": APP_VERSION,
        "latest_version": latest_clean,
        "tag_name": tag_name or f"v{latest_clean}",
        "update_available": available,
        "release_name": str(release.get("name") or tag_name or f"v{latest_clean}"),
        "release_notes": str(release.get("body") or "").strip(),
        "published_at": release.get("published_at"),
        "html_url": str(release.get("html_url") or GITHUB_RELEASES_URL),
        "asset_name": asset.get("name") if asset else None,
        "asset_size": int(asset.get("size") or 0) if asset else 0,
        "download_url": asset.get("browser_download_url") if asset else None,
        "can_auto_update": can_auto,
        "install_mode": info["mode"],
    }


def get_update_status() -> dict[str, Any]:
    with _update_lock:
        return dict(_update_state)


def _set_update_state(**kwargs: Any) -> None:
    with _update_lock:
        _update_state.update(kwargs)


def _download_with_progress(url: str, destination: Path, expected_size: int = 0) -> None:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": f"DermaMetric/{APP_VERSION}",
        },
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    try:
        with _urlopen(req, timeout=60) as resp, open(destination, "wb") as out:
            total = int(resp.headers.get("Content-Length") or expected_size or 0)
            _set_update_state(total_bytes=total, downloaded_bytes=0, progress=1)
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                pct = int(min(92, round((downloaded / total) * 90))) if total > 0 else 50
                _set_update_state(
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    progress=max(1, pct),
                )
    except urllib.error.HTTPError as exc:
        destination.unlink(missing_ok=True)
        reason = f" {exc.reason}" if getattr(exc, "reason", None) else ""
        raise RuntimeError(
            f"Download da atualização recusado pelo servidor "
            f"(HTTP {exc.code}{reason})."
        ) from exc
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            _network_error_message(exc, "baixar a atualização")
        ) from exc


def _schedule_exit(delay: float = 0.8) -> None:
    def _exit() -> None:
        time.sleep(delay)
        os._exit(0)

    threading.Thread(target=_exit, daemon=True).start()


def _install_windows_update(installer_path: Path) -> None:
    """Agenda a execução silenciosa do Inno Setup após encerrar o processo atual."""
    pid = os.getpid()
    exe_path = Path(sys.executable).resolve()
    script_path = installer_path.parent / "apply_update.ps1"
    # Aguarda o processo atual encerrar, instala silenciosamente e reabre o app.
    ps_script = (
        f"$pidToWait = {pid}\n"
        "$proc = Get-Process -Id $pidToWait -ErrorAction SilentlyContinue\n"
        "if ($proc) { $proc.WaitForExit(15000) | Out-Null }\n"
        "Start-Sleep -Milliseconds 400\n"
        f'$installer = "{str(installer_path)}"\n'
        '$p = Start-Process -FilePath $installer -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/CLOSEAPPLICATIONS" -Wait -PassThru\n'
        f'$appExe = "{str(exe_path)}"\n'
        "if (Test-Path $appExe) {\n"
        "  Start-Process -FilePath $appExe\n"
        "}\n"
    )
    script_path.write_text(ps_script, encoding="utf-8")
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script_path),
        ],
        creationflags=creationflags,
        close_fds=True,
    )
    _set_update_state(
        status="restarting",
        progress=100,
        message="Instalando atualização e reiniciando o DermaMetric…",
    )
    _schedule_exit(0.9)


def _install_linux_appimage(downloaded_path: Path, target_str: str) -> None:
    """Valida ELF, substitui o AppImage instalado e reinicia o aplicativo."""
    with open(downloaded_path, "rb") as fh:
        magic = fh.read(4)
    if magic != b"\x7fELF":
        raise RuntimeError("O arquivo baixado não é um executável AppImage válido.")

    target = Path(target_str).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    downloaded_path.chmod(downloaded_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    staged = target.parent / f".{target.name}.new"
    shutil.copy2(downloaded_path, staged)
    staged.chmod(0o755)
    os.replace(staged, target)

    _set_update_state(
        status="restarting",
        progress=100,
        message="Atualização aplicada! Reiniciando o DermaMetric…",
    )
    env = os.environ.copy()
    env.pop("APPIMAGE", None)
    env.pop("APPDIR", None)
    env.pop("OWD", None)
    subprocess.Popen(
        [str(target), *sys.argv[1:]],
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    _schedule_exit(0.8)


def _install_source_update(tag_name: str) -> None:
    """Atualiza instalação baseada em código-fonte e reinicia o servidor/janela."""
    repo_root = paths.REPO_ROOT
    if (repo_root / ".git").exists() and shutil.which("git"):
        _set_update_state(progress=40, message=f"Baixando revisão {tag_name} via Git…")
        subprocess.run(
            ["git", "-C", str(repo_root), "fetch", "--tags", "origin"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        subprocess.run(
            ["git", "-C", str(repo_root), "checkout", "--force", tag_name],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    else:
        archive_url = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{tag_name}.tar.gz"
        tmp_dir = Path(tempfile.mkdtemp(prefix="dermametric-src-"))
        try:
            tar_path = tmp_dir / "release.tar.gz"
            _download_with_progress(archive_url, tar_path)
            _set_update_state(progress=85, message="Extraindo arquivos atualizados…")
            shutil.unpack_archive(str(tar_path), str(tmp_dir))
            extracted = next(
                (p for p in tmp_dir.iterdir() if p.is_dir() and p.name.startswith("dermametric")),
                None,
            )
            if extracted is None:
                raise RuntimeError("Não foi possível extrair o pacote da atualização.")
            for item in extracted.iterdir():
                if item.name in {"data", "storage", ".venv", ".git"}:
                    continue
                dest = repo_root / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    pip_bin = repo_root / ".venv" / "bin" / "pip"
    req_file = repo_root / "requirements.txt"
    if pip_bin.exists() and req_file.exists():
        _set_update_state(progress=92, message="Atualizando dependências Python…")
        subprocess.run(
            [str(pip_bin), "install", "-q", "-r", str(req_file)],
            check=False,
            capture_output=True,
            timeout=180,
        )

    _set_update_state(
        status="restarting",
        progress=100,
        message="Atualização concluída! Reiniciando…",
    )
    subprocess.Popen(
        [sys.executable, *sys.argv],
        cwd=str(repo_root),
        start_new_session=True,
        close_fds=True,
    )
    _schedule_exit(0.8)


def _run_update_worker(release_info: dict[str, Any]) -> None:
    info = detect_install_mode()
    mode = info["mode"]
    tag_name = release_info.get("tag_name") or f"v{release_info.get('latest_version')}"
    download_url = release_info.get("download_url")
    asset_name = release_info.get("asset_name") or "DermaMetric-Update"
    asset_size = int(release_info.get("asset_size") or 0)

    try:
        if mode == "windows-installer":
            if not download_url:
                raise RuntimeError("Instalador Windows não encontrado nesta release.")
            stage_dir = Path(tempfile.mkdtemp(prefix="dermametric-win-"))
            installer_path = stage_dir / asset_name
            _set_update_state(
                status="downloading",
                progress=5,
                message=f"Baixando {asset_name}…",
            )
            _download_with_progress(download_url, installer_path, asset_size)
            _set_update_state(
                status="installing",
                progress=95,
                message="Preparando instalador silencioso…",
            )
            _install_windows_update(installer_path)
            return

        if mode == "linux-appimage":
            if not download_url:
                raise RuntimeError("Pacote AppImage não encontrado nesta release.")
            stage_dir = Path(tempfile.mkdtemp(prefix="dermametric-appimage-"))
            try:
                appimage_tmp = stage_dir / asset_name
                _set_update_state(
                    status="downloading",
                    progress=5,
                    message=f"Baixando {asset_name}…",
                )
                _download_with_progress(download_url, appimage_tmp, asset_size)
                _set_update_state(
                    status="installing",
                    progress=95,
                    message="Substituindo executável AppImage…",
                )
                _install_linux_appimage(appimage_tmp, info["target_path"])
            finally:
                shutil.rmtree(stage_dir, ignore_errors=True)
            return

        if mode == "source":
            _set_update_state(
                status="downloading",
                progress=15,
                message=f"Atualizando código para {tag_name}…",
            )
            _install_source_update(tag_name)
            return

        raise RuntimeError(
            "Atualização automática não disponível para este modo de execução. "
            "Baixe a nova versão manualmente."
        )
    except Exception as exc:  # noqa: BLE001
        _set_update_state(
            status="error",
            error=str(exc),
            message=f"Falha na atualização: {exc}",
        )


def start_update() -> dict[str, Any]:
    """Inicia em background o download e instalação da última release disponível."""
    with _update_lock:
        if _update_state["status"] in {"downloading", "installing", "restarting"}:
            return dict(_update_state)

    release_info = check_for_updates()
    if not release_info.get("update_available"):
        raise ValueError("O DermaMetric já está na versão mais recente.")
    if not release_info.get("can_auto_update"):
        raise ValueError(
            "Nenhum pacote compatível foi encontrado para atualização automática."
        )

    _set_update_state(
        status="downloading",
        progress=2,
        downloaded_bytes=0,
        total_bytes=int(release_info.get("asset_size") or 0),
        message="Iniciando download da atualização…",
        target_version=release_info.get("latest_version"),
        error=None,
    )
    threading.Thread(
        target=_run_update_worker,
        args=(release_info,),
        name="dermametric-updater",
        daemon=True,
    ).start()
    return get_update_status()
