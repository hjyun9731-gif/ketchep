from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import (
    Member, MessageBatch, MessageRecipient, MessageTemplate, Refund,
)
from core.services.audit import log_action


DEFAULT_TEMPLATES = {
    MessageTemplate.TemplateType.ARREARS: {
        'subject': '미수금 납부 안내',
        'body': (
            '현재 {name} 님의 미수금은 {amount}원으로 확인됩니다.\n\n'
            '{due_date}까지 납부하시거나 미납 사유 및 납부계획을 알려주시기 바랍니다. '
            '기한 내 납부 또는 별도 연락이 없을 경우 등록된 주소지로 내용증명을 발송할 예정입니다.\n\n'
            '일시 납부가 어려운 경우 분할납부 협의가 가능합니다. 이미 납부한 경우 확인을 위해 연락해 주시기 바랍니다.\n\n'
            '{association_name} {association_phone}'
        ),
    },
    MessageTemplate.TemplateType.CLOSED_ARREARS: {
        'subject': '폐업 회원 미수금 납부 안내',
        'body': (
            '{name} 님은 폐업 처리되었으나 폐업 전에 발생한 미수금 {amount}원은 납부 대상입니다.\n\n'
            '{due_date}까지 납부하시거나 미납 사유 및 납부계획을 알려주시기 바랍니다. '
            '기한 내 납부 또는 별도 연락이 없을 경우 등록된 주소지로 내용증명을 발송할 예정입니다.\n\n'
            '일시 납부가 어려운 경우 분할납부 협의가 가능합니다.\n\n'
            '{association_name} {association_phone}'
        ),
    },
    MessageTemplate.TemplateType.REFUND_NOTICE: {
        'subject': '선납금 환불 안내',
        'body': (
            '{name} 님, 폐업 처리에 따라 선납금 {amount}원이 확인되었습니다. '
            '환불받을 은행명, 계좌번호, 예금주를 협회로 알려주시기 바랍니다.\n\n'
            '{association_name} {association_phone}'
        ),
    },
    MessageTemplate.TemplateType.REFUND_COMPLETE: {
        'subject': '선납금 환불 완료 안내',
        'body': (
            '{name} 님의 선납금 {amount}원을 {refund_date}에 환불 처리했습니다. '
            '확인이 필요한 경우 협회로 연락해 주시기 바랍니다.\n\n'
            '{association_name} {association_phone}'
        ),
    },
}


def ensure_default_templates():
    result = {}
    for template_type, values in DEFAULT_TEMPLATES.items():
        obj, _ = MessageTemplate.objects.get_or_create(template_type=template_type, defaults=values)
        result[template_type] = obj
    return result


def money_text(value: Decimal | None) -> str:
    return f'{int(value or 0):,}'


def _format_date(value: date | None) -> str:
    return value.strftime('%Y년 %m월 %d일') if value else ''


def render_template(template: MessageTemplate, *, member: Member, amount=None, due_date=None, refund_date=None):
    return template.body.format(
        name=member.name,
        amount=money_text(amount),
        due_date=_format_date(due_date),
        refund_date=_format_date(refund_date),
        association_name=settings.ASSOCIATION_NAME,
        association_phone=settings.ASSOCIATION_PHONE,
    )


def recipient_exclusion_reason(member: Member):
    if not member.phone:
        return '연락처 없음'
    if member.phone_needs_check:
        return '결번/연락처 확인 필요'
    if member.sms_opt_out:
        return '수신거부'
    return ''


def has_successful_arrears_today(member: Member):
    today = timezone.localdate()
    return MessageRecipient.objects.filter(
        member=member,
        batch__message_type=MessageTemplate.TemplateType.ARREARS,
        status=MessageRecipient.Status.SENT,
        sent_at__date=today,
    ).exists()


@transaction.atomic
def create_arrears_batch(member_ids, *, due_date: date, scheduled_at=None, immediate=False, actor='admin'):
    templates = ensure_default_templates()
    base = templates[MessageTemplate.TemplateType.ARREARS]
    batch = MessageBatch.objects.create(
        message_type=MessageTemplate.TemplateType.ARREARS,
        subject=base.subject,
        template_body=base.body,
        due_date=due_date,
        scheduled_at=scheduled_at,
        status=MessageBatch.Status.DRAFT if immediate else MessageBatch.Status.SCHEDULED,
        created_by=actor,
    )
    for member in Member.objects.filter(id__in=member_ids).order_by('name'):
        amount = member.total_outstanding
        exclusion = recipient_exclusion_reason(member)
        if amount <= 0:
            exclusion = '미수금 없음'
        elif has_successful_arrears_today(member):
            exclusion = '당일 성공 발송 이력 있음'
        template = templates[
            MessageTemplate.TemplateType.CLOSED_ARREARS
            if member.operational_status == Member.OperationalStatus.CLOSED
            else MessageTemplate.TemplateType.ARREARS
        ]
        body = render_template(template, member=member, amount=amount, due_date=due_date)
        MessageRecipient.objects.create(
            batch=batch, member=member, phone=member.phone,
            amount_snapshot=amount, body=body,
            status=MessageRecipient.Status.EXCLUDED if exclusion else MessageRecipient.Status.PENDING,
            exclusion_reason=exclusion,
        )
    log_action(action='message_batch_created', instance=batch, actor=actor)
    return batch


@transaction.atomic
def create_refund_batch(refund_ids, *, message_type: str, scheduled_at=None, immediate=False, actor='admin'):
    if message_type not in {MessageTemplate.TemplateType.REFUND_NOTICE, MessageTemplate.TemplateType.REFUND_COMPLETE}:
        raise ValueError('환불 문자유형이 아닙니다.')
    templates = ensure_default_templates()
    template = templates[message_type]
    batch = MessageBatch.objects.create(
        message_type=message_type,
        subject=template.subject,
        template_body=template.body,
        scheduled_at=scheduled_at,
        status=MessageBatch.Status.DRAFT if immediate else MessageBatch.Status.SCHEDULED,
        created_by=actor,
    )
    for refund in Refund.objects.select_related('member').filter(id__in=refund_ids):
        member = refund.member
        exclusion = recipient_exclusion_reason(member)
        if message_type == MessageTemplate.TemplateType.REFUND_NOTICE and refund.status != Refund.Status.PENDING:
            exclusion = '환불대기 상태 아님'
        if message_type == MessageTemplate.TemplateType.REFUND_COMPLETE and refund.status != Refund.Status.COMPLETED:
            exclusion = '환불완료 상태 아님'
        body = render_template(
            template, member=member, amount=refund.amount,
            refund_date=refund.refund_date,
        )
        MessageRecipient.objects.create(
            batch=batch, member=member, phone=member.phone,
            amount_snapshot=refund.amount, refund_date_snapshot=refund.refund_date,
            body=body,
            status=MessageRecipient.Status.EXCLUDED if exclusion else MessageRecipient.Status.PENDING,
            exclusion_reason=exclusion,
        )
    log_action(action='refund_message_batch_created', instance=batch, actor=actor)
    return batch


def _latest_refund(member: Member, completed: bool):
    qs = member.refunds.filter(status=Refund.Status.COMPLETED if completed else Refund.Status.PENDING)
    return qs.order_by('-refund_date', '-created_at').first()


@transaction.atomic
def refresh_batch_recipients(batch: MessageBatch):
    templates = ensure_default_templates()
    for recipient in batch.recipients.select_related('member'):
        if recipient.status in {MessageRecipient.Status.SENT, MessageRecipient.Status.CANCELLED}:
            continue
        if recipient.status == MessageRecipient.Status.EXCLUDED and recipient.exclusion_reason == '이번 발송 제외':
            continue
        member = recipient.member
        exclusion = recipient_exclusion_reason(member)
        template_type = batch.message_type
        amount = recipient.amount_snapshot
        refund_date = recipient.refund_date_snapshot

        if batch.message_type == MessageTemplate.TemplateType.ARREARS:
            amount = member.total_outstanding
            if amount <= 0:
                exclusion = '발송 전 전액 납부 완료'
            elif has_successful_arrears_today(member):
                exclusion = '당일 성공 발송 이력 있음'
            template_type = (
                MessageTemplate.TemplateType.CLOSED_ARREARS
                if member.operational_status == Member.OperationalStatus.CLOSED
                else MessageTemplate.TemplateType.ARREARS
            )
        elif batch.message_type == MessageTemplate.TemplateType.REFUND_NOTICE:
            refund = _latest_refund(member, completed=False)
            if not refund:
                exclusion = '발송 전 환불대기 건 없음'
            else:
                amount = refund.amount
        elif batch.message_type == MessageTemplate.TemplateType.REFUND_COMPLETE:
            refund = _latest_refund(member, completed=True)
            if not refund:
                exclusion = '발송 전 환불완료 건 없음'
            else:
                amount = refund.amount
                refund_date = refund.refund_date

        template = templates[template_type]
        recipient.phone = member.phone
        recipient.amount_snapshot = amount
        recipient.refund_date_snapshot = refund_date
        recipient.body = render_template(
            template, member=member, amount=amount,
            due_date=batch.due_date, refund_date=refund_date,
        )
        if exclusion:
            recipient.status = MessageRecipient.Status.EXCLUDED
            recipient.exclusion_reason = exclusion
        else:
            recipient.status = MessageRecipient.Status.PENDING
            recipient.exclusion_reason = ''
        recipient.save(update_fields=[
            'phone', 'amount_snapshot', 'refund_date_snapshot', 'body',
            'status', 'exclusion_reason', 'updated_at',
        ])


def _euc_kr_safe(text: str):
    try:
        text.encode('euc-kr')
        return True, ''
    except UnicodeEncodeError as exc:
        return False, f'EUC-KR에서 지원하지 않는 문자: {exc.object[exc.start:exc.end]}'


class BalsongClient:
    def __init__(self):
        self.url = settings.BALSEONG_API_URL
        self.user_id = settings.BALSEONG_USER_ID
        self.user_pw = settings.BALSEONG_USER_PW
        self.callback = settings.BALSEONG_CALLBACK
        self.dry_run = settings.BALSEONG_DRY_RUN

    def send(self, *, subject: str, recipients: list[MessageRecipient]):
        if self.dry_run:
            return {'dry_run': True, 'Job_No': 'DRY-RUN', 'count': len(recipients)}
        destination = []
        for r in recipients:
            destination.append({
                'Company': settings.ASSOCIATION_NAME,
                'Name': r.member.name,
                'Phone': r.phone,
                'Msg_Text': r.body,
            })
        payload = {
            'UserID': self.user_id,
            'UserPW': self.user_pw,
            'Service': 'LMS',
            'Type': 'Send',
            'Callback': self.callback,
            'Subject': subject,
            'Main_Text': recipients[0].body if recipients else '',
            'Destination': json.dumps(destination, ensure_ascii=False),
        }
        response = requests.post(self.url, data=payload, timeout=30)
        response.raise_for_status()
        try:
            parsed = response.json()
        except ValueError:
            parsed = {'raw': response.text}
        return parsed


@transaction.atomic
def send_batch(batch: MessageBatch, *, actor='admin'):
    batch = MessageBatch.objects.select_for_update().get(pk=batch.pk)
    if batch.status == MessageBatch.Status.CANCELLED:
        raise ValueError('취소된 예약입니다.')
    refresh_batch_recipients(batch)
    pending = list(batch.recipients.filter(status=MessageRecipient.Status.PENDING).select_related('member'))
    if not pending:
        batch.status = MessageBatch.Status.SENT
        batch.sent_at = timezone.now()
        batch.provider_response = {'message': '발송 가능한 대상자가 없습니다.'}
        batch.save(update_fields=['status', 'sent_at', 'provider_response', 'updated_at'])
        return batch

    invalid = []
    for recipient in pending:
        ok, reason = _euc_kr_safe(recipient.body)
        if not ok:
            recipient.status = MessageRecipient.Status.FAILED
            recipient.failure_reason = reason
            recipient.save(update_fields=['status', 'failure_reason', 'updated_at'])
            invalid.append(recipient.id)
    pending = [r for r in pending if r.id not in invalid]
    if not pending:
        batch.status = MessageBatch.Status.FAILED
        batch.sent_at = timezone.now()
        batch.save(update_fields=['status', 'sent_at', 'updated_at'])
        return batch

    batch.status = MessageBatch.Status.SENDING
    batch.save(update_fields=['status', 'updated_at'])
    client = BalsongClient()
    try:
        result = client.send(subject=batch.subject, recipients=pending)
        now = timezone.now()
        dry_run = bool(result.get('dry_run'))
        recipient_status = MessageRecipient.Status.DRY_RUN if dry_run else MessageRecipient.Status.SENT
        for recipient in pending:
            recipient.status = recipient_status
            recipient.sent_at = now
            recipient.failure_reason = ''
            recipient.save(update_fields=['status', 'sent_at', 'failure_reason', 'updated_at'])
        batch.status = MessageBatch.Status.DRY_RUN if dry_run else MessageBatch.Status.SENT
        batch.sent_at = now
        batch.provider_job_no = str(result.get('Job_No') or result.get('job_no') or '')
        batch.provider_response = result
        batch.save(update_fields=[
            'status', 'sent_at', 'provider_job_no', 'provider_response', 'updated_at',
        ])
    except Exception as exc:
        now = timezone.now()
        for recipient in pending:
            recipient.status = MessageRecipient.Status.FAILED
            recipient.failure_reason = str(exc)
            recipient.sent_at = now
            recipient.save(update_fields=['status', 'failure_reason', 'sent_at', 'updated_at'])
        batch.status = MessageBatch.Status.FAILED
        batch.sent_at = now
        batch.provider_response = {'error': type(exc).__name__, 'message': str(exc)}
        batch.save(update_fields=['status', 'sent_at', 'provider_response', 'updated_at'])
    log_action(action='message_batch_sent', instance=batch, actor=actor, after=batch.provider_response)
    return batch


@transaction.atomic
def retry_failed_batch(batch: MessageBatch, *, actor='admin'):
    failed = list(batch.recipients.filter(status=MessageRecipient.Status.FAILED).select_related('member'))
    retry = MessageBatch.objects.create(
        message_type=batch.message_type,
        subject=batch.subject,
        template_body=batch.template_body,
        due_date=batch.due_date,
        status=MessageBatch.Status.DRAFT,
        created_by=actor,
    )
    for old in failed:
        MessageRecipient.objects.create(
            batch=retry, member=old.member, phone=old.member.phone,
            amount_snapshot=old.amount_snapshot,
            refund_date_snapshot=old.refund_date_snapshot,
            body=old.body,
            status=MessageRecipient.Status.PENDING,
            retry_of=old,
        )
    return send_batch(retry, actor=actor)


def send_due_batches():
    now = timezone.now()
    results = []
    for batch in MessageBatch.objects.filter(
        status=MessageBatch.Status.SCHEDULED,
        scheduled_at__lte=now,
    ).order_by('scheduled_at'):
        results.append(send_batch(batch))
    return results
