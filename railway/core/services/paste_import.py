from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from core.models import BankTransaction, MonthlyJob, Payment, UploadedFile
from core.services.matching import auto_match_bank_transaction
from core.utils import normalize_header, normalize_text, parse_datetime, parse_decimal, stable_hash


PASTE_HEADER_ALIASES = {
    'transaction_at': {
        '거래일시', '거래일자', '거래일', '일시', '일자', '입금일', '거래시간', '거래일시각',
    },
    'payer_text': {
        '입금자명', '입금자', '보낸분', '보낸사람', '의뢰인', '적요', '거래내용', '기재내용', '내용',
    },
    'amount': {
        '입금액', '입금금액', '맡기신금액', '거래금액', '금액', '입금',
    },
    'withdrawal': {
        '출금액', '출금금액', '찾으신금액', '출금',
    },
    'balance': {
        '잔액', '거래후잔액', '예금잔액', '통장잔액',
    },
    'transaction_id': {
        '거래번호', '거래고유번호', '거래id', '순번', '거래순번',
    },
}


def _normalized_alias_map():
    result = {}
    for field, aliases in PASTE_HEADER_ALIASES.items():
        for alias in aliases:
            result[normalize_header(alias)] = field
    return result


ALIAS_MAP = _normalized_alias_map()


def get_or_create_current_job(target_date=None):
    target_date = target_date or timezone.localdate()
    current = MonthlyJob.objects.filter(
        year=target_date.year, month=target_date.month, is_current=True,
    ).first()
    if current:
        return current
    latest = MonthlyJob.objects.filter(year=target_date.year, month=target_date.month).order_by('-version').first()
    if latest:
        MonthlyJob.objects.filter(year=target_date.year, month=target_date.month, is_current=True).update(is_current=False)
        latest.is_current = True
        latest.save(update_fields=['is_current', 'updated_at'])
        return latest
    return MonthlyJob.objects.create(
        year=target_date.year,
        month=target_date.month,
        version=1,
        version_name='자동 원장',
        status=MonthlyJob.Status.DRAFT,
        is_current=True,
        memo='일일 입금내역 붙여넣기로 자동 생성된 월 원장',
    )


def _split_rows(pasted_text: str):
    text = (pasted_text or '').replace('\r\n', '\n').replace('\r', '\n').strip('\n')
    if not text.strip():
        return []
    # Excel clipboard data is tab-delimited. Preserve empty cells.
    return [row for row in csv.reader(io.StringIO(text), delimiter='\t') if any(str(v).strip() for v in row)]


def _find_header(rows):
    best = None
    for idx, row in enumerate(rows[:30]):
        mapping = {}
        for col, value in enumerate(row):
            normalized = normalize_header(value)
            if normalized in ALIAS_MAP:
                mapping[ALIAS_MAP[normalized]] = col
        score = len(mapping)
        if 'amount' in mapping and ('payer_text' in mapping or 'transaction_at' in mapping):
            score += 4
        if best is None or score > best[0]:
            best = (score, idx, mapping)
    if best and best[0] >= 3:
        return best[1], best[2]
    return None, {}


def _looks_like_date(value):
    text = str(value or '').strip()
    return bool(re.search(r'\d{2,4}[./-]\d{1,2}[./-]\d{1,2}', text))


def _looks_like_money(value):
    text = str(value or '').strip().replace(',', '')
    return bool(re.fullmatch(r'[-+]?\d+(?:\.\d+)?(?:원)?', text))


def _heuristic_mapping(rows):
    """Fallback only when NH headers are not present.

    Old code picked the numerically most-populated column, so row numbers such as
    20, 19, 18 could be mistaken for deposit amounts. Prefer columns containing
    realistic monetary magnitudes and only fall back to small numbers when no
    other numeric column exists.
    """
    sample = rows[:40]
    max_cols = max((len(r) for r in sample), default=0)
    date_scores = Counter()
    money_values = defaultdict(list)
    text_scores = Counter()
    for row in sample:
        for col in range(max_cols):
            value = row[col] if col < len(row) else ''
            if _looks_like_date(value):
                date_scores[col] += 1
            parsed = parse_decimal(value)
            if parsed is not None and parsed > 0:
                money_values[col].append(parsed)
            if re.search(r'[가-힣A-Za-z]', str(value or '')):
                text_scores[col] += 1

    mapping = {}
    if date_scores:
        mapping['transaction_at'] = date_scores.most_common(1)[0][0]

    if money_values:
        candidates = []
        for col, values in money_values.items():
            ordered = sorted(values)
            median = ordered[len(ordered) // 2]
            max_value = max(ordered)
            substantial = sum(1 for value in ordered if value >= 1000)
            # realistic deposit columns win over row-number/index columns
            score = (1 if substantial else 0, substantial, median, max_value, len(values))
            candidates.append((score, col))
        mapping['amount'] = max(candidates)[1]

    if text_scores:
        excluded = {mapping.get('transaction_at'), mapping.get('amount')}
        candidates = [(score, col) for col, score in text_scores.items() if col not in excluded]
        if candidates:
            mapping['payer_text'] = max(candidates)[1]
    return mapping


def _cell(row, mapping, field):
    index = mapping.get(field)
    if index is None or index >= len(row):
        return ''
    return str(row[index] or '').strip()


def _combine_date_time(value, row):
    parsed = parse_datetime(value, default_year=timezone.localdate().year)
    if parsed:
        return parsed
    # Some NH exports split date and time. Search the row for a time token and combine.
    date_match = re.search(r'(\d{2,4}[./-]\d{1,2}[./-]\d{1,2})', str(value or ''))
    if not date_match:
        for item in row:
            if _looks_like_date(item):
                date_match = re.search(r'(\d{2,4}[./-]\d{1,2}[./-]\d{1,2})', str(item))
                if date_match:
                    break
    if date_match:
        time_text = ''
        for item in row:
            m = re.fullmatch(r'\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*', str(item or ''))
            if m:
                time_text = m.group(1)
                break
        combined = f'{date_match.group(1)} {time_text}'.strip()
        return parse_datetime(combined, default_year=timezone.localdate().year)
    return None


def _raw_dict(headers, row):
    if headers:
        result = {}
        for idx, value in enumerate(row):
            key = headers[idx] if idx < len(headers) and headers[idx] else f'열{idx + 1}'
            if key in result:
                key = f'{key}_{idx + 1}'
            result[key] = value
        return result
    return {f'열{idx + 1}': value for idx, value in enumerate(row)}


@transaction.atomic
def process_pasted_bank_text(*, slot_type: str, pasted_text: str, actor='admin'):
    if slot_type not in {
        UploadedFile.SlotType.BANK_1,
        UploadedFile.SlotType.BANK_2,
        UploadedFile.SlotType.BANK_3,
    }:
        raise ValueError('통장 계좌 1·2·3 중 하나를 선택해야 합니다.')

    rows = _split_rows(pasted_text)
    if not rows:
        raise ValueError('붙여넣은 거래내역이 없습니다.')

    header_index, mapping = _find_header(rows)
    if not mapping:
        mapping = _heuristic_mapping(rows)
    if 'amount' not in mapping or 'payer_text' not in mapping:
        raise ValueError('입금액과 입금자명 열을 자동으로 찾지 못했습니다. 농협 엑셀의 표 전체를 다시 복사해 붙여넣으세요.')

    headers = rows[header_index] if header_index is not None else []
    data_rows = rows[header_index + 1:] if header_index is not None else rows
    now = timezone.localtime()
    job = get_or_create_current_job(now.date())
    label = dict(UploadedFile.SlotType.choices).get(slot_type, slot_type)

    uploaded = UploadedFile(
        job=job,
        slot_type=slot_type,
        original_name=f'{now:%Y%m%d_%H%M%S}_{slot_type}_붙여넣기.tsv',
        sha256=stable_hash({'slot': slot_type, 'text': pasted_text}),
        size=len(pasted_text.encode('utf-8')),
        parse_status=UploadedFile.ParseStatus.PROCESSED,
        header_row=(header_index + 1) if header_index is not None else None,
        detected_headers=headers,
        column_mapping=mapping,
    )
    uploaded.file.save(uploaded.original_name, ContentFile(pasted_text.encode('utf-8')), save=False)
    uploaded.save()

    created = skipped_duplicate = ignored_rows = matched = review = 0
    occurrence_counter = defaultdict(int)
    source_row = (header_index + 2) if header_index is not None else 1

    for offset, row in enumerate(data_rows):
        amount = parse_decimal(_cell(row, mapping, 'amount'))
        withdrawal = parse_decimal(_cell(row, mapping, 'withdrawal'))
        payer = _cell(row, mapping, 'payer_text')
        if amount is None or amount <= 0 or (withdrawal and withdrawal > 0 and amount <= 0):
            ignored_rows += 1
            continue
        if not payer:
            payer = next((str(v).strip() for v in row if re.search(r'[가-힣A-Za-z]', str(v or ''))), '')
        transaction_at = _combine_date_time(_cell(row, mapping, 'transaction_at'), row)
        balance = _cell(row, mapping, 'balance')
        transaction_id = _cell(row, mapping, 'transaction_id')
        raw_data = _raw_dict(headers, row)

        fingerprint = stable_hash({
            'slot': slot_type,
            'transaction_at': str(transaction_at or ''),
            'payer': normalize_text(payer),
            'amount': str(amount),
            'balance': normalize_text(balance),
            'transaction_id': normalize_text(transaction_id),
        })
        occurrence_counter[fingerprint] += 1
        occurrence_no = occurrence_counter[fingerprint]

        existing = BankTransaction.objects.filter(
            job=job,
            duplicate_group_key=fingerprint,
            occurrence_no=occurrence_no,
            is_effective=True,
        ).first()
        if existing:
            skipped_duplicate += 1
            continue

        txn_key = transaction_id or stable_hash({
            'fingerprint': fingerprint,
            'occurrence': occurrence_no,
            'uploaded': uploaded.id,
            'row': source_row + offset,
        })
        tx = BankTransaction.objects.create(
            job=job,
            uploaded_file=uploaded,
            txn_key=txn_key,
            occurrence_no=occurrence_no,
            bank_account_label=label,
            transaction_at=transaction_at,
            payer_text=payer,
            amount=amount,
            source_sheet='붙여넣기',
            source_row=source_row + offset,
            raw_data=raw_data,
            duplicate_group_key=fingerprint,
            status=BankTransaction.Status.UNMATCHED,
        )
        Payment.objects.create(
            source_type=Payment.SourceType.BANK,
            payment_date=transaction_at or timezone.now(),
            amount=amount,
            bank_transaction=tx,
            monthly_job=job,
            memo=f'{label} 엑셀 붙여넣기',
        )
        created += 1
        if auto_match_bank_transaction(tx):
            tx.refresh_from_db()
            if tx.status == BankTransaction.Status.IGNORED:
                pass
            else:
                matched += 1
        else:
            review += 1

    uploaded.parse_summary = {
        'rows_received': len(data_rows),
        'created_transactions': created,
        'skipped_duplicates': skipped_duplicate,
        'ignored_rows': ignored_rows,
        'auto_matched': matched,
        'review': review,
        'actor': actor,
        'processed_at': timezone.now().isoformat(),
    }
    uploaded.save(update_fields=['parse_summary', 'updated_at'])
    return uploaded.parse_summary
