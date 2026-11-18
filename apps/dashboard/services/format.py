"""Money as the staff screens show it: whole dollars unless there are cents."""

from decimal import Decimal


def dollars(value):
    """$8,450 for a whole amount, $8,450.50 when there are cents."""
    amount = Decimal(value or 0)
    if amount == amount.to_integral_value():
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"
