"""Software image processing for /capture and /stream — pure functions, no I/O.

Added 2026-11-17 on feature/image-processing, when the client asked for the
Falcon's picture to look like a phone photo of the same tray. The camera's
own controls could not get there (measured on the Jetson the same day):
auto white balance leaves the fish orange under the room's warm light,
contrast 6/8/10 changed nothing useful, and any brightness above 0 burns the
steel's light reflections white while the fish stay dark. A phone closes that
gap in software, so this does too:

1. white_balance: grey-world on the tray. The steel tray is neutral grey and
   fills most of the cropped frame, so the colour cast is measured on the
   least colourful pixels (mostly steel) and divided out.
2. deglare: the ceiling lights mirror off the steel as bright vertical bands.
   A band is a smooth brightness field over the tray, so estimating it with a
   large blur and dividing it out removes it and leaves the texture (from the
   ZED branch, feature/fish-crop, where it was measured: the worst band went
   L 222 -> 156 and the clean tray came up 86 -> 109, with no halos around
   the fish). Pixels that are genuinely clipped stay clipped; that part
   needs a polarizer or diffused light.
3. enhance: CLAHE on lightness + an adaptive shadow lift that lands the
   frame's median on `target` (and does nothing to a frame already that
   bright) + a light unsharp mask. Also from feature/fish-crop, where the
   constants were tuned with the client over a week.

All of it is scale-independent (blur sizes are fractions of the image width,
CLAHE uses a fixed tile grid), so /stream can run it on the small frame and
/capture on the full one and get the same look.
"""

from __future__ import annotations

import functools
import math

import cv2
import numpy as np

# Defaults, also what POST /camera/settings/reset returns to.
DEFAULTS = {
    "white_balance": True,
    "deglare": True,
    "deglare_strength": 0.8,  # 0-1, blends against the un-deglared frame; 0.8 keeps it natural
    "enhance": True,
    "target": 140,  # enhance's median lightness target, 0-255
    "keep_chroma": False,  # scale colour with lightness so lifted fish keep their colour
    "saturation": 0.8,  # colour strength, 0-2; the camera renders the fish more orange than a phone
}

# Deglare's blur, as a fraction of the image width. The ZED used sigma 60 on a
# ~1580 px wide tray crop; 60 / 1580 = 0.038.
DEGLARE_SIGMA_FRAC = 0.038
CLAHE_CLIP = 1.5
UNSHARP = 1.3
GAMMA_MIN = 0.6  # strongest lift
WB_GAIN_LIMIT = (0.5, 2.0)


def check(settings: dict) -> str | None:
    """None if every value is acceptable, else why not."""
    for k, v in settings.items():
        if k not in DEFAULTS:
            return f"unknown processing setting {k!r}"
        if isinstance(DEFAULTS[k], bool):
            if not isinstance(v, bool):
                return f"{k} must be true or false"
        elif k == "deglare_strength":
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0.0 <= v <= 1.0:
                return "deglare_strength must be 0-1"
        elif k == "saturation":
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0.0 <= v <= 2.0:
                return "saturation must be 0-2"
        elif k == "target":
            if not isinstance(v, int) or isinstance(v, bool) or not 60 <= v <= 220:
                return "target must be 60-220"
    return None


def _small(image: np.ndarray, width: int = 640) -> np.ndarray:
    h, w = image.shape[:2]
    if w <= width:
        return image
    return cv2.resize(image, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)


def white_balance_gains(image: np.ndarray) -> tuple[float, float, float]:
    """Per-channel (B, G, R) gains that make the least colourful part of the
    frame grey. Measured on a small copy: it's an average, detail is irrelevant."""
    small = _small(image)
    lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB)
    L = lab[:, :, 0]
    chroma = np.hypot(lab[:, :, 1].astype(np.float32) - 128, lab[:, :, 2].astype(np.float32) - 128)
    usable = (L > 30) & (L < 240) & (small.max(axis=2) < 250)  # not black, not clipped
    if usable.sum() < 100:
        return 1.0, 1.0, 1.0
    neutral = usable & (chroma <= np.percentile(chroma[usable], 50))
    means = small[neutral].reshape(-1, 3).astype(np.float64).mean(axis=0)
    grey = means.mean()
    lo, hi = WB_GAIN_LIMIT
    return tuple(float(np.clip(grey / max(m, 1.0), lo, hi)) for m in means)


def apply_gains(image: np.ndarray, gains) -> np.ndarray:
    lut = np.stack(
        [np.clip(np.arange(256) * g, 0, 255).astype(np.uint8) for g in gains], axis=1
    )  # 256x3
    out = np.empty_like(image)
    for c in range(3):
        out[:, :, c] = cv2.LUT(image[:, :, c], lut[:, c])
    return out


def _keep_chroma(lab: np.ndarray, l_before: np.ndarray) -> None:
    """Scale a*/b* in place by how much L moved, clamped to 0.5-2x."""
    k = (lab[:, :, 0].astype(np.float32) + 1.0) / (l_before.astype(np.float32) + 1.0)
    k = np.clip(k, 0.5, 2.0)[:, :, None]
    ab = 128.0 + (lab[:, :, 1:].astype(np.float32) - 128.0) * k
    lab[:, :, 1:] = np.clip(ab, 0, 255).astype(np.uint8)


def _illumination(L: np.ndarray) -> np.ndarray:
    """The smooth brightness field over the frame. Blurred on a 1/8 copy and
    scaled back: a sigma of ~120 px on 20 MP is seconds of CPU otherwise, and
    the field has no detail to lose."""
    h, w = L.shape
    f = 8 if w >= 800 else 1
    small = cv2.resize(L, (max(1, w // f), max(1, h // f)), interpolation=cv2.INTER_AREA)
    sigma = max(1.0, DEGLARE_SIGMA_FRAC * w / f)
    blurred = cv2.GaussianBlur(small, (0, 0), sigma)
    return cv2.resize(blurred, (w, h), interpolation=cv2.INTER_LINEAR)


@functools.lru_cache(maxsize=32)
def _gamma_lut(gamma: float) -> np.ndarray:
    return np.array([((i / 255.0) ** gamma) * 255.0 for i in range(256)], dtype=np.uint8)


def _lift_gamma(L: np.ndarray, target: int) -> tuple[float, int]:
    """The gamma that lands the centre's median lightness on `target`."""
    h, w = L.shape
    centre = L[int(h * 0.1):int(h * 0.9), int(w * 0.1):int(w * 0.9)]
    median = int(np.median(centre)) if centre.size else 0
    if median <= 0:
        return GAMMA_MIN, median
    if median >= target:
        return 1.0, median
    gamma = math.log(target / 255.0) / math.log(median / 255.0)
    gamma = round(min(max(gamma, GAMMA_MIN), 1.0) / 0.02) * 0.02
    return round(gamma, 2), median


def process(image: np.ndarray, settings: dict, stats: dict | None = None) -> np.ndarray:
    """Run the enabled steps on a BGR image. `stats`, if given, is filled with
    what each step decided (for the response headers)."""
    s = {**DEFAULTS, **settings}
    stats = stats if stats is not None else {}
    out = image
    if s["white_balance"]:
        gains = white_balance_gains(out)
        out = apply_gains(out, gains)
        stats["wb_gains"] = [round(g, 3) for g in gains]
    if s["deglare"] or s["enhance"] or s["saturation"] != 1.0:
        lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
        l_before = lab[:, :, 0].copy()
        L = l_before
        if s["deglare"]:
            Lf = L.astype(np.float32)
            illum = _illumination(Lf)
            flat = Lf / np.maximum(illum, 1.0) * float(np.median(illum))
            if s["deglare_strength"] < 1.0:
                flat = Lf + s["deglare_strength"] * (flat - Lf)
            L = np.clip(flat, 0, 255).astype(np.uint8)
        if s["enhance"]:
            L = cv2.createCLAHE(clipLimit=CLAHE_CLIP, tileGridSize=(8, 8)).apply(L)
            gamma, median = _lift_gamma(L, s["target"])
            if gamma < 1.0:
                L = cv2.LUT(L, _gamma_lut(gamma))
            stats["gamma"], stats["median"] = gamma, median
        lab[:, :, 0] = L
        if s["keep_chroma"]:
            _keep_chroma(lab, l_before)
        if s["saturation"] != 1.0:
            ab = 128.0 + (lab[:, :, 1:].astype(np.float32) - 128.0) * s["saturation"]
            lab[:, :, 1:] = np.clip(ab, 0, 255).astype(np.uint8)
        out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        if s["enhance"]:
            # Unsharp radius scales with the image so the look matches across sizes.
            sigma = max(0.8, 1.5 * out.shape[1] / 1600)
            blurred = cv2.GaussianBlur(out, (0, 0), sigma)
            out = cv2.addWeighted(out, UNSHARP, blurred, -(UNSHARP - 1.0), 0)
    return out
