"""Authenticated, on-demand viewing of the extracted DICOM series."""
import math
from pathlib import Path

import numpy as np
import pydicom
from pydicom.multival import MultiValue
from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import ExtractionJob, FutureCaseJob, RealGuideJob
from .masks import ensure_binary_mask, mask_plane_bytes, nrrd_chunks, nrrd_header
from .mpr import ensure_volume_cache, mpr_plane_bytes
from .surface import binary_stl, render_buffer, threshold_geometry, wavefront_obj


KINDS = {
    'co-diagnostix': (ExtractionJob, 'coDiagnostiX'),
    'real-guide': (RealGuideJob, 'RealGUIDE'),
    'future-case': (FutureCaseJob, 'Future Case'),
}

TOP_TAGS = (
    ('Patient name', '0010,0010', 'PatientName'),
    ('Patient ID', '0010,0020', 'PatientID'),
    ('Sex (DICOM)', '0010,0040', 'PatientSex'),
    ('Study date', '0008,0020', 'StudyDate'),
    ('Series date', '0008,0021', 'SeriesDate'),
    ('Modality', '0008,0060', 'Modality'),
    ('Series description', '0008,103E', 'SeriesDescription'),
    ('Rows', '0028,0010', 'Rows'),
    ('Columns', '0028,0011', 'Columns'),
    ('Pixel spacing', '0028,0030', 'PixelSpacing'),
    ('Slice thickness', '0018,0050', 'SliceThickness'),
)


def _series(kind, job_id):
    if kind not in KINDS:
        raise Http404('Unknown extractor.')
    model, label = KINDS[kind]
    job = get_object_or_404(model, id=job_id)
    if job.status != 'completed' or not job.matrix_slices or job.matrix_slices < 1:
        raise Http404('The DICOM series is not ready.')

    root = Path(settings.EXTRACTION_OUTPUT_DIR).resolve()
    if kind == 'future-case':
        folder = root / 'future_case' / str(job.id) / 'DICOM_SERIES'
        filename = lambda index: f'IM_{index + 1:04d}.dcm'
    elif kind == 'real-guide':
        folder = root / job.output_dir_name / 'DICOM'
        filename = lambda index: f'CT_{index + 1:05d}.dcm'
    else:
        folder = root / job.output_dir_name
        filename = lambda index: f'IMG{index + 1:04d}'

    if kind != 'future-case' and not job.output_dir_name:
        raise Http404('The DICOM series is missing.')
    folder = folder.resolve()
    try:
        folder.relative_to(root)
    except ValueError as exc:
        raise Http404('Invalid DICOM series path.') from exc
    return job, label, folder, filename


def _slice_path(folder, filename, index, count):
    if index < 0 or index >= count:
        raise Http404('Slice number is outside the series.')
    path = folder / filename(index)
    if not path.is_file():
        raise Http404('DICOM slice is missing.')
    return path


def _tag_text(dataset, keyword):
    value = dataset.get(keyword, '')
    if keyword == 'PixelSpacing' and value:
        return ' × '.join(str(item) for item in value) + ' mm'
    if keyword == 'SliceThickness' and value:
        return f'{value} mm'
    if keyword in ('StudyDate', 'SeriesDate') and len(str(value)) == 8:
        raw = str(value)
        return f'{raw[:4]}-{raw[4:6]}-{raw[6:]}'
    return str(value).strip() or '—'


def _first_number(value):
    if value is None or value == '':
        return None
    try:
        return float(value[0] if isinstance(value, (list, tuple, MultiValue)) else value)
    except (TypeError, ValueError):
        return None


def _superior_last(first, last):
    """Use patient-space Z to put the superior end of the stack at the top."""
    try:
        first_z = float(first.ImagePositionPatient[2])
        last_z = float(last.ImagePositionPatient[2])
        return last_z >= first_z
    except (AttributeError, IndexError, TypeError, ValueError):
        return True


@never_cache
@require_GET
def viewer_list(request):
    """Show completed scans that can be opened from the top navigation."""
    scans = []
    for kind, (model, label) in KINDS.items():
        scans.extend(
            (job, kind, label)
            for job in model.objects.filter(status='completed')[:100]
        )
    scans.sort(key=lambda entry: entry[0].created_at, reverse=True)
    return render(request, 'extractor/viewer_list.html', {'scans': scans[:100]})


@never_cache
@require_GET
def dicom_viewer(request, kind, job_id):
    job, label, folder, filename = _series(kind, job_id)
    try:
        dataset = pydicom.dcmread(
            _slice_path(folder, filename, 0, job.matrix_slices),
            stop_before_pixels=True,
        )
        last_dataset = pydicom.dcmread(
            _slice_path(folder, filename, job.matrix_slices - 1, job.matrix_slices),
            stop_before_pixels=True,
        )
        rows, columns = int(dataset.Rows), int(dataset.Columns)
        if rows < 1 or columns < 1 or rows * columns > 16_777_216:
            raise ValueError('Unsupported DICOM image dimensions.')
        if int(dataset.BitsAllocated) not in (8, 16) or int(dataset.SamplesPerPixel) != 1:
            raise ValueError('This viewer supports single-channel, 8- or 16-bit DICOM slices.')
    except (OSError, AttributeError, ValueError, pydicom.errors.InvalidDicomError) as exc:
        raise Http404('The extracted DICOM series cannot be opened.') from exc

    metadata = [(label, tag, _tag_text(dataset, keyword)) for label, tag, keyword in TOP_TAGS]
    summary = (
        ('Patient name', _tag_text(dataset, 'PatientName')),
        ('Sex (DICOM)', _tag_text(dataset, 'PatientSex')),
        ('Study date', _tag_text(dataset, 'StudyDate')),
        ('Series date', _tag_text(dataset, 'SeriesDate')),
    )
    pixel_spacing = dataset.get('PixelSpacing', [])
    spacing_y = _first_number(pixel_spacing[0]) if len(pixel_spacing) > 0 else None
    spacing_x = _first_number(pixel_spacing[1]) if len(pixel_spacing) > 1 else None
    spacing_z = (
        _first_number(dataset.get('SpacingBetweenSlices'))
        or job.spacing_z
        or _first_number(dataset.get('SliceThickness'))
        or 1
    )
    spacing_x = spacing_x if spacing_x and spacing_x > 0 else 1
    spacing_y = spacing_y if spacing_y and spacing_y > 0 else 1
    spacing_z = spacing_z if spacing_z > 0 else 1
    config = {
        'superiorLast': _superior_last(dataset, last_dataset),
        'surfaceUrl': reverse('viewer_surface', args=[kind, job.id]),
        'stlUrl': reverse('viewer_surface_stl', args=[kind, job.id]),
        'objUrl': reverse('viewer_surface_obj', args=[kind, job.id]),
        'maskUrl': reverse('viewer_mask', args=[kind, job.id]),
        'maskPlanes': {
            plane: reverse('viewer_mask_slice', args=[kind, job.id, plane, 0])[:-2]
            for plane in ('axial', 'coronal', 'sagittal')
        },
        'planes': {
            'axial': {
                'urlBase': reverse('viewer_slice', args=[kind, job.id, 0])[:-2],
                'rows': rows, 'columns': columns, 'count': job.matrix_slices,
                'bytesPerPixel': int(dataset.BitsAllocated) // 8,
                'rowSpacing': spacing_y, 'columnSpacing': spacing_x,
            },
            'coronal': {
                'urlBase': reverse('viewer_mpr_slice', args=[kind, job.id, 'coronal', 0])[:-2],
                'rows': job.matrix_slices, 'columns': columns, 'count': rows,
                'bytesPerPixel': 2,
                'rowSpacing': spacing_z, 'columnSpacing': spacing_x,
            },
            'sagittal': {
                'urlBase': reverse('viewer_mpr_slice', args=[kind, job.id, 'sagittal', 0])[:-2],
                'rows': job.matrix_slices, 'columns': rows, 'count': columns,
                'bytesPerPixel': 2,
                'rowSpacing': spacing_z, 'columnSpacing': spacing_y,
            },
        },
        'signed': bool(int(dataset.get('PixelRepresentation', 0))),
        'photometric': str(dataset.get('PhotometricInterpretation', 'MONOCHROME2')),
        'slope': _first_number(dataset.get('RescaleSlope')) or 1,
        'intercept': _first_number(dataset.get('RescaleIntercept')) or 0,
        'windowCenter': _first_number(dataset.get('WindowCenter')),
        'windowWidth': _first_number(dataset.get('WindowWidth')),
        'rescaleType': str(dataset.get('RescaleType', '')).strip(),
    }
    detail_names = {
        'co-diagnostix': 'job_detail',
        'real-guide': 'real_guide_job_detail',
        'future-case': 'future_case_job_detail',
    }
    return render(request, 'extractor/viewer.html', {
        'job': job,
        'extractor_label': label,
        'kind': kind,
        'metadata': metadata,
        'summary': summary,
        'plane_labels': (('axial', 'Axial'), ('coronal', 'Coronal'), ('sagittal', 'Sagittal')),
        'viewer_config': config,
        'detail_url': reverse(detail_names[kind], args=[job.id]) + '?details=1',
    })


@never_cache
@require_GET
def viewer_slice(request, kind, job_id, index):
    job, _, folder, filename = _series(kind, job_id)
    try:
        dataset = pydicom.dcmread(_slice_path(folder, filename, index, job.matrix_slices))
        if int(dataset.BitsAllocated) not in (8, 16) or int(dataset.SamplesPerPixel) != 1:
            raise ValueError('Unsupported pixel format.')
        pixels = dataset.pixel_array
        if pixels.ndim != 2 or pixels.size > 16_777_216:
            raise ValueError('Unsupported pixel array.')
        if int(dataset.BitsAllocated) == 8:
            dtype = 'i1' if int(dataset.PixelRepresentation) else 'u1'
        else:
            dtype = '<i2' if int(dataset.PixelRepresentation) else '<u2'
        pixel_bytes = np.ascontiguousarray(pixels, dtype=dtype).tobytes()
    except (OSError, AttributeError, ValueError, pydicom.errors.InvalidDicomError) as exc:
        raise Http404('DICOM slice cannot be decoded.') from exc
    response = HttpResponse(pixel_bytes, content_type='application/octet-stream')
    response['X-Image-Rows'] = str(pixels.shape[0])
    response['X-Image-Columns'] = str(pixels.shape[1])
    return response


@never_cache
@require_GET
def viewer_mpr_slice(request, kind, job_id, plane, index):
    if plane not in ('coronal', 'sagittal'):
        raise Http404('Unknown view plane.')
    job, _, folder, filename = _series(kind, job_id)
    try:
        first_slice = pydicom.dcmread(
            _slice_path(folder, filename, 0, job.matrix_slices),
            stop_before_pixels=True,
        )
        last_slice = pydicom.dcmread(
            _slice_path(folder, filename, job.matrix_slices - 1, job.matrix_slices),
            stop_before_pixels=True,
        )
        rows, columns = int(first_slice.Rows), int(first_slice.Columns)
        signed = bool(int(first_slice.PixelRepresentation))
        if rows < 1 or columns < 1 or rows * columns > 16_777_216:
            raise ValueError('Unsupported DICOM dimensions.')
        bits_allocated = int(first_slice.BitsAllocated)
        if bits_allocated not in (8, 16) or int(first_slice.SamplesPerPixel) != 1:
            raise ValueError('Unsupported DICOM pixel format.')
    except (OSError, AttributeError, ValueError, pydicom.errors.InvalidDicomError) as exc:
        raise Http404('The extracted DICOM series cannot be opened.') from exc
    if index >= (rows if plane == 'coronal' else columns):
        raise Http404('Plane number is outside the series.')
    try:
        cache_file = ensure_volume_cache(
            Path(settings.EXTRACTION_OUTPUT_DIR).resolve(), kind, job.id,
            folder, filename, job.matrix_slices, rows, columns, signed, bits_allocated,
        )
        pixel_bytes, plane_rows, plane_columns = mpr_plane_bytes(
            cache_file, plane, index, job.matrix_slices, rows, columns, signed,
            _superior_last(first_slice, last_slice),
        )
    except (OSError, ValueError, pydicom.errors.InvalidDicomError):
        return HttpResponse('Unable to prepare this view from the extracted DICOM series.', status=503)
    response = HttpResponse(pixel_bytes, content_type='application/octet-stream')
    response['X-Image-Rows'] = str(plane_rows)
    response['X-Image-Columns'] = str(plane_columns)
    return response


@never_cache
@require_GET
def viewer_surface(request, kind, job_id):
    """Stream a decimated binary-mask surface for the WebGL viewer."""
    return _threshold_response(request, kind, job_id, format='mesh')


@never_cache
@require_GET
def viewer_surface_stl(request, kind, job_id):
    """Export the current binary-mask surface as STL in millimetres."""
    return _threshold_response(request, kind, job_id, format='stl')


@never_cache
@require_GET
def viewer_surface_obj(request, kind, job_id):
    """Export the current binary-mask surface as OBJ in millimetres."""
    return _threshold_response(request, kind, job_id, format='obj')


def _threshold_options(request):
    try:
        minimum = float(request.GET['minimum'])
        maximum = float(request.GET['maximum'])
        if (not math.isfinite(minimum) or not math.isfinite(maximum)
                or minimum >= maximum or minimum < -100_000 or maximum > 100_000):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError('Enter a valid threshold range.')
    try:
        smoothing = float(request.GET.get('smoothing', '0.8'))
        if not math.isfinite(smoothing) or not 0 <= smoothing <= 2:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError('Enter a valid surface smoothing level.')
    preset = request.GET.get('preset', 'custom')
    if preset not in ('maxillofacial', 'bone', 'dental', 'air', 'soft', 'custom'):
        raise ValueError('Unknown threshold preset.')
    return minimum, maximum, preset, smoothing


def _mask_context(kind, job_id, minimum, maximum, preset):
    job, _, folder, filename = _series(kind, job_id)
    first = pydicom.dcmread(_slice_path(folder, filename, 0, job.matrix_slices), stop_before_pixels=True)
    last = pydicom.dcmread(_slice_path(folder, filename, job.matrix_slices - 1, job.matrix_slices), stop_before_pixels=True)
    rows, columns = int(first.Rows), int(first.Columns)
    bits = int(first.BitsAllocated)
    signed = bool(int(first.PixelRepresentation))
    if (rows < 1 or columns < 1 or rows * columns > 16_777_216
            or bits not in (8, 16) or int(first.SamplesPerPixel) != 1):
        raise ValueError('Unsupported DICOM pixel format.')
    pixel_spacing = first.get('PixelSpacing', [])
    spacing_y = _first_number(pixel_spacing[0]) if len(pixel_spacing) > 0 else None
    spacing_x = _first_number(pixel_spacing[1]) if len(pixel_spacing) > 1 else None
    spacing_z = (_first_number(first.get('SpacingBetweenSlices'))
                 or job.spacing_z or _first_number(first.get('SliceThickness')) or 1)
    spacing = tuple(value if value and value > 0 else 1 for value in (spacing_z, spacing_y, spacing_x))
    output_root = Path(settings.EXTRACTION_OUTPUT_DIR).resolve()
    cache_file = ensure_volume_cache(
        output_root, kind, job.id, folder, filename,
        job.matrix_slices, rows, columns, signed, bits,
    )
    effective_preset = (preset if str(first.get('RescaleType', '')).strip() == 'HU'
                        or preset == 'maxillofacial' else 'custom')
    shape = (job.matrix_slices, rows, columns)
    mask_file, count = ensure_binary_mask(
        output_root, kind, job.id, cache_file, shape, signed,
        _first_number(first.get('RescaleSlope')) or 1,
        _first_number(first.get('RescaleIntercept')) or 0,
        minimum, maximum, effective_preset,
    )
    return job, mask_file, count, shape, spacing, effective_preset, _superior_last(first, last)


@never_cache
@require_GET
def viewer_mask(request, kind, job_id):
    """Calculate a full-resolution binary mask or export it as NRRD."""
    try:
        minimum, maximum, preset, _ = _threshold_options(request)
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)
    try:
        job, mask_file, count, shape, spacing, _, _ = _mask_context(
            kind, job_id, minimum, maximum, preset,
        )
    except (OSError, ValueError, RuntimeError, ImportError, pydicom.errors.InvalidDicomError):
        return HttpResponse('Unable to calculate a binary mask from this series.', status=503)
    if request.GET.get('download') == '1':
        header = nrrd_header(shape, spacing)
        response = StreamingHttpResponse(
            nrrd_chunks(mask_file, header), content_type='application/octet-stream',
        )
        response['Content-Length'] = str(len(header) + mask_file.stat().st_size)
        response['Content-Disposition'] = f'attachment; filename="{kind}-{job.id}-binary-mask.nrrd"'
        return response
    return JsonResponse({
        'selectedVoxels': count,
        'totalVoxels': math.prod(shape),
        'dimensionsXYZ': [shape[2], shape[1], shape[0]],
        'spacingXYZ': [spacing[2], spacing[1], spacing[0]],
    })


@never_cache
@require_GET
def viewer_mask_slice(request, kind, job_id, plane, index):
    """Serve the exact binary mask plane aligned with the CT view."""
    if plane not in ('axial', 'coronal', 'sagittal'):
        raise Http404('Unknown mask plane.')
    try:
        minimum, maximum, preset, _ = _threshold_options(request)
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)
    try:
        _, mask_file, _, shape, _, _, superior_last = _mask_context(
            kind, job_id, minimum, maximum, preset,
        )
        pixels = mask_plane_bytes(mask_file, shape, plane, index, superior_last)
    except IndexError:
        raise Http404('Mask plane is outside the series.')
    except (OSError, ValueError, RuntimeError, ImportError, pydicom.errors.InvalidDicomError):
        return HttpResponse('Unable to read this mask plane.', status=503)
    return HttpResponse(pixels, content_type='application/octet-stream')


def _threshold_response(request, kind, job_id, format):
    try:
        minimum, maximum, preset, smoothing = _threshold_options(request)
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)
    try:
        job, mask_file, _, shape, spacing, effective_preset, superior_last = _mask_context(
            kind, job_id, minimum, maximum, preset,
        )
        positions, normals, faces, stride, extent = threshold_geometry(
            mask_file, shape, spacing, effective_preset, superior_last, smoothing,
        )
    except (OSError, ValueError, RuntimeError, ImportError, pydicom.errors.InvalidDicomError):
        return HttpResponse('Unable to build a 3D surface from this mask.', status=503)
    if format != 'mesh' and not len(faces):
        return HttpResponse('No surface matches this mask.', status=422)
    if format == 'stl':
        response = HttpResponse(binary_stl(positions, faces), content_type='model/stl')
        response['Content-Disposition'] = f'attachment; filename="{kind}-{job.id}-mask-mm.stl"'
    elif format == 'obj':
        response = HttpResponse(wavefront_obj(positions, faces), content_type='text/plain; charset=ascii')
        response['Content-Disposition'] = f'attachment; filename="{kind}-{job.id}-mask-mm.obj"'
    else:
        mesh = render_buffer(positions, normals, faces, extent)
        response = HttpResponse(mesh, content_type='application/octet-stream')
        response['X-Mesh-Vertices'] = str(len(mesh) // 24)
    response['X-Volume-Step'] = ','.join(str(value) for value in stride)
    return response
