"""Template filters for documents and emails."""

from decimal import Decimal, InvalidOperation

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def usd(value):
    """A money value ("1234.5", Decimal) as "$1,234.50"; negative as "-$1,234.50"."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


@register.filter
def nonzero(value):
    """True when a money value is above zero."""
    try:
        return Decimal(str(value)) > 0
    except (InvalidOperation, TypeError, ValueError):
        return False


@register.filter
def css_string(value):
    """Text made safe to put inside a double-quoted CSS string."""
    escaped = str(value).replace("\\", "\\5c ").replace('"', "\\22 ").replace("<", "\\3c ")
    return mark_safe(escaped.replace("\n", " "))  # noqa: S308  every special character is escaped
