"""Schemas Pydantic (v2) da API do DermaMetric."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Códigos flexíveis: L1..L4 (novo padrão) e E1..E4 (legado).
StriaCode = Annotated[
    str,
    Field(pattern=r"^(?:L[1-4]|E[1-4](?:_(?:maior|menor))?)$"),
    Field(description="Código da região-alvo (L1..L4; E1..E4 aceito por compatibilidade)"),
]

Point = Annotated[list[float], Field(min_length=2, max_length=2)]

_DATETIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$"
)


def _validate_point(point: list[float], name: str) -> list[float]:
    if len(point) != 2:
        raise ValueError(f"{name} deve ter exatamente 2 coordenadas [x, y]")
    return [float(point[0]), float(point[1])]


def normalize_datetime(value: str) -> str:
    """Normaliza ``YYYY-MM-DD[ T]HH:MM[:SS]`` para ``YYYY-MM-DD HH:MM:SS``."""
    text = str(value).strip()
    if not _DATETIME_PATTERN.match(text):
        raise ValueError("Data inválida; use YYYY-MM-DD ou YYYY-MM-DD HH:MM:SS")
    text = text.replace("T", " ")
    formats = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ValueError("Data inválida")


class LandmarkPoint(BaseModel):
    """Ponto anatômico nomeado pela profissional."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", max_length=80)
    x: float
    y: float


class CalibrationRequest(BaseModel):
    """Dois pontos da régua milimetrada e a distância real conhecida."""

    model_config = ConfigDict(extra="forbid")

    p1: Point
    p2: Point
    known_mm: float = Field(gt=0, description="Distância real entre p1 e p2 em mm")

    @field_validator("p1")
    @classmethod
    def _check_p1(cls, value: list[float]) -> list[float]:
        return _validate_point(value, "p1")

    @field_validator("p2")
    @classmethod
    def _check_p2(cls, value: list[float]) -> list[float]:
        return _validate_point(value, "p2")

    @field_validator("known_mm")
    @classmethod
    def _check_known(cls, value: float) -> float:
        if value > 1000:
            raise ValueError("known_mm não pode exceder 1000 mm")
        return value


class RegisterRequest(BaseModel):
    """Pontos anatômicos (homografia) e ROI de pele saudável (CIELAB)."""

    model_config = ConfigDict(extra="forbid")

    landmarks: list[Point | LandmarkPoint] = Field(
        min_length=4,
        max_length=4,
        description="4 pontos [x,y] ou objetos {label,x,y} na imagem limpa",
    )
    healthy_skin_roi: list[int] = Field(
        min_length=4,
        max_length=4,
        description="[x, y, w, h] da pele íntegra de referência",
    )

    @field_validator("landmarks")
    @classmethod
    def _check_landmarks(cls, value: list) -> list:
        normalized = []
        for index, point in enumerate(value):
            if isinstance(point, LandmarkPoint):
                normalized.append(point)
            else:
                coords = _validate_point(point, f"landmark[{index}]")
                normalized.append(LandmarkPoint(label="", x=coords[0], y=coords[1]))
        return normalized

    @field_validator("healthy_skin_roi")
    @classmethod
    def _check_roi(cls, value: list[int]) -> list[int]:
        if value[2] <= 0 or value[3] <= 0:
            raise ValueError("healthy_skin_roi deve ter largura e altura positivas")
        if value[0] < 0 or value[1] < 0:
            raise ValueError("healthy_skin_roi não pode ter coordenadas negativas")
        return value


class MeasureRequest(BaseModel):
    """Polígono traçado sobre a imagem alinhada para medir uma região-alvo."""

    model_config = ConfigDict(extra="forbid")

    stria_code: StriaCode
    stria_label: str | None = Field(default=None, max_length=120)
    polygon: list[Point] = Field(
        min_length=3,
        description="Vértices [[x, y], ...] do polígono",
    )

    @field_validator("polygon")
    @classmethod
    def _check_polygon(cls, value: list[list[float]]) -> list[list[float]]:
        points = [_validate_point(p, f"polygon[{i}]") for i, p in enumerate(value)]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        if max(xs) - min(xs) < 1 or max(ys) - min(ys) < 1:
            raise ValueError("polígono degenerado: área nula")
        return points


class ClinicalScalesRequest(BaseModel):
    """Escalas clínicas subjetivas registradas na sessão."""

    model_config = ConfigDict(extra="forbid")

    end_pain: int = Field(ge=0, le=10, description="Dor (END) 0-10")
    likert_satisfaction: int = Field(
        ge=1, le=5, description="Satisfação Likert 1-5"
    )
    gais_score: int = Field(ge=-1, le=3, description="GAIS -1 a +3")
    notes: str = Field(default="", max_length=4000)


class SessionMetadataRequest(BaseModel):
    """Edição manual de rótulo, data de captura e descrições das lesões."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=120)
    captured_at: str | None = None
    lesion_labels: dict[str, str] | None = None

    @field_validator("captured_at")
    @classmethod
    def _check_captured_at(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        return normalize_datetime(value)

    @field_validator("lesion_labels")
    @classmethod
    def _check_labels(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        cleaned: dict[str, str] = {}
        for code, label in value.items():
            if not re.match(r"^(?:L[1-4]|E[1-4](?:_(?:maior|menor))?)$", code):
                raise ValueError(f"Código de lesão inválido: {code}")
            cleaned[code] = str(label).strip()[:120]
        return cleaned


class LesionLabelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labels: dict[str, str]

    @field_validator("labels")
    @classmethod
    def _check_labels(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for code, label in value.items():
            if not re.match(r"^(?:L[1-4]|E[1-4](?:_(?:maior|menor))?)$", code):
                raise ValueError(f"Código de lesão inválido: {code}")
            cleaned[code] = str(label).strip()[:120]
        return cleaned


# Mantido para compatibilidade com importações antigas.
__all__ = [
    "CalibrationRequest",
    "ClinicalScalesRequest",
    "LandmarkPoint",
    "LesionLabelsRequest",
    "MeasureRequest",
    "RegisterRequest",
    "SessionMetadataRequest",
    "StriaCode",
    "normalize_datetime",
    "Literal",
]
