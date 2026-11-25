"""Image quality assessment and preprocessing for OCR.

Two pipelines are used, cheapest first:

* ``light``      - grayscale, upscale small text, deskew, CLAHE contrast. OCR engines
                   binarize internally, so a clean gray image usually works best.
* ``aggressive`` - additionally denoises, removes uneven illumination (shadows, phone
                   photos) and binarizes adaptively. Used only when ``light`` gives
                   low confidence, because it can hurt clean scans.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..models import ImageQuality

TARGET_DPI = 300
MIN_TEXT_SIDE_PX = 1600  # images smaller than this on their long side get upscaled
MAX_UPSCALE = 3.0
MAX_UPSCALED_PIXELS = 40_000_000


def to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_RGBA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)


def otsu_ink_mask(gray: np.ndarray) -> np.ndarray:
    """Boolean mask of ink pixels.

    Smoothing first matters: on a noisy page where ink is ~1% of pixels, plain Otsu
    splits the background peak in half and calls a third of the paper "ink"."""
    smooth = cv2.GaussianBlur(gray, (5, 5), 0)
    thr, _ = cv2.threshold(smooth, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    return smooth < thr


def estimate_skew(gray: np.ndarray) -> float:
    """Dominant text-line angle in degrees (positive = counter-clockwise)."""
    small = gray
    scale = 1.0
    if max(gray.shape) > 2000:
        scale = 2000 / max(gray.shape)
        small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    inv = otsu_ink_mask(small).astype(np.uint8) * 255
    # Smear characters horizontally into line blobs, then measure the blobs' angle.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, small.shape[1] // 60), 3))
    blobs = cv2.dilate(inv, kernel, iterations=1)
    contours, _ = cv2.findContours(blobs, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    angles, weights = [], []
    min_w = small.shape[1] * 0.1
    for c in contours:
        (_, _), (w, h), ang = cv2.minAreaRect(c)
        if w < h:  # make `ang` the angle of the long side
            w, h = h, w
            ang += 90
        if w < min_w or h == 0 or w / h < 4:
            continue
        # OpenCV 4.5+ and 5.x report different angle ranges; fold into (-45, 45].
        while ang > 45:
            ang -= 90
        while ang <= -45:
            ang += 90
        angles.append(ang)
        weights.append(w)
    if not angles:
        return 0.0
    order = np.argsort(angles)
    a = np.asarray(angles)[order]
    wts = np.cumsum(np.asarray(weights)[order])
    median = float(a[np.searchsorted(wts, wts[-1] / 2)])  # weighted median
    # minAreaRect angles are clockwise in image coordinates.
    return -median if abs(median) <= 15 else 0.0


def rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    h, w = gray.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(gray, m, (nw, nh), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def rotate_quadrant(gray: np.ndarray, degrees: int) -> np.ndarray:
    codes = {90: cv2.ROTATE_90_COUNTERCLOCKWISE, 180: cv2.ROTATE_180,
             270: cv2.ROTATE_90_CLOCKWISE}
    return cv2.rotate(gray, codes[degrees]) if degrees in codes else gray


# Thresholds calibrated on rendered invoices degraded with blur / noise / JPEG / low DPI
# (clean 300 DPI: sharpness ~0.7, Gaussian blur sigma 2: ~0.24, 72 DPI upscaled: ~0.3).
BLURRY_BELOW = 0.30
LOW_CONTRAST_BELOW = 80.0
# Background std-dev at native resolution: Gaussian noise sigma 15 -> ~9, sigma 30 -> ~18.
NOISY_ABOVE = 10.0


def estimate_noise(gray: np.ndarray) -> float:
    """Background grain measured at native resolution on a central crop
    (downscaling would average the noise away)."""
    h, w = gray.shape[:2]
    ch, cw = min(h, 2000), min(w, 2000)
    crop = gray[(h - ch) // 2:(h - ch) // 2 + ch, (w - cw) // 2:(w - cw) // 2 + cw]
    ink = otsu_ink_mask(crop).astype(np.uint8)
    far = ~cv2.dilate(ink, np.ones((15, 15), np.uint8)).astype(bool)
    return float(crop[far].std()) if far.sum() > 1000 else 0.0


def assess_quality(gray: np.ndarray, dpi: float | None) -> ImageQuality:
    """Quality metrics measured on the *text*, so mostly-white pages are not penalised.

    * contrast  - paper brightness minus ink brightness (Otsu classes), 0-255
    * sharpness - std-dev of the Laplacian around ink pixels, normalised by contrast
    * noise     - std-dev of background pixels away from any ink (speckle, grain)
    """
    h, w = gray.shape[:2]
    sample = gray
    if max(h, w) > 2500:  # metrics on a downscaled copy are just as informative
        f = 2500 / max(h, w)
        sample = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)

    ink = otsu_ink_mask(sample)
    ink_ratio = float(ink.mean())
    reasons: list[str] = []
    if ink_ratio < 0.0005 or ink_ratio > 0.6:
        contrast = sharpness = noise = 0.0
        skew = 0.0
    else:
        contrast = float(sample[~ink].mean() - sample[ink].mean())
        near_ink = cv2.dilate(ink.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
        lap = cv2.Laplacian(sample, cv2.CV_64F)
        sharpness = float(np.sqrt(lap[near_ink].var()) / max(contrast, 1.0))
        noise = estimate_noise(gray)
        skew = estimate_skew(gray)
        if sharpness < BLURRY_BELOW:
            reasons.append(f"blurry (sharpness {sharpness:.2f})")
        if contrast < LOW_CONTRAST_BELOW:
            reasons.append(f"low contrast ({contrast:.0f})")
        if noise > NOISY_ABOVE:
            reasons.append(f"noisy background ({noise:.0f})")
        if abs(skew) > 2:
            reasons.append(f"skewed {skew:.1f} deg")

    if dpi and dpi < 150:
        reasons.append(f"low resolution ({dpi:.0f} DPI)")
    elif not dpi and max(h, w) < 1000:
        reasons.append(f"small image ({w}x{h}px)")
    return ImageQuality(
        width=w, height=h, dpi=dpi, sharpness=round(sharpness, 3),
        contrast=round(contrast, 1), noise=round(noise, 1), ink_ratio=round(ink_ratio, 4),
        skew_angle=round(skew, 2), is_low_quality=bool(reasons), reasons=reasons,
    )


def upscale_factor(gray: np.ndarray, dpi: float | None) -> float:
    f = 1.0
    if dpi and dpi < TARGET_DPI * 0.8:
        f = min(MAX_UPSCALE, TARGET_DPI / dpi)
    elif not dpi and max(gray.shape) < MIN_TEXT_SIDE_PX:
        f = min(MAX_UPSCALE, MIN_TEXT_SIDE_PX / max(gray.shape))
    # Never let upscaling itself create a huge image.
    max_f = (MAX_UPSCALED_PIXELS / (gray.shape[0] * gray.shape[1])) ** 0.5
    return max(1.0, min(f, max_f))


def prepare_light(gray: np.ndarray, dpi: float | None, skew: float) -> np.ndarray:
    f = upscale_factor(gray, dpi)
    if f > 1.05:
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    if abs(skew) >= 0.3:
        gray = rotate(gray, -skew)  # undo the measured tilt
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def prepare_aggressive(gray: np.ndarray, dpi: float | None, skew: float) -> np.ndarray:
    f = upscale_factor(gray, dpi)
    if f > 1.05:
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    if abs(skew) >= 0.3:
        gray = rotate(gray, -skew)  # undo the measured tilt
    gray = cv2.fastNlMeansDenoising(gray, None, h=12, templateWindowSize=7, searchWindowSize=21)
    # Divide by a heavily blurred background estimate -> flattens shadows / gradients.
    k = max(31, (min(gray.shape) // 20) | 1)
    background = cv2.medianBlur(gray, k if k <= 255 else 255)
    flat = cv2.divide(gray, background, scale=255)
    block = max(15, (min(flat.shape) // 40) | 1)
    binary = cv2.adaptiveThreshold(flat, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, block, 15)
    # Close tiny gaps in broken characters (faxes, faint dot-matrix prints).
    return cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
