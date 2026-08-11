from __future__ import annotations

import uuid

from django import forms
from django.forms import formset_factory
from django.utils import timezone

from core.models import (
    AccountType, LegalNotice, Member, MemberLink, MessageBatch, MessageTemplate, MonthlyJob, Refund, UploadedFile, Vehicle,
)
from core.services.imports import HEADER_ALIASES, REQUIRED_FIELDS, validate_excel_signature
from core.utils import extract_purpose_char, normalize_vehicle_no


class DateInput(forms.DateInput):
    input_type = 'date'


class DateTimeInput(forms.DateTimeInput):
    input_type = 'datetime-local'


class MonthlyJobForm(forms.ModelForm):
    class Meta:
        model = MonthlyJob
        fields = ['year', 'month', 'version_name', 'memo']
        widgets = {'memo': forms.Textarea(attrs={'rows': 2})}


class UploadForm(forms.Form):
    slot_type = forms.ChoiceField(choices=UploadedFile.SlotType.choices, label='파일 유형')
    file = forms.FileField(label='Excel 파일')

    def clean_file(self):
        f = self.cleaned_data['file']
        validate_excel_signature(f)
        if f.size > 50 * 1024 * 1024:
            raise forms.ValidationError('파일 크기는 50MB 이하여야 합니다.')
        return f


def _clean_excel_file(file_obj):
    validate_excel_signature(file_obj)
    if file_obj.size > 50 * 1024 * 1024:
        raise forms.ValidationError('파일 크기는 50MB 이하여야 합니다.')
    return file_obj


class InitialDataImportForm(forms.Form):
    license_file = forms.FileField(
        label='전체면허자현황',
        help_text='현재 사용 중인 전체면허자현황 xls, xlsx, xlsm 파일',
        widget=forms.ClearableFileInput(attrs={'accept': '.xls,.xlsx,.xlsm'}),
    )
    receivables_file = forms.FileField(
        label='미수금 파일',
        help_text='현재 사용 중인 미수금 xls, xlsx, xlsm 파일',
        widget=forms.ClearableFileInput(attrs={'accept': '.xls,.xlsx,.xlsm'}),
    )

    def clean_license_file(self):
        return _clean_excel_file(self.cleaned_data['license_file'])

    def clean_receivables_file(self):
        return _clean_excel_file(self.cleaned_data['receivables_file'])


class SimpleExcelUploadForm(forms.Form):
    file = forms.FileField(
        label='엑셀 파일',
        widget=forms.ClearableFileInput(attrs={'accept': '.xls,.xlsx,.xlsm'}),
    )

    def clean_file(self):
        return _clean_excel_file(self.cleaned_data['file'])


class BankPasteForm(forms.Form):
    slot_type = forms.ChoiceField(
        choices=[
            (UploadedFile.SlotType.BANK_1, '농협 계좌 1'),
            (UploadedFile.SlotType.BANK_2, '농협 계좌 2'),
            (UploadedFile.SlotType.BANK_3, '농협 계좌 3'),
        ],
        widget=forms.HiddenInput(),
    )
    pasted_text = forms.CharField(
        label='농협 거래내역 붙여넣기',
        widget=forms.Textarea(attrs={
            'rows': 8,
            'class': 'paste-textarea',
            'placeholder': '농협 엑셀에서 표 전체를 복사한 뒤 이 칸을 클릭하고 Ctrl+V 하세요.',
            'autocomplete': 'off',
            'spellcheck': 'false',
        }),
    )


class ColumnMappingForm(forms.Form):
    def __init__(self, *args, uploaded: UploadedFile, **kwargs):
        super().__init__(*args, **kwargs)
        self.uploaded = uploaded
        headers = uploaded.detected_headers or []
        choices = [('', '선택 안 함')] + [(h, h) for h in headers]
        expected = HEADER_ALIASES.get(uploaded.slot_type, {})
        required = REQUIRED_FIELDS.get(uploaded.slot_type, set())
        current = uploaded.column_mapping or {}
        for field_name in expected:
            initial = current.get(field_name, '')
            if isinstance(initial, int) and 0 <= initial < len(headers):
                initial = headers[initial]
            self.fields[field_name] = forms.ChoiceField(
                label=field_name,
                choices=choices,
                required=field_name in required,
                initial=initial,
            )


class QuickMemberForm(forms.Form):
    management_no = forms.CharField(label='관리번호', max_length=100, required=False)
    name = forms.CharField(label='성명', max_length=100)
    region = forms.CharField(label='지역', max_length=100)
    vehicle_no = forms.CharField(label='차량번호', max_length=50)
    membership_status = forms.ChoiceField(
        label='구분',
        choices=[
            (Member.MembershipStatus.NON_MEMBER, '비회원 · 관리비'),
            (Member.MembershipStatus.ACTIVE, '협회가입 · 협회비'),
        ],
    )
    certificate_issued_on = forms.DateField(label='자격증명 발급일', required=False, widget=DateInput())
    membership_started_on = forms.DateField(label='협회 가입일', required=False, widget=DateInput())
    memo = forms.CharField(label='비고·다른 입금자명', required=False, widget=forms.Textarea(attrs={'rows': 2}))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('membership_status') == Member.MembershipStatus.ACTIVE and not cleaned.get('membership_started_on'):
            self.add_error('membership_started_on', '협회가입자는 실제 가입일을 입력하세요.')
        return cleaned

    def save(self):
        status = self.cleaned_data['membership_status']
        member = Member.objects.create(
            management_no=self.cleaned_data.get('management_no', '').strip(),
            name=self.cleaned_data['name'].strip(),
            region=self.cleaned_data['region'].strip(),
            membership_status=status,
            receivable_account_type=(
                AccountType.MEMBERSHIP_FEE if status == Member.MembershipStatus.ACTIVE
                else AccountType.MANAGEMENT_FEE
            ),
            membership_started_on=self.cleaned_data.get('membership_started_on'),
            certificate_issued_on=self.cleaned_data.get('certificate_issued_on'),
            certificate_date_recorded_on=timezone.localdate() if self.cleaned_data.get('certificate_issued_on') else None,
            first_seen_on=timezone.localdate(),
            memo=self.cleaned_data.get('memo', '').strip(),
        )
        vehicle_no = self.cleaned_data['vehicle_no'].strip()
        Vehicle.objects.create(
            member=member,
            vehicle_no=vehicle_no,
            normalized_vehicle_no=normalize_vehicle_no(vehicle_no),
            purpose_char=extract_purpose_char(vehicle_no),
            start_date=timezone.localdate(),
            is_current=True,
            change_reason='신규 명단 간편등록',
        )
        return member


class MemberForm(forms.ModelForm):
    vehicle_no = forms.CharField(label='현재 차량번호', required=False)

    class Meta:
        model = Member
        fields = [
            'management_no', 'name', 'birth6', 'phone', 'address', 'official_address',
            'memo', 'region', 'receivable_account_type',
            'membership_status', 'membership_started_on', 'membership_ended_on',
            'certificate_issued_on',
        ]
        widgets = {
            'membership_started_on': DateInput(),
            'membership_ended_on': DateInput(),
            'certificate_issued_on': DateInput(),
            'memo': forms.Textarea(attrs={'rows': 2}),
            'address': forms.TextInput(),
            'official_address': forms.TextInput(attrs={'placeholder': '기본 주소와 다를 때만 입력'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.current_vehicle:
            self.fields['vehicle_no'].initial = self.instance.current_vehicle.vehicle_no

    def save(self, commit=True):
        # 공문주소 수동관리 체크박스는 사용자에게 노출하지 않는다.
        # 공문주소가 비어 있거나 기본주소와 같으면 기본주소를 따라가고,
        # 실제로 다른 주소가 입력된 경우에만 내부 플래그를 자동 설정한다.
        address = (self.cleaned_data.get('address') or '').strip()
        official = (self.cleaned_data.get('official_address') or '').strip()
        self.instance.official_address_custom = bool(official and official != address)
        if not official:
            self.instance.official_address = address
        member = super().save(commit=commit)
        if not commit:
            return member
        vehicle_no = self.cleaned_data.get('vehicle_no', '').strip()
        current = member.current_vehicle
        normalized = normalize_vehicle_no(vehicle_no)
        if vehicle_no and (not current or current.normalized_vehicle_no != normalized):
            if current:
                current.is_current = False
                current.end_date = timezone.localdate()
                current.change_reason = '회원 상세 수동변경'
                current.save()
            Vehicle.objects.create(
                member=member, vehicle_no=vehicle_no, normalized_vehicle_no=normalized,
                purpose_char=extract_purpose_char(vehicle_no), start_date=timezone.localdate(),
                is_current=True, change_reason='회원 상세 수동변경',
            )
        return member


class ManualPaymentForm(forms.Form):
    payment_date = forms.DateField(label='입금일', widget=DateInput())
    amount = forms.DecimalField(label='입금액', max_digits=14, decimal_places=0, min_value=1)
    account_type = forms.ChoiceField(
        label='계정',
        choices=[
            (AccountType.MEMBERSHIP_FEE, '협회비'),
            (AccountType.MANAGEMENT_FEE, '관리비'),
            (AccountType.CERTIFICATE_FEE, '자격증명 발급비'),
            (AccountType.OTHER_INCOME, '기타수입'),
        ],
    )
    payer_name = forms.CharField(label='입금자명', required=False, max_length=100)
    memo = forms.CharField(label='메모', required=False, max_length=255)
    request_key = forms.CharField(widget=forms.HiddenInput(), required=False)

    def __init__(self, *args, member=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.member = member
        if not self.is_bound:
            self.fields['payment_date'].initial = timezone.localdate()
            self.fields['request_key'].initial = uuid.uuid4().hex
            if member and member.receivable_account_type:
                self.fields['account_type'].initial = member.receivable_account_type
            if member:
                self.fields['payer_name'].initial = member.name



class CloseMemberForm(forms.Form):
    closure_date = forms.DateField(label='폐업일', widget=DateInput())
    reason = forms.CharField(label='폐업사유', required=False, max_length=200)
    memo = forms.CharField(label='메모', required=False, widget=forms.Textarea(attrs={'rows': 3}))


class ReopenMemberForm(forms.Form):
    re_registered_on = forms.DateField(label='재등록일', widget=DateInput())
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))


class JoinAssociationForm(forms.Form):
    join_date = forms.DateField(label='실제 협회가입일', widget=DateInput())
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))


class LeaveAssociationForm(forms.Form):
    leave_date = forms.DateField(label='탈퇴일', widget=DateInput())
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))


class AllocationLineForm(forms.Form):
    # 3,000+ members must never be rendered as <option> rows.
    # The real value is kept in a hidden ModelChoiceField and selected through
    # the AJAX type-ahead picker on the allocation screen.
    member = forms.ModelChoiceField(
        queryset=Member.objects.none(), label='회원', widget=forms.HiddenInput(),
    )
    account_type = forms.ChoiceField(choices=AccountType.choices, label='계정')
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01, label='배정금액')
    memo = forms.CharField(required=False, label='메모')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Member.objects.filter(is_active_record=True)
        self.fields['member'].queryset = queryset
        self.member_display = ''

        raw_value = None
        if self.is_bound:
            raw_value = self.data.get(self.add_prefix('member'))
        if not raw_value:
            raw_value = self.initial.get('member')
        if isinstance(raw_value, Member):
            member = raw_value
        else:
            try:
                member = queryset.filter(pk=int(raw_value)).first() if raw_value else None
            except (TypeError, ValueError):
                member = None
        if member:
            vehicle = member.current_vehicle
            parts = [member.name]
            if vehicle and vehicle.vehicle_no:
                parts.append(vehicle.vehicle_no)
            if member.region:
                parts.append(member.region)
            if member.management_no:
                parts.append(f'관리 {member.management_no}')
            self.member_display = ' · '.join(parts)


AllocationFormSet = formset_factory(AllocationLineForm, extra=0, can_delete=True)


class RefundForm(forms.ModelForm):
    send_completion_sms = forms.BooleanField(label='환불 완료문자 발송', required=False, initial=True)

    class Meta:
        model = Refund
        fields = ['account_type', 'amount', 'bank', 'account_no', 'holder', 'refund_date', 'method', 'memo']
        widgets = {'refund_date': DateInput(), 'memo': forms.Textarea(attrs={'rows': 3})}


class RefundPendingForm(forms.ModelForm):
    class Meta:
        model = Refund
        fields = ['account_type', 'amount', 'bank', 'account_no', 'holder', 'memo']
        widgets = {'memo': forms.Textarea(attrs={'rows': 3})}


class LegalNoticeForm(forms.ModelForm):
    class Meta:
        model = LegalNotice
        fields = [
            'address_type', 'registered_no', 'sent_date', 'delivery_status',
            'result_date', 'memo',
        ]
        widgets = {'sent_date': DateInput(), 'result_date': DateInput(), 'memo': forms.Textarea(attrs={'rows': 3})}


class ArrearsComposeForm(forms.Form):
    due_date = forms.DateField(label='납부기한', widget=DateInput())
    scheduled_at = forms.DateTimeField(label='예약일시', required=False, widget=DateTimeInput())


class RefundMessageForm(forms.Form):
    message_type = forms.ChoiceField(
        choices=[
            (MessageTemplate.TemplateType.REFUND_NOTICE, '환불 안내'),
            (MessageTemplate.TemplateType.REFUND_COMPLETE, '환불 완료'),
        ]
    )
    scheduled_at = forms.DateTimeField(label='예약일시', required=False, widget=DateTimeInput())
    send_now = forms.BooleanField(label='즉시 발송', required=False)


class MessageScheduleEditForm(forms.ModelForm):
    class Meta:
        model = MessageBatch
        fields = ['due_date', 'scheduled_at']
        widgets = {'due_date': DateInput(), 'scheduled_at': DateTimeInput()}


class TransferMemberForm(forms.Form):
    received_date = forms.DateField(label='접수일자', widget=DateInput())
    approval_date = forms.DateField(label='인가일자', required=False, widget=DateInput())
    effective_date = forms.DateField(label='처리일자(양도일)', widget=DateInput())
    new_name = forms.CharField(label='양수자 성명', max_length=100)
    new_birth6 = forms.CharField(label='주민번호 앞 6자리', max_length=6, required=False)
    new_phone = forms.CharField(label='휴대전화번호', max_length=30, required=False)
    new_address = forms.CharField(label='주소', required=False, widget=forms.TextInput())
    new_vehicle_no = forms.CharField(label='차량번호', max_length=50, required=False)
    new_region = forms.CharField(label='지역', max_length=100, required=False)
    memo = forms.CharField(label='비고', required=False, widget=forms.Textarea(attrs={'rows': 2}))

