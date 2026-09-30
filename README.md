# DicomExtractor Pro

A Django application for extracting DICOM series and viewing axial, coronal,
sagittal, and threshold-derived 3D mask views. It supports coDiagnostiX,
RealGUIDE, and Future Case files.

## Run on Windows

Install Python, then run `start_server.bat` from this folder. On the first
installation, the launcher asks you to set the `admin` password. Open
`http://localhost:8000/` on the host PC. See [LAN_SETUP.md](LAN_SETUP.md) for
access from other PCs, including a host assigned `192.168.10.170`.

The repository contains source code only. Uploaded scans, extracted results,
the SQLite database, Python environment, and the local Django secret key are
kept outside Git. Do not commit patient data or credentials.

## Check

Run `python manage.py test extractor` and `python manage.py check` from the
project folder.
