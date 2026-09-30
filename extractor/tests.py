from pathlib import Path
from io import BytesIO
import socket
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from datetime import timedelta
from zipfile import ZipFile

import numpy as np
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from .dicom_metadata import extracted_patient_name
from .forms import UploadForm, RealGuideUploadForm, FutureCaseUploadForm
from .models import ExtractionJob, RealGuideJob, FutureCaseJob, PasswordRecoveryCode


class RealGuideWorkerTests(TestCase):
    def test_job_moves_from_pending_to_completed(self):
        from .tasks import _do_real_guide

        archive_bytes = BytesIO()
        with ZipFile(archive_bytes, 'w'):
            pass
        with TemporaryDirectory() as temporary, override_settings(
            MEDIA_ROOT=temporary, EXTRACTION_OUTPUT_DIR=temporary
        ):
            output_dir = Path(temporary) / 'converted'
            (output_dir / 'DICOM').mkdir(parents=True)
            job = RealGuideJob.objects.create(
                project_zip=SimpleUploadedFile('project.zip', archive_bytes.getvalue()),
                allow_assumed_orientation=True,
                entered_patient_id='TEST-ID',
            )
            project = SimpleNamespace(
                dimensions_xyz=(4, 5, 6), spacing_xyz=(0.2, 0.3, 0.4)
            )
            with patch('extractor.core_real_guide.export_cbct', return_value=output_dir), \
                 patch('extractor.core_real_guide.inspect_archive', return_value=project):
                _do_real_guide(job.id)

            job.refresh_from_db()
            self.assertEqual(job.status, 'completed', job.error_message)
            self.assertEqual(job.progress, 100)
            self.assertEqual(job.matrix_slices, 6)
            self.assertEqual(job.output_dir_name, output_dir.name)


class LoginProtectionTests(TestCase):
    def test_login_accepts_planned_lan_address(self):
        response = self.client.get(reverse('login'), HTTP_HOST='192.168.10.170')
        self.assertEqual(response.status_code, 200)

    def test_login_accepts_this_pcs_lan_address(self):
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
        if not addresses:
            self.skipTest('This machine has no resolved IPv4 address.')
        response = self.client.get(reverse('login'), HTTP_HOST=addresses[0])
        self.assertEqual(response.status_code, 200)

    def test_every_extractor_and_result_route_requires_login(self):
        job_id = uuid4()
        protected_urls = [
            reverse('index'),
            reverse('co_diagnostix_index'),
            reverse('real_guide_index'),
            reverse('future_case_index'),
            reverse('job_list'),
            reverse('about'),
            reverse('job_detail', args=[job_id]),
            reverse('real_guide_job_detail', args=[job_id]),
            reverse('future_case_job_detail', args=[job_id]),
            reverse('job_status_api', args=[job_id]),
            reverse('real_guide_status_api', args=[job_id]),
            reverse('future_case_status_api', args=[job_id]),
            reverse('download_zip', args=[job_id]),
            reverse('download_report', args=[job_id]),
            reverse('rg_download_zip', args=[job_id]),
            reverse('rg_download_report', args=[job_id]),
            reverse('future_case_download', args=[job_id, 'dicom']),
            reverse('dicom_viewer', args=['co-diagnostix', job_id]),
            reverse('viewer_list'),
            reverse('viewer_slice', args=['co-diagnostix', job_id, 0]),
            reverse('viewer_surface', args=['co-diagnostix', job_id]),
            reverse('viewer_surface_stl', args=['co-diagnostix', job_id]),
            reverse('viewer_surface_obj', args=['co-diagnostix', job_id]),
            reverse('viewer_mask', args=['co-diagnostix', job_id]),
            reverse('viewer_mask_slice', args=['co-diagnostix', job_id, 'axial', 0]),
            reverse('viewer_mpr_slice', args=['co-diagnostix', job_id, 'coronal', 0]),
        ]
        for url in protected_urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith('/login/?next='))

    def test_login_and_logout(self):
        user = get_user_model().objects.get(username='admin')
        self.assertFalse(user.has_usable_password())
        user.set_password('temporary-test-password')
        user.save()
        response = self.client.post(reverse('login'), {
            'username': 'admin', 'password': 'temporary-test-password',
        })
        self.assertRedirects(response, reverse('index'))
        self.assertEqual(self.client.get(reverse('index')).status_code, 200)
        self.assertRedirects(self.client.post(reverse('logout')), reverse('login'))
        self.assertEqual(self.client.get(reverse('index')).status_code, 302)

    def test_patient_name_is_not_a_form_input(self):
        for form_class in (UploadForm, RealGuideUploadForm, FutureCaseUploadForm):
            with self.subTest(form=form_class.__name__):
                self.assertNotIn('patient_name', form_class().fields)
                self.assertIn('entered_patient_id', form_class().fields)
                self.assertTrue(form_class().fields['entered_patient_id'].required)


class AccountManagementTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.get(username='admin')

    def test_only_admin_can_create_user_and_email_must_match_company_domain(self):
        url = reverse('register_user')
        self.assertRedirects(self.client.get(url), reverse('login') + '?next=' + url)
        regular = get_user_model().objects.create_user('regular', 'regular@3ddx.com', 'RegularPass!2026')
        self.client.force_login(regular)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.admin)
        details = {'username': 'newuser', 'email': 'newuser@other.com',
                   'password1': 'StrongPass!2026', 'password2': 'StrongPass!2026'}
        response = self.client.post(url, details)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(username='newuser').exists())
        details['email'] = 'newuser@3ddx.com.evil'
        self.assertEqual(self.client.post(url, details).status_code, 200)
        self.assertFalse(get_user_model().objects.filter(username='newuser').exists())
        details['email'] = 'NewUser@3DDX.COM'
        self.assertRedirects(self.client.post(url, details), url)
        user = get_user_model().objects.get(username='newuser')
        self.assertEqual(user.email, 'newuser@3ddx.com')
        self.assertFalse(user.is_staff)
        self.assertTrue(user.check_password('StrongPass!2026'))
        self.assertContains(self.client.get(url), 'data-password-toggle')
        self.client.logout()
        self.assertContains(self.client.get(reverse('login')), 'data-password-toggle')

    def test_admin_can_supply_one_time_code_without_smtp(self):
        user = get_user_model().objects.create_user('caseuser', 'caseuser@3ddx.com', 'OriginalPass!2026')
        response = self.client.post(reverse('forgot_password'), {'identifier': user.email})
        self.assertRedirects(response, reverse('password_code'))
        recovery = PasswordRecoveryCode.objects.get(user=user)
        self.assertFalse(recovery.delivered_by_email)
        self.assertEqual(len(recovery.code()), 8)
        self.assertEqual(self.client.get(reverse('recovery_codes')).status_code, 302)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('recovery_codes')), recovery.code())
        self.client.logout()
        response = self.client.post(reverse('password_code'), {
            'identifier': user.email, 'code': recovery.code(),
            'new_password1': 'NewSecurePass!2026', 'new_password2': 'NewSecurePass!2026',
        })
        self.assertRedirects(response, reverse('login'))
        user.refresh_from_db()
        recovery.refresh_from_db()
        self.assertTrue(user.check_password('NewSecurePass!2026'))
        self.assertIsNotNone(recovery.used_at)
        self.assertEqual(self.client.post(reverse('password_code'), {
            'identifier': user.email, 'code': recovery.code(),
            'new_password1': 'AgainSecurePass!2026', 'new_password2': 'AgainSecurePass!2026',
        }).status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password('NewSecurePass!2026'))

    def test_wrong_or_expired_recovery_code_cannot_reset_password(self):
        user = get_user_model().objects.create_user('otheruser', 'otheruser@3ddx.com', 'OriginalPass!2026')
        self.client.post(reverse('forgot_password'), {'identifier': user.username})
        recovery = PasswordRecoveryCode.objects.get(user=user)
        payload = {'identifier': user.username, 'code': '00000000',
                   'new_password1': 'NewSecurePass!2026', 'new_password2': 'NewSecurePass!2026'}
        if recovery.code() == payload['code']:
            payload['code'] = '11111111'
        for _ in range(5):
            self.assertEqual(self.client.post(reverse('password_code'), payload).status_code, 200)
        recovery.refresh_from_db()
        self.assertEqual(recovery.attempts, 5)
        payload['code'] = recovery.code()
        self.assertEqual(self.client.post(reverse('password_code'), payload).status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password('OriginalPass!2026'))
        recovery.attempts = 0
        recovery.expires_at = timezone.now() - timedelta(seconds=1)
        recovery.save(update_fields=['attempts', 'expires_at'])
        self.assertEqual(self.client.post(reverse('password_code'), payload).status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password('OriginalPass!2026'))

    @override_settings(
        DICOM_EMAIL_ENABLED=True,
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        DEFAULT_FROM_EMAIL='noreply@3ddx.com',
    )
    def test_configured_sender_emails_code(self):
        user = get_user_model().objects.create_user('emailuser', 'emailuser@3ddx.com', 'OriginalPass!2026')
        self.client.post(reverse('forgot_password'), {'identifier': user.email})
        recovery = PasswordRecoveryCode.objects.get(user=user)
        self.assertTrue(recovery.delivered_by_email)
        self.assertEqual(mail.outbox[0].to, [user.email])
        self.assertIn(recovery.code(), mail.outbox[0].body)


class ExtractedDicomMetadataTests(TestCase):
    def test_reads_patient_name_from_written_dicom_slice(self):
        with TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            file_meta = FileMetaDataset()
            file_meta.MediaStorageSOPClassUID = CTImageStorage
            file_meta.MediaStorageSOPInstanceUID = generate_uid()
            file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            dataset = FileDataset(
                str(output_dir / 'IMG0001'), {},
                file_meta=file_meta, preamble=b'\0' * 128,
            )
            dataset.PatientName = 'Smith^Jane'
            dataset.save_as(output_dir / 'IMG0001', write_like_original=False)
            self.assertEqual(extracted_patient_name(output_dir, 'IMG*'), 'Smith^Jane')
            self.assertEqual(extracted_patient_name(output_dir, '*.dcm'), '')


class DicomViewerTests(TestCase):
    def _write_slice(self, path, values, z, signed=True, bits_allocated=16):
        path.parent.mkdir(parents=True, exist_ok=True)
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = CTImageStorage
        file_meta.MediaStorageSOPInstanceUID = generate_uid()
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b'\0' * 128)
        dataset.PatientName = 'Smith^Jane'
        dataset.PatientSex = 'F'
        dataset.PatientID = 'PAT-001'
        dataset.StudyDate = '20260928'
        dataset.SeriesDate = '20260927'
        dataset.Modality = 'CT'
        dataset.Manufacturer = 'Test scanner'
        dataset.SeriesDescription = 'Axial CBCT'
        dataset.Rows, dataset.Columns = values.shape
        dataset.PixelSpacing = ['0.3', '0.3']
        dataset.SliceThickness = '0.3'
        dataset.ImagePositionPatient = [0, 0, z]
        dataset.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        dataset.SamplesPerPixel = 1
        dataset.PhotometricInterpretation = 'MONOCHROME2'
        dataset.BitsAllocated = bits_allocated
        dataset.BitsStored = bits_allocated
        dataset.HighBit = bits_allocated - 1
        dataset.PixelRepresentation = int(signed)
        dataset.RescaleSlope = 1
        dataset.RescaleIntercept = -1024
        dataset.WindowCenter = 200
        dataset.WindowWidth = 1000
        dtype = ('<i2' if signed else '<u2') if bits_allocated == 16 else ('i1' if signed else 'u1')
        dataset.PixelData = values.astype(dtype).tobytes()
        dataset.save_as(path, write_like_original=False)
        return dataset.PixelData

    def test_all_three_completed_series_open_and_serve_pixels(self):
        with TemporaryDirectory() as temporary, override_settings(EXTRACTION_OUTPUT_DIR=temporary):
            root = Path(temporary)
            jobs = [
                ('co-diagnostix', ExtractionJob.objects.create(
                    status='completed', matrix_slices=2, output_dir_name='cdx-test',
                    cdx_file='x.cdxPatient', cache_file='x.cache',
                ), root / 'cdx-test' / 'IMG0001', 'job_detail'),
                ('real-guide', RealGuideJob.objects.create(
                    status='completed', matrix_slices=2, output_dir_name='rg-test',
                    project_zip='x.zip',
                ), root / 'rg-test' / 'DICOM' / 'CT_00001.dcm', 'real_guide_job_detail'),
                ('future-case', FutureCaseJob.objects.create(
                    status='completed', matrix_slices=2, source_file='x.inv',
                ), root / 'future_case' / 'unused' / 'IM_0001.dcm', 'future_case_job_detail'),
            ]
            jobs[2] = (jobs[2][0], jobs[2][1], root / 'future_case' / str(jobs[2][1].id) / 'DICOM_SERIES' / 'IM_0001.dcm', jobs[2][3])
            self.client.force_login(get_user_model().objects.get(username='admin'))
            for kind, job, path, detail_name in jobs:
                with self.subTest(kind=kind):
                    first_values = np.array([[1, 2, 3], [4, 5, 6]], dtype='<i2')
                    second_values = np.array([[7, 8, 9], [10, 11, 12]], dtype='<i2')
                    descending_z = kind == 'real-guide'
                    expected = self._write_slice(path, first_values, 1 if descending_z else 0)
                    second_name = {
                        'co-diagnostix': 'IMG0002',
                        'real-guide': 'CT_00002.dcm',
                        'future-case': 'IM_0002.dcm',
                    }[kind]
                    self._write_slice(path.parent / second_name, second_values, 0 if descending_z else 1)
                    viewer_url = reverse('dicom_viewer', args=[kind, job.id])
                    response = self.client.get(viewer_url)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context['viewer_config']['superiorLast'], not descending_z)
                    self.assertEqual(len(response.context['metadata']), 11)
                    self.assertEqual(response.context['metadata'][0][2], 'Smith^Jane')
                    self.assertIn(('Study date', '0008,0020', '2026-09-28'), response.context['metadata'])
                    self.assertIn(('Series date', '0008,0021', '2026-09-27'), response.context['metadata'])
                    self.assertIn(('Sex (DICOM)', 'F'), response.context['summary'])
                    self.assertIn(('Study date', '2026-09-28'), response.context['summary'])
                    self.assertContains(response, 'Top DICOM metadata')
                    self.assertNotContains(response, 'aria-label="DICOM metadata"')
                    self.assertContains(response, 'Sex (DICOM)')
                    self.assertContains(response, 'Coronal')
                    self.assertContains(response, 'Sagittal')
                    self.assertEqual(
                        self.client.get(reverse('viewer_slice', args=[kind, job.id, 0])).content,
                        expected,
                    )
                    coronal = self.client.get(reverse('viewer_mpr_slice', args=[kind, job.id, 'coronal', 1]))
                    self.assertEqual(coronal.status_code, 200)
                    self.assertEqual(coronal['X-Image-Rows'], '2')
                    self.assertEqual(coronal['X-Image-Columns'], '3')
                    expected_coronal = [[4, 5, 6], [10, 11, 12]] if descending_z else [[10, 11, 12], [4, 5, 6]]
                    np.testing.assert_array_equal(
                        np.frombuffer(coronal.content, dtype='<i2').reshape(2, 3), expected_coronal,
                    )
                    sagittal = self.client.get(reverse('viewer_mpr_slice', args=[kind, job.id, 'sagittal', 2]))
                    self.assertEqual(sagittal.status_code, 200)
                    self.assertEqual(sagittal['X-Image-Rows'], '2')
                    self.assertEqual(sagittal['X-Image-Columns'], '2')
                    expected_sagittal = [[3, 6], [9, 12]] if descending_z else [[9, 12], [3, 6]]
                    np.testing.assert_array_equal(
                        np.frombuffer(sagittal.content, dtype='<i2').reshape(2, 2), expected_sagittal,
                    )
                    self.assertEqual(
                        self.client.get(reverse('viewer_slice', args=[kind, job.id, 2])).status_code,
                        404,
                    )
                    self.assertRedirects(self.client.get(reverse(detail_name, args=[job.id])), viewer_url)
                    self.assertEqual(self.client.get(reverse(detail_name, args=[job.id]) + '?details=1').status_code, 200)
            listing = self.client.get(reverse('viewer_list'))
            self.assertEqual(listing.status_code, 200)
            self.assertEqual(len(listing.context['scans']), 3)

    def test_unsigned_multiplanar_pixels(self):
        with TemporaryDirectory() as temporary, override_settings(EXTRACTION_OUTPUT_DIR=temporary):
            job = FutureCaseJob.objects.create(
                status='completed', matrix_slices=2, source_file='unsigned.inv',
            )
            folder = Path(temporary) / 'future_case' / str(job.id) / 'DICOM_SERIES'
            self._write_slice(folder / 'IM_0001.dcm', np.array([[50000, 65000]], dtype='<u2'), 0, signed=False)
            self._write_slice(folder / 'IM_0002.dcm', np.array([[40000, 60000]], dtype='<u2'), 1, signed=False)
            self.client.force_login(get_user_model().objects.get(username='admin'))
            response = self.client.get(reverse('viewer_mpr_slice', args=['future-case', job.id, 'coronal', 0]))
            self.assertEqual(response.status_code, 200)
            np.testing.assert_array_equal(
                np.frombuffer(response.content, dtype='<u2').reshape(2, 2),
                [[40000, 60000], [50000, 65000]],
            )

    def test_threshold_surface_and_stl_export(self):
        with TemporaryDirectory() as temporary, override_settings(EXTRACTION_OUTPUT_DIR=temporary):
            job = ExtractionJob.objects.create(
                status='completed', matrix_slices=2, output_dir_name='surface-test',
                cdx_file='x.cdxPatient', cache_file='x.cache',
            )
            folder = Path(temporary) / 'surface-test'
            self._write_slice(folder / 'IMG0001', np.array([[1, 2, 3], [4, 5, 6]]), 0)
            self._write_slice(folder / 'IMG0002', np.array([[7, 8, 9], [10, 11, 12]]), 1)
            self.client.force_login(get_user_model().objects.get(username='admin'))
            page = self.client.get(reverse('dicom_viewer', args=['co-diagnostix', job.id]))
            for panel in ('coronal', 'axial', 'sagittal', 'surface'):
                self.assertContains(page, f'data-view="{panel}"')
            markup = page.content.decode('utf-8')
            self.assertLess(markup.index('data-view="axial"'), markup.index('data-view="surface"'))
            self.assertLess(markup.index('data-view="surface"'), markup.index('data-view="coronal"'))
            self.assertLess(markup.index('data-view="coronal"'), markup.index('data-view="sagittal"'))
            self.assertContains(page, 'Export STL')
            self.assertContains(page, 'Export OBJ')
            self.assertContains(page, 'Export binary mask (NRRD)')
            self.assertContains(page, 'class="viewer-sidebar"')
            self.assertContains(page, 'id="show-mask-overlay"')
            self.assertContains(page, 'id="mask-opacity"')
            self.assertContains(page, 'id="locator-coordinates"')
            self.assertContains(page, 'id="clear-locator"')
            self.assertContains(page, 'Shift + left click on a CT image')
            self.assertTrue(page.context['viewer_config']['superiorLast'])
            self.assertContains(page, 'Maxillofacial bone')
            self.assertContains(page, 'id="surface-smoothing"')
            self.assertLess(markup.index('viewer-top-metadata'), markup.index('id="viewer-grid"'))

            params = {'minimum': '-1020', 'maximum': '-1010', 'preset': 'custom', 'smoothing': '0.8'}
            mask_url = reverse('viewer_mask', args=['co-diagnostix', job.id])
            mask_info = self.client.get(mask_url, params)
            self.assertEqual(mask_info.status_code, 200)
            self.assertEqual(mask_info.json()['selectedVoxels'], 9)
            self.assertEqual(mask_info.json()['totalVoxels'], 12)
            mask_download = self.client.get(mask_url, {**params, 'download': '1'})
            self.assertEqual(mask_download.status_code, 200)
            header, raw_mask = b''.join(mask_download.streaming_content).split(b'\n\n', 1)
            self.assertTrue(header.startswith(b'NRRD0005\n'))
            self.assertIn(b'sizes: 3 2 2', header)
            self.assertEqual(len(raw_mask), 12)
            self.assertEqual(set(raw_mask), {0, 1})
            self.assertEqual(sum(raw_mask), 9)
            mask_plane_url = reverse('viewer_mask_slice', args=['co-diagnostix', job.id, 'axial', 0])
            self.assertEqual(
                list(self.client.get(mask_plane_url, params).content), [0, 0, 0, 1, 1, 1],
            )
            self.assertEqual(self.client.get(reverse(
                'viewer_mask_slice', args=['co-diagnostix', job.id, 'axial', 2]
            ), params).status_code, 404)

            url = reverse('viewer_surface', args=['co-diagnostix', job.id])
            mesh = self.client.get(url, params)
            self.assertEqual(mesh.status_code, 200)
            self.assertGreater(len(mesh.content), 0)
            self.assertEqual(len(mesh.content) % 72, 0)
            self.assertEqual(int(mesh['X-Mesh-Vertices']), len(mesh.content) // 24)
            self.assertEqual(self.client.get(url, {'minimum': '10', 'maximum': '0'}).status_code, 400)
            self.assertEqual(self.client.get(url, {**params, 'smoothing': '3'}).status_code, 400)
            self.assertEqual(self.client.get(url, {**params, 'smoothing': 'nan'}).status_code, 400)
            self.assertEqual(self.client.get(mask_url, {'minimum': '10', 'maximum': '0'}).status_code, 400)

            stl_url = reverse('viewer_surface_stl', args=['co-diagnostix', job.id])
            stl = self.client.get(stl_url, params)
            self.assertEqual(stl.status_code, 200)
            triangles = struct.unpack('<I', stl.content[80:84])[0]
            self.assertEqual(triangles, len(mesh.content) // 72)
            self.assertEqual(len(stl.content), 84 + triangles * 50)
            self.assertIn('attachment;', stl['Content-Disposition'])
            obj = self.client.get(reverse('viewer_surface_obj', args=['co-diagnostix', job.id]), params)
            self.assertEqual(obj.status_code, 200)
            self.assertEqual(sum(line.startswith(b'f ') for line in obj.content.splitlines()), triangles)
            self.assertIn(b'# Coordinates in millimetres', obj.content)
            self.assertEqual(self.client.get(stl_url, {
                'minimum': '5000', 'maximum': '6000', 'preset': 'custom',
            }).status_code, 422)

    def test_eight_bit_dicom_series_uses_compact_axial_transfer(self):
        with TemporaryDirectory() as temporary, override_settings(EXTRACTION_OUTPUT_DIR=temporary):
            job = ExtractionJob.objects.create(
                status='completed', matrix_slices=2, output_dir_name='eight-bit-test',
                cdx_file='x.cdxPatient', cache_file='x.cache',
            )
            folder = Path(temporary) / 'eight-bit-test'
            first = np.array([[0, 128], [255, 64]], dtype='u1')
            second = np.array([[10, 20], [30, 40]], dtype='u1')
            self._write_slice(folder / 'IMG0001', first, 0, signed=False, bits_allocated=8)
            self._write_slice(folder / 'IMG0002', second, 1, signed=False, bits_allocated=8)
            self.client.force_login(get_user_model().objects.get(username='admin'))
            viewer = self.client.get(reverse('dicom_viewer', args=['co-diagnostix', job.id]))
            self.assertEqual(viewer.status_code, 200)
            self.assertEqual(viewer.context['viewer_config']['planes']['axial']['bytesPerPixel'], 1)
            self.assertEqual(viewer.context['viewer_config']['planes']['coronal']['bytesPerPixel'], 2)
            self.assertNotContains(viewer, 'Loading DICOM slice')
            axial = self.client.get(reverse('viewer_slice', args=['co-diagnostix', job.id, 0]))
            self.assertEqual(axial.status_code, 200)
            np.testing.assert_array_equal(np.frombuffer(axial.content, dtype='u1').reshape(2, 2), first)
            coronal = self.client.get(reverse('viewer_mpr_slice', args=['co-diagnostix', job.id, 'coronal', 1]))
            self.assertEqual(coronal.status_code, 200)
            np.testing.assert_array_equal(
                np.frombuffer(coronal.content, dtype='<u2').reshape(2, 2), [[30, 40], [255, 64]],
            )
