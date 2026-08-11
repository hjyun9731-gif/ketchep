from datetime import date, datetime
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
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


class TemplateSmokeTests(TestCase):
    def test_all_html_templates_compile(self):
        from pathlib import Path
        from django.conf import settings
        from django.template.loader import get_template

        roots = [
            Path(settings.BASE_DIR) / 'templates',
            Path(settings.BASE_DIR) / 'core' / 'templates',
        ]
        names = set()
        for root in roots:
            if root.exists():
                names.update(path.relative_to(root).as_posix() for path in root.rglob('*.html'))
        self.assertTrue(names)
        for name in sorted(names):
            with self.subTest(template=name):
                get_template(name)


class BalsongClientTests(TestCase):
    def test_sms_lms_payload_is_sent_once_for_multiple_recipients(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from django.test import override_settings
        from core.services.messaging import BalsongClient

        recipients = [
            SimpleNamespace(
                body='홍길동 님의 미수금은 10,000원입니다. 납부기한까지 납부해 주시기 바랍니다.',
                phone='010-1111-2222',
                member=SimpleNamespace(name='홍길동'),
            ),
            SimpleNamespace(
                body='김영희 님의 미수금은 20,000원입니다. 납부기한까지 납부해 주시기 바랍니다.',
                phone='010-3333-4444',
                member=SimpleNamespace(name='김영희'),
            ),
        ]
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'Result': 'OK', 'Code': 0, 'Service': 'LMS', 'Job_No': 12345,
        }
        with override_settings(
            BALSONG_API_URL='https://balsong.com/Linkage/API/',
            BALSONG_USER_ID='user',
            BALSONG_USER_PW='pw',
            BALSONG_CALLBACK='0331234567',
            BALSONG_DRY_RUN=False,
            ASSOCIATION_NAME='강원 화물협회',
        ):
            with patch('core.services.messaging.requests.post', return_value=response) as post:
                result = BalsongClient().send(subject='미수금 납부 안내', recipients=recipients)

        self.assertEqual(result['Job_No'], 12345)
        self.assertEqual(post.call_count, 1)
        payload = post.call_args.kwargs['data']
        self.assertEqual(payload['Type'], 'Send')
        self.assertEqual(payload['Service'], 'LMS')
        self.assertEqual(payload['Callback'], '0331234567')
        destination = json.loads(payload['Destination'])
        self.assertEqual(len(destination), 2)
        self.assertEqual(destination[0]['Phone'], '01011112222')
        self.assertIn('10,000원', destination[0]['Msg_Text'])

    def test_api_error_response_raises(self):
        from unittest.mock import Mock, patch
        from django.test import override_settings
        from core.services.messaging import BalsongAPIError, BalsongClient

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'Result': 'ERROR', 'Code': 100, 'Message': '인증 실패',
        }
        with override_settings(
            BALSONG_API_URL='https://balsong.com/Linkage/API/',
            BALSONG_USER_ID='user',
            BALSONG_USER_PW='bad',
            BALSONG_CALLBACK='0331234567',
            BALSONG_DRY_RUN=False,
        ):
            with patch('core.services.messaging.requests.post', return_value=response):
                with self.assertRaises(BalsongAPIError):
                    BalsongClient().callback_list()


class LegacyHeaderDetectionTests(TestCase):
    def test_license_header_prefers_mobile_phone(self):
        from core.models import UploadedFile
        from core.services.imports import detect_header

        rows = [[
            '지역', '관리번호', '차량번호', '성    명', '주민등록번호',
            '주               소', '전화번호', '핸 드 폰', '인가일자',
            '가입일자', '자격증명\n발급일자', '비고',
        ]]
        score, header_index, mapping = detect_header(
            rows, UploadedFile.SlotType.LICENSE,
        )
        self.assertEqual(header_index, 0)
        self.assertEqual(mapping['vehicle_no'], 2)
        self.assertEqual(mapping['name'], 3)
        self.assertEqual(mapping['phone'], 7)
        self.assertGreaterEqual(score, 8)

    def test_receivables_header_uses_latest_populated_month(self):
        from core.models import UploadedFile
        from core.services.imports import detect_header

        rows = [
            [
                '지역', '계정', '비고', '차량번호', '성 명',
                '6월 미수금', '7월 미수금', '8월 미수금',
            ],
            ['강릉시', '관리비', '', '82배1001', '홍길동', 5000, 10000, None],
            ['원주시', '협회비', '', '83바1002', '김영희', 0, 20000, None],
        ]
        _, _, mapping = detect_header(
            rows, UploadedFile.SlotType.RECEIVABLES,
        )
        self.assertEqual(mapping['balance'], 6)


class NhHeaderlessPasteRegressionTests(SimpleTestCase):
    def test_nh_headerless_mapping_uses_deposit_not_running_balance(self):
        from core.services.paste_import import _nh_headerless_mapping, _parse_bank_money
        rows = [
            ['3', '2026-07-28', '30,000', '35,175,145', '폰토스뱅크', '1069', '토스뱅크 0921008'],
            ['4', '2026-07-28', '630,000', '35,805,145', '폰우체국', '김문영', '우체국 0720102'],
            ['5', '2026-07-28', '150,000', '35,955,145', '폰신한은행', '미도상', '신한 0887715'],
            ['6', '2026-07-28', '530,035', '36,485,180', 'PC신한은행', 'ciderpay', '신한 0218290'],
        ]
        mapping = _nh_headerless_mapping(rows)
        self.assertEqual(mapping['transaction_at'], 1)
        self.assertEqual(mapping['amount'], 2)
        self.assertEqual(mapping['balance'], 3)
        self.assertEqual(mapping['payer_text'], 5)
        self.assertEqual(_parse_bank_money(rows[1][mapping['amount']]), Decimal('630000'))
        self.assertEqual(_parse_bank_money(rows[1][mapping['balance']]), Decimal('35805145'))
        self.assertEqual(rows[1][mapping['payer_text']], '김문영')

    def test_dot_thousands_are_parsed_as_won(self):
        from core.services.paste_import import _parse_bank_money
        self.assertEqual(_parse_bank_money('35.805.145'), Decimal('35805145'))
        self.assertEqual(_parse_bank_money('630.000'), Decimal('630000'))


class V440RegressionTests(TestCase):
    def test_card_headers_match_real_altolan_and_cider_exports(self):
        from core.models import UploadedFile
        from core.services.imports import detect_header
        altolan = [['연번','처리결과','분류','고객조회번호','거  래  처','발송월일','수납월일','수납금액','이체일','이체금액','수수료']]
        _, _, mapping = detect_header(altolan, UploadedFile.SlotType.ALTOLAN)
        self.assertEqual(mapping['vehicle_no'], 4)
        self.assertEqual(mapping['gross'], 7)
        self.assertEqual(mapping['net'], 9)
        self.assertEqual(mapping['fee'], 10)
        cider = [['주문번호','완료일시','판매금액','정산일시','정산금액','구매자명','고객번호','결제상태']]
        _, _, mapping = detect_header(cider, UploadedFile.SlotType.CIDER)
        self.assertEqual(mapping['transaction_id'], 0)
        self.assertEqual(mapping['gross'], 2)
        self.assertEqual(mapping['vehicle_no'], 6)

    def test_member_picker_default_account_uses_existing_account_enum(self):
        self.assertTrue(hasattr(AccountType, 'MEMBERSHIP_FEE'))
        self.assertFalse(hasattr(AccountType, 'ASSOCIATION_DUE'))
