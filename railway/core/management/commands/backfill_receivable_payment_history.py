from django.core.management.base import BaseCommand

from core.services.historical_payments import backfill_latest_receivable_payment_history


class Command(BaseCommand):
    help = '기존 미수금 파일의 2026년 1~7월 입금액/입금일을 회원별 조회용 이력으로 복구합니다.'

    def handle(self, *args, **options):
        uploaded, result = backfill_latest_receivable_payment_history()
        if uploaded is None:
            self.stdout.write('기존 미수금 업로드 이력이 없어 1~7월 입금내역 복구를 건너뜁니다.')
            return
        self.stdout.write(self.style.SUCCESS(
            '1~7월 입금내역 복구 완료: '
            f'파일={uploaded.original_name}, 회원={result["matched_members"]}명, '
            f'신규={result["created"]}건, 갱신={result["updated"]}건, 매칭제외={result["skipped"]}행'
        ))
