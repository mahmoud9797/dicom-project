"""RealGUIDE 5.4.2 raw-project CBCT -> derived DICOM, with Windows drag/drop.

Python 3.10+; only the Python standard library is required on Windows.
This is a narrowly scoped recovery tool, NOT a universal project converter.
It never changes the source archive or invents pixels. Patient orientation
is NOT recovered: the user must explicitly accept an assumed axial mapping.
The project grid is not transformed to match an implant plan or STL.

GUI: python cbct_drag_drop.py
CLI: python cbct_drag_drop.py case.zip --cli --allow-assumed-orientation

See README.txt for scope, clinical limitations, references, and testing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import re
import shutil
import struct
import sys
import tempfile
import threading
import traceback
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

VERSION = "1.0.0"
SUPPORTED_SOURCE_VERSION = "5.4.2"
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_RAW_BYTES = 32 * 1024**3
MAX_SLICE_BYTES = 256 * 1024**2
CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
EXPLICIT_LE = "1.2.840.10008.1.2.1"
IMPLEMENTATION_UID = "2.25.313513593596116799561786311269730915867"
ORIENTATION = (1, 0, 0, 0, 1, 0)
LIMITATIONS = [
    "Derived project volume, NOT an original scanner DICOM export.",
    "Axial LPS orientation [1,0,0,0,1,0] is ASSUMED, not recovered or verified.",
    "Project offsets are interpreted as the first voxel center; this is unverified.",
    "Raw-layout interpretation: signed 16-bit little-endian; X varies fastest, then Y, then Z.",
    "This interpretation was tested on one RealGUIDE 5.4.2 project, not every project variant.",
    "Saved grid only: no application of user/actor matrices, STL registration, or display crop.",
    "No resampling, smoothing, filling, intensity shifting, or removal of existing blank regions.",
    "Intensity calibration is unknown; RescaleType US means unspecified units, not calibrated HU.",
    "New study, series, frame and image UIDs are generated; original scan links are not preserved.",
    "Patient identifiers are retained when available. Output and reports are NOT anonymized.",
    "Verify identity, left/right and slice orientation, scale and registration before clinical use.",
    "No guarantee of clinical suitability or coDiagnostiX/RealGUIDE import compatibility.",
]
Progress = Callable[[float, str], None]


class ExportError(Exception):
    """An unsupported or unsafe input, or a failed technical check."""


class ExportCancelled(ExportError):
    pass


def check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise ExportCancelled("Cancelled. Unfinished export files were removed.")


def new_uid() -> str:
    return "2.25." + str(uuid.uuid4().int)


def normal_name(name: str) -> str:
    return name.replace("\\", "/").casefold()


def tag_bytes(data: bytes, name: str, required: bool = True) -> bytes:
    """Read the project's XML-like tags; .pr can also contain binary previews.

    Do not use a general XML parser: these files are not well-formed XML.
    Duplicate target tags are rejected rather than choosing an arbitrary one.
    """
    n = re.escape(name.encode("ascii"))
    matches = re.findall(rb"<" + n + rb"(?:\s[^<>]*?)?>(.*?)</" + n + rb">", data, re.S)
    if not matches:
        if required:
            raise ExportError(f"Missing required project tag: {name}")
        return b""
    if len(matches) != 1:
        raise ExportError(f"Ambiguous repeated project tag: {name}")
    return matches[0].strip()


def decode_project(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # Observed Windows project text may contain CP1252 symbols.
        return data.decode("cp1252", errors="replace")


@dataclass
class Project:
    volume_member: str
    metadata_members: list[str]
    dimensions_xyz: tuple[int, int, int]
    spacing_xyz: tuple[float, float, float]
    origin_xyz: tuple[float, float, float]
    patient: dict[str, str]
    version_string: str
    window_center: float | None
    window_width: float | None
    unapplied_settings: dict[str, str]
    raw_bytes: int
    metadata_crosschecked: bool


def read_metadata(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> dict[str, Any]:
    if info.flag_bits & 1:
        raise ExportError("Encrypted metadata is unsupported. Export an unencrypted project.")
    if info.file_size > MAX_METADATA_BYTES:
        raise ExportError("Metadata exceeds the supported size. Refusing to guess its format.")
    data = zf.read(info)  # Also checks this ZIP member's CRC.
    try:
        dims = tuple(int(tag_bytes(data, f"DIM_{i}")) for i in range(3))
        spacing = tuple(float(tag_bytes(data, f"MM_{i}")) for i in range(3))
        origin = tuple(float(tag_bytes(data, f"OFFSET_{i}")) for i in range(3))
    except (ValueError, OverflowError) as exc:
        raise ExportError(f"Invalid dimensions, spacing or offsets in {info.filename}") from exc
    version = decode_project(tag_bytes(data, "VERSION_STRING"))
    found = re.search(r"RealGUIDE.*?\b(\d+\.\d+\.\d+)\b", version, re.I)
    if not found or found.group(1) != SUPPORTED_SOURCE_VERSION:
        raise ExportError(
            f"Unsupported project version: {version!r}. This exporter is restricted to the "
            f"observed RealGUIDE {SUPPORTED_SOURCE_VERSION} raw-volume layout."
        )
    if any(v < 1 or v > 65535 for v in dims):
        raise ExportError("Invalid volume dimensions.")
    if any(not math.isfinite(v) or v <= 0 for v in spacing):
        raise ExportError("Voxel spacing must contain positive finite millimetre values.")
    if any(not math.isfinite(v) for v in origin):
        raise ExportError("Project offsets must be finite.")
    patient: dict[str, str] = {}
    for line in decode_project(tag_bytes(data, "DICOM_DATA", False)).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            patient[key.strip()] = value.strip()
    def optional_number(name: str) -> float | None:
        raw = tag_bytes(data, name, False)
        if not raw:
            return None
        try:
            value = float(raw)
            return value if math.isfinite(value) else None
        except ValueError:
            return None
    settings = {}
    for name in ("PROJECT_CROPPED", "CROPPED_POINT_MIN", "CROPPED_POINT_MAX", "USER_MATRIX"):
        raw = tag_bytes(data, name, False)
        if raw:
            settings[name] = decode_project(raw)
    return dict(dimensions_xyz=dims, spacing_xyz=spacing, origin_xyz=origin,
                patient=patient, version_string=version,
                window_center=optional_number("WL"), window_width=optional_number("WW"),
                unapplied_settings=settings)


def inspect_archive(zf: zipfile.ZipFile) -> Project:
    """Find one volume and only its same-stem metadata. Never extract member paths."""
    entries: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        key = normal_name(info.filename)
        if key in entries:
            raise ExportError("Duplicate/ambiguous ZIP member names are unsupported.")
        entries[key] = info
    volumes = [i for key, i in entries.items() if key.endswith(".prinfo")]
    if len(volumes) != 1:
        raise ExportError(
            f"Expected exactly one .prinfo volume; found {len(volumes)}. "
            "Provide one RealGUIDE project ZIP. Other ZIP formats are not supported."
        )
    volume = volumes[0]
    if volume.flag_bits & 1:
        raise ExportError("Encrypted volumes are unsupported.")
    base = normal_name(volume.filename)[:-len(".prinfo")]
    metadata = [entries[base + ext] for ext in (".mobile", ".pr") if base + ext in entries]
    if not metadata:
        raise ExportError("Missing matching .mobile or .pr metadata. Dimensions will not be guessed.")
    parsed = [read_metadata(zf, info) for info in metadata]
    primary = parsed[0]
    for other in parsed[1:]:
        if primary["dimensions_xyz"] != other["dimensions_xyz"]:
            raise ExportError("Volume dimensions disagree between .mobile and .pr.")
        for key in ("spacing_xyz", "origin_xyz"):
            if not all(math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-7)
                       for a, b in zip(primary[key], other[key])):
                raise ExportError(f"{key} disagrees between .mobile and .pr.")
        for key in ("PATIENT_NAME", "PATIENT_ID", "PATIENT_DOB", "PATIENT_STUDY_DATE"):
            a, b = primary["patient"].get(key), other["patient"].get(key)
            if a and b and a != b:
                raise ExportError(f"Patient/study metadata disagrees: {key}.")
        for key, value in other["patient"].items():
            if not primary["patient"].get(key):
                primary["patient"][key] = value
        primary["unapplied_settings"].update(other["unapplied_settings"])
    x, y, z = primary["dimensions_xyz"]
    expected = x * y * z * 2
    if expected != volume.file_size:
        raise ExportError(
            f"Raw size mismatch: expected {expected:,} bytes for {x} x {y} x {z} signed "
            f"16-bit voxels, but found {volume.file_size:,}. No export was performed."
        )
    if expected > MAX_RAW_BYTES or x * y * 2 > MAX_SLICE_BYTES:
        raise ExportError("Volume exceeds this tool's configured safety limits.")
    return Project(volume.filename, [i.filename for i in metadata],
                   **primary, raw_bytes=expected, metadata_crosschecked=len(parsed) > 1)


# Minimal writer for the specific uncompressed, single-frame CT output used here.
# This is deliberately NOT a general-purpose DICOM implementation.
LONG_VR = {"OB", "OD", "OF", "OL", "OV", "OW", "SQ", "UC", "UR", "UT", "UN"}
TEXT_UTF8_VR = {"PN", "LO", "LT", "SH", "ST", "UT", "UC"}
Tag = tuple[int, int]


def ds_number(value: float) -> str:
    """DICOM DS values are limited to 16 characters, including any exponent."""
    if not math.isfinite(value):
        raise ExportError("Non-finite DICOM geometry value.")
    for precision in range(14, 5, -1):
        text = format(value, f".{precision}g")
        if len(text) <= 16:
            return text
    raise ExportError("Geometry cannot be represented safely as DICOM DS.")


def element(tag: Tag, vr: str, value: Any) -> bytes:
    if vr == "US":
        raw = struct.pack("<H", value)
    elif vr == "UL":
        raw = struct.pack("<I", value)
    elif isinstance(value, bytes):
        raw = value
    else:
        text = "\\".join(str(v) for v in value) if isinstance(value, (tuple, list)) else str(value)
        raw = text.encode("utf-8" if vr in TEXT_UTF8_VR else "ascii")
    if len(raw) & 1:
        raw += b"\0" if vr in LONG_VR or vr == "UI" else b" "
    tag_header = struct.pack("<HH", *tag) + vr.encode("ascii")
    if vr in LONG_VR:
        return tag_header + b"\0\0" + struct.pack("<I", len(raw)) + raw
    if len(raw) > 65535:
        raise ExportError(f"DICOM {vr} element too large: {tag}")
    return tag_header + struct.pack("<H", len(raw)) + raw


def dicom_date(value: str) -> str:
    for fmt in ("%Y%m%d", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).strftime("%Y%m%d")
        except ValueError:
            pass
    return ""  # Do not turn export time into an unknown acquisition date.


def safe_text(value: str, max_chars: int) -> str:
    # All original metadata is retained in source_project_metadata.json.
    # Unknown/invalid single-value identifiers are left empty, never truncated.
    value = value.strip()
    if len(value) > max_chars or "\\" in value or any(ord(c) < 32 for c in value):
        return ""
    return value


def common_elements(project: Project, uids: dict[str, str], now: datetime) -> dict[Tag, tuple[str, Any]]:
    x, y, _ = project.dimensions_xyz
    dx, dy, dz = project.spacing_xyz
    p = project.patient
    today, current_time = now.strftime("%Y%m%d"), now.strftime("%H%M%S.%f")
    # Empty Type 2 values mean unknown; no scanner, position or acquisition values are fabricated.
    elements: dict[Tag, tuple[str, Any]] = {
        (0x0008, 0x0005): ("CS", "ISO_IR 192"),
        (0x0008, 0x0008): ("CS", ["DERIVED", "SECONDARY", "AXIAL"]),
        (0x0008, 0x0012): ("DA", today),
        (0x0008, 0x0013): ("TM", current_time),
        (0x0008, 0x0016): ("UI", CT_STORAGE),
        (0x0008, 0x0020): ("DA", dicom_date(p.get("PATIENT_STUDY_DATE", ""))),
        (0x0008, 0x0021): ("DA", today),
        (0x0008, 0x0023): ("DA", today),
        (0x0008, 0x0030): ("TM", ""),
        (0x0008, 0x0031): ("TM", current_time),
        (0x0008, 0x0033): ("TM", current_time),
        (0x0008, 0x0050): ("SH", ""),
        (0x0008, 0x0060): ("CS", "CT"),
        (0x0008, 0x0070): ("LO", ""),
        (0x0008, 0x0090): ("PN", safe_text(p.get("PATIENT_REF", ""), 64)),
        (0x0008, 0x1030): ("LO", safe_text(p.get("PATIENT_STUDY_DESCR", ""), 64)),
        (0x0008, 0x103E): ("LO", "DERIVED CBCT - VERIFY ORIENTATION"),
        (0x0008, 0x2111): ("ST", "Saved RealGUIDE raw grid copied unchanged. Signed 16-bit little-endian "
                             "layout; axial LPS orientation ASSUMED. Project offset used as first "
                             "voxel center. No user matrix/crop applied. Calibration unverified. "
                             "Verify orientation, scale and registration before clinical use."),
        (0x0010, 0x0010): ("PN", safe_text(p.get("PATIENT_NAME", ""), 64)),
        (0x0010, 0x0020): ("LO", safe_text(p.get("PATIENT_ID", ""), 64)),
        (0x0010, 0x0030): ("DA", dicom_date(p.get("PATIENT_DOB", ""))),
        (0x0010, 0x0040): ("CS", ""),
        (0x0018, 0x0050): ("DS", ds_number(dz)),
        (0x0018, 0x0060): ("DS", ""),
        (0x0018, 0x0088): ("DS", ds_number(dz)),
        (0x0018, 0x5100): ("CS", ""),
        (0x0020, 0x000D): ("UI", uids["study"]),
        (0x0020, 0x000E): ("UI", uids["series"]),
        (0x0020, 0x0010): ("SH", safe_text(p.get("PATIENT_STUDY_ID", ""), 16)),
        (0x0020, 0x0011): ("IS", "1"),
        (0x0020, 0x0012): ("IS", ""),
        (0x0020, 0x0037): ("DS", ORIENTATION),
        (0x0020, 0x0052): ("UI", uids["frame"]),
        (0x0020, 0x1040): ("LO", ""),
        (0x0020, 0x4000): ("LT", "ORIENTATION ASSUMED. NOT AN ORIGINAL SCANNER EXPORT. "
                             "Original processing history and intensity calibration unknown."),
        (0x0028, 0x0002): ("US", 1),
        (0x0028, 0x0004): ("CS", "MONOCHROME2"),
        (0x0028, 0x0010): ("US", y),
        (0x0028, 0x0011): ("US", x),
        (0x0028, 0x0030): ("DS", [ds_number(dy), ds_number(dx)]),
        (0x0028, 0x0100): ("US", 16),
        (0x0028, 0x0101): ("US", 16),
        (0x0028, 0x0102): ("US", 15),
        (0x0028, 0x0103): ("US", 1),
        (0x0028, 0x1052): ("DS", "0"),
        (0x0028, 0x1053): ("DS", "1"),
        (0x0028, 0x1054): ("LO", "US"),
    }
    if project.window_center is not None and project.window_width is not None and project.window_width >= 1:
        elements[(0x0028, 0x1050)] = ("DS", ds_number(project.window_center))
        elements[(0x0028, 0x1051)] = ("DS", ds_number(project.window_width))
    return elements


def write_slice(path: Path, common: dict[Tag, tuple[str, Any]], project: Project,
                index: int, pixels: bytes, sop_uid: str) -> tuple[float, float, float]:
    origin = project.origin_xyz
    pos = (origin[0], origin[1], origin[2] + index * project.spacing_xyz[2])
    metadata = b"".join([
        element((0x0002, 0x0001), "OB", b"\x00\x01"),
        element((0x0002, 0x0002), "UI", CT_STORAGE),
        element((0x0002, 0x0003), "UI", sop_uid),
        element((0x0002, 0x0010), "UI", EXPLICIT_LE),
        element((0x0002, 0x0012), "UI", IMPLEMENTATION_UID),
        element((0x0002, 0x0013), "SH", "CBCTEXPORT_1_0"),
    ])
    tags = common.copy()
    tags[(0x0008, 0x0018)] = ("UI", sop_uid)
    tags[(0x0020, 0x0013)] = ("IS", str(index + 1))
    tags[(0x0020, 0x0032)] = ("DS", [ds_number(v) for v in pos])
    with path.open("xb") as f:
        f.write(b"\0" * 128 + b"DICM")
        f.write(element((0x0002, 0x0000), "UL", len(metadata)))
        f.write(metadata)
        for tag, (vr, value) in sorted(tags.items()):
            f.write(element(tag, vr, value))
        # Pixels have an even byte count (16-bit); avoid an extra full-slice copy.
        f.write(struct.pack("<HH2s2sI", 0x7FE0, 0x0010, b"OW", b"\0\0", len(pixels)))
        f.write(pixels)
    return pos


def read_exact(stream: BinaryIO, count: int) -> bytes:
    data = stream.read(count)
    if len(data) != count:
        raise ExportError(f"Unexpected end of data: wanted {count:,} bytes, got {len(data):,}.")
    return data


def read_generated_dicom(path: Path) -> dict[Tag, bytes]:
    """Reopen only OUR explicit-VR files. Not a general DICOM import function."""
    tags: dict[Tag, bytes] = {}
    file_size = path.stat().st_size
    with path.open("rb") as f:
        if read_exact(f, 132)[128:] != b"DICM":
            raise ExportError("DICOM signature is missing after writing.")
        previous = (-1, -1)
        while f.tell() < file_size:
            group, item = struct.unpack("<HH", read_exact(f, 4))
            tag = (group, item)
            if tag <= previous:
                raise ExportError("DICOM elements are not strictly ordered.")
            previous = tag
            vr = read_exact(f, 2).decode("ascii")
            if vr in LONG_VR:
                if read_exact(f, 2) != b"\0\0":
                    raise ExportError("Invalid DICOM reserved bytes.")
                length = struct.unpack("<I", read_exact(f, 4))[0]
            else:
                length = struct.unpack("<H", read_exact(f, 2))[0]
            if length & 1 or length > file_size - f.tell():
                raise ExportError("Invalid DICOM value length.")
            tags[tag] = read_exact(f, length)
    return tags


def tag_text(tags: dict[Tag, bytes], tag: Tag) -> str:
    return tags[tag].rstrip(b" \0").decode("utf-8")


def verify_slice(path: Path, expected: bytes, project: Project, index: int,
                 pos: tuple[float, float, float], uid: str, uids: dict[str, str]) -> bytes:
    tags = read_generated_dicom(path)
    tests = {
        (0x0002, 0x0002): CT_STORAGE, (0x0002, 0x0003): uid,
        (0x0002, 0x0010): EXPLICIT_LE, (0x0008, 0x0016): CT_STORAGE,
        (0x0008, 0x0018): uid, (0x0020, 0x000D): uids["study"],
        (0x0020, 0x000E): uids["series"], (0x0020, 0x0052): uids["frame"],
        (0x0020, 0x0013): str(index + 1), (0x0028, 0x0004): "MONOCHROME2",
        (0x0028, 0x1052): "0", (0x0028, 0x1053): "1", (0x0028, 0x1054): "US",
    }
    for tag, value in tests.items():
        if tag_text(tags, tag) != value:
            raise ExportError(f"DICOM read-back metadata mismatch: {tag}")
    for tag, value in {
        (0x0028, 0x0002): 1, (0x0028, 0x0010): project.dimensions_xyz[1],
        (0x0028, 0x0011): project.dimensions_xyz[0], (0x0028, 0x0100): 16,
        (0x0028, 0x0101): 16, (0x0028, 0x0102): 15, (0x0028, 0x0103): 1,
    }.items():
        if struct.unpack("<H", tags[tag])[0] != value:
            raise ExportError(f"DICOM read-back pixel metadata mismatch: {tag}")
    for tag, expected_numbers in {
        (0x0020, 0x0032): pos, (0x0020, 0x0037): ORIENTATION,
        (0x0028, 0x0030): (project.spacing_xyz[1], project.spacing_xyz[0]),
        (0x0018, 0x0050): (project.spacing_xyz[2],),
        (0x0018, 0x0088): (project.spacing_xyz[2],),
    }.items():
        actual = [float(v) for v in tag_text(tags, tag).split("\\")]
        if len(actual) != len(expected_numbers) or not all(
            math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-8) for a, b in zip(actual, expected_numbers)
        ):
            raise ExportError(f"DICOM read-back geometry mismatch: {tag}")
    pixels = tags[(0x7FE0, 0x0010)]
    if pixels != expected:
        raise ExportError(f"Pixel data mismatch in slice {index + 1}.")
    return pixels


def export_cbct(archive: Path | str, output_parent: Path | str | None = None, *,
                allow_assumed_orientation: bool = False, make_zip: bool = True,
                progress: Progress | None = None, cancel: threading.Event | None = None,
                patient_name: str | None = None,
                patient_id: str | None = None) -> Path:
    """Export one project. Returns a NEW output folder; never overwrites a case."""
    if not allow_assumed_orientation:
        raise ExportError("Explicit acknowledgment of assumed/unverified orientation is required.")
    report_progress = progress or (lambda fraction, message: None)
    stop = cancel or threading.Event()
    source = Path(archive).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".zip":
        raise ExportError("Select a ZIP file containing one supported RealGUIDE project.")
    parent = Path(output_parent).expanduser().resolve() if output_parent else source.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage: Path | None = None
    try:
        with zipfile.ZipFile(source, "r") as zf:
            check_cancel(stop)
            report_progress(0, "Checking project metadata and raw volume size...")
            project = inspect_archive(zf)
            x, y, count = project.dimensions_xyz
            report_progress(0.02, f"Found {x} x {y} x {count} voxels; spacing {project.spacing_xyz} mm.")
            # Uncompressed DICOM + worst-case output ZIP + modest report/metadata overhead.
            needed = project.raw_bytes * (2 if make_zip else 1) + 128 * 1024**2
            if shutil.disk_usage(parent).free < needed:
                raise ExportError(f"Not enough free output disk space. Allow at least {needed / 1024**3:.2f} GiB.")
            stage = Path(tempfile.mkdtemp(prefix=".cbct_work_", dir=parent))
            package = stage / "package"
            dicom_dir = package / "DICOM"
            dicom_dir.mkdir(parents=True)
            now = datetime.now(timezone.utc)
            uids = {key: new_uid() for key in ("study", "series", "frame")}
            common = common_elements(project, uids, now)
            if patient_name:
                common[(0x0010, 0x0010)] = ("PN", safe_text(patient_name, 64))
            if patient_id:
                common[(0x0010, 0x0020)] = ("LO", safe_text(patient_id, 64))
            source_hash, exported_hash = hashlib.sha256(), hashlib.sha256()
            manifest: list[dict[str, Any]] = []
            seen_uids: set[str] = set()
            positions: list[float] = []
            with zf.open(project.volume_member, "r") as raw:
                for index in range(count):
                    check_cancel(stop)
                    pixels = read_exact(raw, x * y * 2)
                    source_hash.update(pixels)
                    uid = new_uid()
                    if uid in seen_uids:
                        raise ExportError("Generated duplicate image UID.")
                    seen_uids.add(uid)
                    target = dicom_dir / f"CT_{index + 1:05d}.dcm"
                    pos = write_slice(target, common, project, index, pixels, uid)
                    checked_pixels = verify_slice(target, pixels, project, index, pos, uid, uids)
                    exported_hash.update(checked_pixels)
                    positions.append(float(ds_number(pos[2])))
                    manifest.append({"file": target.name, "instance_number": index + 1,
                                     "sop_instance_uid": uid, "position_mm": pos,
                                     "pixel_sha256": hashlib.sha256(pixels).hexdigest()})
                    if index % 8 == 0 or index + 1 == count:
                        report_progress(0.03 + 0.72 * (index + 1) / count,
                                        f"Wrote and verified slice {index + 1}/{count}.")
                if raw.read(1):
                    raise ExportError("Unexpected extra volume bytes.")
            if source_hash.digest() != exported_hash.digest():
                raise ExportError("Whole-volume pixel hash mismatch.")
            if any(b <= a or not math.isclose(b - a, project.spacing_xyz[2], rel_tol=1e-8, abs_tol=1e-7)
                   for a, b in zip(positions, positions[1:])):
                raise ExportError("Slice positions are not unique and uniformly spaced.")
            source_metadata = asdict(project)
            source_metadata["source_archive_name"] = source.name
            report = {
                "entered_patient_name": patient_name or "",
                "entered_patient_id": patient_id or "",
                "status": "TECHNICAL_EXPORT_PASSED; CLINICAL_ORIENTATION_NOT_VERIFIED",
                "exporter_version": VERSION, "created_utc": now.isoformat(),
                "source_project": source_metadata,
                "dicom": {"sop_class_uid": CT_STORAGE, "transfer_syntax_uid": EXPLICIT_LE,
                          "uids": uids, "image_orientation_patient": ORIENTATION,
                          "orientation_is_assumed": True, "rescale_type": "US", "rescale_slope": 1,
                          "rescale_intercept": 0, "rows": y, "columns": x, "slice_count": count},
                "validation": {"all_files_reopened_by_builtin_reader": True,
                               "all_pixel_bytes_match_source": True, "all_geometry_tags_checked": True,
                               "source_volume_sha256": source_hash.hexdigest(),
                               "exported_pixels_sha256": exported_hash.hexdigest(),
                               "unique_image_uids": len(seen_uids),
                               "uniform_increasing_slice_positions": True,
                               "independent_dicom_library_used_in_this_run": False,
                               "full_dicom_iod_conformance_validated": False,
                               "clinical_orientation_verified": False,
                               "target_planning_software_import_tested": False},
                "limitations": LIMITATIONS,
            }
            for name, obj in (("validation.json", report), ("slice_manifest.json", manifest),
                              ("source_project_metadata.json", source_metadata)):
                (package / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
            notes = (
                "DERIVED CBCT DICOM EXPORT - VERIFY ORIENTATION\n"
                "=============================================\n\n"
                f"Volume: {x} columns x {y} rows x {count} slices\n"
                f"Spacing XYZ (mm): {project.spacing_xyz}\n"
                f"Project offsets used as first voxel center (mm): {project.origin_xyz}\n"
                "Encoding: uncompressed Explicit VR Little Endian, signed 16-bit.\n"
                f"Original raw volume / exported pixel SHA-256: {source_hash.hexdigest()}\n\n"
                "TECHNICAL CHECKS PASSED. This is NOT clinical or full IOD validation.\n"
                "All generated files were reopened by this program's own narrow-format reader;\n"
                "their pixel bytes, slice metadata and geometry were compared with the input.\n\n"
                "IMPORT: Select the DICOM folder in your software's DICOM import window.\n"
                "There is no DICOMDIR. Compatibility with a specific application is not guaranteed.\n\n"
                "IMPORTANT LIMITATIONS\n" + "\n".join("- " + item for item in LIMITATIONS) + "\n"
            )
            (package / "README.txt").write_text(notes, encoding="utf-8")
            check_cancel(stop)
            staged_zip = stage / "output.zip"
            if make_zip:
                report_progress(0.76, "Creating optional ZIP (lossless archive compression)...")
                paths = sorted(p for p in package.rglob("*") if p.is_file())
                total_bytes = sum(p.stat().st_size for p in paths)
                completed = 0
                with zipfile.ZipFile(staged_zip, "w", compression=zipfile.ZIP_DEFLATED,
                                     compresslevel=3, allowZip64=True) as output_zip:
                    for path in paths:
                        with path.open("rb") as src, output_zip.open(path.relative_to(package).as_posix(),
                                                                     "w", force_zip64=True) as dst:
                            while block := src.read(1024 * 1024):
                                check_cancel(stop)
                                dst.write(block)
                                completed += len(block)
                        report_progress(0.76 + 0.22 * completed / total_bytes,
                                        f"Packaging {completed * 100 // max(total_bytes, 1)}%...")
            check_cancel(stop)
            # Names are generated, never taken from ZIP paths. Existing cases are not overwritten.
            stem = re.sub(r"[^\w.-]+", "_", source.stem, flags=re.UNICODE)[:70].strip("._") or "CBCT"
            while True:
                name = f"{stem}_CBCT_DICOM_{now:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
                final = parent / name
                final_zip = parent / (name + ".zip")
                if not final.exists() and not final_zip.exists():
                    break
            package.rename(final)
            if make_zip:
                try:
                    staged_zip.rename(final_zip)
                except OSError as exc:
                    # Do not destroy the completed, validated DICOM folder on a ZIP move failure.
                    raise ExportError(f"DICOM export succeeded at {final}, but ZIP finalization failed: {exc}") from exc
            report_progress(1, f"Complete: {final}")
            return final
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise ExportError(f"Cannot read this ZIP or its compression method: {exc}") from exc
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
