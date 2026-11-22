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
def _validate_roi(roi: Iterable[int], width: int, height: int) -> tuple[int, int, int, int]:
    x, y, w, h = (int(v) for v in roi)
    if w <= 0 or h <= 0:
        raise ValueError("healthy_skin_roi deve ter largura/altura positivas")
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + w), min(height, y + h)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("healthy_skin_roi fora dos limites da imagem")
    return x0, y0, x1 - x0, y1 - y0


def compute_region_lab_means(
    img_path: str, roi: list[int]
) -> tuple[float, float, float]:
    """Média CIELAB real ``(L*, a*, b*)`` de uma ROI ``[x, y, w, h]``."""
    image = read_image_bgr(img_path)
    height, width = image.shape[:2]
    x, y, w, h = _validate_roi(roi, width, height)
    lab = bgr_to_cielab_float(image)
    means = lab[y : y + h, x : x + w].reshape(-1, 3).mean(axis=0)
    return tuple(float(v) for v in means)  # type: ignore[return-value]


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
    current_means = tuple(float(v) for v in roi_lab.mean(axis=0))

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
def _polygon_mask(
    shape: tuple[int, int], polygon_pts: list[list[float]]
) -> np.ndarray:
    points = np.asarray(polygon_pts, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3:
        raise ValueError("Polígono inválido: mínimo de 3 vértices [x, y]")
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


def measure_stria_polygon(
    aligned_img_path: str,
    polygon_pts: list[list[float]],
    px_per_mm: float,
) -> dict[str, float]:
    """Mede uma região-alvo delimitada por polígono na imagem alinhada.

    Calcula área, largura máxima, largura média e comprimento a partir da
    máscara e do esqueleto (eixo medial), além do contraste perceptual
    CIEDE2000 entre o interior da região e o anel de pele saudável ao redor.

    Returns:
        Dicionário com ``length_mm``, ``mean_width_mm``, ``max_width_mm``,
        ``area_mm2`` e ``delta_e_00``.
    """
    if px_per_mm <= 0:
        raise ValueError("px_per_mm deve ser maior que zero")

    image = read_image_bgr(aligned_img_path)
    height, width = image.shape[:2]
    mask = _polygon_mask((height, width), polygon_pts)

    pixel_count = int(cv2.countNonZero(mask))
    area_mm2 = pixel_count / (px_per_mm**2)

    # Eixo medial e largura local via transformada de distância.
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    skeleton = _zhang_suen_thinning(mask)
    skeleton_coords = np.argwhere(skeleton > 0)
    if len(skeleton_coords) == 0:
        raise ValueError("Não foi possível extrair o esqueleto da região-alvo")

    radii = distance[skeleton_coords[:, 0], skeleton_coords[:, 1]]
    max_radius_px = float(radii.max())
    mean_radius_px = float(radii.mean())

    # ``distanceTransform`` mede de centro a centro de pixel: a borda física
    # do polígono está meio pixel além do último pixel interno. Corrigimos
    # essa quantização em cada lado (largura -= 1 px).
    width_quantization_px = 1.0
    max_width_px = max(0.0, 2.0 * max_radius_px - width_quantization_px)
    mean_width_px = max(0.0, 2.0 * mean_radius_px - width_quantization_px)

    arc_length_px = _skeleton_arc_length_px(skeleton, mask)

    lab = bgr_to_cielab_float(image)
    stria_lab = _mask_mean_lab(lab, mask)
    ring = _ring_mask(mask, px_per_mm)
    ring_lab = _mask_mean_lab(lab, ring)
    delta_e = float(delta_e_ciede2000(stria_lab, ring_lab))

    return {
        "length_mm": arc_length_px / px_per_mm,
        "mean_width_mm": mean_width_px / px_per_mm,
        "max_width_mm": max_width_px / px_per_mm,
        "area_mm2": float(area_mm2),
        "delta_e_00": delta_e,
    }
