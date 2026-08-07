from __future__ import annotations

from django.db import transaction

from core.models import AccountType, Charge, Member, MonthlyJob, PaymentAllocationLine
from core.services.audit import log_action
from core.services.ledger import RECURRING_ACCOUNTS, rebuild_member_account


@transaction.atomic
def create_job_version(*, year: int, month: int, version_name: str, based_on: MonthlyJob | None = None, actor='admin') -> MonthlyJob:
    latest = MonthlyJob.objects.filter(year=year, month=month).order_by('-version').first()
    version = (latest.version + 1) if latest else 1
    if based_on is None and latest:
        based_on = latest
    MonthlyJob.objects.filter(year=year, month=month, is_current=True).update(is_current=False)
    job = MonthlyJob.objects.create(
        year=year, month=month, version=version, version_name=version_name,
        based_on=based_on, is_current=True,
    )
    if based_on:
        for charge in based_on.charges.all():
            Charge.objects.create(
                member=charge.member,
                account_type=charge.account_type,
                charge_date=charge.charge_date,
                amount=charge.amount,
                status=charge.status,
                source_rule=charge.source_rule,
                monthly_job=job,
                cancellation_reason=charge.cancellation_reason,
                cancelled_at=charge.cancelled_at,
            )
        impacted = set(based_on.charges.values_list('member_id', 'account_type'))
        impacted.update(job.charges.values_list('member_id', 'account_type'))
        for member_id, account_type in impacted:
            if account_type in RECURRING_ACCOUNTS:
                rebuild_member_account(Member.objects.get(pk=member_id), account_type)
    log_action(action='monthly_job_version_created', instance=job, actor=actor)
    return job


@transaction.atomic
def set_current_job(job: MonthlyJob, *, actor='admin'):
    old = MonthlyJob.objects.filter(year=job.year, month=job.month, is_current=True).exclude(pk=job.pk).first()
    MonthlyJob.objects.filter(year=job.year, month=job.month, is_current=True).exclude(pk=job.pk).update(is_current=False)
    if not job.is_current:
        job.is_current = True
        job.save(update_fields=['is_current', 'updated_at'])
    impacted = set()
    if old:
        impacted.update(old.charges.values_list('member_id', 'account_type'))
        impacted.update(old.payments.filter(allocation_lines__status=PaymentAllocationLine.Status.ACTIVE).values_list(
            'allocation_lines__member_id', 'allocation_lines__account_type'
        ))
    impacted.update(job.charges.values_list('member_id', 'account_type'))
    impacted.update(job.payments.filter(allocation_lines__status=PaymentAllocationLine.Status.ACTIVE).values_list(
        'allocation_lines__member_id', 'allocation_lines__account_type'
    ))
    for member_id, account_type in impacted:
        if member_id and account_type in RECURRING_ACCOUNTS:
            rebuild_member_account(Member.objects.get(pk=member_id), account_type)
    log_action(
        action='monthly_job_set_current', instance=job,
        before={'previous_current_id': old.id if old else None},
        after={'current_id': job.id}, actor=actor,
    )
    return job


def clone_latest_uploaded_files(source_job: MonthlyJob, target_job: MonthlyJob, *, exclude_slot: str | None = None):
    """Copy the latest file metadata and parsed rows into a new job version.

    The stored file is referenced, not duplicated. Processed files are reset to
    PARSED so the target version receives its own transactions and matching state.
    """
    from core.models import ParsedRow, UploadedFile

    latest = {}
    for uploaded in source_job.uploaded_files.order_by('slot_type', '-created_at'):
        latest.setdefault(uploaded.slot_type, uploaded)
    cloned = []
    for slot, source in latest.items():
        if slot == exclude_slot:
            continue
        status = source.parse_status
        if status == UploadedFile.ParseStatus.PROCESSED:
            status = UploadedFile.ParseStatus.PARSED
        target = UploadedFile.objects.create(
            job=target_job,
            slot_type=source.slot_type,
            file=source.file.name,
            original_name=source.original_name,
            sha256=source.sha256,
            size=source.size,
            parse_status=status,
            parse_error=source.parse_error,
            header_row=source.header_row,
            detected_headers=source.detected_headers,
            column_mapping=source.column_mapping,
            parse_summary=source.parse_summary,
        )
        ParsedRow.objects.bulk_create([
            ParsedRow(
                uploaded_file=target,
                sheet_name=row.sheet_name,
                source_row=row.source_row,
                raw_data=row.raw_data,
                row_hash=row.row_hash,
            )
            for row in source.parsed_rows.all()
        ], batch_size=1000)
        cloned.append(target)
    return cloned
