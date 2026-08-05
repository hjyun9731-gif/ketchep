from __future__ import annotations

from datetime import date

from django.db import transaction
from django.utils import timezone

from core.models import (
    AccountType, Charge, Member, MemberLink, PaymentAllocationLine, Prepayment,
    Refund, Vehicle,
)
from core.services.audit import log_action
from core.services.billing import close_member
from core.services.ledger import RECURRING_ACCOUNTS, rebuild_member_account
from core.utils import extract_purpose_char, normalize_vehicle_no


@transaction.atomic
def transfer_member(
    old_member: Member,
    *,
    transfer_type: str,
    effective_date: date,
    new_name: str,
    new_birth6: str = '',
    new_phone: str = '',
    new_address: str = '',
    new_official_address: str = '',
    new_vehicle_no: str = '',
    new_region: str = '',
    memo: str = '',
    actor: str = 'admin',
):
    if transfer_type not in {
        MemberLink.LinkType.FAMILY_SUCCESSION,
        MemberLink.LinkType.GENERAL_TRANSFER,
    }:
        raise ValueError('지원하지 않는 양도양수 유형입니다.')
    old_member = Member.objects.select_for_update().get(pk=old_member.pk)
    if old_member.operational_status == Member.OperationalStatus.CLOSED:
        raise ValueError('이미 폐업 처리된 회원입니다.')
    if not new_name.strip():
        raise ValueError('새 명의자 성명을 입력하세요.')

    family = transfer_type == MemberLink.LinkType.FAMILY_SUCCESSION
    old_outstanding_before = old_member.total_outstanding
    if family and Prepayment.objects.filter(member=old_member, balance__gt=0).exists():
        raise ValueError('선납금이 남아 있습니다. 기존 명의자 환불을 먼저 처리한 뒤 승계하세요.')
    if family and Refund.objects.filter(member=old_member, status=Refund.Status.PENDING).exists():
        raise ValueError('환불대기 건을 먼저 처리한 뒤 승계하세요.')

    close_member(
        old_member,
        effective_date,
        reason='가족·직계 승계' if family else '일반 양도양수',
        memo=memo,
        actor=actor,
    )
    old_member.refresh_from_db()

    new_member = Member.objects.create(
        name=new_name.strip(),
        birth6=''.join(ch for ch in (new_birth6 or '') if ch.isdigit())[:6],
        phone=''.join(ch for ch in (new_phone or '') if ch.isdigit()),
        address=(new_address or '').strip(),
        official_address=(new_official_address or new_address or '').strip(),
        official_address_custom=bool(new_official_address and new_official_address.strip() != new_address.strip()),
        region=(new_region or old_member.region or '').strip(),
        operational_status=Member.OperationalStatus.ACTIVE,
        membership_status=Member.MembershipStatus.NON_MEMBER,
        first_seen_on=effective_date,
        certificate_issued_on=effective_date,
        certificate_date_recorded_on=timezone.localdate(),
        memo=memo,
    )

    current = old_member.current_vehicle
    vehicle_no = (new_vehicle_no or (current.vehicle_no if current else '')).strip()
    if current:
        current.is_current = False
        current.end_date = effective_date
        current.change_reason = '명의 이전'
        current.save(update_fields=['is_current', 'end_date', 'change_reason', 'updated_at'])
    if vehicle_no:
        Vehicle.objects.create(
            member=new_member,
            vehicle_no=vehicle_no,
            normalized_vehicle_no=normalize_vehicle_no(vehicle_no),
            purpose_char=extract_purpose_char(vehicle_no),
            start_date=effective_date,
            is_current=True,
            change_reason='가족·직계 승계' if family else '일반 양도양수',
        )

    link = MemberLink.objects.create(
        old_member=old_member,
        new_member=new_member,
        link_type=transfer_type,
        effective_date=effective_date,
        arrears_transferred=family,
        memo=memo,
    )

    if family:
        # 승계형은 회계원장 자체를 새 명의자로 이동해 기존 미수금과 납부이력을 함께 유지한다.
        Charge.objects.filter(member=old_member).update(member=new_member)
        PaymentAllocationLine.objects.filter(member=old_member).update(member=new_member)
        Refund.objects.filter(member=old_member).update(member=new_member)
        for prepayment in list(Prepayment.objects.filter(member=old_member)):
            target, _ = Prepayment.objects.get_or_create(
                member=new_member, account_type=prepayment.account_type,
                defaults={'balance': 0},
            )
            prepayment.movements.update(prepayment=target)
            prepayment.delete()
        for account in RECURRING_ACCOUNTS:
            rebuild_member_account(new_member, account)
            rebuild_member_account(old_member, account)

    log_action(
        action='member_transferred',
        instance=link,
        before={'old_member_id': old_member.id, 'old_outstanding': str(old_outstanding_before)},
        after={
            'new_member_id': new_member.id,
            'transfer_type': transfer_type,
            'arrears_transferred': family,
            'new_outstanding': str(new_member.total_outstanding),
        },
        reason=memo,
        actor=actor,
    )
    return new_member, link
