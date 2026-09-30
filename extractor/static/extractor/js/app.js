/**
 * DicomExtractor Pro - Frontend JavaScript
 * Handles drag-and-drop, file upload, and job progress polling.
 */

document.addEventListener('DOMContentLoaded', function () {
    initDropZone();
});


// ==========================================================================
// Drag & Drop
// ==========================================================================

function initDropZone() {
    const dropZone = document.getElementById('drop-zone');
    const cdxInput = document.getElementById('cdx-file-input');
    const cacheInput = document.getElementById('cache-file-input');
    const filesDisplay = document.getElementById('drop-zone-files');

    if (!dropZone || !cdxInput || !cacheInput) return;

    // Click to browse
    dropZone.addEventListener('click', function (e) {
        if (e.target.tagName === 'INPUT') return;
        // Open a generic file picker — user picks either
        const tempInput = document.createElement('input');
        tempInput.type = 'file';
        tempInput.multiple = true;
        tempInput.accept = '.cdxPatient,.cdxpatient,.cache';
        tempInput.addEventListener('change', function () {
            handleDroppedFiles(tempInput.files, cdxInput, cacheInput, filesDisplay);
        });
        tempInput.click();
    });

    // Drag events
    ['dragenter', 'dragover'].forEach(evt => {
        dropZone.addEventListener(evt, function (e) {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.add('drag-over');
        });
    });

    ['dragleave', 'drop'].forEach(evt => {
        dropZone.addEventListener(evt, function (e) {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.remove('drag-over');
        });
    });

    dropZone.addEventListener('drop', function (e) {
        const files = e.dataTransfer.files;
        handleDroppedFiles(files, cdxInput, cacheInput, filesDisplay);
    });

    // Also update display when file inputs change directly
    cdxInput.addEventListener('change', function () {
        updateFileDisplay(cdxInput, cacheInput, filesDisplay);
    });
    cacheInput.addEventListener('change', function () {
        updateFileDisplay(cdxInput, cacheInput, filesDisplay);
    });
}


function handleDroppedFiles(files, cdxInput, cacheInput, filesDisplay) {
    let cdxFile = null;
    let cacheFile = null;

    for (let i = 0; i < files.length; i++) {
        const name = files[i].name.toLowerCase();
        if (name.endsWith('.cdxpatient')) {
            cdxFile = files[i];
        } else if (name.endsWith('.cache')) {
            cacheFile = files[i];
        }
    }

    // Use DataTransfer to set file input values
    if (cdxFile) {
        const dt = new DataTransfer();
        dt.items.add(cdxFile);
        cdxInput.files = dt.files;
    }
    if (cacheFile) {
        const dt = new DataTransfer();
        dt.items.add(cacheFile);
        cacheInput.files = dt.files;
    }

    updateFileDisplay(cdxInput, cacheInput, filesDisplay);
}


function updateFileDisplay(cdxInput, cacheInput, filesDisplay) {
    if (!filesDisplay) return;
    let html = '';

    if (cdxInput.files.length > 0) {
        html += '<span class="file-badge"><i class="bi bi-database me-1"></i>' +
                escapeHtml(cdxInput.files[0].name) + '</span>';
    }
    if (cacheInput.files.length > 0) {
        html += '<span class="file-badge"><i class="bi bi-file-binary me-1"></i>' +
                escapeHtml(cacheInput.files[0].name) + '</span>';
    }

    filesDisplay.innerHTML = html;
}


// ==========================================================================
// Job Progress Polling
// ==========================================================================

function pollJobStatus(statusUrl) {
    const interval = setInterval(function () {
        fetch(statusUrl)
            .then(r => r.json())
            .then(data => {
                updateJobUI(data);
                if (data.is_complete) {
                    clearInterval(interval);
                    if (data.status === 'completed' && typeof VIEWER_URL !== 'undefined') {
                        window.location.assign(VIEWER_URL);
                    }
                }
            })
            .catch(err => console.error('Poll error:', err));
    }, 1500);
}


function updateJobUI(data) {
    // Progress bar
    const bar = document.getElementById('progress-bar');
    const progressText = document.getElementById('progress-text');
    const statusMessage = document.getElementById('status-message');
    const statusBadge = document.getElementById('status-badge');
    const logOutput = document.getElementById('log-output');
    const statusCard = document.getElementById('status-card');

    if (bar) {
        bar.style.width = data.progress + '%';
        bar.setAttribute('aria-valuenow', data.progress);

        // Update bar color
        bar.className = 'progress-bar progress-bar-striped';
        if (data.status === 'processing') {
            bar.classList.add('progress-bar-animated', 'bg-info');
        } else if (data.status === 'completed') {
            bar.classList.add('bg-success');
        } else if (data.status === 'failed') {
            bar.classList.add('bg-danger');
        }
    }

    if (progressText) progressText.textContent = Math.round(data.progress) + '%';
    if (statusMessage) statusMessage.textContent = data.message;

    if (statusBadge) {
        statusBadge.textContent = data.status.toUpperCase();
        statusBadge.className = 'badge fs-6';
        if (data.status === 'completed') statusBadge.classList.add('bg-success');
        else if (data.status === 'processing') statusBadge.classList.add('bg-warning', 'text-dark');
        else if (data.status === 'failed') statusBadge.classList.add('bg-danger');
        else statusBadge.classList.add('bg-secondary');
    }

    if (logOutput && data.log) {
        logOutput.textContent = data.log;
        logOutput.scrollTop = logOutput.scrollHeight;
    }

    // Scan info
    const infoFields = {
        'info-patient-name': data.patient_name || '—',
        'info-scan-id': data.scan_id || '—',
        'info-device': data.device || '—',
        'info-geometry': data.geometry || '—',
        'info-spacing': data.spacing || '—',
    };
    for (const [id, val] of Object.entries(infoFields)) {
        const el = document.getElementById(id);
        if (el) el.textContent = val;
    }

    // Status card animation
    if (statusCard) {
        if (data.status === 'processing') {
            statusCard.classList.add('processing');
        } else {
            statusCard.classList.remove('processing');
        }
    }

    // Show/hide sections on completion
    if (data.is_complete) {
        const downloadsCard = document.getElementById('downloads-card');
        const integrityCard = document.getElementById('integrity-card');

        if (data.status === 'completed') {
            if (downloadsCard) downloadsCard.classList.remove('d-none');
            if (integrityCard) integrityCard.classList.remove('d-none');

            // Update hash displays
            const srcHash = document.getElementById('info-source-hash');
            const outHash = document.getElementById('info-output-hash');
            if (srcHash && data.source_hash) srcHash.textContent = data.source_hash.substring(0, 16) + '...';
            if (outHash && data.output_hash) outHash.textContent = data.output_hash.substring(0, 16) + '...';

            // Show success alert
            let successAlert = document.getElementById('success-alert');
            if (!successAlert) {
                const cardBody = bar ? bar.closest('.card-body') : null;
                if (cardBody) {
                    successAlert = document.createElement('div');
                    successAlert.className = 'alert alert-success';
                    successAlert.id = 'success-alert';
                    successAlert.innerHTML = '<i class="bi bi-check-circle me-2"></i><strong>All slices extracted and validated successfully.</strong>';
                    cardBody.appendChild(successAlert);
                }
            }
        } else if (data.status === 'failed') {
            // Show error alert
            let errorAlert = document.getElementById('error-alert');
            if (!errorAlert) {
                const cardBody = bar ? bar.closest('.card-body') : null;
                if (cardBody) {
                    errorAlert = document.createElement('div');
                    errorAlert.className = 'alert alert-danger';
                    errorAlert.id = 'error-alert';
                    errorAlert.innerHTML = '<i class="bi bi-exclamation-triangle me-2"></i><strong>Error:</strong> <span id="error-message">' + escapeHtml(data.error_message) + '</span>';
                    cardBody.appendChild(errorAlert);
                }
            } else {
                const errMsg = document.getElementById('error-message');
                if (errMsg) errMsg.textContent = data.error_message;
            }
        }
    }
}


// ==========================================================================
// Utilities
// ==========================================================================

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
