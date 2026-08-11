from __future__ import annotations

from collections import deque
from datetime import datetime, time
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from core.models import (
    AccountType, Charge, ChargeSettlement, Member, Payment, PaymentAllocationLine,
    Prepayment, PrepaymentMovement, Refund,
)
from core.services.audit import log_action
from core.utils import json_safe_model

RECURRING_ACCOUNTS = {AccountType.MEMBERSHIP_FEE, AccountType.MANAGEMENT_FEE}


def _event_dt(value):
    if isinstance(value, datetime):
        return value
    result = datetime.combine(value, time.min)
    return timezone.make_aware(result, timezone.get_current_timezone())


def _effective_charge_q():
    return Q(monthly_job__isnull=True) | Q(monthly_job__is_current=True)


def _effective_payment_q():
    return Q(payment__monthly_job__isnull=True) | Q(payment__monthly_job__is_current=True)


@transaction.atomic
def rebuild_member_account(member: Member, account_type: str) -> Prepayment:
    """Rebuild settlements and prepayment deterministically for one member/account.

    The source of truth is active charge rows, active payment allocation lines, and
    completed refunds. Rebuilding makes reallocation safe even after a former
    prepayment was consumed by later charges.
    """
    if account_type not in RECURRING_ACCOUNTS:
        prepayment, _ = Prepayment.objects.get_or_create(member=member, account_type=account_type)
        prepayment.balance = Decimal('0')
        prepayment.save(update_fields=['balance', 'updated_at'])
        return prepayment

    old_settlements = ChargeSettlement.objects.filter(
        allocation_line__member=member,
        allocation_line__account_type=account_type,
        is_active=True,
    )
    old_settlements.update(is_active=False)

    prepayment, _ = Prepayment.objects.select_for_update().get_or_create(
        member=member, account_type=account_type,
    )
    prepayment.movements.filter(is_active=True).update(is_active=False)

    charges = list(
        Charge.objects.filter(
            _effective_charge_q(), member=member, account_type=account_type,
            status=Charge.Status.POSTED,
        ).order_by('charge_date', 'id')
    )
    lines = list(
        PaymentAllocationLine.objects.filter(
            _effective_payment_q(), member=member, account_type=account_type,
            status=PaymentAllocationLine.Status.ACTIVE,
            payment__is_effective=True,
        ).exclude(payment__status=Payment.Status.CANCELLED)
        .select_related('payment')
        .order_by('payment__payment_date', 'id')
    )
    refunds = list(
        Refund.objects.filter(
            member=member, account_type=account_type, status=Refund.Status.COMPLETED,
        ).order_by('refund_date', 'id')
    )

    events = []
    # charges first on the same date, then payments, then refunds.
    for charge in charges:
        events.append((_event_dt(charge.charge_date), 0, charge.id, 'charge', charge))
    for line in lines:
        events.append((line.payment.payment_date, 1, line.id, 'payment', line))
    for refund in refunds:
        effective_date = refund.refund_date or refund.created_at.date()
        events.append((_event_dt(effective_date), 2, refund.id, 'refund', refund))
    events.sort(key=lambda x: (x[0], x[1], x[2]))

    # Entries are [source allocation line, remaining amount].
    prepayment_pool = deque()
    # Entries are [charge, remaining amount].
    outstanding = deque()
    sequence = 1

    def settle(source_line, charge, amount):
        nonlocal sequence
        if amount <= 0:
            return
        ChargeSettlement.objects.create(
            allocation_line=source_line,
            charge=charge,
            amount=amount,
            is_active=True,
            sequence=sequence,
        )
        sequence += 1

    def consume_pool_for_charge(charge, remaining):
        nonlocal sequence
        while remaining > 0 and prepayment_pool:
            source_line, available = prepayment_pool[0]
            used = min(remaining, available)
            settle(source_line, charge, used)
            PrepaymentMovement.objects.create(
                prepayment=prepayment,
                movement_type=PrepaymentMovement.MovementType.DEBIT_CHARGE,
                amount=used,
                allocation_line=source_line,
                charge=charge,
                is_active=True,
                sequence=sequence,
            )
            sequence += 1
            remaining -= used
            available -= used
            if available <= 0:
                prepayment_pool.popleft()
            else:
                prepayment_pool[0][1] = available
        return remaining

    for _dt, _order, _id, kind, obj in events:
        if kind == 'charge':
            remaining = consume_pool_for_charge(obj, obj.amount)
            if remaining > 0:
                outstanding.append([obj, remaining])

        elif kind == 'payment':
            remaining = obj.amount
            while remaining > 0 and outstanding:
                charge, due = outstanding[0]
                used = min(remaining, due)
                settle(obj, charge, used)
                remaining -= used
                due -= used
                if due <= 0:
                    outstanding.popleft()
                else:
                    outstanding[0][1] = due
            if remaining > 0:
                prepayment_pool.append([obj, remaining])
                PrepaymentMovement.objects.create(
                    prepayment=prepayment,
                    movement_type=PrepaymentMovement.MovementType.CREDIT_PAYMENT,
                    amount=remaining,
                    allocation_line=obj,
                    is_active=True,
                    sequence=sequence,
                )
                sequence += 1

        elif kind == 'refund':
            remaining = obj.amount
            while remaining > 0 and prepayment_pool:
                source_line, available = prepayment_pool[0]
                used = min(remaining, available)
                PrepaymentMovement.objects.create(
                    prepayment=prepayment,
                    movement_type=PrepaymentMovement.MovementType.DEBIT_REFUND,
                    amount=used,
                    allocation_line=source_line,
                    refund=obj,
                    is_active=True,
                    sequence=sequence,
                )
                sequence += 1
                remaining -= used
                available -= used
                if available <= 0:
                    prepayment_pool.popleft()
                else:
                    prepayment_pool[0][1] = available
            if remaining > 0:
                # Invalid historical data is retained but visibly negative is never cached.
                log_action(
                    action='refund_exceeds_prepayment', instance=obj,
                    after={'uncovered_amount': str(remaining)},
                    reason='환불액이 재계산 선납잔액을 초과함',
                )

    prepayment.balance = sum((entry[1] for entry in prepayment_pool), Decimal('0'))
    prepayment.save(update_fields=['balance', 'updated_at'])
    return prepayment



def member_balance_snapshot(member_ids):
    """Return signed current balances in three aggregate queries.

    net_balance > 0: 미수금, == 0: 완납, < 0: 선납.  The negative sign is
    preserved for every UI/export/message decision instead of converting it to
    a separate positive display value.
    """
    ids = list(dict.fromkeys(int(v) for v in member_ids if v))
    if not ids:
        return {}
    charge_map = {
        row['member_id']: row['total'] or Decimal('0')
        for row in Charge.objects.filter(
            _effective_charge_q(), member_id__in=ids, status=Charge.Status.POSTED,
        ).values('member_id').annotate(total=Sum('amount'))
    }
    # ChargeSettlement is effective through its charge's monthly job, not payment.
    settlement_map = {
        row['charge__member_id']: row['total'] or Decimal('0')
        for row in ChargeSettlement.objects.filter(
            charge__member_id__in=ids,
            charge__status=Charge.Status.POSTED,
            is_active=True,
        ).filter(Q(charge__monthly_job__isnull=True) | Q(charge__monthly_job__is_current=True))
        .values('charge__member_id').annotate(total=Sum('amount'))
    }
    prepayment_map = {
        row['member_id']: row['total'] or Decimal('0')
        for row in Prepayment.objects.filter(member_id__in=ids, balance__gt=0)
        .values('member_id').annotate(total=Sum('balance'))
    }
    result = {}
    for member_id in ids:
        outstanding = max(Decimal('0'), charge_map.get(member_id, Decimal('0')) - settlement_map.get(member_id, Decimal('0')))
        prepayment = max(Decimal('0'), prepayment_map.get(member_id, Decimal('0')))
        result[member_id] = {
            'outstanding': outstanding,
            'prepayment': prepayment,
            'net_balance': outstanding - prepayment,
        }
    return result

def update_payment_status(payment: Payment):
    if payment.status == Payment.Status.CANCELLED:
        return
    total = payment.allocation_lines.filter(status=PaymentAllocationLine.Status.ACTIVE).aggregate(v=Sum('amount'))['v'] or Decimal('0')
    if total <= 0:
        new_status = Payment.Status.OPEN
    elif total < payment.amount:
        new_status = Payment.Status.PARTIAL
    else:
        new_status = Payment.Status.ALLOCATED
    if payment.status != new_status:
        payment.status = new_status
        payment.save(update_fields=['status', 'updated_at'])


@transaction.atomic
def replace_payment_allocations(payment: Payment, allocations: list[dict], *, reason: str = '', actor: str = 'admin'):
    """Replace the user allocation lines of one payment and rebuild affected ledgers.

    allocations: [{'member': Member, 'account_type': str, 'amount': Decimal, 'memo': str}, ...]
    """
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    normalized = []
    total = Decimal('0')
    for row in allocations:
        amount = Decimal(str(row['amount']))
        if amount <= 0:
            continue
        member = row['member']
        account_type = row['account_type']
        normalized.append({
            'member': member,
            'account_type': account_type,
            'amount': amount,
            'memo': row.get('memo', ''),
        })
        total += amount
    if total > payment.amount:
        raise ValueError('분할금액 합계가 원입금액을 초과합니다.')

    before_lines = list(payment.allocation_lines.filter(status=PaymentAllocationLine.Status.ACTIVE).values(
        'id', 'member_id', 'account_type', 'amount', 'memo'
    ))
    impacted = set(
        payment.allocation_lines.filter(status=PaymentAllocationLine.Status.ACTIVE)
        .values_list('member_id', 'account_type')
    )
    payment.allocation_lines.filter(status=PaymentAllocationLine.Status.ACTIVE).update(status=PaymentAllocationLine.Status.CANCELLED)

    created = []
    for row in normalized:
        line = PaymentAllocationLine.objects.create(payment=payment, **row)
        created.append(line)
        impacted.add((line.member_id, line.account_type))

    for member_id, account_type in impacted:
        if account_type in RECURRING_ACCOUNTS:
            rebuild_member_account(Member.objects.get(pk=member_id), account_type)

    update_payment_status(payment)
    if payment.bank_transaction_id:
        tx = payment.bank_transaction
        tx.status = (
            tx.Status.MANUAL_MATCHED if total == payment.amount
            else tx.Status.REVIEW if total > 0
            else tx.Status.UNMATCHED
        )
        tx.save(update_fields=['status', 'updated_at'])
    if payment.card_transaction_id:
        tx = payment.card_transaction
        tx.status = tx.Status.MATCHED if total == payment.amount else tx.Status.REVIEW if total else tx.Status.UNMATCHED
        if total > 0:
            tx.duplicate_suspected = False
        tx.save(update_fields=['status', 'duplicate_suspected', 'updated_at'])

    log_action(
        action='payment_reallocated', instance=payment,
        before={'allocation_lines': [{**x, 'amount': str(x['amount'])} for x in before_lines]},
        after={'allocation_lines': [
            {'id': x.id, 'member_id': x.member_id, 'account_type': x.account_type, 'amount': str(x.amount), 'memo': x.memo}
            for x in created
        ]},
        reason=reason,
        actor=actor,
    )
    return created


@transaction.atomic
def complete_refund(refund: Refund, *, actor='admin'):
    refund = Refund.objects.select_for_update().get(pk=refund.pk)
    prepayment = rebuild_member_account(refund.member, refund.account_type)
    # Pending refund is not included in rebuild, so current balance is available.
    if refund.amount > prepayment.balance:
        raise ValueError(f'환불액이 선납잔액({prepayment.balance:,.0f}원)을 초과합니다.')
    if not refund.refund_date:
        refund.refund_date = timezone.localdate()
    refund.status = Refund.Status.COMPLETED
    refund.completed_at = timezone.now()
    refund.save(update_fields=['refund_date', 'status', 'completed_at', 'updated_at'])
    rebuild_member_account(refund.member, refund.account_type)
    log_action(action='refund_completed', instance=refund, actor=actor)
    return refund
