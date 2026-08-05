from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def money(value):
    try:
        return f'{Decimal(value):,.0f}'
    except (InvalidOperation, TypeError, ValueError):
        return '0'


@register.filter
def dict_get(mapping, key):
    if not mapping:
        return None
    return mapping.get(key)


@register.filter
def status_class(value):
    value = str(value or '')
    if value in {'final', 'sent', 'completed', 'processed', 'parsed', 'auto_matched', 'manual_matched', 'matched', 'posted', 'delivered'}:
        return 'success'
    if value in {'failed', 'cancelled', 'closed', 'returned', 'unknown_recipient', 'unknown_address'}:
        return 'danger'
    if value in {'review', 'duplicate', 'partial', 'pending', 'scheduled', 'modified', 'needs_mapping', 'absent'}:
        return 'warning'
    return 'neutral'
