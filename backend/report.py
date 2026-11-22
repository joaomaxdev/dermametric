"""Laudo clínico longitudinal em PDF (ReportLab).

O relatório é vetorial (texto/tabelas) e embute apenas as fotografias como
imagens. Nada aqui toca o banco ou o storage cifrado: o endpoint em
``backend.main`` resolve as imagens de exibição (já com marca d'água) e o
payload do paciente, e este módulo cuida exclusivamente da composição do PDF.

Estrutura do documento:

1. Cabeçalho institucional, identificação do paciente e fingerprint LGPD.
2. Resumo metodológico e tabela de protocolo (escala px/mm, homografia,
   CIELAB/CIEDE2000).
3. Tabela comparativa longitudinal por lesão (L1..L4) com variação vs. T0.
4. Escalas clínicas subjetivas consolidadas.
5. Registro fotográfico comparativo (alinhado/realçado, com marca d'água).
6. Encerramento com espaço para assinatura e rodapé em todas as páginas.
"""

from __future__ import annotations

import hashlib
import io
from datetime import datetime
from typing import Any, Iterable
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    Image as RLImage,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

try:  # Pillow já é dependência do projeto (redimensiona fotos embutidas).
    from PIL import Image as PILImage
except Exception:  # pragma: no cover - ambiente sem Pillow
    PILImage = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Layout e identidade visual
# ---------------------------------------------------------------------------
PAGE_W, PAGE_H = A4
MARGIN_X = 17 * mm
TOP_MARGIN = 24 * mm
BOTTOM_MARGIN = 22 * mm
CONTENT_W = PAGE_W - 2 * MARGIN_X

INK = colors.HexColor("#10242b")
# Paleta alinhada à interface web: verde-petróleo clínico nos cabeçalhos,
# turquesa/menta nos destaques e cinzas frios nas linhas e bordas.
BRAND = colors.HexColor("#114b46")
BRAND_DEEP = colors.HexColor("#0c2323")
ACCENT = colors.HexColor("#18c9a8")
ACCENT_LIGHT = colors.HexColor("#57f5cc")
ACCENT_PALE = colors.HexColor("#9fe9db")
MUTED = colors.HexColor("#5b6d75")
LINE = colors.HexColor("#d5e2df")
ZEBRA = colors.HexColor("#f5f9f8")
CARD = colors.HexColor("#f8faf9")
BAND = colors.HexColor("#eef4f2")
WHITE = colors.white

LESION_CODES = ("L1", "L2", "L3", "L4")
LEGACY_CODES = ("E1", "E1_maior", "E1_menor", "E2", "E3", "E4")
LESION_TITLES = {
    "L1": "L1 — região-alvo 1",
    "L2": "L2 — região-alvo 2",
    "L3": "L3 — região-alvo 3",
    "L4": "L4 — região-alvo 4",
}

# Aviso que cabe em uma linha no rodapé de cada página.
_FOOTER_NOTICE = (
    "Documento confidencial — LGPD (Lei nº 13.709/2018). Uso restrito ao profissional responsável."
)

_FONT_CANDIDATES = {
    "regular": (
        "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/OpenSans/OpenSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ),
    "bold": (
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/OpenSans/OpenSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ),
    "italic": (
        "/usr/share/fonts/liberation/LiberationSans-Italic.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
        "/usr/share/fonts/noto/NotoSans-Italic.ttf",
        "/usr/share/fonts/OpenSans/OpenSans-Italic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    ),
    "mono": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/noto/NotoSansMono-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Roman.ttf",
    ),
}

_fonts: dict[str, str] | None = None
_style_cache: dict[str, ParagraphStyle] | None = None


def _register_fonts() -> dict[str, str]:
    """Registra Liberation/Noto/DejaVu (Δ, ², ã) com fallback para Helvetica."""
    global _fonts
    if _fonts is not None:
        return _fonts
    resolved: dict[str, str] = {}
    from pathlib import Path

    for weight, candidates in _FONT_CANDIDATES.items():
        path = next((c for c in candidates if Path(c).exists()), None)
        name = f"DM-{'Sans' if weight == 'regular' else weight.title()}"
        if path:
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                resolved[weight] = name
                continue
            except Exception:  # noqa: BLE001 - fonte inválida: usa Helvetica
                pass
        resolved[weight] = {
            "regular": "Helvetica",
            "bold": "Helvetica-Bold",
            "italic": "Helvetica-Oblique",
            "mono": "Courier",
        }[weight]
    try:
        pdfmetrics.registerFontFamily(
            resolved["regular"],
            normal=resolved["regular"],
            bold=resolved["bold"],
            italic=resolved["italic"],
            boldItalic=resolved["bold"],
        )
    except Exception:  # noqa: BLE001 - família já registrada
        pass
    _fonts = resolved
    return resolved


def _styles() -> dict[str, ParagraphStyle]:
    global _style_cache
    if _style_cache is not None:
        return _style_cache
    fonts = _register_fonts()

    def style(name: str, **kwargs) -> ParagraphStyle:
        base = {
            "fontName": fonts["regular"],
            "fontSize": 8.6,
            "leading": 11.6,
            "textColor": INK,
            "alignment": TA_LEFT,
            "spaceAfter": 0,
        }
        base.update(kwargs)
        return ParagraphStyle(name, **base)

    _style_cache = {
        "brand": style(
            "dm-brand", fontName=fonts["bold"], fontSize=19, leading=22,
            textColor=WHITE,
        ),
        "brand-right": style(
            "dm-brand-right", fontSize=7.6, leading=10, textColor=colors.HexColor("#c9dde0"),
        ),
        "title": style(
            "dm-title", fontName=fonts["bold"], fontSize=12.5, leading=15.5,
            textColor=INK, spaceBefore=9, spaceAfter=3,
        ),
        "h2": style(
            "dm-h2", fontName=fonts["bold"], fontSize=10.2, leading=13,
            textColor=BRAND, spaceBefore=12, spaceAfter=4, keepWithNext=1,
        ),
        "body": style("dm-body", fontSize=8.6, leading=12.2, spaceAfter=3),
        "small": style("dm-small", fontSize=7.8, leading=10.6, textColor=MUTED),
        "note": style(
            "dm-note", fontSize=7.6, leading=10.4, textColor=MUTED,
            spaceBefore=2, spaceAfter=2,
        ),
        "cell": style("dm-cell", fontSize=7.6, leading=9.8),
        "cell-mono": style(
            "dm-cell-mono", fontName=fonts["mono"], fontSize=7.2, leading=9.6,
            textColor=INK,
        ),
        "cell-muted": style("dm-cell-muted", fontSize=7.4, leading=9.4, textColor=MUTED),
        "meta-label": style(
            "dm-meta-label", fontName=fonts["bold"], fontSize=7.2, leading=9.2,
            textColor=BRAND,
        ),
        "cell-head": style(
            "dm-cell-head", fontName=fonts["bold"], fontSize=7.4, leading=9.4,
            textColor=WHITE,
        ),
        "caption": style(
            "dm-caption", fontSize=7.1, leading=9.4, textColor=MUTED,
            spaceBefore=2,
        ),
        "caption-strong": style(
            "dm-caption-strong", fontName=fonts["bold"], fontSize=7.4, leading=9.6,
            textColor=BRAND,
        ),
        "legend": style(
            "dm-legend", fontSize=7.4, leading=10, textColor=MUTED,
            spaceBefore=3,
        ),
        "signature": style(
            "dm-signature", fontName=fonts["bold"], fontSize=8.4, leading=11,
            textColor=INK,
        ),
        "summary": style(
            "dm-summary", fontSize=8.2, leading=11.4, textColor=INK,
            spaceBefore=3, spaceAfter=2, keepWithNext=1,
        ),
    }
    return _style_cache


# ---------------------------------------------------------------------------
# Formatação (pt-BR)
# ---------------------------------------------------------------------------
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


def _text(value: Any) -> str:
    """Escapa texto para uso dentro de ``Paragraph`` e preserva quebras."""
    raw = escape(str(value if value is not None else ""))
    return raw.replace("\r\n", "\n").replace("\n", "<br/>")


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:.{digits}f}".replace(".", ",")


def _fmt_signed(value: Any, digits: int = 1, unit: str = "") -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if number > 0 else ("−" if number < 0 else "")
    text = f"{abs(number):.{digits}f}".replace(".", ",")
    return f"{sign}{text}{unit}"


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        pass
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _date(value: Any, *, with_time: bool = True) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return "—"
    return parsed.strftime("%d/%m/%Y %H:%M" if with_time else "%d/%m/%Y")


def _code(session: dict[str, Any]) -> str:
    return str(session.get("code") or session.get("session_code") or session.get("id") or "—")


def _patient_display_name(patient: dict[str, Any]) -> str:
    """Nome exibido no laudo: completo só quando explicitamente autorizado."""
    name = str(patient.get("name") or "").strip()
    if patient.get("anonymized") or not name:
        return str(patient.get("masked_name") or "").strip() or "Paciente identificado por código"
    return name


def _session_number(session: dict[str, Any]) -> int:
    digits = "".join(ch for ch in _code(session) if ch.isdigit())
    return int(digits) if digits else 999


def _sort_sessions(sessions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(sessions, key=lambda s: (_session_number(s), _code(s)))


def _lesion_label(session: dict[str, Any] | None, code: str) -> str:
    if session:
        labels = session.get("lesion_labels") or {}
        label = (labels.get(code) or "").strip()
        if label:
            return label
        for measurement in session.get("measurements") or []:
            if measurement.get("stria_code") == code and measurement.get("stria_label"):
                return str(measurement["stria_label"])
    return ""


def _measurement(session: dict[str, Any] | None, code: str) -> dict[str, Any] | None:
    if not session:
        return None
    for measurement in session.get("measurements") or []:
        if measurement.get("stria_code") == code:
            return measurement
    return None


def _landmark_summary(session: dict[str, Any]) -> str:
    landmarks = session.get("landmarks") or []
    labels = [
        str(point.get("label") or "").strip()
        for point in landmarks
        if isinstance(point, dict)
    ]
    named = [label for label in labels if label]
    if not landmarks:
        return "—"
    if len(named) == len(landmarks) and named:
        return ", ".join(named)
    if named:
        return f"{', '.join(named)} (+{len(landmarks) - len(named)} sem rótulo)"
    return f"{len(landmarks)} pontos homólogos"


def _roi_summary(session: dict[str, Any]) -> str:
    roi = session.get("healthy_skin_roi")
    if not isinstance(roi, (list, tuple)) or len(roi) < 4:
        return "—"
    x, y, w, h = roi[:4]
    return f"x={_fmt(x, 0)} y={_fmt(y, 0)} · {_fmt(w, 0)}×{_fmt(h, 0)} px"


def _fingerprint(patient: dict[str, Any], sessions: list[dict[str, Any]]) -> str:
    """Fingerprint determinístico dos dados do laudo (integridade/LGPD)."""
    parts: list[str] = [
        str(patient.get("id") or ""),
        str(patient.get("code") or ""),
        str(patient.get("name") or ""),
        str(patient.get("created_at") or ""),
    ]
    for session in sessions:
        parts.append(_code(session))
        parts.append(str(session.get("captured_at") or ""))
        parts.append(_fmt(session.get("px_per_mm"), 3))
        for measurement in session.get("measurements") or []:
            parts.append(
                "|".join(
                    [
                        str(measurement.get("stria_code") or ""),
                        _fmt(measurement.get("length_mm"), 3),
                        _fmt(measurement.get("mean_width_mm"), 3),
                        _fmt(measurement.get("max_width_mm"), 3),
                        _fmt(measurement.get("area_mm2"), 3),
                        _fmt(measurement.get("delta_e_00"), 3),
                    ]
                )
            )
        scales = session.get("clinical_scales") or {}
        if scales:
            parts.append(
                "|".join(
                    _fmt(scales.get(key), 0)
                    for key in ("end_pain", "likert_satisfaction", "gais_score")
                )
            )
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest().upper()
    return f"DM-{digest[:8]}-{digest[8:16]}"


# ---------------------------------------------------------------------------
# Blocos do documento
# ---------------------------------------------------------------------------
class _LogoEmblem(Flowable):
    """Emblema vetorial do monograma "D" desenhado nativamente no PDF.

    Reproduz o ícone da aplicação (quadrado arredondado verde-petróleo,
    monograma "D" em quatro faixas turquesa/menta) usando
    ``canvas.linearGradient`` e recortes vetoriais, sem depender de ficheiros
    de imagem externos.
    """

    # Recortes curvos ascendentes que separam as quatro faixas do "D".
    _CUTS = (
        (30, 88, 54, 87, 78, 83, 100, 72),
        (30, 68, 56, 67, 80, 62, 100, 52),
        (30, 48, 54, 47, 74, 43, 89, 34),
    )

    def __init__(self, size: float = 10 * mm) -> None:
        super().__init__()
        self.size = size

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        return self.size, self.size

    def draw(self) -> None:
        _draw_logo(self.canv, self.size)


def _draw_logo(canvas: pdfcanvas.Canvas, size: float) -> None:
    """Desenha o emblema DermaMetric na origem atual, escalado para ``size``.

    Coordenadas internas em 0..128 (mesmo sistema do ``favicon.svg``).
    """
    scale = size / 128.0
    canvas.saveState()
    canvas.scale(scale, scale)

    bg = colors.HexColor("#123131")
    bg_deep = BRAND_DEEP
    ink_top = ACCENT
    ink_base = ACCENT_LIGHT

    # Contentor: quadrado arredondado com gradiente verde-petróleo.
    contorno = canvas.beginPath()
    contorno.roundRect(1, 1, 126, 126, 28)
    canvas.saveState()
    canvas.clipPath(contorno, stroke=0, fill=0)
    canvas.linearGradient(0, 128, 128, 0, [bg, bg_deep], [0, 1], extend=True)
    canvas.restoreState()

    # Borda interna subtil (equivalente a rgba(110,231,210,0.24) sobre o fundo).
    borda = canvas.beginPath()
    borda.roundRect(2.5, 2.5, 123, 123, 26)
    canvas.setStrokeColor(colors.HexColor("#265752"))
    canvas.setLineWidth(1.5)
    canvas.drawPath(borda, stroke=1, fill=0)

    # Silhueta do "D": haste reta à esquerda e arco semicircular à direita.
    letra = canvas.beginPath()
    letra.moveTo(42, 26)
    letra.lineTo(62, 26)
    letra.curveTo(83.5, 26, 100, 43, 100, 64)
    letra.curveTo(100, 85, 83.5, 102, 62, 102)
    letra.lineTo(42, 102)
    letra.curveTo(35, 102, 32, 98.5, 32, 92)
    letra.lineTo(32, 36)
    letra.curveTo(32, 29.5, 35, 26, 42, 26)
    letra.close()

    canvas.saveState()
    canvas.clipPath(letra, stroke=0, fill=0)
    canvas.linearGradient(32, 102, 100, 26, [ink_top, ink_base], [0, 1], extend=True)
    canvas.restoreState()

    # Três recortes curvos ascendentes, separando as quatro faixas.
    canvas.setStrokeColor(bg_deep)
    canvas.setLineWidth(4.5)
    canvas.setLineCap(1)
    for pontos in _LogoEmblem._CUTS:
        corte = canvas.beginPath()
        corte.moveTo(pontos[0], pontos[1])
        corte.curveTo(*pontos[2:])
        canvas.drawPath(corte, stroke=1, fill=0)

    canvas.restoreState()


class _Banner(Flowable):
    """Banner institucional da página 1: emblema, marca e subtítulo."""

    def __init__(self, institution: str = "", height: float = 20 * mm) -> None:
        super().__init__()
        self.institution = institution
        self.height = height
        self.width = CONTENT_W

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        self.width = availWidth or CONTENT_W
        return self.width, self.height

    def draw(self) -> None:
        canvas = self.canv
        fonts = _register_fonts()
        height = self.height
        radius = 3 * mm
        pad = 7 * mm

        banner = canvas.beginPath()
        banner.roundRect(0, 0, self.width, height, radius)
        canvas.setFillColor(BRAND_DEEP)
        canvas.drawPath(banner, stroke=0, fill=1)

        # Filete turquesa subtil na base do banner.
        canvas.setStrokeColor(colors.HexColor("#1d4a46"))
        canvas.setLineWidth(0.8)
        canvas.line(pad, 0.9, self.width - pad, 0.9)

        logo_size = 11.5 * mm
        canvas.saveState()
        canvas.translate(pad, (height - logo_size) / 2)
        _draw_logo(canvas, logo_size)
        canvas.restoreState()

        canvas.setFillColor(WHITE)
        canvas.setFont(fonts["bold"], 17)
        canvas.drawString(
            pad + logo_size + 4.5 * mm,
            height / 2 - 17 * 0.35,
            "DermaMetric",
        )

        lines = ["Fotogrametria · Morfometria", "Laudo clínico longitudinal"]
        if self.institution:
            lines.append(self.institution)
        canvas.setFillColor(ACCENT_PALE)
        canvas.setFont(fonts["regular"], 7.2)
        leading = 9.6
        block_h = leading * len(lines)
        baseline = height / 2 + block_h / 2 - leading + 1.6
        for index, line in enumerate(lines):
            canvas.drawRightString(
                self.width - pad, baseline - index * leading, line
            )


def _band(institution: str = "") -> _Banner:
    return _Banner(institution)


def _meta_table(patient: dict[str, Any], comparison: dict[str, Any]) -> Table:
    styles = _styles()
    fingerprint = comparison.get("fingerprint") or _fingerprint(
        patient, comparison.get("_sessions") or []
    )
    status = patient.get("status") or ("locked" if patient.get("has_pin") else "active")
    status_label = (
        "Finalizado / protegido por PIN"
        if patient.get("has_pin") or status == "locked"
        else "Em acompanhamento"
    )
    done = patient.get("sessions_done")
    total = patient.get("sessions_total") or len(comparison.get("_sessions") or [])
    notes = (patient.get("notes") or "").strip() or "Sem observações clínicas registradas."

    rows = [
        (
            "Paciente",
            Paragraph(_text(_patient_display_name(patient)), styles["cell"]),
            "Código",
            Paragraph(_text(patient.get("code") or "—"), styles["cell-mono"]),
        ),
        (
            "Emissão",
            Paragraph(
                _text(comparison.get("generated_at_label") or _date(comparison.get("generated_at"))),
                styles["cell"],
            ),
            "Status",
            Paragraph(_text(status_label), styles["cell"]),
        ),
        (
            "Sessões avaliadas",
            Paragraph(f"{done if done is not None else '—'} de {total or '—'}", styles["cell"]),
            "Fingerprint SHA-256",
            Paragraph(_text(fingerprint), styles["cell-mono"]),
        ),
    ]
    body = []
    for label_a, value_a, label_b, value_b in rows:
        body.append(
            [
                Paragraph(_text(label_a), styles["meta-label"]),
                value_a,
                Paragraph(_text(label_b), styles["meta-label"]),
                value_b,
            ]
        )
    body.append(
        [
            Paragraph("Observações clínicas", styles["meta-label"]),
            Paragraph(_text(notes), styles["cell"]),
            "",
            "",
        ]
    )
    table = Table(
        body,
        colWidths=[CONTENT_W * 0.17, CONTENT_W * 0.34, CONTENT_W * 0.17, CONTENT_W * 0.32],
    )
    table.setStyle(
        TableStyle(
            [
                ("SPAN", (1, 3), (3, 3)),
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("ROWBACKGROUNDS", (1, 0), (1, 2), [colors.white, ZEBRA]),
                ("ROWBACKGROUNDS", (3, 0), (3, 2), [colors.white, ZEBRA]),
                ("BACKGROUND", (0, 0), (0, -1), BAND),
                ("BACKGROUND", (2, 0), (2, -2), BAND),
                ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("LINEBEFORE", (2, 0), (2, -1), 0.4, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5.5),
            ]
        )
    )
    return table


def _methodology(sessions: list[dict[str, Any]]) -> list[Any]:
    styles = _styles()
    story: list[Any] = [Paragraph("1. Resumo metodológico e protocolo", styles["h2"])]
    story.append(
        Paragraph(
            "As sessões são capturadas com régua milimetrada no plano da pele. A escala "
            "métrica da sessão (<b>px/mm</b>) é obtida pela distância conhecida entre dois "
            "pontos da régua e aplicada a todas as medidas daquele fotograma. O alinhamento "
            "entre visitas usa uma <b>homografia</b> estimada a partir de quatro marcos "
            "anatómicos homólogos, reamostrando cada imagem para o referencial do T0; assim "
            "as regiões-alvo permanecem comparáveis ao longo do tempo. A fotometria é "
            "avaliada no espaço <b>CIELAB</b> (iluminante D65, observador 2°) com normalização "
            "pelo padrão de pele sadia (ROI de referência) e diferença de cor pelo "
            "<b>CIEDE2000 (ΔE<sub>00</sub>)</b>: quanto menor o ΔE<sub>00</sub> em relação à "
            "pele íntegra, mais próxima a lesão está da coloração normal. A morfometria "
            "reporta comprimento (eixo maior), largura média (média das cordas "
            "perpendiculares), largura máxima e área do polígono traçado. Os arquivos "
            "fotográficos são armazenados cifrados (AES/Fernet) e o laudo é emitido com "
            "marca d'água de rastreabilidade.",
            styles["body"],
        )
    )

    header = [
        Paragraph("Sessão", styles["cell-head"]),
        Paragraph("Data da coleta", styles["cell-head"]),
        Paragraph("Escala (px/mm)", styles["cell-head"]),
        Paragraph("Marcos anatômicos (homografia)", styles["cell-head"]),
        Paragraph("ROI pele sadia", styles["cell-head"]),
    ]
    rows = [header]
    for session in sessions:
        rows.append(
            [
                Paragraph(f"<b>{_text(_code(session))}</b>", styles["cell"]),
                Paragraph(_text(_date(session.get("captured_at"))), styles["cell"]),
                Paragraph(
                    _fmt(session.get("px_per_mm"), 2)
                    if session.get("px_per_mm")
                    else "não calibrada",
                    styles["cell"],
                ),
                Paragraph(_text(_landmark_summary(session)), styles["cell"]),
                Paragraph(_text(_roi_summary(session)), styles["cell"]),
            ]
        )
    table = Table(
        rows,
        colWidths=[CONTENT_W * 0.10, CONTENT_W * 0.18, CONTENT_W * 0.15, CONTENT_W * 0.33, CONTENT_W * 0.24],
        repeatRows=1,
    )
    table.setStyle(_grid_style())
    story.append(Spacer(1, 4))
    story.append(table)
    return story


def _grid_style(*, bold_head: bool = True) -> TableStyle:
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, BRAND_DEEP),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA]),
    ]
    if bold_head:
        commands.append(("FONTNAME", (0, 0), (-1, 0), _register_fonts()["bold"]))
    return TableStyle(commands)


def _delta(base: Any, last: Any) -> tuple[float | None, float | None]:
    try:
        base_f = float(base)
        last_f = float(last)
    except (TypeError, ValueError):
        return None, None
    if base_f == 0:
        return None, last_f - base_f
    return (last_f - base_f) / base_f * 100.0, last_f - base_f


# ---------------------------------------------------------------------------
# Mapas visuais das áreas marcadas (OpenCV) e métricas derivadas
# ---------------------------------------------------------------------------
# Cores distintas por região-alvo, estáveis entre mapas e recortes.
_LESION_PALETTE = (
    (209, 73, 91),
    (0, 121, 140),
    (237, 174, 73),
    (106, 76, 147),
    (42, 157, 143),
    (231, 111, 81),
    (69, 123, 157),
    (233, 196, 106),
)


def _lesion_colors(codes: Iterable[str]) -> dict[str, tuple[int, int, int]]:
    return {
        code: _LESION_PALETTE[index % len(_LESION_PALETTE)]
        for index, code in enumerate(codes)
    }


class _PhotoStore:
    """Decodifica sob demanda as imagens alinhadas usadas nos mapas/recortes."""

    def __init__(self, images: list[dict[str, Any]] | None) -> None:
        self._raw: dict[str, bytes] = {}
        for entry in images or []:
            code = str(entry.get("session_code") or "")
            data = (
                entry.get("aligned_png")
                or entry.get("enhanced_png")
                or entry.get("image_png")
            )
            if code and data:
                self._raw[code] = data
        self._cache: dict[str, Any] = {}

    def get(self, code: Any):
        key = str(code or "")
        if key in self._cache:
            return self._cache[key]
        image = None
        data = self._raw.get(key)
        if data:
            try:
                from . import vision

                image = vision.imdecode_bgr(data)
            except Exception:  # noqa: BLE001 - imagem ilegível: segue sem mapa
                image = None
        self._cache[key] = image
        return image


def _encode_png(image: Any) -> bytes | None:
    try:
        import cv2

        ok, buffer = cv2.imencode(".png", image)
        return buffer.tobytes() if ok else None
    except Exception:  # noqa: BLE001 - OpenCV indisponível
        return None


def _polygon_array(polygon: Iterable[Iterable[float]]):
    import numpy as np

    return np.array(
        [[int(round(x)), int(round(y))] for x, y in polygon], dtype=np.int32
    )


def _fit_image(image: Any, max_px: int = 1200):
    height, width = image.shape[:2]
    longest = max(width, height)
    if longest <= max_px:
        return image
    try:
        import cv2

        scale = max_px / float(longest)
        return cv2.resize(
            image,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    except Exception:  # noqa: BLE001
        return image


def _draw_polygon(
    image: Any,
    polygon: Iterable[Iterable[float]],
    rgb: tuple[int, int, int],
    label: str = "",
) -> None:
    if not polygon or len(list(polygon)) < 3:
        return
    polygon = list(polygon)
    try:
        import cv2
    except Exception:  # noqa: BLE001 - OpenCV indisponível
        return
    height, width = image.shape[:2]
    points = _polygon_array(polygon).reshape(-1, 1, 2)
    stroke = max(2, int(round(min(width, height) / 240)))
    cv2.polylines(image, [points], True, rgb[::-1], stroke, cv2.LINE_AA)
    if not label:
        return
    cx, cy = points[:, 0, 0].mean(), points[:, 0, 1].mean()
    scale = max(0.45, min(width, height) / 1200)
    (tw, th), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    x, y = int(cx - tw / 2), int(cy + th / 2)
    pad = max(3, stroke)
    cv2.rectangle(
        image,
        (x - pad, y - th - pad),
        (x + tw + pad, y + base + pad),
        (12, 35, 35),
        -1,
    )
    cv2.putText(
        image, label, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
        (255, 255, 255), 2, cv2.LINE_AA,
    )


def _draw_dashed_rect(
    image: Any,
    roi: Iterable[float] | None,
    rgb: tuple[int, int, int],
    label: str = "",
) -> None:
    if not roi or len(list(roi)) < 4:
        return
    roi = list(roi)
    try:
        import cv2
    except Exception:  # noqa: BLE001
        return
    height, width = image.shape[:2]
    x, y, w, h = (int(round(value)) for value in roi[:4])
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width - 1, x + w), min(height - 1, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    color = rgb[::-1]
    stroke = max(2, int(round(min(width, height) / 320)))
    dash = max(8, int(round(min(width, height) / 45)))
    gap = max(5, dash // 2)

    def dashes(start: int, end: int):
        pos = start
        while pos < end:
            stop = min(pos + dash, end)
            yield int(pos), int(stop)
            pos = stop + gap

    for a, b in dashes(x0, x1):
        cv2.line(image, (a, y0), (b, y0), color, stroke, cv2.LINE_AA)
        cv2.line(image, (a, y1), (b, y1), color, stroke, cv2.LINE_AA)
    for a, b in dashes(y0, y1):
        cv2.line(image, (x0, a), (x0, b), color, stroke, cv2.LINE_AA)
        cv2.line(image, (x1, a), (x1, b), color, stroke, cv2.LINE_AA)
    if label:
        cv2.putText(
            image,
            label,
            (x0, max(16, y0 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            max(0.42, min(width, height) / 1400),
            color,
            1,
            cv2.LINE_AA,
        )


def _annotate_lesion_map(
    image: Any,
    session: dict[str, Any],
    codes: Iterable[str],
    colors: dict[str, tuple[int, int, int]],
) -> bytes | None:
    annotated = image.copy()
    _draw_dashed_rect(
        annotated, session.get("healthy_skin_roi"), (0, 158, 140),
        "ROI pele sadia",
    )
    for code in codes:
        measurement = _measurement(session, code)
        if not measurement:
            continue
        _draw_polygon(
            annotated,
            measurement.get("polygon") or [],
            colors.get(code, (0, 150, 136)),
            code,
        )
    return _encode_png(_fit_image(annotated, 1100))


def _crop_region(
    image: Any,
    polygons: Iterable[Iterable[Iterable[float]]],
    margin: float = 0.35,
    min_pad: int = 30,
) -> tuple[Any | None, tuple[int, int]]:
    import numpy as np

    points = [
        point
        for polygon in polygons
        for point in (polygon or [])
        if len(point) >= 2
    ]
    if len(points) < 2:
        return None, (0, 0)
    height, width = image.shape[:2]
    array = np.array([[float(p[0]), float(p[1])] for p in points])
    x0, y0 = array.min(axis=0)
    x1, y1 = array.max(axis=0)
    pad = max(min_pad, int(round(max(x1 - x0, y1 - y0) * margin)))
    x0 = max(0, int(x0 - pad))
    y0 = max(0, int(y0 - pad))
    x1 = min(width, int(x1 + pad))
    y1 = min(height, int(y1 + pad))
    if x1 - x0 < 12 or y1 - y0 < 12:
        return None, (0, 0)
    return image[y0:y1, x0:x1].copy(), (x0, y0)


def _polygon_perimeter_mm(polygon: Iterable[Iterable[float]], px_per_mm: Any) -> float | None:
    if not polygon or not px_per_mm:
        return None
    polygon = list(polygon)
    if len(polygon) < 3:
        return None
    try:
        import numpy as np

        points = _polygon_array(polygon).astype(float)
        closed = np.vstack([points, points[:1]])
        length_px = float(np.linalg.norm(np.diff(closed, axis=0), axis=1).sum())
        return length_px / float(px_per_mm)
    except Exception:  # noqa: BLE001
        return None


def _polygon_lab_means(image: Any, polygon: Iterable[Iterable[float]]) -> tuple[float, float, float] | None:
    if not polygon:
        return None
    polygon = list(polygon)
    if len(polygon) < 3:
        return None
    try:
        import cv2
        import numpy as np

        from . import vision

        mask = np.zeros(image.shape[:2], dtype=np.uint8)
        cv2.fillPoly(mask, [_polygon_array(polygon)], 255)
        if not mask.any():
            return None
        lab = vision.bgr_to_cielab_float(image)
        pixels = lab[mask > 0]
        if pixels.size == 0:
            return None
        means = pixels.mean(axis=0)
        return float(means[0]), float(means[1]), float(means[2])
    except Exception:  # noqa: BLE001 - sem OpenCV/CIELAB: omite L*a*b*
        return None


def _lesion_metrics(
    session: dict[str, Any],
    code: str,
    photos: _PhotoStore | None,
    cache: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    key = (str(_code(session)), code)
    if key in cache:
        return cache[key]
    measurement = _measurement(session, code) or {}
    polygon = measurement.get("polygon") or []
    px_per_mm = session.get("px_per_mm")
    lab = None
    if photos is not None:
        image = photos.get(_code(session))
        if image is not None:
            lab = _polygon_lab_means(image, polygon)
    data = {
        "length": measurement.get("length_mm"),
        "mean_width": measurement.get("mean_width_mm"),
        "max_width": measurement.get("max_width_mm"),
        "area": measurement.get("area_mm2"),
        "delta_e_00": measurement.get("delta_e_00"),
        "perimeter": _polygon_perimeter_mm(polygon, px_per_mm),
        "lab": lab,
        "polygon": polygon,
    }
    cache[key] = data
    return data


def _framed_image(
    png_bytes: bytes | None,
    caption_html: str,
    width: float,
    max_height: float = 62 * mm,
) -> Table | None:
    if not png_bytes:
        return None
    styles = _styles()
    ratio = 0.75
    if PILImage is not None:
        try:
            with PILImage.open(io.BytesIO(png_bytes)) as probe:
                probe_w, probe_h = probe.size
            if probe_w:
                ratio = probe_h / float(probe_w)
        except Exception:  # noqa: BLE001
            ratio = 0.75
    inner_w = width - 12
    inner_h = inner_w * ratio
    if inner_h > max_height:
        inner_h = max_height
        inner_w = inner_h / ratio if ratio else width - 12
    image = RLImage(io.BytesIO(png_bytes), width=inner_w, height=inner_h)
    return Table(
        [[image], [Paragraph(caption_html, styles["caption"])]],
        colWidths=[width],
        style=TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("BACKGROUND", (0, 0), (-1, -1), CARD),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (0, 0), "MIDDLE"),
                ("VALIGN", (0, 1), (0, 1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (0, 0), 6),
                ("BOTTOMPADDING", (0, 0), (0, 0), 3),
                ("TOPPADDING", (0, 1), (0, 1), 3),
                ("BOTTOMPADDING", (0, 1), (0, 1), 6),
            ]
        ),
    )


def _lesion_map_block(
    sessions: list[dict[str, Any]],
    codes: list[str],
    photos: _PhotoStore | None,
    colors: dict[str, tuple[int, int, int]],
) -> list[Any]:
    if photos is None:
        return []
    measured = [
        session
        for session in sessions
        if any(_measurement(session, code) for code in codes)
    ]
    if not measured:
        return []
    picks = [measured[0]]
    if measured[-1] is not measured[0]:
        picks.append(measured[-1])

    cards: list[Table] = []
    for session in picks:
        image = photos.get(_code(session))
        if image is None:
            continue
        png = _annotate_lesion_map(image, session, codes, colors)
        caption = (
            f"<b>{_text(_code(session))}</b> · mapa das áreas marcadas · "
            f"{_text(_date(session.get('captured_at'), with_time=False))}<br/>"
            f"Contornos: {_text(', '.join(codes))} · caixa tracejada: ROI de pele saudável"
        )
        card = _framed_image(png, caption, (CONTENT_W - 4 * mm) / 2, max_height=58 * mm)
        if card is not None:
            cards.append(card)
    if not cards:
        return []

    gap = 4 * mm
    while len(cards) < 2:
        cards.append(Spacer(1, 1))
    row = Table([cards[:2]], colWidths=[CONTENT_W / 2, CONTENT_W / 2])
    row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), gap / 2),
                ("LEFTPADDING", (1, 0), (1, 0), gap / 2),
                ("RIGHTPADDING", (1, 0), (1, 0), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return [KeepTogether([row]), Spacer(1, 4 * mm)]


def _executive_summary(
    sessions: list[dict[str, Any]],
    codes: list[str],
    photos: _PhotoStore | None,
    cache: dict[tuple[str, str], dict[str, Any]],
) -> list[Any]:
    styles = _styles()
    base_area = 0.0
    last_area = 0.0
    have_area = False
    de_base: list[float] = []
    de_last: list[float] = []
    for code in codes:
        measured = [session for session in sessions if _measurement(session, code)]
        if not measured:
            continue
        baseline = _lesion_metrics(measured[0], code, photos, cache)
        latest = _lesion_metrics(measured[-1], code, photos, cache)
        if baseline["area"] is not None and latest["area"] is not None:
            base_area += float(baseline["area"])
            last_area += float(latest["area"])
            have_area = True
        if baseline["delta_e_00"] is not None:
            de_base.append(float(baseline["delta_e_00"]))
        if latest["delta_e_00"] is not None:
            de_last.append(float(latest["delta_e_00"]))

    if have_area:
        pct = (last_area - base_area) / base_area * 100.0 if base_area else None
        area_text = (
            f"{_fmt(base_area, 1)} → {_fmt(last_area, 1)} mm²"
            + (f"<br/><b>{_fmt_signed(pct, 1, ' %')}</b> de redução global" if pct is not None else "")
        )
    else:
        area_text = "—"
    if de_base and de_last:
        de_text = (
            f"{_fmt(sum(de_base) / len(de_base), 2)} → "
            f"{_fmt(sum(de_last) / len(de_last), 2)}"
        )
    else:
        de_text = "—"

    scale_entries = [session for session in sessions if session.get("clinical_scales")]
    if scale_entries:
        first = scale_entries[0].get("clinical_scales") or {}
        last = scale_entries[-1].get("clinical_scales") or {}

        def pair(key: str, digits: int = 0) -> str:
            return f"{_fmt(first.get(key), digits)} → {_fmt(last.get(key), digits)}"

        scales_text = (
            f"END {pair('end_pain')}<br/>"
            f"Likert {pair('likert_satisfaction')}<br/>"
            f"GAIS {_fmt_signed(first.get('gais_score'), 0)} → "
            f"{_fmt_signed(last.get('gais_score'), 0)}"
        )
    else:
        scales_text = "—"

    cards = Table(
        [
            [
                Paragraph("Lesões avaliadas", styles["cell-muted"]),
                Paragraph("Área total T0 → Tn", styles["cell-muted"]),
                Paragraph("ΔE<sub>00</sub> médio T0 → Tn", styles["cell-muted"]),
                Paragraph("Escalas clínicas T0 → Tn", styles["cell-muted"]),
            ],
            [
                Paragraph(f"<b>{len(codes)}</b>", styles["cell"]),
                Paragraph(area_text, styles["cell"]),
                Paragraph(de_text, styles["cell"]),
                Paragraph(scales_text, styles["cell"]),
            ],
        ],
        colWidths=[CONTENT_W * 0.18, CONTENT_W * 0.29, CONTENT_W * 0.22, CONTENT_W * 0.31],
    )
    cards.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), BAND),
                ("BACKGROUND", (0, 1), (-1, 1), CARD),
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 4.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
            ]
        )
    )
    return [
        Paragraph("Resumo executivo consolidado", styles["summary"]),
        cards,
        Spacer(1, 3 * mm),
    ]


def _comparison_visual(
    code: str,
    baseline: dict[str, Any],
    latest: dict[str, Any],
    photos: _PhotoStore | None,
    color: tuple[int, int, int],
) -> Table | None:
    if photos is None:
        return None
    base_image = photos.get(_code(baseline))
    last_image = photos.get(_code(latest))
    if base_image is None or last_image is None:
        return None
    base_polygon = (_measurement(baseline, code) or {}).get("polygon") or []
    last_polygon = (_measurement(latest, code) or {}).get("polygon") or []
    if not base_polygon or not last_polygon:
        return None
    base_crop, origin = _crop_region(base_image, [base_polygon, last_polygon])
    last_crop, _ = _crop_region(last_image, [base_polygon, last_polygon])
    if base_crop is None or last_crop is None:
        return None
    if base_crop.shape[:2] != last_crop.shape[:2]:
        try:
            import cv2

            last_crop = cv2.resize(
                last_crop,
                (base_crop.shape[1], base_crop.shape[0]),
                interpolation=cv2.INTER_AREA,
            )
        except Exception:  # noqa: BLE001
            return None
    offset = {"x": origin[0], "y": origin[1]}

    def shifted(polygon: list[Any]) -> list[list[float]]:
        return [[p[0] - offset["x"], p[1] - offset["y"]] for p in polygon]

    _draw_polygon(base_crop, shifted(base_polygon), color, code)
    _draw_polygon(last_crop, shifted(last_polygon), color, code)

    width = (CONTENT_W - 4 * mm) / 2
    first = _framed_image(
        _encode_png(base_crop),
        f"<b>Recorte {_text(_code(baseline))}</b> · "
        f"{_text(_date(baseline.get('captured_at'), with_time=False))}",
        width,
        max_height=42 * mm,
    )
    second = _framed_image(
        _encode_png(last_crop),
        f"<b>Recorte {_text(_code(latest))}</b> · "
        f"{_text(_date(latest.get('captured_at'), with_time=False))}",
        width,
        max_height=42 * mm,
    )
    if first is None or second is None:
        return None
    gap = 4 * mm
    row = Table([[first, second]], colWidths=[CONTENT_W / 2, CONTENT_W / 2])
    row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), gap / 2),
                ("LEFTPADDING", (1, 0), (1, 0), gap / 2),
                ("RIGHTPADDING", (1, 0), (1, 0), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return row


def _lesion_header(
    code: str,
    label: str,
    baseline: dict[str, Any],
    latest: dict[str, Any],
) -> Table:
    styles = _styles()
    title = LESION_TITLES.get(code, f"Região {code}")
    if label:
        title = f"{title} · {label}"
    pct, _ = _delta(baseline.get("area"), latest.get("area"))
    badges = []
    if pct is not None:
        badges.append(f"Δ Área {_fmt_signed(pct, 1, ' %')}")
    if baseline.get("delta_e_00") is not None and latest.get("delta_e_00") is not None:
        badges.append(
            f"ΔE<sub>00</sub> {_fmt(baseline.get('delta_e_00'), 2)} → "
            f"{_fmt(latest.get('delta_e_00'), 2)}"
        )
    badge = " · ".join(badges) or "Sessão única"
    table = Table(
        [
            [
                Paragraph(f"<b>{_text(title)}</b>", styles["summary"]),
                Paragraph(badge, styles["cell-head"]),
            ]
        ],
        colWidths=[CONTENT_W * 0.66, CONTENT_W * 0.34],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), BAND),
                ("BACKGROUND", (1, 0), (1, 0), BRAND),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _lesion_synthesis(
    code: str,
    baseline: dict[str, Any],
    latest: dict[str, Any],
    baseline_metrics: dict[str, Any],
    latest_metrics: dict[str, Any],
) -> Table:
    styles = _styles()
    if _code(baseline) == _code(latest):
        text = (
            "<b>Síntese clínica da lesão:</b> sessão única registrada; ainda não há "
            "variação longitudinal para esta região-alvo."
        )
    else:
        parts = []
        area_pct, area_abs = _delta(baseline_metrics.get("area"), latest_metrics.get("area"))
        if baseline_metrics.get("area") is not None and latest_metrics.get("area") is not None:
            parts.append(
                f"área {_fmt(baseline_metrics.get('area'), 1)} → "
                f"{_fmt(latest_metrics.get('area'), 1)} mm² "
                f"({_fmt_signed(area_abs, 1, ' mm²')}; {_fmt_signed(area_pct, 1, ' %')})"
            )
        width_pct, width_abs = _delta(
            baseline_metrics.get("max_width"), latest_metrics.get("max_width")
        )
        if (
            baseline_metrics.get("max_width") is not None
            and latest_metrics.get("max_width") is not None
        ):
            parts.append(
                f"largura máxima {_fmt(baseline_metrics.get('max_width'), 2)} → "
                f"{_fmt(latest_metrics.get('max_width'), 2)} mm "
                f"({_fmt_signed(width_abs, 2, ' mm')}; {_fmt_signed(width_pct, 1, ' %')})"
            )
        de_pct, de_abs = _delta(
            baseline_metrics.get("delta_e_00"), latest_metrics.get("delta_e_00")
        )
        if (
            baseline_metrics.get("delta_e_00") is not None
            and latest_metrics.get("delta_e_00") is not None
        ):
            parts.append(
                f"ΔE<sub>00</sub> {_fmt(baseline_metrics.get('delta_e_00'), 2)} → "
                f"{_fmt(latest_metrics.get('delta_e_00'), 2)} "
                f"({_fmt_signed(de_abs, 2)})"
            )
        base_lab, last_lab = baseline_metrics.get("lab"), latest_metrics.get("lab")
        if base_lab and last_lab:
            delta_a = last_lab[1] - base_lab[1]
            tendency = "redução do eritema" if delta_a < 0 else (
                "aumento do eritema" if delta_a > 0 else "eritema estável"
            )
            parts.append(
                f"a* {_fmt(base_lab[1], 1)} → {_fmt(last_lab[1], 1)} "
                f"({_fmt_signed(delta_a, 1)}; {tendency})"
            )
        text = (
            f"<b>Síntese clínica da lesão ({_text(_code(baseline))} → "
            f"{_text(_code(latest))}):</b> "
            + ("; ".join(parts) if parts else "sem métricas comparáveis.")
            + "."
        )
    box = Table(
        [[Paragraph(text, styles["small"])]],
        colWidths=[CONTENT_W],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), CARD),
                ("LINEBEFORE", (0, 0), (0, 0), 2.2, ACCENT),
                ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        ),
    )
    return box


def _lesion_block(
    code: str,
    sessions: list[dict[str, Any]],
    photos: _PhotoStore | None,
    colors: dict[str, tuple[int, int, int]],
    cache: dict[tuple[str, str], dict[str, Any]],
) -> list[Any]:
    styles = _styles()
    measured = [session for session in sessions if _measurement(session, code)]
    if not measured:
        return []
    baseline = measured[0]
    latest = measured[-1]
    label = next(
        (
            found
            for found in (_lesion_label(session, code) for session in measured)
            if found
        ),
        "",
    )
    baseline_metrics = _lesion_metrics(baseline, code, photos, cache)
    latest_metrics = _lesion_metrics(latest, code, photos, cache)

    block: list[Any] = [_lesion_header(code, label, baseline_metrics, latest_metrics)]
    visual = _comparison_visual(code, baseline, latest, photos, colors.get(code, (0, 150, 136)))
    if visual is not None:
        block.append(Spacer(1, 3 * mm))
        block.append(visual)

    header = [
        Paragraph("Sessão", styles["cell-head"]),
        Paragraph("Data", styles["cell-head"]),
        Paragraph("Compr.<br/>(mm)", styles["cell-head"]),
        Paragraph("Larg. méd. / máx.<br/>(mm)", styles["cell-head"]),
        Paragraph("Perím.<br/>(mm)", styles["cell-head"]),
        Paragraph("Área<br/>(mm²)", styles["cell-head"]),
        Paragraph("ΔE<sub>00</sub>", styles["cell-head"]),
        Paragraph("L* / a* / b*", styles["cell-head"]),
        Paragraph("Δ Área<br/>vs. T0 (%)", styles["cell-head"]),
    ]
    col_widths = [
        CONTENT_W * 0.075,
        CONTENT_W * 0.115,
        CONTENT_W * 0.085,
        CONTENT_W * 0.145,
        CONTENT_W * 0.085,
        CONTENT_W * 0.085,
        CONTENT_W * 0.075,
        CONTENT_W * 0.185,
        CONTENT_W * 0.15,
    ]
    rows = [header]
    for session in measured:
        metrics = _lesion_metrics(session, code, photos, cache)
        is_baseline = session is baseline
        pct, _ = (None, None) if is_baseline else _delta(
            baseline_metrics.get("area"), metrics.get("area")
        )
        lab = metrics.get("lab")
        lab_text = (
            f"{_fmt(lab[0], 1)} / {_fmt_signed(lab[1], 1)} / {_fmt_signed(lab[2], 1)}"
            if lab
            else "—"
        )
        rows.append(
            [
                Paragraph(f"<b>{_text(_code(session))}</b>", styles["cell"]),
                Paragraph(
                    _text(_date(session.get("captured_at"), with_time=False)),
                    styles["cell"],
                ),
                Paragraph(_fmt(metrics.get("length"), 1), styles["cell"]),
                Paragraph(
                    f"{_fmt(metrics.get('mean_width'), 2)} / "
                    f"{_fmt(metrics.get('max_width'), 2)}",
                    styles["cell"],
                ),
                Paragraph(_fmt(metrics.get("perimeter"), 1), styles["cell"]),
                Paragraph(_fmt(metrics.get("area"), 1), styles["cell"]),
                Paragraph(_fmt(metrics.get("delta_e_00"), 2), styles["cell"]),
                Paragraph(lab_text, styles["cell"]),
                Paragraph(_fmt_signed(pct, 1, " %"), styles["cell"]),
            ]
        )
    table = Table(rows, colWidths=col_widths, repeatRows=1)
    table.setStyle(_grid_style())
    block.append(Spacer(1, 3 * mm))
    block.append(table)
    block.append(Spacer(1, 2.5 * mm))
    block.append(
        _lesion_synthesis(code, baseline, latest, baseline_metrics, latest_metrics)
    )
    return block


def _lesion_codes(sessions: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    for session in sessions:
        for measurement in session.get("measurements") or []:
            code = str(measurement.get("stria_code") or "")
            if code:
                seen.add(code)
    ordered = [code for code in LESION_CODES if code in seen]
    ordered += [code for code in LEGACY_CODES if code in seen]
    ordered += sorted(seen.difference(ordered))
    return ordered


def _longitudinal(
    sessions: list[dict[str, Any]],
    photos: _PhotoStore | None = None,
) -> list[Any]:
    styles = _styles()
    story: list[Any] = [Paragraph("2. Comparativo longitudinal por lesão", styles["h2"])]
    codes = _lesion_codes(sessions)
    if not codes:
        story.append(
            Paragraph(
                "Nenhuma região-alvo foi medida até o momento. Meça as lesões no "
                "workspace para compor a tabela comparativa.",
                styles["body"],
            )
        )
        return story

    colors = _lesion_colors(codes)
    cache: dict[tuple[str, str], dict[str, Any]] = {}

    # Mapa anatômico de T0 e da última sessão, logo na abertura da secção.
    story.extend(_lesion_map_block(sessions, codes, photos, colors))
    story.extend(_executive_summary(sessions, codes, photos, cache))

    for code in codes:
        # Cada lesão (cabeçalho + recortes + tabela + síntese) é indivisível.
        block = _lesion_block(code, sessions, photos, colors, cache)
        if block:
            story.append(KeepTogether(block))
            story.append(Spacer(1, 4.5 * mm))
    return story


def _clinical_scales(sessions: list[dict[str, Any]]) -> list[Any]:
    styles = _styles()
    story: list[Any] = [Paragraph("3. Escalas clínicas subjetivas", styles["h2"])]
    entries = [s for s in sessions if s.get("clinical_scales")]
    if not entries:
        story.append(
            Paragraph(
                "Nenhuma escala subjetiva (END, Likert, GAIS) foi registrada nas sessões "
                "avaliadas.",
                styles["body"],
            )
        )
        return story

    header = [
        Paragraph("Sessão", styles["cell-head"]),
        Paragraph("Data da coleta", styles["cell-head"]),
        Paragraph("END — dor (0–10)", styles["cell-head"]),
        Paragraph("Likert satisfação (1–5)", styles["cell-head"]),
        Paragraph("GAIS (−1 a +3)", styles["cell-head"]),
        Paragraph("Observações", styles["cell-head"]),
    ]
    rows = [header]
    for session in entries:
        scales = session.get("clinical_scales") or {}
        rows.append(
            [
                Paragraph(f"<b>{_text(_code(session))}</b>", styles["cell"]),
                Paragraph(_text(_date(session.get("captured_at"))), styles["cell"]),
                Paragraph(_fmt(scales.get("end_pain"), 0), styles["cell"]),
                Paragraph(_fmt(scales.get("likert_satisfaction"), 0), styles["cell"]),
                Paragraph(_fmt_signed(scales.get("gais_score"), 0), styles["cell"]),
                Paragraph(_text(scales.get("notes") or "—"), styles["cell"]),
            ]
        )
    table = Table(
        rows,
        colWidths=[
            CONTENT_W * 0.08,
            CONTENT_W * 0.17,
            CONTENT_W * 0.14,
            CONTENT_W * 0.16,
            CONTENT_W * 0.13,
            CONTENT_W * 0.32,
        ],
        repeatRows=1,
    )
    table.setStyle(_grid_style())
    story.append(table)
    return story


def _prepare_image(data: bytes, max_px: int = 900) -> io.BytesIO | None:
    """Reamostra a foto para manter o PDF leve sem perder legibilidade da tarja."""
    if PILImage is None:
        return io.BytesIO(data) if data else None
    try:
        with PILImage.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            if max(image.size) > max_px:
                scale = max_px / float(max(image.size))
                image = image.resize(
                    (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                    PILImage.LANCZOS,
                )
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=88, optimize=True)
            buffer.seek(0)
            return buffer
    except Exception:  # noqa: BLE001 - imagem corrompida: segue sem a foto
        return None


def _photo_cell(entry: dict[str, Any]) -> Table:
    styles = _styles()
    gap = 4 * mm
    card_w = (CONTENT_W - gap) / 2
    pad = 6
    inner_w = card_w - 2 * pad
    data = entry.get("aligned_png") or entry.get("enhanced_png") or entry.get("image_png")
    buffer = _prepare_image(data) if data else None

    if buffer is None:
        caption = Paragraph("Imagem indisponível", styles["cell-muted"])
        return Table(
            [[Paragraph("&nbsp;", styles["small"])], [caption]],
            colWidths=[card_w],
            style=TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                    ("BACKGROUND", (0, 0), (-1, -1), CARD),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("TOPPADDING", (0, 0), (-1, -1), pad),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
                ]
            ),
        )

    width_px, height_px = 4, 3
    if PILImage is not None:
        try:
            with PILImage.open(buffer) as probe:
                width_px, height_px = probe.size
            buffer.seek(0)
        except Exception:  # noqa: BLE001 - metadados ilegíveis: usa 4:3
            width_px, height_px = 4, 3
    ratio = height_px / float(width_px) if width_px else 0.75
    width_pt = inner_w
    height_pt = width_pt * ratio
    max_height = 58 * mm
    if height_pt > max_height:
        height_pt = max_height
        width_pt = height_pt / ratio if ratio else inner_w

    if entry.get("aligned_png"):
        detail = "Alinhada"
    elif entry.get("enhanced_png"):
        detail = "Realce CIELAB"
    else:
        detail = "Original"
    scale = entry.get("px_per_mm")
    scale_text = f"{_fmt(scale, 2)} px/mm" if scale else "sem calibração"
    caption = Paragraph(
        f"<b>{_text(entry.get('session_code') or '—')}</b> · "
        f"{_text(_date(entry.get('captured_at')))} · "
        f"{_text(scale_text)} · <b>{_text(detail)}</b>",
        styles["caption"],
    )
    image_row = [RLImage(buffer, width=width_pt, height=height_pt)]
    card = Table(
        [image_row, [caption]],
        colWidths=[card_w],
        style=TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("BACKGROUND", (0, 0), (-1, -1), CARD),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (0, 0), "MIDDLE"),
                ("VALIGN", (0, 1), (0, 1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), pad),
                ("RIGHTPADDING", (0, 0), (-1, -1), pad),
                ("TOPPADDING", (0, 0), (0, 0), pad),
                ("BOTTOMPADDING", (0, 0), (0, 0), 3),
                ("TOPPADDING", (0, 1), (0, 1), 3),
                ("BOTTOMPADDING", (0, 1), (0, 1), pad),
            ]
        ),
    )
    return card


def _photo_grid(sessions: list[dict[str, Any]], images: list[dict[str, Any]]) -> list[Any]:
    styles = _styles()
    heading = Paragraph("4. Registro fotográfico comparativo", styles["h2"])
    story: list[Any] = []
    entries = images or [
        {
            "session_code": _code(session),
            "captured_at": session.get("captured_at"),
            "px_per_mm": session.get("px_per_mm"),
        }
        for session in sessions
        if session.get("aligned_image_url")
    ]
    entries = [entry for entry in entries if entry.get("aligned_png") or entry.get("image_png") or entry.get("enhanced_png")]
    if not entries:
        story.append(heading)
        story.append(
            Paragraph(
                "Nenhuma imagem alinhada disponível. Conclua o passo de alinhamento "
                "anatômico para compor o registro fotográfico comparativo.",
                styles["body"],
            )
        )
        return story

    note = Paragraph(
        "Fotografias de exibição alinhadas ao referencial do T0, em escala "
        "comparável e com tarja de rastreabilidade (paciente, sessão e data). "
        "O arquivo original sem tarja permanece preservado para o motor de cálculo.",
        styles["note"],
    )
    cards = [_photo_cell(entry) for entry in entries]
    gap = 4 * mm
    rows: list[Table] = []
    for index in range(0, len(cards), 2):
        pair = cards[index:index + 2]
        while len(pair) < 2:
            pair.append(Spacer(1, 1))
        row = Table([pair], colWidths=[CONTENT_W / 2, CONTENT_W / 2])
        row.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (0, 0), 0),
                    ("RIGHTPADDING", (0, 0), (0, 0), gap / 2),
                    ("LEFTPADDING", (1, 0), (1, 0), gap / 2),
                    ("RIGHTPADDING", (1, 0), (1, 0), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        rows.append(row)

    # Título + nota acompanham a primeira linha para nunca ficarem órfãos.
    story.append(KeepTogether([heading, note, rows[0]]))
    story.append(Spacer(1, 3 * mm))
    for row in rows[1:]:
        # Uma linha da grade nunca se divide entre páginas.
        story.append(KeepTogether([row]))
        story.append(Spacer(1, 3 * mm))
    return story


def _closing(
    comparison: dict[str, Any],
    fingerprint: str,
    profile: dict[str, Any] | None = None,
) -> list[Any]:
    styles = _styles()
    story: list[Any] = [Spacer(1, 4 * mm)]
    story.append(Paragraph("Validação e assinatura profissional", styles["h2"]))
    statement = (
        "Declaro que as informações deste laudo refletem os dados coletados e "
        "processados na plataforma DermaMetric para o paciente identificado, sob minha "
        "responsabilidade técnica."
    )
    story.append(Paragraph(_text(statement), styles["body"]))
    story.append(Spacer(1, 8 * mm))

    profile = profile or {}
    name = str(profile.get("professional_name") or "").strip()
    registry = str(profile.get("professional_registry") or "").strip()
    institution = str(profile.get("institution") or "").strip()
    signature_title = name or "Assinatura do profissional / pesquisador responsável"
    hint_parts = [part for part in (registry, institution) if part]
    signature_hint = " · ".join(hint_parts) or "Nome e registro (CRM / COREN / conselho)"

    emitted_at = comparison.get("generated_at_label") or _date(comparison.get("generated_at"))

    signature_cells = []
    for title, hint in (
        (signature_title, signature_hint),
        ("Data da assinatura", emitted_at),
    ):
        signature_cells.append(
            Table(
                [
                    [Paragraph("&nbsp;", styles["small"])],
                    [Paragraph(_text(title), styles["signature"])],
                    [Paragraph(_text(hint), styles["small"])],
                ],
                colWidths=[CONTENT_W * 0.46],
                style=TableStyle(
                    [
                        ("LINEABOVE", (0, 0), (0, 0), 0.7, INK),
                        ("TOPPADDING", (0, 0), (0, 0), 14),
                        ("BOTTOMPADDING", (0, 0), (0, 0), 1),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ]
                ),
            )
        )
    story.append(
        Table(
            [signature_cells],
            colWidths=[CONTENT_W * 0.58, CONTENT_W * 0.42],
            style=TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM")]),
        )
    )
    story.append(Spacer(1, 5 * mm))

    integrity = Table(
        [
            [
                Paragraph("Fingerprint de integridade (SHA-256)", styles["meta-label"]),
                Paragraph(_text(fingerprint), styles["cell-mono"]),
            ],
            [
                Paragraph("Emissão do laudo", styles["meta-label"]),
                Paragraph(
                    f"{_text(emitted_at)}"
                    + (f" · {_text(institution)}" if institution else ""),
                    styles["cell"],
                ),
            ],
        ],
        colWidths=[CONTENT_W * 0.32, CONTENT_W * 0.68],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), CARD),
                ("BACKGROUND", (0, 0), (0, -1), BAND),
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        ),
    )
    story.append(integrity)
    return story


# ---------------------------------------------------------------------------
# Canvas com numeração e rodapé LGPD
# ---------------------------------------------------------------------------
class _NumberedCanvas(pdfcanvas.Canvas):
    """Adia a paginação para conhecer o total de páginas do documento."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict[str, Any]] = []

    def showPage(self) -> None:  # noqa: N802 - API do ReportLab
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:  # noqa: D401 - API do ReportLab
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total: int) -> None:
        fonts = _register_fonts()
        y = 13 * mm
        self.saveState()
        self.setStrokeColor(LINE)
        self.setLineWidth(0.5)
        self.line(MARGIN_X, y + 9, PAGE_W - MARGIN_X, y + 9)
        self.setFont(fonts["regular"], 6.8)
        self.setFillColor(MUTED)
        self.drawString(MARGIN_X, y + 2.5, _FOOTER_NOTICE)
        self.setFont(fonts["bold"], 7.4)
        self.setFillColor(INK)
        self.drawRightString(
            PAGE_W - MARGIN_X, y + 2.5, f"Página {self._pageNumber} de {total}"
        )
        self.restoreState()


def _draw_running_header(canvas: pdfcanvas.Canvas, doc: BaseDocTemplate) -> None:
    if doc.page <= 1:
        return
    fonts = _register_fonts()
    canvas.saveState()
    brand_text = "DermaMetric"
    canvas.setFont(fonts["bold"], 7.6)
    canvas.setFillColor(BRAND)
    canvas.drawString(MARGIN_X, PAGE_H - 13 * mm, brand_text)
    brand_w = pdfmetrics.stringWidth(brand_text, fonts["bold"], 7.6)
    canvas.setFont(fonts["regular"], 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(
        MARGIN_X + brand_w + 4,
        PAGE_H - 13 * mm,
        "· Laudo longitudinal de fotogrametria e morfometria cutânea",
    )
    patient = getattr(doc, "_dm_patient_code", "")
    if patient:
        canvas.setFont(fonts["bold"], 7.2)
        canvas.setFillColor(INK)
        canvas.drawRightString(PAGE_W - MARGIN_X, PAGE_H - 13 * mm, str(patient))
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN_X, PAGE_H - 15 * mm, PAGE_W - MARGIN_X, PAGE_H - 15 * mm)
    canvas.restoreState()


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def report_filename(patient: dict[str, Any], when: datetime | None = None) -> str:
    """Nome de arquivo estável para ``Content-Disposition``."""
    code = "".join(ch for ch in str(patient.get("code") or "paciente") if ch.isalnum() or ch in "-_")
    stamp = (when or datetime.now()).strftime("%Y%m%d")
    return f"DermaMetric_{code or 'paciente'}_{stamp}.pdf"


def build_patient_pdf_report(
    patient: dict[str, Any],
    sessions: list[dict[str, Any]],
    comparison: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> bytes:
    """Compõe o laudo longitudinal completo e devolve os bytes do PDF.

    ``comparison`` carrega os dados resolvidos pelo chamador:

    - ``generated_at``: ISO da emissão (opcional).
    - ``images``: lista de ``{session_code, captured_at, px_per_mm, aligned_png}``.
    - ``fingerprint``: fingerprint pré-calculado (opcional).

    ``profile`` traz o profissional/pesquisador responsável
    (``professional_name``, ``professional_registry``, ``institution``).
    """
    styles = _styles()
    ordered = _sort_sessions(sessions or [])
    comparison = dict(comparison or {})
    profile = dict(profile or {})
    institution = str(profile.get("institution") or "").strip()

    generated = _parse_datetime(comparison.get("generated_at")) or datetime.now()
    comparison["generated_at_label"] = generated.strftime("%d/%m/%Y %H:%M")
    fingerprint = comparison.get("fingerprint") or _fingerprint(patient, ordered)
    comparison["fingerprint"] = fingerprint
    comparison.setdefault("_sessions", ordered)

    story: list[Any] = []
    story.append(_band(institution))
    story.append(
        Paragraph(
            "DermaMetric — Relatório Longitudinal de Fotogrametria e Morfometria Cutânea",
            styles["title"],
        )
    )
    story.append(
        Paragraph(
            "Instrumento de apoio à decisão clínica: mensuração objetiva de lesões "
            "cutâneas e evolução colorimétrica entre sessões. Não substitui o "
            "julgamento do profissional responsável.",
            styles["small"],
        )
    )
    story.append(Spacer(1, 5))
    story.append(_meta_table(patient, comparison))
    story.append(Spacer(1, 2))
    photos = _PhotoStore(comparison.get("images") or [])
    story.extend(_methodology(ordered))
    story.extend(_longitudinal(ordered, photos))
    story.extend(_clinical_scales(ordered))
    story.extend(_photo_grid(ordered, comparison.get("images") or []))
    story.append(KeepTogether(_closing(comparison, fingerprint, profile)))

    buffer = io.BytesIO()
    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN_X,
        rightMargin=MARGIN_X,
        topMargin=TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
        title=f"DermaMetric — Laudo longitudinal — {patient.get('code') or ''}".strip(),
        author="DermaMetric",
        subject="Relatório longitudinal de fotogrametria e morfometria cutânea",
        creator="DermaMetric",
    )
    doc._dm_patient_code = patient.get("code") or ""  # type: ignore[attr-defined]
    frame = Frame(
        MARGIN_X,
        BOTTOM_MARGIN,
        CONTENT_W,
        PAGE_H - TOP_MARGIN - BOTTOM_MARGIN,
        id="dm-body",
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    doc.addPageTemplates(
        [PageTemplate(id="dm-page", frames=[frame], onPage=_draw_running_header)]
    )
    doc.build(story, canvasmaker=_NumberedCanvas)
    data = buffer.getvalue()
    buffer.close()
    if not data.startswith(b"%PDF"):
        raise RuntimeError("Falha ao gerar o PDF do laudo")
    return data


__all__ = [
    "build_patient_pdf_report",
    "report_filename",
]
