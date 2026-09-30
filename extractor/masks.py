"""Full-resolution binary threshold masks backed by bounded disk cache."""
import hashlib
import json
import math
import os
import shutil
import tempfile
import threading
from pathlib import Path

import numpy as np


_build_lock = threading.Lock()
MAX_MASKS_PER_JOB = 4


def ensure_binary_mask(output_root: Path, kind: str, job_id, volume_file: Path,
                       shape: tuple[int, int, int], signed: bool,
                       slope: float, intercept: float, minimum: float,
                       maximum: float, preset: str) -> tuple[Path, int]:
    """Return a raw uint8 ZYX mask and its selected voxel count.

    The mask is full resolution. Smoothing is intentionally absent so values
    remain exactly zero or one; it is applied only when making a 3D surface.
    """
    root = output_root.resolve()
    source = volume_file.resolve()
    source.relative_to(root)
    source_stat = source.stat()
    expected = math.prod(shape)
    identity = [str(source), source_stat.st_mtime_ns, source_stat.st_size,
                shape, signed, slope, intercept, minimum, maximum, preset]
    digest = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()[:24]
    folder = (root / '.viewer_masks' / kind / str(job_id)).resolve()
    folder.relative_to(root)
    target = folder / f'{digest}.raw'

    with _build_lock:
        if not target.is_file() or target.stat().st_size != expected:
            folder.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(folder).free < expected + 64 * 1024 * 1024:
                raise OSError('Not enough disk space to calculate a binary mask.')
            descriptor, temporary_name = tempfile.mkstemp(prefix='mask-', suffix='.raw', dir=folder)
            os.close(descriptor)
            temporary = Path(temporary_name)
            dtype = '<i2' if signed else '<u2'
            volume = np.memmap(source, dtype=dtype, mode='r', shape=shape)
            try:
                mask = np.memmap(temporary, dtype='u1', mode='w+', shape=shape)
                try:
                    for start in range(0, shape[0], 16):
                        stop = min(start + 16, shape[0])
                        values = np.asarray(volume[start:stop], dtype=np.float32)
                        values = values * slope + intercept
                        selected = (values >= minimum) & (values <= maximum)
                        if preset == 'air':
                            from scipy.ndimage import binary_fill_holes
                            for z in range(stop - start):
                                selected[z] &= binary_fill_holes(values[z] > -350)
                        mask[start:stop] = selected
                    mask.flush()
                finally:
                    del mask
                os.replace(temporary, target)
            finally:
                del volume
                temporary.unlink(missing_ok=True)

            # Limit per-scan cache growth when thresholds are adjusted often.
            cached = sorted(folder.glob('*.raw'), key=lambda path: path.stat().st_mtime_ns, reverse=True)
            for stale in cached[MAX_MASKS_PER_JOB:]:
                try:
                    stale.unlink(missing_ok=True)
                except OSError:
                    # A download may still have this mask open on Windows.
                    pass

        mask = np.memmap(target, dtype='u1', mode='r', shape=shape)
        try:
            count = int(np.count_nonzero(mask))
        finally:
            del mask
    return target, count


def nrrd_header(shape: tuple[int, int, int], spacing: tuple[float, float, float]) -> bytes:
    """Attached NRRD header with spacing only; patient orientation is unknown."""
    z, y, x = shape
    sz, sy, sx = spacing
    return (
        'NRRD0005\n'
        '# Binary threshold mask: 0=outside, 1=inside. Orientation not verified.\n'
        'type: uchar\n'
        'dimension: 3\n'
        f'sizes: {x} {y} {z}\n'
        f'spacings: {sx:.9g} {sy:.9g} {sz:.9g}\n'
        'kinds: domain domain domain\n'
        'encoding: raw\n\n'
    ).encode('ascii')


def nrrd_chunks(mask_file: Path, header: bytes):
    yield header
    with mask_file.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            yield chunk


def mask_plane_bytes(mask_file: Path, shape: tuple[int, int, int],
                     plane: str, index: int, superior_last: bool) -> bytes:
    """Return one uint8 mask plane in the same order as the CT viewer."""
    volume = np.memmap(mask_file, dtype='u1', mode='r', shape=shape)
    try:
        if plane == 'axial':
            if not 0 <= index < shape[0]:
                raise IndexError('Mask slice is outside the volume.')
            return np.ascontiguousarray(volume[index]).tobytes()
        ordered = volume[::-1] if superior_last else volume
        if plane == 'coronal':
            if not 0 <= index < shape[1]:
                raise IndexError('Mask plane is outside the volume.')
            return np.ascontiguousarray(ordered[:, index, :]).tobytes()
        if plane == 'sagittal':
            if not 0 <= index < shape[2]:
                raise IndexError('Mask plane is outside the volume.')
            return np.ascontiguousarray(ordered[:, :, index]).tobytes()
        raise ValueError('Unknown mask plane.')
    finally:
        del volume
