from __future__ import annotations

from decimal import Decimal
import re

from django.db import transaction
from django.db.models import Q

from core.models import (
    AccountType, BankTransaction, CardTransaction, ImportIssue, Member, PayerAlias, PaymentAllocationLine,
    UploadedFile, Vehicle,
)
from core.services.billing import suggested_service_fees
from core.services.ledger import replace_payment_allocations
from core.utils import normalize_text, normalize_vehicle_no

GENERIC_PAYER_WORDS = {
    '지로', '일반이체', 'atm', 'cd', '현금', '입금', '타행이체', '전자금융', '인터넷', '모바일',
}


def _all_member_vehicle_data():
    vehicles = list(Vehicle.objects.select_related('member').filter(member__is_active_record=True))
    member_map = {}
    for vehicle in vehicles:
        member_map.setdefault(vehicle.member_id, {'member': vehicle.member, 'vehicles': []})['vehicles'].append(vehicle)
    for member in Member.objects.filter(is_active_record=True).exclude(id__in=member_map):
        member_map[member.id] = {'member': member, 'vehicles': []}
    return list(member_map.values())


def _unique_members(members):
    return list({member.id: member for member in members}.values())


def _digit_fragments(text: str):
    compact = normalize_vehicle_no(text)
    return {m.group(0) for m in re.finditer(r'\d{4,}', compact)} | {
        m.group(0)[-4:] for m in re.finditer(r'\d{4,}', compact)
    }


def _member_memo_aliases(member: Member):
    memo = member.memo or ''
    aliases = set()
    for token in re.split(r'[,/;|·\n()\[\]]+', memo):
        normalized = normalize_text(token)
        if normalized:
            aliases.add(normalized)
    # Also allow exact search within a free-form note such as "입금자 홍길동".
    aliases.add(normalize_text(memo))
    return aliases


def member_candidates_from_text(payer_text: str, bank_account_label: str = ''):
    raw = str(payer_text or '')
    compact = normalize_text(raw)
    normalized_vehicle_text = normalize_vehicle_no(raw)
    digit_fragments = _digit_fragments(raw)
    data = _all_member_vehicle_data()

    # 1. User-confirmed payer aliases. Account-specific aliases have priority.
    alias_qs = PayerAlias.objects.filter(normalized_alias=compact, auto_apply=True).select_related('member')
    if bank_account_label:
        account_aliases = alias_qs.filter(bank_account_label=bank_account_label)
        if account_aliases.exists():
            members = _unique_members([row.member for row in account_aliases])
            return members, '사용자가 저장한 계좌별 입금자 별칭'
    general_aliases = alias_qs.filter(bank_account_label='')
    if general_aliases.exists():
        members = _unique_members([row.member for row in general_aliases])
        return members, '사용자가 저장한 입금자 별칭'

    # 2. Full current or historical vehicle number contained in payer text.
    exact_vehicle = []
    for entry in data:
        for vehicle in entry['vehicles']:
            number = vehicle.normalized_vehicle_no
            if number and len(number) >= 6 and number in normalized_vehicle_text:
                exact_vehicle.append(entry['member'])
                break
    exact_vehicle = _unique_members(exact_vehicle)
    if exact_vehicle:
        return exact_vehicle, '현재·과거 전체 차량번호 일치'

    # 3. Exact member name plus a vehicle fragment/last four digits.
    name_and_fragment = []
    for entry in data:
        member = entry['member']
        name = normalize_text(member.name)
        if not name or name not in compact:
            continue
        for vehicle in entry['vehicles']:
            number = vehicle.normalized_vehicle_no
            fragments = {number[-4:], number[-5:], number[-6:]}
            if any(fragment and (fragment in normalized_vehicle_text or fragment in digit_fragments) for fragment in fragments):
                name_and_fragment.append(member)
                break
    name_and_fragment = _unique_members(name_and_fragment)
    if name_and_fragment:
        return name_and_fragment, '성명 + 현재·과거 차량번호 일부 일치'

    # 4. Memo alias plus vehicle fragment. This remains review-only in auto_match_bank_transaction.
    memo_and_fragment = []
    for entry in data:
        member = entry['member']
        aliases = _member_memo_aliases(member)
        alias_hit = any(alias and (alias == compact or alias in compact or compact in alias) for alias in aliases)
        if not alias_hit:
            continue
        for vehicle in entry['vehicles']:
            number = vehicle.normalized_vehicle_no
            fragments = {number[-4:], number[-5:], number[-6:]}
            if any(fragment and (fragment in normalized_vehicle_text or fragment in digit_fragments) for fragment in fragments):
                memo_and_fragment.append(member)
                break
    memo_and_fragment = _unique_members(memo_and_fragment)
    if memo_and_fragment:
        return memo_and_fragment, '미수금 비고 입금자명 + 차량번호 일부 일치'

    # 5. Vehicle last-four or numeric fragment only. Auto only when globally unique.
    fragment_matches = []
    for entry in data:
        for vehicle in entry['vehicles']:
            number = vehicle.normalized_vehicle_no
            if any(fragment and number.endswith(fragment[-4:]) for fragment in digit_fragments if len(fragment) >= 4):
                fragment_matches.append(entry['member'])
                break
    fragment_matches = _unique_members(fragment_matches)
    if len(fragment_matches) == 1:
        return fragment_matches, '현재·과거 차량 끝번호가 전체에서 유일'
    if len(fragment_matches) > 1:
        return fragment_matches, '차량 끝번호 후보 복수'

    # 6. Exact/contained full name. A globally unique full name can auto-match.
    name_matches = []
    for entry in data:
        member = entry['member']
        name = normalize_text(member.name)
        if name and name in compact:
            name_matches.append(member)
    name_matches = _unique_members(name_matches)
    if len(name_matches) == 1:
        return name_matches, '고유한 성명 일치'
    if len(name_matches) > 1:
        return name_matches, '동명이인'

    # 7. Free-form memo alias. Always shown for confirmation even when unique.
    memo_matches = []
    for entry in data:
        member = entry['member']
        for alias in _member_memo_aliases(member):
            if alias and (alias == compact or alias in compact or compact in alias):
                memo_matches.append(member)
                break
    memo_matches = _unique_members(memo_matches)
    if len(memo_matches) == 1:
        return memo_matches, '미수금 비고 입금자명 단독 일치'
    if len(memo_matches) > 1:
        return memo_matches, '미수금 비고 입금자명 후보 복수'

    return [], '일치 후보 없음'

def _is_generic_payer(text: str) -> bool:
    compact = normalize_text(text)
    return not compact or compact in GENERIC_PAYER_WORDS or any(compact == x for x in GENERIC_PAYER_WORDS)


def infer_recurring_account(member: Member):
    if member.receivable_account_type in {AccountType.MEMBERSHIP_FEE, AccountType.MANAGEMENT_FEE}:
        return member.receivable_account_type
    membership_outstanding = member.outstanding(AccountType.MEMBERSHIP_FEE)
    management_outstanding = member.outstanding(AccountType.MANAGEMENT_FEE)
    if membership_outstanding > 0 and management_outstanding == 0:
        return AccountType.MEMBERSHIP_FEE
    if management_outstanding > 0 and membership_outstanding == 0:
        return AccountType.MANAGEMENT_FEE
    if member.membership_status == Member.MembershipStatus.ACTIVE:
        return AccountType.MEMBERSHIP_FEE
    return AccountType.MANAGEMENT_FEE


def certificate_fee_candidate(member: Member, tx: BankTransaction):
    if not member.certificate_issued_on:
        return False
    if member.certificate_issued_on.year != tx.job.year or member.certificate_issued_on.month != tx.job.month:
        return False
    expected = suggested_service_fees(member)[AccountType.CERTIFICATE_FEE]
    return expected > 0 and tx.amount == expected


@transaction.atomic
def auto_match_bank_transaction(tx: BankTransaction):
    if tx.status == BankTransaction.Status.DUPLICATE:
        return False
    payer_normalized = normalize_text(tx.payer_text)
    if '사이다' in payer_normalized or 'cider' in payer_normalized:
        tx.is_card_settlement = True
        tx.card_provider = CardTransaction.Provider.CIDER
        tx.status = BankTransaction.Status.IGNORED
        tx.match_reason = '사이다페이 순정산입금 - 회원에게 재반영하지 않음'
        tx.save(update_fields=['is_card_settlement', 'card_provider', 'status', 'match_reason', 'updated_at'])
        return True
    if '알토란' in payer_normalized:
        tx.is_card_settlement = True
        tx.card_provider = CardTransaction.Provider.ALTOLAN
        tx.status = BankTransaction.Status.IGNORED
        tx.match_reason = '알토란 순정산입금 - 회원에게 재반영하지 않음'
        tx.save(update_fields=['is_card_settlement', 'card_provider', 'status', 'match_reason', 'updated_at'])
        return True
    if _is_generic_payer(tx.payer_text):
        tx.status = BankTransaction.Status.REVIEW
        tx.match_reason = '식별정보가 없는 일반 입금'
        tx.save(update_fields=['status', 'match_reason', 'updated_at'])
        return False

    candidates, reason = member_candidates_from_text(tx.payer_text, tx.bank_account_label)
    if len(candidates) != 1:
        tx.status = BankTransaction.Status.REVIEW
        tx.match_reason = reason
        tx.save(update_fields=['status', 'match_reason', 'updated_at'])
        return False
    member = candidates[0]
    if reason.startswith('미수금 비고'):
        tx.status = BankTransaction.Status.REVIEW
        tx.match_reason = reason + ' - 사용자 확인 필요'
        tx.save(update_fields=['status', 'match_reason', 'updated_at'])
        return False
    expected_certificate = suggested_service_fees(member)[AccountType.CERTIFICATE_FEE]
    certificate_month = bool(
        member.certificate_issued_on
        and member.certificate_issued_on.year == tx.job.year
        and member.certificate_issued_on.month == tx.job.month
        and expected_certificate > 0
    )
    if certificate_month and tx.amount == expected_certificate:
        account_type = AccountType.CERTIFICATE_FEE
    elif certificate_month and tx.amount > expected_certificate:
        tx.status = BankTransaction.Status.REVIEW
        tx.match_reason = '자격증명 발급비와 관리비·대폐차비 등이 합산된 복합입금 가능'
        tx.save(update_fields=['status', 'match_reason', 'updated_at'])
        return False
    else:
        account_type = infer_recurring_account(member)
    replace_payment_allocations(
        tx.payment,
        [{'member': member, 'account_type': account_type, 'amount': tx.amount, 'memo': reason}],
        reason='자동매칭: ' + reason,
    )
    tx.status = BankTransaction.Status.AUTO_MATCHED
    tx.match_reason = reason
    tx.save(update_fields=['status', 'match_reason', 'updated_at'])
    return True


@transaction.atomic
def auto_match_bank_job(job):
    success = review = ignored = 0
    for tx in job.bank_transactions.select_related('payment').filter(is_effective=True).order_by('transaction_at', 'id'):
        if tx.status in {BankTransaction.Status.MANUAL_MATCHED, BankTransaction.Status.DUPLICATE}:
            continue
        matched = auto_match_bank_transaction(tx)
        tx.refresh_from_db()
        if tx.status == BankTransaction.Status.IGNORED:
            ignored += 1
        elif matched:
            success += 1
        else:
            review += 1
    return {'matched': success, 'review': review, 'ignored': ignored}


def card_member_candidate(tx: CardTransaction):
    vehicle_normalized = normalize_vehicle_no(tx.vehicle_no)
    vehicle_members = Member.objects.none()
    if vehicle_normalized:
        vehicle_members = Member.objects.filter(
            vehicles__normalized_vehicle_no=vehicle_normalized,
            is_active_record=True,
        ).distinct()
    name_members = Member.objects.filter(name=tx.member_name, is_active_record=True) if tx.member_name else Member.objects.none()
    if vehicle_members.count() == 1:
        member = vehicle_members.first()
        if tx.member_name and not name_members.filter(pk=member.pk).exists():
            return None, '차량번호와 이름 충돌'
        return member, '차량번호 우선 일치'
    if vehicle_members.count() > 1:
        return None, '차량번호 후보 복수'
    if name_members.count() == 1:
        return name_members.first(), '고유한 이름 일치'
    return None, '회원 후보를 확정할 수 없음'


@transaction.atomic
def auto_match_card_job(job):
    matched = review = 0
    for tx in job.card_transactions.select_related('payment').filter(is_effective=True, duplicate_suspected=False):
        if tx.status == CardTransaction.Status.MATCHED and tx.payment.allocation_lines.filter(
            status=PaymentAllocationLine.Status.ACTIVE
        ).exists():
            continue
        member, reason = card_member_candidate(tx)
        if not member:
            tx.status = CardTransaction.Status.REVIEW
            tx.save(update_fields=['status', 'updated_at'])
            review += 1
            continue
        account_type = infer_recurring_account(member)
        replace_payment_allocations(
            tx.payment,
            [{'member': member, 'account_type': account_type, 'amount': tx.gross_amount, 'memo': reason}],
            reason='카드 자동매칭: ' + reason,
        )
        tx.status = CardTransaction.Status.MATCHED
        tx.save(update_fields=['status', 'updated_at'])
        matched += 1
    return {'matched': matched, 'review': review}


@transaction.atomic
def copy_allocations_from_previous_version(job):
    previous = job.based_on
    if not previous:
        return {'copied': 0, 'changed': 0, 'deleted': 0}

    copied = changed = deleted = 0
    ImportIssue.objects.filter(
        uploaded_file__job=job,
        issue_type__in=['previous_transaction_missing', 'new_or_changed_transaction'],
    ).delete()
    prev_groups = {}
    for tx in previous.bank_transactions.select_related('payment').order_by('source_row', 'id'):
        prev_groups.setdefault(tx.duplicate_group_key, []).append(tx)
    new_groups = {}
    for tx in job.bank_transactions.select_related('payment').order_by('source_row', 'id'):
        new_groups.setdefault(tx.duplicate_group_key, []).append(tx)

    for fingerprint, new_rows in new_groups.items():
        old_rows = prev_groups.get(fingerprint, [])
        for index, new_tx in enumerate(new_rows):
            if index >= len(old_rows):
                changed += 1
                continue
            old_tx = old_rows[index]
            allocations = []
            for line in old_tx.payment.allocation_lines.filter(status=PaymentAllocationLine.Status.ACTIVE):
                allocations.append({
                    'member': line.member,
                    'account_type': line.account_type,
                    'amount': line.amount,
                    'memo': f'이전 버전 {previous.version_name}에서 복사',
                })
            if allocations:
                replace_payment_allocations(new_tx.payment, allocations, reason='이전 버전 매칭 복사')
                new_tx.status = old_tx.status
                new_tx.match_reason = f'{previous.version_name} 매칭 복사'
                new_tx.save(update_fields=['status', 'match_reason', 'updated_at'])
                copied += 1
            else:
                changed += 1

    latest_files = {}
    for uploaded in job.uploaded_files.filter(
        slot_type__in=[UploadedFile.SlotType.BANK_1, UploadedFile.SlotType.BANK_2, UploadedFile.SlotType.BANK_3]
    ).order_by('slot_type', '-created_at'):
        latest_files.setdefault(uploaded.slot_type, uploaded)
    fallback_file = next(iter(latest_files.values()), None)

    for fingerprint, old_rows in prev_groups.items():
        new_rows = new_groups.get(fingerprint, [])
        if len(old_rows) <= len(new_rows):
            continue
        for old_tx in old_rows[len(new_rows):]:
            target_file = latest_files.get(old_tx.uploaded_file.slot_type) or fallback_file
            if not target_file:
                continue
            ImportIssue.objects.create(
                uploaded_file=target_file,
                sheet_name=old_tx.source_sheet,
                source_row=old_tx.source_row,
                issue_type='previous_transaction_missing',
                message=f'이전 버전({previous.version_name})에는 있었지만 새 파일에서 찾지 못한 거래입니다. 삭제 또는 변경 여부를 확인하세요.',
                raw_data=old_tx.raw_data,
            )
            deleted += 1

    for fingerprint, new_rows in new_groups.items():
        old_rows = prev_groups.get(fingerprint, [])
        if len(new_rows) <= len(old_rows):
            continue
        for new_tx in new_rows[len(old_rows):]:
            ImportIssue.objects.create(
                uploaded_file=new_tx.uploaded_file,
                sheet_name=new_tx.source_sheet,
                source_row=new_tx.source_row,
                issue_type='new_or_changed_transaction',
                message='이전 버전에서 동일 거래를 찾지 못했습니다. 신규 또는 금액·일시·입금자 변경 거래입니다.',
                raw_data=new_tx.raw_data,
            )
    return {'copied': copied, 'changed': changed, 'deleted': deleted}
