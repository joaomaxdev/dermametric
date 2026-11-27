"""Testes do mapeamento de campo (ROI) — detecção múltipla de estrias.

Cobre a visão computacional (``vision.scan_field_striae``) e o endpoint
``POST /api/sessions/{session_id}/field-scan``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest

TEST_ROOT = Path(__file__).resolve().parent / "tmp_field_scan"
if TEST_ROOT.exists():
    shutil.rmtree(TEST_ROOT)
TEST_ROOT.mkdir(parents=True)
os.environ["DERMAMETRIC_STORAGE_DIR"] = str(TEST_ROOT / "storage")
os.environ["DERMAMETRIC_DB_PATH"] = str(TEST_ROOT / "storage" / "dermametric.db")

from fastapi.testclient import TestClient  # noqa: E402

from backend import database as db  # noqa: E402
from backend import vision  # noqa: E402
from backend.main import app  # noqa: E402

PIXEL_SIZE = 8.0  # px/mm
ROI_BOX = [100.0, 100.0, 400.0, 300.0]
SKIN_BGR = (145, 170, 208)  # RGB (208, 170, 145)
STRIA_BGR = (222, 228, 238)  # alba/hipocrômica

# (y do centro, largura real em mm, comprimento em mm)
STRIPES = [
    (150.0, 1.5, 25.0),
    (220.0, 3.0, 25.0),
    (290.0, 5.0, 25.0),
]


def make_field_image(
    width: int = 700, height: int = 500, with_stripes: bool = True
) -> np.ndarray:
    image = np.full((height, width, 3), SKIN_BGR, dtype=np.uint8)
    if with_stripes:
        for center_y, width_mm, length_mm in STRIPES:
            half = int(round(width_mm * PIXEL_SIZE / 2.0))
            x0 = int(round(150))
            x1 = int(round(x0 + length_mm * PIXEL_SIZE))
            y = int(round(center_y))
            cv2.rectangle(image, (x0, y - half), (x1, y + half), STRIA_BGR, -1)
    # Poros circulares pequenos (devem ser filtrados pelo aspecto/limiar).
    for cx in range(130, 470, 40):
        for cy in range(130, 380, 40):
            cv2.circle(image, (cx, cy), 3, (150, 175, 212), -1)
    return image


def write_image(path: Path, image: np.ndarray) -> str:
    return vision.write_image_bgr(path, image)


def png_bytes(image: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    return buffer.tobytes()


def test_scan_detects_three_striae_and_filters_pores():
    path = write_image(TEST_ROOT / "field_detect.png", make_field_image())
    result = vision.scan_field_striae(
        str(path),
        ROI_BOX,
        PIXEL_SIZE,
        mode="auto",
        sensitivity=0.5,
        min_length_mm=4.0,
        max_length_mm=120.0,
        min_aspect_ratio=2.5,
    )

    objects = result["objects"]
    assert len(objects) == 3, [round(o["mean_width_mm"], 2) for o in objects]

    widths = sorted(float(obj["mean_width_mm"]) for obj in objects)
    expected_widths = sorted(width_mm for _, width_mm, _ in STRIPES)
    for observed, expected in zip(widths, expected_widths):
        # Tolerância para a discretização de pixels/bordas.
        assert observed == pytest.approx(expected, rel=0.30)

    expected_length = STRIPES[0][2]
    for obj in objects:
        assert obj["length_mm"] == pytest.approx(expected_length, rel=0.10)
        # Consistência dimensional exata: area = comprimento × largura média.
        assert obj["length_mm"] * obj["mean_width_mm"] == pytest.approx(
            obj["area_mm2"], rel=1e-9
        )
        assert obj["max_width_mm"] >= obj["mean_width_mm"]
        assert obj["length_mm"] / max(obj["mean_width_mm"], 0.1) >= 2.5
        assert 8 <= len(obj["polygon"]) <= 20
        assert obj["delta_e00"] >= 0.0

    summary = result["summary"]
    assert summary["total_count"] == 3
    assert summary["roi_area_mm2"] == pytest.approx(400 * 300 / PIXEL_SIZE**2)
    assert summary["total_area_mm2"] == pytest.approx(
        sum(obj["area_mm2"] for obj in objects)
    )
    assert 0.0 < summary["coverage_pct"] < 100.0


def test_scan_filters_by_min_length_and_aspect():
    path = write_image(TEST_ROOT / "field_filter.png", make_field_image())
    # Estrias sintéticas têm 25 mm: exigir 30 mm remove todas.
    result = vision.scan_field_striae(
        str(path), ROI_BOX, PIXEL_SIZE, min_length_mm=30.0
    )
    assert result["objects"] == []
    assert result["summary"]["total_count"] == 0

    # Um aspecto absurdo também descarta tudo.
    strict = vision.scan_field_striae(
        str(path), ROI_BOX, PIXEL_SIZE, min_aspect_ratio=100.0
    )
    assert strict["objects"] == []


def test_scan_uniform_skin_returns_no_objects():
    path = write_image(
        TEST_ROOT / "field_uniform.png", make_field_image(with_stripes=False)
    )
    result = vision.scan_field_striae(str(path), ROI_BOX, PIXEL_SIZE)
    assert result["objects"] == []


def test_scan_validates_inputs():
    path = write_image(TEST_ROOT / "field_validation.png", make_field_image())
    with pytest.raises(ValueError):
        vision.scan_field_striae(str(path), ROI_BOX, 0.0)
    with pytest.raises(ValueError):
        vision.scan_field_striae(str(path), ROI_BOX, PIXEL_SIZE, mode="invalido")
    with pytest.raises(ValueError):
        vision.scan_field_striae(
            str(path), ROI_BOX, PIXEL_SIZE, min_length_mm=50.0, max_length_mm=10.0
        )


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _new_baseline_session() -> tuple[str, str]:
    patient = db.create_patient(
        code=None, name="Paciente Campo", notes="", sessions_count=1
    )
    return patient["id"], patient["sessions"][0]["id"]


def test_field_scan_endpoint(client):
    patient_id, session_id = _new_baseline_session()
    try:
        _exercise_field_scan_endpoint(client, session_id)
    finally:
        # Não polui o diretório/sessões compartilhados por outros módulos.
        db.delete_patient(patient_id)


def _exercise_field_scan_endpoint(client, session_id: str) -> None:
    image = make_field_image()
    files = {
        "clean_image": ("campo_clean.png", png_bytes(image), "image/png"),
        "ruler_image": ("campo_ruler.png", png_bytes(image), "image/png"),
    }
    upload = client.post(
        f"/api/sessions/{session_id}/upload",
        files=files,
        data={"label": "Mapeamento de campo"},
    )
    assert upload.status_code == 200, upload.text

    # Antes do alinhamento/calibração o endpoint deve recusar.
    early = client.post(
        f"/api/sessions/{session_id}/field-scan",
        json={"roi_box": ROI_BOX},
    )
    assert early.status_code == 400

    calibration = client.post(
        f"/api/sessions/{session_id}/calibrate",
        json={"p1": [100, 450], "p2": [420, 450], "known_mm": 40.0},
    )
    assert calibration.status_code == 200, calibration.text
    assert calibration.json()["px_per_mm"] == pytest.approx(PIXEL_SIZE)

    landmarks = [[120, 120], [580, 120], [120, 420], [580, 420]]
    register = client.post(
        f"/api/sessions/{session_id}/register",
        json={"landmarks": landmarks, "healthy_skin_roi": [30, 30, 60, 60]},
    )
    assert register.status_code == 200, register.text

    response = client.post(
        f"/api/sessions/{session_id}/field-scan",
        json={
            "roi_box": ROI_BOX,
            "mode": "auto",
            "sensitivity": 0.5,
            "min_length_mm": 4.0,
            "max_length_mm": 120.0,
            "min_aspect_ratio": 2.5,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == session_id
    assert body["summary"]["total_count"] == len(body["objects"]) >= 3
    for key in ("total_area_mm2", "roi_area_mm2", "coverage_pct"):
        assert key in body["summary"]
    for obj in body["objects"]:
        assert set(obj) >= {
            "id",
            "polygon",
            "area_mm2",
            "length_mm",
            "mean_width_mm",
            "max_width_mm",
            "delta_e00",
        }

    # Payload inválido (max <= min) é rejeitado pelo schema.
    invalid = client.post(
        f"/api/sessions/{session_id}/field-scan",
        json={"roi_box": ROI_BOX, "min_length_mm": 50.0, "max_length_mm": 10.0},
    )
    assert invalid.status_code == 422

    missing = client.post("/api/sessions/inexistente/field-scan", json={"roi_box": ROI_BOX})
    assert missing.status_code == 404
