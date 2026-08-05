from __future__ import annotations

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


class MemberForm(forms.ModelForm):
    vehicle_no = forms.CharField(label='현재 차량번호', required=False)

    class Meta:
        model = Member
        fields = [
            'name', 'birth6', 'phone', 'address', 'official_address', 'official_address_custom',
            'memo', 'region', 'operational_status', 'membership_status',
            'membership_started_on', 'membership_ended_on', 'membership_billing_anchor',
            'management_billing_anchor', 'first_seen_on', 'certificate_issued_on',
            'certificate_date_recorded_on', 'address_needs_check', 'phone_needs_check', 'sms_opt_out',
            'collection_status',
        ]
        widgets = {
            'membership_started_on': DateInput(), 'membership_ended_on': DateInput(),
            'membership_billing_anchor': DateInput(), 'management_billing_anchor': DateInput(),
            'first_seen_on': DateInput(), 'certificate_issued_on': DateInput(),
            'certificate_date_recorded_on': DateInput(), 'memo': forms.Textarea(attrs={'rows': 3}),
            'address': forms.Textarea(attrs={'rows': 2}), 'official_address': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.current_vehicle:
            self.fields['vehicle_no'].initial = self.instance.current_vehicle.vehicle_no

    def save(self, commit=True):
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


class CloseMemberForm(forms.Form):
    closure_date = forms.DateField(label='폐업일', widget=DateInput())
    reason = forms.CharField(label='폐업사유', required=False, max_length=200)
    memo = forms.CharField(label='메모', required=False, widget=forms.Textarea(attrs={'rows': 3}))
    send_refund_notice = forms.BooleanField(label='선납금이 있으면 환불 안내문자 즉시 발송', required=False, initial=True)


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
    member = forms.ModelChoiceField(queryset=Member.objects.none(), label='회원')
    account_type = forms.ChoiceField(choices=AccountType.choices, label='계정')
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01, label='배정금액')
    memo = forms.CharField(required=False, label='메모')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['member'].queryset = Member.objects.filter(is_active_record=True).order_by('name', 'id')


AllocationFormSet = formset_factory(AllocationLineForm, extra=1, can_delete=True)


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
    send_now = forms.BooleanField(label='즉시 발송', required=False)


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
    transfer_type = forms.ChoiceField(
        label='양도양수 유형',
        choices=[
            (MemberLink.LinkType.FAMILY_SUCCESSION, '가족·직계 승계 — 기존 미수금 승계'),
            (MemberLink.LinkType.GENERAL_TRANSFER, '일반 양도양수 — 기존 미수금 미승계'),
        ],
    )
    effective_date = forms.DateField(label='명의 이전일', widget=DateInput())
    new_name = forms.CharField(label='새 명의자 성명', max_length=100)
    new_birth6 = forms.CharField(label='주민번호 앞 6자리', max_length=6, required=False)
    new_phone = forms.CharField(label='휴대전화번호', max_length=30, required=False)
    new_address = forms.CharField(label='기본 주소', required=False, widget=forms.Textarea(attrs={'rows': 2}))
    new_official_address = forms.CharField(label='공문 주소', required=False, widget=forms.Textarea(attrs={'rows': 2}))
    new_vehicle_no = forms.CharField(label='새 차량번호', max_length=50, required=False)
    new_region = forms.CharField(label='지역', max_length=100, required=False)
    memo = forms.CharField(label='메모', required=False, widget=forms.Textarea(attrs={'rows': 3}))
