"""Testes de regressão da UI do modal de restauração ``.dmbak``.

Cobrem o bug do "cartão branco vazio" no ``#profile-modal``:

* todos os IDs usados pelo fluxo de progresso/logs existem no HTML;
* o cabeçalho do modal nunca é ocultado pelo CSS (``flex-shrink`` e ``hidden``);
* o ``backdrop-filter`` do backdrop foi removido (bug de composição do Chromium);
* verificação end-to-end em Chromium headless (opcional, pulada quando o
  Playwright/browser não está disponível).
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND = REPO_ROOT / "frontend"
E2E_SCRIPT = REPO_ROOT / "tests" / "e2e" / "check_restore_modal.cjs"

BACKUP_FLOW_IDS = {
    "profile-modal",
    "profile-title",
    "backup-file",
    "backup-inspect",
    "backup-restore-password",
    "backup-preview",
    "backup-progress",
    "backup-progress-step",
    "backup-progress-percent",
    "backup-progress-bar",
    "backup-log",
    "backup-finish",
    "restore-banner",
    "restore-banner-title",
    "restore-banner-detail",
    "restore-banner-close",
}

PROFILE_TAB_IDS = {
    "profile-tab-profile",
    "profile-tab-security",
    "profile-tab-backup",
    "profile-panel-profile",
    "profile-panel-security",
    "profile-panel-backup",
}

PROFILE_TAB_NAMES = ("profile", "security", "backup")


def _html() -> str:
    return (FRONTEND / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return (FRONTEND / "styles.css").read_text(encoding="utf-8")


def _js() -> str:
    return (FRONTEND / "app.js").read_text(encoding="utf-8")


def test_backup_flow_ids_exist_in_html():
    declared = set(re.findall(r'id="([^"]+)"', _html()))
    missing = BACKUP_FLOW_IDS - declared
    assert not missing, f"IDs ausentes em frontend/index.html: {sorted(missing)}"


def test_profile_tabs_structure_and_panels():
    html = _html()
    declared = set(re.findall(r'id="([^"]+)"', html))
    missing = PROFILE_TAB_IDS - declared
    assert not missing, f"IDs de abas ausentes em frontend/index.html: {sorted(missing)}"
    assert 'role="tablist"' in html
    for name in PROFILE_TAB_NAMES:
        assert f'data-profile-tab="{name}"' in html
        assert f'aria-controls="profile-panel-{name}"' in html
        panel = re.search(rf'<section id="profile-panel-{name}"([^>]*)>', html)
        assert panel, f"painel {name} ausente"
        assert 'role="tabpanel"' in panel.group(1)
    # A aba padrão é a de dados do profissional.
    active = re.search(r'<button[^>]*data-profile-tab="profile"[^>]*>', html).group(0)
    assert "is-active" in active and 'aria-selected="true"' in active
    for name in ("security", "backup"):
        button = re.search(rf'<button[^>]*data-profile-tab="{name}"[^>]*>', html).group(0)
        assert 'aria-selected="false"' in button
    # Cada seção vive em seu próprio painel, sem tudo empilhado no corpo.
    assert html.count('class="profile-tab-panel"') == 3


def test_backup_tab_only_has_full_station_backup():
    html = _html()
    panel = html[html.index('id="profile-panel-backup"'):html.index("<!-- Guia de Uso")]
    assert "backup-export-full" in panel
    assert "backup-inspect" in panel and "backup-file" in panel
    # O painel de progresso/logs abre a aba, antes dos cards.
    assert panel.index('id="backup-progress"') < panel.index('id="backup-export-full"')
    # Exportação individual/selecionada agora pertence ao fluxo de pacientes.
    assert "data-backup-export-mode" not in html
    assert "backup-export-selected" not in html
    assert "backup-archive-password" not in html
    js = _js()
    assert "function exportPatientCaseBackup" in js


def test_profile_tab_switching_contract():
    js = _js()
    assert "function switchProfileTab" in js
    assert "state.profileTab" in js
    body = js.split("function switchProfileTab", 1)[1].split("\n}", 1)[0]
    assert "scrollTop = 0" in body
    assert "aria-selected" in body
    # A restauração precisa ativar a aba de backup antes de mostrar o relatório.
    reveal = js.split("function revealBackupReport", 1)[1].split("async function syncDirectoryAfterRestore", 1)[0]
    assert 'switchProfileTab("backup")' in reveal


def test_backup_flow_ids_referenced_by_js_are_unique():
    declared = re.findall(r'id="([^"]+)"', _html())
    duplicates = {value for value in declared if declared.count(value) > 1}
    assert not duplicates, f"IDs duplicados em frontend/index.html: {sorted(duplicates)}"


def test_modal_header_cannot_shrink_or_hide():
    css = _css()
    assert "[hidden] { display: none !important; }" in css
    head_block = re.search(r"\.modal-head\s*\{([^}]*)\}", css)
    assert head_block, "regra .modal-head não encontrada"
    assert "flex: 0 0 auto" in head_block.group(1), "cabeçalho do modal pode encolher"
    window_block = re.search(r"\.modal-window\s*\{([^}]*)\}", css)
    assert window_block and "z-index" in window_block.group(1)


def test_backdrop_does_not_use_backdrop_filter():
    """O blur do backdrop já deixou o cartão do modal em branco no Chromium."""
    css = re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)
    block = re.search(r"\.modal-backdrop\s*\{([^}]*)\}", css)
    assert block, "regra .modal-backdrop não encontrada"
    assert "backdrop-filter" not in block.group(1)


def test_modal_window_avoids_unstable_gpu_layers():
    """Brave/Chromium no Wayland descarta camadas compostas e pinta o cartão branco."""
    css = re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)
    block = re.search(r"\.modal-window\s*\{([^}]*)\}", css)
    assert block, "regra .modal-window não encontrada"
    body = block.group(1)
    for prop in ("isolation", "will-change", "contain", "transform"):
        assert prop not in body, f".modal-window não pode usar {prop} (camada composta instável)"
    animation = re.search(r"animation\s*:\s*modal-in([^;]*);", body)
    assert animation and "both" not in animation.group(1), "animação do cartão não pode reter transform"
    keyframes = re.search(r"@keyframes modal-in\s*\{([^}]*)\}", css)
    assert keyframes and "transform" not in keyframes.group(1)


def test_restore_fail_safe_banner_outside_modal():
    html = _html()
    banner = re.search(r'id="restore-banner"([^>]*)>', html)
    assert banner, "banner fail-safe ausente em frontend/index.html"
    modal_start = html.index('id="profile-modal"')
    profile_end = html.index("<!-- Guia de Uso", modal_start)
    banner_start = html.index('id="restore-banner"')
    assert banner_start > profile_end, "banner fail-safe precisa ficar fora do #profile-modal"
    js = _js()
    assert "function showRestoreBanner" in js
    assert "showRestoreBanner(" in js
    assert "hideRestoreBanner" in js


def test_modal_is_not_blanked_by_sync_helpers():
    js = _js()
    assert "function revealBackupReport" in js
    # A sincronização pós-restore precisa reexibir o modal e o painel.
    sync = js.split("async function syncDirectoryAfterRestore", 1)
    assert len(sync) == 2
    assert "revealBackupReport()" in sync[1].split("\n}", 1)[0]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.e2e
def test_restore_report_visible_in_chromium(tmp_path):
    """Abre o modal, simula a restauração e confere o cartão/relatório visíveis."""
    if os.environ.get("DERMAMETRIC_E2E", "").strip().lower() not in {"1", "true", "yes"}:
        pytest.skip("teste e2e opt-in: defina DERMAMETRIC_E2E=1 para executá-lo")
    node = "node" if os.name != "nt" else "node.exe"
    if not E2E_SCRIPT.exists():
        pytest.skip("script e2e ausente")
    port = _free_port()
    env = {
        **os.environ,
        "DERMAMETRIC_DATA_DIR": str(tmp_path / "data"),
        "DERMAMETRIC_STORAGE_DIR": str(tmp_path / "storage"),
        "DERMAMETRIC_DB_PATH": str(tmp_path / "data" / "dermametric.db"),
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 30
        healthy = False
        import urllib.error
        import urllib.request

        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{base}/api/health", timeout=1) as response:
                    healthy = response.status == 200
                    if healthy:
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        if not healthy:
            pytest.skip("não foi possível subir o servidor para o teste e2e")

        result = subprocess.run(
            [node, str(E2E_SCRIPT), "--url", base],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = f"{result.stdout}\n{result.stderr}"
        if "Pacote 'playwright' não encontrado" in output:
            pytest.skip("playwright não instalado neste ambiente")
        assert result.returncode == 0, output[-4000:]
        assert "OK: modal, progresso, logs e botão final visíveis" in output
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            server.kill()


# ---------------------------------------------------------------------------
# Caso clínico individual: exportação com senha própria × importação no modal
# ---------------------------------------------------------------------------
CASE_IMPORT_IDS = {
    "patient-tab-manual",
    "patient-tab-import",
    "patient-form",
    "patient-panel-import",
    "case-import-drop",
    "case-import-file",
    "case-import-file-status",
    "case-import-pin",
    "case-import-error",
    "case-import-validate",
    "case-import-preview",
    "case-import-conflict",
    "case-import-progress",
    "case-import-status",
    "case-import-percent",
    "case-import-bar",
    "case-import-submit",
}

EXPORT_CASE_IDS = {
    "export-case-modal",
    "export-case-form",
    "export-case-password",
    "export-case-pin",
    "export-case-pin-label",
    "export-case-pin-hint",
    "export-case-pin-toggle",
    "export-case-error",
    "export-case-submit",
}


def test_case_export_and_import_ids_exist_in_html():
    declared = set(re.findall(r'id="([^"]+)"', _html()))
    missing = (CASE_IMPORT_IDS | EXPORT_CASE_IDS) - declared
    assert not missing, f"IDs ausentes em frontend/index.html: {sorted(missing)}"


def test_patient_modal_tabs_default_to_manual_form():
    html = _html()
    manual = re.search(r'<button[^>]*data-patient-tab="manual"[^>]*>', html).group(0)
    assert "is-active" in manual and 'aria-selected="true"' in manual
    import_tab = re.search(r'<button[^>]*data-patient-tab="import"[^>]*>', html).group(0)
    assert 'aria-selected="false"' in import_tab
    panel = re.search(r'<section id="patient-panel-import"([^>]*)>', html)
    assert panel, "painel de importação ausente"
    assert "role=\"tabpanel\"" in panel.group(1) and "hidden" in panel.group(1)
    form = re.search(r'<form id="patient-form"([^>]*)>', html)
    assert form and 'role="tabpanel"' in form.group(1) and "hidden" not in form.group(1)
    # Conflito tem a opção nova marcada por padrão.
    conflict = html[html.index('id="case-import-conflict"'):html.index('id="case-import-progress"')]
    assert 'value="new" checked' in conflict
    assert 'value="merge"' in conflict


def test_case_export_uses_the_case_pin_not_the_master_password():
    js = _js()
    request = js.split("async function requestBackupExport", 1)[1].split("\n}", 1)[0]
    assert "body.pin" in request
    assert "archive_password" not in request
    submit = js.split("async function submitExportCaseBackup", 1)[1].split("\n}", 1)[0]
    assert "export-case-pin" in submit
    assert "requestBackupExport(password, patient.id, casePin)" in submit
    assert "/^\\d{4,8}$/" in submit
    assert "DermaMetric-Caso-" in js


def test_case_import_sends_the_case_pin_and_shows_pin_errors():
    js = _js()
    validate = js.split("async function validateCaseImport", 1)[1].split("\n}\n", 1)[0]
    assert 'form.append("case_pin", pin)' in validate
    submit = js.split("async function submitCaseImport", 1)[1].split("\n}\n", 1)[0]
    assert 'form.append("case_pin", pin)' in submit
    assert "case-import-pin" in submit
    assert "case-import-password" not in js


def test_case_import_flow_contract():
    js = _js()
    for fn in ("switchPatientTab", "validateCaseImport", "submitCaseImport", "resetCaseImport"):
        assert f"function {fn}" in js, fn
    submit = js.split("async function submitCaseImport", 1)[1].split("\n}\n", 1)[0]
    assert 'form.append("import_as_new"' in submit
    assert 'form.append("mode", "merge")' in submit
    assert "postFormWithProgress" in submit
    assert "openPatient(target.id)" in submit
    # Importação de caso recusa backup completo, empurrando para Perfil → Backup.
    validate = js.split("async function validateCaseImport", 1)[1].split("\n}\n", 1)[0]
    assert 'summary.mode !== "patient"' in validate
    assert "backup completo da estação" in validate
    assert 'caseImportPreviewHtml' in validate or 'caseImportPreviewHtml' in js


def test_case_import_tab_resets_on_open_and_close():
    js = _js()
    open_fn = js.split("function openPatientModal", 1)[1].split("\n}", 1)[0]
    close_fn = js.split("function closePatientModal", 1)[1].split("\n}", 1)[0]
    assert 'switchPatientTab("manual")' in open_fn
    assert "resetCaseImport()" in open_fn
    assert "resetCaseImport()" in close_fn


# ---------------------------------------------------------------------------
# Tela "Sobre" — carta institucional e dedicatória discreta
# ---------------------------------------------------------------------------
def test_about_screen_letter_content_and_credits():
    html = _html()
    for element_id in ("about-open", "about-modal", "about-logo", "about-title", "about-dedication"):
        assert f'id="{element_id}"' in html, element_id
    assert "Sobre o DermaMetric" in html
    assert "Dra. Isabela Sousa" in html
    assert "joaomaxdev" in html
    assert "Idealização e Desenvolvimento · DermaMetric" in html
    assert "https://github.com/joaomaxdev/dermametric" in html
    assert "plataforma completa de análise morfométrica cutânea em 2D" in html
    assert "portabilidade segura de casos por meio de PIN individual" in html
    # Dedicatória escondida no rodapé da carta e easter egg no HTML.
    dedication = re.search(r'<p id="about-dedication"[^>]*>', html).group(0)
    assert "hidden" in dedication
    assert "Dedicado ao meu filho Heitor — minha maior inspiração." in html
    assert 'data-inspiration="Heitor"' in html
    assert "<!-- Dedicado com amor a Heitor -->" in html


def test_about_logo_click_easter_egg_contract():
    js = _js()
    fn = js.split("function onAboutLogoClick", 1)[1].split("\n}\n", 1)[0]
    assert "aboutLogoClickCount < 5" in fn
    assert "about-dedication" in fn
    assert "is-visible" in fn
    # Segundo clique depois de revelada esconde a dedicatória.
    assert "aboutDedicationVisible" in fn
    assert "hidden = true" in fn
    css = _css()
    assert ".about-dedication" in css
    assert "transition: opacity" in css
    assert ".about-dedication.is-visible" in css


# ---------------------------------------------------------------------------
# Barras laterais recolhíveis e foco no canvas
# ---------------------------------------------------------------------------
RAIL_IDS = {
    "workspace-sidebar",
    "workspace-inspector",
    "rail-collapse-left",
    "rail-collapse-right",
    "canvas-focus",
}


def test_rail_collapse_controls_exist_in_html():
    html = _html()
    declared = set(re.findall(r'id="([^"]+)"', html))
    missing = RAIL_IDS - declared
    assert not missing, f"IDs ausentes em frontend/index.html: {sorted(missing)}"

    left = re.search(r'<button[^>]*id="rail-collapse-left"[^>]*>', html).group(0)
    right = re.search(r'<button[^>]*id="rail-collapse-right"[^>]*>', html).group(0)
    for button in (left, right):
        assert 'aria-expanded="true"' in button
        assert 'aria-controls="workspace-' in button
    focus = re.search(r'<button[^>]*id="canvas-focus"[^>]*>', html).group(0)
    assert 'aria-pressed="false"' in focus
    assert 'aria-controls="workspace-sidebar workspace-inspector"' in focus
    assert "Foco no Canvas" in html


def test_rail_collapse_contract_in_js():
    js = _js()
    for fn in (
        "loadRailPreferences",
        "saveRailPreferences",
        "applyRailState",
        "setRailCollapsed",
        "toggleRail",
        "toggleCanvasFocus",
        "exitCanvasFocus",
        "resetWorkspaceRails",
    ):
        assert f"function {fn}" in js, fn

    assert 'dermametric.rails.v1' in js
    assert "window.localStorage" in js
    # Persistência nunca pode quebrar a UI se o localStorage falhar.
    save = js.split("function saveRailPreferences", 1)[1].split("\n}", 1)[0]
    assert "try" in save and "catch" in save
    apply = js.split("function applyRailState", 1)[1].split("\n}\n", 1)[0]
    assert "is-left-collapsed" in apply
    assert "is-right-collapsed" in apply
    assert "aria-expanded" in apply
    assert "aria-pressed" in apply
    focus = js.split("function toggleCanvasFocus", 1)[1].split("\n}", 1)[0]
    assert "state.canvasFocus = !state.canvasFocus" in focus
    assert "applyRailState()" in focus
    open_fn = js.split("async function openPatient", 1)[1].split("\n\n", 1)[0]
    assert "resetWorkspaceRails()" in open_fn


def test_rail_collapse_css_and_persistence_reset():
    css = _css()
    # O palco central expande via grid dinâmico e transiciona suavemente.
    assert ".app-shell.is-left-collapsed { grid-template-columns: 46px" in css
    assert ".app-shell.is-right-collapsed { grid-template-columns: 286px" in css
    assert "transition: grid-template-columns 220ms ease" in css
    assert "writing-mode: vertical-rl" in css
    # Ao sair do prontuário o foco no canvas é encerrado (sem estado preso).
    js = _js()
    screen = js.split("function setScreen", 1)[1].split("\n}", 1)[0]
    assert 'name !== "workspace"' in screen
    assert "exitCanvasFocus()" in screen


# ---------------------------------------------------------------------------
# Launch token da janela nativa (WebView2) propagado às imagens
# ---------------------------------------------------------------------------
def test_launch_token_is_captured_and_removed_from_url():
    js = _js()
    assert 'sessionStorage.setItem("dm_launch_token"' in js
    assert 'sessionStorage.getItem("dm_launch_token")' in js
    assert "history.replaceState" in js
    assert 'searchParams.delete("launch_token")' in js
    assert "function withLaunchToken" in js
    # O header também acompanha fetch/XHR (fallback do cookie no WebView2).
    assert 'headers["X-DermaMetric-Launch"] = DESKTOP_LAUNCH_TOKEN' in js
    assert 'xhr.setRequestHeader("X-DermaMetric-Launch", DESKTOP_LAUNCH_TOKEN)' in js


def test_launch_token_is_appended_to_every_image_url():
    js = _js()
    load = js.split("function loadImage", 1)[1].split("\n}", 1)[0]
    assert "withLaunchToken(url)" in load
    thumb = js.split("function thumbHTML", 1)[1].split("</span>`;", 1)[0]
    assert "withLaunchToken(url)" in thumb
    compare = js.split("function updateBA", 1)[1].split("\n}", 1)[0]
    assert "assignImageElement(base, urls.base)" in compare
    assert "assignImageElement(top, urls.top)" in compare
    # URLs de blob/data e domínios externos não recebem query de token.
    helper = js.split("function withLaunchToken", 1)[1].split("\n}", 1)[0]
    assert 'str.startsWith("blob:")' in helper
    assert 'str.startsWith("data:")' in helper
    assert 'str.startsWith("/api/")' in helper


def test_images_are_loaded_via_authenticated_fetch():
    js = _js()
    fetch_blob = js.split("async function fetchImageBlob", 1)[1].split("\n}", 1)[0]
    assert "fetch(url" in fetch_blob
    assert 'headers["X-DermaMetric-Launch"] = DESKTOP_LAUNCH_TOKEN' in fetch_blob or (
        "imageRequestHeaders()" in fetch_blob
    )
    assert "response.status" in fetch_blob
    assert "console.error" in fetch_blob
    assert "HTTP ${response.status}" in fetch_blob

    headers = js.split("function imageRequestHeaders", 1)[1].split("\n}", 1)[0]
    assert 'headers["X-DermaMetric-Launch"] = DESKTOP_LAUNCH_TOKEN' in headers
    assert "headers.Authorization = `Bearer ${state.auth.token}`" in headers

    load = js.split("function loadImage", 1)[1].split("\n}", 1)[0]
    assert "URL.createObjectURL(blob)" in load
    assert "URL.revokeObjectURL(objectUrl)" in load
    assert "img.src = resolvedUrl" not in load

    # Miniaturas e comparador não montam <img src="/api/files/..."> direto.
    assert js.count('<img src="${esc(resolvedUrl)}"') == 0
    assert "hydrateThumbnails(panel)" in js
    assert "assignImageElement(base, urls.base)" in js
    assert "assignImageElement(top, urls.top)" in js


# ---------------------------------------------------------------------------
# Auto-detecção assistida (caixa + CIELAB/GrabCut) e guia por T0
# ---------------------------------------------------------------------------
def test_auto_segment_controls_and_api_calls():
    js = _js()
    # Ferramenta de caixa, seletor de perfil e painel de sensibilidade.
    assert 'data-action="auto-segment"' in js
    assert 'id="auto-sensitivity"' in js
    assert 'data-auto-mode="${value}"' in js
    assert '["auto", "Auto"], ["rubra", "Rubra"], ["alba", "Alba"]' in js
    assert "Auto-detectar (Caixa)" in js
    # Ações guiadas por T0 no seguimento.
    assert 'data-action="auto-from-t0"' in js
    assert 'data-action="pin-t0-mask"' in js
    assert "Auto-ajustar de T0" in js
    assert "Fixar máscara de T0" in js
    # Chamada autenticada ao endpoint e envio do corredor/sensibilidade.
    call = js.split("async function runAutoSegment", 1)[1].split("\n}", 1)[0]
    assert '"/auto-segment"' in call
    assert "seed_polygon" in call
    assert "sensitivity" in call
    assert "mode" in call
    # Debounce da sensibilidade e edição fina do resultado.
    assert "function scheduleAutoSegment" in js
    assert "window.clearTimeout(autoSegmentTimer)" in js
    assert "state.polygon = response.polygon" in call
    assert "state.polygonAuto = true" in call


def test_auto_segment_ghost_overlay_and_t0_validation():
    js = _js()
    # Fantasma tracejado translúcido dos polígonos de T0.
    assert "function drawGhostPolygon" in js
    assert "setLineDash([4, 4])" in js
    assert "drawGhostRois(ctx)" in js
    # Caixa de busca desenhada por arraste.
    assert "type: \"autobox\"" in js
    assert "function drawAutoBox" in js
    # Após a auto-detecção não sobra linha-guia solta; vértices já editáveis.
    assert "!state.polygonAuto" in js
    assert "function insertVertexOnEdge" in js
    assert "function nearestPointOnSegment" in js
    assert "type: \"vertex\"" in js
    # Validação visual antes de salvar desvios relevantes.
    assert "function t0DeviationWarnings" in js
    assert "> 10" in js
    assert "1.35" in js
    assert "window.confirm(" in js
    css = _css()
    assert ".auto-segment" in css
    assert ".auto-sensitivity" in css
    assert ".auto-mode" in css
