from django.urls import path
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.views.decorators.http import require_POST
from . import accounts, views, viewer

urlpatterns = [
    path('login/', LoginView.as_view(template_name='extractor/login.html', redirect_authenticated_user=True), name='login'),
    path('logout/', require_POST(LogoutView.as_view()), name='logout'),
    path('users/new/', accounts.register_user, name='register_user'),
    path('users/recovery/', accounts.recovery_codes, name='recovery_codes'),
    path('password/forgot/', accounts.forgot_password, name='forgot_password'),
    path('password/code/', accounts.password_code, name='password_code'),
    path('', login_required(views.index), name='index'),
    path('co-diagnostix/', login_required(views.co_diagnostix_index), name='co_diagnostix_index'),
    path('jobs/', login_required(views.job_list), name='job_list'),
    path('jobs/<uuid:job_id>/', login_required(views.job_detail), name='job_detail'),
    path('api/jobs/<uuid:job_id>/status/', login_required(views.job_status_api), name='job_status_api'),
    path('jobs/<uuid:job_id>/download/zip/', login_required(views.download_zip), name='download_zip'),
    path('jobs/<uuid:job_id>/download/report/', login_required(views.download_report), name='download_report'),
    path('jobs/<uuid:job_id>/delete/', login_required(views.delete_job), name='delete_job'),
    path('about/', login_required(views.about), name='about'),
    path('viewer/', login_required(viewer.viewer_list), name='viewer_list'),
    path('viewer/<str:kind>/<uuid:job_id>/', login_required(viewer.dicom_viewer), name='dicom_viewer'),
    path('viewer/<str:kind>/<uuid:job_id>/slice/<int:index>/', login_required(viewer.viewer_slice), name='viewer_slice'),
    path('viewer/<str:kind>/<uuid:job_id>/surface/', login_required(viewer.viewer_surface), name='viewer_surface'),
    path('viewer/<str:kind>/<uuid:job_id>/surface/stl/', login_required(viewer.viewer_surface_stl), name='viewer_surface_stl'),
    path('viewer/<str:kind>/<uuid:job_id>/surface/obj/', login_required(viewer.viewer_surface_obj), name='viewer_surface_obj'),
    path('viewer/<str:kind>/<uuid:job_id>/mask/', login_required(viewer.viewer_mask), name='viewer_mask'),
    path('viewer/<str:kind>/<uuid:job_id>/mask/<str:plane>/<int:index>/', login_required(viewer.viewer_mask_slice), name='viewer_mask_slice'),
    path('viewer/<str:kind>/<uuid:job_id>/<str:plane>/<int:index>/', login_required(viewer.viewer_mpr_slice), name='viewer_mpr_slice'),

    path('real-guide/', login_required(views.real_guide_index), name='real_guide_index'),
    path('real-guide/jobs/<uuid:job_id>/', login_required(views.real_guide_job_detail), name='real_guide_job_detail'),
    path('api/real-guide/jobs/<uuid:job_id>/status/', login_required(views.real_guide_status_api), name='real_guide_status_api'),
    path('real-guide/jobs/<uuid:job_id>/download/zip/', login_required(views.rg_download_zip), name='rg_download_zip'),
    path('real-guide/jobs/<uuid:job_id>/download/report/', login_required(views.rg_download_report), name='rg_download_report'),
    path('future-case/', login_required(views.future_case_index), name='future_case_index'),
    path('future-case/jobs/<uuid:job_id>/', login_required(views.future_case_job_detail), name='future_case_job_detail'),
    path('api/future-case/jobs/<uuid:job_id>/status/', login_required(views.future_case_status_api), name='future_case_status_api'),
    path('future-case/jobs/<uuid:job_id>/download/<str:kind>/', login_required(views.future_case_download), name='future_case_download'),
]
