from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import AccountType, Member, ParsedRow, UploadedFile
from core.utils import normalize_header, normalize_text, normalize_vehicle_no, parse_date


ALIASES = {
    'name': ['성명', '이름'],
    'birth6': ['주민등록번호', '주민번호', '생년월일'],
    'vehicle_no': ['차량번호', '자동차등록번호'],
    'management_no': ['관리번호'],
    'join_date': ['가입일자', '협회가입일자', '협회가입일'],
    'certificate_date': ['자격증명발급일자', '자격증명발급일'],
    'membership_status': ['가입여부', '협회가입여부', '가입상태'],
}


def _value(raw, names):
    canonical = raw.get('__canonical__') or {}
    canonical_map = {normalize_header(key): value for key, value in canonical.items()}
    raw_map = {normalize_header(key): value for key, value in raw.items() if not str(key).startswith('__')}
    for name in names:
        key = normalize_header(name)
        if key in canonical_map and canonical_map[key] not in (None, ''):
            return canonical_map[key]
        if key in raw_map and raw_map[key] not in (None, ''):
            return raw_map[key]
    return None


def _name(value):
    return ''.join(str(value or '').split())


def _birth6(value):
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[:6] if len(digits) >= 6 else ''


class Command(BaseCommand):
    help = '전체면허자현황 원본행에서 가입일·자격증명 발급일·가입상태를 복구합니다.'

    @transaction.atomic
    def handle(self, *args, **options):
        upload = (
            UploadedFile.objects.filter(
                slot_type=UploadedFile.SlotType.LICENSE,
                parsed_rows__isnull=False,
            ).distinct()
            .exclude(parse_status=UploadedFile.ParseStatus.FAILED)
            .order_by('-created_at', '-id')
            .first()
        )
        if not upload:
            self.stdout.write('전체면허자현황 업로드 이력이 없어 건너뜁니다.')
            return

        members = list(Member.objects.filter(is_active_record=True).prefetch_related('vehicles', 'membership_events'))
        by_identity = defaultdict(list)
        by_vehicle = defaultdict(list)
        for member in members:
            key = (_name(member.name), member.birth6 or '')
            by_identity[key].append(member)
            for vehicle in member.vehicles.all():
                if vehicle.is_current:
                    by_vehicle[vehicle.normalized_vehicle_no or normalize_vehicle_no(vehicle.vehicle_no)].append(member)

        changed = {}
        matched = skipped = 0
        for row in ParsedRow.objects.filter(uploaded_file=upload).iterator(chunk_size=1000):
            raw = row.raw_data or {}
            name = _name(_value(raw, ALIASES['name']))
            birth6 = _birth6(_value(raw, ALIASES['birth6']))
            vehicle_no = normalize_vehicle_no(_value(raw, ALIASES['vehicle_no']))
            if not name:
                continue

            candidates = by_vehicle.get(vehicle_no, []) if vehicle_no else []
            if len(candidates) != 1:
                candidates = by_identity.get((name, birth6), []) if birth6 else []
            if len(candidates) != 1:
                skipped += 1
                continue
            member = candidates[0]
            matched += 1

            canonical = raw.get('__canonical__') or {}
            join_raw = canonical.get('join_date') or _value(raw, ALIASES['join_date'])
            cert_raw = canonical.get('certificate_date') or _value(raw, ALIASES['certificate_date'])
            status_raw = canonical.get('membership_status') or _value(raw, ALIASES['membership_status'])
            management_no = str(canonical.get('management_no') or _value(raw, ALIASES['management_no']) or '').strip()
            join_date = parse_date(join_raw)
            cert_date = parse_date(cert_raw)
            status_key = normalize_text(status_raw)
            has_manual_events = bool(list(member.membership_events.all()))
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

            if not has_manual_events:
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
            member.membership_mark_raw = str(status_raw or join_raw or '')
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
        self.stdout.write(
            self.style.SUCCESS(
                f'회원정보 복구 완료: 원본 연결 {matched}명, 실제 수정 {len(changed)}명, 확인 제외 {skipped}건'
            )
        )
