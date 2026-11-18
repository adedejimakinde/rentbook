"""Customer reviews: staff publish real reviews; the home shelf shows only those."""

from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.core.models import Review
from apps.core.services import published_reviews

pytestmark = pytest.mark.django_db


def review(**fields):
    defaults = {
        "customer_name": "Tasha Brooks",
        "city": "Garland",
        "text": "On time and the kids loved the bounce house.",
        "rating": 5,
        "source": Review.Source.GOOGLE,
        "review_date": date(2026, 10, 3),
        "is_published": True,
    }
    return Review.objects.create(**{**defaults, **fields})


def test_home_hides_the_reviews_shelf_until_a_review_is_published(client):
    review(is_published=False, text="Draft review text")

    html = client.get(reverse("core:home")).content.decode()

    assert "What DFW families say" not in html
    assert "Draft review text" not in html


def test_home_shows_only_published_reviews_with_stars_and_source(client):
    review(customer_name="Tasha Brooks", city="Garland")
    review(customer_name="Hidden Person", is_published=False)

    html = client.get(reverse("core:home")).content.decode()

    assert "What DFW families say" in html
    assert "Tasha Brooks, Garland" in html
    assert 'aria-label="5 out of 5 stars"' in html
    assert "Google review" in html
    assert "Hidden Person" not in html


def test_published_reviews_are_newest_first_and_limited():
    older = review(review_date=date(2026, 1, 1))
    newer = review(review_date=date(2026, 6, 1))

    assert published_reviews() == [newer, older]
    assert len(published_reviews(limit=1)) == 1


@pytest.mark.parametrize("rating", [0, 6])
def test_rating_must_be_one_to_five(rating):
    with pytest.raises(ValidationError):
        Review(
            customer_name="A", text="B", rating=rating, review_date=date(2026, 1, 1)
        ).full_clean()
    with pytest.raises(IntegrityError), transaction.atomic():
        review(rating=rating)


def test_staff_manage_reviews_in_the_admin(admin_client, settings):
    review(customer_name="Tasha Brooks")

    response = admin_client.get(f"/{settings.ADMIN_URL}core/review/")

    assert response.status_code == 200
    assert "Tasha Brooks" in response.content.decode()
