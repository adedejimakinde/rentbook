"""Image upload processing: renditions, orientation, metadata and the placeholder tag."""

from unittest import mock

import pytest
from django.template import Context, Template
from PIL import Image

from apps.bookings.tests.factories import CategoryFactory, ItemFactory
from apps.catalogue import tasks
from apps.catalogue.models import ItemImage
from apps.catalogue.services import images

from .conftest import make_jpeg

pytestmark = pytest.mark.django_db


def stored_files(media_root):
    return [path for path in media_root.rglob("*") if path.is_file()]


def make_photo(upload, **kwargs):
    item = ItemFactory(name="Chiavari chair")
    photo = ItemImage.objects.create(item=item, image=upload(**kwargs), alt_text="Gold chairs")
    tasks.process_image(photo._meta.label_lower, photo.pk)
    photo.refresh_from_db()
    return photo


def render_picture(photo, **extra):
    template = Template("{% load catalogue_media %}{% picture photo name='Chiavari chair' %}")
    return template.render(Context({"photo": photo, **extra}))


def test_renditions_cover_three_widths_and_three_formats(upload):
    photo = make_photo(upload, size=(3000, 1500))

    combos = {(r["format"], r["width"]) for r in photo.renditions}
    assert combos == {(f, w) for f in ("avif", "webp", "jpeg") for w in (400, 800, 1200)}
    assert photo.renditions_ready
    for rendition in photo.renditions:
        with Image.open(photo.image.storage.path(rendition["name"])) as stored:
            assert stored.size == (rendition["width"], rendition["height"])
            assert rendition["height"] == round(1500 * rendition["width"] / 3000)


def test_small_images_are_never_upscaled(upload):
    photo = make_photo(upload, size=(300, 200))

    assert {r["width"] for r in photo.renditions} == {300}
    assert (photo.image_width, photo.image_height) == (300, 200)


def test_original_is_downscaled_to_2400_on_the_long_side(upload):
    photo = make_photo(upload, size=(3600, 1800))

    assert (photo.image_width, photo.image_height) == (2400, 1200)
    with Image.open(photo.image.path) as original:
        assert original.size == (2400, 1200)


def test_exif_orientation_is_applied(upload):
    photo = make_photo(upload, size=(300, 100), orientation=6)

    assert (photo.image_width, photo.image_height) == (100, 300)
    with Image.open(photo.image.path) as original:
        assert original.size == (100, 300)


def test_no_exif_or_gps_remains_in_any_stored_file(upload, media_root):
    source = make_jpeg(size=(1500, 1000), orientation=6, gps=True)
    with Image.open(__import__("io").BytesIO(source)) as check:
        assert check.getexif().get_ifd(0x8825), "test image must start with GPS data"

    make_photo(upload, size=(1500, 1000), orientation=6, gps=True)

    files = stored_files(media_root)
    assert len(files) == 1 + 3 * 3
    for path in files:
        with Image.open(path) as stored:
            assert len(stored.getexif()) == 0, path.name
            assert "exif" not in stored.info, path.name
            assert not stored.getexif().get_ifd(0x8825), path.name


def test_sanitise_drops_png_text_and_keeps_transparency():
    source = Image.new("RGBA", (10, 10), (255, 0, 0, 128))
    source.info["Comment"] = "secret"

    clean = images.sanitise(source)

    assert clean.mode == "RGBA"
    assert clean.info == {}


def test_placeholder_is_small_data_uri(upload):
    photo = make_photo(upload)

    assert photo.placeholder.startswith("data:image/jpeg;base64,")
    assert len(photo.placeholder) < 1500


def test_reprocessing_replaces_old_renditions(upload, media_root):
    photo = make_photo(upload)
    before = len(stored_files(media_root))

    tasks.process_image(photo._meta.label_lower, photo.pk)

    assert len(stored_files(media_root)) == before


def test_upload_queues_task_and_tag_switches_from_placeholder_to_renditions(
    upload, django_capture_on_commit_callbacks
):
    item = ItemFactory(name="Chiavari chair")
    with (
        mock.patch("apps.catalogue.signals.process_image.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        photo = ItemImage.objects.create(item=item, image=upload(), alt_text="Gold chairs")
    delay.assert_called_once_with("catalogue.itemimage", photo.pk)

    before = render_picture(ItemImage.objects.get(pk=photo.pk))
    assert "<picture>" not in before
    assert "Chiavari chair" in before

    tasks.process_image("catalogue.itemimage", photo.pk)

    after = render_picture(ItemImage.objects.get(pk=photo.pk))
    assert "<picture>" in after
    assert 'type="image/avif"' in after
    assert 'type="image/webp"' in after
    assert "400w" in after and "1200w" in after
    assert 'width="800"' in after and 'height="400"' in after
    assert 'loading="lazy"' in after
    assert 'alt="Gold chairs"' in after
    assert "data:image/jpeg;base64," in after


def test_eager_images_are_not_lazy(upload):
    photo = make_photo(upload)
    template = Template("{% load catalogue_media %}{% picture photo eager=True %}")

    assert 'loading="eager"' in template.render(Context({"photo": photo}))


def test_missing_image_renders_a_tinted_placeholder_block():
    html = Template("{% load catalogue_media %}{% picture None name='Water slide' %}").render(
        Context()
    )

    assert "bg-surface-tint" in html and 'data-placeholder="Water slide"' in html
    assert "aspect-ratio:4/3" in html
    assert "<img" not in html


def test_replacing_the_upload_marks_renditions_stale_and_requeues(
    upload, django_capture_on_commit_callbacks
):
    photo = make_photo(upload)
    photo = ItemImage.objects.get(pk=photo.pk)
    photo.image = upload(name="new.jpg")
    with (
        mock.patch("apps.catalogue.signals.process_image.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        photo.save()

    delay.assert_called_once()
    assert ItemImage.objects.get(pk=photo.pk).renditions_ready is False


def test_saving_without_changing_the_file_does_not_requeue(upload):
    photo = make_photo(upload)
    photo = ItemImage.objects.get(pk=photo.pk)
    photo.alt_text = "Updated"
    with mock.patch("apps.catalogue.signals.process_image.delay") as delay:
        photo.save()

    delay.assert_not_called()
    assert ItemImage.objects.get(pk=photo.pk).renditions_ready is True


def test_deleting_a_photo_removes_its_files(upload, media_root):
    photo = make_photo(upload)

    photo.delete()

    assert stored_files(media_root) == []


def test_category_images_are_processed_too(upload):
    category = CategoryFactory(image=upload())

    tasks.process_image("catalogue.category", category.pk)

    category.refresh_from_db()
    assert category.renditions_ready
    assert {r["format"] for r in category.renditions} == {"avif", "webp", "jpeg"}


def test_only_one_primary_image_per_item(upload):
    item = ItemFactory()
    ItemImage.objects.create(item=item, image=upload(), alt_text="a", is_primary=True)

    with pytest.raises(Exception, match="one_primary_image_per_item"):
        ItemImage.objects.create(
            item=item, image=upload(name="b.jpg"), alt_text="b", is_primary=True
        )


def test_upload_is_stripped_of_exif_and_gps_before_the_task_runs(upload, media_root):
    item = ItemFactory(name="Chiavari chair")
    with mock.patch.object(tasks.process_image, "delay") as queued:
        photo = ItemImage.objects.create(
            item=item, image=upload(size=(1200, 800), orientation=6, gps=True), alt_text="x"
        )

    files = stored_files(media_root)
    assert len(files) == 1, "only the original exists before renditions"
    with Image.open(files[0]) as stored:
        assert len(stored.getexif()) == 0
        assert not stored.getexif().get_ifd(0x8825)
        assert stored.size == (800, 1200), "orientation applied before stripping"
    assert photo.renditions_ready is False
    queued.assert_not_called()  # still queued on commit, outside this test transaction


def test_category_upload_is_also_stripped(upload, media_root):
    CategoryFactory(image=upload(size=(600, 400), gps=True))

    (stored_path,) = stored_files(media_root)
    with Image.open(stored_path) as stored:
        assert len(stored.getexif()) == 0
