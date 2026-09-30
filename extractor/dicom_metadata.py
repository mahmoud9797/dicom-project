"""Read patient metadata from a DICOM slice produced by an extractor."""
from pathlib import Path

import pydicom


def extracted_patient_name(output_dir: Path, pattern: str) -> str:
    """Return Patient Name (0010,0010) from the first output slice, if present."""
    first_slice = next(iter(sorted(output_dir.glob(pattern))), None)
    if first_slice is None:
        return ''
    dataset = pydicom.dcmread(
        first_slice,
        stop_before_pixels=True,
        specific_tags=[(0x0010, 0x0010)],
    )
    return str(dataset.get('PatientName', '')).strip()[:64]
