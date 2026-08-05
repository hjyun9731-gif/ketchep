from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from core.models import (
    AccountType, BankTransaction, CardTransaction, ImportIssue, Member, PaymentAllocationLine,
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


def member_candidates_from_text(payer_text: str):
    raw = str(payer_text or '')
    compact = normalize_text(raw)
    normalized_vehicle_text = normalize_vehicle_no(raw)
    data = _all_member_vehicle_data()

    exact_vehicle = []
    for entry in data:
        for vehicle in entry['vehicles']:
            if vehicle.normalized_vehicle_no and vehicle.normalized_vehicle_no in normalized_vehicle_text:
                exact_vehicle.append(entry['member'])
                break
    exact_vehicle = list({m.id: m for m in exact_vehicle}.values())
    if exact_vehicle:
        return exact_vehicle, '완전한 차량번호 일치'

    name_and_fragment = []
    for entry in data:
        member = entry['member']
        if normalize_text(member.name) and normalize_text(member.name) in compact:
            for vehicle in entry['vehicles']:
                number = vehicle.normalized_vehicle_no
                fragments = {number[-4:], number[-5:]}
                if any(fragment and fragment in normalized_vehicle_text for fragment in fragments):
                    name_and_fragment.append(member)
                    break
    name_and_fragment = list({m.id: m for m in name_and_fragment}.values())
    if name_and_fragment:
        return name_and_fragment, '성명 + 차량번호 일부 일치'

    name_matches = []
    for entry in data:
        member = entry['member']
        if normalize_text(member.name) and normalize_text(member.name) in compact:
            name_matches.append(member)
    name_matches = list({m.id: m for m in name_matches}.values())
    if len(name_matches) == 1:
        return name_matches, '고유한 성명 일치'
    if len(name_matches) > 1:
        return name_matches, '동명이인'
    return [], '일치 후보 없음'


def _is_generic_payer(text: str) -> bool:
    compact = normalize_text(text)
    return not compact or compact in GENERIC_PAYER_WORDS or any(compact == x for x in GENERIC_PAYER_WORDS)


def infer_recurring_account(member: Member):
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

    candidates, reason = member_candidates_from_text(tx.payer_text)
    if len(candidates) != 1:
        tx.status = BankTransaction.Status.REVIEW
        tx.match_reason = reason
        tx.save(update_fields=['status', 'match_reason', 'updated_at'])
        return False
    member = candidates[0]
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
