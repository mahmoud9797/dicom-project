"""
Background task runner for DICOM extraction.
Uses a simple threading approach to avoid requiring Celery.
"""
import threading
import traceback
from pathlib import Path

from django.conf import settings

from .dicom_metadata import extracted_patient_name


def run_extraction_job(job_id):
    """Run extraction in a background thread."""
    thread = threading.Thread(target=_do_extraction, args=(job_id,), daemon=True)
    thread.start()


def _do_extraction(job_id):
    """The actual extraction logic, run in a background thread."""
    import django
    django.setup()

    from extractor.models import ExtractionJob
    from extractor.core import extract_exact, inspect_pair

    try:
        job = ExtractionJob.objects.get(id=job_id)
    except ExtractionJob.DoesNotExist:
        return

    try:
        job.status = 'processing'
        job.status_message = 'Starting extraction...'
        job.save(update_fields=['status', 'status_message', 'updated_at'])
        job.append_log('Extraction started.')

        cdx_path = Path(job.cdx_file.path)
        cache_path = Path(job.cache_file.path)

        # Inspect the pair to get metadata
        try:
            info = inspect_pair(cdx_path, cache_path)
            job.scan_id = info.scan_id
            job.patient_id = info.patient_id
            job.device = info.device
            job.matrix_rows = info.rows
            job.matrix_cols = info.cols
            job.matrix_slices = info.slices
            job.spacing_x = info.spacing_x
            job.spacing_y = info.spacing_y
            job.spacing_z = info.spacing_z_db
            job.save(update_fields=[
                'scan_id', 'patient_id', 'device',
                'matrix_rows', 'matrix_cols', 'matrix_slices',
                'spacing_x', 'spacing_y', 'spacing_z', 'updated_at',
            ])
            job.append_log(
                f'Matched scan: {info.scan_id} | '
                f'{info.cols}x{info.rows}x{info.slices} | '
                f'{info.device or "unknown device"}'
            )
        except Exception as exc:
            job.status = 'failed'
            job.status_message = f'File inspection failed: {exc}'
            job.error_message = str(exc)
            job.save(update_fields=['status', 'status_message', 'error_message', 'updated_at'])
            job.append_log(f'ERROR: {exc}')
            return

        # Set up output directory
        output_root = Path(settings.EXTRACTION_OUTPUT_DIR)
        output_root.mkdir(parents=True, exist_ok=True)

        # Progress callback
        def progress_cb(fraction, message):
            job.progress = fraction * 100.0
            job.status_message = message
            job.save(update_fields=['progress', 'status_message', 'updated_at'])

        # Run extraction
        if job.fix_left_right:
            job.append_log('Left/right correction: ON')
        else:
            job.append_log('Left/right correction: OFF')

        result = extract_exact(
            cdx_path=cdx_path,
            cache_path=cache_path,
            output_root=output_root,
            progress=progress_cb,
            fix_left_right=job.fix_left_right,
            patient_id=job.entered_patient_id,
        )

        # Update job with results
        job.patient_name = extracted_patient_name(result.output_dir, 'IMG*')
        job.status = 'completed'
        job.progress = 100.0
        job.status_message = 'Extraction completed successfully. All slices validated.'
        job.matrix_rows = result.rows
        job.matrix_cols = result.cols
        job.matrix_slices = result.slices
        job.spacing_x = result.spacing_x
        job.spacing_y = result.spacing_y
        job.spacing_z = result.spacing_z
        job.source_hash = result.source_hash
        job.output_hash = result.output_hash
        job.output_dir_name = result.output_dir.name
        job.zip_filename = result.zip_path.name
        job.report_filename = result.report_path.name
        job.save()
        job.append_log(f'PASS: {result.slices} slices extracted and validated.')
        job.append_log(f'ZIP: {result.zip_path.name}')
        job.append_log(f'Source SHA-256: {result.source_hash}')
        job.append_log(f'Output SHA-256: {result.output_hash}')

    except Exception as exc:
        tb = traceback.format_exc()
        try:
            job.status = 'failed'
            job.progress = 0
            job.status_message = f'Extraction failed: {exc}'
            job.error_message = str(exc)
            job.save(update_fields=['status', 'progress', 'status_message', 'error_message', 'updated_at'])
            job.append_log(f'FAILED: {exc}')
            job.append_log(tb)
        except Exception:
            pass

def run_real_guide_job(job_id):
    threading.Thread(target=_do_real_guide, args=(job_id,), daemon=True).start()

def _do_real_guide(job_id):
    import django
    django.setup()
    from extractor.models import RealGuideJob
    import zipfile

    try:
        job = RealGuideJob.objects.get(id=job_id)
    except RealGuideJob.DoesNotExist:
        return

    try:
        job.status = 'processing'
        job.status_message = 'Starting extraction...'
        job.save(update_fields=['status', 'status_message', 'updated_at'])
        job.append_log('RealGUIDE Extraction started.')
        from extractor.core_real_guide import export_cbct, inspect_archive

        zip_path = Path(job.project_zip.path)

        # We need a progress callback
        def progress_cb(fraction, message):
            job.progress = fraction * 100.0
            job.status_message = message
            job.save(update_fields=['progress', 'status_message', 'updated_at'])

        output_root = Path(settings.EXTRACTION_OUTPUT_DIR)
        output_root.mkdir(parents=True, exist_ok=True)

        # The core export_cbct returns the Path of the final DICOM output folder
        # It also generates the zip beside it.
        result_dir = export_cbct(
            archive=zip_path,
            output_parent=output_root,
            allow_assumed_orientation=job.allow_assumed_orientation,
            make_zip=True,
            progress=progress_cb,
            patient_id=job.entered_patient_id,
        )
        job.patient_name = extracted_patient_name(result_dir / 'DICOM', '*.dcm')

        # Get geometry info to update model
        with zipfile.ZipFile(zip_path, "r") as zf:
            project = inspect_archive(zf)
            job.matrix_cols, job.matrix_rows, job.matrix_slices = project.dimensions_xyz
            job.spacing_x, job.spacing_y, job.spacing_z = project.spacing_xyz

        job.status = 'completed'
        job.progress = 100.0
        job.status_message = 'Completed.'
        job.output_dir_name = result_dir.name
        job.zip_filename = result_dir.name + ".zip"
        # In real guide script, the validation report is placed inside the output folder:
        job.report_filename = f"{result_dir.name}/validation.json"

        job.save()
        job.append_log(f'PASS: Extracted RealGUIDE project successfully.')

    except Exception as exc:
        job.status = 'failed'
        job.progress = 0
        job.status_message = f'Extraction failed: {exc}'
        job.error_message = str(exc)
        job.save(update_fields=['status', 'progress', 'status_message', 'error_message', 'updated_at'])
        job.append_log(f'FAILED: {exc}')


def run_future_case_job(job_id):
    threading.Thread(target=_do_future_case, args=(job_id,), daemon=True).start()


def _do_future_case(job_id):
    from extractor.models import FutureCaseJob

    try:
        job = FutureCaseJob.objects.get(id=job_id)
    except FutureCaseJob.DoesNotExist:
        return

    try:
        job.status = 'processing'
        job.status_message = 'Starting Future Case recovery...'
        job.save(update_fields=['status', 'status_message', 'updated_at'])
        job.append_log('Future Case extraction started.')

        from extractor.future_case import recover_cbct

        output_dir = Path(settings.EXTRACTION_OUTPUT_DIR) / 'future_case' / str(job.id)

        def progress_cb(percent, message):
            job.progress = percent
            job.status_message = message
            job.save(update_fields=['progress', 'status_message', 'updated_at'])

        result = recover_cbct(
            input_path=job.source_file.path,
            output_dir=output_dir,
            reverse_z=job.reverse_z,
            create_zip_packages=job.create_zip_packages,
            patient_id=job.entered_patient_id,
            log=job.append_log,
            progress=progress_cb,
        )
        if result['status'] != 'PASS':
            raise RuntimeError('Validation failed: ' + '; '.join(result.get('errors', [])))

        job.patient_name = extracted_patient_name(output_dir / 'DICOM_SERIES', '*.dcm')
        job.matrix_cols, job.matrix_rows, job.matrix_slices = result['matrix_xyz']
        job.spacing_x, job.spacing_y, job.spacing_z = result['voxel_spacing_mm_xyz']
        job.nifti_filename = Path(result['nifti_file']).name
        job.output_dir_name = str(Path('future_case') / str(job.id))
        job.status = 'completed'
        job.progress = 100.0
        job.status_message = 'Recovery completed and validated.'
        job.save()
        job.append_log('PASS: Future Case recovery completed.')
    except Exception as exc:
        job.status = 'failed'
        job.status_message = f'Recovery failed: {exc}'
        job.error_message = str(exc)
        job.save(update_fields=['status', 'status_message', 'error_message', 'updated_at'])
        job.append_log(f'FAILED: {exc}')
        job.append_log(traceback.format_exc())
