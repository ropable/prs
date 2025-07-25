# Generated by Django 5.2.3 on 2025-06-25 06:37

import django.contrib.postgres.indexes
import django.contrib.postgres.search
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("referral", "0005_userprofile_referral_history_array"),
        ("taggit", "0006_rename_taggeditem_content_type_object_id_taggit_tagg_content_8fc721_idx"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="condition",
            name="search_document",
            field=models.TextField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="condition",
            name="search_vector",
            field=django.contrib.postgres.search.SearchVectorField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="note",
            name="search_document",
            field=models.TextField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="note",
            name="search_vector",
            field=django.contrib.postgres.search.SearchVectorField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="record",
            name="search_document",
            field=models.TextField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="record",
            name="search_vector",
            field=django.contrib.postgres.search.SearchVectorField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="referral",
            name="search_document",
            field=models.TextField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="referral",
            name="search_vector",
            field=django.contrib.postgres.search.SearchVectorField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="task",
            name="search_document",
            field=models.TextField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="task",
            name="search_vector",
            field=django.contrib.postgres.search.SearchVectorField(editable=False, null=True),
        ),
        migrations.AddIndex(
            model_name="condition",
            index=django.contrib.postgres.indexes.GinIndex(fields=["search_vector"], name="idx_condition_search_vector"),
        ),
        migrations.AddIndex(
            model_name="note",
            index=django.contrib.postgres.indexes.GinIndex(fields=["search_vector"], name="idx_note_search_vector"),
        ),
        migrations.AddIndex(
            model_name="record",
            index=django.contrib.postgres.indexes.GinIndex(fields=["search_vector"], name="idx_record_search_vector"),
        ),
        migrations.AddIndex(
            model_name="referral",
            index=django.contrib.postgres.indexes.GinIndex(fields=["search_vector"], name="idx_referral_search_vector"),
        ),
        migrations.AddIndex(
            model_name="task",
            index=django.contrib.postgres.indexes.GinIndex(fields=["search_vector"], name="idx_task_search_vector"),
        ),
        # Create functions and triggers to update the model search_vector fields on save.
        migrations.RunSQL("""CREATE OR REPLACE FUNCTION fnc_search_vector_update()
RETURNS TRIGGER AS $$
BEGIN
    NEW.search_vector := to_tsvector('english', COALESCE(NEW.search_document, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;"""),
        migrations.RunSQL("""CREATE TRIGGER referral_referral_after_update_trigger
BEFORE INSERT OR UPDATE ON referral_referral
FOR EACH ROW EXECUTE FUNCTION fnc_search_vector_update();"""),
        migrations.RunSQL("""CREATE TRIGGER referral_task_after_update_trigger
BEFORE INSERT OR UPDATE ON referral_task
FOR EACH ROW EXECUTE FUNCTION fnc_search_vector_update();"""),
        migrations.RunSQL("""CREATE TRIGGER referral_record_after_update_trigger
BEFORE INSERT OR UPDATE ON referral_record
FOR EACH ROW EXECUTE FUNCTION fnc_search_vector_update();"""),
        migrations.RunSQL("""CREATE TRIGGER referral_note_after_update_trigger
BEFORE INSERT OR UPDATE ON referral_note
FOR EACH ROW EXECUTE FUNCTION fnc_search_vector_update();"""),
        migrations.RunSQL("""CREATE TRIGGER referral_condition_after_update_trigger
BEFORE INSERT OR UPDATE ON referral_condition
FOR EACH ROW EXECUTE FUNCTION fnc_search_vector_update();"""),
    ]
