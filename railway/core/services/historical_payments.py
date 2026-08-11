from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from django.db import transaction

from core.models import AccountType, HistoricalPaymentRecord, Member, UploadedFile, Vehicle
from core.utils import normalize_header, normalize_text, normalize_vehicle_no, parse_date, parse_decimal


def _person_name(value) -> str:
    return re.sub(r'\s+', '', str(value or '')).strip()


def _account_type(value, member: Member) -> str:
    text = normalize_text(value)
    if '협회' in text:
        return AccountType.MEMBERSHIP_FEE
    if '관리' in text:
        return AccountType.MANAGEMENT_FEE
    return member.receivable_account_type or (
        AccountType.MEMBERSHIP_FEE
        if member.membership_status == Member.MembershipStatus.ACTIVE
        else AccountType.MANAGEMENT_FEE
    )


def _raw_value(raw: dict, normalized_key: str):
    for key, value in (raw or {}).items():
        if str(key).startswith('__'):
            continue
        if normalize_header(key) == normalized_key:
            return value
    return None


def _monthly_amount(raw: dict, month: int):
    return _raw_value(raw, f'{month}월입금액')


def _monthly_date(raw: dict, month: int):
    # Duplicate headers are stored as 입금날짜, 입금날짜_2 ... by the parser.
    # normalize_header removes the underscore, yielding 입금날짜2 etc.
    normalized = '입금날짜' if month == 1 else f'입금날짜{month}'
    value = _raw_value(raw, normalized)
    if value not in (None, ''):
        return value
    # Some files explicitly include the month in the date header.
    return _raw_value(raw, f'{month}월입금날짜')


def _date_text(value, *, year: int):
    if value in (None, ''):
        return '', None
    parsed = parse_date(value, default_year=year)
    if parsed:
        return parsed.isoformat(), parsed
    text = str(value).strip()
    if '#' in text:
        return '원본 날짜 확인불가', None
    return text[:120], None


def _unique(items):
    result = []
    seen = set()
    for item in items:
        if item.id not in seen:
            result.append(item)
            seen.add(item.id)
    return result


def _member_indexes():
    members = list(Member.objects.filter(is_active_record=True).prefetch_related('vehicles'))
    by_exact_vehicle = defaultdict(list)
    by_name_suffix = defaultdict(list)
    by_name = defaultdict(list)
    for member in members:
        name = _person_name(member.name)
        by_name[name].append(member)
        for vehicle in member.vehicles.all():
            normalized = vehicle.normalized_vehicle_no or normalize_vehicle_no(vehicle.vehicle_no)
            if not normalized:
                continue
            by_exact_vehicle[normalized].append(member)
            digits = ''.join(ch for ch in normalized if ch.isdigit())
            if len(digits) >= 4:
                by_name_suffix[(name, digits[-4:])].append(member)
    return by_exact_vehicle, by_name_suffix, by_name


def _match_member(row, uploaded, indexes):
    raw = row.raw_data or {}
    canonical = raw.get('__canonical__') or {}
    name = _person_name(canonical.get('name') or _raw_value(raw, '성명') or _raw_value(raw, '이름'))
    vehicle_raw = canonical.get('vehicle_no') or _raw_value(raw, '차량번호') or _raw_value(raw, '차번')
    vehicle = normalize_vehicle_no(vehicle_raw)
    digits = ''.join(ch for ch in vehicle if ch.isdigit())
    suffix = digits[-4:] if len(digits) >= 4 else ''
    by_exact_vehicle, by_name_suffix, by_name = indexes
    candidates = _unique(by_exact_vehicle.get(vehicle, [])) if vehicle else []
    if len(candidates) != 1 and suffix:
        candidates = _unique(by_name_suffix.get((name, suffix), []))
    if len(candidates) != 1:
        candidates = _unique(by_name.get(name, []))
    return candidates[0] if len(candidates) == 1 else None


@transaction.atomic
def backfill_receivable_payment_history(uploaded: UploadedFile, *, months=range(1, 8)):
    """Import 2026 Jan-Jul legacy monthly payment history without touching the live ledger.

    The current receivables workbook keeps one row per member and monthly columns such as
    ``1월 입금액`` / ``입금날짜``. We preserve those facts in HistoricalPaymentRecord so
    a member name click can show old payment history while current arrears/prepayments
    remain calculated only from the live ledger.
    """
    if uploaded.slot_type != UploadedFile.SlotType.RECEIVABLES:
        return {'created': 0, 'updated': 0, 'skipped': 0, 'matched_members': 0}

    year = uploaded.job.year if uploaded.job_id else 2026
    months = tuple(int(m) for m in months)
    HistoricalPaymentRecord.objects.filter(
        year=year, month__in=months, source_label='기존 미수금 파일',
    ).exclude(uploaded_file=uploaded).delete()
    indexes = _member_indexes()
    created = updated = skipped = 0
    matched_members = set()

    for row in uploaded.parsed_rows.all().iterator(chunk_size=750):
        raw = row.raw_data or {}
        member = _match_member(row, uploaded, indexes)
        if member is None:
            skipped += 1
            continue
        matched_members.add(member.id)
        canonical = raw.get('__canonical__') or {}
        account = _account_type(canonical.get('account_type') or _raw_value(raw, '계정'), member)

        for month in months:
            amount = parse_decimal(_monthly_amount(raw, month))
            if amount is None or amount <= 0:
                # Delete stale historical row if the source month was later cleared.
                HistoricalPaymentRecord.objects.filter(
                    source_key=f'receivables:{uploaded.sha256}:{row.sheet_name}:{row.source_row}:{month}'
                ).delete()
                continue
            raw_date = _monthly_date(raw, month)
            date_text, parsed_date = _date_text(raw_date, year=year)
            source_key = f'receivables:{uploaded.sha256}:{row.sheet_name}:{row.source_row}:{month}'
            defaults = {
                'member': member,
                'uploaded_file': uploaded,
                'year': year,
                'month': month,
                'account_type': account,
                'payment_date': parsed_date,
                'payment_date_text': date_text,
                'amount': amount,
                'source_label': '기존 미수금 파일',
                'raw_data': {
                    'sheet': row.sheet_name,
                    'source_row': row.source_row,
                    'raw_date': raw_date,
                },
            }
            _, was_created = HistoricalPaymentRecord.objects.update_or_create(
                source_key=source_key, defaults=defaults,
            )
            if was_created:
                created += 1
            else:
                updated += 1

    return {
        'created': created,
        'updated': updated,
        'skipped': skipped,
        'matched_members': len(matched_members),
    }


def backfill_latest_receivable_payment_history():
    uploaded = (
        UploadedFile.objects.filter(slot_type=UploadedFile.SlotType.RECEIVABLES)
        .exclude(parse_status=UploadedFile.ParseStatus.FAILED)
        .order_by('-created_at', '-id')
        .first()
    )
    if not uploaded:
        return None, {'created': 0, 'updated': 0, 'skipped': 0, 'matched_members': 0}
    return uploaded, backfill_receivable_payment_history(uploaded)
