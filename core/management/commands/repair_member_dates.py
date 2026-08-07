from __future__ import annotations

from collections import defaultdict

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import AccountType, Member, ParsedRow, UploadedFile
from core.utils import normalize_header, normalize_text, normalize_vehicle_no, parse_date


ALIASES = {
    'name': ['성명', '이름'],
    'birth6': ['주민등록번호', '주민번호', '생년월일'],
    'vehicle_no': ['차량번호', '자동차등록번호'],
    'management_no': ['관리번호', '관리 번호'],
    'join_date': ['가입일자', '협회가입일자', '협회가입일', '가입일'],
    'certificate_date': ['자격증명발급일자', '자격증명발급일', '발급일자'],
    'membership_status': ['가입여부', '협회가입여부', '가입상태'],
}


def _flat_map(raw):
    values = {}
    for source in (raw or {}, (raw or {}).get('__canonical__') or {}):
        for key, value in source.items():
            if str(key).startswith('__'):
                continue
            normalized = normalize_header(key)
            if normalized and value not in (None, ''):
                values.setdefault(normalized, value)
    return values


def _value(values, names):
    for name in names:
        value = values.get(normalize_header(name))
        if value not in (None, ''):
            return value
    return None


def _name(value):
    return ''.join(str(value or '').split())


def _birth6(value):
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[:6] if len(digits) >= 6 else ''


class Command(BaseCommand):
    help = '저장된 전체면허자현황 원본행에서 관리번호·가입상태·가입일·자격증명 발급일을 복구합니다.'

    @transaction.atomic
    def handle(self, *args, **options):
        upload = (
            UploadedFile.objects.filter(slot_type=UploadedFile.SlotType.LICENSE)
            .exclude(parse_status=UploadedFile.ParseStatus.FAILED)
            .order_by('-created_at', '-id')
            .first()
        )
        if not upload or not ParsedRow.objects.filter(uploaded_file=upload).exists():
            self.stdout.write('전체면허자현황 원본행이 없어 날짜 복구를 건너뜁니다.')
            return

        members = list(
            Member.objects.filter(is_active_record=True)
            .prefetch_related('vehicles', 'membership_events')
        )
        by_management_no = defaultdict(list)
        by_identity = defaultdict(list)
        by_vehicle = defaultdict(list)
        manual_event_ids = set()
        for member in members:
            if member.management_no:
                by_management_no[normalize_text(member.management_no)].append(member)
            by_identity[(_name(member.name), member.birth6 or '')].append(member)
            for vehicle in member.vehicles.all():
                if vehicle.is_current:
                    by_vehicle[vehicle.normalized_vehicle_no or normalize_vehicle_no(vehicle.vehicle_no)].append(member)
            if list(member.membership_events.all()):
                manual_event_ids.add(member.id)

        changed = {}
        matched = ambiguous = no_date = 0
        for row in ParsedRow.objects.filter(uploaded_file=upload).iterator(chunk_size=1000):
            values = _flat_map(row.raw_data or {})
            management_no = str(_value(values, ALIASES['management_no']) or '').strip()
            name = _name(_value(values, ALIASES['name']))
            birth6 = _birth6(_value(values, ALIASES['birth6']))
            vehicle_no = normalize_vehicle_no(_value(values, ALIASES['vehicle_no']))
            join_raw = _value(values, ALIASES['join_date'])
            cert_raw = _value(values, ALIASES['certificate_date'])
            status_raw = _value(values, ALIASES['membership_status'])
            join_date = parse_date(join_raw)
            cert_date = parse_date(cert_raw)
            if not management_no and not name and not vehicle_no:
                continue
            if not join_date and not cert_date and status_raw in (None, ''):
                no_date += 1

            candidates = by_management_no.get(normalize_text(management_no), []) if management_no else []
            if len(candidates) != 1 and vehicle_no:
                candidates = by_vehicle.get(vehicle_no, [])
            if len(candidates) != 1 and name and birth6:
                candidates = by_identity.get((name, birth6), [])
            if len(candidates) != 1:
                ambiguous += 1
                continue

            member = candidates[0]
            matched += 1
            dirty = False
            if management_no and member.management_no != management_no:
                member.management_no = management_no
                dirty = True
            if join_date and member.membership_started_on != join_date:
                member.membership_started_on = join_date
                dirty = True
            if cert_date and member.certificate_issued_on != cert_date:
                member.certificate_issued_on = cert_date
                if not member.certificate_date_recorded_on:
                    member.certificate_date_recorded_on = member.first_seen_on or timezone.localdate()
                dirty = True

            status_key = normalize_text(status_raw)
            if member.id not in manual_event_ids:
                if status_key in {'가입', '협회가입', '회원', 'o', '0', '○', 'y', 'yes'}:
                    if member.membership_status != Member.MembershipStatus.ACTIVE:
                        member.membership_status = Member.MembershipStatus.ACTIVE
                        dirty = True
                    if member.receivable_account_type != AccountType.MEMBERSHIP_FEE:
                        member.receivable_account_type = AccountType.MEMBERSHIP_FEE
                        dirty = True
                elif status_key in {'미가입', '비가입', '비회원', 'x', 'n', 'no'}:
                    if member.membership_status != Member.MembershipStatus.NON_MEMBER:
                        member.membership_status = Member.MembershipStatus.NON_MEMBER
                        dirty = True
                    if member.receivable_account_type != AccountType.MANAGEMENT_FEE:
                        member.receivable_account_type = AccountType.MANAGEMENT_FEE
                        dirty = True
            raw_mark = str(status_raw or join_raw or '')
            if raw_mark and member.membership_mark_raw != raw_mark:
                member.membership_mark_raw = raw_mark
                dirty = True
            if dirty:
                member.updated_at = timezone.now()
                changed[member.id] = member

        if changed:
            Member.objects.bulk_update(
                list(changed.values()),
                [
                    'management_no', 'membership_started_on', 'certificate_issued_on',
                    'certificate_date_recorded_on', 'membership_status',
                    'receivable_account_type', 'membership_mark_raw', 'updated_at',
                ],
                batch_size=500,
            )
        cache.delete_many(['member-status-counts-v4', 'member-regions-v4'])
        self.stdout.write(self.style.SUCCESS(
            f'회원 원본복구 완료: 연결 {matched}건, 실제수정 {len(changed)}명, 확인제외 {ambiguous}건, 날짜없는 원본 {no_date}건'
        ))
