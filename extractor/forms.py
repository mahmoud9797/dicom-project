from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm


class AdminUserCreationForm(UserCreationForm):
    email = forms.EmailField(label='3ddx email address', max_length=254)

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ('username', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'
        for name in ('password1', 'password2'):
            self.fields[name].widget.attrs['data-password-toggle'] = True

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if '@' in username:
            raise forms.ValidationError('Use a username without @; enter the email separately.')
        if get_user_model().objects.filter(username__iexact=username).exists():
            raise forms.ValidationError('This username is already in use.')
        return username

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if email.rsplit('@', 1)[-1] != '3ddx.com':
            raise forms.ValidationError('Use an email address ending in @3ddx.com.')
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('This email address is already in use.')
        return email


class ForgotPasswordForm(forms.Form):
    identifier = forms.CharField(
        label='Username or 3ddx email address', max_length=254,
        widget=forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'username'}),
    )


class PasswordRecoveryForm(forms.Form):
    identifier = forms.CharField(
        label='Username or 3ddx email address', max_length=254,
        widget=forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'username'}),
    )
    code = forms.RegexField(
        regex=r'^\d{8}$', label='8-digit recovery code',
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'inputmode': 'numeric',
            'autocomplete': 'one-time-code', 'maxlength': '8',
        }),
    )
    new_password1 = forms.CharField(
        label='New password', widget=forms.PasswordInput(attrs={
            'class': 'form-control', 'autocomplete': 'new-password',
            'data-password-toggle': True,
        }),
    )
    new_password2 = forms.CharField(
        label='Confirm new password', widget=forms.PasswordInput(attrs={
            'class': 'form-control', 'autocomplete': 'new-password',
            'data-password-toggle': True,
        }),
    )

    def clean(self):
        cleaned = super().clean()
        if (cleaned.get('new_password1') and cleaned.get('new_password2')
                and cleaned['new_password1'] != cleaned['new_password2']):
            self.add_error('new_password2', 'Passwords do not match.')
        return cleaned


class PatientIdFormMixin(forms.Form):
    entered_patient_id = forms.CharField(
        label='Patient ID',
        max_length=64,
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'autocomplete': 'off',
            'placeholder': 'Enter patient ID for this extraction',
        }),
    )
    def clean_entered_patient_id(self):
        patient_id = self.cleaned_data['entered_patient_id'].strip()
        if not patient_id or '\\' in patient_id or any(ord(char) < 32 for char in patient_id):
            raise forms.ValidationError('Enter a valid patient ID without backslashes or control characters.')
        return patient_id


class UploadForm(PatientIdFormMixin):
    """Form for uploading .cdxPatient and .cache files."""

    cdx_file = forms.FileField(
        label='coDiagnostiX Patient Database (.cdxPatient)',
        help_text='Select the .cdxPatient SQLite database file',
        widget=forms.ClearableFileInput(attrs={
            'accept': '.cdxPatient,.cdxpatient',
            'class': 'form-control',
            'id': 'cdx-file-input',
        }),
    )
    cache_file = forms.FileField(
        label='Cache File (.cache)',
        help_text='Select the matching .cache file',
        widget=forms.ClearableFileInput(attrs={
            'accept': '.cache',
            'class': 'form-control',
            'id': 'cache-file-input',
        }),
    )
    fix_left_right = forms.BooleanField(
        required=False,
        initial=True,
        label='Fix left/right mirror',
        help_text='Reverse columns in every slice to correct horizontal mirror. Recommended.',
        widget=forms.CheckboxInput(attrs={
            'class': 'form-check-input',
            'id': 'fix-lr-check',
        }),
    )

    def clean_cdx_file(self):
        f = self.cleaned_data['cdx_file']
        if not f.name.lower().endswith('.cdxpatient'):
            raise forms.ValidationError('File must have a .cdxPatient extension.')
        return f

    def clean_cache_file(self):
        f = self.cleaned_data['cache_file']
        if not f.name.lower().endswith('.cache'):
            raise forms.ValidationError('File must have a .cache extension.')
        return f

class RealGuideUploadForm(PatientIdFormMixin):
    project_zip = forms.FileField(
        label='RealGUIDE Project ZIP (.zip)',
        widget=forms.ClearableFileInput(attrs={'accept': '.zip', 'class': 'form-control'})
    )
    allow_assumed_orientation = forms.BooleanField(
        required=True,
        label='I understand: orientation is assumed; output needs verification.',
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'})
    )

    def clean_project_zip(self):
        source = self.cleaned_data['project_zip']
        if not source.name.lower().endswith('.zip'):
            raise forms.ValidationError('File must have a .zip extension.')
        return source


class FutureCaseUploadForm(PatientIdFormMixin):
    source_file = forms.FileField(
        label='Future Case file (.inv, .exe, or .zip)',
        widget=forms.ClearableFileInput(attrs={
            'accept': '.inv,.exe,.zip', 'class': 'form-control',
        }),
    )
    reverse_z = forms.BooleanField(
        required=False, label='Reverse slice order (Z)',
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )
    create_zip_packages = forms.BooleanField(
        required=False, initial=True, label='Create DICOM and NIfTI ZIP packages',
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )

    def clean_source_file(self):
        source = self.cleaned_data['source_file']
        if not source.name.lower().endswith(('.inv', '.exe', '.zip')):
            raise forms.ValidationError('Choose a .inv, .exe, or .zip file.')
        return source
