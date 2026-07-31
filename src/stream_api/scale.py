"""DX11 weight-indicator protocol — pure parsing and port discovery, no I/O.

Deliberately kept out of `main.py` so debug tooling (`dx11.py`) can import the
exact parser the service runs: importing `stream_api.main` constructs an
`rs.pipeline()` at module level, which would fight the live service for the
camera. Nothing here opens a port or touches hardware, so importing this module
is always safe and always cheap.

Wire format, confirmed against the indicator on the Jetson (9600 8N1)::

    S+00002.03\\r\\n

One status letter, a mandatory sign, then a fixed-width zero-padded number.

Two things about that frame drive the whole design of this module:

* **The indicator sends no unit.** The "kg" the hardware team's script prints is
  a hardcoded label, not data. So a kilogram figure can only ever be produced
  against an *assumed* unit — see `DEFAULT_UNIT` — and callers are told which
  one was used rather than being handed a number that looks measured.
* **The status letter is the only stability signal there is.** Dropping it (as
  the original script does) means an overload frame reads as an ordinary
  weight, which is the one failure mode on a weighing station that silently
  produces a plausible wrong answer.
"""

import glob
import os
import re
from typing import Optional

# Conversions to kilograms. Only used to fill `weight_kg`; `unit` always
# reports what was actually on the wire, which for this indicator is nothing.
UNIT_TO_KG: dict[str, float] = {
    "kg": 1.0,
    "g": 0.001,
    "t": 1000.0,
    "lb": 0.45359237,
    "oz": 0.028349523125,
}

# What to assume when the frame carries no unit — which is every frame this
# indicator sends. Override with STREAM_API_SCALE_UNIT if the indicator's menu
# is set to anything other than kilograms.
DEFAULT_UNIT = os.environ.get("STREAM_API_SCALE_UNIT", "kg").lower()

# Frame terminators. CR, LF and ETX all end a frame; accepting all three means
# the reader does not care which line ending the indicator is configured for.
_TERMINATORS = frozenset(b"\r\n\x03")

# Guards against an indicator that never sends a terminator: without a ceiling
# the reader's buffer would grow for as long as the service runs.
_MAX_BUFFER = 4096

# Status letters. `S`/`ST` and `U`/`US` are confirmed-plus-conventional; the
# rest are the usual siblings across indicator families. An unrecognised letter
# leaves `stable` as None (unknown) rather than guessing True.
_STABLE_CODES = frozenset({"S", "ST"})
_UNSTABLE_CODES = frozenset({"U", "US", "M", "MO"})
_OVERLOAD_CODES = frozenset({"O", "OL", "OV"})

_UNITS_PATTERN = "|".join(sorted(UNIT_TO_KG, key=len, reverse=True))

# Layer 1 — the confirmed DX11 frame. The sign is mandatory: that single
# requirement is what stops this matching arbitrary noise or a partial frame.
#
# The trailing lookahead accepts a delimiter as well as end-of-line, so a frame
# that later grows extra comma-separated fields still yields the *leading*
# weight. Anchoring on the front rather than taking the last number on the line
# is the whole difference between this and the shared script, which reads
# `S+00002.03,03` as 3.0 kg.
_RE_DX11 = re.compile(
    r"^(?P<status>[A-Za-z]{1,2})?"
    r"(?P<sign>[-+])"
    r"(?P<value>\d+(?:\.\d+)?)"
    rf"\s*(?P<unit>{_UNITS_PATTERN})?"
    r"(?=$|[,;\s])",
    re.IGNORECASE,
)

# Layer 2 — Toledo-style continuous output, the other common family. Kept so a
# menu change or a swapped indicator degrades instead of going dark.
_RE_TOLEDO = re.compile(
    r"^(?P<status>ST|US|OL)\s*,\s*(?P<mode>GS|NT|G|N)\s*,\s*"
    r"(?P<value>[-+]?\d+(?:\.\d+)?)"
    rf"\s*(?P<unit>{_UNITS_PATTERN})?",
    re.IGNORECASE,
)

# Layer 3 — any number that sits next to a unit token.
_RE_NUMBER_UNIT = re.compile(
    rf"(?P<value>[-+]?\d+(?:\.\d+)?)\s*(?P<unit>{_UNITS_PATTERN})\b",
    re.IGNORECASE,
)

# Layer 4 — last resort, a bare number.
_RE_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def split_frames(buffer: bytearray, max_bytes: int = _MAX_BUFFER) -> list[str]:
    """Drain every complete frame off the front of `buffer`, leaving a partial
    tail in place for the next read.

    Consuming only up to the last terminator is the point: a frame that is
    still arriving must never reach the parser, because a truncated
    ``S+000`` parses perfectly happily as 0.0 kg.
    """
    frames: list[str] = []
    consumed = 0
    start = 0

    for index, byte in enumerate(buffer):
        if byte not in _TERMINATORS:
            continue
        text = bytes(buffer[start:index]).decode("ascii", errors="ignore")
        # STX opens a frame when the indicator uses STX/ETX framing, so it
        # survives the split and has to come off here.
        text = text.replace("\x02", "").strip()
        if text:
            frames.append(text)
        start = index + 1
        consumed = start

    del buffer[:consumed]

    # No terminator has arrived and the tail is implausibly long — this is not
    # a frame, it is a wiring or baud-rate problem. Drop the oldest bytes so the
    # buffer cannot grow without bound while it persists.
    if len(buffer) > max_bytes:
        del buffer[: len(buffer) - max_bytes]

    return frames


def _build(
    value: Optional[float],
    unit: Optional[str],
    stable: Optional[bool],
    overload: bool,
    fmt: str,
) -> dict:
    """Assemble a reading. `unit` is what the wire said (None if it said
    nothing); `assumed_unit` is what `weight_kg` was actually computed in, so a
    caller can always tell a measured unit from an assumed one."""
    assumed = (unit or DEFAULT_UNIT).lower()
    weight_kg = None
    if value is not None:
        # An unknown unit falls back to a 1:1 conversion rather than raising —
        # the raw line is always returned alongside, so nothing is lost.
        weight_kg = round(value * UNIT_TO_KG.get(assumed, 1.0), 6)
    return {
        "weight": value,
        "unit": unit.lower() if unit else None,
        "weight_kg": weight_kg,
        "assumed_unit": assumed,
        "stable": stable,
        "overload": overload,
        "format": fmt,
    }


def _status_flags(status: Optional[str]) -> tuple[Optional[bool], bool]:
    """-> (stable, overload). `stable` is None when the frame carried no status
    letter, or one we do not recognise — unknown is not the same as steady."""
    if not status:
        return None, False
    code = status.upper()
    if code in _OVERLOAD_CODES:
        return False, True
    if code in _STABLE_CODES:
        return True, False
    if code in _UNSTABLE_CODES:
        return False, False
    return None, False


def parse_scale_line(text: str) -> Optional[dict]:
    """Parse one frame into a reading, or None if it holds no number.

    Four layers, most specific first, and the layer that matched is reported as
    `format`. That field is the thing to watch on site: if it ever reads
    something other than "dx11", the indicator is not sending what this was
    written against.
    """
    text = text.replace("\x02", "").replace("\x03", "").strip()
    if not text:
        return None

    match = _RE_DX11.match(text)
    if match:
        stable, overload = _status_flags(match.group("status"))
        # An over-range frame is not a measurement. Reporting its digits as a
        # weight is how a pinned scale ends up recorded as a valid reading.
        value = None if overload else float(match.group("sign") + match.group("value"))
        return _build(value, match.group("unit"), stable, overload, "dx11")

    match = _RE_TOLEDO.match(text)
    if match:
        stable, overload = _status_flags(match.group("status"))
        value = None if overload else float(match.group("value"))
        return _build(value, match.group("unit"), stable, overload, "toledo")

    # Prefer the last number that carries a unit over merely the last number on
    # the line. This is the fix for the shared script's `numbers[-1]`: a
    # trailing sequence or checksum field would otherwise become "the weight".
    matches = list(_RE_NUMBER_UNIT.finditer(text))
    if matches:
        last = matches[-1]
        return _build(
            float(last.group("value")), last.group("unit"), None, False, "number_unit"
        )

    numbers = _RE_NUMBER.findall(text)
    if numbers:
        return _build(float(numbers[-1]), None, None, False, "bare")

    return None


def resolve_scale_port(configured: Optional[str] = None) -> Optional[str]:
    """Pick the serial device to open, or None if there is nothing plausible.

    An explicitly configured path always wins and is returned even if it does
    not exist — a missing explicit path should fail loudly rather than silently
    fall through to whatever else happens to be plugged in.

    Discovery is `sorted()` at every step. The shared script's `glob(...)[0]`
    returns whatever order the filesystem hands back, which picks an arbitrary
    device once a second adapter is attached.
    """
    if configured:
        return configured

    # /dev/serial/by-id/ names are stable across reboots and re-enumeration;
    # ttyUSBN numbering is not, so it is only ever the fallback.
    for pattern in ("/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        ports = sorted(glob.glob(pattern))
        if ports:
            return ports[0]

    return None
