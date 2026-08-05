from django.core.management.base import BaseCommand

from core.services.messaging import ensure_default_templates


class Command(BaseCommand):
    help = '기본 문자 템플릿과 시스템 초기값을 생성합니다.'

    def handle(self, *args, **options):
        ensure_default_templates()
        self.stdout.write(self.style.SUCCESS('기본 문자 템플릿 생성 완료'))
