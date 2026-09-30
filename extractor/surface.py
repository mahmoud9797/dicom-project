"""Make a bounded 3D surface from the same pixel volume used by the MPR viewer."""
import math
import struct

import numpy as np


MAX_AXIS_SAMPLES = 144
MAX_TRIANGLES = 300_000


def threshold_geometry(mask_file, shape, spacing, preset, superior_last, smoothing=0.8):
    """Return a smoothed physical surface reconstructed from a binary mask."""
    from scipy.ndimage import gaussian_filter, generate_binary_structure, label
    from skimage.measure import marching_cubes

    mask_volume = np.memmap(mask_file, dtype='u1', mode='r', shape=shape)
    ordered = mask_volume[::-1] if superior_last else mask_volume
    stride = [max(1, math.ceil(length / MAX_AXIS_SAMPLES)) for length in shape]
    scan_extent = np.maximum((np.asarray(shape, dtype=np.float32) - 1) * spacing, 1e-6)
    try:
        while True:
            mask = np.asarray(ordered[::stride[0], ::stride[1], ::stride[2]], dtype=bool)
            if preset == 'maxillofacial' and mask.any():
                # Remove isolated threshold specks while retaining separate
                # anatomical pieces such as the upper and lower jaws.
                components, _ = label(mask, structure=generate_binary_structure(3, 3))
                sizes = np.bincount(components.ravel())
                keep = sizes >= 4
                keep[0] = False
                mask = keep[components]
            if not mask.any():
                return (np.empty((0, 3), dtype=np.float32),
                        np.empty((0, 3), dtype=np.float32),
                        np.empty((0, 3), dtype=np.int32), tuple(stride), scan_extent)
            mask_int = mask.astype(np.int8)
            transitions = sum(np.count_nonzero(np.diff(mask_int, axis=axis)) for axis in range(3))
            if transitions > 350_000 and max(mask.shape) > 16:
                stride = [step + 1 for step in stride]
                continue
            padded = np.pad(mask.astype(np.float32), 1)
            if smoothing:
                smoothed = gaussian_filter(padded, sigma=smoothing, mode='constant')
                # Preserve very small valid structures if blurring would make
                # the entire selected surface disappear.
                if smoothed.max() > 0.5:
                    padded = smoothed
            vertices, faces, normals, _ = marching_cubes(
                padded, level=0.5, allow_degenerate=False,
            )
            if len(faces) <= MAX_TRIANGLES or max(mask.shape) <= 16:
                break
            stride = [step + 1 for step in stride]

        # marching_cubes coordinates are z, y, x. Store centered, normalized
        # x, y, z coordinates for the small WebGL renderer in the browser.
        physical = np.asarray(spacing, dtype=np.float32) * stride
        extent = scan_extent
        vertices = np.clip((vertices - 1) * physical, 0, extent)
        positions = vertices[:, [2, 1, 0]].astype(np.float32)
        vertex_normals = normals[:, [2, 1, 0]] / physical[[2, 1, 0]]
        lengths = np.linalg.norm(vertex_normals, axis=1, keepdims=True)
        vertex_normals = np.divide(vertex_normals, lengths, out=np.zeros_like(vertex_normals), where=lengths > 0)
        return positions, vertex_normals, faces, tuple(stride), extent
    finally:
        del mask_volume


def render_buffer(positions, normals, faces, extent):
    """Interleaved normalized XYZ/normal float32 triangles for WebGL."""
    if not len(faces):
        return b''
    scale = float(max(extent))
    centered = (positions - extent[[2, 1, 0]] / 2) / scale
    mesh = np.empty((len(faces) * 3, 6), dtype='<f4')
    mesh[:, :3] = centered[faces].reshape(-1, 3)
    mesh[:, 3:] = normals[faces].reshape(-1, 3)
    return mesh.tobytes()


def binary_stl(positions, faces):
    """Binary STL with vertex positions in millimetres."""
    if not len(faces):
        return b''
    triangles = positions[faces].astype('<f4', copy=False)
    vectors = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    lengths = np.linalg.norm(vectors, axis=1)
    normals = np.divide(vectors, lengths[:, None], out=np.zeros_like(vectors), where=lengths[:, None] > 0)
    records = np.empty(len(faces), dtype=[('normal', '<f4', (3,)),
                                          ('vertices', '<f4', (3, 3)),
                                          ('attribute', '<u2')])
    records['normal'] = normals
    records['vertices'] = triangles
    records['attribute'] = 0
    header = b'DicomExtractor threshold surface; units: mm'.ljust(80, b' ')
    return header + struct.pack('<I', len(faces)) + records.tobytes()


def wavefront_obj(positions, faces):
    """OBJ mesh with vertex coordinates in millimetres."""
    lines = ['# DicomExtractor binary mask surface\n', '# Coordinates in millimetres\n']
    lines.extend(f'v {x:.6f} {y:.6f} {z:.6f}\n' for x, y, z in positions)
    lines.extend(f'f {a + 1} {b + 1} {c + 1}\n' for a, b, c in faces)
    return ''.join(lines).encode('ascii')
