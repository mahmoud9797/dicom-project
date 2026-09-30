"""Build a file-backed volume once, then read coronal and sagittal planes."""
import os
import shutil
import tempfile
import threading
from pathlib import Path

import numpy as np
import pydicom


_build_lock = threading.Lock()


def _cache_file(output_root: Path, kind: str, job_id) -> Path:
    root = output_root.resolve()
    path = (root / '.viewer_cache' / kind / str(job_id) / 'volume.raw').resolve()
    path.relative_to(root)
    return path


def _cache_is_current(cache_file: Path, first_slice: Path, last_slice: Path, byte_count: int) -> bool:
    if not cache_file.is_file():
        return False
    cache_stat = cache_file.stat()
    return (
        cache_stat.st_size == byte_count
        and cache_stat.st_mtime_ns >= first_slice.stat().st_mtime_ns
        and cache_stat.st_mtime_ns >= last_slice.stat().st_mtime_ns
    )


def ensure_volume_cache(
    output_root: Path, kind: str, job_id, folder: Path, filename,
    count: int, rows: int, columns: int, signed: bool, bits_allocated: int,
) -> Path:
    """Create an atomic z,y,x raw volume from validated DICOM output slices."""
    cache_file = _cache_file(output_root, kind, job_id)
    first_slice = folder / filename(0)
    last_slice = folder / filename(count - 1)
    if not first_slice.is_file() or not last_slice.is_file():
        raise ValueError('The DICOM series is incomplete.')
    byte_count = count * rows * columns * 2
    if _cache_is_current(cache_file, first_slice, last_slice, byte_count):
        return cache_file

    with _build_lock:
        if _cache_is_current(cache_file, first_slice, last_slice, byte_count):
            return cache_file
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(cache_file.parent).free < byte_count + 64 * 1024 * 1024:
            raise OSError('Not enough disk space to prepare multiplanar views.')
        descriptor, temporary_name = tempfile.mkstemp(
            prefix='volume-', suffix='.raw', dir=cache_file.parent,
        )
        os.close(descriptor)
        temporary_file = Path(temporary_name)
        dtype = '<i2' if signed else '<u2'
        try:
            volume = np.memmap(temporary_file, dtype=dtype, mode='w+', shape=(count, rows, columns))
            try:
                for index in range(count):
                    source = folder / filename(index)
                    if not source.is_file():
                        raise ValueError(f'DICOM slice {index + 1} is missing.')
                    dataset = pydicom.dcmread(source)
                    if (int(dataset.BitsAllocated) != bits_allocated
                            or int(dataset.PixelRepresentation) != int(signed)
                            or int(dataset.SamplesPerPixel) != 1):
                        raise ValueError('DICOM pixel format changes within this series.')
                    pixels = dataset.pixel_array
                    if pixels.shape != (rows, columns):
                        raise ValueError('DICOM image dimensions change within this series.')
                    volume[index] = pixels
                volume.flush()
            finally:
                del volume
            os.replace(temporary_file, cache_file)
        finally:
            temporary_file.unlink(missing_ok=True)
    return cache_file


def mpr_plane_bytes(
    cache_file: Path, plane: str, index: int,
    count: int, rows: int, columns: int, signed: bool, superior_last: bool,
) -> tuple[bytes, int, int]:
    """Return a 16-bit plane in screen row order, with superior slices on top."""
    dtype = '<i2' if signed else '<u2'
    volume = np.memmap(cache_file, dtype=dtype, mode='r', shape=(count, rows, columns))
    try:
        ordered = volume[::-1] if superior_last else volume
        if plane == 'coronal':
            pixels = np.ascontiguousarray(ordered[:, index, :])
            return pixels.tobytes(), count, columns
        if plane == 'sagittal':
            pixels = np.ascontiguousarray(ordered[:, :, index])
            return pixels.tobytes(), count, rows
        raise ValueError('Unknown multiplanar view.')
    finally:
        del volume
