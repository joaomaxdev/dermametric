"""Testes do refinamento elástico local das referências de T0.

A homografia global alinha a foto inteira, mas a pele conserva
microdeformações elásticas entre T0 e T1–Tn. ``refine_t0_polygons_local``
estima um warp local por ECC sobre a textura (L* + CLAHE) e só aplica o
ajuste quando a correlação e o deslocamento são seguros.

Cobrem:

* encaixe de um deslocamento local sintético de ~3 mm;
* trava de correlação baixa;
* trava de deslocamento acima de 10 mm;
* ausência de escala px/mm.
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from backend import vision

PX_PER_MM = 10.0
SIZE = 600


def _textured_skin(size: int = SIZE, seed: int = 7) -> np.ndarray:
    """Pele sintética com microtextura reprodutível e uma estria elíptica."""
    rng = np.random.default_rng(seed)
    base = np.full((size, size, 3), (150, 170, 200), dtype=np.float32)
    noise = rng.normal(0, 18, (size, size, 1)).repeat(3, axis=2)
    noise = cv2.GaussianBlur(noise, (0, 0), 1.2)
    image = np.clip(base + noise, 0, 255).astype(np.uint8)
    cv2.ellipse(
        image,
        (size // 2, size // 2),
        (80, 20),
        0,
        0,
        360,
        (60, 60, 140),
        -1,
        lineType=cv2.LINE_8,
    )
    return image


def _ellipse_polygon(cx: float, cy: float, a: float = 80, b: float = 20, n: int = 48):
    angles = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
    return [
        [float(cx + a * math.cos(t)), float(cy + b * math.sin(t))]
        for t in angles
    ]


def _write(path: Path, image: np.ndarray) -> str:
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    path.write_bytes(buffer.tobytes())
    return str(path)


def _shift_region(
    image: np.ndarray,
    dx: int,
    dy: int = 0,
    box: tuple[int, int, int, int] = (110, 170, 450, 430),
) -> np.ndarray:
    """Desloca uma janela ampla (textura + estria) para a direita/baixo.

    A janela cobre toda a área de busca com margem de 90 px, de modo que o
    conteúdo visto pelo alinhamento seja efetivamente deslocado — como numa
    microdeformação elástica real.
    """
    x0, y0, x1, y1 = box
    out = image.copy()
    out[y0 + dy : y1 + dy, x0 + dx : x1 + dx] = image[y0:y1, x0:x1]
    return out


def _centroid(points) -> np.ndarray:
    return np.asarray(points, dtype=np.float64).mean(axis=0)


def test_refine_follows_local_elastic_shift_of_three_mm(tmp_path):
    t0 = _textured_skin()
    polygon = _ellipse_polygon(SIZE / 2, SIZE / 2)
    # 3 mm a 10 px/mm = 30 px de deformação local residual.
    current = _shift_region(t0, dx=int(3 * PX_PER_MM))

    reference_path = _write(tmp_path / "t0.png", t0)
    current_path = _write(tmp_path / "t1.png", current)

    result = vision.refine_t0_polygons_local(
        reference_path,
        current_path,
        {"L1": polygon},
        PX_PER_MM,
    )["L1"]

    assert result["applied"] is True
    assert result["reason"] == "ok"
    assert result["score"] >= vision.LOCAL_REFINE_MIN_SCORE
    assert result["cc"] is not None
    assert result["shift_mm"] == pytest.approx(3.0, abs=0.7)
    assert result["motion"] in {"coarse_translation", "euclidean", "translation"}

    delta = _centroid(result["polygon"]) - _centroid(polygon)
    assert delta[0] == pytest.approx(30.0, abs=4.0)
    assert abs(delta[1]) <= 3.0
    # O polígono de referência permanece rastreável no metadado.
    assert result["reference_polygon"] == polygon


def test_coarse_to_fine_finds_stria_shift_despite_periodic_wrinkles(tmp_path):
    """ECC puro fica preso nas rugas; a busca grossa acha os ~6 mm reais."""
    size = 800
    px_per_mm = 6.0
    rng = np.random.default_rng(3)
    image = np.full((size, size, 3), (150, 170, 200), dtype=np.float32)
    noise = cv2.GaussianBlur(
        rng.normal(0, 10, (size, size, 1)).repeat(3, axis=2), (0, 0), 1.0
    )
    image = np.clip(image + noise, 0, 255).astype(np.uint8)
    # Rugas horizontais periódicas (13 px): a textura não codifica posição em x.
    rows = np.arange(size)[:, None]
    wrinkles = (8 * np.sin(2 * np.pi * rows / 13.0)).astype(np.float32)
    image = np.clip(image.astype(np.float32) + wrinkles[..., None], 0, 255).astype(np.uint8)
    cv2.ellipse(
        image, (400, 400), (120, 25), 0, 0, 360, (70, 80, 150), -1,
        lineType=cv2.LINE_8,
    )
    polygon = _ellipse_polygon(400, 400, a=120, b=25, n=64)

    # 35 px a 6 px/mm ≈ 5,8 mm de deslocamento local lateral.
    shifted = _shift_region(
        image, dx=35, box=(20, 250, 740, 550)
    )
    reference_path = _write(tmp_path / "t0.png", image)
    current_path = _write(tmp_path / "t1.png", shifted)

    result = vision.refine_t0_polygons_local(
        reference_path, current_path, {"L1": polygon}, px_per_mm
    )["L1"]

    assert result["applied"] is True
    assert result["reason"] == "ok"
    assert result["coarse_score"] >= vision.LOCAL_REFINE_MIN_SCORE
    assert result["shift_mm"] == pytest.approx(35 / px_per_mm, abs=0.6)

    delta = _centroid(result["polygon"]) - _centroid(polygon)
    # Erro de posição < 3 px em relação à estria real.
    assert delta[0] == pytest.approx(35.0, abs=3.0)
    assert abs(delta[1]) < 1.0


def test_refine_keeps_global_polygon_when_shift_exceeds_limit(tmp_path, monkeypatch):
    t0 = _textured_skin()
    polygon = _ellipse_polygon(SIZE / 2, SIZE / 2)
    paths = (
        _write(tmp_path / "t0.png", t0),
        _write(tmp_path / "t1.png", t0),
    )
    # 25 mm > 20 mm: a transformação estimada deve ser rejeitada.
    warp = np.array([[1.0, 0.0, 25.0 * PX_PER_MM], [0.0, 1.0, 0.0]], dtype=np.float64)
    monkeypatch.setattr(vision, "_coarse_template_shift", lambda *a, **k: None)
    monkeypatch.setattr(
        vision,
        "_ecc_warp_local",
        lambda *args, **kwargs: (0.92, warp, "euclidean"),
    )

    result = vision.refine_t0_polygons_local(
        paths[0], paths[1], {"L1": polygon}, PX_PER_MM
    )["L1"]

    assert result["applied"] is False
    assert result["reason"] == "shift_exceeds_limit"
    assert result["shift_mm"] == pytest.approx(25.0, abs=1e-6)
    assert result["polygon"] == polygon


def test_refine_keeps_global_polygon_when_correlation_is_low(tmp_path, monkeypatch):
    t0 = _textured_skin()
    polygon = _ellipse_polygon(SIZE / 2, SIZE / 2)
    paths = (
        _write(tmp_path / "t0.png", t0),
        _write(tmp_path / "t1.png", t0),
    )
    identity = np.eye(2, 3, dtype=np.float64)
    monkeypatch.setattr(vision, "_coarse_template_shift", lambda *a, **k: None)
    monkeypatch.setattr(
        vision,
        "_ecc_warp_local",
        lambda *args, **kwargs: (0.20, identity, "euclidean"),
    )

    result = vision.refine_t0_polygons_local(
        paths[0], paths[1], {"L1": polygon}, PX_PER_MM
    )["L1"]

    assert result["applied"] is False
    assert result["reason"] == "coarse_unavailable"
    assert result["polygon"] == polygon


def test_refine_requires_scale_and_keeps_polygon(tmp_path):
    t0 = _textured_skin()
    polygon = _ellipse_polygon(SIZE / 2, SIZE / 2)
    paths = (
        _write(tmp_path / "t0.png", t0),
        _write(tmp_path / "t1.png", t0),
    )

    results = vision.refine_t0_polygons_local(
        paths[0], paths[1], {"L1": polygon}, 0.0
    )

    assert results["L1"]["applied"] is False
    assert results["L1"]["reason"] == "missing_scale"
    assert results["L1"]["polygon"] == polygon


def test_refine_does_not_mutate_inputs(tmp_path):
    t0 = _textured_skin()
    original = t0.copy()
    polygon = _ellipse_polygon(SIZE / 2, SIZE / 2)
    polygon_copy = [list(point) for point in polygon]
    path = _write(tmp_path / "t0.png", t0)

    vision.refine_t0_polygons_local(path, path, {"L1": polygon}, PX_PER_MM)

    assert np.array_equal(t0, original)
    assert polygon == polygon_copy
