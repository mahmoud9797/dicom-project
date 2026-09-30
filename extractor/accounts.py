"""Administrator-created accounts and short-lived password recovery codes."""

import ipaddress
import secrets
import smtplib
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .forms import AdminUserCreationForm, ForgotPasswordForm, PasswordRecoveryForm
from .models import PasswordRecoveryCode


def _user_for(identifier):
    field = 'email__iexact' if '@' in identifier else 'username__iexact'
    return get_user_model().objects.filter(is_active=True, **{field: identifier}).first()


def _client_ip(request):
    try:
        return str(ipaddress.ip_address(request.META.get('REMOTE_ADDR', '')))
    except ValueError:
        return None


@login_required
@sensitive_post_parameters('password1', 'password2')
@require_http_methods(['GET', 'POST'])
def register_user(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    form = AdminUserCreationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        messages.success(request, f'Account {user.username} was created.')
        return redirect('register_user')
    return render(request, 'extractor/register_user.html', {'form': form})


@never_cache
@require_http_methods(['GET', 'POST'])
def forgot_password(request):
    form = ForgotPasswordForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        identifier = form.cleaned_data['identifier'].strip()
        request.session['recovery_identifier'] = identifier
        user = _user_for(identifier)
        if user:
            now = timezone.now()
            ip = _client_ip(request)
            recent_ip_count = PasswordRecoveryCode.objects.filter(
                requested_ip=ip, created_at__gte=now - timedelta(minutes=15),
            ).count() if ip else 0
            recent_user_code = PasswordRecoveryCode.objects.filter(
                user=user, created_at__gte=now - timedelta(seconds=60),
            ).exists()
            if recent_ip_count < 5 and not recent_user_code:
                PasswordRecoveryCode.objects.filter(user=user, used_at__isnull=True).update(used_at=now)
                recovery = PasswordRecoveryCode.objects.create(
                    user=user, nonce=secrets.token_hex(16), requested_ip=ip,
                    expires_at=now + timedelta(minutes=10),
                )
                if settings.DICOM_EMAIL_ENABLED and user.email.lower().endswith('@3ddx.com'):
                    try:
                        send_mail(
                            'DicomExtractor password recovery code',
                            f'Your one-time recovery code is {recovery.code()}. '
                            'It expires in 10 minutes. If you did not request it, ignore this email.',
                            settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False,
                        )
                    except (OSError, smtplib.SMTPException):
                        pass  # The superuser can still provide the code manually.
                    else:
                        recovery.delivered_by_email = True
                        recovery.save(update_fields=['delivered_by_email'])
        delivery_help = (
            'Check your company email or ask the administrator for the code.'
            if settings.DICOM_EMAIL_ENABLED else 'Ask the administrator for the code.'
        )
        messages.info(request, f'If the account exists, a one-time code has been prepared. {delivery_help}')
        return redirect('password_code')
    return render(request, 'extractor/forgot_password.html', {'form': form})


@never_cache
@sensitive_post_parameters('code', 'new_password1', 'new_password2')
@require_http_methods(['GET', 'POST'])
def password_code(request):
    initial = {'identifier': request.session.get('recovery_identifier', '')}
    form = PasswordRecoveryForm(request.POST or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        user = _user_for(form.cleaned_data['identifier'].strip())
        now = timezone.now()
        recovery = (PasswordRecoveryCode.objects.filter(
            user=user, used_at__isnull=True, expires_at__gt=now, attempts__lt=5,
        ).order_by('-created_at').first() if user else None)
        attempt_allowed = (PasswordRecoveryCode.objects.filter(
            pk=recovery.pk, used_at__isnull=True, expires_at__gt=now, attempts__lt=5,
        ).update(attempts=F('attempts') + 1) if recovery else 0)
        if not attempt_allowed or not constant_time_compare(recovery.code(), form.cleaned_data['code']):
            form.add_error('code', 'Invalid or expired recovery code.')
        else:
            try:
                validate_password(form.cleaned_data['new_password1'], user=user)
            except ValidationError as exc:
                form.add_error('new_password1', exc)
            else:
                with transaction.atomic():
                    claimed = PasswordRecoveryCode.objects.filter(
                        pk=recovery.pk, used_at__isnull=True, expires_at__gt=timezone.now(),
                    ).update(used_at=timezone.now())
                    if claimed:
                        user.set_password(form.cleaned_data['new_password1'])
                        user.save(update_fields=['password'])
                        PasswordRecoveryCode.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
                if claimed:
                    request.session.pop('recovery_identifier', None)
                    messages.success(request, 'Password changed. Sign in with the new password.')
                    return redirect('login')
                form.add_error('code', 'Invalid or expired recovery code.')
    return render(request, 'extractor/password_code.html', {'form': form})


@login_required
@never_cache
@require_http_methods(['GET'])
def recovery_codes(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    active = PasswordRecoveryCode.objects.select_related('user').filter(
        used_at__isnull=True, expires_at__gt=timezone.now(), attempts__lt=5,
    ).order_by('-created_at')[:50]
    return render(request, 'extractor/recovery_codes.html', {'codes': active})
