from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from core.models import (
    AccountType, Charge, Member, MonthlyJob, Payment, Vehicle,
)
from core.services.billing import (
    desired_recurring_charge, join_association, membership_fee_amount,
    suggested_service_fees,
)
from core.services.ledger import replace_payment_allocations


class BusinessRuleTests(TestCase):
    def make_member(self, *, name='홍길동', birth6='600101', vehicle='강원86배1000', joined=False):
        member = Member.objects.create(
            name=name,
            birth6=birth6,
            first_seen_on=date(2026, 1, 1),
            certificate_issued_on=date(2026, 1, 10),
            certificate_date_recorded_on=date(2026, 1, 10),
            membership_status=Member.MembershipStatus.ACTIVE if joined else Member.MembershipStatus.NON_MEMBER,
            membership_started_on=date(2026, 1, 10) if joined else None,
        )
        Vehicle.objects.create(
            member=member,
            vehicle_no=vehicle,
            normalized_vehicle_no=vehicle.replace(' ', ''),
            purpose_char='배' if '배' in vehicle else '바',
            start_date=date(2026, 1, 1),
            is_current=True,
        )
        return member

    def test_membership_fee_reduces_from_calendar_year_turning_70(self):
        member = self.make_member(birth6='570101', joined=True)
        self.assertEqual(membership_fee_amount(member, 2026), Decimal('10000'))
        self.assertEqual(membership_fee_amount(member, 2027), Decimal('5000'))

    def test_2026_only_delivery_non_member_gets_management_fee(self):
        delivery = self.make_member(vehicle='강원86배1000')
        general = self.make_member(name='김일반', vehicle='강원86바1001')
        self.assertEqual(desired_recurring_charge(delivery, 2026, 2)['amount'], Decimal('5000'))
        self.assertIsNone(desired_recurring_charge(general, 2026, 2))

    def test_2027_existing_non_member_billed_on_first(self):
        member = self.make_member(vehicle='강원86바1000')
        result = desired_recurring_charge(member, 2027, 1)
        self.assertEqual(result['charge_date'], date(2027, 1, 1))
        self.assertEqual(result['amount'], Decimal('5000'))

    def test_service_fee_suggestions(self):
        delivery = self.make_member(vehicle='강원86배1000')
        general = self.make_member(name='김일반', vehicle='강원86바1001')
        joined = self.make_member(name='김회원', vehicle='강원86바1002', joined=True)
        self.assertEqual(suggested_service_fees(delivery)[AccountType.CERTIFICATE_FEE], Decimal('30000'))
        self.assertEqual(suggested_service_fees(delivery)[AccountType.REPLACEMENT_FEE], Decimal('0'))
        self.assertEqual(suggested_service_fees(general)[AccountType.CERTIFICATE_FEE], Decimal('50000'))
        self.assertEqual(suggested_service_fees(general)[AccountType.REPLACEMENT_FEE], Decimal('30000'))
        self.assertEqual(suggested_service_fees(joined)[AccountType.CERTIFICATE_FEE], Decimal('0'))

    def test_oldest_receivable_first_and_excess_becomes_prepayment(self):
        member = self.make_member()
        Charge.objects.create(
            member=member, account_type=AccountType.MANAGEMENT_FEE,
            charge_date=date(2026, 2, 10), amount=Decimal('5000'), source_rule='test',
        )
        Charge.objects.create(
            member=member, account_type=AccountType.MANAGEMENT_FEE,
            charge_date=date(2026, 3, 10), amount=Decimal('5000'), source_rule='test',
        )
        payment = Payment.objects.create(
            source_type=Payment.SourceType.MANUAL,
            payment_date=timezone.make_aware(datetime(2026, 3, 20, 10, 0)),
            amount=Decimal('12000'),
        )
        replace_payment_allocations(payment, [{
            'member': member,
            'account_type': AccountType.MANAGEMENT_FEE,
            'amount': Decimal('12000'),
        }])
        charges = list(member.charges.order_by('charge_date'))
        self.assertEqual(charges[0].balance, Decimal('0'))
        self.assertEqual(charges[1].balance, Decimal('0'))
        self.assertEqual(member.prepayments.get(account_type=AccountType.MANAGEMENT_FEE).balance, Decimal('2000'))

    def test_join_is_pending_when_management_arrears_exist(self):
        member = self.make_member()
        Charge.objects.create(
            member=member, account_type=AccountType.MANAGEMENT_FEE,
            charge_date=date(2026, 2, 10), amount=Decimal('5000'), source_rule='test',
        )
        completed = join_association(member, date(2026, 3, 1))
        member.refresh_from_db()
        self.assertFalse(completed)
        self.assertEqual(member.membership_status, Member.MembershipStatus.PENDING)

    def test_job_month_charge_isolated_by_current_version(self):
        member = self.make_member()
        old = MonthlyJob.objects.create(year=2026, month=2, version=1, version_name='1차', is_current=False)
        current = MonthlyJob.objects.create(year=2026, month=2, version=2, version_name='2차', is_current=True)
        Charge.objects.create(member=member, account_type=AccountType.MANAGEMENT_FEE, charge_date=date(2026, 2, 10), amount=5000, source_rule='old', monthly_job=old)
        Charge.objects.create(member=member, account_type=AccountType.MANAGEMENT_FEE, charge_date=date(2026, 2, 10), amount=5000, source_rule='new', monthly_job=current)
        self.assertEqual(member.total_outstanding, Decimal('5000'))
