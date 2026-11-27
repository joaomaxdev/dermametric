"""Testes de ROI reprojetada, qualidade CIELAB, geometria e escala do laudo.

Cobrem as melhorias de precisão do laudo:

* ROI de pele sadia herdada de T0 e reprojetada por homografia;
* alertas de área inválida (borda preta) e alta variância interna;
* descritor CIELAB robusto (mediana);
* sanitização de polígonos (duplicados, colineares e autointerseções);
* guarda contra comprimentos em mm fisicamente implausíveis;
* régua/grade métrica na imagem anotada sem alterar os dados originais.
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from backend import report, vision

SKIN_BGR = (150, 170, 200)


def _write(path: Path, image: np.ndarray) -> str:
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    path.write_bytes(buffer.tobytes())
    return str(path)


def _skin_image(width: int = 200, height: int = 200) -> np.ndarray:
    return np.full((height, width, 3), SKIN_BGR, dtype=np.uint8)


# ---------------------------------------------------------------------------
# ROI reprojetada e qualidade
# ---------------------------------------------------------------------------
def test_project_t0_roi_through_homography_and_back():
    t0_roi = [20.0, 30.0, 40.0, 50.0]
    homography = np.array(
        [[1.10, 0.02, 15.0], [-0.01, 0.92, -8.0], [0.0004, 0.0002, 1.0]],
        dtype=np.float64,
    )
    projected = vision.project_roi(t0_roi, homography, (400, 400))
    assert projected is not None
    assert projected[2] > 0 and projected[3] > 0

    inverse = np.linalg.inv(homography)
    roundtrip = vision.project_roi(projected, inverse, (400, 400))
    assert roundtrip is not None
    # A ROI é um retângulo inteiro (4 cantos) e a ida/volta passa por
    # bounding boxes inteiros, daí a tolerância de poucos pixels.
    assert roundtrip == pytest.approx(t0_roi, abs=5)


def test_project_t0_roi_back_to_source_frame():
    homography = np.array(
        [[1.0, 0.0, 20.0], [0.0, 1.0, -10.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    # A ROI em T0 (espaço alinhado) volta 20 px à esquerda e 10 px abaixo na
    # foto original, exatamente o inverso da translação.
    source_roi = vision.project_roi_inverse([100, 100, 40, 30], homography, (400, 400))
    assert source_roi is not None
    assert source_roi == pytest.approx([80, 110, 40, 30], abs=1)


def test_project_roi_rejects_projection_outside_valid_area():
    # Translação que empurra quase toda a ROI para fora da imagem alinhada.
    outside = np.array([[1, 0, 195], [0, 1, 195], [0, 0, 1]], dtype=np.float64)
    assert vision.project_roi([10, 10, 60, 60], outside, (200, 200)) is None


def test_roi_over_black_border_flags_invalid_area(tmp_path):
    image = _skin_image()
    image[150:, :] = 0
    image[:, 150:] = 0
    path = _write(tmp_path / "roi_border.png", image)

    stats = vision.analyze_region_lab(path, [130, 130, 60, 60])
    codes = {warning["code"] for warning in stats["warnings"]}
    assert "roi_outside_valid_area" in codes
    assert stats["valid_fraction"] < vision.ROI_MIN_VALID_FRACTION


def test_roi_high_internal_variance_flags_review(tmp_path):
    image = _skin_image()
    image[:, 100:] = (35, 55, 95)  # metade escura dentro da ROI
    path = _write(tmp_path / "roi_variance.png", image)

    stats = vision.analyze_region_lab(path, [40, 40, 120, 120])
    codes = {warning["code"] for warning in stats["warnings"]}
    assert "roi_high_variance" in codes
    assert stats["lightness_std"] > vision.ROI_MAX_LIGHTNESS_STD


def test_cielab_reference_uses_median_instead_of_mean(tmp_path):
    image = _skin_image(120, 120)
    # Pequena mancha de lesão no canto da ROI: desloca a média, não a mediana.
    image[10:25, 10:25] = (40, 60, 210)
    path = _write(tmp_path / "roi_median.png", image)

    lab = vision.bgr_to_cielab_float(image)
    pixels = lab[5:95, 5:95].reshape(-1, 3)
    mean = pixels.mean(axis=0)
    median = np.median(pixels, axis=0)
    measured = np.asarray(vision.compute_region_lab_means(path, [5, 5, 90, 90]))

    assert np.linalg.norm(measured - median) < 1e-6
    assert np.linalg.norm(measured - mean) > 0.05


# ---------------------------------------------------------------------------
# Sanitização de polígonos e comprimento
# ---------------------------------------------------------------------------
def test_sanitize_removes_duplicate_and_collinear_vertices():
    polygon = [[0, 0], [5, 0], [10, 0], [10, 10], [10, 10], [0, 10]]
    cleaned, info = vision.sanitize_polygon(polygon, (100, 100))

    assert cleaned == [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
    codes = {warning["code"] for warning in info["warnings"]}
    assert "polygon_duplicate_vertices" in codes
    assert "polygon_redundant_vertices" in codes


def test_sanitize_repairs_self_intersecting_polygon():
    bowtie = [[10, 10], [100, 100], [10, 100], [100, 10]]
    cleaned, info = vision.sanitize_polygon(bowtie, (200, 200))

    assert len(cleaned) >= 3
    assert not vision.polygon_self_intersects(cleaned)
    assert abs(vision._signed_polygon_area(np.asarray(cleaned))) > 0
    codes = {warning["code"] for warning in info["warnings"]}
    assert "polygon_self_intersections" in codes


def test_sanitize_keeps_valid_polygon_unchanged():
    angles = np.linspace(0.0, 2.0 * math.pi, 32, endpoint=False)
    polygon = [[100 + 40 * math.cos(t), 100 + 20 * math.sin(t)] for t in angles]
    cleaned, info = vision.sanitize_polygon(polygon, (300, 300))

    assert np.allclose(cleaned, polygon)
    assert info["changes"] == []


def test_measure_anomalous_length_is_rejected(tmp_path):
    image = _skin_image(300, 300)
    path = _write(tmp_path / "measure_len.png", image)
    polygon = [[40, 40], [240, 40], [240, 90], [40, 90]]

    with pytest.raises(ValueError, match="Comprimento anômalo"):
        vision.measure_stria_polygon(path, polygon, 0.001)


def test_measure_accepts_sanitized_polygon_and_keeps_numeric_contract(tmp_path):
    image = _skin_image(300, 300)
    path = _write(tmp_path / "measure_clean.png", image)
    polygon = [[40, 40], [240, 40], [240, 90], [240, 90], [40, 90]]

    cleaned, geometry = vision.sanitize_polygon(polygon, image.shape[:2])
    metrics = vision.measure_stria_polygon(path, cleaned, 2.0)

    codes = {warning["code"] for warning in geometry["warnings"]}
    assert "polygon_duplicate_vertices" in codes
    assert len(cleaned) == 4
    assert metrics["length_mm"] > 0
    # O contrato numérico permanece estável para relatório/watermark.
    assert set(metrics) == {
        "length_mm", "mean_width_mm", "max_width_mm", "area_mm2", "delta_e_00"
    }


# ---------------------------------------------------------------------------
# Escala/régua no laudo
# ---------------------------------------------------------------------------
def test_metric_scale_and_grid_are_drawn_without_mutating_source():
    image = _skin_image(420, 300)
    original = image.copy()

    gridded = image.copy()
    report._draw_metric_grid(gridded, 10.0)
    assert not np.array_equal(gridded, original)

    scaled = image.copy()
    report._draw_metric_scale(scaled, 10.0, avoid_points=[[350, 40], [380, 60]])
    assert not np.array_equal(scaled, original)
    # A régua é ancorada no canto oposto às marcações e usa fundo escuro.
    changed = np.count_nonzero(np.any(scaled != original, axis=2))
    assert changed > 100
    top_left = scaled[:80, :170]
    assert top_left.mean() < original[:80, :170].mean()

    # A imagem de entrada (dados originais) nunca é alterada no local.
    assert np.array_equal(image, original)


def test_annotated_lesion_map_embeds_scale_reference():
    image = _skin_image(420, 300)
    original = image.copy()
    session = {
        "px_per_mm": 10.0,
        "healthy_skin_roi": [20, 20, 80, 60],
        "measurements": [
            {
                "stria_code": "L1",
                "polygon": [[260, 60], [360, 60], [360, 100], [260, 100]],
                "metrics": {"technical_warnings": []},
            }
        ],
    }
    png = report._annotate_lesion_map(
        image, session, ["L1"], {"L1": (0, 150, 136)}
    )

    assert png
    decoded = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape == image.shape
    assert not np.array_equal(decoded, original)
    assert np.array_equal(image, original)


def test_report_roi_summary_and_warnings_are_explicit():
    session = {
        "healthy_skin_roi": [20, 30, 40, 50],
        "roi_mode": "manual",
        "roi_warnings": [
            {"code": "roi_manual_override", "message": "ajuste manual excepcional"}
        ],
    }
    assert "ajuste manual" in report._roi_summary(session)
    assert report._session_warnings(session) == ["ajuste manual excepcional"]
