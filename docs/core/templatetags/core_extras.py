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
    if value in {'review', 'duplicate', 'partial', 'pending', 'scheduled', 'accepted', 'sending', 'modified', 'needs_mapping', 'absent'}:
        return 'warning'
    return 'neutral'


@register.filter
def phone_format(value):
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) == 11:
        return f'{digits[:3]}-{digits[3:7]}-{digits[7:]}'
    if len(digits) == 10:
        if digits.startswith('02'):
            return f'{digits[:2]}-{digits[2:6]}-{digits[6:]}'
        return f'{digits[:3]}-{digits[3:6]}-{digits[6:]}'
    return str(value or '')


@register.filter
def birth6_format(value):
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())[:6]
    if len(digits) == 6:
        return f'{digits[:2]}.{digits[2:4]}.{digits[4:]}'
    return digits


@register.filter
def short_key(value):
    value = str(value or '').strip()
    if not value:
        return '-'
    return value if len(value) <= 14 else value[:13] + '…'
