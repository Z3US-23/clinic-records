from django import template

register = template.Library()


@register.filter
def rx_details(item):
    """'1 tablet · 1+0+1 · 5 days' — a prescription line's dose, frequency and duration, skipping blanks."""
    parts = [item.dose, item.frequency, item.duration]
    return " · ".join(part for part in parts if part)
