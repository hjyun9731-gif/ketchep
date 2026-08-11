from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone


MONEY_ZERO = Decimal('0')


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AccountType(models.TextChoices):
    MEMBERSHIP_FEE = 'membership_fee', '협회비'
    MANAGEMENT_FEE = 'management_fee', '관리비'
    CERTIFICATE_FEE = 'certificate_fee', '자격증명 발급비'
    REPLACEMENT_FEE = 'replacement_fee', '대폐차비'
    OTHER_INCOME = 'other_income', '기타수입'
    SUSPENSE = 'suspense', '가수금/확인필요'


class Member(TimeStampedModel):
    class OperationalStatus(models.TextChoices):
        ACTIVE = 'active', '정상'
        CLOSED = 'closed', '폐업'

    class MembershipStatus(models.TextChoices):
        NON_MEMBER = 'non_member', '비가입'
        ACTIVE = 'active', '협회가입'
        PENDING = 'pending', '가입대기'

    class CollectionStatus(models.TextChoices):
        NONE = 'none', '미연락'
        CONTACTED = 'contacted', '연락완료'
        INSTALLMENT = 'installment', '분할납부 협의'
        LEGAL_NOTICE_PLANNED = 'legal_notice_planned', '내용증명 예정'
        DISPUTED = 'disputed', '이의·확인중'

    name = models.CharField('성명', max_length=100, db_index=True)
    birth6 = models.CharField(
        '주민등록번호 앞 6자리',
        max_length=6,
        blank=True,
        validators=[RegexValidator(r'^\d{6}$', '숫자 6자리로 입력하세요.')],
        db_index=True,
    )
    phone = models.CharField('휴대전화번호', max_length=30, blank=True, db_index=True)
    address = models.CharField('기본 주소', max_length=500, blank=True)
    official_address = models.CharField('공문 주소', max_length=500, blank=True)
    official_address_custom = models.BooleanField('공문주소 수동관리', default=False)
    memo = models.TextField('비고', blank=True)
    region = models.CharField('지역', max_length=100, blank=True, db_index=True)
    management_no = models.CharField('관리번호', max_length=100, blank=True, db_index=True)
    receivable_account_type = models.CharField(
        '미수금 계정', max_length=30, choices=[
            (AccountType.MEMBERSHIP_FEE, '협회비'),
            (AccountType.MANAGEMENT_FEE, '관리비'),
        ], blank=True, db_index=True,
        help_text='회원별 월 정기계정. 협회비와 관리비는 동시에 사용할 수 없습니다.',
    )

    operational_status = models.CharField(
        '운영상태', max_length=20, choices=OperationalStatus.choices,
        default=OperationalStatus.ACTIVE, db_index=True,
    )
    closed_on = models.DateField('폐업일', null=True, blank=True, db_index=True)
    re_registered_on = models.DateField('재등록일', null=True, blank=True)

    membership_status = models.CharField(
        '가입상태', max_length=20, choices=MembershipStatus.choices,
        default=MembershipStatus.NON_MEMBER, db_index=True,
    )
    membership_started_on = models.DateField('협회가입일', null=True, blank=True, db_index=True)
    membership_ended_on = models.DateField('협회탈퇴일', null=True, blank=True)
    membership_mark_raw = models.CharField('원본 가입표시', max_length=50, blank=True)
    membership_billing_anchor = models.DateField(
        '협회비 부과기준일', null=True, blank=True,
        help_text='가입일이 날짜로 확인되지 않는 기존 가입자의 월 부과 기준일',
    )

    management_billing_anchor = models.DateField(
        '관리비 부과기준일', null=True, blank=True,
        help_text='기존 비가입자의 월 관리비 부과 기준일',
    )

    first_seen_on = models.DateField('최초 등록 확인일', null=True, blank=True, db_index=True)
    certificate_issued_on = models.DateField('자격증명 발급일', null=True, blank=True, db_index=True)
    certificate_date_recorded_on = models.DateField(
        '발급일 시스템등록일', null=True, blank=True,
        help_text='지연 입력 시 소급부과를 막기 위한 기준일',
    )

    address_needs_check = models.BooleanField('주소 확인 필요', default=False, db_index=True)
    phone_needs_check = models.BooleanField('연락처 확인 필요', default=False, db_index=True)
    sms_opt_out = models.BooleanField('문자 수신거부', default=False, db_index=True)
    collection_status = models.CharField(
        '미수금 연락상태', max_length=30, choices=CollectionStatus.choices,
        default=CollectionStatus.NONE, db_index=True,
    )
    source_row_key = models.CharField('원본 식별값', max_length=255, blank=True)
    association_system_registration_needed = models.BooleanField('협회관리시스템 예정자 등록 필요', default=False, db_index=True)
    is_active_record = models.BooleanField('유효 원장', default=True, db_index=True)

    class Meta:
        ordering = ['name', 'id']
        indexes = [
            models.Index(fields=['name', 'birth6']),
            models.Index(fields=['operational_status', 'membership_status']),
        ]
        verbose_name = '회원'
        verbose_name_plural = '회원'

    def __str__(self):
        vehicle = self.current_vehicle
        return f'{self.name} ({vehicle.vehicle_no if vehicle else "차량없음"})'

    def save(self, *args, **kwargs):
        if not self.official_address and self.address and not self.official_address_custom:
            self.official_address = self.address
        memo_normalized = (self.memo or '').replace(' ', '').lower()
        if '결번' in memo_normalized:
            self.phone_needs_check = True
        if '수신거부' in memo_normalized:
            self.sms_opt_out = True
        super().save(*args, **kwargs)

    @property
    def current_vehicle(self):
        return self.vehicles.filter(is_current=True).first()

    @property
    def purpose_char(self):
        vehicle = self.current_vehicle
        return vehicle.purpose_char if vehicle else ''

    @property
    def can_receive_sms(self):
        return bool(self.phone and not self.phone_needs_check and not self.sms_opt_out)

    def outstanding(self, account_type: str | None = None) -> Decimal:
        charges = self.charges.filter(
            Q(monthly_job__isnull=True) | Q(monthly_job__is_current=True),
            status=Charge.Status.POSTED,
        )
        if account_type:
            charges = charges.filter(account_type=account_type)
        total = MONEY_ZERO
        for charge in charges:
            total += charge.balance
        return total

    @property
    def total_outstanding(self):
        return self.outstanding()


class Vehicle(TimeStampedModel):
    member = models.ForeignKey(Member, related_name='vehicles', on_delete=models.PROTECT)
    vehicle_no = models.CharField('차량번호', max_length=50, db_index=True)
    normalized_vehicle_no = models.CharField('정규화 차량번호', max_length=50, db_index=True)
    purpose_char = models.CharField('용도기호', max_length=2, blank=True, db_index=True)
    start_date = models.DateField('사용 시작일', null=True, blank=True)
    end_date = models.DateField('사용 종료일', null=True, blank=True)
    is_current = models.BooleanField('현재 차량', default=True, db_index=True)
    change_reason = models.CharField('변경 사유', max_length=200, blank=True)

    class Meta:
        ordering = ['-is_current', '-start_date', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['member'], condition=Q(is_current=True),
                name='one_current_vehicle_per_member',
            ),
        ]
        verbose_name = '차량'
        verbose_name_plural = '차량'

    def __str__(self):
        return self.vehicle_no


class PayerAlias(TimeStampedModel):
    member = models.ForeignKey(Member, related_name='payer_aliases', on_delete=models.PROTECT)
    alias = models.CharField('입금자 별칭', max_length=200)
    normalized_alias = models.CharField('정규화 별칭', max_length=200, db_index=True)
    bank_account_label = models.CharField('적용 계좌', max_length=100, blank=True)
    auto_apply = models.BooleanField('다음부터 자동매칭', default=True)
    memo = models.CharField('메모', max_length=255, blank=True)
    actor = models.CharField(max_length=50, default='admin')

    class Meta:
        ordering = ['normalized_alias', 'member__name']
        constraints = [
            models.UniqueConstraint(
                fields=['member', 'normalized_alias', 'bank_account_label'],
                name='unique_member_payer_alias_per_account',
            ),
        ]

    def __str__(self):
        return f'{self.alias} → {self.member.name}'


class MembershipEvent(TimeStampedModel):
    class EventType(models.TextChoices):
        JOIN = 'join', '가입'
        LEAVE = 'leave', '탈퇴'
        PENDING = 'pending', '가입대기'
        CANCEL_PENDING = 'cancel_pending', '가입대기 취소'

    member = models.ForeignKey(Member, related_name='membership_events', on_delete=models.PROTECT)
    event_type = models.CharField(max_length=30, choices=EventType.choices)
    effective_date = models.DateField(db_index=True)
    memo = models.TextField(blank=True)
    actor = models.CharField(max_length=50, default='admin')

    class Meta:
        ordering = ['effective_date', 'id']


class ClosureEvent(TimeStampedModel):
    class EventType(models.TextChoices):
        CLOSE = 'close', '폐업'
        REOPEN = 'reopen', '재등록'

    member = models.ForeignKey(Member, related_name='closure_events', on_delete=models.PROTECT)
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    effective_date = models.DateField(db_index=True)
    reason = models.CharField(max_length=200, blank=True)
    memo = models.TextField(blank=True)
    actor = models.CharField(max_length=50, default='admin')

    class Meta:
        ordering = ['effective_date', 'id']


class MemberLink(TimeStampedModel):
    class LinkType(models.TextChoices):
        SAME_PERSON = 'same_person', '폐업 후 동일인 재등록'
        FAMILY_SUCCESSION = 'family_succession', '가족·직계 승계'
        GENERAL_TRANSFER = 'general_transfer', '일반 양도양수'
        MOVE = 'move', '강원도 내 이전'

    old_member = models.ForeignKey(Member, related_name='outgoing_links', on_delete=models.PROTECT)
    new_member = models.ForeignKey(Member, related_name='incoming_links', on_delete=models.PROTECT)
    link_type = models.CharField(max_length=30, choices=LinkType.choices)
    effective_date = models.DateField()
    arrears_transferred = models.BooleanField(default=False)
    memo = models.TextField(blank=True)


class MonthlyJob(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', '작성중'
        REVIEW = 'review', '검토중'
        FINAL = 'final', '최종확정'
        MODIFIED = 'modified', '수정됨'

    year = models.PositiveSmallIntegerField(db_index=True)
    month = models.PositiveSmallIntegerField(db_index=True)
    version = models.PositiveIntegerField(default=1)
    version_name = models.CharField(max_length=100, default='1차 작업')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    based_on = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='derived_versions')
    is_current = models.BooleanField(default=False, db_index=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    memo = models.TextField(blank=True)

    class Meta:
        ordering = ['-year', '-month', '-version']
        constraints = [
            models.UniqueConstraint(fields=['year', 'month', 'version'], name='unique_month_job_version'),
            models.UniqueConstraint(
                fields=['year', 'month'], condition=Q(is_current=True),
                name='one_current_job_per_month',
            ),
            models.CheckConstraint(condition=Q(month__gte=1, month__lte=12), name='valid_job_month'),
        ]

    def __str__(self):
        return f'{self.year}년 {self.month}월 {self.version_name}'

    @property
    def period_start(self):
        from datetime import date
        return date(self.year, self.month, 1)

    @property
    def period_end(self):
        import calendar
        from datetime import date
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])


class UploadedFile(TimeStampedModel):
    class SlotType(models.TextChoices):
        RECEIVABLES = 'receivables', '미수금 통합문서'
        BANK_1 = 'bank_1', '통장 거래내역 1'
        BANK_2 = 'bank_2', '통장 거래내역 2'
        BANK_3 = 'bank_3', '통장 거래내역 3'
        LICENSE = 'license', '전체면허자현황'
        ALTOLAN = 'altolan', '알토란 결제내역'
        CIDER = 'cider', '사이다페이 결제내역'

    class ParseStatus(models.TextChoices):
        UPLOADED = 'uploaded', '업로드됨'
        NEEDS_MAPPING = 'needs_mapping', '열 매핑 필요'
        PARSED = 'parsed', '파싱완료'
        PROCESSED = 'processed', '반영완료'
        FAILED = 'failed', '실패'

    job = models.ForeignKey(MonthlyJob, related_name='uploaded_files', on_delete=models.PROTECT)
    slot_type = models.CharField(max_length=30, choices=SlotType.choices, db_index=True)
    file = models.FileField(upload_to='uploads/%Y/%m/')
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, db_index=True)
    size = models.PositiveBigIntegerField(default=0)
    parse_status = models.CharField(max_length=30, choices=ParseStatus.choices, default=ParseStatus.UPLOADED)
    parse_error = models.TextField(blank=True)
    header_row = models.PositiveIntegerField(null=True, blank=True)
    detected_headers = models.JSONField(default=list, blank=True)
    column_mapping = models.JSONField(default=dict, blank=True)
    parse_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['slot_type', '-created_at']

    def __str__(self):
        return f'{self.get_slot_type_display()} - {self.original_name}'

    @property
    def extension(self):
        return Path(self.original_name).suffix.lower()


class ParsedRow(models.Model):
    uploaded_file = models.ForeignKey(UploadedFile, related_name='parsed_rows', on_delete=models.CASCADE)
    sheet_name = models.CharField(max_length=255)
    source_row = models.PositiveIntegerField()
    raw_data = models.JSONField(default=dict)
    row_hash = models.CharField(max_length=64, db_index=True)

    class Meta:
        ordering = ['sheet_name', 'source_row']
        constraints = [
            models.UniqueConstraint(fields=['uploaded_file', 'sheet_name', 'source_row'], name='unique_parsed_source_row')
        ]


class ImportIssue(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = 'open', '미처리'
        RESOLVED = 'resolved', '처리완료'
        IGNORED = 'ignored', '제외'

    uploaded_file = models.ForeignKey(UploadedFile, related_name='import_issues', on_delete=models.CASCADE)
    sheet_name = models.CharField(max_length=255, blank=True)
    source_row = models.PositiveIntegerField(null=True, blank=True)
    issue_type = models.CharField(max_length=100, db_index=True)
    message = models.TextField()
    candidate_member_ids = models.JSONField(default=list, blank=True)
    raw_data = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)

    class Meta:
        ordering = ['uploaded_file', 'source_row', 'id']



class BankTransaction(TimeStampedModel):
    class Status(models.TextChoices):
        UNMATCHED = 'unmatched', '미배정'
        AUTO_MATCHED = 'auto_matched', '자동매칭'
        MANUAL_MATCHED = 'manual_matched', '수동매칭'
        REVIEW = 'review', '확인필요'
        DUPLICATE = 'duplicate', '중복의심'
        IGNORED = 'ignored', '제외'

    job = models.ForeignKey(MonthlyJob, related_name='bank_transactions', on_delete=models.PROTECT)
    uploaded_file = models.ForeignKey(UploadedFile, related_name='bank_transactions', on_delete=models.PROTECT)
    txn_key = models.CharField(max_length=255, db_index=True)
    occurrence_no = models.PositiveIntegerField(default=1)
    bank_account_label = models.CharField(max_length=100, blank=True)
    transaction_at = models.DateTimeField(null=True, blank=True, db_index=True)
    payer_text = models.CharField(max_length=500, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    source_sheet = models.CharField(max_length=255, blank=True)
    source_row = models.PositiveIntegerField()
    raw_data = models.JSONField(default=dict)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.UNMATCHED, db_index=True)
    match_reason = models.CharField(max_length=255, blank=True)
    duplicate_group_key = models.CharField(max_length=255, blank=True, db_index=True)
    is_card_settlement = models.BooleanField('카드사 순정산입금', default=False, db_index=True)
    card_provider = models.CharField('정산 카드사', max_length=20, blank=True)
    is_effective = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ['transaction_at', 'source_row', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['uploaded_file', 'source_sheet', 'source_row'],
                name='unique_bank_source_row',
            ),
        ]

    def __str__(self):
        return f'{self.transaction_at or "날짜없음"} {self.payer_text} {self.amount}'

    @property
    def allocated_amount(self):
        if not hasattr(self, 'payment'):
            return MONEY_ZERO
        return self.payment.allocation_lines.filter(status=PaymentAllocationLine.Status.ACTIVE).aggregate(v=Sum('amount'))['v'] or MONEY_ZERO

    @property
    def unallocated_amount(self):
        return self.amount - self.allocated_amount


class CardTransaction(TimeStampedModel):
    class Provider(models.TextChoices):
        ALTOLAN = 'altolan', '알토란'
        CIDER = 'cider', '사이다페이'

    class Status(models.TextChoices):
        UNMATCHED = 'unmatched', '미배정'
        MATCHED = 'matched', '매칭완료'
        REVIEW = 'review', '확인필요'
        DUPLICATE = 'duplicate', '중복의심'
        CANCELLED = 'cancelled', '결제취소'
        IGNORED = 'ignored', '제외'

    job = models.ForeignKey(MonthlyJob, related_name='card_transactions', on_delete=models.PROTECT)
    uploaded_file = models.ForeignKey(UploadedFile, related_name='card_transactions', on_delete=models.PROTECT)
    provider = models.CharField(max_length=20, choices=Provider.choices)
    txn_key = models.CharField(max_length=255, db_index=True)
    transaction_at = models.DateTimeField(null=True, blank=True)
    vehicle_no = models.CharField(max_length=50, blank=True, db_index=True)
    member_name = models.CharField(max_length=100, blank=True, db_index=True)
    gross_amount = models.DecimalField(max_digits=14, decimal_places=2)
    fee_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    settlement_date = models.DateField(null=True, blank=True)
    source_sheet = models.CharField(max_length=255, blank=True)
    source_row = models.PositiveIntegerField()
    raw_data = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UNMATCHED)
    duplicate_suspected = models.BooleanField(default=False)
    is_effective = models.BooleanField(default=True)

    class Meta:
        ordering = ['transaction_at', 'source_row', 'id']
        constraints = [
            models.UniqueConstraint(fields=['uploaded_file', 'source_sheet', 'source_row'], name='unique_card_source_row')
        ]


class Charge(TimeStampedModel):
    class Status(models.TextChoices):
        POSTED = 'posted', '부과'
        CANCELLED = 'cancelled', '취소'

    member = models.ForeignKey(Member, related_name='charges', on_delete=models.PROTECT)
    account_type = models.CharField(max_length=30, choices=AccountType.choices, db_index=True)
    charge_date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.POSTED, db_index=True)
    source_rule = models.CharField(max_length=100)
    monthly_job = models.ForeignKey(MonthlyJob, related_name='charges', null=True, blank=True, on_delete=models.PROTECT)
    cancellation_reason = models.CharField(max_length=255, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['charge_date', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['member', 'account_type', 'charge_date', 'monthly_job'],
                name='unique_member_charge_per_job_date',
            ),
        ]

    def __str__(self):
        return f'{self.member.name} {self.get_account_type_display()} {self.charge_date} {self.amount}'

    @property
    def settled_amount(self):
        if self.status != self.Status.POSTED:
            return MONEY_ZERO
        return self.settlements.filter(is_active=True).aggregate(v=Sum('amount'))['v'] or MONEY_ZERO

    @property
    def balance(self):
        if self.status != self.Status.POSTED:
            return MONEY_ZERO
        return max(MONEY_ZERO, self.amount - self.settled_amount)


class Payment(TimeStampedModel):
    class SourceType(models.TextChoices):
        BANK = 'bank', '통장'
        CARD = 'card', '카드'
        MANUAL = 'manual', '수기입금'
        OPENING = 'opening', '기초자료'

    class Status(models.TextChoices):
        OPEN = 'open', '미배정'
        PARTIAL = 'partial', '부분배정'
        ALLOCATED = 'allocated', '완전배정'
        CANCELLED = 'cancelled', '취소'

    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    payment_date = models.DateTimeField(db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    bank_transaction = models.OneToOneField(BankTransaction, related_name='payment', null=True, blank=True, on_delete=models.PROTECT)
    card_transaction = models.OneToOneField(CardTransaction, related_name='payment', null=True, blank=True, on_delete=models.PROTECT)
    monthly_job = models.ForeignKey(MonthlyJob, related_name='payments', null=True, blank=True, on_delete=models.PROTECT)
    memo = models.TextField(blank=True)
    is_effective = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ['payment_date', 'id']

    @property
    def allocated_amount(self):
        return self.allocation_lines.filter(status=PaymentAllocationLine.Status.ACTIVE).aggregate(v=Sum('amount'))['v'] or MONEY_ZERO

    @property
    def unallocated_amount(self):
        return self.amount - self.allocated_amount


class PaymentAllocationLine(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = 'active', '유효'
        CANCELLED = 'cancelled', '취소'

    payment = models.ForeignKey(Payment, related_name='allocation_lines', on_delete=models.PROTECT)
    member = models.ForeignKey(Member, related_name='payment_allocation_lines', on_delete=models.PROTECT)
    account_type = models.CharField(max_length=30, choices=AccountType.choices, db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    memo = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['payment__payment_date', 'id']


class ChargeSettlement(TimeStampedModel):
    allocation_line = models.ForeignKey(PaymentAllocationLine, related_name='settlements', on_delete=models.PROTECT)
    charge = models.ForeignKey(Charge, related_name='settlements', on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    is_active = models.BooleanField(default=True, db_index=True)
    sequence = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['charge__charge_date', 'sequence', 'id']


class Prepayment(TimeStampedModel):
    member = models.ForeignKey(Member, related_name='prepayments', on_delete=models.PROTECT)
    account_type = models.CharField(max_length=30, choices=AccountType.choices)
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['member', 'account_type'], name='unique_member_prepayment_account')
        ]


class HistoricalPaymentRecord(TimeStampedModel):
    """Read-only historical payment facts imported from the legacy receivables workbook.

    These rows are intentionally separate from Payment/PaymentAllocationLine so that
    showing 2026 Jan-Jul history never changes the live ledger, current arrears, or
    prepayment balances.
    """
    member = models.ForeignKey(Member, related_name='historical_payment_records', on_delete=models.PROTECT)
    uploaded_file = models.ForeignKey(UploadedFile, related_name='historical_payment_records', null=True, blank=True, on_delete=models.SET_NULL)
    year = models.PositiveSmallIntegerField(db_index=True)
    month = models.PositiveSmallIntegerField(db_index=True)
    account_type = models.CharField(max_length=30, choices=AccountType.choices, db_index=True)
    payment_date = models.DateField(null=True, blank=True, db_index=True)
    payment_date_text = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    source_label = models.CharField(max_length=100, default='미수금 원본')
    source_key = models.CharField(max_length=255, unique=True, db_index=True)
    raw_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-year', '-month', '-payment_date', '-id']
        indexes = [
            models.Index(fields=['member', 'year', 'month']),
        ]


class Refund(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = 'pending', '환불대기'
        COMPLETED = 'completed', '환불완료'
        CANCELLED = 'cancelled', '취소'

    member = models.ForeignKey(Member, related_name='refunds', on_delete=models.PROTECT)
    account_type = models.CharField(max_length=30, choices=AccountType.choices, default=AccountType.MANAGEMENT_FEE)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    bank = models.CharField(max_length=100, blank=True)
    account_no = models.CharField(max_length=100, blank=True)
    holder = models.CharField(max_length=100, blank=True)
    refund_date = models.DateField(null=True, blank=True)
    method = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    memo = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class PrepaymentMovement(TimeStampedModel):
    class MovementType(models.TextChoices):
        CREDIT_PAYMENT = 'credit_payment', '입금 선납발생'
        DEBIT_CHARGE = 'debit_charge', '부과 자동충당'
        DEBIT_REFUND = 'debit_refund', '환불'

    prepayment = models.ForeignKey(Prepayment, related_name='movements', on_delete=models.PROTECT)
    movement_type = models.CharField(max_length=30, choices=MovementType.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    allocation_line = models.ForeignKey(PaymentAllocationLine, null=True, blank=True, on_delete=models.PROTECT)
    charge = models.ForeignKey(Charge, null=True, blank=True, on_delete=models.PROTECT)
    refund = models.ForeignKey(Refund, null=True, blank=True, on_delete=models.PROTECT)
    is_active = models.BooleanField(default=True, db_index=True)
    sequence = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['sequence', 'id']


class MessageTemplate(TimeStampedModel):
    class TemplateType(models.TextChoices):
        ARREARS = 'arrears', '일반 미수금'
        CLOSED_ARREARS = 'closed_arrears', '폐업 회원 미수금'
        REFUND_NOTICE = 'refund_notice', '선납금 환불 안내'
        REFUND_COMPLETE = 'refund_complete', '환불 완료'

    template_type = models.CharField(max_length=30, choices=TemplateType.choices, unique=True)
    subject = models.CharField(max_length=100, blank=True)
    body = models.TextField()
    is_active = models.BooleanField(default=True)


class MessageBatch(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', '작성중'
        SCHEDULED = 'scheduled', '예약'
        SENDING = 'sending', '발송중'
        ACCEPTED = 'accepted', '접수완료'
        SENT = 'sent', '전송성공'
        PARTIAL = 'partial', '일부실패'
        FAILED = 'failed', '실패'
        CANCELLED = 'cancelled', '취소'
        DRY_RUN = 'dry_run', '연동전 시험'

    message_type = models.CharField(max_length=30, choices=MessageTemplate.TemplateType.choices)
    subject = models.CharField(max_length=100, blank=True)
    template_body = models.TextField()
    due_date = models.DateField(null=True, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    provider_job_no = models.CharField(max_length=100, blank=True)
    provider_response = models.JSONField(default=dict, blank=True)
    created_by = models.CharField(max_length=50, default='admin')

    class Meta:
        ordering = ['-created_at']


class MessageRecipient(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = 'pending', '대기'
        ACCEPTED = 'accepted', '접수완료'
        SENT = 'sent', '전송성공'
        FAILED = 'failed', '실패'
        EXCLUDED = 'excluded', '제외'
        CANCELLED = 'cancelled', '취소'
        DRY_RUN = 'dry_run', '연동전 시험'

    batch = models.ForeignKey(MessageBatch, related_name='recipients', on_delete=models.PROTECT)
    member = models.ForeignKey(Member, related_name='message_recipients', on_delete=models.PROTECT)
    phone = models.CharField(max_length=30, blank=True)
    amount_snapshot = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    refund_date_snapshot = models.DateField(null=True, blank=True)
    body = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    exclusion_reason = models.CharField(max_length=255, blank=True)
    failure_reason = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    provider_recipient_id = models.CharField(max_length=100, blank=True)
    retry_of = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='retries')

    class Meta:
        ordering = ['id']


class LegalNotice(TimeStampedModel):
    class AddressType(models.TextChoices):
        BASIC = 'basic', '기본주소'
        OFFICIAL = 'official', '공문주소'
        BOTH = 'both', '두 주소 모두'

    class DeliveryStatus(models.TextChoices):
        SENT = 'sent', '발송'
        DELIVERED = 'delivered', '배달완료'
        RETURNED = 'returned', '반송'
        UNKNOWN_RECIPIENT = 'unknown_recipient', '수취인불명'
        UNKNOWN_ADDRESS = 'unknown_address', '주소불명'
        ABSENT = 'absent', '폐문부재'
        OTHER = 'other', '기타'

    member = models.ForeignKey(Member, related_name='legal_notices', on_delete=models.PROTECT)
    address_type = models.CharField(max_length=20, choices=AddressType.choices)
    address_snapshot = models.TextField()
    second_address_snapshot = models.TextField(blank=True)
    registered_no = models.CharField(max_length=100, blank=True, db_index=True)
    sent_date = models.DateField()
    delivery_status = models.CharField(max_length=30, choices=DeliveryStatus.choices, default=DeliveryStatus.SENT)
    result_date = models.DateField(null=True, blank=True)
    memo = models.TextField(blank=True)
    actor = models.CharField(max_length=50, default='admin')

    class Meta:
        ordering = ['-sent_date', '-id']


class AuditLog(models.Model):
    actor = models.CharField(max_length=50, default='admin')
    action = models.CharField(max_length=100)
    model_name = models.CharField(max_length=100)
    object_id = models.CharField(max_length=100)
    before_json = models.JSONField(default=dict, blank=True)
    after_json = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']


class SystemSetting(TimeStampedModel):
    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField(default=dict)
    description = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return self.key
