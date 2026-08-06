from django.core.management.base import BaseCommand

from core.models import Member, ParsedRow
from core.utils import normalize_header


class Command(BaseCommand):
    help = '기존 업로드 원본행에서 실제 관리번호를 안전하게 복구합니다.'

    def handle(self, *args, **options):
        targets = Member.objects.filter(management_no='').exclude(source_row_key='').only('id', 'source_row_key')
        updates = []
        checked = 0
        for member in targets.iterator(chunk_size=500):
            checked += 1
            try:
                upload_id, sheet_name, source_row = member.source_row_key.split(':', 2)
                source_row = int(source_row)
                parsed = ParsedRow.objects.filter(
                    uploaded_file_id=int(upload_id),
                    sheet_name=sheet_name,
                    source_row=source_row,
                ).only('raw_data').first()
            except (ValueError, TypeError):
                parsed = None
            if not parsed:
                continue
            raw = parsed.raw_data or {}
            value = (raw.get('__canonical__') or {}).get('management_no')
            if value in (None, ''):
                for key, raw_value in raw.items():
                    if key == '__canonical__':
                        continue
                    if normalize_header(key) == normalize_header('관리번호'):
                        value = raw_value
                        break
            if value in (None, ''):
                continue
            member.management_no = str(value).strip()
            updates.append(member)
            if len(updates) >= 500:
                Member.objects.bulk_update(updates, ['management_no'], batch_size=500)
                updates.clear()
        if updates:
            Member.objects.bulk_update(updates, ['management_no'], batch_size=500)
        restored = Member.objects.exclude(management_no='').count()
        self.stdout.write(self.style.SUCCESS(f'관리번호 복구 확인: 대상 {checked}명 / 현재 보유 {restored}명'))
