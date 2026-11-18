"""Template helpers for the staff screens."""

from django import template

from apps.dashboard.services.format import dollars as format_dollars

register = template.Library()


@register.filter
def dollars(value):
    """$8,450 for a whole amount, $8,450.50 when there are cents."""
    return format_dollars(value)


@register.filter
def tel(phone):
    """A phone number reduced to what a tel: link needs."""
    return "".join(char for char in str(phone) if char.isdigit() or char == "+")
