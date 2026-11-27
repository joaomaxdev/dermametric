"""Contratos estáticos dos novos controles de referência e zoom.

Cobrem a UI que acompanha as melhorias do laudo:

* ROI travada no T0, com ajuste manual explícito e avisos técnicos;
* overlay fantasma de T0 (L1–L4) com opacidade configurável;
* régua/grade em mm sobre a imagem do paciente;
* zoom do comparador (botões, roda, pinça e arraste) funcional durante o
  arraste da cortina.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND = REPO_ROOT / "frontend"


def _html() -> str:
    return (FRONTEND / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (FRONTEND / "app.js").read_text(encoding="utf-8")


def _css() -> str:
    return (FRONTEND / "styles.css").read_text(encoding="utf-8")


def test_compare_zoom_controls_exist_in_html():
    html = _html()
    for element_id in (
        "ba-viewport",
        "ba-stage",
        "ba-zoom-level",
        "ba-zoom-in",
        "ba-zoom-out",
        "ba-zoom-reset",
    ):
        assert f'id="{element_id}"' in html, f"#{element_id} ausente no HTML"


def test_compare_viewport_wraps_the_clipped_stage():
    html = _html()
    viewport_start = html.index('id="ba-viewport"')
    stage_start = html.index('id="ba-stage"')
    assert viewport_start < stage_start
    assert stage_start < html.index('class="compare-zoom"')


def test_ghost_roi_controls_exist_in_javascript_and_html():
    js = _js()
    assert "ghost: { enabled: true, opacity: 0.45 }" in js
    assert 'id="ghost-enabled"' in js
    assert 'id="ghost-opacity"' in js
    assert "function ghostRois()" in js
    assert "function drawGhostPolygon" in js


def test_scale_guide_is_rendered_over_the_patient_image():
    js = _js()
    assert "function drawScaleGuide(ctx)" in js
    assert "state.scaleGuide" in js
    assert 'data-action="toggle-scale"' in js
    assert "pxPerMm" in js and "stepMm" in js
    assert "drawScaleGuide(ctx)" in js


def test_compare_zoom_contract_in_javascript():
    js = _js()
    for function in (
        "function applyCompareZoom()",
        "function setCompareZoom(",
        "function zoomCompare(",
        "function resetCompareZoom()",
    ):
        assert function in js, f"{function} ausente"

    apply_block = js.split("function applyCompareZoom()", 1)[1].split("\n}", 1)[0]
    assert "scale(${zoom})" in apply_block
    assert "translate(" in apply_block
    assert "state.compare.panX" in apply_block

    assert 'addEventListener("wheel"' in js
    assert "passive: false" in js
    assert "comparePointers.size === 2" in js
    assert "ba-zoom-in" in js and "ba-zoom-out" in js and "ba-zoom-reset" in js


def test_compare_slider_keeps_working_with_zoom():
    js = _js()
    block = js.split("function setComparePosition(", 1)[1].split("\n}", 1)[0]
    # A posição usa o retângulo transformado do palco, portanto continua
    # correta durante o zoom e o arraste da cortina.
    assert "getBoundingClientRect()" in block
    assert "state.compare.pos = pct" in block
    update = js.split("function updateBA()", 1)[1].split("\n}", 1)[0]
    assert "applyComparePosition()" in update
    assert "applyCompareZoom()" in update


def test_roi_lock_manual_override_and_payload():
    js = _js()
    assert "state.roiManual" in js
    assert "function step3HTML(session)" in js
    assert 'data-action="manual-roi"' in js
    assert "roi_manual:" in js
    assert "roi_mode" in js
    assert "roi_warnings" in js
    assert "roiWarnings" in js
    assert "reprojetada do T0" in js or "reprojetada de T0" in js


def test_roi_visual_alerts_and_scale_css():
    css = _css()
    assert ".technical-alert" in css
    assert ".reference-guides" in css
    assert ".ba-viewport" in css
    viewport = re.search(r"\.ba-viewport\s*\{([^}]*)\}", css)
    assert viewport and "overflow: hidden" in viewport.group(1)
    stage = re.search(r"\.ba-stage\s*\{([^}]*)\}", css)
    assert stage and "position: absolute" in stage.group(1)
    assert "transform-origin" in stage.group(1)
    assert ".compare-zoom" in css


def test_local_elastic_refinement_feeds_t0_references():
    js = _js()
    assert "async function loadLocalAlignment(session" in js
    assert "/reference-polygons" in js
    assert "local_alignment" in js
    assert "Alinhamento local T0→" in js
    assert "Alinhamento global mantido" in js
    assert "LOCAL_ALIGNMENT_REASONS" in js
    assert ".is-local-adjust" in _css()

    rois = js.split("function referenceRois()", 1)[1].split("\n}", 1)[0]
    assert "localAdjustment" in rois
    assert "localEntry" in rois
    assert "referencePoints" in rois
    assert "entry.polygon" in rois

    # Fantasma, "Fixar máscara" e seed de auto-ajuste usam referenceRois().
    ghost = js.split("function ghostRois()", 1)[1].split("\n}", 1)[0]
    assert "referenceRois()" in ghost
    pin = js.split("function pinT0Mask()", 1)[1].split("\n}", 1)[0]
    assert "referenceRoiForActive()" in pin
    seed = js.split("function autoSegmentFromT0()", 1)[1].split("\n}", 1)[0]
    assert "referenceRoiForActive()" in seed
    active = js.split("function referenceRoiForActive()", 1)[1].split("\n}", 1)[0]
    assert "referenceRois()" in active
