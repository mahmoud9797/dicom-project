from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('extractor', '0004_extractionjob_entered_patient_id_and_more')]

    operations = [
        migrations.AlterField(
            model_name='futurecasejob',
            name='patient_name',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
    ]
