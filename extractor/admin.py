from django.contrib import admin
from .models import ExtractionJob, RealGuideJob, FutureCaseJob


@admin.register(ExtractionJob)
class ExtractionJobAdmin(admin.ModelAdmin):
    list_display = ['id', 'entered_patient_id', 'patient_name', 'status', 'scan_id', 'device', 'progress', 'created_at']
    list_filter = ['status', 'fix_left_right']
    search_fields = ['entered_patient_id', 'patient_name', 'scan_id', 'patient_id', 'device']
    readonly_fields = [
        'id', 'created_at', 'updated_at', 'progress', 'status_message',
        'source_hash', 'output_hash', 'log', 'error_message',
    ]
    ordering = ['-created_at']


@admin.register(RealGuideJob)
class RealGuideJobAdmin(admin.ModelAdmin):
    list_display = ['id', 'entered_patient_id', 'patient_name', 'status', 'progress', 'created_at']
    search_fields = ['entered_patient_id', 'patient_name']


@admin.register(FutureCaseJob)
class FutureCaseJobAdmin(admin.ModelAdmin):
    list_display = ['id', 'entered_patient_id', 'patient_name', 'status', 'progress', 'created_at']
    search_fields = ['entered_patient_id', 'patient_name']
