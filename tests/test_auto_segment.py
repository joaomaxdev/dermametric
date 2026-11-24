"""Testes da segmentação assistida por CIELAB + GrabCut.

Cobrem o motor de visão em imagens sintéticas (rubra, alba e redução de área
em T1 guiada pelo polígono de T0) e o endpoint ``/auto-segment``.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

# Isolamento antecipado quando este módulo é o primeiro a importar o backend.
TEST_ROOT = Path(__file__).resolve().parent / "tmp_auto_segment"
if "backend.database" not in sys.modules:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    import os

    os.environ["DERMAMETRIC_STORAGE_DIR"] = str(TEST_ROOT / "storage")
    os.environ["DERMAMETRIC_DB_PATH"] = str(TEST_ROOT / "storage" / "dermametric.db")

from fastapi.testclient import TestClient  # noqa: E402

from backend import database as db, security, vision  # noqa: E402
from backend.main import app  # noqa: E402

SKIN_BGR = (150, 170, 200)
RUBRA_BGR = (110, 120, 210)
ALBA_SKIN_BGR = (140, 160, 190)
ALBA_BGR = (225, 230, 235)
BOX = [80, 80, 160, 160]


def _write_image(path: Path, skin: tuple[int, int, int], lesion: tuple[int, int, int], axes: tuple[int, int]) -> str:
    image = np.full((320, 320, 3), skin, dtype=np.uint8)
    cv2.ellipse(image, (160, 160), axes, 0, 0, 360, lesion, -1)
    assert cv2.imwrite(str(path), image)
    return str(path)


def _polygon_area(points: list[list[float]]) -> float:
    total = 0.0
    for index, point in enumerate(points):
        following = points[(index + 1) % len(points)]
        total += point[0] * following[1] - following[0] * point[1]
    return abs(total) / 2.0


@pytest.fixture()
def station(tmp_path, monkeypatch):
    """Estação isolada (banco, storage e DEK próprios) para o endpoint."""
    storage = tmp_path / "storage"
    data_dir = tmp_path / "data"
    for directory in (storage, data_dir, storage / "uploads", storage / "processed"):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(db, "DB_PATH", storage / "dermametric.db")
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "STORAGE_DIR", storage)
    monkeypatch.setattr(db, "UPLOAD_DIR", storage / "uploads")
    monkeypatch.setattr(db, "PROCESSED_DIR", storage / "processed")
    monkeypatch.setattr(db, "WORK_DIR", storage / "work")
    monkeypatch.setattr(security, "STORAGE_DIR", storage)
    monkeypatch.setattr(security, "KEY_PATH", storage / ".secret.key")
    monkeypatch.setattr(security, "_fernet", None)
    monkeypatch.setattr(security, "_cached_key", None)

    db.init_db()
    with TestClient(app) as client:
        yield client


def test_segments_rubra_lesion_with_clean_polygon(tmp_path):
    path = _write_image(tmp_path / "rubra.png", SKIN_BGR, RUBRA_BGR, (55, 25))
    result = vision.auto_segment_polygon(path, BOX, None, 0.5, "rubra")

    assert isinstance(result["polygon"], list)
    assert 10 <= len(result["polygon"]) <= 30
    assert result["area_px"] > 500
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["confidence"] > 0.2
    # O contorno acompanha a elipse central, não a caixa inteira.
    xs = [point[0] for point in result["polygon"]]
    ys = [point[1] for point in result["polygon"]]
    assert 90 <= min(xs) and max(xs) <= 230
    assert 115 <= min(ys) and max(ys) <= 205


def test_segments_alba_lesion(tmp_path):
    path = _write_image(tmp_path / "alba.png", ALBA_SKIN_BGR, ALBA_BGR, (55, 25))
    result = vision.auto_segment_polygon(path, BOX, None, 0.5, "alba")

    assert 10 <= len(result["polygon"]) <= 30
    assert result["area_px"] > 500
    assert result["confidence"] > 0.2
    # Modo rubra não deve encontrar a lesão hipocromática (alba).
    rubra = vision.auto_segment_polygon(path, BOX, None, 0.5, "rubra")
    assert rubra["area_px"] < result["area_px"]


def test_seed_polygon_reduces_area_in_follow_up(tmp_path):
    t0_path = _write_image(tmp_path / "t0.png", SKIN_BGR, RUBRA_BGR, (70, 32))
    t0 = vision.auto_segment_polygon(t0_path, [60, 60, 200, 200], None, 0.5, "rubra")

    t1_path = _write_image(tmp_path / "t1.png", SKIN_BGR, RUBRA_BGR, (45, 20))
    t1 = vision.auto_segment_polygon(
        t1_path, [60, 60, 200, 200], t0["polygon"], 0.5, "rubra"
    )

    assert t1["area_px"] > 300
    assert t1["area_px"] < t0["area_px"]
    assert t1["area_px"] < _polygon_area(t0["polygon"]) * 0.9


def test_blank_box_returns_clean_fallback(tmp_path):
    image = np.full((320, 320, 3), SKIN_BGR, dtype=np.uint8)
    path = tmp_path / "blank.png"
    assert cv2.imwrite(str(path), image)

    result = vision.auto_segment_polygon(str(path), BOX, None, 0.5, "auto")
    assert len(result["polygon"]) >= 3
    assert result["area_px"] == 0.0
    assert result["confidence"] <= 0.2


def test_rejects_invalid_parameters(tmp_path):
    path = _write_image(tmp_path / "rubra.png", SKIN_BGR, RUBRA_BGR, (55, 25))
    with pytest.raises(ValueError):
        vision.auto_segment_polygon(path, [10, 10, 0, 40], None, 0.5, "rubra")
    with pytest.raises(ValueError):
        vision.auto_segment_polygon(path, BOX, None, 1.5, "rubra")
    with pytest.raises(ValueError):
        vision.auto_segment_polygon(path, BOX, None, 0.5, "desconhecido")


def test_auto_segment_endpoint(station: TestClient, tmp_path):
    image_path = Path(_write_image(tmp_path / "endpoint.png", SKIN_BGR, RUBRA_BGR, (55, 25)))
    png = image_path.read_bytes()

    upload = station.post(
        "/api/sessions/T0/upload",
        files={
            "clean_image": ("clean.png", png, "image/png"),
            "ruler_image": ("ruler.png", png, "image/png"),
        },
    )
    assert upload.status_code == 200, upload.text

    calibration = station.post(
        "/api/sessions/T0/calibrate",
        json={"p1": [30, 30], "p2": [130, 30], "known_mm": 10.0},
    )
    assert calibration.status_code == 200, calibration.text

    early = station.post(
        "/api/sessions/T0/auto-segment", json={"box": BOX, "mode": "rubra"}
    )
    assert early.status_code == 400

    register = station.post(
        "/api/sessions/T0/register",
        json={
            "landmarks": [[40, 40], [280, 40], [40, 280], [280, 280]],
            "healthy_skin_roi": [10, 10, 50, 50],
        },
    )
    assert register.status_code == 200, register.text

    response = station.post(
        "/api/sessions/T0/auto-segment",
        json={"box": BOX, "sensitivity": 0.55, "mode": "rubra"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == "T0"
    assert 10 <= len(body["polygon"]) <= 30
    assert body["area_px"] > 500
    assert 0.0 <= body["confidence"] <= 1.0

    invalid = station.post(
        "/api/sessions/T0/auto-segment", json={"box": BOX, "sensitivity": 0.0}
    )
    assert invalid.status_code == 422

    missing = station.post(
        "/api/sessions/T9/auto-segment", json={"box": BOX, "mode": "auto"}
    )
    assert missing.status_code == 404
