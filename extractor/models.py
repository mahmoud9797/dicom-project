import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.crypto import salted_hmac


class ExtractionJob(models.Model):
    """Tracks a DICOM extraction job from upload through completion."""

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    progress = models.FloatField(default=0.0, help_text='Progress 0-100')
    status_message = models.TextField(default='Waiting to start...', blank=True)
    patient_name = models.CharField(max_length=64, blank=True, default='')
    entered_patient_id = models.CharField(max_length=64, blank=True, default='')

    # Input files
    cdx_file = models.FileField(upload_to='uploads/cdx/', max_length=500)
    cache_file = models.FileField(upload_to='uploads/cache/', max_length=500)

    # Options
    fix_left_right = models.BooleanField(
        default=True,
        help_text='Fix left/right mirror (reverse columns in every slice)'
    )

    # Scan info (populated after inspection)
    scan_id = models.CharField(max_length=255, blank=True, default='')
    patient_id = models.CharField(max_length=255, blank=True, default='')
    device = models.CharField(max_length=255, blank=True, default='')
    matrix_rows = models.IntegerField(null=True, blank=True)
    matrix_cols = models.IntegerField(null=True, blank=True)
    matrix_slices = models.IntegerField(null=True, blank=True)
    spacing_x = models.FloatField(null=True, blank=True)
    spacing_y = models.FloatField(null=True, blank=True)
    spacing_z = models.FloatField(null=True, blank=True)
    source_hash = models.CharField(max_length=64, blank=True, default='')
    output_hash = models.CharField(max_length=64, blank=True, default='')

    # Output paths (relative to EXTRACTION_OUTPUT_DIR)
    output_dir_name = models.CharField(max_length=500, blank=True, default='')
    zip_filename = models.CharField(max_length=500, blank=True, default='')
    report_filename = models.CharField(max_length=500, blank=True, default='')

    # Log
    log = models.TextField(default='', blank=True)
    error_message = models.TextField(default='', blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Extraction Job'
        verbose_name_plural = 'Extraction Jobs'

    def __str__(self):
        return f"Job {self.id} - {self.status} ({self.created_at:%Y-%m-%d %H:%M})"

    def append_log(self, message: str):
        """Thread-safe log appending."""
        from django.utils import timezone as tz
        timestamp = tz.now().strftime('%H:%M:%S')
        self.log += f"[{timestamp}] {message}\n"
        self.save(update_fields=['log', 'updated_at'])

    @property
    def progress_percent(self):
        return min(100, max(0, self.progress))

    @property
    def is_complete(self):
        return self.status in ('completed', 'failed')

    @property
    def geometry_display(self):
        if self.matrix_cols and self.matrix_rows and self.matrix_slices:
            return f"{self.matrix_cols} × {self.matrix_rows} × {self.matrix_slices}"
        return "—"

    @property
    def spacing_display(self):
        if self.spacing_x and self.spacing_y and self.spacing_z:
            return f"{self.spacing_x:.4g} × {self.spacing_y:.4g} × {self.spacing_z:.4g} mm"
        return "—"

class RealGuideJob(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    progress = models.FloatField(default=0.0)
    status_message = models.TextField(default='Waiting to start...', blank=True)
    patient_name = models.CharField(max_length=64, blank=True, default='')
    entered_patient_id = models.CharField(max_length=64, blank=True, default='')

    project_zip = models.FileField(upload_to='uploads/realguide/', max_length=500)
    allow_assumed_orientation = models.BooleanField(default=False)

    matrix_rows = models.IntegerField(null=True, blank=True)
    matrix_cols = models.IntegerField(null=True, blank=True)
    matrix_slices = models.IntegerField(null=True, blank=True)
    spacing_x = models.FloatField(null=True, blank=True)
    spacing_y = models.FloatField(null=True, blank=True)
    spacing_z = models.FloatField(null=True, blank=True)
    source_hash = models.CharField(max_length=64, blank=True, default='')
    output_hash = models.CharField(max_length=64, blank=True, default='')

    output_dir_name = models.CharField(max_length=500, blank=True, default='')
    zip_filename = models.CharField(max_length=500, blank=True, default='')
    report_filename = models.CharField(max_length=500, blank=True, default='')

    log = models.TextField(default='', blank=True)
    error_message = models.TextField(default='', blank=True)

    class Meta:
        ordering = ['-created_at']

    def append_log(self, message: str):
        from django.utils import timezone as tz
        timestamp = tz.now().strftime('%H:%M:%S')
        self.log += f"[{timestamp}] {message}\n"
        self.save(update_fields=['log', 'updated_at'])

    @property
    def progress_percent(self):
        return min(100, max(0, self.progress))

    @property
    def is_complete(self):
        return self.status in ('completed', 'failed')

    @property
    def geometry_display(self):
        if self.matrix_cols and self.matrix_rows and self.matrix_slices:
            return f"{self.matrix_cols} × {self.matrix_rows} × {self.matrix_slices}"
        return "—"

    @property
    def spacing_display(self):
        if self.spacing_x and self.spacing_y and self.spacing_z:
            return f"{self.spacing_x:.4g} × {self.spacing_y:.4g} × {self.spacing_z:.4g} mm"
        return "—"


class FutureCaseJob(models.Model):
    STATUS_CHOICES = ExtractionJob.STATUS_CHOICES

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    progress = models.FloatField(default=0.0)
    status_message = models.TextField(default='Waiting to start...', blank=True)
    patient_name = models.CharField(max_length=64, blank=True, default='')
    entered_patient_id = models.CharField(max_length=64, blank=True, default='')
    source_file = models.FileField(upload_to='uploads/future_case/', max_length=500)
    reverse_z = models.BooleanField(default=False)
    create_zip_packages = models.BooleanField(default=True)
    matrix_rows = models.IntegerField(null=True, blank=True)
    matrix_cols = models.IntegerField(null=True, blank=True)
    matrix_slices = models.IntegerField(null=True, blank=True)
    spacing_x = models.FloatField(null=True, blank=True)
    spacing_y = models.FloatField(null=True, blank=True)
    spacing_z = models.FloatField(null=True, blank=True)
    output_dir_name = models.CharField(max_length=500, blank=True, default='')
    nifti_filename = models.CharField(max_length=500, blank=True, default='')
    log = models.TextField(default='', blank=True)
    error_message = models.TextField(default='', blank=True)

    class Meta:
        ordering = ['-created_at']

    def append_log(self, message: str):
        timestamp = timezone.now().strftime('%H:%M:%S')
        self.log += f"[{timestamp}] {message}\n"
        self.save(update_fields=['log', 'updated_at'])

    @property
    def progress_percent(self):
        return min(100, max(0, self.progress))

    @property
    def is_complete(self):
        return self.status in ('completed', 'failed')

    @property
    def geometry_display(self):
        if self.matrix_cols and self.matrix_rows and self.matrix_slices:
            return f"{self.matrix_cols} × {self.matrix_rows} × {self.matrix_slices}"
        return "—"

    @property
    def spacing_display(self):
        if self.spacing_x and self.spacing_y and self.spacing_z:
            return f"{self.spacing_x:.4g} × {self.spacing_y:.4g} × {self.spacing_z:.4g} mm"
        return "—"


class PasswordRecoveryCode(models.Model):
    """Short-lived, one-use code; the plaintext is derived only when needed."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    nonce = models.CharField(max_length=32)
    requested_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    used_at = models.DateTimeField(null=True, blank=True)
    delivered_by_email = models.BooleanField(default=False)

    def code(self):
        digest = salted_hmac(
            'extractor.password-recovery',
            f'{self.id}:{self.user_id}:{self.nonce}',
            secret=settings.SECRET_KEY,
        ).hexdigest()
        return f'{int(digest[:16], 16) % 100_000_000:08d}'

    def is_valid(self):
        return self.used_at is None and self.attempts < 5 and self.expires_at > timezone.now()
