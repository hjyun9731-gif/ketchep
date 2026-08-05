from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import (
    AccountType, Charge, ClosureEvent, Member, MembershipEvent, MonthlyJob,
    Prepayment, Refund, Vehicle,
)
from core.services.audit import log_action
from core.services.ledger import RECURRING_ACCOUNTS, rebuild_member_account
from core.utils import add_month_same_day


MEMBERSHIP_FULL = Decimal('10000')
MEMBERSHIP_REDUCED = Decimal('5000')
MANAGEMENT_AMOUNT = Decimal('5000')


def date_with_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def first_occurrence_on_or_after(anchor_day: int, not_before: date) -> date:
    current = date_with_day(not_before.year, not_before.month, anchor_day)
    if current >= not_before:
        return current
    if not_before.month == 12:
        return date_with_day(not_before.year + 1, 1, anchor_day)
    return date_with_day(not_before.year, not_before.month + 1, anchor_day)


def infer_birth_year(birth6: str, reference_year: int) -> int | None:
    if not birth6 or len(birth6) != 6 or not birth6.isdigit():
        return None
    yy = int(birth6[:2])
    current_yy = reference_year % 100
    return 2000 + yy if yy <= current_yy else 1900 + yy


def membership_fee_amount(member: Member, charge_year: int) -> Decimal:
    birth_year = infer_birth_year(member.birth6, charge_year)
    if birth_year and charge_year >= birth_year + 70:
        return MEMBERSHIP_REDUCED
    return MEMBERSHIP_FULL


def vehicle_for_date(member: Member, target: date) -> Vehicle | None:
    qs = member.vehicles.filter(Q(start_date__isnull=True) | Q(start_date__lte=target)).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=target)
    ).order_by('-is_current', '-start_date', '-id')
    return qs.first() or member.current_vehicle


def latest_membership_state(member: Member, target: date) -> str:
    event = member.membership_events.filter(effective_date__lte=target).order_by('-effective_date', '-id').first()
    if event:
        if event.event_type == MembershipEvent.EventType.JOIN:
            return Member.MembershipStatus.ACTIVE
        if event.event_type == MembershipEvent.EventType.PENDING:
            return Member.MembershipStatus.PENDING
        return Member.MembershipStatus.NON_MEMBER
    if member.membership_started_on and member.membership_started_on <= target:
        if not member.membership_ended_on or member.membership_ended_on > target:
            return Member.MembershipStatus.ACTIVE
    return member.membership_status if not member.membership_started_on else Member.MembershipStatus.NON_MEMBER


def month_has_closure_transition(member: Member, year: int, month: int) -> bool:
    return member.closure_events.filter(effective_date__year=year, effective_date__month=month).exists()


def is_closed_on(member: Member, target: date) -> bool:
    event = member.closure_events.filter(effective_date__lte=target).order_by('-effective_date', '-id').first()
    if event:
        return event.event_type == ClosureEvent.EventType.CLOSE
    return bool(member.closed_on and member.closed_on <= target and not (member.re_registered_on and member.re_registered_on <= target))


def membership_charge_date(member: Member, year: int, month: int) -> date | None:
    # A leave event in the month cancels the whole month's association fee.
    if member.membership_events.filter(
        event_type=MembershipEvent.EventType.LEAVE,
        effective_date__year=year,
        effective_date__month=month,
    ).exists():
        return None

    if member.membership_started_on:
        first = add_month_same_day(member.membership_started_on, 1)
        day = member.membership_started_on.day
    elif member.membership_billing_anchor:
        first = member.membership_billing_anchor
        day = member.membership_billing_anchor.day
    else:
        return None

    if member.re_registered_on:
        reopen_first = add_month_same_day(member.re_registered_on, 1)
        if reopen_first > first:
            first = reopen_first
            day = member.re_registered_on.day

    candidate = date_with_day(year, month, day)
    if candidate < first:
        return None
    if latest_membership_state(member, candidate) != Member.MembershipStatus.ACTIVE:
        return None
    return candidate


def management_charge_date(member: Member, year: int, month: int) -> date | None:
    target_start = date(year, month, 1)
    target_end = date(year, month, calendar.monthrange(year, month)[1])

    # Active association members never pay management fee.
    # Use a candidate-date check after deriving the date as well.
    vehicle = vehicle_for_date(member, target_end)
    purpose = vehicle.purpose_char if vehicle else ''

    if year <= 2026:
        if purpose != '배':
            return None
        if member.certificate_issued_on:
            first = add_month_same_day(member.certificate_issued_on, 1)
            day = member.certificate_issued_on.day
        elif member.management_billing_anchor:
            first = member.management_billing_anchor
            day = member.management_billing_anchor.day
        else:
            return None
    else:
        cutoff = date(2027, 1, 1)
        existed_on_cutoff = bool(
            (member.first_seen_on and member.first_seen_on <= cutoff)
            or (member.certificate_issued_on and member.certificate_issued_on <= cutoff)
        )
        if existed_on_cutoff:
            first = cutoff
            day = 1
        else:
            if not member.certificate_issued_on:
                return None
            natural_first = add_month_same_day(member.certificate_issued_on, 1)
            day = member.certificate_issued_on.day
            recorded = member.certificate_date_recorded_on or member.certificate_issued_on
            if recorded > natural_first:
                first = first_occurrence_on_or_after(day, recorded)
            else:
                first = natural_first

    if member.re_registered_on:
        reopen_first = add_month_same_day(member.re_registered_on, 1)
        if reopen_first > first:
            first = reopen_first
            day = member.re_registered_on.day

    candidate = date_with_day(year, month, day)
    if candidate < first:
        return None
    if latest_membership_state(member, candidate) == Member.MembershipStatus.ACTIVE:
        return None
    return candidate


def desired_recurring_charge(member: Member, year: int, month: int):
    if not member.is_active_record:
        return None
    if month_has_closure_transition(member, year, month):
        return None

    assoc_date = membership_charge_date(member, year, month)
    if assoc_date and not is_closed_on(member, assoc_date):
        return {
            'account_type': AccountType.MEMBERSHIP_FEE,
            'charge_date': assoc_date,
            'amount': membership_fee_amount(member, year),
            'source_rule': 'membership_monthly',
        }

    management_date = management_charge_date(member, year, month)
    if management_date and not is_closed_on(member, management_date):
        return {
            'account_type': AccountType.MANAGEMENT_FEE,
            'charge_date': management_date,
            'amount': MANAGEMENT_AMOUNT,
            'source_rule': 'management_monthly_2027' if year >= 2027 else 'management_delivery',
        }
    return None


@transaction.atomic
def generate_charges_for_job(job: MonthlyJob, *, actor='admin') -> dict:
    created = 0
    cancelled = 0
    unchanged = 0
    affected = set()

    # Existing charges in this version are reconciled against the current rules.
    existing = {
        (c.member_id, c.account_type, c.charge_date): c
        for c in job.charges.select_for_update().all()
    }
    desired_keys = set()

    for member in Member.objects.filter(is_active_record=True).iterator():
        desired = desired_recurring_charge(member, job.year, job.month)
        if not desired:
            continue
        key = (member.id, desired['account_type'], desired['charge_date'])
        desired_keys.add(key)
        charge = existing.get(key)
        if charge:
            updates = []
            if charge.amount != desired['amount']:
                charge.amount = desired['amount']
                updates.append('amount')
            if charge.status != Charge.Status.POSTED:
                charge.status = Charge.Status.POSTED
                charge.cancellation_reason = ''
                charge.cancelled_at = None
                updates.extend(['status', 'cancellation_reason', 'cancelled_at'])
            if charge.source_rule != desired['source_rule']:
                charge.source_rule = desired['source_rule']
                updates.append('source_rule')
            if updates:
                charge.save(update_fields=updates + ['updated_at'])
                affected.add((member.id, desired['account_type']))
            else:
                unchanged += 1
        else:
            Charge.objects.create(member=member, monthly_job=job, **desired)
            created += 1
            affected.add((member.id, desired['account_type']))

    for key, charge in existing.items():
        if key not in desired_keys and charge.status == Charge.Status.POSTED:
            charge.status = Charge.Status.CANCELLED
            charge.cancellation_reason = '월 부과규칙 재계산 또는 폐업·가입상태 변경'
            charge.cancelled_at = timezone.now()
            charge.save(update_fields=['status', 'cancellation_reason', 'cancelled_at', 'updated_at'])
            cancelled += 1
            affected.add((charge.member_id, charge.account_type))

    if job.is_current:
        for member_id, account_type in affected:
            rebuild_member_account(Member.objects.get(pk=member_id), account_type)

    job.status = MonthlyJob.Status.REVIEW if job.status == MonthlyJob.Status.DRAFT else job.status
    job.save(update_fields=['status', 'updated_at'])
    result = {'created': created, 'cancelled': cancelled, 'unchanged': unchanged}
    log_action(action='generate_monthly_charges', instance=job, after=result, actor=actor)
    return result


@transaction.atomic
def close_member(member: Member, closure_date: date, *, reason='', memo='', actor='admin'):
    member = Member.objects.select_for_update().get(pk=member.pk)
    before = {
        'operational_status': member.operational_status,
        'closed_on': str(member.closed_on or ''),
    }
    ClosureEvent.objects.create(
        member=member, event_type=ClosureEvent.EventType.CLOSE,
        effective_date=closure_date, reason=reason, memo=memo, actor=actor,
    )
    member.operational_status = Member.OperationalStatus.CLOSED
    member.closed_on = closure_date
    member.save(update_fields=['operational_status', 'closed_on', 'updated_at'])

    charges = Charge.objects.filter(
        Q(monthly_job__isnull=True) | Q(monthly_job__is_current=True),
        member=member,
        account_type__in=RECURRING_ACCOUNTS,
        charge_date__year=closure_date.year,
        charge_date__month=closure_date.month,
        status=Charge.Status.POSTED,
    )
    affected = set(charges.values_list('account_type', flat=True))
    charges.update(
        status=Charge.Status.CANCELLED,
        cancellation_reason='폐업월 부과 취소',
        cancelled_at=timezone.now(),
    )
    for account_type in affected or RECURRING_ACCOUNTS:
        rebuild_member_account(member, account_type)

    pending_refunds = []
    for prepayment in Prepayment.objects.filter(member=member, balance__gt=0):
        refund, created = Refund.objects.get_or_create(
            member=member,
            account_type=prepayment.account_type,
            status=Refund.Status.PENDING,
            defaults={'amount': prepayment.balance, 'memo': '폐업에 따른 선납금 환불'},
        )
        if not created and refund.amount != prepayment.balance:
            refund.amount = prepayment.balance
            refund.save(update_fields=['amount', 'updated_at'])
        pending_refunds.append(refund)

    log_action(
        action='member_closed', instance=member, before=before,
        after={'operational_status': member.operational_status, 'closed_on': str(closure_date)},
        reason=reason or memo, actor=actor,
    )
    return pending_refunds


@transaction.atomic
def reopen_member(member: Member, re_registered_on: date, *, memo='', actor='admin'):
    member = Member.objects.select_for_update().get(pk=member.pk)
    before = {'operational_status': member.operational_status, 're_registered_on': str(member.re_registered_on or '')}
    ClosureEvent.objects.create(
        member=member, event_type=ClosureEvent.EventType.REOPEN,
        effective_date=re_registered_on, memo=memo, actor=actor,
    )
    member.operational_status = Member.OperationalStatus.ACTIVE
    member.re_registered_on = re_registered_on
    member.save(update_fields=['operational_status', 're_registered_on', 'updated_at'])
    log_action(
        action='member_reopened', instance=member, before=before,
        after={'operational_status': member.operational_status, 're_registered_on': str(re_registered_on)},
        reason=memo, actor=actor,
    )
    return member


@transaction.atomic
def join_association(member: Member, join_date: date, *, memo='', actor='admin'):
    member = Member.objects.select_for_update().get(pk=member.pk)
    if member.outstanding(AccountType.MANAGEMENT_FEE) > 0:
        member.membership_status = Member.MembershipStatus.PENDING
        member.save(update_fields=['membership_status', 'updated_at'])
        MembershipEvent.objects.create(
            member=member, event_type=MembershipEvent.EventType.PENDING,
            effective_date=join_date, memo='관리비 미수금 정리 필요. ' + memo, actor=actor,
        )
        log_action(
            action='membership_pending', instance=member,
            after={'membership_status': Member.MembershipStatus.PENDING},
            reason='관리비 미수금 정리 필요. ' + memo, actor=actor,
        )
        return False
    before = {'membership_status': member.membership_status, 'membership_started_on': str(member.membership_started_on or '')}
    MembershipEvent.objects.create(
        member=member, event_type=MembershipEvent.EventType.JOIN,
        effective_date=join_date, memo=memo, actor=actor,
    )
    member.membership_status = Member.MembershipStatus.ACTIVE
    member.membership_started_on = join_date
    member.membership_ended_on = None
    member.save(update_fields=['membership_status', 'membership_started_on', 'membership_ended_on', 'updated_at'])
    log_action(action='membership_joined', instance=member, before=before, reason=memo, actor=actor)
    return True


@transaction.atomic
def leave_association(member: Member, leave_date: date, *, memo='', actor='admin'):
    member = Member.objects.select_for_update().get(pk=member.pk)
    if member.outstanding(AccountType.MEMBERSHIP_FEE) > 0:
        raise ValueError('협회비 미수금을 전액 납부해야 탈퇴 처리할 수 있습니다.')
    before = {'membership_status': member.membership_status, 'membership_ended_on': str(member.membership_ended_on or '')}
    MembershipEvent.objects.create(
        member=member, event_type=MembershipEvent.EventType.LEAVE,
        effective_date=leave_date, memo=memo, actor=actor,
    )
    member.membership_status = Member.MembershipStatus.NON_MEMBER
    member.membership_ended_on = leave_date
    member.save(update_fields=['membership_status', 'membership_ended_on', 'updated_at'])

    # The entire leave month association fee is cancelled.
    charges = Charge.objects.filter(
        Q(monthly_job__isnull=True) | Q(monthly_job__is_current=True),
        member=member,
        account_type=AccountType.MEMBERSHIP_FEE,
        charge_date__year=leave_date.year,
        charge_date__month=leave_date.month,
        status=Charge.Status.POSTED,
    )
    charges.update(
        status=Charge.Status.CANCELLED,
        cancellation_reason='탈퇴월 협회비 취소',
        cancelled_at=timezone.now(),
    )
    rebuild_member_account(member, AccountType.MEMBERSHIP_FEE)

    # Management fee starts in the leave month when eligible. Use the leave day as anchor.
    vehicle = vehicle_for_date(member, leave_date)
    eligible = leave_date.year >= 2027 or (vehicle and vehicle.purpose_char == '배')
    if eligible:
        member.management_billing_anchor = leave_date
        member.save(update_fields=['management_billing_anchor', 'updated_at'])
    log_action(action='membership_left', instance=member, before=before, reason=memo, actor=actor)
    return member


def suggested_service_fees(member: Member) -> dict[str, Decimal]:
    if member.membership_status == Member.MembershipStatus.ACTIVE:
        return {AccountType.CERTIFICATE_FEE: Decimal('0'), AccountType.REPLACEMENT_FEE: Decimal('0')}
    is_delivery = member.purpose_char == '배'
    return {
        AccountType.CERTIFICATE_FEE: Decimal('30000') if is_delivery else Decimal('50000'),
        AccountType.REPLACEMENT_FEE: Decimal('0') if is_delivery else Decimal('30000'),
    }
