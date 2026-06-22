"""Diagnose a USB serial scale on /dev/ttyUSB0.

Run on the Jetson with:
    sudo systemctl stop stream-api      # release the port
    cd ~/stream-api
    uv run python scale_test.py

When prompted, place something on the scale and leave it there until the
script finishes (~20 seconds).
"""

import sys
import time

import serial

PORT = "/dev/ttyUSB0"
BAUDS = [2400, 4800, 9600, 19200, 38400, 57600, 115200]
READ_SECONDS = 2


def looks_like_text(data: bytes) -> bool:
    """Heuristic: is this readable ASCII text, not random binary?"""
    if not data:
        return False
    printable = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(data) >= 0.8


def sniff(baud: int) -> bytes:
    """Open the port at `baud`, read for READ_SECONDS, return raw bytes."""
    s = serial.Serial(PORT, baud, bytesize=8, parity="N", stopbits=1, timeout=READ_SECONDS)
    try:
        time.sleep(0.3)
        s.reset_input_buffer()
        return s.read(500)
    finally:
        s.close()


def try_request_response(baud: int) -> dict[bytes, bytes]:
    """Some scales stay silent until asked. Try common request commands."""
    s = serial.Serial(PORT, baud, bytesize=8, parity="N", stopbits=1, timeout=1)
    results = {}
    try:
        for cmd in [b"P\r\n", b"W\r\n", b"S\r\n", b"R\r\n", b"\x05"]:
            s.reset_input_buffer()
            s.write(cmd)
            time.sleep(0.3)
            results[cmd] = s.read(200)
    finally:
        s.close()
    return results


def main() -> int:
    print(f"\nScale diagnostic on {PORT}")
    print("Place something on the scale now and keep it there.\n")

    # Step 1: sweep baud rates, listening for streamed data
    print("--- Listening at each common baud rate ---")
    findings = {}
    for baud in BAUDS:
        try:
            data = sniff(baud)
        except Exception as e:
            print(f"  baud={baud:>6}: ERROR {e}")
            findings[baud] = None
            continue
        findings[baud] = data
        flag = "  <-- looks like text" if looks_like_text(data) else ""
        print(f"  baud={baud:>6}: {data!r}{flag}")

    # Step 2: pick a winner from the sweep
    winners = [b for b, d in findings.items() if d and looks_like_text(d)]
    if winners:
        baud = winners[0]
        print(f"\nLikely baud rate: {baud}")
        sample = findings[baud]
        if b"\r\n" in sample:
            terminator = "\\r\\n  (Windows-style CRLF)"
        elif b"\r" in sample:
            terminator = "\\r  (carriage-return only — readline() will fail)"
        elif b"\n" in sample:
            terminator = "\\n  (Unix-style LF)"
        else:
            terminator = "none detected — scale may use fixed-length frames"
        print(f"Line terminator: {terminator}")
        return 0

    # Step 3: streaming failed — try request/response at 9600
    print("\nNo readable streamed data at any baud.")
    print("Trying request/response mode at 9600 baud (some scales must be asked):\n")
    try:
        replies = try_request_response(9600)
        for cmd, reply in replies.items():
            flag = "  <-- got a reply!" if reply else ""
            print(f"  cmd={cmd!r:14}  reply={reply!r}{flag}")
        if any(r for r in replies.values()):
            print(
                "\nScale is request/response, not streaming. "
                "main.py's read_scale() needs to write a command before reading."
            )
            return 0
    except Exception as e:
        print(f"  request/response test failed: {e}")

    # Step 4: nothing worked
    print(
        "\nNo data received under any condition. Likely causes:\n"
        "  - Scale not powered on\n"
        "  - USB cable not seated / wrong cable\n"
        f"  - Scale on a different device (try: ls -l /dev/ttyUSB*)\n"
        "  - Scale needs a button press (PRINT/SEND) to transmit\n"
        "  - Frame format is not 8N1 (try the scale's manual for parity/stop bits)"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
