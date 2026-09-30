import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse, FileResponse, Http404
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_GET, require_POST

from .forms import UploadForm, FutureCaseUploadForm
from .models import ExtractionJob, FutureCaseJob
from .tasks import run_extraction_job, run_future_case_job


@require_GET
def index(request):
    """Choose one of the three extractor workflows."""
    return render(request, 'extractor/home.html')


def co_diagnostix_index(request):
    """coDiagnostiX upload form and recent jobs."""
    if request.method == 'POST':
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            job = ExtractionJob.objects.create(
                entered_patient_id=form.cleaned_data['entered_patient_id'],
                cdx_file=form.cleaned_data['cdx_file'],
                cache_file=form.cleaned_data['cache_file'],
                fix_left_right=form.cleaned_data['fix_left_right'],
            )
            # Start background extraction
            run_extraction_job(job.id)
            return redirect('job_detail', job_id=job.id)
    else:
        form = UploadForm()

    recent_jobs = ExtractionJob.objects.all()[:10]
    return render(request, 'extractor/index.html', {
        'form': form,
        'recent_jobs': recent_jobs,
    })


def job_detail(request, job_id):
    """Show detailed status of a single extraction job."""
    job = get_object_or_404(ExtractionJob, id=job_id)
    if job.status == 'completed' and request.GET.get('details') != '1':
        return redirect('dicom_viewer', kind='co-diagnostix', job_id=job.id)
    return render(request, 'extractor/job_detail.html', {'job': job})


@require_GET
def job_status_api(request, job_id):
    """JSON API endpoint for polling job progress."""
    job = get_object_or_404(ExtractionJob, id=job_id)
    return JsonResponse({
        'status': job.status,
        'progress': job.progress_percent,
        'message': job.status_message,
        'is_complete': job.is_complete,
        'geometry': job.geometry_display,
        'spacing': job.spacing_display,
        'scan_id': job.scan_id,
        'device': job.device,
        'source_hash': job.source_hash,
        'output_hash': job.output_hash,
        'zip_filename': job.zip_filename,
        'report_filename': job.report_filename,
        'error_message': job.error_message,
        'log': job.log,
        'left_right_fixed': job.fix_left_right,
        'patient_name': job.patient_name,
    })


def job_list(request):
    """List all extraction jobs."""
    jobs = (
        [(job, 'coDiagnostiX', 'job_detail', 'download_zip') for job in ExtractionJob.objects.all()]
        + [(job, 'RealGUIDE', 'real_guide_job_detail', 'rg_download_zip') for job in RealGuideJob.objects.all()]
        + [(job, 'Future Case', 'future_case_job_detail', 'future_case_download') for job in FutureCaseJob.objects.all()]
    )
    jobs.sort(key=lambda item: item[0].created_at, reverse=True)
    return render(request, 'extractor/jobs.html', {'jobs': jobs})


@require_GET
def download_zip(request, job_id):
    """Download the output ZIP file."""
    job = get_object_or_404(ExtractionJob, id=job_id)
    if job.status != 'completed' or not job.zip_filename:
        raise Http404('ZIP file not available.')

    zip_path = Path(settings.EXTRACTION_OUTPUT_DIR) / job.zip_filename
    if not zip_path.is_file():
        raise Http404('ZIP file not found on disk.')

    return FileResponse(
        open(zip_path, 'rb'),
        as_attachment=True,
        filename=job.zip_filename,
        content_type='application/zip',
    )


@require_GET
def download_report(request, job_id):
    """Download the validation report."""
    job = get_object_or_404(ExtractionJob, id=job_id)
    if job.status != 'completed' or not job.report_filename:
        raise Http404('Report not available.')

    report_path = Path(settings.EXTRACTION_OUTPUT_DIR) / job.report_filename
    if not report_path.is_file():
        raise Http404('Report file not found on disk.')

    return FileResponse(
        open(report_path, 'rb'),
        as_attachment=True,
        filename=job.report_filename,
        content_type='text/plain',
    )


@require_POST
def delete_job(request, job_id):
    """Delete a job and its output files."""
    import shutil
    job = get_object_or_404(ExtractionJob, id=job_id)

    # Clean up output files
    output_root = Path(settings.EXTRACTION_OUTPUT_DIR).resolve()
    if job.output_dir_name:
        output_dir = (output_root / job.output_dir_name).resolve()
        if output_dir.parent == output_root and output_dir.exists():
            shutil.rmtree(output_dir, ignore_errors=True)
    viewer_cache_parent = (output_root / '.viewer_cache' / 'co-diagnostix').resolve()
    viewer_cache = (viewer_cache_parent / str(job.id)).resolve()
    if viewer_cache_parent.parent.parent == output_root and viewer_cache.parent == viewer_cache_parent and viewer_cache.exists():
        shutil.rmtree(viewer_cache, ignore_errors=True)
    if job.zip_filename:
        zip_path = output_root / job.zip_filename
        if zip_path.exists():
            zip_path.unlink(missing_ok=True)
    if job.report_filename:
        report_path = output_root / job.report_filename
        if report_path.exists():
            report_path.unlink(missing_ok=True)

    # Clean up uploaded files
    try:
        if job.cdx_file:
            job.cdx_file.delete(save=False)
        if job.cache_file:
            job.cache_file.delete(save=False)
    except Exception:
        pass

    job.delete()
    return redirect('job_list')


def about(request):
    """About page with product information."""
    return render(request, 'extractor/about.html')

from .models import RealGuideJob
from .forms import RealGuideUploadForm
from .tasks import run_real_guide_job

def real_guide_index(request):
    if request.method == 'POST':
        form = RealGuideUploadForm(request.POST, request.FILES)
        if form.is_valid():
            job = RealGuideJob.objects.create(
                entered_patient_id=form.cleaned_data['entered_patient_id'],
                project_zip=form.cleaned_data['project_zip'],
                allow_assumed_orientation=form.cleaned_data['allow_assumed_orientation'],
            )
            run_real_guide_job(job.id)
            return redirect('real_guide_job_detail', job_id=job.id)
    else:
        form = RealGuideUploadForm()

    recent_jobs = RealGuideJob.objects.all()[:10]
    return render(request, 'extractor/real_guide_index.html', {'form': form, 'recent_jobs': recent_jobs})

def real_guide_job_detail(request, job_id):
    job = get_object_or_404(RealGuideJob, id=job_id)
    if job.status == 'completed' and request.GET.get('details') != '1':
        return redirect('dicom_viewer', kind='real-guide', job_id=job.id)
    return render(request, 'extractor/real_guide_job_detail.html', {'job': job})

@require_GET
def real_guide_status_api(request, job_id):
    job = get_object_or_404(RealGuideJob, id=job_id)
    return JsonResponse({
        'status': job.status,
        'progress': job.progress_percent,
        'message': job.status_message,
        'is_complete': job.is_complete,
        'geometry': job.geometry_display,
        'spacing': job.spacing_display,
        'zip_filename': job.zip_filename,
        'report_filename': job.report_filename,
        'error_message': job.error_message,
        'log': job.log,
        'patient_name': job.patient_name,
    })

@require_GET
def rg_download_zip(request, job_id):
    job = get_object_or_404(RealGuideJob, id=job_id)
    if job.status != 'completed' or not job.zip_filename: raise Http404('Not available.')
    zip_path = Path(settings.EXTRACTION_OUTPUT_DIR) / job.zip_filename
    if not zip_path.is_file(): raise Http404('Not found.')
    return FileResponse(open(zip_path, 'rb'), as_attachment=True, filename=job.zip_filename, content_type='application/zip')

@require_GET
def rg_download_report(request, job_id):
    job = get_object_or_404(RealGuideJob, id=job_id)
    if job.status != 'completed' or not job.report_filename: raise Http404('Not available.')
    report_path = Path(settings.EXTRACTION_OUTPUT_DIR) / job.report_filename
    if not report_path.is_file(): raise Http404('Not found.')
    return FileResponse(open(report_path, 'rb'), as_attachment=True, filename="validation.json", content_type='application/json')


def future_case_index(request):
    if request.method == 'POST':
        form = FutureCaseUploadForm(request.POST, request.FILES)
        if form.is_valid():
            job = FutureCaseJob.objects.create(
                entered_patient_id=form.cleaned_data['entered_patient_id'],
                source_file=form.cleaned_data['source_file'],
                reverse_z=form.cleaned_data['reverse_z'],
                create_zip_packages=form.cleaned_data['create_zip_packages'],
            )
            run_future_case_job(job.id)
            return redirect('future_case_job_detail', job_id=job.id)
    else:
        form = FutureCaseUploadForm()
    return render(request, 'extractor/future_case_index.html', {
        'form': form,
        'recent_jobs': FutureCaseJob.objects.all()[:10],
    })


def future_case_job_detail(request, job_id):
    job = get_object_or_404(FutureCaseJob, id=job_id)
    if job.status == 'completed' and request.GET.get('details') != '1':
        return redirect('dicom_viewer', kind='future-case', job_id=job.id)
    return render(request, 'extractor/future_case_job_detail.html', {'job': job})


@require_GET
def future_case_status_api(request, job_id):
    job = get_object_or_404(FutureCaseJob, id=job_id)
    return JsonResponse({
        'status': job.status,
        'progress': job.progress_percent,
        'message': job.status_message,
        'is_complete': job.is_complete,
        'geometry': job.geometry_display,
        'spacing': job.spacing_display,
        'error_message': job.error_message,
        'log': job.log,
        'patient_name': job.patient_name,
    })


@require_GET
def future_case_download(request, job_id, kind):
    job = get_object_or_404(FutureCaseJob, id=job_id)
    if job.status != 'completed':
        raise Http404('Output is not available.')
    names = {
        'dicom': 'DICOM_IMPORT.zip' if job.create_zip_packages else None,
        'nifti-zip': 'NIFTI_AND_PREVIEWS.zip' if job.create_zip_packages else None,
        'nifti': job.nifti_filename,
        'report': 'VALIDATION_REPORT.json',
    }
    if kind not in names or not names[kind]:
        raise Http404('Output is not available.')
    output = Path(settings.EXTRACTION_OUTPUT_DIR) / 'future_case' / str(job.id) / names[kind]
    if not output.is_file():
        raise Http404('Output file not found.')
    return FileResponse(output.open('rb'), as_attachment=True, filename=output.name)
