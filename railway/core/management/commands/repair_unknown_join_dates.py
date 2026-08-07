from django.core.management.base import BaseCommand
from core.models import Member
from core.utils import normalize_text


class Command(BaseCommand):
    help = '원본 가입표시 O/가입만 있고 실제 가입일이 없는 회원의 임의 부과기준일을 제거합니다.'

    def handle(self, *args, **options):
        markers = {'o', '0', '○', '가입', '협회가입', '회원', 'y', 'yes'}
        changed = 0
        qs = Member.objects.filter(
            membership_status=Member.MembershipStatus.ACTIVE,
            membership_started_on__isnull=True,
        ).exclude(membership_mark_raw='')
        for member in qs.iterator(chunk_size=500):
            if normalize_text(member.membership_mark_raw) not in markers:
                continue
            if member.membership_billing_anchor is not None:
                member.membership_billing_anchor = None
                member.save(update_fields=['membership_billing_anchor', 'updated_at'])
                changed += 1
        self.stdout.write(self.style.SUCCESS(f'가입일자 미상 정리 완료: {changed}명'))
