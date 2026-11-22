"""Testes da marca d'água clínica do DermaMetric.

Garantias verificadas:

1. ``apply_clinical_watermark`` devolve uma cópia com as mesmas dimensões e
   tipo, sem mutar a imagem de entrada.
2. Apenas a faixa do canto inferior direito é modificada.
3. Campos ausentes (paciente / data) são omitidos do texto.
4. A marca d'água não altera o resultado do motor fotométrico
   (``measure_stria_polygon`` — área, larguras, comprimento e ΔE00).
"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from backend import vision

SKIN_BGR = (145, 170, 208)
STRIA_BGR = (222, 228, 238)
PX_PER_MM = 10.0
IMAGE_W = IMAGE_H = 800


def make_synthetic_image() -> np.ndarray:
    """Pele uniforme com uma estria elíptica central (60 mm × 4 mm)."""
    image = np.full((IMAGE_H, IMAGE_W, 3), SKIN_BGR, dtype=np.uint8)
    cv2.ellipse(
        image,
        (IMAGE_W // 2, IMAGE_H // 2),
        (300, 20),
        0,
        0,
        360,
        STRIA_BGR,
        -1,
        lineType=cv2.LINE_8,
    )
    return image


def ellipse_polygon(cx: float, cy: float, a: float, b: float, n: int = 240):
    angles = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
    return [
        [float(cx + a * math.cos(t)), float(cy + b * math.sin(t))]
        for t in angles
    ]


def diff_mask(before: np.ndarray, after: np.ndarray) -> np.ndarray:
    return np.any(before != after, axis=2)


def test_preserves_dimensions_and_input():
    image = make_synthetic_image()
    original = image.copy()

    marked = vision.apply_clinical_watermark(
        image, "2026-11-21T18:07:00", "T2", "PAC-01"
    )

    assert marked is not image
    assert marked.shape == image.shape
    assert marked.dtype == np.uint8
    assert np.array_equal(image, original), "a imagem de entrada não pode ser mutada"


def test_only_bottom_right_band_changes():
    image = make_synthetic_image()
    marked = vision.apply_clinical_watermark(
        image, "2026-11-21T18:07:00", "T0", "PAC-07"
    )

    changed = diff_mask(image, marked)
    assert changed.any(), "a marca d'água precisa alterar algum pixel"

    rows, cols = np.where(changed)
    height, width = image.shape[:2]
    # Faixa confinada ao canto inferior direito.
    assert rows.min() >= int(height * 0.75), (rows.min(), height)
    assert cols.min() >= int(width * 0.25), (cols.min(), width)
    # E não vaza para fora da imagem.
    assert rows.max() < height and cols.max() < width


def test_center_region_untouched():
    image = make_synthetic_image()
    marked = vision.apply_clinical_watermark(image, None, "T3")
    height, width = image.shape[:2]
    assert np.array_equal(
        image[: int(height * 0.7), : int(width * 0.7)],
        marked[: int(height * 0.7), : int(width * 0.7)],
    )


def test_text_omits_missing_fields():
    assert vision.watermark_text(None, "T0") == "DermaMetric · T0"
    assert (
        vision.watermark_text("2026-11-21T18:07:00", "T1", "PAC-02")
        == "DermaMetric · PAC-02 · T1 · 21/11/2026 18:07"
    )
    assert (
        vision.watermark_text("2026-11-21 18:07:00", "T1", None)
        == "DermaMetric · T1 · 21/11/2026 18:07"
    )


def test_invalid_datetime_is_omitted():
    assert vision.watermark_text("sem-data", "T4", "PAC-09") == "DermaMetric · PAC-09 · T4"


def test_does_not_change_stria_measurements(tmp_path):
    image = make_synthetic_image()
    marked = vision.apply_clinical_watermark(
        image, "2026-11-21T18:07:00", "T2", "PAC-01"
    )
    clean_path = tmp_path / "clean.png"
    marked_path = tmp_path / "marked.png"
    vision.write_image_bgr(clean_path, image)
    vision.write_image_bgr(marked_path, marked)

    polygon = ellipse_polygon(IMAGE_W / 2, IMAGE_H / 2, 300, 20)
    clean_metrics = vision.measure_stria_polygon(str(clean_path), polygon, PX_PER_MM)
    marked_metrics = vision.measure_stria_polygon(str(marked_path), polygon, PX_PER_MM)

    # A tarja fica fora do polígono/anel de pele: métricas idênticas.
    assert marked_metrics == pytest.approx(clean_metrics, rel=0, abs=0)
    for key in ("area_mm2", "mean_width_mm", "max_width_mm", "length_mm"):
        assert marked_metrics[key] == clean_metrics[key]
    assert marked_metrics["delta_e_00"] == clean_metrics["delta_e_00"]


def test_handles_tiny_and_odd_images():
    tiny = np.full((12, 12, 3), 180, dtype=np.uint8)
    marked = vision.apply_clinical_watermark(tiny, None, "T0")
    assert marked.shape == tiny.shape
    assert np.array_equal(marked, tiny)  # pequena demais: nada a desenhar

    narrow = np.full((400, 40, 3), 180, dtype=np.uint8)
    marked_narrow = vision.apply_clinical_watermark(narrow, None, "T0")
    assert marked_narrow.shape == narrow.shape


def test_rejects_non_bgr_image():
    with pytest.raises(ValueError):
        vision.apply_clinical_watermark(np.zeros((10, 10), dtype=np.uint8), None, "T0")
