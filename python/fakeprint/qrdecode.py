"""A small QR Code (Model 2) decoder for rasters the harness has already registered to a known place.

Why this exists: the print test page carries a QR with its parameters, and the received-document analyzer must
read it back with no third-party barcode library (the venv has none).  Scope: versions 1-10, all four error
correction levels, numeric / alphanumeric / byte modes, the eight masks.  The Reed-Solomon syndromes are
checked (``ec_ok``) but errors are not corrected: a damaged symbol is reported, not repaired.

``decode_region(gray_at, rect_px)`` finds the symbol inside a pixel rectangle (the dark bounding box fixes the
module pitch; the version is the one whose timing pattern and finder patterns match), samples the grid and decodes it.
Pure Python; ``gray_at(x, y)`` returns 0..255 at continuous pixel coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# (ec codewords per block, [(blocks, data codewords per block), ...]) for versions 1..10 and levels L, M, Q, H.
_EC: dict[tuple[int, str], tuple[int, list[tuple[int, int]]]] = {}


def _t(v: int, lvl: str, ec: int, *groups: tuple[int, int]) -> None:
    _EC[(v, lvl)] = (ec, list(groups))


for _row in (
    (1, 7, (1, 19), 10, (1, 16), 13, (1, 13), 17, (1, 9)),
    (2, 10, (1, 34), 16, (1, 28), 22, (1, 22), 28, (1, 16)),
    (3, 15, (1, 55), 26, (1, 44), 18, (2, 17), 22, (2, 13)),
    (4, 20, (1, 80), 18, (2, 32), 26, (2, 24), 16, (4, 9)),
    (5, 26, (1, 108), 24, (2, 43), 18, (2, 15, 2, 16), 22, (2, 11, 2, 12)),
    (6, 18, (2, 68), 16, (4, 27), 24, (4, 19), 28, (4, 15)),
    (7, 20, (2, 78), 18, (4, 31), 18, (2, 14, 4, 15), 26, (4, 13, 1, 14)),
    (8, 24, (2, 97), 22, (2, 38, 2, 39), 22, (4, 18, 2, 19), 26, (4, 14, 2, 15)),
    (9, 30, (2, 116), 22, (3, 36, 2, 37), 20, (4, 16, 4, 17), 24, (4, 12, 4, 13)),
    (10, 18, (2, 68, 2, 69), 26, (4, 43, 1, 44), 24, (6, 19, 2, 20), 28, (6, 15, 2, 16)),
):
    _v = _row[0]
    for _i, _lvl in enumerate("LMQH"):
        _ec = _row[1 + 2 * _i]
        _g = _row[2 + 2 * _i]
        _t(_v, _lvl, _ec, *[(_g[k], _g[k + 1]) for k in range(0, len(_g), 2)])

_ALIGN = {2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
_ECL_BITS = {1: "L", 0: "M", 3: "Q", 2: "H"}
_ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"

# GF(256), primitive polynomial 0x11D
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gmul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def rs_syndromes_zero(block: list[int], nec: int) -> bool:
    """True when the codeword block (data then ``nec`` EC codewords) has all-zero syndromes."""
    for i in range(nec):
        s = 0
        for c in block:
            s = _gmul(s, _EXP[i]) ^ c
        if s:
            return False
    return True


def _bch_format(data5: int) -> int:
    v = data5 << 10
    g = 0x537
    for i in range(14, 9, -1):
        if v & (1 << i):
            v ^= g << (i - 10)
    return ((data5 << 10) | v) ^ 0x5412


_FORMATS = {_bch_format(d): d for d in range(32)}


def _function_mask(version: int) -> list[list[bool]]:
    n = 17 + 4 * version
    f = [[False] * n for _ in range(n)]

    def box(r0: int, c0: int, r1: int, c1: int) -> None:
        for r in range(max(r0, 0), min(r1, n)):
            for c in range(max(c0, 0), min(c1, n)):
                f[r][c] = True

    box(0, 0, 9, 9)
    box(0, n - 8, 9, n)
    box(n - 8, 0, n, 9)
    for i in range(n):
        f[6][i] = f[i][6] = True
    for r in _ALIGN.get(version, []):
        for c in _ALIGN.get(version, []):
            if (r, c) in ((6, 6), (6, _ALIGN[version][-1]), (_ALIGN[version][-1], 6)):
                continue
            box(r - 2, c - 2, r + 3, c + 3)
    if version >= 7:
        box(0, n - 11, 6, n - 8)
        box(n - 11, 0, n - 8, 6)
    return f


def _format_positions(n: int) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    a: list[tuple[int, int]] = []
    b: list[tuple[int, int]] = []
    for i in range(15):
        a.append((i if i < 6 else i + 1, 8) if i < 8 else (n - 15 + i, 8))
        b.append((8, n - i - 1) if i < 8 else ((8, 15 - i - 1 + 1) if i < 9 else (8, 15 - i - 1)))
    return a, b


@dataclass
class QrResult:
    ok: bool
    text: str = ""
    data: bytes = b""
    version: int = 0
    ecl: str = ""
    mask: int = -1
    ec_ok: bool = False
    note: str = ""
    modules: int = 0
    pitch_px: float = 0.0
    origin_px: tuple[float, float] = (0.0, 0.0)


def decode_matrix(m: list[list[int]]) -> QrResult:
    """Decode a square 0/1 module matrix (1 = dark)."""
    n = len(m)
    if n < 21 or (n - 17) % 4:
        return QrResult(False, note=f"bad size {n}")
    version = (n - 17) // 4
    if version > 10:
        return QrResult(False, note=f"version {version} > 10 not supported")
    pa, pb = _format_positions(n)

    def bits(pos: list[tuple[int, int]]) -> int:
        v = 0
        for i, (r, c) in enumerate(pos):
            v |= (m[r][c] & 1) << i
        return v

    fmt = None
    for raw in (bits(pa), bits(pb)):
        best = min(_FORMATS, key=lambda k: bin(k ^ raw).count("1"))
        if bin(best ^ raw).count("1") <= 3:
            fmt = _FORMATS[best]
            break
    if fmt is None:
        return QrResult(False, version=version, note="format information unreadable")
    ecl = _ECL_BITS[fmt >> 3]
    mask = fmt & 7
    fn = _function_mask(version)
    mfun = [
        lambda i, j: (i + j) % 2 == 0,
        lambda i, j: i % 2 == 0,
        lambda i, j: j % 3 == 0,
        lambda i, j: (i + j) % 3 == 0,
        lambda i, j: (i // 2 + j // 3) % 2 == 0,
        lambda i, j: (i * j) % 2 + (i * j) % 3 == 0,
        lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
        lambda i, j: ((i + j) % 2 + (i * j) % 3) % 2 == 0,
    ][mask]
    stream: list[int] = []
    up = True
    col = n - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(n - 1, -1, -1) if up else range(n)
        for r in rows:
            for c in (col, col - 1):
                if not fn[r][c]:
                    b = m[r][c] & 1
                    if mfun(r, c):
                        b ^= 1
                    stream.append(b)
        up = not up
        col -= 2
    cws = [int("".join(map(str, stream[i : i + 8])), 2) for i in range(0, len(stream) - 7, 8)]
    nec, groups = _EC[(version, ecl)]
    nblocks = sum(g[0] for g in groups)
    total = sum(g[0] * (g[1] + nec) for g in groups)
    if len(cws) < total:
        return QrResult(False, version=version, ecl=ecl, mask=mask, note=f"{len(cws)} codewords, expected {total}")
    cws = cws[:total]
    sizes = [g[1] for g in groups for _ in range(g[0])]
    blocks: list[list[int]] = [[] for _ in range(nblocks)]
    k = 0
    for i in range(max(sizes)):
        for b in range(nblocks):
            if i < sizes[b]:
                blocks[b].append(cws[k])
                k += 1
    ecs: list[list[int]] = [[] for _ in range(nblocks)]
    for i in range(nec):
        for b in range(nblocks):
            ecs[b].append(cws[k])
            k += 1
    ec_ok = all(rs_syndromes_zero(blocks[b] + ecs[b], nec) for b in range(nblocks))
    data = [c for b in blocks for c in b]
    bitstr = "".join(f"{c:08b}" for c in data)
    pos = 0
    out = bytearray()
    note = ""
    while pos + 4 <= len(bitstr):
        mode = int(bitstr[pos : pos + 4], 2)
        pos += 4
        if mode == 0:
            break
        cc_bits = {1: (10, 12, 14), 2: (9, 11, 13), 4: (8, 16, 16)}.get(mode)
        if cc_bits is None:
            note = f"unsupported mode {mode}"
            break
        cb = cc_bits[0 if version <= 9 else 1]
        count = int(bitstr[pos : pos + cb], 2)
        pos += cb
        if mode == 4:
            for _ in range(count):
                out.append(int(bitstr[pos : pos + 8], 2))
                pos += 8
        elif mode == 2:
            vals = []
            while count >= 2:
                v = int(bitstr[pos : pos + 11], 2)
                pos += 11
                vals += [v // 45, v % 45]
                count -= 2
            if count:
                vals.append(int(bitstr[pos : pos + 6], 2))
                pos += 6
            out += "".join(_ALNUM[v] for v in vals).encode()
        else:
            digs = ""
            while count >= 3:
                digs += f"{int(bitstr[pos : pos + 10], 2):03d}"
                pos += 10
                count -= 3
            if count == 2:
                digs += f"{int(bitstr[pos : pos + 7], 2):02d}"
                pos += 7
            elif count == 1:
                digs += f"{int(bitstr[pos : pos + 4], 2):d}"
                pos += 4
            out += digs.encode()
    return QrResult(True, bytes(out).decode("utf-8", "replace"), bytes(out), version, ecl, mask, ec_ok, note, n)


def decode_region(gray_at: Callable[[float, float], float], rect_px: tuple[float, float, float, float]) -> QrResult:
    """Find and decode the (upright) QR symbol inside ``rect_px = (x0, y0, x1, y1)`` in pixel coordinates."""
    x0, y0, x1, y1 = rect_px
    xs0, ys0 = int(x0), int(y0)
    w, h = int(x1) - xs0, int(y1) - ys0
    if w < 20 or h < 20:
        return QrResult(False, note="region too small")
    vals = [[gray_at(xs0 + x + 0.5, ys0 + y + 0.5) for x in range(w)] for y in range(h)]
    flat = sorted(v for row in vals for v in row)
    lo, hi = flat[int(0.02 * len(flat))], flat[int(0.98 * len(flat))]
    if hi - lo < 60:
        return QrResult(False, note="no contrast in the QR region")
    thr = (lo + hi) / 2
    dark_cols = [x for x in range(w) if any(vals[y][x] < thr for y in range(h))]
    dark_rows = [y for y in range(h) if any(vals[y][x] < thr for x in range(w))]
    if not dark_cols or not dark_rows:
        return QrResult(False, note="no dark pixels in the QR region")
    bx0, bx1 = xs0 + dark_cols[0], xs0 + dark_cols[-1] + 1
    by0, by1 = ys0 + dark_rows[0], ys0 + dark_rows[-1] + 1
    best: QrResult | None = None
    for version in range(1, 11):
        n = 17 + 4 * version
        px, py = (bx1 - bx0) / n, (by1 - by0) / n
        if not 0.6 < px / py < 1.6667:
            continue
        m = [[1 if gray_at(bx0 + (c + 0.5) * px, by0 + (r + 0.5) * py) < thr else 0 for c in range(n)] for r in range(n)]
        timing_ok = all(m[6][c] == (1 - c % 2) for c in range(8, n - 8)) and all(m[r][6] == (1 - r % 2) for r in range(8, n - 8))
        finder_ok = m[0][0] and m[0][6] and m[6][0] and m[3][3] and not m[1][1] and m[0][n - 1] and m[n - 1][0]
        if not (timing_ok and finder_ok):
            continue
        res = decode_matrix(m)
        res.pitch_px = (px + py) / 2
        res.origin_px = (float(bx0), float(by0))
        if res.ok and (best is None or res.ec_ok):
            best = res
            if res.ec_ok:
                break
    return best or QrResult(False, note="no QR symbol found (timing/finder patterns did not match any version 1-10)")
