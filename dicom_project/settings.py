import os
import secrets
import socket
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

_secret_file = BASE_DIR / '.django_secret_key'
if not _secret_file.exists():
    try:
        with _secret_file.open('x', encoding='utf-8') as stream:
            stream.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
SECRET_KEY = os.environ.get('DICOM_SECRET_KEY') or _secret_file.read_text(encoding='utf-8').strip()

DEBUG = os.environ.get('DICOM_DEBUG') == '1'

def _local_ipv4_hosts():
    """Accept this machine's current LAN addresses without accepting arbitrary hosts."""
    try:
        return {
            entry[4][0]
            for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        }
    except OSError:
        return set()


ALLOWED_HOSTS = sorted(
    {'localhost', '127.0.0.1', '192.168.10.170', socket.gethostname().lower()}
    | _local_ipv4_hosts()
    | {host.strip() for host in os.environ.get('DICOM_ALLOWED_HOSTS', '').split(',') if host.strip()}
)
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'index'
LOGOUT_REDIRECT_URL = 'login'

# Optional SMTP delivery for one-time password recovery codes. Without an SMTP
# sender, a superuser can provide a code from the Recovery page.
EMAIL_HOST = os.environ.get('DICOM_SMTP_HOST', '').strip()
EMAIL_PORT = int(os.environ.get('DICOM_SMTP_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('DICOM_SMTP_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('DICOM_SMTP_PASSWORD', '')
EMAIL_USE_SSL = os.environ.get('DICOM_SMTP_SSL', '0') == '1'
EMAIL_USE_TLS = os.environ.get('DICOM_SMTP_TLS', '1') == '1' and not EMAIL_USE_SSL
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = os.environ.get('DICOM_SMTP_FROM', '').strip()
DICOM_EMAIL_ENABLED = bool(EMAIL_HOST and DEFAULT_FROM_EMAIL)

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'extractor',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'dicom_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'dicom_project.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Upload settings
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024 * 1024  # 5GB
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024 * 1024  # 5GB

# Extraction output directory
EXTRACTION_OUTPUT_DIR = BASE_DIR / 'extraction_output'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
