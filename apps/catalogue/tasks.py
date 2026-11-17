"""Background jobs for the catalogue."""

from celery import shared_task
from django.apps import apps

from apps.catalogue.services import images
from apps.core.services import clear_section_flags


@shared_task(
    name="catalogue.process_image",
    ignore_result=True,
    autoretry_for=(OSError,),
    retry_backoff=True,
    max_retries=3,
)
def process_image(model_label, pk):
    """Generate renditions for one ItemImage or Category image ("catalogue.itemimage")."""
    model = apps.get_model(model_label)
    instance = model.objects.filter(pk=pk).first()
    if instance is not None:
        images.process_image(instance)
        clear_section_flags()
