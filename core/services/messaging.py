from __future__ import annotations

import json
from datetime import date, datetime
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
        status__in=[
            MessageRecipient.Status.ACCEPTED,
            MessageRecipient.Status.SENT,
        ],
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
        bad = exc.object[exc.start:exc.end]
        return False, f'EUC-KR에서 지원하지 않는 문자: {bad}'


def _euc_kr_bytes(text: str) -> int:
    return len((text or '').encode('euc-kr'))


def _clip_euc_kr(text: str, max_bytes: int) -> str:
    result = []
    used = 0
    for char in text or '':
        encoded = char.encode('euc-kr')
        if used + len(encoded) > max_bytes:
            break
        result.append(char)
        used += len(encoded)
    return ''.join(result)


def _digits(value: str) -> str:
    return ''.join(char for char in str(value or '') if char.isdigit())


def _service_for_recipients(recipients: list[MessageRecipient]) -> str:
    max_bytes = max((_euc_kr_bytes(recipient.body) for recipient in recipients), default=0)
    if max_bytes > 2000:
        raise ValueError(f'LMS 최대 2,000Bytes를 초과한 문자가 있습니다. 현재 최대 {max_bytes}Bytes입니다.')
    return 'SMS' if max_bytes <= 90 else 'LMS'


def _provider_service(batch: MessageBatch) -> str:
    response = batch.provider_response or {}
    service = str(response.get('Service') or response.get('service') or '').upper()
    return service if service in {'SMS', 'LMS', 'MMS'} else 'SMS'


class BalsongAPIError(RuntimeError):
    pass


class BalsongClient:
    """발송닷컴 서버 간 POST API 클라이언트."""

    def __init__(self):
        self.url = settings.BALSONG_API_URL
        self.user_id = settings.BALSONG_USER_ID
        self.user_pw = settings.BALSONG_USER_PW
        self.callback = settings.BALSONG_CALLBACK
        self.dry_run = settings.BALSONG_DRY_RUN

    def _credentials_payload(self) -> dict:
        if not self.user_id or not self.user_pw:
            raise BalsongAPIError('발송닷컴 아이디와 비밀번호가 Railway 환경변수에 등록되지 않았습니다.')
        return {'UserID': self.user_id, 'UserPW': self.user_pw}

    def _post(self, payload: dict, *, timeout: int = 30) -> dict:
        response = requests.post(
            self.url,
            data={**self._credentials_payload(), **payload},
            timeout=timeout,
        )
        response.raise_for_status()
        try:
            parsed = response.json()
        except ValueError as exc:
            raise BalsongAPIError(
                f'발송닷컴 응답이 JSON 형식이 아닙니다: {response.text[:300]}'
            ) from exc

        result = str(parsed.get('Result') or '').upper()
        code = str(parsed.get('Code') if parsed.get('Code') is not None else '')
        if result != 'OK' or code not in {'0', '0.0'}:
            message = parsed.get('Message') or '발송닷컴 요청이 실패했습니다.'
            raise BalsongAPIError(f'{message} (코드 {code or "없음"})')
        return parsed

    def callback_list(self) -> dict:
        # 조회 API는 과금·발송이 없으므로 시험모드에서도 실제 연결을 확인한다.
        return self._post({'Service': 'CALLBACK', 'Type': 'List'})

    def send(self, *, subject: str, recipients: list[MessageRecipient], send_date=None) -> dict:
        if not recipients:
            raise ValueError('발송 대상자가 없습니다.')

        for recipient in recipients:
            ok, reason = _euc_kr_safe(recipient.body)
            if not ok:
                raise ValueError(f'{recipient.member.name}: {reason}')

        service = _service_for_recipients(recipients)
        destination = [
            {
                'Company': settings.ASSOCIATION_NAME,
                'Name': recipient.member.name,
                'Phone': _digits(recipient.phone),
                'Msg_Text': recipient.body,
            }
            for recipient in recipients
        ]

        if self.dry_run:
            return {
                'dry_run': True, 'Result': 'OK', 'Code': 0,
                'Cash': 0, 'Service': service, 'Job_No': 'DRY-RUN',
                'count': len(recipients), 'scheduled': bool(send_date),
            }

        if not self.callback:
            raise BalsongAPIError('발송닷컴에 등록된 발신번호가 Railway 환경변수에 없습니다.')

        payload = {
            'Service': service,
            'Type': 'Send',
            'Callback': _digits(self.callback),
            'Subject': _clip_euc_kr(subject or '', 64),
            'Main_Text': recipients[0].body,
            'Destination': json.dumps(destination, ensure_ascii=False),
        }
        if send_date:
            payload['Send_Date'] = timezone.localtime(send_date).strftime('%Y-%m-%d %H:%M')

        parsed = self._post(payload)
        parsed.setdefault('Service', service)
        parsed['count'] = len(recipients)
        return parsed

    def report_detail(self, *, job_no: str) -> dict:
        first = self._post({
            'Service': 'SMS',
            'Type': 'Report_Detail',
            'Job_No': job_no,
            'List_EA': 100,
            'Page': 1,
        })
        items = list(first.get('List') or [])
        total_pages = int(first.get('Total_Page') or 1)
        for page in range(2, total_pages + 1):
            result = self._post({
                'Service': 'SMS',
                'Type': 'Report_Detail',
                'Job_No': job_no,
                'List_EA': 100,
                'Page': page,
            })
            items.extend(result.get('List') or [])
        first['List'] = items
        return first

    def cancel(self, *, service: str, job_no: str) -> dict:
        if self.dry_run:
            return {
                'dry_run': True, 'Result': 'OK', 'Code': 0,
                'Service': service, 'Job_No': job_no,
            }
        return self._post({
            'Service': service,
            'Type': 'Cancel',
            'Job_No': job_no,
        })

    def reserve_edit(self, *, service: str, job_no: str, subject: str, send_date) -> dict:
        if self.dry_run:
            return {
                'dry_run': True, 'Result': 'OK', 'Code': 0,
                'Service': service, 'Job_No': job_no,
            }
        return self._post({
            'Service': service,
            'Type': 'Reserve_Edit',
            'Job_No': job_no,
            'Subject': _clip_euc_kr(subject or '', 64),
            'Send_Date': timezone.localtime(send_date).strftime('%Y-%m-%d %H:%M'),
        })


def _provider_item_status(item: dict) -> tuple[str, str]:
    status = str(item.get('Status') or '').strip()
    detail = str(item.get('Status_Detail') or '').strip()
    combined = f'{status} {detail}'.strip()
    if status == '성공' or detail == '성공':
        return MessageRecipient.Status.SENT, ''
    if item.get('Done_Date') or any(
        marker in combined
        for marker in ('실패', '결번', '오류', '차단', '초과', '없음', '거부')
    ):
        return MessageRecipient.Status.FAILED, detail or status or '발송 실패'
    return MessageRecipient.Status.ACCEPTED, ''


def _recalculate_batch_status(batch: MessageBatch) -> None:
    statuses = list(batch.recipients.values_list('status', flat=True))
    deliverable = [
        status for status in statuses
        if status not in {MessageRecipient.Status.EXCLUDED, MessageRecipient.Status.CANCELLED}
    ]
    if not deliverable:
        batch.status = MessageBatch.Status.SENT
    elif any(
        status in {MessageRecipient.Status.PENDING, MessageRecipient.Status.ACCEPTED}
        for status in deliverable
    ):
        batch.status = (
            MessageBatch.Status.SCHEDULED
            if batch.scheduled_at and batch.scheduled_at > timezone.now()
            else MessageBatch.Status.ACCEPTED
        )
    elif all(status == MessageRecipient.Status.SENT for status in deliverable):
        batch.status = MessageBatch.Status.SENT
    elif all(status == MessageRecipient.Status.FAILED for status in deliverable):
        batch.status = MessageBatch.Status.FAILED
    else:
        batch.status = MessageBatch.Status.PARTIAL


@transaction.atomic
def send_batch(batch: MessageBatch, *, actor='admin'):
    batch = MessageBatch.objects.select_for_update().get(pk=batch.pk)
    if batch.status == MessageBatch.Status.CANCELLED:
        raise ValueError('취소된 예약입니다.')
    if batch.provider_job_no or batch.status in {
        MessageBatch.Status.ACCEPTED,
        MessageBatch.Status.SENT,
        MessageBatch.Status.PARTIAL,
        MessageBatch.Status.DRY_RUN,
    }:
        return batch

    refresh_batch_recipients(batch)
    pending = list(
        batch.recipients.filter(status=MessageRecipient.Status.PENDING)
        .select_related('member')
    )
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
            continue
        if _euc_kr_bytes(recipient.body) > 2000:
            recipient.status = MessageRecipient.Status.FAILED
            recipient.failure_reason = 'LMS 최대 2,000Bytes 초과'
            recipient.save(update_fields=['status', 'failure_reason', 'updated_at'])
            invalid.append(recipient.id)

    pending = [recipient for recipient in pending if recipient.id not in invalid]
    if not pending:
        batch.status = MessageBatch.Status.FAILED
        batch.sent_at = timezone.now()
        batch.save(update_fields=['status', 'sent_at', 'updated_at'])
        return batch

    batch.status = MessageBatch.Status.SENDING
    batch.save(update_fields=['status', 'updated_at'])
    client = BalsongClient()
    try:
        future_schedule = (
            batch.scheduled_at
            if batch.scheduled_at and batch.scheduled_at > timezone.now()
            else None
        )
        result = client.send(
            subject=batch.subject,
            recipients=pending,
            send_date=future_schedule,
        )
        now = timezone.now()
        dry_run = bool(result.get('dry_run'))
        recipient_status = (
            MessageRecipient.Status.DRY_RUN
            if dry_run else MessageRecipient.Status.ACCEPTED
        )
        for recipient in pending:
            recipient.status = recipient_status
            recipient.sent_at = now
            recipient.failure_reason = ''
            recipient.save(update_fields=[
                'status', 'sent_at', 'failure_reason', 'updated_at',
            ])

        if dry_run:
            batch.status = MessageBatch.Status.DRY_RUN
        elif future_schedule:
            batch.status = MessageBatch.Status.SCHEDULED
        else:
            batch.status = MessageBatch.Status.ACCEPTED
        batch.sent_at = now
        batch.provider_job_no = str(result.get('Job_No') or result.get('job_no') or '')
        batch.provider_response = result
        batch.save(update_fields=[
            'status', 'sent_at', 'provider_job_no',
            'provider_response', 'updated_at',
        ])
    except Exception as exc:
        now = timezone.now()
        for recipient in pending:
            recipient.status = MessageRecipient.Status.FAILED
            recipient.failure_reason = str(exc)
            recipient.sent_at = now
            recipient.save(update_fields=[
                'status', 'failure_reason', 'sent_at', 'updated_at',
            ])
        batch.status = MessageBatch.Status.FAILED
        batch.sent_at = now
        batch.provider_response = {
            'error': type(exc).__name__,
            'message': str(exc),
        }
        batch.save(update_fields=[
            'status', 'sent_at', 'provider_response', 'updated_at',
        ])
    log_action(
        action='message_batch_submitted',
        instance=batch,
        actor=actor,
        after=batch.provider_response,
    )
    return batch


@transaction.atomic
def sync_batch_results(batch: MessageBatch, *, actor='admin'):
    batch = MessageBatch.objects.select_for_update().get(pk=batch.pk)
    if not batch.provider_job_no or batch.provider_job_no == 'DRY-RUN':
        raise ValueError('발송닷컴 접수번호가 없어 결과를 확인할 수 없습니다.')

    result = BalsongClient().report_detail(job_no=batch.provider_job_no)
    provider_items = list(result.get('List') or [])
    recipients = list(
        batch.recipients.exclude(
            status__in=[
                MessageRecipient.Status.EXCLUDED,
                MessageRecipient.Status.CANCELLED,
            ]
        ).select_related('member').order_by('id')
    )
    unmatched = recipients[:]

    def pop_match(item):
        phone = _digits(item.get('Phone'))
        name = str(item.get('Name') or '').strip()
        for index, recipient in enumerate(unmatched):
            if _digits(recipient.phone) == phone and (
                not name or recipient.member.name.strip() == name
            ):
                return unmatched.pop(index)
        for index, recipient in enumerate(unmatched):
            if _digits(recipient.phone) == phone:
                return unmatched.pop(index)
        return None

    for item in provider_items:
        recipient = pop_match(item)
        if not recipient:
            continue
        recipient.status, recipient.failure_reason = _provider_item_status(item)
        done_date = item.get('Done_Date')
        if done_date:
            parsed_done = None
            for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
                try:
                    parsed_done = datetime.strptime(str(done_date), pattern)
                    break
                except (TypeError, ValueError):
                    continue
            recipient.sent_at = (
                timezone.make_aware(parsed_done)
                if parsed_done is not None
                else timezone.now()
            )
        recipient.provider_recipient_id = str(item.get('No') or '')
        recipient.save(update_fields=[
            'status', 'failure_reason', 'sent_at',
            'provider_recipient_id', 'updated_at',
        ])

    batch.provider_response = {
        **(batch.provider_response or {}),
        'last_report': result,
        'last_synced_at': timezone.now().isoformat(),
    }
    _recalculate_batch_status(batch)
    batch.save(update_fields=['status', 'provider_response', 'updated_at'])
    log_action(
        action='message_batch_result_synced',
        instance=batch,
        actor=actor,
        after={
            'job_no': batch.provider_job_no,
            'status': batch.status,
            'result_count': len(provider_items),
        },
    )
    return batch


@transaction.atomic
def cancel_batch(batch: MessageBatch, *, actor='admin'):
    batch = MessageBatch.objects.select_for_update().get(pk=batch.pk)
    if batch.status == MessageBatch.Status.CANCELLED:
        return batch
    if batch.provider_job_no and batch.provider_job_no != 'DRY-RUN':
        if not batch.scheduled_at or batch.scheduled_at <= timezone.now():
            raise ValueError('즉시 발송 또는 이미 발송시각이 지난 건은 예약취소할 수 없습니다.')
        result = BalsongClient().cancel(
            service=_provider_service(batch),
            job_no=batch.provider_job_no,
        )
        batch.provider_response = {
            **(batch.provider_response or {}),
            'cancel_response': result,
        }
    batch.status = MessageBatch.Status.CANCELLED
    batch.recipients.filter(
        status__in=[
            MessageRecipient.Status.PENDING,
            MessageRecipient.Status.ACCEPTED,
        ]
    ).update(
        status=MessageRecipient.Status.CANCELLED,
        updated_at=timezone.now(),
    )
    batch.save(update_fields=['status', 'provider_response', 'updated_at'])
    log_action(action='message_batch_cancelled', instance=batch, actor=actor)
    return batch


@transaction.atomic
def update_batch_schedule(
    batch: MessageBatch, *, scheduled_at, due_date=None, actor='admin'
):
    batch = MessageBatch.objects.select_for_update().get(pk=batch.pk)
    if not scheduled_at or scheduled_at <= timezone.now():
        raise ValueError('예약일시는 현재보다 뒤여야 합니다.')

    if batch.provider_job_no and batch.provider_job_no != 'DRY-RUN':
        result = BalsongClient().reserve_edit(
            service=_provider_service(batch),
            job_no=batch.provider_job_no,
            subject=batch.subject,
            send_date=scheduled_at,
        )
        batch.provider_response = {
            **(batch.provider_response or {}),
            'reserve_edit_response': result,
        }

    batch.scheduled_at = scheduled_at
    if due_date is not None:
        batch.due_date = due_date
    batch.status = MessageBatch.Status.SCHEDULED
    batch.save(update_fields=[
        'scheduled_at', 'due_date', 'status',
        'provider_response', 'updated_at',
    ])
    log_action(action='message_schedule_updated', instance=batch, actor=actor)
    return batch


@transaction.atomic
def retry_failed_batch(batch: MessageBatch, *, actor='admin'):
    failed = list(
        batch.recipients.filter(status=MessageRecipient.Status.FAILED)
        .select_related('member')
    )
    if not failed:
        raise ValueError('재발송할 실패 건이 없습니다.')

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
            batch=retry,
            member=old.member,
            phone=old.member.phone,
            amount_snapshot=old.amount_snapshot,
            refund_date_snapshot=old.refund_date_snapshot,
            body=old.body,
            status=MessageRecipient.Status.PENDING,
            retry_of=old,
        )
    return send_batch(retry, actor=actor)


def send_due_batches():
    """호환용 명령.

    v3.0.2부터 예약은 사용자가 최종확인할 때 발송닷컴 Send_Date로 즉시
    접수한다. 최종확인하지 않은 예약 초안이 자동 발송되지 않도록 로컬
    스케줄러에서는 아무 것도 보내지 않는다.
    """
    return []
