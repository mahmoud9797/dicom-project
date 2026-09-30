from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import migrations


def set_initial_admin(apps, schema_editor):
    app_label, model_name = settings.AUTH_USER_MODEL.split('.')
    User = apps.get_model(app_label, model_name)
    manager = User.objects.using(schema_editor.connection.alias)
    if not manager.filter(username='admin').exists():
        manager.create(
            username='admin',
            password=make_password(None),
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )


class Migration(migrations.Migration):
    dependencies = [
        ('extractor', '0005_futurecasejob_patient_name_optional'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [migrations.RunPython(set_initial_admin, migrations.RunPython.noop)]
