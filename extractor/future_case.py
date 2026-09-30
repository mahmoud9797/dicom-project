from __future__ import annotations

"""
Future Case CBCT Recovery - drag and drop GUI

Recovers CBCT volumes stored in Future/Anatomage INV containers, including:
  - direct .inv files
  - Windows .exe wrappers containing an embedded INV file
  - .zip archives containing either format

The source volume can contain lossless multi-component JPEG 2000 codestreams.
This script uses imagecodecs instead of glymur, avoiding the OpenJPEG 2.3
"maximum 4 components" failure.

Outputs:
  DICOM_SERIES/                  classic single-frame CT DICOM files
  <case>_CBCT_HU.nii.gz          NIfTI volume for 3D Slicer
  DICOM_IMPORT.zip               DICOM series package
  NIFTI_AND_PREVIEWS.zip         NIfTI and preview package
  VALIDATION_REPORT.json
  metadata_header.xml
  preview images and slice_stats.csv

Recommended Python: 3.10 through 3.13, 64-bit.
Dependencies:
  python -m pip install -U numpy pydicom nibabel pillow imagecodecs tkinterdnd2
"""

import argparse
import csv
import hashlib
import importlib
import json
import math
import mmap
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import traceback
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable
import xml.etree.ElementTree as ET


def _run_pip_install(packages: list[str], *, user_install: bool = False) -> subprocess.CompletedProcess[str]:
    """Install missing binary wheels with the exact interpreter running this script."""
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--upgrade-strategy",
        "only-if-needed",
    ]
    if user_install:
        command.append("--user")
    command.extend(packages)
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _load_dependencies() -> dict[str, object]:
    """Import dependencies and restart cleanly after installing missing wheels.

    NumPy and its compiled extensions must never be removed from ``sys.modules``
    and imported again in the same process. Doing so can leave lazy modules such
    as ``numpy.linalg`` in a recursive import state. A clean interpreter restart
    is therefore mandatory after pip changes the environment.
    """
    packages = {
        "numpy": "numpy",
        "pydicom": "pydicom",
        "nibabel": "nibabel",
        "PIL": "pillow",
        "imagecodecs": "imagecodecs",
    }

    modules: dict[str, object] = {}
    missing: list[str] = []
    import_errors: dict[str, str] = {}

    for import_name, package_name in packages.items():
        try:
            modules[import_name] = importlib.import_module(import_name)
        except (ImportError, ModuleNotFoundError, OSError) as exc:
            missing.append(package_name)
            import_errors[package_name] = f"{type(exc).__name__}: {exc}"

    if not missing:
        return modules

    # Prevent an endless install/restart loop if a wheel installed but remains
    # unusable because of antivirus interference, an unsupported CPU, etc.
    if os.environ.get("FUTURE_CBCT_DEPENDENCY_RESTART") == "1":
        details = "\n".join(
            f"- {name}: {message}" for name, message in import_errors.items()
        )
        command = (
            f'"{sys.executable}" -m pip install --force-reinstall '
            f'--only-binary=:all: ' + " ".join(missing)
        )
        raise RuntimeError(
            "The required packages are still unavailable after automatic installation.\n\n"
            f"Import errors:\n{details}\n\n"
            "Close all Python windows, then run this command in Command Prompt:\n"
            + command
        )

    print("Missing Python packages: " + ", ".join(missing), flush=True)
    print("Installing them automatically with: " + sys.executable, flush=True)

    pip_check = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if pip_check.returncode != 0:
        ensure_result = subprocess.run(
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if ensure_result.returncode != 0:
            raise RuntimeError(
                "pip is not available and could not be installed.\n\n"
                + ensure_result.stdout
            )

    result = _run_pip_install(missing)
    if result.returncode != 0:
        user_result = _run_pip_install(missing, user_install=True)
        if user_result.returncode != 0:
            command = (
                f'"{sys.executable}" -m pip install --only-binary=:all: '
                + " ".join(missing)
            )
            original_details = "\n".join(
                f"- {name}: {details}" for name, details in import_errors.items()
            )
            raise RuntimeError(
                "Automatic dependency installation failed.\n\n"
                f"Initial import errors:\n{original_details}\n\n"
                f"pip output:\n{result.stdout}\n\n"
                f"pip --user output:\n{user_result.stdout}\n\n"
                "Run this command in Command Prompt while connected to the internet:\n"
                + command
            )
        result = user_result

    print(result.stdout, flush=True)
    print("Dependencies installed. Restarting the program cleanly...", flush=True)

    # Never reload NumPy or compiled packages in this process. Restart the same
    # script with the same arguments so all extensions are loaded consistently.
    os.environ["FUTURE_CBCT_DEPENDENCY_RESTART"] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])
    raise RuntimeError("Unable to restart Python after dependency installation.")


import numpy as np
import pydicom
import nibabel as nib
import imagecodecs
from PIL import Image, ImageDraw
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, UID, generate_uid


APP_VERSION = "2.2.0"
SOC_SIZ = b"\xff\x4f\xff\x51"
EOC = b"\xff\xd9"
INV_START = b"<INVFile"
INV_END = b"</INVFile>"
APPENDED_START = b"<AppendedData"
APPENDED_END = b"</AppendedData>"

LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]


@dataclass
class BlockInfo:
    offset: int
    length: int


@dataclass
class InvMetadata:
    rows: int
    columns: int
    slices: int
    spacing_x: float
    spacing_y: float
    spacing_z: float
    origin_lps: tuple[float, float, float]
    orientation: tuple[float, float, float, float, float, float]
    scalar_type: str
    bits_allocated: int
    bits_stored: int
    high_bit: int
    pixel_representation: int
    rescale_intercept: float
    rescale_slope: float
    rescale_type: str
    window_width: float
    window_center: float
    patient_name: str
    patient_id: str
    patient_birth_date: str
    patient_sex: str
    study_uid: str
    frame_uid: str
    modality: str
    manufacturer_model: str
    kvp: str
    patient_position: str


@dataclass
class SourceContext:
    source_path: Path
    temporary_directory: tempfile.TemporaryDirectory | None

    def close(self) -> None:
        if self.temporary_directory is not None:
            self.temporary_directory.cleanup()


def log_noop(message: str) -> None:
    pass


def progress_noop(value: float, message: str) -> None:
    pass


def sanitize_filename(value: str, fallback: str = "Recovered_CBCT") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    value = value.strip("._-")
    return value[:100] or fallback


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def contains_inv(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return mm.find(INV_START) >= 0 and mm.find(APPENDED_START) >= 0
    except (OSError, ValueError):
        return False


def resolve_source(input_path: Path, log: LogFn) -> SourceContext:
    input_path = input_path.expanduser().resolve()
    if not input_path.exists() or not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if input_path.suffix.lower() != ".zip":
        if not contains_inv(input_path):
            raise ValueError(
                "No embedded <INVFile> and <AppendedData> payload was found. "
                "This file may use another vendor format."
            )
        return SourceContext(input_path, None)

    log("Opening ZIP archive and searching for an INV/EXE payload...")
    temp_dir = tempfile.TemporaryDirectory(prefix="future_cbct_zip_")
    temp_root = Path(temp_dir.name)
    try:
        with zipfile.ZipFile(input_path, "r") as archive:
            candidates = [
                item for item in archive.infolist()
                if not item.is_dir()
                and Path(item.filename).suffix.lower() in {".inv", ".exe", ".bin", ".dat"}
            ]
            if not candidates:
                candidates = [item for item in archive.infolist() if not item.is_dir()]
            candidates.sort(
                key=lambda item: (
                    Path(item.filename).suffix.lower() in {".inv", ".exe"},
                    item.file_size,
                ),
                reverse=True,
            )
            for index, item in enumerate(candidates[:30], start=1):
                safe_name = sanitize_filename(Path(item.filename).name, f"candidate_{index}.bin")
                extracted = temp_root / f"{index:02d}_{safe_name}"
                log(f"Checking ZIP entry: {item.filename}")
                try:
                    with archive.open(item, "r") as src, extracted.open("wb") as dst:
                        shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
                except RuntimeError as exc:
                    if "password" in str(exc).lower() or "encrypted" in str(exc).lower():
                        raise RuntimeError("The ZIP archive is encrypted. Remove the password first.") from exc
                    raise
                if contains_inv(extracted):
                    log(f"Found embedded INV payload in: {item.filename}")
                    return SourceContext(extracted, temp_dir)
                extracted.unlink(missing_ok=True)
    except Exception:
        temp_dir.cleanup()
        raise

    temp_dir.cleanup()
    raise ValueError("No supported INV payload was found inside the ZIP archive.")


def parse_number_list(value: str | None) -> list[float]:
    if not value:
        return []
    value = value.replace("\\", " ").replace(",", " ")
    result: list[float] = []
    for token in value.split():
        try:
            result.append(float(token))
        except ValueError:
            continue
    return result


def find_value(root: ET.Element, *tag_names: str, default: str = "") -> str:
    for tag_name in tag_names:
        element = root.find(f".//{tag_name}")
        if element is None:
            continue
        value = element.get("Value")
        if value is None:
            value = element.text
        if value:
            return str(value).strip()
    return default


def valid_uid_or_new(value: str) -> str:
    value = value.strip()
    try:
        if value and UID(value).is_valid and len(value) <= 64:
            return value
    except Exception:
        pass
    return generate_uid()


def parse_inv_structure(source_path: Path, work_dir: Path, log: LogFn) -> tuple[InvMetadata, list[BlockInfo], Path]:
    metadata_path = work_dir / "metadata_header.xml"

    with source_path.open("rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            inv_start = mm.find(INV_START)
            if inv_start < 0:
                raise ValueError("INV XML start marker was not found.")
            appended_tag = mm.find(APPENDED_START, inv_start)
            if appended_tag < 0:
                raise ValueError("INV AppendedData marker was not found.")
            inv_end = mm.find(INV_END, appended_tag)
            if inv_end < 0:
                raise ValueError("INV XML end marker was not found.")

            tag_end = mm.find(b">", appended_tag)
            if tag_end < 0 or tag_end > appended_tag + 300:
                raise ValueError("Malformed AppendedData opening tag.")
            underscore = mm.find(b"_", tag_end + 1, min(tag_end + 64, inv_end))
            if underscore < 0:
                raise ValueError("Raw AppendedData underscore marker was not found.")

            xml_bytes = bytes(mm[inv_start:appended_tag]) + b"</INVFile>"
            metadata_path.write_bytes(xml_bytes)
            try:
                root = ET.fromstring(xml_bytes.decode("utf-8", errors="replace"))
            except ET.ParseError as exc:
                raise ValueError(f"Cannot parse INV XML metadata: {exc}") from exc

            volume = root.find(".//Volume")
            if volume is None:
                raise ValueError("No <Volume> element was found in the INV metadata.")

            dims = [int(round(number)) for number in parse_number_list(volume.get("Dimensions"))]
            if len(dims) < 3 or any(number <= 0 for number in dims[:3]):
                raise ValueError(f"Invalid volume dimensions: {volume.get('Dimensions')}")
            columns, rows, slices = dims[:3]

            spacing = parse_number_list(volume.get("Spacing"))
            if len(spacing) < 3 or any(number <= 0 for number in spacing[:3]):
                raise ValueError(f"Invalid voxel spacing: {volume.get('Spacing')}")
            spacing_x, spacing_y, spacing_z = spacing[:3]

            orientation_values = parse_number_list(find_value(root, "ImageOrientationPatient"))
            if len(orientation_values) >= 6:
                orientation = tuple(float(v) for v in orientation_values[:6])
            else:
                orientation = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)

            origin_values = parse_number_list(volume.get("Origin"))
            coordinate_system = parse_number_list(volume.get("CoordinateSystem"))
            if len(coordinate_system) >= 7 and any(abs(v) > 1e-9 for v in coordinate_system[-3:]):
                origin_lps = tuple(-float(v) for v in coordinate_system[-3:])
            elif len(origin_values) >= 3 and any(abs(v) > 1e-9 for v in origin_values[:3]):
                origin_lps = tuple(float(v) for v in origin_values[:3])
            else:
                origin_lps = (
                    -((columns - 1) * spacing_x) / 2.0,
                    -((rows - 1) * spacing_y) / 2.0,
                    -((slices - 1) * spacing_z) / 2.0,
                )

            scalar_type = volume.get("ScalarType", "UInt16")
            scalar_lower = scalar_type.lower()
            pixel_representation = 1 if "int16" in scalar_lower and "uint" not in scalar_lower else 0

            bits_allocated = int(float(find_value(root, "BitsAllocated", default="16") or 16))
            bits_stored = int(float(find_value(root, "BitsStore", "BitsStored", default=str(bits_allocated)) or bits_allocated))
            high_bit = int(float(find_value(root, "HighBit", default=str(bits_stored - 1)) or (bits_stored - 1)))
            rescale_intercept = float(find_value(root, "RescaleIntercept", default="0") or 0)
            rescale_slope = float(find_value(root, "RescaleSlope", default="1") or 1)
            rescale_type = find_value(root, "RescaleType", default="HU") or "HU"

            window = parse_number_list(volume.get("WindowLevel"))
            window_width = window[0] if len(window) >= 1 else 4000.0
            window_center = window[1] if len(window) >= 2 else 1000.0

            metadata = InvMetadata(
                rows=rows,
                columns=columns,
                slices=slices,
                spacing_x=spacing_x,
                spacing_y=spacing_y,
                spacing_z=spacing_z,
                origin_lps=(float(origin_lps[0]), float(origin_lps[1]), float(origin_lps[2])),
                orientation=orientation,
                scalar_type=scalar_type,
                bits_allocated=bits_allocated,
                bits_stored=bits_stored,
                high_bit=high_bit,
                pixel_representation=pixel_representation,
                rescale_intercept=rescale_intercept,
                rescale_slope=rescale_slope,
                rescale_type=rescale_type,
                window_width=window_width,
                window_center=window_center,
                patient_name=find_value(root, "PatientName") or "",
                patient_id=find_value(root, "PatientID", default="RECOVERED") or "RECOVERED",
                patient_birth_date=find_value(root, "PatientBirthDay", "PatientBirthDate"),
                patient_sex=find_value(root, "PatientSex"),
                study_uid=valid_uid_or_new(find_value(root, "StudyInstanceUID")),
                frame_uid=valid_uid_or_new(find_value(root, "FrameOfReferenceUID")),
                modality=find_value(root, "Modality", default="CT") or "CT",
                manufacturer_model=find_value(root, "MaufacturModelName", "ManufacturerModelName"),
                kvp=find_value(root, "KVP"),
                patient_position=find_value(root, "PatientPostion", "PatientPosition", default="HFS") or "HFS",
            )

            blocks: list[BlockInfo] = []
            table_pos = underscore + 1
            if table_pos + 8 <= inv_end:
                block_count, component_hint = struct.unpack_from("<II", mm, table_pos)
                if 0 < block_count < 100000 and 0 < component_hint < 10000:
                    table_bytes = 4 * (block_count + 1)
                    lengths_pos = table_pos + 8
                    data_start = lengths_pos + table_bytes
                    if data_start < inv_end:
                        values = list(struct.unpack_from(f"<{block_count + 1}I", mm, lengths_pos))
                        lengths = values[1:] if values and values[0] == 0 else values[:block_count]
                        if len(lengths) == block_count and all(length > 0 for length in lengths):
                            cursor = data_start
                            candidate_blocks: list[BlockInfo] = []
                            valid = True
                            for length in lengths:
                                if cursor + length > inv_end or mm[cursor:cursor + 4] != SOC_SIZ:
                                    valid = False
                                    break
                                candidate_blocks.append(BlockInfo(cursor, int(length)))
                                cursor += int(length)
                            if valid:
                                blocks = candidate_blocks
                                log(
                                    f"Parsed INV block table: {block_count} JPEG 2000 blocks, "
                                    f"component hint {component_hint}."
                                )

            if not blocks:
                log("Block table was not recognized; scanning JPEG 2000 codestream markers...")
                cursor = underscore + 1
                while True:
                    soc = mm.find(SOC_SIZ, cursor, inv_end)
                    if soc < 0:
                        break
                    eoc = mm.find(EOC, soc + 4, inv_end)
                    if eoc < 0:
                        raise ValueError("A JPEG 2000 codestream has no EOC marker.")
                    blocks.append(BlockInfo(soc, eoc + 2 - soc))
                    cursor = eoc + 2

            if not blocks:
                raise ValueError("No JPEG 2000 codestreams were found in AppendedData.")

    log(
        f"Metadata: {metadata.columns} x {metadata.rows} x {metadata.slices}, "
        f"spacing {metadata.spacing_x:g} x {metadata.spacing_y:g} x {metadata.spacing_z:g} mm."
    )
    log(f"Found {len(blocks)} JPEG 2000 blocks.")
    return metadata, blocks, metadata_path


def decode_volume(
    source_path: Path,
    metadata: InvMetadata,
    blocks: list[BlockInfo],
    work_dir: Path,
    reverse_z: bool,
    log: LogFn,
    progress: ProgressFn,
) -> tuple[Path, list[dict[str, float]]]:
    dtype = np.dtype("<i2" if metadata.pixel_representation else "<u2")
    raw_path = work_dir / "decoded_volume.raw"
    volume = np.memmap(
        raw_path,
        dtype=dtype,
        mode="w+",
        shape=(metadata.slices, metadata.rows, metadata.columns),
    )
    stats: list[dict[str, float]] = []
    output_index = 0

    try:
        jpeg_version = imagecodecs.jpeg2k_version()
    except Exception:
        jpeg_version = "unknown"
    log(f"JPEG 2000 decoder: imagecodecs {getattr(imagecodecs, '__version__', 'unknown')} ({jpeg_version}).")

    with source_path.open("rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for block_index, block in enumerate(blocks):
                progress(5.0 + 45.0 * block_index / max(1, len(blocks)), f"Decoding block {block_index + 1}/{len(blocks)}")
                codestream = bytes(mm[block.offset:block.offset + block.length])
                try:
                    decoded = imagecodecs.jpeg2k_decode(codestream)
                except Exception as exc:
                    raise RuntimeError(
                        f"JPEG 2000 decode failed for block {block_index + 1}: {exc}\n\n"
                        "Upgrade imagecodecs with:\n"
                        f'"{sys.executable}" -m pip install -U imagecodecs'
                    ) from exc

                decoded = np.asarray(decoded)
                if decoded.ndim == 2:
                    decoded_zyx = decoded[np.newaxis, :, :]
                elif decoded.ndim == 3:
                    if decoded.shape[0] == metadata.rows and decoded.shape[1] == metadata.columns:
                        decoded_zyx = np.transpose(decoded, (2, 0, 1))
                    elif decoded.shape[1] == metadata.rows and decoded.shape[2] == metadata.columns:
                        decoded_zyx = decoded
                    else:
                        raise ValueError(
                            f"Unexpected decoded block shape {decoded.shape}; expected rows={metadata.rows}, "
                            f"columns={metadata.columns}."
                        )
                else:
                    raise ValueError(f"Unexpected JPEG 2000 decoded rank: {decoded.ndim}")

                if decoded_zyx.shape[1:] != (metadata.rows, metadata.columns):
                    raise ValueError(
                        f"Decoded block matrix {decoded_zyx.shape[2]} x {decoded_zyx.shape[1]} does not match "
                        f"metadata {metadata.columns} x {metadata.rows}."
                    )

                for component in decoded_zyx:
                    if output_index >= metadata.slices:
                        raise ValueError(
                            f"Decoded more than the expected {metadata.slices} slices. "
                            "The source may use an unsupported block layout."
                        )
                    target_index = metadata.slices - 1 - output_index if reverse_z else output_index
                    component_cast = np.asarray(component, dtype=dtype)
                    volume[target_index] = component_cast
                    stats.append(
                        {
                            "slice": float(target_index),
                            "min": float(component_cast.min()),
                            "max": float(component_cast.max()),
                            "mean": float(component_cast.mean()),
                            "nonzero_fraction": float(np.count_nonzero(component_cast) / component_cast.size),
                        }
                    )
                    output_index += 1

    volume.flush()
    del volume

    if output_index != metadata.slices:
        raise ValueError(
            f"Decoded {output_index} slices, but metadata declares {metadata.slices}. "
            "This source uses a different or incomplete block layout."
        )

    stats.sort(key=lambda item: item["slice"])
    progress(50.0, f"Decoded all {output_index} slices")
    return raw_path, stats


def float_ds(value: float) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def _vector_norm(vector: np.ndarray) -> float:
    """Small Euclidean norm helper that avoids NumPy's lazily imported linalg."""
    values = np.asarray(vector, dtype=np.float64)
    return float(np.sqrt(np.sum(values * values)))


def build_geometry(metadata: InvMetadata) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    row_cosine = np.asarray(metadata.orientation[:3], dtype=np.float64).copy()
    column_cosine = np.asarray(metadata.orientation[3:6], dtype=np.float64).copy()

    row_length = _vector_norm(row_cosine)
    column_length = _vector_norm(column_cosine)
    if row_length < 1e-9 or column_length < 1e-9:
        row_cosine = np.array([1.0, 0.0, 0.0], dtype=np.float64)
        column_cosine = np.array([0.0, 1.0, 0.0], dtype=np.float64)
    else:
        row_cosine /= row_length
        column_cosine /= column_length

    # Remove any small non-orthogonal component before calculating the normal.
    column_cosine -= row_cosine * float(np.sum(row_cosine * column_cosine))
    column_length = _vector_norm(column_cosine)
    if column_length < 1e-9:
        row_cosine = np.array([1.0, 0.0, 0.0], dtype=np.float64)
        column_cosine = np.array([0.0, 1.0, 0.0], dtype=np.float64)
    else:
        column_cosine /= column_length

    normal = np.cross(row_cosine, column_cosine)
    normal_length = _vector_norm(normal)
    if normal_length < 1e-9:
        row_cosine = np.array([1.0, 0.0, 0.0], dtype=np.float64)
        column_cosine = np.array([0.0, 1.0, 0.0], dtype=np.float64)
        normal = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    else:
        normal /= normal_length

    origin = np.asarray(metadata.origin_lps, dtype=np.float64)
    return row_cosine, column_cosine, normal, origin


def create_dicom_series(
    raw_path: Path,
    metadata: InvMetadata,
    output_dir: Path,
    log: LogFn,
    progress: ProgressFn,
) -> tuple[str, str, str]:
    dicom_dir = output_dir / "DICOM_SERIES"
    dicom_dir.mkdir(parents=True, exist_ok=True)
    dtype = np.dtype("<i2" if metadata.pixel_representation else "<u2")
    volume = np.memmap(raw_path, dtype=dtype, mode="r", shape=(metadata.slices, metadata.rows, metadata.columns))

    study_uid = metadata.study_uid
    series_uid = generate_uid()
    frame_uid = metadata.frame_uid
    implementation_uid = generate_uid()
    row_cosine, column_cosine, normal, origin = build_geometry(metadata)
    orientation = [*row_cosine.tolist(), *column_cosine.tolist()]

    for index in range(metadata.slices):
        progress(50.0 + 25.0 * index / max(1, metadata.slices), f"Writing DICOM {index + 1}/{metadata.slices}")
        sop_uid = generate_uid()
        file_meta = FileMetaDataset()
        file_meta.FileMetaInformationVersion = b"\x00\x01"
        file_meta.MediaStorageSOPClassUID = CTImageStorage
        file_meta.MediaStorageSOPInstanceUID = sop_uid
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        file_meta.ImplementationClassUID = implementation_uid
        file_meta.ImplementationVersionName = "FUTURE_INV_220"

        path = dicom_dir / f"IM_{index + 1:04d}.dcm"
        ds = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
        ds.SpecificCharacterSet = "ISO_IR 192"
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = sop_uid
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid

        ds.PatientName = metadata.patient_name[:64]
        ds.PatientID = metadata.patient_id[:64]
        if re.fullmatch(r"\d{8}", metadata.patient_birth_date):
            ds.PatientBirthDate = metadata.patient_birth_date
        if metadata.patient_sex in {"M", "F", "O"}:
            ds.PatientSex = metadata.patient_sex

        ds.StudyDate = ""
        ds.StudyTime = ""
        ds.AccessionNumber = ""
        ds.ReferringPhysicianName = ""
        ds.StudyID = "RECOVERED"
        ds.SeriesDate = ""
        ds.SeriesTime = ""

        ds.Modality = "CT"
        ds.SeriesNumber = 1
        ds.AcquisitionNumber = 1
        ds.InstanceNumber = index + 1
        ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
        ds.StudyDescription = "Recovered CBCT"
        ds.SeriesDescription = "Recovered CBCT - Classic CT"
        ds.ProtocolName = "Recovered INV CBCT"
        ds.BodyPartExamined = "HEAD"
        ds.PatientPosition = metadata.patient_position[:16]
        ds.PositionReferenceIndicator = ""
        if metadata.manufacturer_model:
            ds.ManufacturerModelName = metadata.manufacturer_model[:64]
        if metadata.kvp:
            try:
                ds.KVP = float_ds(float(metadata.kvp))
            except ValueError:
                pass

        ds.Rows = metadata.rows
        ds.Columns = metadata.columns
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = metadata.bits_allocated
        ds.BitsStored = metadata.bits_stored
        ds.HighBit = metadata.high_bit
        ds.PixelRepresentation = metadata.pixel_representation

        ds.PixelSpacing = [float_ds(metadata.spacing_y), float_ds(metadata.spacing_x)]
        ds.SliceThickness = float_ds(metadata.spacing_z)
        ds.SpacingBetweenSlices = float_ds(metadata.spacing_z)
        ds.ImageOrientationPatient = [float_ds(v) for v in orientation]
        position = origin + normal * (index * metadata.spacing_z)
        ds.ImagePositionPatient = [float_ds(v) for v in position.tolist()]
        ds.SliceLocation = float_ds(float(np.dot(position, normal)))

        ds.RescaleIntercept = float_ds(metadata.rescale_intercept)
        ds.RescaleSlope = float_ds(metadata.rescale_slope)
        ds.RescaleType = metadata.rescale_type[:16]
        ds.WindowCenter = float_ds(metadata.window_center)
        ds.WindowWidth = float_ds(metadata.window_width)

        ds.PixelData = np.asarray(volume[index], dtype=dtype).tobytes(order="C")
        ds.save_as(path, enforce_file_format=True)

    del volume
    log(f"Created {metadata.slices} classic single-frame DICOM CT files.")
    return study_uid, series_uid, frame_uid


def create_nifti(
    raw_path: Path,
    metadata: InvMetadata,
    output_dir: Path,
    case_name: str,
    log: LogFn,
    progress: ProgressFn,
) -> Path:
    dtype = np.dtype("<i2" if metadata.pixel_representation else "<u2")
    stored = np.memmap(raw_path, dtype=dtype, mode="r", shape=(metadata.slices, metadata.rows, metadata.columns))

    minimum_hu = float(stored.min()) * metadata.rescale_slope + metadata.rescale_intercept
    maximum_hu = float(stored.max()) * metadata.rescale_slope + metadata.rescale_intercept
    if metadata.rescale_slope == 1.0 and minimum_hu >= -32768 and maximum_hu <= 32767:
        hu_dtype = np.dtype("<i2")
    elif minimum_hu >= -2147483648 and maximum_hu <= 2147483647 and float(metadata.rescale_slope).is_integer():
        hu_dtype = np.dtype("<i4")
    else:
        hu_dtype = np.dtype("<f4")

    hu_raw = output_dir / "_temporary_hu_volume.raw"
    hu_volume = np.memmap(hu_raw, dtype=hu_dtype, mode="w+", shape=stored.shape)
    chunk_size = max(1, min(32, metadata.slices))
    for start in range(0, metadata.slices, chunk_size):
        stop = min(metadata.slices, start + chunk_size)
        progress(75.0 + 4.0 * start / max(1, metadata.slices), "Converting NIfTI intensities")
        converted = stored[start:stop].astype(np.float64) * metadata.rescale_slope + metadata.rescale_intercept
        hu_volume[start:stop] = converted.astype(hu_dtype)
    hu_volume.flush()

    row_cosine, column_cosine, normal, origin = build_geometry(metadata)
    affine_lps = np.eye(4, dtype=float)
    affine_lps[:3, 0] = row_cosine * metadata.spacing_x
    affine_lps[:3, 1] = column_cosine * metadata.spacing_y
    affine_lps[:3, 2] = normal * metadata.spacing_z
    affine_lps[:3, 3] = origin
    lps_to_ras = np.diag([-1.0, -1.0, 1.0, 1.0])
    affine_ras = lps_to_ras @ affine_lps

    data_xyz = np.transpose(hu_volume, (2, 1, 0))
    image = nib.Nifti1Image(data_xyz, affine_ras)
    image.header.set_xyzt_units("mm")
    image.header["descrip"] = b"Recovered CBCT in rescaled HU-like values"
    image.set_qform(affine_ras, code=1)
    image.set_sform(affine_ras, code=1)
    nifti_path = output_dir / f"{case_name}_CBCT_HU.nii.gz"
    nib.save(image, nifti_path)

    del image, data_xyz, hu_volume, stored
    try:
        hu_raw.unlink()
    except OSError:
        pass
    log(f"Created NIfTI volume: {nifti_path.name}")
    return nifti_path


def normalize_preview(array: np.ndarray, low: float | None = None, high: float | None = None) -> np.ndarray:
    array = np.asarray(array, dtype=np.float32)
    if low is None or high is None or high <= low:
        nonzero = array[np.isfinite(array)]
        if nonzero.size:
            low, high = np.percentile(nonzero, [1.0, 99.5])
        else:
            low, high = 0.0, 1.0
    if high <= low:
        high = low + 1.0
    scaled = np.clip((array - low) / (high - low), 0.0, 1.0)
    return (scaled * 255.0).astype(np.uint8)


def create_previews_and_stats(
    raw_path: Path,
    metadata: InvMetadata,
    output_dir: Path,
    stats: list[dict[str, float]],
    log: LogFn,
) -> None:
    dtype = np.dtype("<i2" if metadata.pixel_representation else "<u2")
    stored = np.memmap(raw_path, dtype=dtype, mode="r", shape=(metadata.slices, metadata.rows, metadata.columns))
    hu = np.asarray(stored, dtype=np.float32) * metadata.rescale_slope + metadata.rescale_intercept
    low = metadata.window_center - metadata.window_width / 2.0
    high = metadata.window_center + metadata.window_width / 2.0

    axial = normalize_preview(hu[metadata.slices // 2], low, high)
    coronal = normalize_preview(hu[:, metadata.rows // 2, :], low, high)
    sagittal = normalize_preview(hu[:, :, metadata.columns // 2], low, high)
    Image.fromarray(axial).save(output_dir / "axial_mid.png")
    Image.fromarray(coronal).save(output_dir / "coronal_mid.png")
    Image.fromarray(sagittal).save(output_dir / "sagittal_mid.png")

    count = min(16, metadata.slices)
    indices = np.linspace(0, metadata.slices - 1, count, dtype=int)
    tiles = [normalize_preview(hu[index], low, high) for index in indices]
    tile_h, tile_w = tiles[0].shape
    columns = 4
    rows = math.ceil(count / columns)
    montage = Image.new("L", (columns * tile_w, rows * tile_h), 0)
    draw = ImageDraw.Draw(montage)
    for position, (index, tile) in enumerate(zip(indices, tiles)):
        x = (position % columns) * tile_w
        y = (position // columns) * tile_h
        montage.paste(Image.fromarray(tile), (x, y))
        draw.text((x + 5, y + 5), str(int(index + 1)), fill=255)
    montage.save(output_dir / "axial_montage.png")

    with (output_dir / "slice_stats.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["slice", "min", "max", "mean", "nonzero_fraction"])
        writer.writeheader()
        for item in stats:
            writer.writerow(
                {
                    "slice": int(item["slice"]),
                    "min": int(item["min"]),
                    "max": int(item["max"]),
                    "mean": f"{item['mean']:.6f}",
                    "nonzero_fraction": f"{item['nonzero_fraction']:.8f}",
                }
            )
    del hu, stored
    log("Created axial, coronal, sagittal, and montage previews.")


def validate_outputs(
    raw_path: Path,
    metadata: InvMetadata,
    output_dir: Path,
    nifti_path: Path,
    study_uid: str,
    series_uid: str,
    frame_uid: str,
    source_hash: str,
    log: LogFn,
    progress: ProgressFn,
) -> dict[str, object]:
    errors: list[str] = []
    dicom_paths = sorted((output_dir / "DICOM_SERIES").glob("*.dcm"))
    dtype = np.dtype("<i2" if metadata.pixel_representation else "<u2")
    original = np.memmap(raw_path, dtype=dtype, mode="r", shape=(metadata.slices, metadata.rows, metadata.columns))
    positions: list[np.ndarray] = []
    sop_uids: set[str] = set()
    series_uids: set[str] = set()
    study_uids: set[str] = set()

    if len(dicom_paths) != metadata.slices:
        errors.append(f"Expected {metadata.slices} DICOM files, found {len(dicom_paths)}")

    for index, path in enumerate(dicom_paths):
        progress(80.0 + 12.0 * index / max(1, len(dicom_paths)), f"Validating DICOM {index + 1}/{len(dicom_paths)}")
        try:
            ds = pydicom.dcmread(path)
            if ds.file_meta.TransferSyntaxUID != ExplicitVRLittleEndian:
                errors.append(f"{path.name}: wrong transfer syntax")
            if int(ds.Rows) != metadata.rows or int(ds.Columns) != metadata.columns:
                errors.append(f"{path.name}: wrong matrix")
            if len(ds.PixelData) != metadata.rows * metadata.columns * (metadata.bits_allocated // 8):
                errors.append(f"{path.name}: wrong PixelData length")
            if index < metadata.slices and not np.array_equal(ds.pixel_array, np.asarray(original[index])):
                errors.append(f"{path.name}: pixel mismatch after DICOM round trip")
            positions.append(np.asarray(ds.ImagePositionPatient, dtype=float))
            series_uids.add(str(ds.SeriesInstanceUID))
            study_uids.add(str(ds.StudyInstanceUID))
            sop = str(ds.SOPInstanceUID)
            if sop in sop_uids:
                errors.append(f"{path.name}: duplicate SOP Instance UID")
            sop_uids.add(sop)
        except Exception as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")

    if len(positions) == metadata.slices and len(positions) > 1:
        row_cosine, column_cosine, normal, _ = build_geometry(metadata)
        projected = np.array([np.dot(position, normal) for position in positions])
        differences = np.diff(projected)
        if not np.allclose(differences, metadata.spacing_z, atol=1e-5):
            errors.append(
                f"Non-uniform slice spacing: min={differences.min():.8f}, max={differences.max():.8f}"
            )
    if len(series_uids) != 1:
        errors.append(f"Expected one SeriesInstanceUID, found {len(series_uids)}")
    if len(study_uids) != 1:
        errors.append(f"Expected one StudyInstanceUID, found {len(study_uids)}")

    try:
        nifti = nib.load(nifti_path)
        if tuple(nifti.shape) != (metadata.columns, metadata.rows, metadata.slices):
            errors.append(f"NIfTI shape is {nifti.shape}, expected {(metadata.columns, metadata.rows, metadata.slices)}")
        expected_spacing = np.array([metadata.spacing_x, metadata.spacing_y, metadata.spacing_z])
        actual_spacing = np.array(nifti.header.get_zooms()[:3])
        if not np.allclose(actual_spacing, expected_spacing, atol=1e-5):
            errors.append(f"NIfTI spacing is {actual_spacing.tolist()}, expected {expected_spacing.tolist()}")
    except Exception as exc:
        errors.append(f"NIfTI validation failed: {type(exc).__name__}: {exc}")

    stored_min = int(original.min())
    stored_max = int(original.max())
    del original
    report: dict[str, object] = {
        "application": "Future Case CBCT Recovery",
        "patient_name": metadata.patient_name,
        "patient_id": metadata.patient_id,
        "application_version": APP_VERSION,
        "status": "PASS" if not errors else "FAIL",
        "source_sha256": source_hash,
        "matrix_xyz": [metadata.columns, metadata.rows, metadata.slices],
        "voxel_spacing_mm_xyz": [metadata.spacing_x, metadata.spacing_y, metadata.spacing_z],
        "physical_size_mm_xyz": [
            metadata.columns * metadata.spacing_x,
            metadata.rows * metadata.spacing_y,
            metadata.slices * metadata.spacing_z,
        ],
        "stored_value_range": [stored_min, stored_max],
        "rescaled_value_range": [
            stored_min * metadata.rescale_slope + metadata.rescale_intercept,
            stored_max * metadata.rescale_slope + metadata.rescale_intercept,
        ],
        "dicom_files": len(dicom_paths),
        "transfer_syntax": str(ExplicitVRLittleEndian),
        "study_instance_uid": study_uid,
        "series_instance_uid": series_uid,
        "frame_of_reference_uid": frame_uid,
        "errors": errors,
    }
    (output_dir / "VALIDATION_REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    log("Validation result: " + str(report["status"]))
    if errors:
        for error in errors:
            log("VALIDATION ERROR: " + error)
    return report


def create_packages(output_dir: Path, nifti_path: Path, log: LogFn, progress: ProgressFn) -> tuple[Path, Path]:
    progress(93.0, "Creating DICOM ZIP package")
    dicom_zip = output_dir / "DICOM_IMPORT.zip"
    with zipfile.ZipFile(dicom_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted((output_dir / "DICOM_SERIES").glob("*.dcm")):
            archive.write(path, arcname=f"DICOM_SERIES/{path.name}")
        for name in ("README_IMPORT.txt", "VALIDATION_REPORT.json"):
            path = output_dir / name
            if path.exists():
                archive.write(path, arcname=name)

    progress(96.0, "Creating NIfTI ZIP package")
    nifti_zip = output_dir / "NIFTI_AND_PREVIEWS.zip"
    with zipfile.ZipFile(nifti_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        names = [
            nifti_path.name,
            "README_IMPORT.txt",
            "VALIDATION_REPORT.json",
            "metadata_header.xml",
            "axial_mid.png",
            "coronal_mid.png",
            "sagittal_mid.png",
            "axial_montage.png",
            "slice_stats.csv",
        ]
        for name in names:
            path = output_dir / name
            if path.exists():
                archive.write(path, arcname=name)
    log(f"Created packages: {dicom_zip.name} and {nifti_zip.name}")
    return dicom_zip, nifti_zip


def write_readme(output_dir: Path, metadata: InvMetadata, nifti_path: Path, report: dict[str, object]) -> None:
    text = f"""RECOVERED CBCT
===============

Validation: {report['status']}
Matrix: {metadata.columns} x {metadata.rows} x {metadata.slices}
Voxel spacing: {metadata.spacing_x:g} x {metadata.spacing_y:g} x {metadata.spacing_z:g} mm
Physical field: {metadata.columns * metadata.spacing_x:g} x {metadata.rows * metadata.spacing_y:g} x {metadata.slices * metadata.spacing_z:g} mm
DICOM transfer syntax: Explicit VR Little Endian, uncompressed
DICOM type: classic single-frame CT, one file per slice
Rescale: output = stored value * {metadata.rescale_slope:g} + {metadata.rescale_intercept:g}

IMPORT
------
coDiagnostiX / RealGUIDE / Mimics:
1. Extract DICOM_IMPORT.zip.
2. Import the complete DICOM_SERIES folder as a CT/CBCT study.
3. Do not import only one DICOM file.

3D Slicer:
- Drag {nifti_path.name} into Slicer, or use the DICOM module to import DICOM_SERIES.

NOTES
-----
The source volume was decoded from the original lossless JPEG 2000 components.
The DICOM output is deliberately uncompressed for broad dental software compatibility.
No acquisition date is invented when it is absent from the source metadata.
"""
    (output_dir / "README_IMPORT.txt").write_text(text, encoding="utf-8")


def recover_cbct(
    input_path: str | Path,
    output_dir: str | Path | None = None,
    reverse_z: bool = False,
    create_zip_packages: bool = True,
    log: LogFn = log_noop,
    progress: ProgressFn = progress_noop,
    patient_name: str | None = None,
    patient_id: str | None = None,
) -> dict[str, object]:
    input_path = Path(input_path)
    resolved = resolve_source(input_path, log)
    try:
        source_path = resolved.source_path
        if output_dir is None:
            stem = sanitize_filename(input_path.stem)
            output_path = input_path.resolve().parent / f"{stem}_CBCT_Recovered"
        else:
            output_path = Path(output_dir).expanduser().resolve()
        output_path.mkdir(parents=True, exist_ok=True)

        log(f"Input: {input_path.resolve()}")
        log(f"Output: {output_path}")
        log("Computing source SHA-256...")
        source_hash = sha256_file(source_path)
        work_dir = output_path / "_work"
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)
        progress(1.0, "Parsing INV container")

        metadata, blocks, metadata_path = parse_inv_structure(source_path, work_dir, log)
        if patient_name:
            metadata.patient_name = patient_name
        if patient_id:
            metadata.patient_id = patient_id
        shutil.copy2(metadata_path, output_path / "metadata_header.xml")
        raw_path, stats = decode_volume(source_path, metadata, blocks, work_dir, reverse_z, log, progress)
        case_name = sanitize_filename(metadata.patient_name.replace("^", "_"), sanitize_filename(input_path.stem))

        study_uid, series_uid, frame_uid = create_dicom_series(raw_path, metadata, output_path, log, progress)
        nifti_path = create_nifti(raw_path, metadata, output_path, case_name, log, progress)
        create_previews_and_stats(raw_path, metadata, output_path, stats, log)
        report = validate_outputs(
            raw_path,
            metadata,
            output_path,
            nifti_path,
            study_uid,
            series_uid,
            frame_uid,
            source_hash,
            log,
            progress,
        )
        write_readme(output_path, metadata, nifti_path, report)
        packages: list[str] = []
        if create_zip_packages:
            dicom_zip, nifti_zip = create_packages(output_path, nifti_path, log, progress)
            packages = [str(dicom_zip), str(nifti_zip)]

        shutil.rmtree(work_dir, ignore_errors=True)
        progress(100.0, "Recovery completed")
        result = {
            "status": report["status"],
            "output_directory": str(output_path),
            "dicom_directory": str(output_path / "DICOM_SERIES"),
            "nifti_file": str(nifti_path),
            "packages": packages,
            "validation_report": str(output_path / "VALIDATION_REPORT.json"),
            "errors": report.get("errors", []),
            "matrix_xyz": report["matrix_xyz"],
            "voxel_spacing_mm_xyz": report["voxel_spacing_mm_xyz"],
            "patient_name": metadata.patient_name,
            "patient_id": metadata.patient_id,
        }
        log(json.dumps(result, indent=2))
        return result
    finally:
        resolved.close()


def install_drag_drop_dependency() -> bool:
    """Ensure tkinterdnd2 is available so the GUI supports file dropping."""
    try:
        importlib.import_module("tkinterdnd2")
        return True
    except (ImportError, ModuleNotFoundError, OSError):
        pass

    result = _run_pip_install(["tkinterdnd2"])
    if result.returncode != 0:
        result = _run_pip_install(["tkinterdnd2"], user_install=True)
    if result.returncode != 0:
        return False

    importlib.invalidate_caches()
    try:
        sys.modules.pop("tkinterdnd2", None)
        importlib.import_module("tkinterdnd2")
        return True
    except (ImportError, ModuleNotFoundError, OSError):
        return False


def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    dnd_available = install_drag_drop_dependency()
    if dnd_available:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
        DND_FILES = None

    root.title("Future Case CBCT Recovery")
    root.geometry("920x700")
    root.minsize(760, 580)

    events: queue.Queue[tuple[str, object]] = queue.Queue()
    input_var = tk.StringVar()
    output_var = tk.StringVar()
    reverse_var = tk.BooleanVar(value=False)
    zip_var = tk.BooleanVar(value=True)
    progress_var = tk.DoubleVar(value=0.0)
    status_var = tk.StringVar(value="Ready")

    outer = ttk.Frame(root, padding=14)
    outer.pack(fill="both", expand=True)

    ttk.Label(outer, text="Future Case CBCT Recovery", font=("Segoe UI", 18, "bold")).pack(anchor="w")
    ttk.Label(
        outer,
        text=(
            "Drop a Future/Anatomage .inv, wrapper .exe, or .zip. "
            "Creates classic CT DICOM for coDiagnostiX, RealGUIDE, and Mimics, plus NIfTI for 3D Slicer."
        ),
        wraplength=860,
    ).pack(anchor="w", pady=(3, 12))

    drop_frame = tk.Frame(outer, height=145, relief="groove", borderwidth=2, background="#f2f2f2")
    drop_frame.pack(fill="x")
    drop_frame.pack_propagate(False)
    drop_label = tk.Label(
        drop_frame,
        text="Drop .inv / .exe / .zip here" if dnd_available else "Drag and drop requires tkinterdnd2. Use Browse File below.",
        font=("Segoe UI", 15, "bold"),
        background="#f2f2f2",
    )
    drop_label.pack(expand=True)

    fields = ttk.Frame(outer)
    fields.pack(fill="x", pady=(12, 6))
    fields.columnconfigure(1, weight=1)

    ttk.Label(fields, text="Input file:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(fields, textvariable=input_var).grid(row=0, column=1, sticky="ew", pady=4)
    ttk.Button(fields, text="Browse File", command=lambda: browse_input()).grid(row=0, column=2, padx=(8, 0), pady=4)

    ttk.Label(fields, text="Output folder:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(fields, textvariable=output_var).grid(row=1, column=1, sticky="ew", pady=4)
    ttk.Button(fields, text="Browse Folder", command=lambda: browse_output()).grid(row=1, column=2, padx=(8, 0), pady=4)

    options = ttk.Frame(outer)
    options.pack(fill="x", pady=(2, 8))
    ttk.Checkbutton(options, text="Create ZIP packages", variable=zip_var).pack(side="left")
    ttk.Checkbutton(
        options,
        text="Reverse slice order (only use if anatomy imports upside down)",
        variable=reverse_var,
    ).pack(side="left", padx=(18, 0))

    action_frame = ttk.Frame(outer)
    action_frame.pack(fill="x", pady=(0, 8))
    run_button = ttk.Button(action_frame, text="Recover CBCT", command=lambda: start_recovery())
    run_button.pack(side="left")
    ttk.Button(action_frame, text="Open Output", command=lambda: open_output()).pack(side="left", padx=(8, 0))
    ttk.Label(action_frame, textvariable=status_var).pack(side="right")

    ttk.Progressbar(outer, variable=progress_var, maximum=100.0).pack(fill="x", pady=(0, 8))
    log_text = tk.Text(outer, height=20, wrap="word", font=("Consolas", 9))
    log_text.pack(fill="both", expand=True)
    scrollbar = ttk.Scrollbar(log_text, orient="vertical", command=log_text.yview)
    scrollbar.pack(side="right", fill="y")
    log_text.configure(yscrollcommand=scrollbar.set)

    def append_log(message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_text.insert("end", f"[{timestamp}] {message}\n")
        log_text.see("end")

    def set_input(path_text: str) -> None:
        path = Path(path_text.strip().strip('"'))
        input_var.set(str(path))
        if path.exists():
            output_var.set(str(path.parent / f"{sanitize_filename(path.stem)}_CBCT_Recovered"))

    def browse_input() -> None:
        selected = filedialog.askopenfilename(
            title="Select INV, EXE, or ZIP",
            filetypes=[
                ("Supported files", "*.inv *.exe *.zip"),
                ("INV files", "*.inv"),
                ("EXE wrappers", "*.exe"),
                ("ZIP archives", "*.zip"),
                ("All files", "*.*"),
            ],
        )
        if selected:
            set_input(selected)

    def browse_output() -> None:
        selected = filedialog.askdirectory(title="Select output folder")
        if selected:
            output_var.set(selected)

    def open_output() -> None:
        path = Path(output_var.get())
        if not path.exists():
            messagebox.showinfo("Output", "The output folder does not exist yet.")
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Open Output", str(exc))

    def on_drop(event: object) -> None:
        try:
            paths = root.tk.splitlist(event.data)  # type: ignore[attr-defined]
            if paths:
                set_input(paths[0])
        except Exception as exc:
            messagebox.showerror("Drop Error", str(exc))

    if dnd_available:
        drop_frame.drop_target_register(DND_FILES)
        drop_frame.dnd_bind("<<Drop>>", on_drop)
        drop_label.drop_target_register(DND_FILES)
        drop_label.dnd_bind("<<Drop>>", on_drop)
    else:
        append_log("Drag and drop package is missing. Install it with:")
        append_log(f'"{sys.executable}" -m pip install -U tkinterdnd2')

    def worker() -> None:
        try:
            result = recover_cbct(
                input_var.get(),
                output_var.get() or None,
                reverse_z=reverse_var.get(),
                create_zip_packages=zip_var.get(),
                log=lambda message: events.put(("log", message)),
                progress=lambda value, message: events.put(("progress", (value, message))),
            )
            events.put(("done", result))
        except Exception as exc:
            events.put(("error", (str(exc), traceback.format_exc())))

    def start_recovery() -> None:
        input_path = Path(input_var.get().strip().strip('"'))
        if not input_path.exists():
            messagebox.showerror("Input", "Select a valid input file first.")
            return
        if not output_var.get().strip():
            output_var.set(str(input_path.parent / f"{sanitize_filename(input_path.stem)}_CBCT_Recovered"))
        log_text.delete("1.0", "end")
        progress_var.set(0.0)
        status_var.set("Working...")
        run_button.configure(state="disabled")
        threading.Thread(target=worker, daemon=True).start()

    def poll_events() -> None:
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "log":
                    append_log(str(payload))
                elif kind == "progress":
                    value, message = payload  # type: ignore[misc]
                    progress_var.set(float(value))
                    status_var.set(str(message))
                elif kind == "done":
                    result = payload  # type: ignore[assignment]
                    progress_var.set(100.0)
                    status_var.set("Completed")
                    run_button.configure(state="normal")
                    if result.get("status") == "PASS":
                        messagebox.showinfo(
                            "Recovery Completed",
                            "CBCT recovery and validation passed.\n\nOutput:\n" + result["output_directory"],
                        )
                    else:
                        messagebox.showwarning(
                            "Recovery Completed with Warnings",
                            "Files were created, but validation reported errors. Review VALIDATION_REPORT.json.",
                        )
                elif kind == "error":
                    message, details = payload  # type: ignore[misc]
                    append_log(details)
                    status_var.set("Failed")
                    run_button.configure(state="normal")
                    messagebox.showerror("Recovery Failed", message)
        except queue.Empty:
            pass
        root.after(100, poll_events)

    poll_events()
    root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover Future/Anatomage INV CBCT to DICOM and NIfTI.")
    parser.add_argument("input", nargs="?", help="Input .inv, .exe, or .zip")
    parser.add_argument("--output", help="Output directory")
    parser.add_argument("--reverse-z", action="store_true", help="Reverse decoded slice order")
    parser.add_argument("--no-zip", action="store_true", help="Do not create ZIP packages")
    parser.add_argument("--gui", action="store_true", help="Force GUI mode")
    args = parser.parse_args()

    if args.gui or not args.input:
        run_gui()
        return 0

    def cli_log(message: str) -> None:
        print(message, flush=True)

    def cli_progress(value: float, message: str) -> None:
        print(f"[{value:6.2f}%] {message}", flush=True)

    try:
        result = recover_cbct(
            args.input,
            args.output,
            reverse_z=args.reverse_z,
            create_zip_packages=not args.no_zip,
            log=cli_log,
            progress=cli_progress,
        )
        return 0 if result.get("status") == "PASS" else 2
    except Exception as exc:
        print("ERROR:", exc, file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
