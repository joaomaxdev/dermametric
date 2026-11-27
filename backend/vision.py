"""Motor de visão computacional do DermaMetric.

Pipeline de fotogrametria clínica para lesões e regiões cutâneas:

1. Calibração métrica pela régua milimetrada (``compute_px_per_mm``).
2. Alinhamento por homografia contra a referência T0 (``align_to_reference``).
3. Normalização de exposição/balanço de branco em CIELAB e realce de
   relevo com CLAHE (``normalize_and_enhance_cielab``).
4. Morfometria da região-alvo e contraste perceptual CIEDE2000
   (``measure_stria_polygon``).

Convenções:
- Imagens em disco: BGR, 8 bits (OpenCV).
- Cores CIELAB são sempre reportadas em unidades reais:
  ``L*`` em [0, 100], ``a*`` e ``b*`` com sinal (D65, 2°).
- ``output_shape`` é ``(altura, largura)``, como em ``cv2.warpPerspective``.
- Distâncias físicas em milímetros; calibração em pixels por milímetro.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Constantes fotométricas (sRGB / D65 / observador 2°)
# ---------------------------------------------------------------------------
_WHITE_D65 = np.array([0.95047, 1.00000, 1.08883], dtype=np.float64)
_RGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)
_XYZ_TO_RGB = np.array(
    [
        [3.2404542, -1.5371385, -0.4985314],
        [-0.9692660, 1.8760108, 0.0415560],
        [0.0556434, -0.2040259, 1.0572252],
    ],
    dtype=np.float64,
)
_LAB_EPS = (6.0 / 29.0) ** 3
_LAB_KAPPA_SLOPE = 1.0 / (3.0 * (6.0 / 29.0) ** 2)
_LAB_OFFSET = 4.0 / 29.0


# ---------------------------------------------------------------------------
# I/O robusto
# ---------------------------------------------------------------------------
def read_image_bgr(path: str | Path) -> np.ndarray:
    """Lê uma imagem colorida (BGR, uint8) aceitando caminhos arbitrários."""
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Não foi possível ler a imagem: {path}")
    return image


def imdecode_bgr(data: bytes) -> np.ndarray:
    """Decodifica bytes de imagem (BGR, uint8) sem tocar no disco."""
    buffer = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Não foi possível decodificar a imagem enviada")
    return image


def write_image_bgr(path: str | Path, image: np.ndarray) -> str:
    """Grava uma imagem (BGR) preservando a extensão e criando diretórios."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = target.suffix.lower() or ".png"
    ok, buffer = cv2.imencode(suffix, image)
    if not ok:
        raise ValueError(f"Falha ao codificar a imagem: {path}")
    buffer.tofile(str(target))
    return str(target)


def encode_image_bgr(image: np.ndarray, suffix: str = ".png") -> bytes:
    """Codifica uma imagem BGR em memória (padrão PNG, sem perdas)."""
    ok, buffer = cv2.imencode(suffix or ".png", image)
    if not ok:
        raise ValueError("Falha ao codificar a imagem")
    return buffer.tobytes()


# ---------------------------------------------------------------------------
# Marca d'água clínica (não destrutiva, apenas para visualização/exportação)
# ---------------------------------------------------------------------------
# Ordem de preferência: DejaVu costuma existir em ambientes CI; os demais são
# os substitutos usuais em distribuições Linux sem fontes da Mozilla/Red Hat.
_WATERMARK_FONTS = (
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/OpenSans/OpenSans-Regular.ttf",
    "/usr/share/fonts/gsfonts/NimbusSans-Regular.otf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
)
_WATERMARK_FONT_CACHE: dict[int, object] = {}
_WATERMARK_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
)


def _watermark_font(size: int):
    """Carrega (com cache) uma fonte TrueType fina; ``None`` se indisponível."""
    if size in _WATERMARK_FONT_CACHE:
        return _WATERMARK_FONT_CACHE[size]
    from PIL import ImageFont  # importação tardia: Pillow é opcional no motor

    font = None
    for candidate in _WATERMARK_FONTS:
        try:
            font = ImageFont.truetype(candidate, size)
            break
        except (OSError, ValueError):
            continue
    _WATERMARK_FONT_CACHE[size] = font
    return font


def _format_watermark_datetime(value: str | None) -> str | None:
    """Normaliza ``captured_at`` para ``DD/MM/AAAA HH:MM`` (omite se inválido)."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        pass
    for fmt in _WATERMARK_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%d/%m/%Y %H:%M")
        except ValueError:
            continue
    return None


def watermark_text(
    captured_at: str | None, session_id: str, patient_code: str | None = None
) -> str:
    """Monta o texto da tarja, omitindo campos ausentes."""
    parts = ["DermaMetric"]
    parts.append(str(patient_code).strip() if patient_code else "")
    parts.append(str(session_id).strip() if session_id else "")
    parts.append(_format_watermark_datetime(captured_at) or "")
    return " · ".join(part for part in parts if part)


def apply_clinical_watermark(
    image_bgr: np.ndarray,
    captured_at: str | None,
    session_id: str,
    patient_code: str | None = None,
) -> np.ndarray:
    """Devolve uma **cópia** da imagem com tarja discreta no canto inferior direito.

    A tarja é composta por um retângulo escuro semitransparente (~55%) e texto
    branco fino, dimensionado proporcionalmente à altura da imagem. Nenhum
    pixel fora da tarja é alterado — o original nunca é modificado — de modo
    que a imagem limpa usada pelo motor CIELAB (``measure_stria_polygon``,
    ``normalize_and_enhance_cielab``) permanece intacta.
    """
    if image_bgr is None or image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
        raise ValueError("Imagem BGR deve ter shape (H, W, 3)")

    marked = np.array(image_bgr, dtype=np.uint8, copy=True)
    height, width = marked.shape[:2]
    if height < 48 or width < 48:
        # Miniatura: a tarja ocuparia a região útil; nada é desenhado.
        return marked

    label = watermark_text(captured_at, session_id, patient_code)
    if not label:
        return marked

    font_size = max(9, int(round(height * 0.014)))

    from PIL import Image, ImageDraw

    font = _watermark_font(font_size)
    margin = max(4, int(round(min(width, height) * 0.012)))

    if font is not None:
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        box = probe.textbbox((0, 0), label, font=font)
        text_w, text_h = max(1, box[2] - box[0]), max(1, box[3] - box[1])
        pad_x = max(6, int(round(font_size * 0.75)))
        pad_y = max(3, int(round(font_size * 0.38)))
        rect_w = min(width - 2 * margin, text_w + 2 * pad_x)
        rect_h = min(height - 2 * margin, text_h + 2 * pad_y)
        if rect_w <= 2 or rect_h <= 2:
            return marked
        x1 = max(0, width - margin - rect_w)
        y1 = max(0, height - margin - rect_h)
        x2, y2 = x1 + rect_w, y1 + rect_h

        base_rgb = cv2.cvtColor(marked, cv2.COLOR_BGR2RGB)
        patch = Image.fromarray(base_rgb[y1:y2, x1:x2]).convert("RGBA")
        tile = Image.new("RGBA", patch.size, (0, 0, 0, 0))
        tile_draw = ImageDraw.Draw(tile)
        tile_draw.rounded_rectangle(
            [0, 0, rect_w - 1, rect_h - 1],
            radius=max(2, int(round(rect_h * 0.3))),
            fill=(10, 16, 20, 140),  # ~55% de opacidade
        )
        tile_draw.text(
            (pad_x - box[0], pad_y - box[1]),
            label,
            font=font,
            fill=(255, 255, 255, 235),
        )
        merged = Image.alpha_composite(patch, tile).convert("RGB")
        marked[y1:y2, x1:x2] = cv2.cvtColor(np.array(merged), cv2.COLOR_RGB2BGR)
        return marked

    # Fallback (sem TrueType): desenha com a fonte Hershey do OpenCV.
    ascii_label = label.replace(" · ", " | ")
    scale = max(0.32, height * 0.014 / 22.0)
    thickness = 1
    (text_w, text_h), baseline = cv2.getTextSize(
        ascii_label, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness
    )
    pad_x = max(6, int(round(scale * 14)))
    pad_y = max(3, int(round(scale * 10)))
    rect_w = min(width - 2 * margin, text_w + 2 * pad_x)
    rect_h = min(height - 2 * margin, text_h + baseline + 2 * pad_y)
    if rect_w <= 2 or rect_h <= 2:
        return marked
    x1 = max(0, width - margin - rect_w)
    y1 = max(0, height - margin - rect_h)
    x2, y2 = x1 + rect_w, y1 + rect_h

    patch = marked[y1:y2, x1:x2].copy()
    patch[:] = (patch * 0.45 + np.array([10, 16, 20]) * 0.55).astype(np.uint8)
    marked[y1:y2, x1:x2] = patch
    cv2.putText(
        marked,
        ascii_label,
        (x1 + pad_x, y2 - pad_y - baseline),
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (255, 255, 255),
        thickness,
        cv2.LINE_AA,
    )
    return marked


# ---------------------------------------------------------------------------
# Cor: sRGB <-> CIELAB (float, sem quantização de 8 bits)
# ---------------------------------------------------------------------------
def bgr_to_cielab_float(image_bgr: np.ndarray) -> np.ndarray:
    """Converte BGR uint8 em CIELAB float64 real (L*: 0-100, a*/b*: com sinal)."""
    if image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
        raise ValueError("Imagem BGR deve ter shape (H, W, 3)")
    rgb = image_bgr[..., ::-1].astype(np.float64) / 255.0

    # Linearização sRGB.
    linear = np.where(
        rgb <= 0.04045,
        rgb / 12.92,
        ((rgb + 0.055) / 1.055) ** 2.4,
    )
    xyz = linear @ _RGB_TO_XYZ.T
    xyz = xyz / _WHITE_D65

    f = np.where(
        xyz > _LAB_EPS,
        np.cbrt(xyz),
        _LAB_KAPPA_SLOPE * xyz + _LAB_OFFSET,
    )
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]
    lab = np.empty_like(f)
    lab[..., 0] = 116.0 * fy - 16.0
    lab[..., 1] = 500.0 * (fx - fy)
    lab[..., 2] = 200.0 * (fy - fz)
    return lab


def cielab_to_bgr_float(lab: np.ndarray) -> np.ndarray:
    """Converte CIELAB real (float) em BGR float64 no intervalo [0, 1]."""
    lab = np.asarray(lab, dtype=np.float64)
    fy = (lab[..., 0] + 16.0) / 116.0
    fx = fy + lab[..., 1] / 500.0
    fz = fy - lab[..., 2] / 200.0
    f = np.stack([fx, fy, fz], axis=-1)

    xyz = np.where(
        f > 6.0 / 29.0,
        f**3,
        (f - _LAB_OFFSET) / _LAB_KAPPA_SLOPE,
    )
    xyz = xyz * _WHITE_D65

    linear = xyz @ _XYZ_TO_RGB.T
    rgb = np.where(
        linear <= 0.0031308,
        12.92 * linear,
        1.055 * np.power(np.clip(linear, 0.0, None), 1.0 / 2.4) - 0.055,
    )
    rgb = np.clip(rgb, 0.0, 1.0)
    return rgb[..., ::-1]


def cielab_to_bgr_u8(lab: np.ndarray) -> np.ndarray:
    """Converte CIELAB real em BGR uint8 (para gravação em disco)."""
    bgr = cielab_to_bgr_float(lab)
    return np.clip(np.rint(bgr * 255.0), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# CIEDE2000 (Sharma et al., 2005) — vetorizado
# ---------------------------------------------------------------------------
def delta_e_ciede2000(
    lab1: Iterable[float] | np.ndarray,
    lab2: Iterable[float] | np.ndarray,
    k_l: float = 1.0,
    k_c: float = 1.0,
    k_h: float = 1.0,
) -> float | np.ndarray:
    """Diferença de cor perceptual CIEDE2000 entre pares CIELAB.

    Aceita dois vetores ``(L, a, b)`` (retorna float) ou arrays ``(N, 3)``
    (retorna array ``(N,)``).
    """
    a1 = np.asarray(lab1, dtype=np.float64)
    a2 = np.asarray(lab2, dtype=np.float64)
    scalar = a1.ndim == 1
    if scalar:
        a1 = a1.reshape(1, 3)
        a2 = a2.reshape(1, 3)
    if a1.shape[-1] != 3 or a2.shape[-1] != 3:
        raise ValueError("CIEDE2000 exige triplas CIELAB (L, a, b)")
    a1, a2 = np.broadcast_arrays(a1, a2)

    L1, A1, B1 = a1[..., 0], a1[..., 1], a1[..., 2]
    L2, A2, B2 = a2[..., 0], a2[..., 1], a2[..., 2]

    c1 = np.hypot(A1, B1)
    c2 = np.hypot(A2, B2)
    c_bar = 0.5 * (c1 + c2)

    c_bar7 = c_bar**7
    g = 0.5 * (1.0 - np.sqrt(c_bar7 / (c_bar7 + 25.0**7)))

    a1p = (1.0 + g) * A1
    a2p = (1.0 + g) * A2
    c1p = np.hypot(a1p, B1)
    c2p = np.hypot(a2p, B2)

    h1p = np.degrees(np.arctan2(B1, a1p)) % 360.0
    h2p = np.degrees(np.arctan2(B2, a2p)) % 360.0
    h1p = np.where(c1p == 0.0, 0.0, h1p)
    h2p = np.where(c2p == 0.0, 0.0, h2p)

    d_lp = L2 - L1
    d_cp = c2p - c1p

    dh = h2p - h1p
    d_hp = np.where(
        c1p * c2p == 0.0,
        0.0,
        np.where(
            np.abs(dh) <= 180.0,
            dh,
            np.where(dh > 180.0, dh - 360.0, dh + 360.0),
        ),
    )
    d_big_hp = 2.0 * np.sqrt(c1p * c2p) * np.sin(np.radians(d_hp / 2.0))

    l_barp = 0.5 * (L1 + L2)
    c_barp = 0.5 * (c1p + c2p)

    h_sum = h1p + h2p
    h_barp = np.where(
        c1p * c2p == 0.0,
        h_sum,
        np.where(
            np.abs(h1p - h2p) <= 180.0,
            h_sum / 2.0,
            np.where(h_sum < 360.0, (h_sum + 360.0) / 2.0, (h_sum - 360.0) / 2.0),
        ),
    )

    t = (
        1.0
        - 0.17 * np.cos(np.radians(h_barp - 30.0))
        + 0.24 * np.cos(np.radians(2.0 * h_barp))
        + 0.32 * np.cos(np.radians(3.0 * h_barp + 6.0))
        - 0.20 * np.cos(np.radians(4.0 * h_barp - 63.0))
    )

    d_theta = 30.0 * np.exp(-(((h_barp - 275.0) / 25.0) ** 2))
    c_barp7 = c_barp**7
    r_c = 2.0 * np.sqrt(c_barp7 / (c_barp7 + 25.0**7))
    r_t = -np.sin(np.radians(2.0 * d_theta)) * r_c

    s_l = 1.0 + (0.015 * (l_barp - 50.0) ** 2) / np.sqrt(
        20.0 + (l_barp - 50.0) ** 2
    )
    s_c = 1.0 + 0.045 * c_barp
    s_h = 1.0 + 0.015 * c_barp * t

    term_l = d_lp / (k_l * s_l)
    term_c = d_cp / (k_c * s_c)
    term_h = d_big_hp / (k_h * s_h)

    delta = np.sqrt(
        term_l**2 + term_c**2 + term_h**2 + r_t * term_c * term_h
    )
    if scalar:
        return float(delta.reshape(-1)[0])
    return delta


# ---------------------------------------------------------------------------
# 1. Calibração métrica
# ---------------------------------------------------------------------------
def compute_px_per_mm(
    p1: tuple[float, float] | list[float],
    p2: tuple[float, float] | list[float],
    known_mm: float,
) -> float:
    """Converte a distância conhecida entre dois pontos da régua em px/mm."""
    if known_mm <= 0:
        raise ValueError("known_mm deve ser maior que zero")
    distance_px = float(np.hypot(p2[0] - p1[0], p2[1] - p1[1]))
    if distance_px <= 0:
        raise ValueError("Os pontos da régua não podem coincidir")
    return distance_px / float(known_mm)


# ---------------------------------------------------------------------------
# 2. Alinhamento por homografia
# ---------------------------------------------------------------------------
def _as_point_array(points: Iterable[Iterable[float]], name: str) -> np.ndarray:
    array = np.asarray(points, dtype=np.float64)
    if array.shape != (4, 2):
        raise ValueError(f"{name} deve conter exatamente 4 pontos [x, y]")
    if np.linalg.matrix_rank(array - array.mean(axis=0)) < 2:
        raise ValueError(f"{name} degenerados: pontos colineares")
    return array


def align_to_reference(
    target_img_path: str,
    src_points: list[list[float]],
    ref_points: list[list[float]],
    output_shape: tuple[int, int],
    output_path: str,
    *,
    is_reference: bool = False,
    return_homography: bool = False,
) -> str | tuple[str, np.ndarray | None]:
    """Alinha a foto limpa da sessão atual à geometria da referência T0.

    Args:
        target_img_path: imagem limpa da sessão atual.
        src_points: 4 pontos anatômicos na sessão atual.
        ref_points: os mesmos 4 pontos na referência T0.
        output_shape: ``(altura, largura)`` da imagem de referência.
        output_path: caminho do PNG alinhado de saída.
        is_reference: quando ``True`` (sessão T0), apenas padroniza o
            tamanho da imagem, sem homografia.
        return_homography: quando ``True``, devolve também a matriz
            ``H`` (3x3) que leva coordenadas da sessão atual para o espaço
            alinhado de T0 (``None`` para a própria referência).

    Returns:
        O caminho da imagem alinhada ou ``(caminho, H)`` quando
        ``return_homography`` é verdadeiro.
    """
    image = read_image_bgr(target_img_path)
    height, width = int(output_shape[0]), int(output_shape[1])
    if height <= 0 or width <= 0:
        raise ValueError("output_shape inválido")

    if is_reference:
        if image.shape[:2] != (height, width):
            image = cv2.resize(
                image, (width, height), interpolation=cv2.INTER_LANCZOS4
            )
        written = write_image_bgr(output_path, image)
        return (written, None) if return_homography else written

    src = _as_point_array(src_points, "src_points")
    ref = _as_point_array(ref_points, "ref_points")

    homography, mask = cv2.findHomography(
        src, ref, method=0, ransacReprojThreshold=5.0
    )
    if homography is None or (mask is not None and int(mask.sum()) < 4):
        raise ValueError("Não foi possível estimar a homografia entre as sessões")

    aligned = cv2.warpPerspective(
        image,
        homography,
        (width, height),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
    written = write_image_bgr(output_path, aligned)
    return (written, homography) if return_homography else written


def project_roi(
    roi: Iterable[int],
    homography: np.ndarray,
    output_shape: tuple[int, int],
) -> list[int] | None:
    """Projeta ``[x, y, w, h]`` para o espaço alinhado por ``homography``.

    A ROI é tratada como o retângulo dos 4 cantos, não apenas a translação do
    canto superior esquerdo. Devolve ``None`` quando a projeção sai
    substancialmente dos limites da imagem alinhada, para que o chamador
    possa recorrer à ROI de referência de T0.
    """
    x, y, w, h = (float(v) for v in roi)
    if w <= 0 or h <= 0:
        raise ValueError("healthy_skin_roi deve ter largura/altura positivas")

    matrix = np.asarray(homography, dtype=np.float64)
    if matrix.shape != (3, 3):
        raise ValueError("homografia deve ser uma matriz 3x3")

    corners = np.array(
        [[[x, y], [x + w, y], [x + w, y + h], [x, y + h]]],
        dtype=np.float64,
    )
    projected = cv2.perspectiveTransform(corners, matrix)[0]
    x0, y0 = projected.min(axis=0)
    x1, y1 = projected.max(axis=0)

    height, width = int(output_shape[0]), int(output_shape[1])
    ix0 = max(0, int(np.floor(x0)))
    iy0 = max(0, int(np.floor(y0)))
    ix1 = min(width, int(np.ceil(x1)))
    iy1 = min(height, int(np.ceil(y1)))
    if ix1 - ix0 < 2 or iy1 - iy0 < 2:
        return None

    projected_area = max((x1 - x0) * (y1 - y0), 1e-6)
    kept_area = float((ix1 - ix0) * (iy1 - iy0))
    if kept_area / projected_area < 0.5:
        return None

    return [ix0, iy0, ix1 - ix0, iy1 - iy0]


# ---------------------------------------------------------------------------
# 3. Normalização de exposição + realce CLAHE
# ---------------------------------------------------------------------------
def project_roi_inverse(
    roi: Iterable[int],
    homography: np.ndarray,
    source_shape: tuple[int, int],
) -> list[int] | None:
    """Projeta uma ROI do espaço alinhado de T0 de volta para a foto de origem.

    Usado para confirmar que a ROI herdada de T0 ainda cai dentro da foto
    atual depois da homografia. A ROI canônica continua sendo a de T0; esta
    projeção é apenas uma validação de visibilidade/área válida.
    """
    matrix = np.asarray(homography, dtype=np.float64)
    if matrix.shape != (3, 3):
        raise ValueError("homografia deve ser uma matriz 3x3")
    try:
        inverse = np.linalg.inv(matrix)
    except np.linalg.LinAlgError as exc:
        raise ValueError("homografia singular: não é possível reprojetar a ROI") from exc
    return project_roi(roi, inverse, source_shape)


# ---------------------------------------------------------------------------
# 3b. Refinamento elástico local das referências de T0 (pós-homografia)
# ---------------------------------------------------------------------------
LOCAL_REFINE_DEFAULT_MARGIN_PX = 90
LOCAL_COARSE_TEMPLATE_MARGIN_PX = 45
LOCAL_COARSE_SEARCH_MARGIN_PX = 160
LOCAL_REFINE_MIN_SCORE = 0.35
LOCAL_REFINE_MIN_CC = 0.42
LOCAL_REFINE_MAX_SHIFT_MM = 20.0


def _robust_unit(values: np.ndarray, low: float = 1.0, high: float = 99.0) -> np.ndarray:
    """Normaliza para 0..1 por percentis, ignorando outliers de textura."""
    array = np.asarray(values, dtype=np.float32)
    finite = array[np.isfinite(array)]
    if finite.size == 0:
        return np.zeros_like(array, dtype=np.float32)
    lo = float(np.percentile(finite, low))
    hi = float(np.percentile(finite, high))
    if hi - lo < 1e-6:
        return np.zeros_like(array, dtype=np.float32)
    return np.clip((array - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)


def _local_structure_map(image_bgr: np.ndarray) -> np.ndarray:
    """Mapa estrutural invariante à iluminação para o alinhamento coarse.

    Combina três pistas estáveis entre sessões:

    * luminância normalizada por CLAHE (contraste global independente do flash);
    * crista local ``L* - GaussianBlur(L*)`` (relevo da estria e da pele);
    * queda de croma ``blur(C*) - C*`` (estrias costumam perder saturação).

    Uma suavização leve reduz rugas/microtexturas perpendiculares que não
    carregam posição útil, sem apagar o contorno alongado da lesão.
    """
    lab = bgr_to_cielab_float(image_bgr)
    lightness = lab[..., 0]
    chroma = np.hypot(lab[..., 1], lab[..., 2])

    lightness_u8 = np.clip(lightness * 2.55, 0.0, 255.0).astype(np.uint8)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    normalized_lightness = _robust_unit(clahe.apply(lightness_u8))

    ridge = lightness - cv2.GaussianBlur(lightness, (0, 0), 3.0)
    ridge_scale = float(np.percentile(np.abs(ridge[np.isfinite(ridge)]), 99) or 0.0)
    normalized_ridge = (
        np.clip(ridge / ridge_scale, -1.0, 1.0).astype(np.float32)
        if ridge_scale > 1e-6
        else np.zeros_like(ridge, dtype=np.float32)
    )

    chroma_blur = cv2.GaussianBlur(chroma, (0, 0), 4.0)
    chroma_drop = _robust_unit(np.maximum(0.0, chroma_blur - chroma))

    combined = (
        0.40 * normalized_lightness
        + 0.40 * normalized_ridge
        + 0.20 * chroma_drop
    )
    # Suavização curta: elimina micro-rugas de alta frequência e preserva a
    # posição do corpo alongado da estria.
    return cv2.GaussianBlur(combined, (0, 0), 1.2).astype(np.float32)


def _coarse_template_shift(
    reference_structure: np.ndarray,
    current_structure: np.ndarray,
    template_box: tuple[int, int, int, int],
    search_margin_px: int,
    image_width: int,
    image_height: int,
) -> dict[str, object] | None:
    """Busca grossa por template: alinha o recorte de T0 na janela expandida.

    ``cv2.matchTemplate`` com ``TM_CCOEFF_NORMED`` é robusto a ganho/offset de
    iluminação e não depende de uma inicialização próxima como o ECC. Retorna o
    deslocamento ``(dx, dy)``, o ``score`` e a própria matriz de resposta para
    conferir, depois do ECC, qual posicionamento explica melhor a textura.
    """
    tx0, ty0, tx1, ty1 = template_box
    if tx1 - tx0 < 12 or ty1 - ty0 < 12:
        return None
    template = reference_structure[ty0:ty1, tx0:tx1]
    if template.size == 0:
        return None

    sx0 = max(0, tx0 - int(search_margin_px))
    sy0 = max(0, ty0 - int(search_margin_px))
    sx1 = min(image_width, tx1 + int(search_margin_px))
    sy1 = min(image_height, ty1 + int(search_margin_px))
    search = current_structure[sy0:sy1, sx0:sx1]
    if search.shape[0] < template.shape[0] or search.shape[1] < template.shape[1]:
        return None

    try:
        response = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
    except cv2.error:
        return None
    if response.size == 0:
        return None
    _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(response)
    if not np.isfinite(max_val):
        return None
    best_x = sx0 + int(max_loc[0])
    best_y = sy0 + int(max_loc[1])
    return {
        "dx": float(best_x - tx0),
        "dy": float(best_y - ty0),
        "score": float(max_val),
        "response": response,
        "sx0": sx0,
        "sy0": sy0,
        "tx0": tx0,
        "ty0": ty0,
    }


def _template_response_at(
    coarse: dict[str, object],
    warp: np.ndarray,
    window_origin: tuple[int, int],
) -> float:
    """Score do mesmo template na posição prevista pelo warp do ECC.

    Permite comparar, em bases iguais, a colocação grosseira e a refinada. Se o
    ECC escorregar para uma micro-ruga vizinha, a resposta nessa posição cai e
    o resultado grosseiro (mais correlacionado) é preservado.
    """
    response = coarse.get("response")
    if not isinstance(response, np.ndarray) or response.size == 0:
        return 0.0
    x0, y0 = window_origin
    tx0 = float(coarse["tx0"])
    ty0 = float(coarse["ty0"])
    sx0 = int(coarse["sx0"])
    sy0 = int(coarse["sy0"])
    point = np.asarray(
        [[[tx0 - x0, ty0 - y0]]], dtype=np.float64
    ).reshape(-1, 1, 2)
    warped = cv2.transform(point, warp).reshape(2) + np.array([x0, y0])
    rx = int(round(float(warped[0]) - sx0))
    ry = int(round(float(warped[1]) - sy0))
    if not (0 <= ry < response.shape[0] and 0 <= rx < response.shape[1]):
        return 0.0
    patch = response[max(0, ry - 1): ry + 2, max(0, rx - 1): rx + 2]
    if patch.size == 0:
        return 0.0
    return float(np.nanmax(patch))


def _ecc_warp_local(
    reference_texture: np.ndarray,
    current_texture: np.ndarray,
    init_translation: tuple[float, float] = (0.0, 0.0),
) -> tuple[float, np.ndarray | None, str | None]:
    """Refina o warp local por ECC partindo do deslocamento grosseiro.

    ``cv2.findTransformECC(template, input, warp)`` devolve um warp que mapeia
    ``input`` no espaço de ``template``. Como queremos mover o polígono de T0
    (input) para a sessão atual (template), passamos a imagem atual como
    template. A matriz é inicializada com ``(dx0, dy0)`` do ``matchTemplate``;
    sem essa inicialização o ECC fica preso em micro-rugas próximas ao ponto
    inicial. Há fallback para translação pura.
    """
    criteria = (
        cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
        50,
        1e-4,
    )
    dx0, dy0 = float(init_translation[0]), float(init_translation[1])
    models = (
        (cv2.MOTION_EUCLIDEAN, "euclidean"),
        (cv2.MOTION_TRANSLATION, "translation"),
    )
    for motion, label in models:
        warp = np.array([[1.0, 0.0, dx0], [0.0, 1.0, dy0]], dtype=np.float32)
        try:
            cc, warp = cv2.findTransformECC(
                current_texture,
                reference_texture,
                warp,
                motion,
                criteria,
            )
        except cv2.error:
            continue
        if not np.isfinite(cc) or not np.isfinite(warp).all():
            continue
        return float(cc), np.asarray(warp, dtype=np.float64), label
    return float("nan"), None, None


def refine_t0_polygons_local(
    reference_image_path: str | Path,
    current_image_path: str | Path,
    polygons: dict[str, Iterable[Iterable[float]]],
    px_per_mm: float,
    *,
    margin_px: int = LOCAL_REFINE_DEFAULT_MARGIN_PX,
    template_margin_px: int = LOCAL_COARSE_TEMPLATE_MARGIN_PX,
    min_score: float = LOCAL_REFINE_MIN_SCORE,
    min_cc: float = LOCAL_REFINE_MIN_CC,
    max_shift_mm: float = LOCAL_REFINE_MAX_SHIFT_MM,
) -> dict[str, dict[str, object]]:
    """Refina os polígonos de T0 para deslocamentos elásticos locais.

    A homografia global corrige rotação/escala/perspectiva, mas a pele ainda
    pode deslizar lateralmente entre T0 e T1–Tn. O ECC puro, inicializado em
    ``(0, 0)``, fica preso em micro-rugas ou aborta sem mover o polígono. Por
    isso o alinhamento é **coarse-to-fine**:

    1. mapa estrutural (L*+CLAHE, crista local e queda de croma);
    2. busca grossa por ``matchTemplate`` (``TM_CCOEFF_NORMED``) numa janela
       expandida;
    3. refino ECC euclidiano inicializado com o deslocamento grosseiro, com
       fallback para translação pura.

    Travas de segurança: aplica apenas quando a busca grossa ou o ECC têm
    evidência suficiente — ``(score >= min_score or ecc_cc >= min_cc)`` — e o
    deslocamento máximo dos vértices não excede ``max_shift_mm``. Caso
    contrário, o polígono de T0 permanece na posição global.

    Returns:
        Mapeamento ``código -> {polygon, reference_polygon, applied, shift_mm,
        score, cc, coarse_score, max_shift_px, motion, reason, window}``.
    """
    reference_polygons = {
        str(code): [[float(x), float(y)] for x, y in points]
        for code, points in (polygons or {}).items()
        if points is not None
    }
    results: dict[str, dict[str, object]] = {}
    if not reference_polygons:
        return results

    def _untouched(points: list[list[float]], reason: str, **extra: object) -> dict[str, object]:
        result: dict[str, object] = {
            "polygon": [list(point) for point in points],
            "reference_polygon": [list(point) for point in points],
            "applied": False,
            "score": 0.0,
            "cc": None,
            "coarse_score": None,
            "shift_mm": 0.0,
            "max_shift_px": 0.0,
            "motion": None,
            "reason": reason,
            "window": None,
        }
        result.update(extra)
        return result

    scale = float(px_per_mm or 0.0)
    if scale <= 0:
        return {
            code: _untouched(points, "missing_scale")
            for code, points in reference_polygons.items()
            if len(points) >= 3
        }

    reference_image = read_image_bgr(reference_image_path)
    current_image = read_image_bgr(current_image_path)
    # As duas imagens estão no mesmo espaço alinhado; usa a interseção dos
    # limites por segurança caso uma delas tenha sido recortada.
    height = min(reference_image.shape[0], current_image.shape[0])
    width = min(reference_image.shape[1], current_image.shape[1])
    reference_structure = _local_structure_map(reference_image[:height, :width])
    current_structure = _local_structure_map(current_image[:height, :width])
    max_shift_px_limit = float(max_shift_mm) * scale
    search_margin_px = int(
        max(32, min(LOCAL_COARSE_SEARCH_MARGIN_PX, round(float(max_shift_mm) * scale)))
    )

    for code, points in reference_polygons.items():
        if len(points) < 3:
            results[code] = _untouched(points, "no_reference_polygon")
            continue

        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        x0 = max(0, int(np.floor(min(xs))) - int(margin_px))
        y0 = max(0, int(np.floor(min(ys))) - int(margin_px))
        x1 = min(width, int(np.ceil(max(xs))) + int(margin_px))
        y1 = min(height, int(np.ceil(max(ys))) + int(margin_px))
        if x1 - x0 < 16 or y1 - y0 < 16:
            results[code] = _untouched(points, "window_too_small")
            continue

        reference_crop = reference_structure[y0:y1, x0:x1]
        current_crop = current_structure[y0:y1, x0:x1]
        if reference_crop.size == 0 or current_crop.shape != reference_crop.shape:
            results[code] = _untouched(points, "window_too_small")
            continue

        # Template mais justo que a janela do ECC: inclui a lesão e uma margem
        # curta, para que a busca grossa não seja diluída pela pele ao redor.
        tx0 = max(0, int(np.floor(min(xs))) - int(template_margin_px))
        ty0 = max(0, int(np.floor(min(ys))) - int(template_margin_px))
        tx1 = min(width, int(np.ceil(max(xs))) + int(template_margin_px))
        ty1 = min(height, int(np.ceil(max(ys))) + int(template_margin_px))
        coarse = _coarse_template_shift(
            reference_structure,
            current_structure,
            (tx0, ty0, tx1, ty1),
            search_margin_px,
            width,
            height,
        )
        if coarse is None:
            dx0, dy0, coarse_score = 0.0, 0.0, 0.0
            coarse_reason = "coarse_unavailable"
        else:
            dx0 = float(coarse["dx"])
            dy0 = float(coarse["dy"])
            coarse_score = float(coarse["score"])
            coarse_reason = None

        cc, warp, motion = _ecc_warp_local(
            reference_crop, current_crop, init_translation=(dx0, dy0)
        )
        window = [x0, y0, x1 - x0, y1 - y0]
        ecc_ok = (
            warp is not None
            and np.isfinite(cc)
            and np.isfinite(warp).all()
        )
        ecc_cc = float(cc) if ecc_ok else float("nan")

        # A busca grossa ancora o ECC, mas se o ECC escorregar para uma
        # micro-ruga vizinha a resposta do template cai. Nesse caso preserva-se
        # a translação grosseira, que explica melhor a textura observada.
        coarse_warp = np.array(
            [[1.0, 0.0, dx0], [0.0, 1.0, dy0]], dtype=np.float64
        )
        chosen_warp = coarse_warp
        chosen_motion = "coarse_translation"
        chosen_score = coarse_score

        if coarse is not None:
            if ecc_ok:
                ecc_match = _template_response_at(coarse, warp, (x0, y0))
                if (
                    ecc_cc >= float(min_cc)
                    and ecc_match >= coarse_score - 0.02
                ):
                    chosen_warp = warp
                    chosen_motion = motion
                    chosen_score = max(ecc_match, ecc_cc)
        elif ecc_ok:
            # Sem template disponível: ECC puro (inicializado em zero).
            chosen_warp = warp
            chosen_motion = motion
            chosen_score = ecc_cc

        local_points = np.asarray(
            [[point[0] - x0, point[1] - y0] for point in points],
            dtype=np.float64,
        ).reshape(-1, 1, 2)
        warped = cv2.transform(local_points, chosen_warp).reshape(-1, 2)
        moved = warped + np.array([x0, y0], dtype=np.float64)
        original = np.asarray(points, dtype=np.float64)
        if not np.isfinite(moved).all():
            results[code] = _untouched(
                points,
                "ecc_failed",
                cc=None if np.isnan(ecc_cc) else ecc_cc,
                coarse_score=coarse_score,
                motion=chosen_motion,
                window=window,
            )
            continue
        max_shift_px = float(np.max(np.linalg.norm(moved - original, axis=1)))
        shift_mm = max_shift_px / scale
        metadata = {
            "cc": None if np.isnan(ecc_cc) else ecc_cc,
            "coarse_score": coarse_score,
            "score": chosen_score,
            "motion": chosen_motion,
            "window": window,
            "max_shift_px": max_shift_px,
            "shift_mm": shift_mm,
        }

        has_evidence = coarse_score >= float(min_score) or (
            np.isfinite(ecc_cc) and ecc_cc >= float(min_cc)
        )
        if not has_evidence:
            results[code] = _untouched(
                points,
                coarse_reason or "low_confidence",
                **metadata,
            )
            continue
        if max_shift_px > max_shift_px_limit:
            results[code] = _untouched(
                points, "shift_exceeds_limit", **metadata
            )
            continue

        results[code] = {
            "polygon": [[float(x), float(y)] for x, y in moved],
            "reference_polygon": [list(point) for point in points],
            "applied": True,
            "score": chosen_score,
            "reason": "ok",
            **metadata,
        }
    return results



def _validate_roi(roi: Iterable[int], width: int, height: int) -> tuple[int, int, int, int]:
    x, y, w, h = (int(v) for v in roi)
    if w <= 0 or h <= 0:
        raise ValueError("healthy_skin_roi deve ter largura/altura positivas")
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + w), min(height, y + h)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("healthy_skin_roi fora dos limites da imagem")
    return x0, y0, x1 - x0, y1 - y0


# Limiares de qualidade da ROI de pele sadia. Valores empíricos: abaixo de
# 80% de pixels válidos a caixa invade a borda preta da imagem alinhada; desvios
# acima de ~16 (L*) ou ~14 (croma combinado) indicam que a caixa caiu sobre
# lesão, pelo, sombra ou outra região não homogênea.
ROI_INVALID_LUMA = 10.0
ROI_MIN_VALID_FRACTION = 0.80
ROI_MAX_LIGHTNESS_STD = 16.0
ROI_MAX_CHROMA_STD = 14.0


def _roi_warnings(
    valid_fraction: float,
    lightness_std: float,
    chroma_std: float,
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    if valid_fraction < ROI_MIN_VALID_FRACTION:
        warnings.append(
            {
                "code": "roi_outside_valid_area",
                "message": (
                    "ROI de pele sadia invade a área inválida da imagem "
                    f"({valid_fraction * 100:.0f}% de pixels válidos; mínimo "
                    f"{ROI_MIN_VALID_FRACTION * 100:.0f}%)."
                ),
            }
        )
    if (
        lightness_std > ROI_MAX_LIGHTNESS_STD
        or chroma_std > ROI_MAX_CHROMA_STD
    ):
        warnings.append(
            {
                "code": "roi_high_variance",
                "message": (
                    "ROI de pele sadia com alta variância interna "
                    f"(σL*={lightness_std:.1f}; σcroma={chroma_std:.1f}). "
                    "Revise se a caixa cobre apenas pele íntegra."
                ),
            }
        )
    return warnings


def analyze_region_lab(img_path: str, roi: list[int]) -> dict[str, object]:
    """Estatística robusta da ROI ``[x, y, w, h]`` em CIELAB.

    A mediana é usada como descritor central para não deixar que poucos
    pixels de lesão/pelo/sombra desloquem a referência de cor. Também
    devolve dispersão (desvio-padrão), fração de pixels válidos (fora da
    borda preta da imagem alinhada) e avisos técnicos prontos para a UI e
    para o laudo.
    """
    image = read_image_bgr(img_path)
    height, width = image.shape[:2]
    x, y, w, h = _validate_roi(roi, width, height)
    region = image[y : y + h, x : x + w]
    if region.size == 0:
        raise ValueError("healthy_skin_roi fora dos limites da imagem")

    # A área inválida típica é a borda preta criada pela homografia. Pixels
    # escuros também aparecem em sombras profundas, por isso o limiar é baixo.
    valid = region.max(axis=2) > ROI_INVALID_LUMA
    valid_fraction = float(valid.mean()) if valid.size else 0.0
    if not valid.any():
        valid = np.ones(region.shape[:2], dtype=bool)

    lab_region = bgr_to_cielab_float(region)
    pixels = lab_region[valid]
    if pixels.size == 0:
        raise ValueError("ROI de pele sadia sem pixels válidos")

    median = np.median(pixels, axis=0)
    mean = pixels.mean(axis=0)
    std = pixels.std(axis=0)
    lightness_std = float(std[0])
    chroma_std = float(np.hypot(std[1], std[2]))
    warnings = _roi_warnings(valid_fraction, lightness_std, chroma_std)

    return {
        "median_lab": [float(value) for value in median],
        "mean_lab": [float(value) for value in mean],
        "std_lab": [float(value) for value in std],
        "lightness_std": lightness_std,
        "chroma_std": chroma_std,
        "valid_fraction": valid_fraction,
        "warnings": warnings,
    }


def compute_region_lab_means(
    img_path: str, roi: list[int]
) -> tuple[float, float, float]:
    """Mediana CIELAB ``(L*, a*, b*)`` de uma ROI ``[x, y, w, h]``.

    A mediana é deliberadamente usada no lugar da média: é o estimador
    robusto que permanece estável quando a ROI contém alguns pixels de
    lesão, pelo ou reflexo especular.
    """
    stats = analyze_region_lab(img_path, roi)
    median = stats["median_lab"]
    assert isinstance(median, list)
    return float(median[0]), float(median[1]), float(median[2])


# Limites de correção fotométrica entre sessões. Evitam que uma diferença
# grande de iluminação "lave" a imagem com um deslocamento arbitrário.
MAX_L_SHIFT = 22.0
MAX_CHROMA_SHIFT = 8.0
_L_TOP_KNEE = 92.0
_L_BOTTOM_KNEE = 8.0
_L_SHADOW_GUARD = (10.0, 30.0)
_L_HIGHLIGHT_GUARD = (85.0, 95.0)


def _smoothstep(value: np.ndarray) -> np.ndarray:
    return value * value * (3.0 - 2.0 * value)


def _soft_limit(value: float | np.ndarray, limit: float | np.ndarray) -> np.ndarray:
    """Limita deslocamentos de forma suave (linear perto de zero)."""
    values = np.asarray(value, dtype=np.float64)
    limits = np.asarray(limit, dtype=np.float64)
    knee = 0.8 * limits
    extra = limits - knee
    excess = np.abs(values) - knee
    compressed = np.where(
        excess <= 0.0,
        np.abs(values),
        knee + extra * np.tanh(np.maximum(excess, 0.0) / np.maximum(extra, 1e-9)),
    )
    return np.sign(values) * compressed


def _soft_compress_lightness(lightness: np.ndarray) -> np.ndarray:
    """Comprime apenas os extremos de L* preservando o contraste dos médios."""
    values = np.asarray(lightness, dtype=np.float64)
    top = _L_TOP_KNEE
    bottom = _L_BOTTOM_KNEE

    high = values > top
    values = np.where(
        high,
        top + (100.0 - top) * np.tanh((values - top) / (100.0 - top)),
        values,
    )
    low = values < bottom
    values = np.where(
        low,
        bottom - bottom * np.tanh((bottom - values) / bottom),
        values,
    )
    return values


def normalize_and_enhance_cielab(
    aligned_img_path: str,
    healthy_roi: list[int],
    ref_lab_means: tuple[float, float, float] | None,
    output_path: str,
    *,
    normalized_output_path: str | None = None,
) -> tuple[str, tuple[float, float, float]]:
    """Normaliza exposição em CIELAB e realça o relevo das lesões com CLAHE.

    A média de ``(L*, a*, b*)`` da pele saudável de referência é medida e,
    quando ``ref_lab_means`` (de T0) é fornecido, a imagem é deslocada para
    igualar luminosidade e temperatura de cor entre sessões. Os
    deslocamentos são limitados de forma suave (``MAX_L_SHIFT`` e
    ``MAX_CHROMA_SHIFT``) e os extremos de luminosidade são comprimidos
    suavemente, mantendo o aspecto fotográfico natural sem estourar altas
    luzes ou sombras.

    Args:
        aligned_img_path: imagem alinhada de entrada (BGR).
        healthy_roi: ``[x, y, w, h]`` da pele íntegra no espaço alinhado.
        ref_lab_means: médias CIELAB da referência T0 (ou ``None``).
        output_path: caminho da imagem realçada (CLAHE).
        normalized_output_path: se informado, grava também a imagem
            normalizada sem CLAHE (usada na medição colorimétrica).

    Returns:
        ``(output_path, medias_lab_da_entrada)`` onde as médias são as
        medidas na imagem original, em unidades CIELAB reais.
    """
    image = read_image_bgr(aligned_img_path)
    height, width = image.shape[:2]
    x, y, w, h = _validate_roi(healthy_roi, width, height)

    lab = bgr_to_cielab_float(image)
    roi_lab = lab[y : y + h, x : x + w].reshape(-1, 3)
    # Mediana robusta: pixels isolados de lesão/pelo/sombra não deslocam a
    # referência fotométrica usada no alinhamento entre sessões.
    current_means = tuple(float(v) for v in np.median(roi_lab, axis=0))

    normalized_lab = lab
    if ref_lab_means is not None:
        reference = np.asarray(ref_lab_means, dtype=np.float64)
        shift = reference - np.asarray(current_means, dtype=np.float64)
        limited = _soft_limit(
            shift, np.array([MAX_L_SHIFT, MAX_CHROMA_SHIFT, MAX_CHROMA_SHIFT])
        )
        # A correção atua plenamente nos tons médios (pele) e se anula
        # suavemente em sombras profundas e altas luzes, preservando o
        # aspecto fotográfico e evitando estouros fora da ROI.
        luminance = lab[..., 0]
        low = np.clip(
            (luminance - _L_SHADOW_GUARD[0])
            / (_L_SHADOW_GUARD[1] - _L_SHADOW_GUARD[0]),
            0.0,
            1.0,
        )
        high = np.clip(
            (_L_HIGHLIGHT_GUARD[1] - luminance)
            / (_L_HIGHLIGHT_GUARD[1] - _L_HIGHLIGHT_GUARD[0]),
            0.0,
            1.0,
        )
        weight = (_smoothstep(low) * _smoothstep(high))[..., None]
        normalized_lab = lab + (limited.reshape(1, 1, 3) * weight)
        normalized_lab[..., 0] = _soft_compress_lightness(normalized_lab[..., 0])
        normalized_lab[..., 1] = np.clip(normalized_lab[..., 1], -127.0, 127.0)
        normalized_lab[..., 2] = np.clip(normalized_lab[..., 2], -127.0, 127.0)

    normalized_bgr = cielab_to_bgr_u8(normalized_lab)
    write_image_bgr(output_path, normalized_bgr)

    # CLAHE apenas no canal L* (preserva a*, b* e, portanto, a cor).
    l_channel = np.clip(np.rint(normalized_lab[..., 0] * 2.55), 0, 255).astype(
        np.uint8
    )
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    l_equalized = clahe.apply(l_channel)

    enhanced_lab = normalized_lab.copy()
    enhanced_lab[..., 0] = l_equalized.astype(np.float64) / 2.55
    enhanced_bgr = cielab_to_bgr_u8(enhanced_lab)

    # O caminho "realçado" é o mesmo caminho pedido para a saída; quando
    # um caminho normalizado separado é informado, ele guarda a base sem CLAHE.
    if normalized_output_path:
        write_image_bgr(normalized_output_path, normalized_bgr)

    return write_image_bgr(output_path, enhanced_bgr), current_means


# ---------------------------------------------------------------------------
# 4. Morfometria + contraste CIEDE2000
# ---------------------------------------------------------------------------
POLYGON_DUPLICATE_EPS = 1e-6
# Tolerância sub-pixel: remove vértices realmente redundantes sem "podar"
# curvas suaves (elipses muito alongadas têm flecha local ~0.002 px).
POLYGON_COLLINEAR_EPS = 1e-3
POLYGON_MIN_AREA_PX = 1e-6
MAX_PLAUSIBLE_LENGTH_MM = 400.0
MIN_PLAUSIBLE_LENGTH_MM = 0.2


def _signed_polygon_area(points: np.ndarray) -> float:
    x = points[:, 0]
    y = points[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _segments_intersect(
    a: np.ndarray, b: np.ndarray, c: np.ndarray, d: np.ndarray
) -> bool:
    def orient(p: np.ndarray, q: np.ndarray, r: np.ndarray) -> float:
        return float((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]))

    def on_segment(p: np.ndarray, q: np.ndarray, r: np.ndarray) -> bool:
        return (
            min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9
            and min(p[1], r[1]) - 1e-9 <= q[1] <= max(p[1], r[1]) + 1e-9
        )

    o1 = orient(a, b, c)
    o2 = orient(a, b, d)
    o3 = orient(c, d, a)
    o4 = orient(c, d, b)
    if (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0):
        return True
    if abs(o1) <= 1e-9 and on_segment(a, c, b):
        return True
    if abs(o2) <= 1e-9 and on_segment(a, d, b):
        return True
    if abs(o3) <= 1e-9 and on_segment(c, a, d):
        return True
    if abs(o4) <= 1e-9 and on_segment(c, b, d):
        return True
    return False


def polygon_self_intersects(points: Iterable[Iterable[float]]) -> bool:
    """Verdadeiro quando duas arestas não adjacentes do polígono se cruzam."""
    array = np.asarray(points, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 2 or len(array) < 4:
        return False
    count = len(array)
    for i in range(count):
        a = array[i]
        b = array[(i + 1) % count]
        for j in range(i + 1, count):
            if j == i or j == (i + 1) % count or i == (j + 1) % count:
                continue
            if _segments_intersect(a, b, array[j], array[(j + 1) % count]):
                return True
    return False


def _drop_redundant_vertices(points: np.ndarray) -> tuple[np.ndarray, int, int]:
    """Remove duplicados consecutivos e vértices colineares/sobrepostos."""
    cleaned: list[np.ndarray] = []
    duplicates = 0
    for point in points:
        if cleaned and np.linalg.norm(point - cleaned[-1]) <= POLYGON_DUPLICATE_EPS:
            duplicates += 1
            continue
        cleaned.append(point)
    if len(cleaned) > 1 and np.linalg.norm(cleaned[0] - cleaned[-1]) <= POLYGON_DUPLICATE_EPS:
        cleaned.pop()
        duplicates += 1

    removed = 0
    changed = True
    while changed and len(cleaned) >= 3:
        changed = False
        kept: list[np.ndarray] = []
        count = len(cleaned)
        for index, current in enumerate(cleaned):
            previous = cleaned[index - 1]
            following = cleaned[(index + 1) % count]
            base = following - previous
            base_length = float(np.linalg.norm(base))
            if base_length <= POLYGON_DUPLICATE_EPS:
                removed += 1
                changed = True
                continue
            cross = abs(
                float((current[0] - previous[0]) * base[1]
                      - (current[1] - previous[1]) * base[0])
            )
            distance = cross / base_length
            # Só remove quando o vértice está entre os vizinhos; assim uma
            # ponta afiada (fora do segmento) é preservada.
            between = float(np.dot(current - previous, current - following)) <= 0.0
            if distance <= POLYGON_COLLINEAR_EPS and between:
                removed += 1
                changed = True
                continue
            kept.append(current)
        if kept and len(kept) != count:
            cleaned = kept
    return np.asarray(cleaned, dtype=np.float64), duplicates, removed


def _repair_self_intersections(points: np.ndarray) -> np.ndarray:
    """Substitui o polígono autointersectado pelo contorno externo preenchido."""
    x0, y0 = np.floor(points.min(axis=0)) - 2
    x1, y1 = np.ceil(points.max(axis=0)) + 2
    width = max(4, int(x1 - x0 + 1))
    height = max(4, int(y1 - y0 + 1))
    local = points - np.array([x0, y0])
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.fillPoly(mask, [np.rint(local).astype(np.int32)], 255)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ValueError("Polígono autointersectado não pôde ser corrigido")
    contour = max(contours, key=cv2.contourArea)
    if len(contour) < 3 or cv2.contourArea(contour) <= 0:
        raise ValueError("Polígono autointersectado não pôde ser corrigido")
    epsilon = max(0.5, 0.002 * cv2.arcLength(contour, True))
    simplified = cv2.approxPolyDP(contour, epsilon, True).reshape(-1, 2)
    if len(simplified) < 3:
        simplified = contour.reshape(-1, 2)
    repaired = simplified.astype(np.float64) + np.array([x0, y0])
    if polygon_self_intersects(repaired):
        # Contornos que apenas se tocam (ex.: "gravata-borboleta") ainda
        # repetem o ponto de cruzamento. O fecho convexo garante um polígono
        # simples; é uma correção conservadora e rastreável no laudo.
        hull = cv2.convexHull(points.astype(np.float32)).reshape(-1, 2)
        hull_epsilon = max(0.5, 0.002 * cv2.arcLength(hull.astype(np.float32), True))
        hull_simplified = cv2.approxPolyDP(
            hull.astype(np.float32), hull_epsilon, True
        ).reshape(-1, 2)
        if len(hull_simplified) >= 3:
            repaired = hull_simplified.astype(np.float64)
    return repaired


def sanitize_polygon(
    polygon_pts: Iterable[Iterable[float]],
    image_shape: tuple[int, int] | None = None,
) -> tuple[list[list[float]], dict[str, object]]:
    """Sanitiza o polígono antes da morfometria.

    Corrige, nesta ordem: coordenadas não finitas/fora da imagem, vértices
    duplicados, vértices redundantes (colineares), autointerseções e
    orientação. Devolve ``(pontos, info)`` onde ``info`` contém a lista de
    ``warnings`` técnicos para persistência/laudo.

    A função é idempotente para polígonos já válidos: as coordenadas
    originais são preservadas, evitando alterar medições aprovadas.
    """
    raw = list(polygon_pts)
    if len(raw) < 3:
        raise ValueError("Polígono inválido: mínimo de 3 vértices [x, y]")
    points: list[list[float]] = []
    for index, point in enumerate(raw):
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError(f"polygon[{index}] deve ter 2 coordenadas [x, y]")
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"polygon[{index}] contém coordenada inválida") from exc
        if not (np.isfinite(x) and np.isfinite(y)):
            raise ValueError(f"polygon[{index}] contém coordenada não finita")
        points.append([x, y])

    warnings: list[dict[str, str]] = []
    changes: list[str] = []
    array = np.asarray(points, dtype=np.float64)

    if image_shape is not None:
        height, width = int(image_shape[0]), int(image_shape[1])
        if height <= 0 or width <= 0:
            raise ValueError("image_shape inválido")
        clipped = np.column_stack(
            (
                np.clip(array[:, 0], 0.0, max(0.0, width - 1.0)),
                np.clip(array[:, 1], 0.0, max(0.0, height - 1.0)),
            )
        )
        if not np.allclose(clipped, array, atol=1e-9):
            array = clipped
            changes.append("clipped_to_image")
            warnings.append(
                {
                    "code": "polygon_clipped",
                    "message": "Vértices fora da imagem foram trazidos para a borda.",
                }
            )

    array, duplicates, removed = _drop_redundant_vertices(array)
    if duplicates:
        changes.append("duplicate_vertices_removed")
        warnings.append(
            {
                "code": "polygon_duplicate_vertices",
                "message": f"{duplicates} vértice(s) duplicado(s) removido(s).",
            }
        )
    if removed:
        changes.append("redundant_vertices_removed")
        warnings.append(
            {
                "code": "polygon_redundant_vertices",
                "message": f"{removed} vértice(s) redundante(s) removido(s).",
            }
        )
    if len(array) < 3:
        raise ValueError("Polígono degenerado: menos de 3 vértices após sanitização")

    if polygon_self_intersects(array):
        array = _repair_self_intersections(array)
        array, extra_duplicates, extra_removed = _drop_redundant_vertices(array)
        duplicates += extra_duplicates
        removed += extra_removed
        changes.append("self_intersections_repaired")
        warnings.append(
            {
                "code": "polygon_self_intersections",
                "message": "Autointerseções corrigidas pelo contorno externo da região.",
            }
        )

    area = _signed_polygon_area(array)
    if abs(area) <= POLYGON_MIN_AREA_PX:
        raise ValueError("Polígono degenerado: área nula após sanitização")
    if area < 0:
        array = array[::-1]
        changes.append("orientation_normalized")
        warnings.append(
            {
                "code": "polygon_orientation",
                "message": "Orientação do polígono normalizada (sentido anti-horário).",
            }
        )

    cleaned = [[float(x), float(y)] for x, y in array]
    return cleaned, {"changes": changes, "warnings": warnings}


def _polygon_mask(
    shape: tuple[int, int], polygon_pts: list[list[float]]
) -> np.ndarray:
    cleaned, _ = sanitize_polygon(polygon_pts, shape)
    points = np.asarray(cleaned, dtype=np.float64)
    mask = np.zeros(shape, dtype=np.uint8)
    cv2.fillPoly(mask, [np.rint(points).astype(np.int32)], 255)
    if cv2.countNonZero(mask) == 0:
        raise ValueError("Polígono degenerado: máscara vazia")
    return mask


def _zhang_suen_thinning(mask: np.ndarray, max_iter: int = 200) -> np.ndarray:
    """Esqueletização morfológica (Zhang-Suen) implementada com NumPy."""
    image = (mask > 0).astype(np.uint8)
    changed = True
    iteration = 0
    while changed and iteration < max_iter:
        changed = False
        for step in (0, 1):
            padded = np.pad(image, 1, mode="constant")
            p2 = padded[:-2, 1:-1]
            p3 = padded[:-2, 2:]
            p4 = padded[1:-1, 2:]
            p5 = padded[2:, 2:]
            p6 = padded[2:, 1:-1]
            p7 = padded[2:, :-2]
            p8 = padded[1:-1, :-2]
            p9 = padded[:-2, :-2]

            neighbors = p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9
            transitions = np.zeros_like(image, dtype=np.uint8)
            sequence = [p2, p3, p4, p5, p6, p7, p8, p9, p2]
            for first, second in zip(sequence[:-1], sequence[1:]):
                transitions += ((first == 0) & (second == 1)).astype(np.uint8)

            if step == 0:
                cond_a = (p2 * p4 * p6) == 0
                cond_b = (p4 * p6 * p8) == 0
            else:
                cond_a = (p2 * p4 * p8) == 0
                cond_b = (p2 * p6 * p8) == 0

            removable = (
                (image == 1)
                & (neighbors >= 2)
                & (neighbors <= 6)
                & (transitions == 1)
                & cond_a
                & cond_b
            )
            if removable.any():
                image[removable] = 0
                changed = True
        iteration += 1
    return (image * 255).astype(np.uint8)


_NEIGHBOR_OFFSETS = [
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
]


def _extend_endpoint_to_boundary_px(
    mask: np.ndarray,
    endpoint: tuple[int, int],
    neighbor: tuple[int, int],
) -> float:
    """Distância que falta do extremo do esqueleto até a borda da máscara.

    O afinamento morfológico termina antes das pontas afiladas de uma região;
    prolongar cada extremo na direção local até a borda recupera o
    comprimento real ponta a ponta.
    """
    position = np.asarray(endpoint, dtype=np.float64)
    direction = position - np.asarray(neighbor, dtype=np.float64)
    norm = float(np.linalg.norm(direction))
    if norm == 0.0:
        return 0.0
    direction /= norm

    height, width = mask.shape
    step = 0.5
    distance = 0.0
    max_steps = int(4.0 * (height + width) / step)
    for _ in range(max_steps):
        position = position + direction * step
        row = int(round(position[0]))
        col = int(round(position[1]))
        if row < 0 or row >= height or col < 0 or col >= width:
            break
        if mask[row, col] == 0:
            break
        distance += step
    return distance


def _skeleton_arc_length_px(
    skeleton: np.ndarray, mask: np.ndarray | None = None
) -> float:
    """Comprimento do esqueleto em pixels (soma euclidiana do caminho).

    Quando ``mask`` é fornecida, os extremos abertos do esqueleto são
    prolongados até a borda (compensando o afinamento nas pontas).
    """
    coords = np.argwhere(skeleton > 0)
    if len(coords) == 0:
        return 0.0
    coord_set = {(int(r), int(c)) for r, c in coords}

    def neighbors(pixel: tuple[int, int]) -> list[tuple[int, int]]:
        row, col = pixel
        return [
            (row + dr, col + dc)
            for dr, dc in _NEIGHBOR_OFFSETS
            if (row + dr, col + dc) in coord_set
        ]

    total = 0.0
    unvisited = set(coord_set)
    while unvisited:
        # Inicia preferencialmente por um extremo (grau 1).
        endpoints = [p for p in unvisited if len(neighbors(p)) == 1]
        start = min(endpoints) if endpoints else min(unvisited)
        current = start
        visited = {start}
        unvisited.discard(start)
        path = [start]
        while True:
            candidates = [n for n in neighbors(current) if n not in visited]
            if not candidates:
                break
            next_pixel = candidates[0]
            visited.add(next_pixel)
            unvisited.discard(next_pixel)
            path.append(next_pixel)
            current = next_pixel
        if len(path) > 1:
            points = np.asarray(path, dtype=np.float64)
            total += float(np.sum(np.hypot(*np.diff(points, axis=0).T)))
            if mask is not None:
                if len(neighbors(path[0])) == 1:
                    total += _extend_endpoint_to_boundary_px(
                        mask, path[0], path[1]
                    )
                if len(neighbors(path[-1])) == 1:
                    total += _extend_endpoint_to_boundary_px(
                        mask, path[-1], path[-2]
                    )
        # Pixels de ramificações não percorridos viram novas componentes.
    return total


def _ring_mask(
    stria_mask: np.ndarray, px_per_mm: float, ring_mm: float = 3.0
) -> np.ndarray:
    """Anel de pele adjacente de ~``ring_mm`` ao redor da região-alvo."""
    radius_px = max(1, int(round(ring_mm * px_per_mm)))
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * radius_px + 1, 2 * radius_px + 1)
    )
    dilated = cv2.dilate(stria_mask, kernel)
    ring = cv2.bitwise_and(dilated, cv2.bitwise_not(stria_mask))
    if cv2.countNonZero(ring) == 0:
        ring = cv2.bitwise_not(stria_mask)
    return ring


def _mask_mean_lab(lab: np.ndarray, mask: np.ndarray) -> np.ndarray:
    pixels = lab[mask > 0]
    if pixels.size == 0:
        raise ValueError("Região de amostragem vazia")
    return pixels.mean(axis=0)


def _mask_dimensions(mask: np.ndarray) -> dict[str, float]:
    """Comprimento (PCA), largura média e máxima de uma máscara binária.

    ``mean_width`` é definido como ``área / comprimento`` para garantir a
    identidade dimensional ``área = comprimento × largura média``. A largura
    máxima vem da transformada de distância (maior círculo inscrito).
    """
    pixel_count = int(cv2.countNonZero(mask))
    if pixel_count == 0:
        raise ValueError("Região vazia: não há pixels para medir")

    ys, xs = np.nonzero(mask)
    coords = np.column_stack((xs, ys)).astype(np.float64)
    centered = coords - coords.mean(axis=0)
    if pixel_count >= 2:
        covariance = np.cov(centered, rowvar=False)
        covariance = np.atleast_2d(covariance)
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        major_axis = eigenvectors[:, int(np.argmax(eigenvalues))]
        projection = centered @ major_axis
        length_px = float(projection.max() - projection.min())
    else:
        length_px = 0.0
    if length_px <= 0.0:
        raise ValueError("Não foi possível medir o comprimento da região-alvo")

    mean_width_px = float(pixel_count) / length_px
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    max_radius_px = float(distance.max())
    # ``distanceTransform`` mede de centro a centro de pixel: corrige a
    # quantização de meio pixel em cada lado (largura -= 1 px).
    max_width_px = max(mean_width_px, max(0.0, 2.0 * max_radius_px - 1.0))
    return {
        "pixel_count": float(pixel_count),
        "length_px": length_px,
        "mean_width_px": mean_width_px,
        "max_width_px": max_width_px,
    }


def measure_stria_polygon(
    aligned_img_path: str,
    polygon_pts: list[list[float]],
    px_per_mm: float,
) -> dict[str, object]:
    """Mede uma região-alvo delimitada por polígono na imagem alinhada.

    O polígono passa por ``sanitize_polygon`` antes de qualquer cálculo
    (duplicados, colineares, autointerseções e orientação). O comprimento é
    validado contra limites físicos plausíveis; calibrações absurdas são
    rejeitadas em vez de produzir valores em mm sem significado.

    Returns:
        Dicionário com ``length_mm``, ``mean_width_mm``, ``max_width_mm``,
        ``area_mm2`` e ``delta_e_00``. Os avisos de sanitização ficam com o
        chamador (``sanitize_polygon``) para não poluir o contrato numérico.
    """
    if px_per_mm <= 0:
        raise ValueError("px_per_mm deve ser maior que zero")

    image = read_image_bgr(aligned_img_path)
    height, width = image.shape[:2]
    cleaned, _geometry = sanitize_polygon(polygon_pts, (height, width))
    mask = _polygon_mask((height, width), cleaned)

    pixel_count = int(cv2.countNonZero(mask))
    area_mm2 = pixel_count / (px_per_mm**2)

    dimensions = _mask_dimensions(mask)
    length_px = dimensions["length_px"]
    mean_width_px = dimensions["mean_width_px"]
    max_width_px = dimensions["max_width_px"]

    length_mm = length_px / px_per_mm
    diagonal_mm = float(np.hypot(width, height)) / px_per_mm
    if length_mm < MIN_PLAUSIBLE_LENGTH_MM:
        raise ValueError(
            "Comprimento anômalo: "
            f"{length_mm:.3f} mm é menor que o mínimo plausível "
            f"({MIN_PLAUSIBLE_LENGTH_MM} mm). Verifique a escala px/mm."
        )
    if length_mm > MAX_PLAUSIBLE_LENGTH_MM or length_mm > diagonal_mm * 1.05:
        raise ValueError(
            "Comprimento anômalo: "
            f"{length_mm:.1f} mm excede o limite plausível "
            f"({min(MAX_PLAUSIBLE_LENGTH_MM, diagonal_mm):.1f} mm). "
            "Verifique a calibração da régua (px/mm) e o contorno."
        )

    lab = bgr_to_cielab_float(image)
    stria_lab = _mask_mean_lab(lab, mask)
    ring = _ring_mask(mask, px_per_mm)
    ring_lab = _mask_mean_lab(lab, ring)
    delta_e = float(delta_e_ciede2000(stria_lab, ring_lab))

    return {
        "length_mm": length_mm,
        "mean_width_mm": mean_width_px / px_per_mm,
        "max_width_mm": max_width_px / px_per_mm,
        "area_mm2": float(area_mm2),
        "delta_e_00": delta_e,
    }


# ---------------------------------------------------------------------------
# 4b. Mapeamento de campo (ROI) — detecção múltipla de estrias
# ---------------------------------------------------------------------------
FIELD_SCAN_MODES = ("auto", "rubra", "alba")


def _field_normalize(values: np.ndarray, floor: float = 0.0) -> np.ndarray:
    """Normaliza um mapa de contraste positivo com piso de ruído absoluto.

    Diferente de ``_auto_normalize``, não amplifica ruído numérico quando o
    mapa é praticamente constante (fundo de pele uniforme): valores abaixo de
    ``floor`` são zerados e mapas sem sinal relevante viram zero.
    """
    values = np.asarray(values, dtype=np.float32)
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.zeros_like(values, dtype=np.float32)
    high = float(np.percentile(finite, 99))
    if high <= max(floor * 2.0, 1e-3):
        return np.zeros_like(values, dtype=np.float32)
    cleaned = np.where(values >= floor, values, 0.0)
    return np.clip(cleaned / high, 0.0, 1.0).astype(np.float32)


def _field_ridge_response(
    channel: np.ndarray, sigmas: tuple[float, ...] = (1.5, 3.0, 5.0, 8.0)
) -> np.ndarray:
    """Realce multi-escala de cristas via autovalores da Hessiana.

    Estruturas lineares claras sobre fundo escuro têm o autovalor de maior
    magnitude negativo. O máximo entre escalas normalizadas gera um mapa
    ``0..1`` que privilegia faixas contínuas e atenua poros isolados.
    """
    values = channel.astype(np.float32)
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.zeros_like(values)
    low = float(np.percentile(finite, 2))
    high = float(np.percentile(finite, 98))
    span = max(high - low, 1e-6)
    normalized = np.clip((values - low) / span, 0.0, 1.0)
    best = np.zeros_like(normalized)
    for sigma in sigmas:
        blur = cv2.GaussianBlur(normalized, (0, 0), sigmaX=sigma, sigmaY=sigma)
        dxx = cv2.Sobel(blur, cv2.CV_32F, 2, 0, ksize=3) * (sigma * sigma)
        dyy = cv2.Sobel(blur, cv2.CV_32F, 0, 2, ksize=3) * (sigma * sigma)
        dxy = cv2.Sobel(blur, cv2.CV_32F, 1, 1, ksize=3) * (sigma * sigma)
        trace = dxx + dyy
        diff = dxx - dyy
        root = np.sqrt(np.maximum(diff * diff + 4.0 * dxy * dxy, 0.0))
        eigen_a = 0.5 * (trace - root)
        eigen_b = 0.5 * (trace + root)
        major = np.where(
            np.abs(eigen_a) >= np.abs(eigen_b), eigen_a, eigen_b
        )
        response = np.maximum(0.0, -major)
        best = np.maximum(best, response)
    return _field_normalize(best, floor=1e-3)


def _field_median_background(
    channel: np.ndarray, kernel_size: int
) -> np.ndarray:
    """Fundo de pele por mediana de grande escala, robusto à polaridade.

    A mediana rejeita a estria (que ocupa menos da metade da janela),
    independentemente de ela ser mais clara ou mais escura que a pele. Isso
    evita o halo que uma média gaussiana cria ao redor de faixas longas.
    """
    values = np.asarray(channel, dtype=np.float32)
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return values.copy()
    low = float(np.percentile(finite, 0.5))
    high = float(np.percentile(finite, 99.5))
    span = high - low
    if span <= 1e-6:
        return np.full_like(values, low)
    scaled = np.clip((values - low) / span * 255.0, 0.0, 255.0).astype(np.uint8)
    ksize = int(kernel_size)
    if ksize % 2 == 0:
        ksize += 1
    ksize = max(3, ksize)
    blurred = cv2.medianBlur(scaled, ksize)
    return blurred.astype(np.float32) / 255.0 * span + low


def _field_profile_maps(
    image: np.ndarray, lab: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Mapas de contraste local alba/rubra, sem suavização direcional.

    O fundo de pele é estimado por mediana de grande escala, que remove a
    própria estria da amostra. Assim o contraste é medido contra a pele ao
    redor — independentemente da orientação e da polaridade da faixa na ROI.
    """
    lightness = lab[..., 0].astype(np.float32)
    a_channel = lab[..., 1].astype(np.float32)
    b_channel = lab[..., 2].astype(np.float32)
    chroma = np.hypot(a_channel, b_channel)
    kernel_size = max(31, int(round(0.4 * float(min(lightness.shape)))))

    light_bg = _field_median_background(lightness, kernel_size)
    lightening = np.maximum(0.0, lightness - light_bg)
    chroma_bg = _field_median_background(chroma, kernel_size)
    desaturation = np.maximum(0.0, chroma_bg - chroma)
    alba = (
        0.65 * _field_normalize(lightening, floor=1.0)
        + 0.35 * _field_normalize(desaturation, floor=1.0)
    )

    a_bg = _field_median_background(a_channel, kernel_size)
    erythema = np.maximum(0.0, a_channel - a_bg)
    red_excess = image[..., 2].astype(np.float32) - image[..., 1].astype(np.float32)
    red_bg = _field_median_background(red_excess, kernel_size)
    local_red = np.maximum(0.0, red_excess - red_bg)
    rubra = (
        0.6 * _field_normalize(erythema, floor=1.0)
        + 0.4 * _field_normalize(local_red, floor=2.5)
    )
    return (
        np.clip(alba, 0.0, 1.0).astype(np.float32),
        np.clip(rubra, 0.0, 1.0).astype(np.float32),
    )


def _field_polygon_from_component(component: np.ndarray) -> list[list[float]]:
    """Contorno simplificado com 8 a 20 vértices para uma estria detectada."""
    contours, _ = cv2.findContours(
        component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return []
    contour = max(contours, key=cv2.contourArea)
    perimeter = cv2.arcLength(contour, True)
    if perimeter <= 0:
        return []
    best: np.ndarray | None = None
    best_distance = 10**9
    for step in range(1, 91):
        epsilon = perimeter * (0.0008 + step * 0.0011)
        approx = cv2.approxPolyDP(contour, epsilon, True)
        count = len(approx)
        if 8 <= count <= 20:
            best = approx
            break
        distance = min(abs(count - 8), abs(count - 20))
        if distance < best_distance:
            best_distance = distance
            best = approx
    if best is None or len(best) < 3:
        return []
    points = best.reshape(-1, 2).astype(np.float64)
    if len(points) < 8:
        points = _auto_resample_polygon(points, 8)
    elif len(points) > 20:
        points = _auto_resample_polygon(points, 20)
    return [[float(round(point[0], 2)), float(round(point[1], 2))] for point in points]


def scan_field_striae(
    aligned_img_path: str,
    roi_box: Iterable[float],
    px_per_mm: float,
    mode: str = "auto",
    sensitivity: float = 0.5,
    min_length_mm: float = 4.0,
    max_length_mm: float = 120.0,
    min_aspect_ratio: float = 2.5,
) -> dict[str, object]:
    """Detecta múltiplas estrias dentro de uma ROI ampla (mapeamento de campo).

    Fluxo: contraste local CIELAB (``L*``/croma para alba, ``a*`` e
    ``R - G`` para rubra, fusão em auto) + realce multi-escala de cristas
    (Hessiana), binarização modulada por ``sensitivity``, limpeza morfológica
    e componentes conexos individuais. Cada componente recebe métricas
    geométricas consistentes (``area_mm2 ≈ length_mm × mean_width_mm``) e o
    filtro global de comprimento/aspecto. Poros e ruídos circulares caem no
    filtro de aspecto.

    Returns:
        ``{"objects": [...], "summary": {...}}`` com polígonos no referencial
        alinhado da sessão.
    """
    if mode not in FIELD_SCAN_MODES:
        raise ValueError(f"Modo de mapeamento inválido: {mode}")
    sensitivity = float(sensitivity)
    if not 0.1 <= sensitivity <= 1.0:
        raise ValueError("sensitivity deve estar entre 0.1 e 1.0")
    min_length_mm = float(min_length_mm)
    max_length_mm = float(max_length_mm)
    min_aspect_ratio = float(min_aspect_ratio)
    if min_length_mm <= 0 or max_length_mm <= 0:
        raise ValueError("Comprimentos mínimo e máximo devem ser positivos")
    if min_length_mm >= max_length_mm:
        raise ValueError("min_length_mm deve ser menor que max_length_mm")
    if min_aspect_ratio <= 0:
        raise ValueError("min_aspect_ratio deve ser positivo")

    image = read_image_bgr(aligned_img_path)
    height, width = image.shape[:2]
    px_per_mm = float(px_per_mm)
    if px_per_mm <= 0:
        raise ValueError("px_per_mm deve ser maior que zero")
    x, y, w, h = _auto_clamp_box(roi_box, width, height)
    if w < 4 or h < 4:
        raise ValueError("ROI pequena demais para o mapeamento de campo")

    crop = image[y : y + h, x : x + w]
    lab = bgr_to_cielab_float(crop)

    alba_map, rubra_map = _field_profile_maps(crop, lab)
    if mode == "alba":
        profile = alba_map
        ridge = _field_ridge_response(lab[..., 0])
    elif mode == "rubra":
        profile = rubra_map
        red_excess = crop[..., 2].astype(np.float32) - crop[..., 1].astype(np.float32)
        kernel_size = max(31, int(round(0.4 * float(min(w, h)))))
        red_bg = _field_median_background(red_excess, kernel_size)
        local_red = np.maximum(0.0, red_excess - red_bg)
        ridge = _field_ridge_response(local_red)
    else:
        # Fusão ponderada evita que o halo de borda do perfil rubra infle a
        # largura de estrias claras (alba).
        profile = np.maximum(alba_map, 0.5 * rubra_map)
        ridge = _field_ridge_response(lab[..., 0])

    score = np.clip(0.62 * profile + 0.38 * ridge, 0.0, 1.0)
    threshold = 0.50 - 0.30 * sensitivity
    candidates = np.where(score >= threshold, 255, 0).astype(np.uint8)

    open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    candidates = cv2.morphologyEx(candidates, cv2.MORPH_OPEN, open_kernel)
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    candidates = cv2.morphologyEx(candidates, cv2.MORPH_CLOSE, close_kernel)

    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        candidates, connectivity=8
    )
    objects: list[dict[str, object]] = []
    min_area_px = 6
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] < min_area_px:
            continue
        component = np.where(labels == label, 255, 0).astype(np.uint8)
        try:
            dimensions = _mask_dimensions(component)
        except ValueError:
            continue
        length_mm = dimensions["length_px"] / px_per_mm
        mean_width_mm = dimensions["mean_width_px"] / px_per_mm
        max_width_mm = dimensions["max_width_px"] / px_per_mm
        area_mm2 = dimensions["pixel_count"] / (px_per_mm**2)
        if length_mm < min_length_mm or length_mm > max_length_mm:
            continue
        aspect = length_mm / max(mean_width_mm, 0.1)
        if aspect < min_aspect_ratio:
            continue
        polygon = _field_polygon_from_component(component)
        if len(polygon) < 3:
            continue
        stria_lab = _mask_mean_lab(lab, component)
        ring = _ring_mask(component, px_per_mm, ring_mm=3.0)
        ring_lab = _mask_mean_lab(lab, ring)
        delta_e = float(delta_e_ciede2000(stria_lab, ring_lab))
        objects.append(
            {
                "polygon": [[px + x, py + y] for px, py in polygon],
                "area_mm2": float(area_mm2),
                "length_mm": float(length_mm),
                "mean_width_mm": float(mean_width_mm),
                "max_width_mm": float(max_width_mm),
                "delta_e00": delta_e,
            }
        )

    objects.sort(key=lambda item: float(item["area_mm2"]), reverse=True)
    for index, item in enumerate(objects, start=1):
        item["id"] = index

    total_area = float(sum(float(item["area_mm2"]) for item in objects))
    roi_area_mm2 = float(w * h) / (px_per_mm**2)
    coverage = (total_area / roi_area_mm2 * 100.0) if roi_area_mm2 > 0 else 0.0
    return {
        "objects": objects,
        "summary": {
            "total_count": len(objects),
            "total_area_mm2": total_area,
            "roi_area_mm2": roi_area_mm2,
            "coverage_pct": coverage,
        },
    }


# ---------------------------------------------------------------------------
# 5. Segmentação assistida por CIELAB + GrabCut
# ---------------------------------------------------------------------------
AUTO_SEGMENT_MODES = ("auto", "rubra", "alba")


def _auto_odd(value: float) -> int:
    size = max(3, int(round(value)))
    return size if size % 2 == 1 else size + 1


def _auto_clamp_box(
    box: Iterable[float], width: int, height: int
) -> tuple[int, int, int, int]:
    values = [float(item) for item in box]
    if len(values) != 4:
        raise ValueError("box deve conter exatamente [x, y, w, h]")
    x, y, w, h = values
    if w <= 0 or h <= 0:
        raise ValueError("box deve ter largura e altura positivas")
    x0 = int(np.clip(round(x), 0, width - 1))
    y0 = int(np.clip(round(y), 0, height - 1))
    x1 = int(np.clip(round(x + w), x0 + 1, width))
    y1 = int(np.clip(round(y + h), y0 + 1, height))
    if x1 - x0 < 4 or y1 - y0 < 4:
        raise ValueError("box muito pequena para segmentar")
    return x0, y0, x1 - x0, y1 - y0


def _auto_skin_ring(
    shape: tuple[int, int], box: tuple[int, int, int, int]
) -> np.ndarray:
    """Anel de pele sadia ao redor da caixa, com moldura da imagem como reserva."""
    height, width = shape
    x, y, w, h = box
    box_mask = np.zeros((height, width), dtype=np.uint8)
    cv2.rectangle(box_mask, (x, y), (x + w - 1, y + h - 1), 255, -1)
    thickness = max(3, int(round(0.08 * max(w, h))))
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * thickness + 1, 2 * thickness + 1)
    )
    ring = cv2.bitwise_and(cv2.dilate(box_mask, kernel), cv2.bitwise_not(box_mask))
    if cv2.countNonZero(ring) < 256:
        border = max(2, int(round(0.03 * min(height, width))))
        ring = np.zeros((height, width), dtype=np.uint8)
        ring[:border, :] = 255
        ring[-border:, :] = 255
        ring[:, :border] = 255
        ring[:, -border:] = 255
        ring = cv2.bitwise_and(ring, cv2.bitwise_not(box_mask))
    if cv2.countNonZero(ring) == 0:
        ring = cv2.bitwise_not(box_mask)
    return ring


def _auto_background(channel: np.ndarray, kernel_size: int) -> np.ndarray:
    """Fundo local suavizado (Gaussiana) de um canal CIELAB/derivado."""
    return cv2.GaussianBlur(
        channel.astype(np.float32), (kernel_size, kernel_size), 0
    )


def _auto_normalize(values: np.ndarray, region: np.ndarray) -> np.ndarray:
    """Normaliza um mapa de contraste local para [0, 1] de forma robusta."""
    sample = values[region > 0]
    sample = sample[np.isfinite(sample)]
    if sample.size < 16:
        sample = values.reshape(-1)
        sample = sample[np.isfinite(sample)]
    if sample.size == 0:
        return np.zeros_like(values, dtype=np.float32)
    low = float(np.percentile(sample, 5))
    high = float(np.percentile(sample, 95))
    span = high - low
    if span <= 1e-6:
        scale = float(np.std(sample))
        if scale <= 1e-6:
            return np.zeros_like(values, dtype=np.float32)
        return np.clip((values - low) / (scale * 3.0), 0.0, 1.0).astype(np.float32)
    return np.clip((values - low) / span, 0.0, 1.0).astype(np.float32)


def _auto_texture_map(lightness: np.ndarray, kernel_size: int) -> np.ndarray:
    """Microrrelevo local: Top-Hat (cristas) + Black-Hat (sulcos) em L*."""
    lightness_u8 = np.clip(lightness * 2.55, 0, 255).astype(np.uint8)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)
    )
    top_hat = cv2.morphologyEx(lightness_u8, cv2.MORPH_TOPHAT, kernel)
    black_hat = cv2.morphologyEx(lightness_u8, cv2.MORPH_BLACKHAT, kernel)
    texture = top_hat.astype(np.float32) + black_hat.astype(np.float32)
    return cv2.GaussianBlur(texture, (5, 5), 0)


def _auto_axis_sigmas(w: int, h: int) -> tuple[float, float]:
    """Sigmas anisotrópicos ``(sigmaX, sigmaY)`` ao longo do eixo da caixa.

    A maior dimensão da caixa define o eixo da estria. A suavização é forte
    nesse eixo (preserva a continuidade da faixa) e fraca no eixo
    perpendicular (preserva a largura real da lesão).
    """
    long_axis = max(w, h)
    along = float(np.clip(0.025 * long_axis, 2.0, 9.0))
    across = float(np.clip(0.006 * long_axis, 0.7, 2.5))
    if w >= h:
        return along, across
    return across, along


def _auto_axis_smooth(channel: np.ndarray, w: int, h: int) -> np.ndarray:
    """Filtro direcional (ridge/band) que realça faixas contínuas.

    Poros e pelos são estruturas pontuais: ao suavizar ao longo do eixo maior
    da caixa, eles praticamente desaparecem, enquanto a energia de uma estria
    contínua se mantém.
    """
    sigma_x, sigma_y = _auto_axis_sigmas(w, h)
    return cv2.GaussianBlur(
        channel.astype(np.float32), (0, 0), sigmaX=sigma_x, sigmaY=sigma_y
    )


def _auto_centrality_profile(length: int, edge: float = 0.15) -> np.ndarray:
    """Perfil gaussiano perpendicular, zerado nos ``edge`` externos."""
    positions = (np.arange(length, dtype=np.float32) + 0.5) / float(length)
    profile = np.exp(-0.5 * ((positions - 0.5) / 0.20) ** 2)
    profile[(positions < edge) | (positions > 1.0 - edge)] = 0.0
    return profile.astype(np.float32)


def _auto_centrality_prior(
    height: int,
    width: int,
    x: int,
    y: int,
    w: int,
    h: int,
    edge: float = 0.15,
) -> np.ndarray:
    """Prior espacial: máximo no corredor central da caixa desenhada.

    A borda lateral da caixa é tratada como referência de pele sadia, então
    os ``edge`` mais externos de cada lado são suprimidos. O perfil é
    perpendicular ao eixo maior da caixa.
    """
    prior = np.zeros((height, width), dtype=np.float32)
    if w >= h:
        # Eixo maior horizontal: o perfil é perpendicular (ao longo de Y).
        prior[y : y + h, x : x + w] = _auto_centrality_profile(h, edge)[:, None]
    else:
        # Eixo maior vertical: o perfil é perpendicular (ao longo de X).
        prior[y : y + h, x : x + w] = _auto_centrality_profile(w, edge)[None, :]
    return prior


def _auto_morph_background(
    channel: np.ndarray, kernel_size: int, *, bright: bool
) -> np.ndarray:
    """Fundo de pele estimado por morfologia (remove a própria lesão).

    ``bright=True`` estima a pele mais escura ao redor de uma estrutura mais
    clara (alba em ``L*``, rubra em ``a*``), aplicando uma abertura que erode
    a lesão. ``bright=False`` estima a pele mais clara ao redor de uma
    estrutura mais escura (queda de croma, ``R - G``), aplicando um
    fechamento.
    """
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)
    )
    operation = cv2.MORPH_OPEN if bright else cv2.MORPH_CLOSE
    return cv2.morphologyEx(channel.astype(np.float32), operation, kernel)


def _auto_profile_maps(
    image: np.ndarray,
    lab: np.ndarray,
    region: np.ndarray,
    background_kernel: int,
    texture_kernel: int,
    structure_kernel: int,
    w: int,
    h: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Mapas normalizados de alba, rubra e ΔE00 local (contraste relativo).

    Os canais usados no contraste local passam antes por um filtro direcional
    ao longo do eixo maior da caixa: estrias/cicatrizes contínuas são
    preservadas e poros/pelos isolados são atenuados.
    """
    raw_lightness = lab[..., 0]
    lightness = _auto_axis_smooth(raw_lightness, w, h)
    a_channel = _auto_axis_smooth(lab[..., 1], w, h)
    b_channel = _auto_axis_smooth(lab[..., 2], w, h)
    chroma = np.hypot(a_channel, b_channel)

    light_bg = _auto_background(lightness, background_kernel)
    a_bg = _auto_background(a_channel, background_kernel)
    b_bg = _auto_background(b_channel, background_kernel)
    chroma_bg = _auto_background(chroma, background_kernel)

    # Fundo morfológico: como a estria pode ser mais larga que a Gaussiana,
    # remove a própria lesão da amostra e mede o contraste contra a pele ao
    # redor, não contra o miolo da lesão.
    light_skin = _auto_morph_background(lightness, structure_kernel, bright=True)
    a_skin = _auto_morph_background(a_channel, structure_kernel, bright=True)
    chroma_skin = _auto_morph_background(chroma, structure_kernel, bright=False)

    # Alba: clareamento local + perda de saturação + microrrelevo atrófico.
    lightening = np.maximum(0.0, lightness - np.minimum(light_bg, light_skin))
    desaturation = np.maximum(0.0, np.maximum(chroma_bg, chroma_skin) - chroma)
    texture = _auto_texture_map(raw_lightness, texture_kernel)
    alba = (
        0.50 * _auto_normalize(lightening, region)
        + 0.30 * _auto_normalize(desaturation, region)
        + 0.20 * _auto_normalize(texture, region)
    )

    # Rubra: excesso local de a* + dominância local de vermelho (R - G).
    red = image[..., 2].astype(np.float32)
    green = image[..., 1].astype(np.float32)
    red_excess = _auto_axis_smooth(red - green, w, h)
    red_bg = _auto_background(red_excess, background_kernel)
    red_skin = _auto_morph_background(red_excess, structure_kernel, bright=True)
    erythema = np.maximum(0.0, a_channel - np.minimum(a_bg, a_skin))
    local_red = np.maximum(0.0, red_excess - np.maximum(red_bg, red_skin))
    rubra = (
        0.6 * _auto_normalize(erythema, region)
        + 0.4 * _auto_normalize(local_red, region)
    )

    # ΔE00 local entre cada pixel e a pele imediatamente ao redor.
    background_lab = np.stack((light_bg, a_bg, b_bg), axis=-1)
    rx, ry, rw, rh = cv2.boundingRect(region)
    delta_crop = delta_e_ciede2000(
        lab[ry : ry + rh, rx : rx + rw].astype(np.float32),
        background_lab[ry : ry + rh, rx : rx + rw].astype(np.float32),
    )
    delta_e = np.zeros((image.shape[0], image.shape[1]), dtype=np.float32)
    delta_e[ry : ry + rh, rx : rx + rw] = np.asarray(delta_crop, dtype=np.float32)
    delta_e = _auto_normalize(delta_e, region)

    return (
        np.clip(alba, 0.0, 1.0).astype(np.float32),
        np.clip(rubra, 0.0, 1.0).astype(np.float32),
        np.clip(delta_e, 0.0, 1.0).astype(np.float32),
    )


def _auto_orientation_kernel(
    w: int, h: int, length: int, thickness: int
) -> np.ndarray:
    """Kernel elíptico alongado no eixo maior da caixa (une a estria)."""
    length = _auto_odd(max(3, length))
    thickness = _auto_odd(max(3, thickness))
    if w >= h:
        return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (length, thickness))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (thickness, length))


def _auto_largest_component(mask: np.ndarray) -> np.ndarray:
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return np.zeros_like(mask)
    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return np.where(labels == best, 255, 0).astype(np.uint8)


def _auto_keep_components(mask: np.ndarray, min_area: float) -> tuple[np.ndarray, int]:
    """Mantém componentes acima de ``min_area``; devolve a máscara e a contagem."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    kept = np.zeros_like(mask)
    kept_count = 0
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] >= min_area:
            kept[labels == label] = 255
            kept_count += 1
    return kept, kept_count


def _auto_resample_polygon(points: np.ndarray, target: int) -> np.ndarray:
    """Reamostra um polígono fechado com ``target`` vértices uniformes."""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 3 or target < 3:
        return pts
    closed = np.vstack([pts, pts[:1]])
    segments = np.linalg.norm(np.diff(closed, axis=0), axis=1)
    total = float(segments.sum())
    if total <= 1e-9:
        return pts
    cumulative = np.concatenate([[0.0], np.cumsum(segments)])
    sampled = []
    for distance in np.linspace(0.0, total, target, endpoint=False):
        index = int(np.searchsorted(cumulative, distance, side="right") - 1)
        index = min(max(index, 0), len(segments) - 1)
        ratio = (distance - cumulative[index]) / max(segments[index], 1e-9)
        sampled.append(closed[index] + ratio * (closed[index + 1] - closed[index]))
    return np.asarray(sampled, dtype=np.float64)


def _auto_polygon_from_mask(
    mask: np.ndarray, fallback: list[list[float]]
) -> tuple[list[list[float]], float]:
    """Aproxima o maior contorno com 10–16 vértices (fallback quando vazio)."""
    # Suavização geométrica: arredonda micro-reentrâncias e evita serrilhado.
    smooth = cv2.GaussianBlur(mask.astype(np.float32), (5, 5), 0)
    mask = np.where(smooth >= 127.0, 255, 0).astype(np.uint8)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return fallback, 0.0
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < 12.0:
        return fallback, 0.0

    perimeter = cv2.arcLength(contour, True)
    if perimeter <= 0:
        return fallback, 0.0

    best: np.ndarray | None = None
    best_distance = 10**9
    for step in range(1, 91):
        epsilon = perimeter * (0.0008 + step * 0.0011)
        approx = cv2.approxPolyDP(contour, epsilon, True)
        count = len(approx)
        if 10 <= count <= 16:
            best = approx
            break
        distance = min(abs(count - 10), abs(count - 16))
        if distance < best_distance:
            best_distance = distance
            best = approx

    if best is None or len(best) < 3:
        return fallback, 0.0
    if len(best) < 10 or len(best) > 16:
        best = _auto_resample_polygon(
            best.reshape(-1, 2), 10 if len(best) < 10 else 16
        ).reshape(-1, 1, 2)
    points = [
        [float(round(point[0][0], 2)), float(round(point[0][1], 2))]
        for point in best
    ]
    return points, float(
        abs(cv2.contourArea(np.asarray(best, dtype=np.float32)))
    )


def auto_segment_polygon(
    aligned_img_path: str,
    box: Iterable[float],
    seed_polygon: Iterable[Iterable[float]] | None = None,
    sensitivity: float = 0.5,
    mode: str = "auto",
) -> dict[str, object]:
    """Segmenta a região-alvo por contraste local CIELAB (anti-colapso).

    Opera sempre sobre a imagem natural colorida alinhada ao T0 (BGR
    original, sem equalizações destrutivas), recebida em
    ``aligned_img_path``.

    Em vez de um limiar global de ``L*``, cada perfil compara o pixel com a
    pele imediatamente ao redor (fundo gaussiano local), após uma suavização
    anisotrópica ao longo do eixo maior da caixa (ridge/band filter) que
    realça faixas contínuas e atenua poros/pelos isolados:

    * ``alba``: clareamento local + queda de croma + textura Top/Black-Hat;
    * ``rubra``: excesso local de ``a*`` + dominância local de ``R - G``;
    * ``auto``: fusão dos dois perfis + ΔE00 local.

    O mapa final é ponderado por um prior espacial gaussiano perpendicular
    ao eixo maior da caixa (corredor central), com os 15% laterais mais
    externos suprimidos (borda da caixa = pele sadia de referência).

    A máscara é aberta (remove poros), fechada com kernel alongado na
    orientação da caixa (conecta a estria), suavizada geometricamente e
    fragmentos pequenos são descartados. O GrabCut é usado de forma
    conservadora e nunca substitui um resultado local por um ponto isolado.

    Returns:
        ``{"polygon": [[x, y], ...], "area_px": float, "confidence": float}``.
    """
    if not 0.1 <= float(sensitivity) <= 1.0:
        raise ValueError("sensitivity deve estar entre 0.1 e 1.0")
    if mode not in AUTO_SEGMENT_MODES:
        raise ValueError(f"Modo de segmentação inválido: {mode}")

    image = read_image_bgr(aligned_img_path)
    height, width = image.shape[:2]
    x, y, w, h = _auto_clamp_box(box, width, height)
    lab = bgr_to_cielab_float(image)

    box_mask = np.zeros((height, width), dtype=np.uint8)
    cv2.rectangle(box_mask, (x, y), (x + w - 1, y + h - 1), 255, -1)

    background_kernel = 31 if max(w, h) < 420 else 41
    texture_kernel = _auto_odd(max(7, min(21, 0.06 * max(w, h))))
    structure_kernel = _auto_odd(max(15, min(151, 0.7 * min(w, h))))
    alba_map, rubra_map, delta_e_map = _auto_profile_maps(
        image,
        lab,
        box_mask,
        background_kernel,
        texture_kernel,
        structure_kernel,
        w,
        h,
    )

    if mode == "alba":
        score = alba_map
    elif mode == "rubra":
        score = rubra_map
    else:
        score = 0.45 * alba_map + 0.45 * rubra_map + 0.10 * delta_e_map

    # Prior de centralidade: a caixa deve envolver a lesão, então a
    # probabilidade máxima fica no corredor central e as bordas laterais
    # (pele sadia de referência) são suprimidas.
    prior = _auto_centrality_prior(height, width, x, y, w, h)
    prior_mask = np.where(prior > 0.0, 255, 0).astype(np.uint8)
    score = (score * prior).astype(np.float32)

    # Sensibilidade modula o limiar do mapa normalizado (0..1).
    threshold = 0.58 - 0.38 * float(sensitivity)
    candidates = np.where(score >= threshold, 255, 0).astype(np.uint8)
    candidates = cv2.bitwise_and(candidates, box_mask)

    corridor: np.ndarray | None = None
    seed_points: np.ndarray | None = None
    if seed_polygon is not None:
        seed_points = np.asarray(list(seed_polygon), dtype=np.float64)
        if seed_points.ndim != 2 or seed_points.shape[1] != 2 or len(seed_points) < 3:
            raise ValueError("seed_polygon inválido: mínimo de 3 vértices [x, y]")
        seed_mask = np.zeros((height, width), dtype=np.uint8)
        cv2.fillPoly(seed_mask, [np.rint(seed_points).astype(np.int32)], 255)
        span = max(
            float(seed_points[:, 0].max() - seed_points[:, 0].min()),
            float(seed_points[:, 1].max() - seed_points[:, 1].min()),
        )
        radius = max(3, int(round(0.12 * span)))
        corridor_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1)
        )
        corridor = cv2.dilate(seed_mask, corridor_kernel)
        candidates = cv2.bitwise_and(candidates, corridor)

    # Fallback limpo: contorno de T0 (se houver) ou a própria caixa.
    if seed_points is not None:
        fallback = [
            [float(round(point[0], 2)), float(round(point[1], 2))]
            for point in seed_points
        ]
    else:
        fallback = [
            [float(x), float(y)],
            [float(x + w), float(y)],
            [float(x + w), float(y + h)],
            [float(x), float(y + h)],
        ]

    # Abertura leve remove poros/pontos de brilho isolados.
    open_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (3, 3)
    )
    candidates = cv2.morphologyEx(candidates, cv2.MORPH_OPEN, open_kernel)

    # Fechamento alongado na orientação da caixa conecta a estria inteira.
    long_axis = max(w, h)
    close_kernel = _auto_orientation_kernel(
        w,
        h,
        length=int(round(0.22 * long_axis)),
        thickness=int(round(0.045 * long_axis)),
    )
    candidates = cv2.morphologyEx(candidates, cv2.MORPH_CLOSE, close_kernel)

    # Anti-colapso: ignora fragmentos menores que 2% da área da caixa.
    min_area = max(12.0, 0.02 * w * h)
    candidates, components = _auto_keep_components(candidates, min_area)
    if components == 0:
        return {
            "polygon": fallback,
            "area_px": 0.0,
            "confidence": 0.05,
        }

    # Vários fragmentos alinhados da mesma estria: envoltório morfológico.
    if components > 1:
        bridge_kernel = _auto_orientation_kernel(
            w,
            h,
            length=int(round(0.35 * long_axis)),
            thickness=int(round(0.08 * long_axis)),
        )
        bridged = cv2.morphologyEx(candidates, cv2.MORPH_CLOSE, bridge_kernel)
        bridged = _auto_largest_component(bridged)
        bridged, bridged_components = _auto_keep_components(bridged, min_area)
        if bridged_components > 0:
            candidates = bridged

    if corridor is not None:
        candidates = cv2.bitwise_and(candidates, corridor)

    # GrabCut conservador: só aceita refinamento que preserva massa suficiente.
    refined = candidates
    candidate_area = float(cv2.countNonZero(candidates))
    pad = max(8, int(round(0.2 * long_axis)))
    cx0, cy0 = max(0, x - pad), max(0, y - pad)
    cx1, cy1 = min(width, x + w + pad), min(height, y + h + pad)
    crop = image[cy0:cy1, cx0:cx1]
    local_box = box_mask[cy0:cy1, cx0:cx1]
    local_candidates = candidates[cy0:cy1, cx0:cx1]
    if mode != "alba" and candidate_area >= 0.003 * w * h:
        gc_mask = np.full(crop.shape[:2], cv2.GC_BGD, dtype=np.uint8)
        gc_mask[local_box > 0] = cv2.GC_PR_BGD
        gc_mask[local_candidates > 0] = cv2.GC_PR_FGD
        if cv2.countNonZero(gc_mask == cv2.GC_PR_FGD) >= 8:
            background = np.zeros((1, 65), dtype=np.float64)
            foreground = np.zeros((1, 65), dtype=np.float64)
            try:
                cv2.grabCut(
                    crop,
                    gc_mask,
                    None,
                    background,
                    foreground,
                    2,
                    cv2.GC_INIT_WITH_MASK,
                )
                grabcut = np.where(
                    (gc_mask == cv2.GC_FGD) | (gc_mask == cv2.GC_PR_FGD), 255, 0
                ).astype(np.uint8)
                grabcut = cv2.bitwise_and(grabcut, local_box)
                grabcut_mask = np.zeros((height, width), dtype=np.uint8)
                grabcut_mask[cy0:cy1, cx0:cx1] = grabcut
                grabcut_area = float(cv2.countNonZero(grabcut_mask))
                if 0.4 * candidate_area <= grabcut_area <= 2.5 * candidate_area:
                    refined = grabcut_mask
            except cv2.error:
                refined = candidates

    refined = cv2.bitwise_and(refined, box_mask)
    refined = cv2.bitwise_and(refined, prior_mask)
    if corridor is not None:
        refined = cv2.bitwise_and(refined, corridor)
    open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    refined = cv2.morphologyEx(refined, cv2.MORPH_CLOSE, open_kernel)
    refined = _auto_largest_component(refined)
    if cv2.countNonZero(refined) == 0:
        refined = candidates

    polygon, area_px = _auto_polygon_from_mask(refined, fallback)
    inside = score[refined > 0]
    mean_inside = float(inside.mean()) if inside.size else 0.0
    contrast = float(np.clip(mean_inside / max(threshold, 0.2), 0.0, 1.0))
    perimeter = cv2.arcLength(
        np.asarray(polygon, dtype=np.float32).reshape(-1, 1, 2), True
    )
    compactness = 0.0
    if perimeter > 0:
        compactness = float(min(1.0, 4.0 * np.pi * area_px / (perimeter**2)))
    confidence = float(
        np.clip(0.30 + 0.45 * contrast + 0.20 * compactness, 0.05, 0.98)
    )

    return {
        "polygon": polygon,
        "area_px": float(area_px),
        "confidence": confidence,
    }
