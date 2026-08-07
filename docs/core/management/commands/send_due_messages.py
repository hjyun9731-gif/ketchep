from django.core.management.base import BaseCommand

from core.services.messaging import send_due_batches


class Command(BaseCommand):
    help = '예약시간이 지난 문자 발송 건을 처리합니다.'

    def handle(self, *args, **options):
        result = send_due_batches()
        self.stdout.write(self.style.SUCCESS(f'예약문자 처리 완료: {result}'))
