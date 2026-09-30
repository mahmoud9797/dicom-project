"""
coDiagnostiX Exact CBCT Multi-Slice Extractor - Core Engine

Rebuild the native CBCT cache volume as a classic multi-slice DICOM series.
By default, correct a left/right mirrored cache by reversing the columns of
every slice while preserving the original voxel values. No resampling,
interpolation, resizing, cropping, padding, or scaling is performed.

This module contains only the extraction logic - no GUI or web framework code.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import struct
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional


APP_TITLE = "coDiagnostiX Exact CBCT Multi-Slice Extractor"
APP_VERSION = "2.0-WEB"

LONG_VR = {
    b"OB", b"OD", b"OF", b"OL", b"OV", b"OW", b"SQ",
    b"UC", b"UR", b"UT", b"UN", b"SV", b"UV",
}
TEXT_VR = {
    b"AE", b"AS", b"CS", b"DA", b"DS", b"DT", b"IS", b"LO",
    b"LT", b"PN", b"SH", b"ST", b"TM", b"UC", b"UR", b"UT",
}


@dataclass
class PairInfo:
    cdx_path: Path
    cache_path: Path
    scan_id: str
    patient_id: str
    device: str
    rows: int
    cols: int
    slices: int
    spacing_x: float
    spacing_y: float
    spacing_z_db: float
    used_bits: int
    bytes_per_pixel: int
    hu_low: float
    hu_high: float


@dataclass
class ExtractionResult:
    output_dir: Path
    zip_path: Path
    report_path: Path
    scan_id: str
    rows: int
    cols: int
    slices: int
    spacing_x: float
    spacing_y: float
    spacing_z: float
    source_hash: str
    output_hash: str
    left_right_fixed: bool


# ---------------------------------------------------------------------------
# DICOM byte helpers
# ---------------------------------------------------------------------------

def pack_elem(tag, vr, value) -> bytes:
    if isinstance(vr, str):
        vr = vr.encode("ascii")
    if isinstance(value, str):
        value = value.encode("utf-8")

    g, e = tag
    if vr == b"UI":
        value = value.rstrip(b"\0 ")
        if len(value) % 2:
            value += b"\0"
    elif vr in TEXT_VR:
        if len(value) % 2:
            value += b" "
    elif len(value) % 2:
        value += b"\0"

    if vr in LONG_VR:
        return (
            struct.pack("<HH", g, e)
            + vr
            + b"\0\0"
            + struct.pack("<I", len(value))
            + value
        )
    return struct.pack("<HH", g, e) + vr + struct.pack("<H", len(value)) + value


def us(tag, n) -> bytes:
    return pack_elem(tag, b"US", struct.pack("<H", int(n)))


def ul(tag, n) -> bytes:
    return pack_elem(tag, b"UL", struct.pack("<I", int(n)))


def txt(tag, vr, s) -> bytes:
    return pack_elem(tag, vr, str(s).encode("utf-8"))


def parse_hdr(buf: bytes, off: int):
    if off + 8 > len(buf):
        raise RuntimeError("Unexpected end of DICOM while reading element header")
    g, e = struct.unpack_from("<HH", buf, off)
    if g == 0xFFFE:
        return g, e, b"", struct.unpack_from("<I", buf, off + 4)[0], 8

    vr = buf[off + 4:off + 6]
    if vr in LONG_VR:
        if off + 12 > len(buf):
            raise RuntimeError("Unexpected end of DICOM while reading long-VR header")
        return g, e, vr, struct.unpack_from("<I", buf, off + 8)[0], 12

    return g, e, vr, struct.unpack_from("<H", buf, off + 6)[0], 8


def skip_undefined_sequence(buf: bytes, value_off: int) -> int:
    pos = value_off
    while pos + 8 <= len(buf):
        g, e = struct.unpack_from("<HH", buf, pos)
        if (g, e) == (0xFFFE, 0xE0DD):
            return pos + 8
        if (g, e) != (0xFFFE, 0xE000):
            raise RuntimeError(f"Unexpected item tag in sequence at byte {pos}: {(g, e)}")

        item_len = struct.unpack_from("<I", buf, pos + 4)[0]
        pos += 8
        if item_len != 0xFFFFFFFF:
            pos += item_len
            continue

        while pos + 8 <= len(buf):
            gg, ee = struct.unpack_from("<HH", buf, pos)
            if (gg, ee) == (0xFFFE, 0xE00D):
                pos += 8
                break

            _g2, _e2, _vr2, length2, header2 = parse_hdr(buf, pos)
            value2 = pos + header2
            if length2 == 0xFFFFFFFF:
                pos = skip_undefined_sequence(buf, value2)
            else:
                pos = value2 + length2
        else:
            raise RuntimeError("Sequence item delimiter not found")

    raise RuntimeError("Sequence delimiter not found")


def top_level_elements(buf: bytes):
    if len(buf) < 132 or buf[128:132] != b"DICM":
        raise RuntimeError("Embedded SCAN_SCANDATA is not a Part-10 DICOM object")

    pos = 132
    out = []
    while pos < len(buf):
        start = pos
        g, e, vr, length, header_len = parse_hdr(buf, pos)
        value_off = pos + header_len

        if (g, e) == (0x7FE0, 0x0010):
            out.append(((g, e), vr, length, start, None, value_off))
            break

        end = (
            skip_undefined_sequence(buf, value_off)
            if length == 0xFFFFFFFF
            else value_off + length
        )
        if end > len(buf):
            raise RuntimeError(f"DICOM element {(g, e)} extends beyond source data")
        out.append(((g, e), vr, length, start, end, value_off))
        pos = end

    return out


def parse_text_top(buf: bytes, tops, tag, default="") -> str:
    for current_tag, _vr, _length, _start, end, value_off in tops:
        if current_tag == tag and end is not None:
            return buf[value_off:end].rstrip(b"\0 ").decode("utf-8", "replace")
    return default


def _find_ds_in_range(buf: bytes, start: int, end: int, tag):
    pattern = struct.pack("<HH", *tag) + b"DS"
    k = buf.find(pattern, start, end)
    if k == -1:
        return None
    _g, _e, _vr, length, header_len = parse_hdr(buf, k)
    raw = buf[k + header_len:k + header_len + length]
    return raw.rstrip(b" ")


def extract_frame_geometry(buf: bytes, tops):
    """Return list[(IPP_raw, IOP_raw)] from Per-Frame Functional Groups."""
    per_frame = next((r for r in tops if r[0] == (0x5200, 0x9230)), None)
    if per_frame is None:
        raise RuntimeError(
            "Embedded DICOM has no PerFrameFunctionalGroupsSequence; "
            "exact per-slice geometry cannot be recovered safely."
        )

    pos = per_frame[5]
    frames = []

    shared_iop = None
    shared = next((r for r in tops if r[0] == (0x5200, 0x9229)), None)
    if shared is not None:
        shared_end = shared[4] if shared[4] is not None else len(buf)
        shared_iop = _find_ds_in_range(buf, shared[5], shared_end, (0x0020, 0x0037))

    top_iop_text = parse_text_top(buf, tops, (0x0020, 0x0037), "")
    top_iop = top_iop_text.encode("ascii", "replace") if top_iop_text else None

    while pos + 8 <= len(buf):
        g, e = struct.unpack_from("<HH", buf, pos)
        length = struct.unpack_from("<I", buf, pos + 4)[0]
        if (g, e) == (0xFFFE, 0xE0DD):
            break
        if (g, e) != (0xFFFE, 0xE000):
            raise RuntimeError(f"Bad per-frame item at byte {pos}: {(g, e)}")

        item_start = pos + 8
        q = item_start
        ipp = None
        iop = None

        if length == 0xFFFFFFFF:
            while q + 8 <= len(buf):
                gg, ee = struct.unpack_from("<HH", buf, q)
                if (gg, ee) == (0xFFFE, 0xE00D):
                    pos = q + 8
                    break

                gg, ee, vr, ll, hh = parse_hdr(buf, q)
                vv = q + hh
                if vr == b"SQ":
                    seq_end = skip_undefined_sequence(buf, vv)
                    if ipp is None:
                        ipp = _find_ds_in_range(buf, vv, seq_end, (0x0020, 0x0032))
                    if iop is None:
                        iop = _find_ds_in_range(buf, vv, seq_end, (0x0020, 0x0037))
                    q = seq_end
                else:
                    if (gg, ee) == (0x0020, 0x0032):
                        ipp = buf[vv:vv + ll].rstrip(b" ")
                    elif (gg, ee) == (0x0020, 0x0037):
                        iop = buf[vv:vv + ll].rstrip(b" ")
                    q = vv + ll
            else:
                raise RuntimeError("Unterminated per-frame item")
        else:
            item_end = item_start + length
            ipp = _find_ds_in_range(buf, item_start, item_end, (0x0020, 0x0032))
            iop = _find_ds_in_range(buf, item_start, item_end, (0x0020, 0x0037))
            pos = item_end

        if iop is None:
            iop = shared_iop or top_iop
        if ipp is None or iop is None:
            raise RuntimeError(
                f"Exact geometry is incomplete in embedded DICOM frame {len(frames) + 1}. "
                "Extraction stopped rather than inventing geometry."
            )

        frames.append((ipp, iop))

    if not frames:
        raise RuntimeError("No per-frame geometry was found in embedded DICOM")
    return frames


def ds_num(value) -> str:
    """Format a DICOM DS number within the 16-character limit."""
    s = f"{float(value):.14g}"
    if len(s) > 16:
        s = f"{float(value):.9E}"
    return s


def parse_triplet(raw: bytes):
    parts = raw.decode("ascii", "replace").strip().split("\\")
    if len(parts) != 3:
        raise RuntimeError(f"Invalid ImagePositionPatient: {raw!r}")
    return tuple(float(v) for v in parts)


def parse_six(raw: bytes):
    parts = raw.decode("ascii", "replace").strip().split("\\")
    if len(parts) != 6:
        raise RuntimeError(f"Invalid ImageOrientationPatient: {raw!r}")
    return tuple(float(v) for v in parts)


def get_ipp_z(ipp_raw: bytes) -> float:
    return parse_triplet(ipp_raw)[2]


def normalize_guid(value: str) -> str:
    return str(value or "").strip().strip("{}").lower()


def row_value(row: sqlite3.Row, key: str, default=None):
    return row[key] if key in row.keys() and row[key] is not None else default


def flip_slice_left_right(slice_data: bytes, rows: int, cols: int, bytes_per_pixel: int) -> bytes:
    """Mirror one axial slice left<->right without changing pixel values."""
    row_bytes = cols * bytes_per_pixel
    expected = rows * row_bytes
    if len(slice_data) != expected:
        raise RuntimeError(
            f"Cannot left/right flip slice: got {len(slice_data)} bytes, expected {expected}."
        )
    if bytes_per_pixel not in (1, 2):
        raise RuntimeError(f"Unsupported bytes per pixel for left/right correction: {bytes_per_pixel}")

    out = bytearray(expected)
    for r in range(rows):
        start = r * row_bytes
        row = slice_data[start:start + row_bytes]
        if bytes_per_pixel == 1:
            out[start:start + row_bytes] = row[::-1]
        else:
            dst = bytearray(row_bytes)
            dst[0::2] = row[-2::-2]
            dst[1::2] = row[-1::-2]
            out[start:start + row_bytes] = dst
    return bytes(out)


# ---------------------------------------------------------------------------
# Input inspection and matching
# ---------------------------------------------------------------------------

def read_scan_rows(cdx_path: Path):
    con = sqlite3.connect(str(cdx_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM PATIENT_SCAN WHERE MODALITY='CT'").fetchall()
        if not rows:
            rows = con.execute("SELECT * FROM PATIENT_SCAN").fetchall()
        return rows
    finally:
        con.close()


def choose_scan_for_cache(cdx_path: Path, cache_path: Path):
    rows = read_scan_rows(cdx_path)
    if not rows:
        raise RuntimeError("No scan record exists in PATIENT_SCAN")

    cache_guid = normalize_guid(cache_path.stem)
    exact = [r for r in rows if normalize_guid(row_value(r, "ID", "")) == cache_guid]
    if len(exact) == 1:
        return exact[0]

    cache_size = cache_path.stat().st_size
    size_matches = []
    for r in rows:
        try:
            voxels = int(r["RESOLUTIONX"]) * int(r["RESOLUTIONY"]) * int(r["RESOLUTIONZ"])
            if voxels > 0 and cache_size % voxels == 0 and cache_size // voxels in (1, 2):
                size_matches.append(r)
        except Exception:
            pass

    if len(size_matches) == 1:
        return size_matches[0]

    ids = ", ".join(str(row_value(r, "ID", "?")) for r in rows)
    raise RuntimeError(
        "Could not safely match this cache to one scan in the .cdxPatient file.\n"
        f"Cache: {cache_path.name}\nAvailable scan IDs: {ids}"
    )


def inspect_pair(cdx_path: Path, cache_path: Path) -> PairInfo:
    cdx_path = Path(cdx_path).resolve()
    cache_path = Path(cache_path).resolve()
    if not cdx_path.is_file():
        raise RuntimeError(f".cdxPatient file does not exist: {cdx_path}")
    if not cache_path.is_file():
        raise RuntimeError(f".cache file does not exist: {cache_path}")

    scan = choose_scan_for_cache(cdx_path, cache_path)
    rows = int(row_value(scan, "RESOLUTIONY", 0))
    cols = int(row_value(scan, "RESOLUTIONX", 0))
    slices = int(row_value(scan, "RESOLUTIONZ", 0))
    if min(rows, cols, slices) <= 0:
        raise RuntimeError(f"Invalid native resolution in PATIENT_SCAN: {cols} x {rows} x {slices}")

    voxels = rows * cols * slices
    size = cache_path.stat().st_size
    if size % voxels != 0:
        raise RuntimeError(
            f"Cache size mismatch. File has {size:,} bytes but native matrix is "
            f"{cols} x {rows} x {slices} ({voxels:,} voxels)."
        )
    bpp = size // voxels
    if bpp not in (1, 2):
        raise RuntimeError(
            f"Unsupported cache bytes per voxel: {bpp}. "
            "The program will not guess or reinterpret the voxel layout."
        )

    return PairInfo(
        cdx_path=cdx_path,
        cache_path=cache_path,
        scan_id=str(row_value(scan, "ID", "")),
        patient_id=str(row_value(scan, "PATIENTID", "")),
        device=str(row_value(scan, "DEVICE", "")),
        rows=rows,
        cols=cols,
        slices=slices,
        spacing_x=float(row_value(scan, "PIXELSIZEX", 0.0)),
        spacing_y=float(row_value(scan, "PIXELSIZEY", 0.0)),
        spacing_z_db=float(row_value(scan, "PIXELSIZEZ", 0.0)),
        used_bits=int(row_value(scan, "USEDBITS", bpp * 8)),
        bytes_per_pixel=bpp,
        hu_low=float(row_value(scan, "HOUNSFIELDLOW", 0.0)),
        hu_high=float(row_value(scan, "HOUNSFIELDHIGH", (2 ** (bpp * 8)) - 1)),
    )


def auto_find_cache_for_cdx(cdx_path: Path) -> Optional[Path]:
    try:
        rows = read_scan_rows(cdx_path)
    except Exception:
        return None

    search_dirs = [cdx_path.parent]
    if cdx_path.parent.parent != cdx_path.parent:
        search_dirs.append(cdx_path.parent.parent)

    names = []
    for r in rows:
        sid = str(row_value(r, "ID", ""))
        if sid:
            names.extend([f"{sid}.cache", f"{sid.strip('{}')}.cache"])

    for base in search_dirs:
        for name in names:
            p = base / name
            if p.is_file():
                return p.resolve()

    target_ids = {normalize_guid(row_value(r, "ID", "")) for r in rows}
    try:
        for p in cdx_path.parent.iterdir():
            if p.is_file() and p.suffix.lower() == ".cache" and normalize_guid(p.stem) in target_ids:
                return p.resolve()
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Exact extraction
# ---------------------------------------------------------------------------

def extract_exact(
    cdx_path: Path,
    cache_path: Path,
    output_root: Path,
    progress: Optional[Callable[[float, str], None]] = None,
    fix_left_right: bool = True,
    patient_name: str | None = None,
    patient_id: str | None = None,
) -> ExtractionResult:
    def report_progress(fraction: float, message: str):
        if progress:
            progress(max(0.0, min(1.0, fraction)), message)

    cdx_path = Path(cdx_path).resolve()
    cache_path = Path(cache_path).resolve()
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    report_progress(0.01, "Reading coDiagnostiX patient database...")

    con = sqlite3.connect(str(cdx_path))
    con.row_factory = sqlite3.Row
    try:
        scan = choose_scan_for_cache(cdx_path, cache_path)
        patient = con.execute(
            "SELECT * FROM PATIENT WHERE ID=?", (row_value(scan, "PATIENTID", ""),)
        ).fetchone()
        if patient is None:
            patient = con.execute("SELECT * FROM PATIENT LIMIT 1").fetchone()
        row = con.execute(
            "SELECT DATA FROM SCAN_SCANDATA WHERE SCANID=?", (row_value(scan, "ID", ""),)
        ).fetchone()
        if row is None or row[0] is None:
            raise RuntimeError("SCAN_SCANDATA does not contain the embedded source DICOM")
        embedded = bytes(row[0])
    finally:
        con.close()

    report_progress(0.06, "Reading native cache and validating matrix...")

    rows = int(row_value(scan, "RESOLUTIONY", 0))
    cols = int(row_value(scan, "RESOLUTIONX", 0))
    nslices = int(row_value(scan, "RESOLUTIONZ", 0))
    if min(rows, cols, nslices) <= 0:
        raise RuntimeError(f"Invalid native matrix in database: {cols} x {rows} x {nslices}")

    spacing_x = float(row_value(scan, "PIXELSIZEX", 0.0))
    spacing_y = float(row_value(scan, "PIXELSIZEY", 0.0))
    spacing_z_db = float(row_value(scan, "PIXELSIZEZ", 0.0))
    if min(spacing_x, spacing_y, spacing_z_db) <= 0:
        raise RuntimeError(
            "Native pixel spacing is missing/invalid in PATIENT_SCAN. "
            "Extraction stopped rather than inventing geometry."
        )

    raw = cache_path.read_bytes()
    voxels = rows * cols * nslices
    if len(raw) % voxels != 0:
        raise RuntimeError(
            f"Cache byte size {len(raw):,} does not divide exactly by native voxel count {voxels:,}."
        )
    bytes_per_pixel = len(raw) // voxels
    if bytes_per_pixel not in (1, 2):
        raise RuntimeError(
            f"Native cache has {bytes_per_pixel} bytes/voxel. Only exact 8-bit and 16-bit caches "
            "are supported; no conversion will be attempted."
        )

    bits_allocated = bytes_per_pixel * 8
    used_bits_db = int(row_value(scan, "USEDBITS", bits_allocated))
    bits_stored = used_bits_db if 1 <= used_bits_db <= bits_allocated else bits_allocated
    high_bit = bits_stored - 1
    pixel_representation = 0

    expected_size = voxels * bytes_per_pixel
    if len(raw) != expected_size:
        raise RuntimeError(f"Cache size mismatch: {len(raw):,} vs expected {expected_size:,}")

    source_hash = hashlib.sha256(raw).hexdigest()

    report_progress(0.10, "Recovering exact orientation and per-slice positions...")
    tops = top_level_elements(embedded)
    frames = extract_frame_geometry(embedded, tops)
    if len(frames) != nslices:
        raise RuntimeError(
            f"Geometry frame count ({len(frames)}) does not equal native cache slice count ({nslices}). "
            "Extraction stopped rather than changing slice count."
        )

    iop_values = [parse_six(iop) for _ipp, iop in frames]
    iop0 = iop_values[0]
    if any(any(abs(a - b) > 1e-8 for a, b in zip(iop, iop0)) for iop in iop_values[1:]):
        raise RuntimeError(
            "Embedded DICOM contains varying slice orientation. The raw cache is a regular volume, "
            "so the program will not guess how to map it."
        )
    iop_text = "\\".join(ds_num(v) for v in iop0)

    z_positions = [get_ipp_z(ipp) for ipp, _iop in frames]
    if nslices > 1:
        z_steps = [z_positions[i + 1] - z_positions[i] for i in range(nslices - 1)]
        abs_steps = [abs(v) for v in z_steps if abs(v) > 1e-10]
        if not abs_steps:
            raise RuntimeError("Embedded DICOM slice positions do not change in Z")
        spacing_z = sum(abs_steps) / len(abs_steps)
        max_dev = max(abs(v - spacing_z) for v in abs_steps)
        if max_dev > max(1e-5, spacing_z * 1e-4):
            raise RuntimeError(
                "Embedded DICOM has non-uniform slice spacing. Extraction stopped instead of "
                "forcing it into a uniform classic-series geometry."
            )
    else:
        spacing_z = spacing_z_db

    if abs(spacing_z - spacing_z_db) > max(1e-4, spacing_z_db * 1e-3):
        raise RuntimeError(
            f"Z-spacing disagreement: embedded DICOM={spacing_z:g} mm, database={spacing_z_db:g} mm. "
            "Extraction stopped rather than choosing one arbitrarily."
        )

    x0 = float(row_value(scan, "LOWESTIMAGEPOSX", parse_triplet(frames[0][0])[0]))
    y0 = float(row_value(scan, "LOWESTIMAGEPOSY", parse_triplet(frames[0][0])[1]))

    hu_low = float(row_value(scan, "HOUNSFIELDLOW", 0.0))
    hu_high = float(row_value(scan, "HOUNSFIELDHIGH", float((1 << bits_stored) - 1)))
    stored_max = float((1 << bits_stored) - 1)
    if stored_max <= 0:
        raise RuntimeError("Invalid stored-bit depth")
    rescale_slope = (hu_high - hu_low) / stored_max
    rescale_intercept = hu_low

    study_uid = parse_text_top(embedded, tops, (0x0020, 0x000D))
    frame_uid = parse_text_top(embedded, tops, (0x0020, 0x0052))
    if not study_uid:
        study_uid = "2.25." + str(uuid.uuid4().int)
    series_uid = "2.25." + str(uuid.uuid4().int)
    impl_uid = "2.25." + str(uuid.uuid4().int)

    patient = patient or {}
    patient_keys = set(patient.keys()) if hasattr(patient, "keys") else set()
    def pget(key, default=""):
        try:
            return patient[key] if key in patient_keys and patient[key] is not None else default
        except Exception:
            return default

    source = {
        "charset": parse_text_top(embedded, tops, (0x0008, 0x0005), "ISO_IR 192"),
        "image_type": parse_text_top(embedded, tops, (0x0008, 0x0008), "ORIGINAL\\PRIMARY\\AXIAL"),
        "inst_date": parse_text_top(embedded, tops, (0x0008, 0x0012)),
        "inst_time": parse_text_top(embedded, tops, (0x0008, 0x0013)),
        "study_date": parse_text_top(embedded, tops, (0x0008, 0x0020)),
        "series_date": parse_text_top(embedded, tops, (0x0008, 0x0021)),
        "acq_date": parse_text_top(embedded, tops, (0x0008, 0x0022)),
        "content_date": parse_text_top(embedded, tops, (0x0008, 0x0023)),
        "study_time": parse_text_top(embedded, tops, (0x0008, 0x0030)),
        "series_time": parse_text_top(embedded, tops, (0x0008, 0x0031)),
        "acq_time": parse_text_top(embedded, tops, (0x0008, 0x0032)),
        "content_time": parse_text_top(embedded, tops, (0x0008, 0x0033)),
        "accession": parse_text_top(embedded, tops, (0x0008, 0x0050)),
        "manufacturer": parse_text_top(embedded, tops, (0x0008, 0x0070), "Carestream Dental"),
        "institution": parse_text_top(embedded, tops, (0x0008, 0x0080)),
        "refphys": parse_text_top(embedded, tops, (0x0008, 0x0090)),
        "station": parse_text_top(embedded, tops, (0x0008, 0x1010)),
        "study_desc": parse_text_top(embedded, tops, (0x0008, 0x1030)),
        "series_desc": parse_text_top(embedded, tops, (0x0008, 0x103E), "3D CBCT Image Set"),
        "model": parse_text_top(embedded, tops, (0x0008, 0x1090), str(row_value(scan, "DEVICE", ""))),
        "patient_name": parse_text_top(
            embedded, tops, (0x0010, 0x0010),
            str(pget("DICOMNAME", "")).replace(", ", "^").replace(",", "^"),
        ),
        "patient_id": parse_text_top(embedded, tops, (0x0010, 0x0020), str(pget("RECORDID", ""))),
        "birth": parse_text_top(embedded, tops, (0x0010, 0x0030), str(pget("DICOMBIRTHDATE", ""))),
        "sex": parse_text_top(embedded, tops, (0x0010, 0x0040), str(pget("SEX", ""))),
        "body": parse_text_top(embedded, tops, (0x0018, 0x0015), "JAW"),
        "kvp": parse_text_top(embedded, tops, (0x0018, 0x0060)),
        "software": parse_text_top(embedded, tops, (0x0018, 0x1020)),
        "patient_position": parse_text_top(embedded, tops, (0x0018, 0x5100), "HFS"),
        "study_id": parse_text_top(embedded, tops, (0x0020, 0x0010)),
        "series_number": parse_text_top(embedded, tops, (0x0020, 0x0011), "1"),
        "acq_number": parse_text_top(embedded, tops, (0x0020, 0x0012), "1"),
    }
    if patient_name:
        source["patient_name"] = patient_name
        source["charset"] = "ISO_IR 192"
    if patient_id:
        source["patient_id"] = patient_id
        source["charset"] = "ISO_IR 192"

    scan_id = str(row_value(scan, "ID", "scan"))
    safe_id = scan_id.strip("{}").replace("/", "_").replace("\\", "_") or "scan"
    base_name = f"{'CBCT_LR_FIXED' if fix_left_right else 'CBCT_EXACT'}_{safe_id}"
    output_dir = output_root / base_name
    if output_dir.exists() or (output_root / f"{base_name}.zip").exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name += f"_{stamp}"
        output_dir = output_root / base_name
    output_dir.mkdir(parents=True, exist_ok=False)
    zip_path = output_root / f"{base_name}.zip"
    report_path = output_root / f"{base_name}_validation.txt"

    sop_class = "1.2.840.10008.5.1.4.1.1.2"
    ts_explicit_le = "1.2.840.10008.1.2.1"
    slice_bytes = rows * cols * bytes_per_pixel
    expected_pixel_hash = hashlib.sha256()

    if fix_left_right:
        report_progress(0.15, f"Writing {nslices} DICOM slices with left/right mirror correction...")
    else:
        report_progress(0.15, f"Writing {nslices} extensionless DICOM slices...")

    try:
        for idx in range(nslices):
            sop_uid = "2.25." + str(uuid.uuid4().int)

            meta_payload = b"".join([
                pack_elem((0x0002, 0x0001), b"OB", b"\x00\x01"),
                txt((0x0002, 0x0002), b"UI", sop_class),
                txt((0x0002, 0x0003), b"UI", sop_uid),
                txt((0x0002, 0x0010), b"UI", ts_explicit_le),
                txt((0x0002, 0x0012), b"UI", impl_uid),
                txt((0x0002, 0x0013), b"SH", "CDX_EXACT_10"),
            ])
            file_meta = ul((0x0002, 0x0000), len(meta_payload)) + meta_payload

            z = z_positions[idx]
            ipp = f"{ds_num(x0)}\\{ds_num(y0)}\\{ds_num(z)}"
            source_slice = raw[idx * slice_bytes:(idx + 1) * slice_bytes]
            sl = (
                flip_slice_left_right(source_slice, rows, cols, bytes_per_pixel)
                if fix_left_right
                else source_slice
            )
            expected_pixel_hash.update(sl)

            elems = []
            def add(tag, vr, value):
                if isinstance(value, (bytes, bytearray)):
                    elems.append(pack_elem(tag, vr, bytes(value)))
                else:
                    elems.append(pack_elem(tag, vr, str(value).encode("utf-8")))

            add((0x0008, 0x0005), b"CS", source["charset"])
            add((0x0008, 0x0008), b"CS", source["image_type"])
            add((0x0008, 0x0012), b"DA", source["inst_date"])
            add((0x0008, 0x0013), b"TM", source["inst_time"])
            add((0x0008, 0x0016), b"UI", sop_class)
            add((0x0008, 0x0018), b"UI", sop_uid)
            add((0x0008, 0x0020), b"DA", source["study_date"])
            add((0x0008, 0x0021), b"DA", source["series_date"])
            add((0x0008, 0x0022), b"DA", source["acq_date"])
            add((0x0008, 0x0023), b"DA", source["content_date"])
            add((0x0008, 0x0030), b"TM", source["study_time"])
            add((0x0008, 0x0031), b"TM", source["series_time"])
            add((0x0008, 0x0032), b"TM", source["acq_time"])
            add((0x0008, 0x0033), b"TM", source["content_time"])
            add((0x0008, 0x0050), b"SH", source["accession"])
            add((0x0008, 0x0060), b"CS", "CT")
            add((0x0008, 0x0070), b"LO", source["manufacturer"])
            add((0x0008, 0x0080), b"LO", source["institution"])
            add((0x0008, 0x0090), b"PN", source["refphys"])
            add((0x0008, 0x1010), b"SH", source["station"])
            add((0x0008, 0x1030), b"LO", source["study_desc"])
            add((0x0008, 0x103E), b"LO", source["series_desc"])
            add((0x0008, 0x1090), b"LO", source["model"])

            add((0x0010, 0x0010), b"PN", source["patient_name"])
            add((0x0010, 0x0020), b"LO", source["patient_id"])
            add((0x0010, 0x0030), b"DA", source["birth"])
            add((0x0010, 0x0040), b"CS", source["sex"])

            add((0x0018, 0x0015), b"CS", source["body"])
            add((0x0018, 0x0050), b"DS", ds_num(spacing_z))
            if source["kvp"]:
                add((0x0018, 0x0060), b"DS", source["kvp"])
            add((0x0018, 0x0088), b"DS", ds_num(spacing_z))
            if source["software"]:
                add((0x0018, 0x1020), b"LO", source["software"])
            add((0x0018, 0x5100), b"CS", source["patient_position"])

            add((0x0020, 0x000D), b"UI", study_uid)
            add((0x0020, 0x000E), b"UI", series_uid)
            add((0x0020, 0x0010), b"SH", source["study_id"])
            add((0x0020, 0x0011), b"IS", source["series_number"])
            add((0x0020, 0x0012), b"IS", source["acq_number"])
            add((0x0020, 0x0013), b"IS", str(idx + 1))
            add((0x0020, 0x0032), b"DS", ipp)
            add((0x0020, 0x0037), b"DS", iop_text)
            if frame_uid:
                add((0x0020, 0x0052), b"UI", frame_uid)
            add((0x0020, 0x1041), b"DS", ds_num(z))

            elems.append(us((0x0028, 0x0002), 1))
            add((0x0028, 0x0004), b"CS", "MONOCHROME2")
            elems.append(us((0x0028, 0x0010), rows))
            elems.append(us((0x0028, 0x0011), cols))
            add((0x0028, 0x0030), b"DS", f"{ds_num(spacing_y)}\\{ds_num(spacing_x)}")
            elems.append(us((0x0028, 0x0100), bits_allocated))
            elems.append(us((0x0028, 0x0101), bits_stored))
            elems.append(us((0x0028, 0x0102), high_bit))
            elems.append(us((0x0028, 0x0103), pixel_representation))
            add((0x0028, 0x1050), b"DS", ds_num((hu_low + hu_high) / 2.0))
            add((0x0028, 0x1051), b"DS", ds_num(hu_high - hu_low))
            add((0x0028, 0x1052), b"DS", ds_num(rescale_intercept))
            add((0x0028, 0x1053), b"DS", ds_num(rescale_slope))
            add((0x0028, 0x1054), b"LO", "HU")
            elems.append(pack_elem((0x7FE0, 0x0010), b"OB" if bits_allocated == 8 else b"OW", sl))

            out = b"\0" * 128 + b"DICM" + file_meta + b"".join(elems)
            filename = output_dir / f"IMG{idx + 1:04d}"
            filename.write_bytes(out)

            if idx % max(1, nslices // 100) == 0 or idx == nslices - 1:
                report_progress(
                    0.15 + 0.65 * ((idx + 1) / nslices),
                    f"Writing slices: {idx + 1}/{nslices}",
                )

        expected_output_hash = expected_pixel_hash.hexdigest()

        report_progress(0.82, "Independently validating every output slice...")

        verify_hash = hashlib.sha256()
        for idx in range(nslices):
            path = output_dir / f"IMG{idx + 1:04d}"
            vals = parse_explicit_file(path)
            if val_us(vals, (0x0028, 0x0010)) != rows:
                raise RuntimeError(f"Validation failed at {path.name}: Rows changed")
            if val_us(vals, (0x0028, 0x0011)) != cols:
                raise RuntimeError(f"Validation failed at {path.name}: Columns changed")
            if val_text(vals, (0x0028, 0x0030)) != f"{ds_num(spacing_y)}\\{ds_num(spacing_x)}":
                raise RuntimeError(f"Validation failed at {path.name}: PixelSpacing changed")
            if val_text(vals, (0x0018, 0x0050)) != ds_num(spacing_z):
                raise RuntimeError(f"Validation failed at {path.name}: SliceThickness changed")
            expected_ipp = f"{ds_num(x0)}\\{ds_num(y0)}\\{ds_num(z_positions[idx])}"
            if val_text(vals, (0x0020, 0x0032)) != expected_ipp:
                raise RuntimeError(f"Validation failed at {path.name}: ImagePositionPatient changed")
            if val_text(vals, (0x0020, 0x0037)) != iop_text:
                raise RuntimeError(f"Validation failed at {path.name}: ImageOrientationPatient changed")
            pixel = vals[(0x7FE0, 0x0010)][1]
            if len(pixel) != slice_bytes:
                raise RuntimeError(f"Validation failed at {path.name}: PixelData length changed")
            verify_hash.update(pixel)

            if idx % max(1, nslices // 20) == 0 or idx == nslices - 1:
                report_progress(
                    0.82 + 0.08 * ((idx + 1) / nslices),
                    f"Validating slices: {idx + 1}/{nslices}",
                )

        if verify_hash.hexdigest() != expected_output_hash:
            raise RuntimeError(
                "Independent validation failed: output PixelData does not match the requested "
                "left/right transformation."
            )

        report_progress(0.91, "Creating ZIP...")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for idx in range(nslices):
                fn = output_dir / f"IMG{idx + 1:04d}"
                zf.write(fn, arcname=fn.name)

        with zipfile.ZipFile(zip_path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise RuntimeError(f"ZIP integrity failed at {bad}")
            if len(zf.namelist()) != nslices:
                raise RuntimeError("ZIP slice count differs from native scan slice count")

        report_progress(0.96, "Writing validation report...")

        first_z = z_positions[0]
        last_z = z_positions[-1]
        report = f"""coDiagnostiX Exact CBCT Multi-Slice Extraction Validation

Program
-------
{APP_TITLE} v{APP_VERSION}

Inputs
------
Patient name: {source['patient_name']}
Patient ID: {source['patient_id']}
Patient database: {cdx_path}
Cache: {cache_path}
Scan ID: {scan_id}
Device: {row_value(scan, 'DEVICE', '')}

Native geometry
---------------
Columns x Rows: {cols} x {rows}
Slices: {nslices}
Native Pixel Spacing X: {ds_num(spacing_x)} mm
Native Pixel Spacing Y: {ds_num(spacing_y)} mm
Slice Thickness: {ds_num(spacing_z)} mm
Spacing Between Slices: {ds_num(spacing_z)} mm
Image Orientation Patient: {iop_text}
Image Position X/Y: {ds_num(x0)} / {ds_num(y0)} mm
First slice Z: {ds_num(first_z)} mm
Last slice Z: {ds_num(last_z)} mm
Frame of Reference UID: {frame_uid}

Native cache
------------
Cache byte size: {len(raw)}
Voxel count: {voxels}
Bytes per voxel: {bytes_per_pixel}
Bits Allocated: {bits_allocated}
Bits Stored: {bits_stored}
Source cache SHA-256: {source_hash}
Output PixelData SHA-256: {verify_hash.hexdigest()}
Left/right mirror correction: {'APPLIED - columns reversed in every slice' if fix_left_right else 'OFF'}
Voxel values changed: NO
Source/output byte-for-byte match: {'INTENTIONALLY DIFFERENT (pixel order only)' if fix_left_right else 'PASS'}
HU low/high from cdxPatient: {hu_low} / {hu_high}
Rescale Intercept: {ds_num(rescale_intercept)}
Rescale Slope: {ds_num(rescale_slope)}

Geometry operations
-------------------
Resampling: NONE
Interpolation: NONE
Resizing: NONE
Cropping: NONE
Padding: NONE
Rotation: NONE
Flip: {'LEFT/RIGHT ONLY (columns reversed)' if fix_left_right else 'NONE'}
Scaling: NONE
Slice count change: NONE

Output
------
DICOM transfer syntax: Explicit VR Little Endian
DICOM files: IMG0001 through IMG{nslices:04d} (intentionally no extension)
Output folder: {output_dir}
ZIP: {zip_path}
ZIP integrity: PASS
Output Series Instance UID: {series_uid}
Validation: ALL {nslices} SLICES PASS
"""
        report_path.write_text(report, encoding="utf-8")

        report_progress(1.0, "Finished. Pixel and geometry validation PASS.")

        return ExtractionResult(
            output_dir=output_dir,
            zip_path=zip_path,
            report_path=report_path,
            scan_id=scan_id,
            rows=rows,
            cols=cols,
            slices=nslices,
            spacing_x=spacing_x,
            spacing_y=spacing_y,
            spacing_z=spacing_z,
            source_hash=source_hash,
            output_hash=verify_hash.hexdigest(),
            left_right_fixed=fix_left_right,
        )

    except Exception:
        try:
            if output_dir.exists():
                shutil.rmtree(output_dir)
        except Exception:
            pass
        try:
            if zip_path.exists():
                zip_path.unlink()
        except Exception:
            pass
        raise


def parse_explicit_file(path: Path):
    data = path.read_bytes()
    if len(data) < 132 or data[128:132] != b"DICM":
        raise RuntimeError(f"{path.name}: DICOM preamble missing")
    pos = 132
    values = {}
    while pos < len(data):
        g, e, vr, length, header_len = parse_hdr(data, pos)
        value_off = pos + header_len
        end = value_off + length
        if end > len(data):
            raise RuntimeError(f"{path.name}: tag {(g, e)} extends beyond file")
        values[(g, e)] = (vr, data[value_off:end])
        pos = end
    return values


def val_text(values, tag):
    return values[tag][1].rstrip(b"\0 ").decode("utf-8", "replace")


def val_us(values, tag):
    return struct.unpack("<H", values[tag][1])[0]
