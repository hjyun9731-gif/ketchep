from __future__ import annotations

import calendar
import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from dateutil.relativedelta import relativedelta
from django.utils import timezone


def normalize_text(value: Any) -> str:
    if value is None:
        return ''
    text = str(value).strip()
    text = re.sub(r'\s+', '', text)
    return text.lower()


def normalize_header(value: Any) -> str:
    text = normalize_text(value)
    return re.sub(r'[^0-9a-z가-힣]', '', text)


def normalize_vehicle_no(value: Any) -> str:
    if value is None:
        return ''
    return re.sub(r'[^0-9가-힣]', '', str(value)).upper()


def extract_purpose_char(vehicle_no: str) -> str:
    normalized = normalize_vehicle_no(vehicle_no)
    match = re.search(r'([가-힣])\d{4}$', normalized)
    return match.group(1) if match else ''


def add_month_same_day(value: date, months: int = 1) -> date:
    target = value + relativedelta(months=months)
    # relativedelta already clamps to month end. Reconstruct to preserve original day
    last_day = calendar.monthrange(target.year, target.month)[1]
    return date(target.year, target.month, min(value.day, last_day))


def month_last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def parse_decimal(value: Any) -> Decimal | None:
    if value in (None, ''):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().replace(',', '').replace('원', '')
    text = re.sub(r'[^0-9.\-]', '', text)
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_date(value: Any, *, default_year: int | None = None) -> date | None:
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        # Excel serial date, Windows epoch.
        try:
            return date(1899, 12, 30) + relativedelta(days=int(value))
        except Exception:
            return None
    text = str(value).strip()
    text = text.replace('년', '-').replace('월', '-').replace('일', '')
    text = re.sub(r'[./]', '-', text)
    text = re.sub(r'\s+', '', text)
    # Legacy sheets commonly store dates as ``18.10.24.`` or ``13. 6. 3.``.
    # Converting dots to hyphens leaves a trailing separator, so trim and
    # collapse separators before parsing.
    text = re.sub(r'-+', '-', text).strip('-')
    for fmt in ('%Y-%m-%d', '%y-%m-%d', '%Y-%m', '%y-%m', '%Y%m%d', '%y%m%d'):
        try:
            parsed = datetime.strptime(text, fmt).date()
            if fmt in ('%Y-%m', '%y-%m'):
                parsed = parsed.replace(day=1)
            return parsed
        except ValueError:
            pass
    match = re.fullmatch(r'(\d{1,2})-(\d{1,2})', text)
    if match and default_year:
        try:
            return date(default_year, int(match.group(1)), int(match.group(2)))
        except ValueError:
            return None
    return None


def parse_datetime(value: Any, *, default_year: int | None = None):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time())
    else:
        text = str(value).strip().replace('.', '-').replace('/', '-')
        result = None
        for fmt in (
            '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%y-%m-%d %H:%M:%S',
            '%y-%m-%d %H:%M', '%Y-%m-%d', '%y-%m-%d',
        ):
            try:
                result = datetime.strptime(text, fmt)
                break
            except ValueError:
                pass
        if result is None:
            d = parse_date(value, default_year=default_year)
            if d:
                result = datetime.combine(d, datetime.min.time())
    if result is None:
        return None
    if timezone.is_naive(result):
        return timezone.make_aware(result, timezone.get_current_timezone())
    return result


def sha256_file(file_obj) -> str:
    digest = hashlib.sha256()
    for chunk in file_obj.chunks() if hasattr(file_obj, 'chunks') else iter(lambda: file_obj.read(1024 * 1024), b''):
        digest.update(chunk)
    if hasattr(file_obj, 'seek'):
        file_obj.seek(0)
    return digest.hexdigest()


def stable_hash(data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def json_safe_value(value: Any):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat(sep=' ')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    if isinstance(value, dict):
        return {str(k): json_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe_value(v) for v in value]
    return str(value)


def json_safe_model(instance, fields: list[str] | None = None) -> dict:
    if instance is None:
        return {}
    data = {}
    for field in instance._meta.concrete_fields:
        if fields and field.name not in fields:
            continue
        value = getattr(instance, field.attname)
        if isinstance(value, (date, datetime, Decimal)):
            value = str(value)
        data[field.name] = value
    return data
