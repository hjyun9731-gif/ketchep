from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import get_template


class Command(BaseCommand):
    help = '배포 전에 모든 HTML 템플릿을 실제 Django 엔진으로 불러와 문법 오류를 검사합니다.'

    def handle(self, *args, **options):
        roots = [
            Path(settings.BASE_DIR) / 'templates',
            Path(settings.BASE_DIR) / 'core' / 'templates',
        ]
        names = []
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob('*.html')):
                names.append(path.relative_to(root).as_posix())

        failures = []
        for name in sorted(set(names)):
            try:
                get_template(name)
            except Exception as exc:  # Django reports the exact template/filter/tag error.
                failures.append(f'{name}: {exc.__class__.__name__}: {exc}')

        if failures:
            raise CommandError('템플릿 검사 실패\n' + '\n'.join(failures))

        self.stdout.write(self.style.SUCCESS(f'템플릿 검사 완료: {len(set(names))}개'))
