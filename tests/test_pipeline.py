"""Validação automática do pipeline DermaMetric.

Cobre:

1. Precisão matemática do motor de visão com imagens sintéticas calibradas
   (px/mm, área, larguras, comprimento e CIEDE2000 — erro < 3%).
2. Todos os endpoints FastAPI e persistência no SQLite.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

# ---------------------------------------------------------------------------
# Isolamento: banco e storage temporários ANTES de importar a aplicação.
# ---------------------------------------------------------------------------
TEST_ROOT = Path(__file__).resolve().parent / "tmp"
if TEST_ROOT.exists():
    shutil.rmtree(TEST_ROOT)
TEST_ROOT.mkdir(parents=True)
os.environ["DERMAMETRIC_STORAGE_DIR"] = str(TEST_ROOT / "storage")
os.environ["DERMAMETRIC_DB_PATH"] = str(TEST_ROOT / "storage" / "dermametric.db")

from fastapi.testclient import TestClient  # noqa: E402

from backend import database as db  # noqa: E402
from backend import auth, recovery  # noqa: E402
from backend import vision  # noqa: E402
from backend.main import app  # noqa: E402

# ---------------------------------------------------------------------------
# Geometria sintética calibrada
# ---------------------------------------------------------------------------
PX_PER_MM = 10.0
IMAGE_W = IMAGE_H = 1000
CX, CY = 500, 500
SEMI_MAJOR_PX = 400.0  # a — 40 mm
SEMI_MINOR_PX = 50.0   # b — 5 mm
SKIN_RGB = (208, 170, 145)
STRIA_RGB = (238, 228, 222)
SKIN_BGR = SKIN_RGB[::-1]
STRIA_BGR = STRIA_RGB[::-1]

# Valores analíticos esperados (mm).
EXPECTED = {
    "area_mm2": math.pi * SEMI_MAJOR_PX * SEMI_MINOR_PX / (PX_PER_MM**2),
    "max_width_mm": 2.0 * SEMI_MINOR_PX / PX_PER_MM,
    # Largura média de uma elipse = média das cordas perpendiculares = (pi/2)*b.
    "mean_width_mm": (math.pi / 2.0) * SEMI_MINOR_PX / PX_PER_MM,
    "length_mm": 2.0 * SEMI_MAJOR_PX / PX_PER_MM,
}


def ellipse_polygon(
    cx: float = CX,
    cy: float = CY,
    a: float = SEMI_MAJOR_PX,
    b: float = SEMI_MINOR_PX,
    n: int = 240,
) -> list[list[float]]:
    angles = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
    return [
        [float(cx + a * math.cos(t)), float(cy + b * math.sin(t))]
        for t in angles
    ]


def make_synthetic_image(
    skin_rgb: tuple[int, int, int] = SKIN_RGB,
    stria_rgb: tuple[int, int, int] = STRIA_RGB,
    width: int = IMAGE_W,
    height: int = IMAGE_H,
) -> np.ndarray:
    image = np.empty((height, width, 3), dtype=np.uint8)
    image[:] = skin_rgb[::-1]
    cv2.ellipse(
        image,
        (int(CX), int(CY)),
        (int(SEMI_MAJOR_PX), int(SEMI_MINOR_PX)),
        0,
        0,
        360,
        stria_rgb[::-1],
        thickness=-1,
        lineType=cv2.LINE_8,
    )
    return image


def png_bytes(image: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    return buffer.tobytes()


# ---------------------------------------------------------------------------
# Implementações independentes de referência (Sharma et al. para CIEDE2000)
# ---------------------------------------------------------------------------
def reference_lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """sRGB (0-255) -> CIELAB D65, implementação escalar independente."""
    channels = []
    for value in rgb:
        c = value / 255.0
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    x, y, z = x / 0.95047, y / 1.0, z / 1.08883

    def f(t: float) -> float:
        delta = 6.0 / 29.0
        return t ** (1.0 / 3.0) if t > delta**3 else t / (3 * delta**2) + 4.0 / 29.0

    fx, fy, fz = f(x), f(y), f(z)
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def reference_ciede2000(
    lab1: tuple[float, float, float], lab2: tuple[float, float, float]
) -> float:
    """CIEDE2000 escalar, transcrito de Sharma et al. (2005)."""
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2

    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2.0
    g = 0.5 * (1.0 - math.sqrt(c_bar**7 / (c_bar**7 + 25.0**7)))

    a1p, a2p = (1.0 + g) * a1, (1.0 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)

    h1p = math.degrees(math.atan2(b1, a1p)) % 360.0 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360.0 if c2p else 0.0

    d_lp = l2 - l1
    d_cp = c2p - c1p
    if c1p * c2p == 0.0:
        d_hp = 0.0
    else:
        d_hp = h2p - h1p
        if d_hp > 180.0:
            d_hp -= 360.0
        elif d_hp < -180.0:
            d_hp += 360.0
    d_big_hp = 2.0 * math.sqrt(c1p * c2p) * math.sin(math.radians(d_hp / 2.0))

    l_barp = (l1 + l2) / 2.0
    c_barp = (c1p + c2p) / 2.0
    if c1p * c2p == 0.0:
        h_barp = h1p + h2p
    elif abs(h1p - h2p) <= 180.0:
        h_barp = (h1p + h2p) / 2.0
    elif h1p + h2p < 360.0:
        h_barp = (h1p + h2p + 360.0) / 2.0
    else:
        h_barp = (h1p + h2p - 360.0) / 2.0

    t = (
        1.0
        - 0.17 * math.cos(math.radians(h_barp - 30.0))
        + 0.24 * math.cos(math.radians(2.0 * h_barp))
        + 0.32 * math.cos(math.radians(3.0 * h_barp + 6.0))
        - 0.20 * math.cos(math.radians(4.0 * h_barp - 63.0))
    )
    d_theta = 30.0 * math.exp(-(((h_barp - 275.0) / 25.0) ** 2))
    r_c = 2.0 * math.sqrt(c_barp**7 / (c_barp**7 + 25.0**7))
    r_t = -math.sin(math.radians(2.0 * d_theta)) * r_c

    s_l = 1.0 + (0.015 * (l_barp - 50.0) ** 2) / math.sqrt(
        20.0 + (l_barp - 50.0) ** 2
    )
    s_c = 1.0 + 0.045 * c_barp
    s_h = 1.0 + 0.015 * c_barp * t

    term_l = d_lp / s_l
    term_c = d_cp / s_c
    term_h = d_big_hp / s_h
    return math.sqrt(
        term_l**2 + term_c**2 + term_h**2 + r_t * term_c * term_h
    )


def rel_error(measured: float, expected: float) -> float:
    return abs(measured - expected) / abs(expected)


# ---------------------------------------------------------------------------
# 1. Calibração
# ---------------------------------------------------------------------------
def test_compute_px_per_mm():
    assert vision.compute_px_per_mm((0, 0), (300, 0), 30.0) == pytest.approx(10.0)
    assert vision.compute_px_per_mm([10, 10], [40, 50], 5.0) == pytest.approx(10.0)
    with pytest.raises(ValueError):
        vision.compute_px_per_mm((0, 0), (0, 0), 10.0)
    with pytest.raises(ValueError):
        vision.compute_px_per_mm((0, 0), (10, 0), 0.0)


# ---------------------------------------------------------------------------
# 2. Cor: CIELAB e CIEDE2000
# ---------------------------------------------------------------------------
def test_cielab_conversion_matches_reference():
    for rgb in [
        (255, 255, 255),
        (0, 0, 0),
        (128, 128, 128),
        SKIN_RGB,
        STRIA_RGB,
    ]:
        bgr = np.array([[rgb[::-1]]], dtype=np.uint8)
        lab = vision.bgr_to_cielab_float(bgr)[0, 0]
        expected = reference_lab(rgb)
        assert lab == pytest.approx(expected, abs=1e-6)

    # Round-trip BGR -> Lab -> BGR sem perda perceptível.
    rng = np.random.default_rng(42)
    colors = rng.integers(0, 256, size=(64, 1, 3), dtype=np.uint8)
    roundtrip = vision.cielab_to_bgr_u8(vision.bgr_to_cielab_float(colors))
    assert np.max(np.abs(roundtrip.astype(int) - colors.astype(int))) <= 1

    white = vision.bgr_to_cielab_float(
        np.full((1, 1, 3), 255, dtype=np.uint8)
    )[0, 0]
    assert white[0] == pytest.approx(100.0, abs=1e-3)
    assert white[1] == pytest.approx(0.0, abs=1e-3)
    assert white[2] == pytest.approx(0.0, abs=1e-3)


@pytest.mark.parametrize(
    ("lab1", "lab2", "expected"),
    [
        ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
        ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
        ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
        ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
        ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
        ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
        ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
        ((50.0000, 2.5000, 0.0000), (61.0000, -5.0000, 29.0000), 22.8977),
        ((50.0000, 2.5000, 0.0000), (56.0000, -27.0000, -3.0000), 31.9030),
        ((50.0000, 2.5000, 0.0000), (58.0000, 24.0000, 15.0000), 19.4535),
    ],
)
def test_ciede2000_sharma_reference_pairs(lab1, lab2, expected):
    measured = vision.delta_e_ciede2000(np.array(lab1), np.array(lab2))
    assert measured == pytest.approx(expected, abs=1e-4)


def test_ciede2000_matches_independent_scalar_implementation():
    rng = np.random.default_rng(7)
    labs1 = rng.uniform(0, 100, size=(25, 3))
    labs1[:, 1:] = rng.uniform(-80, 80, size=(25, 2))
    labs2 = rng.uniform(0, 100, size=(25, 3))
    labs2[:, 1:] = rng.uniform(-80, 80, size=(25, 2))
    vectorized = vision.delta_e_ciede2000(labs1, labs2)
    scalar = [
        reference_ciede2000(tuple(a), tuple(b))
        for a, b in zip(labs1, labs2)
    ]
    assert np.allclose(vectorized, scalar, rtol=1e-10, atol=1e-10)


# ---------------------------------------------------------------------------
# 3. Morfometria sintética
# ---------------------------------------------------------------------------
def test_measure_synthetic_stria_accuracy():
    image = make_synthetic_image()
    image_path = TEST_ROOT / "synthetic_stria.png"
    vision.write_image_bgr(image_path, image)

    metrics = vision.measure_stria_polygon(
        str(image_path), ellipse_polygon(), PX_PER_MM
    )
    print("\nMétricas sintéticas:", metrics)

    for key in ("area_mm2", "mean_width_mm", "max_width_mm", "length_mm"):
        error = rel_error(metrics[key], EXPECTED[key])
        assert error < 0.03, (
            f"{key}: medido={metrics[key]:.4f} esperado={EXPECTED[key]:.4f} "
            f"erro={error:.2%}"
        )

    expected_delta = reference_ciede2000(
        reference_lab(SKIN_RGB), reference_lab(STRIA_RGB)
    )
    assert rel_error(metrics["delta_e_00"], expected_delta) < 0.03, (
        f"delta_e_00: medido={metrics['delta_e_00']:.4f} "
        f"esperado={expected_delta:.4f}"
    )
    print("delta_e_00 esperado:", expected_delta)


# ---------------------------------------------------------------------------
# 4. Alinhamento por homografia
# ---------------------------------------------------------------------------
def test_align_to_reference_translation():
    source = np.zeros((400, 400, 3), dtype=np.uint8)
    cv2.rectangle(source, (100, 100), (120, 120), (0, 0, 255), thickness=-1)
    source_path = TEST_ROOT / "align_source.png"
    vision.write_image_bgr(source_path, source)

    src_points = [[100, 100], [200, 100], [100, 200], [200, 200]]
    ref_points = [[120, 110], [220, 110], [120, 210], [220, 210]]
    output_path = TEST_ROOT / "align_output.png"

    vision.align_to_reference(
        str(source_path), src_points, ref_points, (400, 400), str(output_path)
    )
    aligned = vision.read_image_bgr(output_path)
    assert aligned.shape == (400, 400, 3)

    # A homografia devolvida leva as coordenadas da sessão atual a T0.
    _, homography = vision.align_to_reference(
        str(source_path),
        src_points,
        ref_points,
        (400, 400),
        str(output_path),
        return_homography=True,
    )
    assert homography is not None and homography.shape == (3, 3)
    assert vision.project_roi([100, 100, 20, 20], homography, (400, 400)) == [
        120,
        110,
        20,
        20,
    ]

    # O marcador vermelho deve ter se deslocado em (+20, +10).
    marker = aligned[115:126, 125:136]
    assert marker[..., 2].mean() > 200 and marker[..., 0].mean() < 40
    assert aligned[105, 105, 2] < 30

    # Sessão de referência: apenas padroniza o shape.
    reference_out = TEST_ROOT / "align_reference.png"
    vision.align_to_reference(
        str(source_path),
        src_points,
        src_points,
        (200, 300),
        str(reference_out),
        is_reference=True,
    )
    reference = vision.read_image_bgr(reference_out)
    assert reference.shape == (200, 300, 3)


# ---------------------------------------------------------------------------
# 5. Normalização de exposição + CLAHE
# ---------------------------------------------------------------------------
def test_normalize_exposure_and_clahe(tmp_path):
    reference_image = make_synthetic_image()
    darker_image = make_synthetic_image(skin_rgb=(168, 132, 108), stria_rgb=(196, 185, 178))
    ref_path = tmp_path / "ref.png"
    dark_path = tmp_path / "dark.png"
    vision.write_image_bgr(ref_path, reference_image)
    vision.write_image_bgr(dark_path, darker_image)

    roi = [40, 40, 120, 120]
    ref_means = vision.compute_region_lab_means(str(ref_path), roi)
    assert ref_means == pytest.approx(
        tuple(vision.bgr_to_cielab_float(reference_image)[roi[1] : roi[1] + roi[3], roi[0] : roi[0] + roi[2]].reshape(-1, 3).mean(axis=0)),
        abs=1e-6,
    )

    enhanced_path = tmp_path / "enhanced.png"
    normalized_path = tmp_path / "normalized.png"
    returned_path, input_means = vision.normalize_and_enhance_cielab(
        str(dark_path),
        roi,
        ref_means,
        str(enhanced_path),
        normalized_output_path=str(normalized_path),
    )
    assert returned_path == str(enhanced_path)
    assert enhanced_path.exists() and normalized_path.exists()
    assert input_means != ref_means  # a imagem escura difere da referência

    normalized_means = vision.compute_region_lab_means(str(normalized_path), roi)
    for channel, (got, want) in enumerate(zip(normalized_means, ref_means)):
        assert abs(got - want) < 1.0, (
            f"canal {channel}: normalizado={got:.3f} referência={want:.3f}"
        )


def test_project_roi_uses_full_rectangle_and_bounds(tmp_path):
    """A ROI é projetada pelos 4 cantos da homografia, com fallback fora da imagem."""
    scale = np.array([[2.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 1.0]])
    assert vision.project_roi([10, 20, 30, 40], scale, (400, 400)) == [20, 40, 60, 80]

    translate = np.array([[1.0, 0.0, 25.0], [0.0, 1.0, -15.0], [0.0, 0.0, 1.0]])
    assert vision.project_roi([40, 40, 120, 120], translate, (400, 400)) == [
        65,
        25,
        120,
        120,
    ]

    outside = np.array([[1.0, 0.0, 500.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    assert vision.project_roi([40, 40, 120, 120], outside, (400, 400)) is None

    partial = np.array([[1.0, 0.0, 360.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    assert vision.project_roi([40, 40, 120, 120], partial, (400, 400)) is None
    with pytest.raises(ValueError):
        vision.project_roi([0, 0, 10, 10], np.eye(2), (400, 400))


def test_normalize_preserves_natural_tonal_range(tmp_path):
    """A correção fotométrica não lava a imagem nem estoura altas luzes."""
    image = make_synthetic_image()
    # Realce especular claro e canto escuro fora da ROI de pele saudável.
    image[600:720, 430:570] = (248, 246, 243)
    image[900:1000, 900:1000] = (16, 14, 13)
    path = tmp_path / "natural_input.png"
    vision.write_image_bgr(path, image)

    roi = [40, 40, 120, 120]
    current_means = vision.compute_region_lab_means(str(path), roi)
    reference_means = (
        current_means[0] + 8.0,
        current_means[1] - 1.0,
        current_means[2] + 1.5,
    )
    enhanced_path = tmp_path / "natural_enhanced.png"
    normalized_path = tmp_path / "natural_normalized.png"
    vision.normalize_and_enhance_cielab(
        str(path),
        roi,
        reference_means,
        str(enhanced_path),
        normalized_output_path=str(normalized_path),
    )

    normalized_means = vision.compute_region_lab_means(str(normalized_path), roi)
    for channel, (got, want) in enumerate(zip(normalized_means, reference_means)):
        assert abs(got - want) < 1.0, (
            f"canal {channel}: normalizado={got:.3f} referência={want:.3f}"
        )

    lab_in = vision.bgr_to_cielab_float(image)
    lab_out = vision.bgr_to_cielab_float(vision.read_image_bgr(normalized_path))
    # Contraste global preservado (sem lavagem) e sem saturação nova.
    assert lab_out[..., 0].std() > 0.85 * lab_in[..., 0].std()
    assert float(lab_out[..., 0].max()) < 99.5
    blown_in = float(np.mean(lab_in[..., 0] >= 99.9))
    blown_out = float(np.mean(lab_out[..., 0] >= 99.9))
    assert blown_out <= blown_in + 1e-6


def test_normalize_clamps_extreme_shifts(tmp_path):
    """Deslocamentos extremos são limitados suavemente em vez de estourar a imagem."""
    image = make_synthetic_image(skin_rgb=(168, 132, 108), stria_rgb=(196, 185, 178))
    path = tmp_path / "extreme_input.png"
    vision.write_image_bgr(path, image)
    roi = [40, 40, 120, 120]
    current_means = vision.compute_region_lab_means(str(path), roi)
    extreme_reference = (current_means[0] + 45.0, current_means[1] - 30.0, current_means[2] + 30.0)

    normalized_path = tmp_path / "extreme_normalized.png"
    vision.normalize_and_enhance_cielab(
        str(path),
        roi,
        extreme_reference,
        str(tmp_path / "extreme_enhanced.png"),
        normalized_output_path=str(normalized_path),
    )

    normalized_means = vision.compute_region_lab_means(str(normalized_path), roi)
    assert normalized_means[0] - current_means[0] <= vision.MAX_L_SHIFT + 0.5
    assert abs(normalized_means[1] - current_means[1]) <= vision.MAX_CHROMA_SHIFT + 0.5
    assert abs(normalized_means[2] - current_means[2]) <= vision.MAX_CHROMA_SHIFT + 0.5
    lab_out = vision.bgr_to_cielab_float(vision.read_image_bgr(normalized_path))
    assert float(lab_out[..., 0].max()) < 99.5


# ---------------------------------------------------------------------------
# 6. API FastAPI + persistência
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def upload_session_images(client, session_id: str, with_ruler: bool = True):
    clean = make_synthetic_image()
    files = {"clean_image": (f"{session_id}_clean.png", png_bytes(clean), "image/png")}
    if with_ruler:
        ruler = make_synthetic_image()
        files["ruler_image"] = (f"{session_id}_ruler.png", png_bytes(ruler), "image/png")
    response = client.post(
        f"/api/sessions/{session_id}/upload",
        files=files,
        data={"label": f"Teste {session_id}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def pdf_text(pdf_bytes: bytes) -> str:
    """Extrai o texto do PDF via poppler para validar conteúdo do laudo."""
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext (poppler-utils) indisponível para validar o laudo.")
    result = subprocess.run(
        ["pdftotext", "-", "-"],
        input=pdf_bytes,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout.decode("utf-8", "replace")


def test_api_full_flow_and_persistence(client):
    # Healthcheck.
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    # As quatro sessões pré-populadas.
    sessions = client.get("/api/sessions").json()
    assert sessions["count"] == 4
    assert [s["id"] for s in sessions["sessions"]] == ["T0", "T2", "T3", "T4"]

    # Upload de imagens em todas as sessões.
    for session_id in ("T0", "T2", "T3", "T4"):
        upload = upload_session_images(client, session_id)
        assert upload["session"]["clean_image_url"].startswith("/api/files/uploads/")
        assert upload["session"]["ruler_image_url"].startswith("/api/files/uploads/")
        assert upload["session"]["label"] == f"Teste {session_id}"
        assert upload["captured_at"]

    # Upload vazio deve falhar.
    empty_upload = client.post("/api/sessions/T0/upload", data={})
    assert empty_upload.status_code == 400

    # Sessão inválida.
    assert client.get("/api/sessions/T9").status_code == 404

    # Registro antes de T0 existir deve falhar (T2 depende de T0).
    landmarks = [[150, 150], [850, 150], [150, 850], [850, 850]]
    register_t2_early = client.post(
        "/api/sessions/T2/register",
        json={"landmarks": landmarks, "healthy_skin_roi": [40, 40, 120, 120]},
    )
    assert register_t2_early.status_code == 400

    # Calibração (300 px = 30 mm -> 10 px/mm) em todas as sessões.
    for session_id in ("T0", "T2", "T3", "T4"):
        calibration = client.post(
            f"/api/sessions/{session_id}/calibrate",
            json={"p1": [100, 900], "p2": [400, 900], "known_mm": 30.0},
        )
        assert calibration.status_code == 200, calibration.text
        assert calibration.json()["px_per_mm"] == pytest.approx(PX_PER_MM)

    # Registro T0 (referência) e demais sessões.
    register_responses = {}
    for session_id in ("T0", "T2", "T3", "T4"):
        response = client.post(
            f"/api/sessions/{session_id}/register",
            json={
                "landmarks": landmarks,
                "healthy_skin_roi": [40, 40, 120, 120],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["aligned_url"].startswith("/api/files/processed/")
        assert body["enhanced_url"].startswith("/api/files/processed/")
        assert body["roi"]["input"] == [40, 40, 120, 120]
        assert body["roi"]["normalization"] == pytest.approx(
            [40, 40, 120, 120], abs=1
        )
        register_responses[session_id] = body

    # As imagens processadas são servidas descriptografadas pela API.
    for url in (
        register_responses["T0"]["aligned_url"],
        register_responses["T0"]["enhanced_url"],
    ):
        asset = client.get(url)
        assert asset.status_code == 200
        assert asset.headers["content-type"].startswith("image/")

    # Medição de E1_maior em T0 e T2.
    polygon = ellipse_polygon()
    for session_id in ("T0", "T2"):
        response = client.post(
            f"/api/sessions/{session_id}/measure",
            json={"stria_code": "E1_maior", "polygon": polygon},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        for key in ("area_mm2", "mean_width_mm", "max_width_mm", "length_mm"):
            assert rel_error(body[key], EXPECTED[key]) < 0.03, (
                f"{session_id} {key}: {body[key]} vs {EXPECTED[key]}"
            )
        assert body["delta_e_00"] > 1.0
        assert body["measurement"]["polygon"] == polygon

    # Re-medição substitui a anterior (sem duplicar histórico).
    again = client.post(
        "/api/sessions/T0/measure",
        json={"stria_code": "E1_maior", "polygon": polygon},
    )
    assert again.status_code == 200

    # Escalas clínicas.
    scales = {
        "end_pain": 3,
        "likert_satisfaction": 4,
        "gais_score": 2,
        "notes": "Boa resposta, leve hipercromia residual.",
    }
    response = client.post("/api/sessions/T0/clinical-scales", json=scales)
    assert response.status_code == 200, response.text
    assert response.json()["clinical_scales"] == scales

    # Validações de entrada.
    assert (
        client.post(
            "/api/sessions/T0/measure",
            json={"stria_code": "E9_invalida", "polygon": polygon},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/sessions/T0/register",
            json={"landmarks": landmarks[:3], "healthy_skin_roi": [0, 0, 10, 10]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/sessions/T0/clinical-scales",
            json={"end_pain": 11, "likert_satisfaction": 4, "gais_score": 1},
        ).status_code
        == 422
    )

    # Persistência e leitura consolidada.
    listed = client.get("/api/sessions").json()["sessions"]
    by_id = {session["id"]: session for session in listed}
    assert len(by_id["T0"]["measurements"]) == 1
    assert len(by_id["T2"]["measurements"]) == 1
    assert by_id["T0"]["measurements"][0]["stria_code"] == "E1_maior"
    assert by_id["T0"]["clinical_scales"] == scales
    assert by_id["T0"]["px_per_mm"] == pytest.approx(PX_PER_MM)

    # Verificação direta no SQLite.
    assert db.DB_PATH.exists()
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        measurement_rows = connection.execute(
            "SELECT * FROM striae_measurements ORDER BY session_id"
        ).fetchall()
        session_rows = connection.execute("SELECT * FROM sessions").fetchall()

    assert len(measurement_rows) == 2
    assert {row["session_id"] for row in measurement_rows} == {"T0", "T2"}
    assert all(row["delta_e_00"] > 0 for row in measurement_rows)
    assert len(session_rows) == 4


# ---------------------------------------------------------------------------
# 7. Edição granular, EXIF, landmarks nomeados e criptografia em repouso
# ---------------------------------------------------------------------------
def test_custom_named_landmarks(client):
    """Landmarks são persistidos como objetos {label, x, y} e devolvidos pela API."""
    named = [
        {"label": "Umbigo", "x": 150.5, "y": 150.25},
        {"label": "Nevo lateral D", "x": 850.0, "y": 150.0},
        {"label": "Cicatriz", "x": 150.0, "y": 850.0},
        {"label": "Borda costal", "x": 850.0, "y": 850.0},
    ]
    response = client.post(
        "/api/sessions/T0/register",
        json={"landmarks": named, "healthy_skin_roi": [40, 40, 120, 120]},
    )
    assert response.status_code == 200, response.text

    session = client.get("/api/sessions/T0").json()
    assert [point["label"] for point in session["landmarks"]] == [
        "Umbigo", "Nevo lateral D", "Cicatriz", "Borda costal",
    ]
    assert session["landmarks"][0]["x"] == pytest.approx(150.5)
    assert session["landmarks"][0]["y"] == pytest.approx(150.25)

    # Registro de seguimento aceita os mesmos objetos (nomes herdados de T0).
    follow = client.post(
        "/api/sessions/T2/register",
        json={"landmarks": named, "healthy_skin_roi": [40, 40, 120, 120]},
    )
    assert follow.status_code == 200, follow.text
    inherited = client.get("/api/sessions/T2").json()["landmarks"]
    assert [point["label"] for point in inherited] == [point["label"] for point in named]


def test_delete_single_measurement(client):
    """DELETE /measurements/{code} apaga apenas a região escolhida."""
    polygon = ellipse_polygon()
    client.post(
        "/api/sessions/T0/measure",
        json={"stria_code": "L2", "polygon": polygon},
    )
    before = client.get("/api/sessions/T0").json()["measurements"]
    assert {"L2", "E1_maior"}.issubset({m["stria_code"] for m in before})

    response = client.delete("/api/sessions/T0/measurements/L2")
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    after = client.get("/api/sessions/T0").json()["measurements"]
    assert all(m["stria_code"] != "L2" for m in after)
    assert any(m["stria_code"] == "E1_maior" for m in after)

    # Remover novamente devolve 404 sem afetar as demais.
    assert client.delete("/api/sessions/T0/measurements/L2").status_code == 404

    # Limpeza total da sessão.
    clear = client.delete("/api/sessions/T2/measurements")
    assert clear.status_code == 200, clear.text
    assert clear.json()["deleted"] >= 1
    assert client.get("/api/sessions/T2").json()["measurements"] == []

    # Restaura uma medição em T0 para os próximos testes.
    restored = client.post(
        "/api/sessions/T0/measure",
        json={"stria_code": "L1", "polygon": polygon},
    )
    assert restored.status_code == 200, restored.text


def _jpeg_with_exif(datetime_original: str) -> bytes:
    image = make_synthetic_image()
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)
    exif = pil.getexif()
    exif[36867] = datetime_original  # DateTimeOriginal
    buffer = io.BytesIO()
    pil.save(buffer, format="JPEG", quality=92, exif=exif)
    return buffer.getvalue()


def test_exif_capture_date_and_manual_fallback(client):
    """Data de captura vem do EXIF; sem EXIF aceita informar manualmente."""
    payload = _jpeg_with_exif("2025:03:12 09:15:00")
    response = client.post(
        "/api/sessions/T3/upload",
        files={"clean_image": ("exif.jpg", payload, "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["captured_at"] == "2025-03-12 09:15:00"
    assert client.get("/api/sessions/T3").json()["captured_at"] == "2025-03-12 09:15:00"

    # JPEG sem EXIF: usa a data manual informada no upload.
    plain = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 180, 170)).save(plain, format="JPEG")
    manual = client.post(
        "/api/sessions/T4/upload",
        files={"clean_image": ("sem_exif.jpg", plain.getvalue(), "image/jpeg")},
        data={"captured_at": "2024-01-05"},
    )
    assert manual.status_code == 200, manual.text
    assert manual.json()["captured_at"] == "2024-01-05 00:00:00"

    # PATCH permite corrigir a data e a descrição de uma região.
    edited = client.patch(
        "/api/sessions/T4",
        json={"captured_at": "2024-02-10", "lesion_labels": {"L1": "Estria abdominal direita"}},
    )
    assert edited.status_code == 200, edited.text
    session = edited.json()["session"]
    assert session["captured_at"] == "2024-02-10 00:00:00"
    assert session["lesion_labels"]["L1"] == "Estria abdominal direita"


def test_encryption_at_rest(client):
    """Arquivos e campos sensíveis ficam cifrados no disco, mas legíveis na API."""
    key_file = Path(db.STORAGE_DIR) / ".secret.key"
    assert key_file.exists()
    if os.name != "nt":
        # O Windows só representa o bit somente-leitura via chmod; o modo POSIX
        # 0o600 não é aplicável lá (a proteção real é a ACL do perfil do usuário).
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

    with sqlite3.connect(db.DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM sessions WHERE id = 'T0'").fetchone()
        measurement = connection.execute(
            "SELECT * FROM striae_measurements WHERE session_id = 'T0' LIMIT 1"
        ).fetchone()

    assert row["label"].startswith("enc:v1:")
    assert "Teste T0" not in row["label"]
    assert row["landmarks_json"].startswith("enc:v1:")
    assert "Umbigo" not in row["landmarks_json"]
    assert row["clinical_scales_json"].startswith("enc:v1:")
    assert "hipercromia" not in row["clinical_scales_json"]
    assert measurement["polygon_json"].startswith("enc:v1:")

    # Enquanto isso, a API devolve os valores descriptografados.
    session = client.get("/api/sessions/T0").json()
    assert session["label"] == "Teste T0"
    assert session["landmarks"][0]["label"] == "Umbigo"
    assert session["clinical_scales"]["notes"].startswith("Boa resposta")

    # Imagens em disco não têm assinatura legível (PNG/JPEG).
    uploads = sorted(db.UPLOAD_DIR.glob("*.enc"))
    processed = sorted(db.PROCESSED_DIR.glob("*.enc"))
    assert uploads and processed
    for path in (uploads[0], processed[0]):
        raw = path.read_bytes()
        assert not raw.startswith(b"\x89PNG")
        assert not raw.startswith(b"\xff\xd8\xff")
        assert b"IHDR" not in raw[:1024]

    # Servidas descriptografadas e com assinatura de imagem válida.
    clean_url = session["clean_image_url"]
    asset = client.get(clean_url)
    assert asset.status_code == 200
    assert asset.content.startswith(b"\x89PNG")


def test_watermarked_processed_images(client):
    """Imagens processadas trazem tarja por padrão; o disco e o motor não mudam."""
    session = client.get("/api/sessions/T0").json()
    aligned_url = session["aligned_image_url"]
    assert "watermark=1" in aligned_url
    assert "session=" in aligned_url

    processed = sorted(db.PROCESSED_DIR.glob("*.enc"))
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in processed}

    marked_response = client.get(aligned_url)
    assert marked_response.status_code == 200
    assert marked_response.headers["content-type"].startswith("image/png")
    marked = cv2.imdecode(
        np.frombuffer(marked_response.content, dtype=np.uint8), cv2.IMREAD_COLOR
    )

    plain_response = client.get(aligned_url.replace("watermark=1", "watermark=0"))
    assert plain_response.status_code == 200
    plain = cv2.imdecode(
        np.frombuffer(plain_response.content, dtype=np.uint8), cv2.IMREAD_COLOR
    )

    assert marked.shape == plain.shape
    diff = np.any(marked != plain, axis=2)
    assert diff.any()
    rows, cols = np.where(diff)
    height, width = plain.shape[:2]
    assert rows.min() >= int(height * 0.75)
    assert cols.min() >= int(width * 0.4)

    # O PNG servido com tarja não pode alterar o cálculo do motor CIELAB.
    marked_path = TEST_ROOT / "served_marked.png"
    plain_path = TEST_ROOT / "served_plain.png"
    vision.write_image_bgr(marked_path, marked)
    vision.write_image_bgr(plain_path, plain)
    polygon = ellipse_polygon()
    assert vision.measure_stria_polygon(str(marked_path), polygon, PX_PER_MM) == pytest.approx(
        vision.measure_stria_polygon(str(plain_path), polygon, PX_PER_MM), rel=0, abs=0
    )

    # O arquivo cifrado em disco permanece byte a byte idêntico.
    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in processed}
    assert after == before


def test_profile_settings_roundtrip(client):
    """Perfil do profissional: GET vazio, PUT persistente e validação de campos."""
    empty = client.get("/api/profile")
    assert empty.status_code == 200
    assert empty.json() == {
        "professional_name": "",
        "professional_registry": "",
        "institution": "",
    }

    payload = {
        "professional_name": "Dra. Helena Prado",
        "professional_registry": "CREFITO-6 54321-F",
        "institution": "Clinica Aurora",
    }
    saved = client.put("/api/profile", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json() == payload

    fetched = client.get("/api/profile")
    assert fetched.status_code == 200
    assert fetched.json() == payload

    # Campos longos ou payload sem campos conhecidos são rejeitados.
    assert (
        client.put("/api/profile", json={"professional_name": "x" * 121}).status_code
        == 400
    )
    assert (
        client.put("/api/profile", json={"professional_registry": "x" * 81}).status_code
        == 400
    )
    assert client.put("/api/profile", json={}).status_code == 400
    assert client.put("/api/profile", json={"desconhecido": "valor"}).status_code == 400

    # Limpa o perfil para não contaminar os demais testes.
    cleared = client.put(
        "/api/profile",
        json={"professional_name": "", "professional_registry": "", "institution": ""},
    )
    assert cleared.status_code == 200
    assert all(value == "" for value in cleared.json().values())


def test_patient_directory_never_leaks_locked_names(client):
    """Sigilo nominal: lista expõe apenas iniciais de prontuários com PIN."""
    full_name = "Ana Julia Viana Silva"
    patient = client.post(
        "/api/patients",
        json={"code": "PAC-SIGIL", "name": full_name, "sessions": 2},
    ).json()
    patient_id = patient["id"]

    # Antes do PIN, o nome completo pode ser revelado pelo modo "exibir nomes".
    listed = client.get("/api/patients")
    assert listed.status_code == 200
    entry = next(p for p in listed.json()["patients"] if p["id"] == patient_id)
    assert entry["masked_name"] == "A. J. V. S."
    assert entry["name_initials"] == "AJ"
    assert entry["name"] == full_name
    assert entry["name_hidden"] is False

    # Protegido por PIN: a listagem pública não devolve o nome completo.
    assert (
        client.post(f"/api/patients/{patient_id}/lock", json={"pin": "4821"}).status_code
        == 200
    )
    listed = client.get("/api/patients")
    assert listed.status_code == 200
    body = listed.text
    entry = next(p for p in listed.json()["patients"] if p["id"] == patient_id)
    assert entry["masked_name"] == "A. J. V. S."
    assert entry["name_initials"] == "AJ"
    assert entry["name"] is None
    assert entry["name_hidden"] is True
    assert full_name not in body
    assert "Ana" not in body and "Silva" not in body

    # GET do prontuário também oculta o nome sem PIN válido.
    hidden = client.get(f"/api/patients/{patient_id}")
    assert hidden.status_code == 200
    assert hidden.json()["name"] is None
    assert hidden.json()["masked_name"] == "A. J. V. S."

    # O desbloqueio (e o header de PIN) liberam o nome completo.
    unlocked = client.post(
        f"/api/patients/{patient_id}/unlock", json={"pin": "4821"}
    )
    assert unlocked.status_code == 200
    assert unlocked.json()["patient"]["name"] == full_name
    authorized = client.get(
        f"/api/patients/{patient_id}", headers={"X-Patient-Pin": "4821"}
    )
    assert authorized.status_code == 200
    assert authorized.json()["name"] == full_name


def test_patient_report_pdf(client):
    """Laudo PDF por paciente: autenticado, completo e respeitando o PIN."""
    patient = client.post(
        "/api/patients",
        json={
            "code": "PAC-PDF",
            "name": "Marina Alves de Souza",
            "notes": "Pós-parto; estrias rubras em abdome.",
            "sessions": 3,
        },
    ).json()
    patient_id = patient["id"]

    for session in patient["sessions"][:2]:
        session_id = session["id"]
        upload = upload_session_images(client, session_id)
        assert upload["session_id"] == session_id
        assert upload["session"]["clean_image_url"].startswith("/api/files/uploads/")
        assert (
            client.post(
                f"/api/sessions/{session_id}/calibrate",
                json={"p1": [100, 900], "p2": [400, 900], "known_mm": 30.0},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/sessions/{session_id}/register",
                json={
                    "landmarks": [[150, 150], [850, 150], [150, 850], [850, 850]],
                    "healthy_skin_roi": [40, 40, 120, 120],
                },
            ).status_code
            == 200
        )
        response = client.post(
            f"/api/sessions/{session_id}/measure",
            json={"stria_code": "L1", "polygon": ellipse_polygon()},
        )
        assert response.status_code == 200, response.text

    response = client.get(f"/api/patients/{patient_id}/report.pdf")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/pdf")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "DermaMetric_PAC-PDF_" in disposition and disposition.endswith('.pdf"')
    assert response.content.startswith(b"%PDF-")
    assert len(response.content) > 2048

    # Prontuário protegido por PIN: sem PIN o laudo não é entregue.
    assert (
        client.post(f"/api/patients/{patient_id}/lock", json={"pin": "4821"}).status_code
        == 200
    )
    blocked = client.get(f"/api/patients/{patient_id}/report.pdf")
    assert blocked.status_code == 401
    wrong = client.get(
        f"/api/patients/{patient_id}/report.pdf", headers={"X-Patient-Pin": "0000"}
    )
    assert wrong.status_code == 401

    unlocked = client.get(
        f"/api/patients/{patient_id}/report.pdf", headers={"X-Patient-Pin": "4821"}
    )
    assert unlocked.status_code == 200
    assert unlocked.content.startswith(b"%PDF-")
    assert len(unlocked.content) > 2048

    # Paciente inexistente.
    assert client.get("/api/patients/nao-existe/report.pdf").status_code == 404


def test_patient_report_pdf_includes_professional_profile(client):
    """O laudo traz instituição no cabeçalho e nome/registro na assinatura."""
    profile = {
        "professional_name": "Dra. Helena Prado",
        "professional_registry": "CRM-CE 54321",
        "institution": "Clinica Aurora",
    }
    assert client.put("/api/profile", json=profile).status_code == 200
    try:
        patient = client.post(
            "/api/patients",
            json={"code": "PAC-PERFIL", "name": "Helena Costa", "sessions": 2},
        ).json()
        response = client.get(f"/api/patients/{patient['id']}/report.pdf")
        assert response.status_code == 200, response.text
        text = pdf_text(response.content)
        assert "Clinica Aurora" in text
        assert profile["professional_name"] in text
        assert profile["professional_registry"] in text
    finally:
        client.put(
            "/api/profile",
            json={"professional_name": "", "professional_registry": "", "institution": ""},
        )


def _measure_all_lesions(client, session_id: str) -> None:
    """Mede L1..L4 em posições distintas para exercitar o laudo completo."""
    upload = upload_session_images(client, session_id)
    assert upload["session"]["clean_image_url"].startswith("/api/files/uploads/")
    assert (
        client.post(
            f"/api/sessions/{session_id}/calibrate",
            json={"p1": [100, 900], "p2": [400, 900], "known_mm": 30.0},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/sessions/{session_id}/register",
            json={
                "landmarks": [[150, 150], [850, 150], [150, 850], [850, 850]],
                "healthy_skin_roi": [40, 40, 120, 120],
            },
        ).status_code
        == 200
    )
    centers = ((300.0, 300.0), (700.0, 300.0), (300.0, 700.0), (700.0, 700.0))
    for index, (cx, cy) in enumerate(centers, start=1):
        response = client.post(
            f"/api/sessions/{session_id}/measure",
            json={
                "stria_code": f"L{index}",
                "polygon": ellipse_polygon(cx=cx, cy=cy, a=90.0, b=22.0),
            },
        )
        assert response.status_code == 200, response.text


def test_patient_report_pdf_is_anonymous_by_default(client):
    """O laudo padrão identifica o paciente apenas por código e iniciais."""
    full_name = "Marina Alves de Souza"
    patient = client.post(
        "/api/patients",
        json={"code": "PAC-SIGILO", "name": full_name, "sessions": 2},
    ).json()
    _measure_all_lesions(client, patient["sessions"][0]["id"])

    response = client.get(f"/api/patients/{patient['id']}/report.pdf")
    assert response.status_code == 200, response.text
    text = pdf_text(response.content)

    assert full_name not in text
    assert "Marina" not in text
    assert "Souza" not in text
    assert "PAC-SIGILO" in text
    assert "M. A. S." in text


def test_patient_report_pdf_can_include_name_on_request(client):
    """O nome completo só entra com ?include_name=true e prontuário liberado."""
    full_name = "Helena Costa Ferreira"
    patient = client.post(
        "/api/patients",
        json={"code": "PAC-NOME", "name": full_name, "sessions": 2},
    ).json()

    anonymous = client.get(f"/api/patients/{patient['id']}/report.pdf")
    assert anonymous.status_code == 200, anonymous.text
    assert full_name not in pdf_text(anonymous.content)

    identified = client.get(
        f"/api/patients/{patient['id']}/report.pdf",
        params={"include_name": "true"},
    )
    assert identified.status_code == 200, identified.text
    assert full_name in pdf_text(identified.content)


def test_patient_report_pdf_renders_detailed_lesions(client):
    """Laudo multi-lesão: mapas, recortes, tabela detalhada e síntese clínica."""
    patient = client.post(
        "/api/patients",
        json={"code": "PAC-MULTI", "name": "Paciente Multi Lesão", "sessions": 2},
    ).json()
    for session in patient["sessions"][:2]:
        _measure_all_lesions(client, session["id"])

    response = client.get(f"/api/patients/{patient['id']}/report.pdf")
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"%PDF-")
    assert len(response.content) > 20_000
    text = pdf_text(response.content)

    assert "Resumo executivo consolidado" in text
    for index in range(1, 5):
        assert f"L{index} — região-alvo {index}" in text
    assert "Perím." in text
    assert "L* / a* / b*" in text
    assert "Síntese clínica da lesão" in text
    assert "Registro fotográfico comparativo" in text
    # O nome completo não aparece no laudo padrão, mas o código sim.
    assert "Paciente Multi Lesão" not in text
    assert "PAC-MULTI" in text


# ---------------------------------------------------------------------------
# 7. Senha mestra, chave de recuperação e arquivo-chave (.dmkey)
# ---------------------------------------------------------------------------
RECOVERY_SETTINGS = (
    "dermametric.crypto.dek_password",
    "dermametric.crypto.dek_recovery",
    "dermametric.crypto.recovery_key_id",
    "dermametric.crypto.recovery_created_at",
)
SECURED: dict[str, object] = {}


def _clear_auth_state() -> None:
    keys = (auth.ACCESS_HASH_KEY, *RECOVERY_SETTINGS)
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.executemany(
            "DELETE FROM settings WHERE key = ?", [(key,) for key in keys]
        )
        connection.execute("DELETE FROM access_token")
    # Descarta a DEK adotada em memória, se houve recuperação.
    import backend.security as security_module

    security_module._cached_key = None
    security_module._fernet = None


def _settings_blob() -> str:
    with sqlite3.connect(db.DB_PATH) as connection:
        rows = connection.execute("SELECT key, value FROM settings").fetchall()
    return " ".join(f"{key}={value}" for key, value in rows)


def test_legacy_database_creates_recovery_wrapper_on_login(client):
    """Banco antigo (só hash da senha) ganha wrapper e .dmkey no 1º login."""
    _clear_auth_state()
    auth._save_access_hash(auth._hash("senha-legada-1"))
    try:
        response = client.post(
            "/api/auth/login", params={"password": "senha-legada-1"}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["recovery"]["configured"] is True
        pending = payload["pending_recovery"]
        assert pending is not None
        assert pending["key_file"]["app"] == "DermaMetric"
        assert pending["key_file"]["recovery_key"] == pending["recovery_key"]
        assert recovery.has_password_wrapper()
        assert recovery.has_recovery_wrapper()
        # A senha e a chave nunca aparecem em texto puro no banco.
        blob = _settings_blob()
        assert pending["recovery_key"] not in blob
        assert "senha-legada-1" not in blob
    finally:
        _clear_auth_state()


def test_setup_returns_recovery_key_and_standard_key_file(client):
    """O setup gera a chave de recuperação e o arquivo .dmkey padronizado."""
    _clear_auth_state()
    SECURED.clear()
    response = client.post("/api/auth/setup", params={"password": "senha-mestra-1"})
    assert response.status_code == 200, response.text
    data = response.json()

    key = data["recovery_key"]
    assert key.startswith("DMK-")
    assert len(key.split("-")) == 5
    assert recovery.normalize_recovery_key(key) == key
    assert data["key_id"] == recovery.key_id_for(key)

    key_file = data["key_file"]
    assert key_file["app"] == "DermaMetric"
    assert key_file["type"] == "recovery_key"
    assert key_file["version"] == 1
    assert key_file["key_id"] == data["key_id"]
    assert key_file["created_at"] == data["created_at"]
    assert key_file["recovery_key"] == key
    assert "institution" in key_file

    blob = _settings_blob()
    assert key not in blob
    assert "senha-mestra-1" not in blob
    assert "dek_password" in blob and "dek_recovery" in blob

    status = client.get("/api/auth/status").json()
    assert status["recovery"]["configured"] is True
    assert status["recovery"]["key_id"] == data["key_id"]

    patient = client.post(
        "/api/patients",
        json={"code": "PAC-REC", "name": "Paciente Recuperável", "sessions": 2},
    ).json()
    assert (
        client.post(
            f"/api/patients/{patient['id']}/lock", json={"pin": "4821"}
        ).status_code
        == 200
    )

    SECURED.update(
        key=key,
        key_id=data["key_id"],
        key_file=key_file,
        token=data["access_token"],
        patient_id=patient["id"],
        password="senha-mestra-1",
    )


def test_recovery_with_key_file_keeps_patients_and_rotates(client):
    """Recuperar com o .dmkey preserva os dados e invalida a chave anterior."""
    key = SECURED["key"]
    patient_id = SECURED["patient_id"]

    # Simula senha esquecida: entra apenas com a chave de recuperação.
    response = client.post(
        "/api/auth/recover",
        json={"recovery_key": key, "new_password": "senha-recuperada-2"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["token"]
    assert data["recovery_key"] != key
    assert data["key_file"]["recovery_key"] == data["recovery_key"]
    assert data["key_id"] == recovery.key_id_for(data["recovery_key"])

    # Prontuários continuam legíveis e o PIN existente segue válido.
    patient = client.get(
        f"/api/patients/{patient_id}", headers={"X-Patient-Pin": "4821"}
    )
    assert patient.status_code == 200, patient.text
    assert patient.json()["name"] == "Paciente Recuperável"
    sessions = client.get(
        f"/api/patients/{patient_id}",
        headers={"X-Patient-Pin": "4821"},
    ).json()["sessions"]
    assert sessions

    # Senha nova funciona; a antiga não.
    assert (
        client.post(
            "/api/auth/login", params={"password": "senha-mestra-1"}
        ).status_code
        == 401
    )
    login = client.post(
        "/api/auth/login", params={"password": "senha-recuperada-2"}
    )
    assert login.status_code == 200, login.text
    # Chave antiga (rotacionada) é rejeitada.
    stale = client.post(
        "/api/auth/recover",
        json={"recovery_key": key, "new_password": "outra-senha-3"},
    )
    assert stale.status_code == 400

    # Arquivo de outra clínica/inválido também é rejeitado.
    foreign = client.post(
        "/api/auth/recover",
        json={
            "recovery_key": "DMK-AAAA-AAAA-AAAA-AAAA",
            "new_password": "outra-senha-3",
        },
    )
    assert foreign.status_code == 400
    malformed = client.post(
        "/api/auth/recover",
        json={"recovery_key": "chave-invalida", "new_password": "outra-senha-3"},
    )
    assert malformed.status_code == 400

    SECURED.update(
        key=data["recovery_key"],
        key_id=data["key_id"],
        token=login.json()["token"],
        password="senha-recuperada-2",
    )


def test_change_password_authenticated(client):
    """Troca de senha exige a senha atual e mantém os prontuários."""
    headers = {"Authorization": f"Bearer {SECURED['token']}"}
    patient_id = SECURED["patient_id"]

    unauth = client.post(
        "/api/auth/change-password",
        json={
            "current_password": "senha-recuperada-2",
            "new_password": "senha-trocada-3",
        },
    )
    assert unauth.status_code == 401

    wrong = client.post(
        "/api/auth/change-password",
        json={"current_password": "errada", "new_password": "senha-trocada-3"},
        headers=headers,
    )
    assert wrong.status_code == 400

    changed = client.post(
        "/api/auth/change-password",
        json={
            "current_password": "senha-recuperada-2",
            "new_password": "senha-trocada-3",
        },
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["changed"] is True

    assert (
        client.post(
            "/api/auth/login", params={"password": "senha-recuperada-2"}
        ).status_code
        == 401
    )
    login = client.post("/api/auth/login", params={"password": "senha-trocada-3"})
    assert login.status_code == 200
    SECURED["token"] = login.json()["token"]
    SECURED["password"] = "senha-trocada-3"

    patient = client.get(
        f"/api/patients/{patient_id}", headers={"X-Patient-Pin": "4821"}
    )
    assert patient.status_code == 200


def test_regenerate_recovery_key_rotates_and_requires_password(client):
    """Gerar novo .dmkey rotaciona a chave e exige a senha mestra."""
    headers = {"Authorization": f"Bearer {SECURED['token']}"}
    old_key = SECURED["key"]

    unauth = client.post("/api/auth/regenerate-recovery-key", json={})
    assert unauth.status_code == 401

    wrong = client.post(
        "/api/auth/regenerate-recovery-key",
        json={"master_password": "errada"},
        headers=headers,
    )
    assert wrong.status_code == 400

    response = client.post(
        "/api/auth/regenerate-recovery-key",
        json={"master_password": SECURED["password"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["recovery_key"] != old_key
    assert data["key_file"]["recovery_key"] == data["recovery_key"]
    assert data["recovery"]["key_id"] == data["key_id"]

    # A chave anterior deixa de redefinir a senha.
    stale = client.post(
        "/api/auth/recover",
        json={"recovery_key": old_key, "new_password": "senha-inexistente-4"},
    )
    assert stale.status_code == 400

    SECURED.update(key=data["recovery_key"], key_id=data["key_id"])


def test_reset_patient_pin_with_master_password(client):
    """Redefine/remove o PIN de um prontuário confirmando a senha mestra."""
    headers = {"Authorization": f"Bearer {SECURED['token']}"}
    patient_id = SECURED["patient_id"]

    unauth = client.post(
        f"/api/patients/{patient_id}/reset-pin",
        json={"master_password": SECURED["password"], "new_pin": "1357"},
    )
    assert unauth.status_code == 401

    wrong_password = client.post(
        f"/api/patients/{patient_id}/reset-pin",
        json={"master_password": "errada", "new_pin": "1357"},
        headers=headers,
    )
    assert wrong_password.status_code == 400

    invalid_pin = client.post(
        f"/api/patients/{patient_id}/reset-pin",
        json={"master_password": SECURED["password"], "new_pin": "abc"},
        headers=headers,
    )
    assert invalid_pin.status_code == 400

    reset = client.post(
        f"/api/patients/{patient_id}/reset-pin",
        json={"master_password": SECURED["password"], "new_pin": "1357"},
        headers=headers,
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["locked"] is True
    assert (
        client.post(
            f"/api/patients/{patient_id}/unlock", json={"pin": "4821"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"/api/patients/{patient_id}/unlock", json={"pin": "1357"}
        ).status_code
        == 200
    )

    removed = client.post(
        f"/api/patients/{patient_id}/reset-pin",
        json={"master_password": SECURED["password"], "remove_pin": True},
        headers=headers,
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["locked"] is False
    assert removed.json()["patient"]["has_pin"] is False

    missing = client.post(
        "/api/patients/nao-existe/reset-pin",
        json={"master_password": SECURED["password"], "new_pin": "1357"},
        headers=headers,
    )
    assert missing.status_code == 404


def test_recovery_key_format_and_key_file_payload():
    """Formato DMK é normalizado e o .dmkey segue o contrato v1."""
    key = recovery.generate_recovery_key()
    assert recovery.normalize_recovery_key(key.lower().replace("-", " ")) == key
    assert recovery.key_id_for(key) == recovery.key_id_for(key.replace("-", ""))

    for invalid in ("", "DMK", "DMK-1234", "DMK-1234-5678-9012", "XXX-AAAA-BBBB-CCCC-DDDD"):
        with pytest.raises(ValueError):
            recovery.normalize_recovery_key(invalid)

    created = "2026-01-02T03:04:05+00:00"
    key_file = recovery.build_key_file(key, recovery.key_id_for(key), created, "Clínica Aurora")
    assert key_file == {
        "app": "DermaMetric",
        "type": "recovery_key",
        "version": 1,
        "key_id": recovery.key_id_for(key),
        "created_at": created,
        "institution": "Clínica Aurora",
        "recovery_key": key,
    }


# ---------------------------------------------------------------------------
# 9. Código público do paciente, updated_at e dados pessoais (senha mestra)
# ---------------------------------------------------------------------------
PATIENT_CODE_PATTERN = re.compile(r"^PAC-[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}$")


def parse_iso(value: str) -> datetime:
    assert value, "timestamp vazio"
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def test_patient_code_is_random_unique_and_legacy_compatible(client):
    """Código público é PAC-XXXXXX aleatório, único e sem ordem sequencial."""
    created = []
    for index in range(6):
        response = client.post(
            "/api/patients",
            json={"name": f"Paciente Código {index}", "sessions": 3},
        )
        assert response.status_code == 200, response.text
        created.append(response.json())

    codes = [patient["code"] for patient in created]
    for code in codes:
        assert PATIENT_CODE_PATTERN.match(code), code

    # Unicidade entre prontuários distintos.
    assert len(set(codes)) == len(codes)

    # Aleatório/não sequencial: a ordem de criação não coincide com a ordenação.
    assert codes != sorted(codes)

    # Códigos legados explícitos continuam aceitos (importação/compatibilidade).
    legacy = client.post(
        "/api/patients",
        json={"code": "pac-legado", "name": "Paciente Legado", "sessions": 2},
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["code"] == "PAC-LEGADO"


def test_patient_listing_has_valid_updated_at(client):
    """`updated_at` da listagem é ISO-8601 válido, nunca ausente/vazio."""
    created = client.post(
        "/api/patients",
        json={"name": "Paciente Atualizado", "sessions": 3},
    ).json()
    first_update = parse_iso(created["updated_at"])

    listed = client.get("/api/patients")
    assert listed.status_code == 200
    entries = listed.json()["patients"]
    assert entries
    for entry in entries:
        updated = parse_iso(entry["updated_at"])
        assert updated >= parse_iso(entry["created_at"])

    entry = next(item for item in entries if item["id"] == created["id"])
    assert parse_iso(entry["updated_at"]) >= first_update

    # Uma edição de dados pessoais avança `updated_at` na listagem pública.
    headers = {"Authorization": f"Bearer {SECURED['token']}"}
    edited = client.put(
        f"/api/patients/{created['id']}/personal-data",
        json={"master_password": SECURED["password"], "notes": "Revisão clínica"},
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    refreshed = client.get("/api/patients").json()["patients"]
    entry = next(item for item in refreshed if item["id"] == created["id"])
    assert parse_iso(entry["updated_at"]) >= first_update
    assert entry["notes"] is None


def test_personal_data_unlock_requires_master_password(client):
    """Senha mestra incorreta e estação sem token resultam em 401."""
    patient = client.post(
        "/api/patients",
        json={"name": "Paciente Protegido", "notes": "Dado sensível", "sessions": 2},
    ).json()
    patient_id = patient["id"]
    headers = {"Authorization": f"Bearer {SECURED['token']}"}

    wrong = client.post(
        f"/api/patients/{patient_id}/personal-data/unlock",
        json={"master_password": "senha-errada-9"},
        headers=headers,
    )
    assert wrong.status_code == 401
    assert "Dado sensível" not in wrong.text

    blocked_update = client.put(
        f"/api/patients/{patient_id}/personal-data",
        json={"master_password": "senha-errada-9", "notes": "Alteração indevida"},
        headers=headers,
    )
    assert blocked_update.status_code == 401

    no_token = client.post(
        f"/api/patients/{patient_id}/personal-data/unlock",
        json={"master_password": SECURED["password"]},
    )
    assert no_token.status_code == 401


def test_personal_data_read_and_edit_with_master_password(client):
    """Senha correta libera a leitura e permite editar/re-cifrar os campos."""
    headers = {"Authorization": f"Bearer {SECURED['token']}"}
    patient = client.post(
        "/api/patients",
        json={
            "name": "Clara Nogueira Prado",
            "notes": "Alergia a níquel; gestante.",
            "birth_date": "1990-05-12",
            "phone": "(11) 99999-8888",
            "sessions": 3,
        },
    ).json()
    patient_id = patient["id"]

    unlocked = client.post(
        f"/api/patients/{patient_id}/personal-data/unlock",
        json={"master_password": SECURED["password"]},
        headers=headers,
    )
    assert unlocked.status_code == 200, unlocked.text
    data = unlocked.json()["patient"]
    assert data["code"] == patient["code"]
    assert data["name"] == "Clara Nogueira Prado"
    assert data["birth_date"] == "1990-05-12"
    assert data["phone"] == "(11) 99999-8888"
    assert data["contact"] == "(11) 99999-8888"
    assert data["notes"] == "Alergia a níquel; gestante."
    assert data["has_pin"] is False
    assert data["age"] == max(0, datetime.today().year - 1990)
    assert data["created_at"] and data["updated_at"]

    updated = client.put(
        f"/api/patients/{patient_id}/personal-data",
        json={
            "master_password": SECURED["password"],
            "name": "Clara Nogueira Souza",
            "birth_date": "1991-02-03",
            "phone": "(11) 91234-5678",
            "notes": "Sem alergias relatadas.",
        },
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["updated"] is True
    assert body["patient"]["name"] == "Clara Nogueira Souza"
    assert body["patient"]["birth_date"] == "1991-02-03"
    assert body["patient"]["phone"] == "(11) 91234-5678"
    assert body["patient"]["notes"] == "Sem alergias relatadas."
    assert body["summary"]["personal_data_hidden"] is True
    assert body["summary"]["notes"] is None

    # Releitura pela senha mestra confirma a persistência cifrada.
    again = client.post(
        f"/api/patients/{patient_id}/personal-data/unlock",
        json={"master_password": SECURED["password"]},
        headers=headers,
    )
    assert again.status_code == 200
    assert again.json()["patient"]["name"] == "Clara Nogueira Souza"
    assert again.json()["patient"]["notes"] == "Sem alergias relatadas."

    # Dados sensíveis permanecem cifrados no SQLite.
    with sqlite3.connect(str(db.DB_PATH)) as conn:
        row = conn.execute(
            "SELECT name, notes, birth_date, phone FROM patients WHERE id = ?",
            (patient_id,),
        ).fetchone()
    assert row is not None
    assert all(str(value).startswith("enc:v1:") for value in row)
    assert "Sem alergias" not in row[1]

    # Nem a listagem nem o GET comum expõem os dados pessoais.
    listed_body = client.get("/api/patients").text
    assert "Clara Nogueira Souza" in listed_body
    entry = next(
        item
        for item in client.get("/api/patients").json()["patients"]
        if item["id"] == patient_id
    )
    for field in ("notes", "birth_date", "phone"):
        assert entry[field] is None
    assert entry["personal_data_hidden"] is True
    assert "Sem alergias" not in listed_body
    assert "91234-5678" not in listed_body

    detail = client.get(f"/api/patients/{patient_id}")
    assert detail.status_code == 200
    for field in ("notes", "birth_date", "phone"):
        assert detail.json()[field] is None
