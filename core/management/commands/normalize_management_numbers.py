from django.core.management.base import BaseCommand
from core.models import Member


class Command(BaseCommand):
    help = '관리번호의 잘못된 0/공백 값을 빈값으로 정리합니다.'

    def handle(self, *args, **options):
        qs = Member.objects.filter(management_no__in=['0', '0.0', '00'])
        count = qs.count()
        if count:
            qs.update(management_no='')
        self.stdout.write(self.style.SUCCESS(f'관리번호 0값 정리: {count}명'))
