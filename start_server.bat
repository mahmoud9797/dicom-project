@echo off
setlocal
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found on PATH. Install Python 3.9 or newer.
    pause
    exit /b 1
)

echo Starting DicomExtractor Pro on port 8000...
echo Open http://localhost:8000/ on this PC.
echo For another device, use http://THIS-PC-IP:8000/ with one of these IPv4 addresses:
ipconfig | findstr /C:"IPv4 Address"
ipconfig | findstr /C:"192.168.10.170" >nul
if errorlevel 1 (
    echo Planned LAN address 192.168.10.170 is not assigned to this PC yet.
) else (
    echo Planned LAN viewer: http://192.168.10.170:8000/
)

python -c "import django, numpy, pydicom, nibabel, imagecodecs, PIL, scipy, skimage" >nul 2>&1
if errorlevel 1 (
    echo Installing missing dependencies...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency installation failed. Check your internet connection and retry.
        pause
        exit /b 1
    )
)

python manage.py migrate --noinput
if errorlevel 1 (
    echo Database migration failed.
    pause
    exit /b 1
)

python manage.py shell -c "from django.contrib.auth import get_user_model; import sys; user = get_user_model().objects.filter(username='admin').first(); sys.exit(0 if user and user.has_usable_password() else 1)"
if errorlevel 1 (
    echo Set the administrator password for this installation.
    python manage.py changepassword admin
    if errorlevel 1 (
        echo Administrator password setup failed.
        pause
        exit /b 1
    )
)

echo Open the address assigned to this PC from a browser on the LAN or VPN.
echo Press Ctrl+C to stop the server.
python manage.py runserver --insecure 0.0.0.0:8000
pause
