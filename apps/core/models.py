"""Shared abstract models."""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base that records when a row was created and last changed."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Review(TimeStampedModel):
    """A real customer review, entered by staff. Only published reviews appear on the site."""

    class Source(models.TextChoices):
        GOOGLE = "google", "Google"
        FACEBOOK = "facebook", "Facebook"
        DIRECT = "direct", "Direct"

    customer_name = models.CharField(max_length=100)
    city = models.CharField(max_length=80, blank=True)
    text = models.TextField()
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.GOOGLE)
    review_date = models.DateField()
    is_published = models.BooleanField(
        default=False, help_text="Only published reviews are shown on the website."
    )

    class Meta:
        ordering = ["-review_date", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(rating__gte=1, rating__lte=5), name="review_rating_1_to_5"
            ),
        ]

    def __str__(self):
        return f"{self.customer_name} ({self.rating}/5)"
