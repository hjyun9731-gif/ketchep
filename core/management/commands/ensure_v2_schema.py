from django.core.management.base import BaseCommand
from django.db import connection

from core.models import Member, PayerAlias


class Command(BaseCommand):
    help = '기존 syncdb 데이터베이스에 누락 필드·테이블·성능 인덱스를 안전하게 추가합니다.'

    def handle(self, *args, **options):
        tables = set(connection.introspection.table_names())
        member_table = Member._meta.db_table
        alias_table = PayerAlias._meta.db_table

        with connection.schema_editor() as schema_editor:
            if member_table in tables:
                with connection.cursor() as cursor:
                    columns = {
                        column.name
                        for column in connection.introspection.get_table_description(cursor, member_table)
                    }
                for field_name in ('receivable_account_type', 'management_no'):
                    field = Member._meta.get_field(field_name)
                    if field.column not in columns:
                        schema_editor.add_field(Member, field)
                        self.stdout.write(self.style.SUCCESS(f'Member.{field_name} 컬럼 추가'))
                    else:
                        self.stdout.write(f'Member.{field_name} 컬럼 이미 존재')

            if alias_table not in tables:
                schema_editor.create_model(PayerAlias)
                self.stdout.write(self.style.SUCCESS('PayerAlias 테이블 생성'))
            else:
                self.stdout.write('PayerAlias 테이블 이미 존재')

        # The project originally used syncdb without migrations. Add the
        # composite indexes explicitly so existing Railway databases also get
        # the performance fix. CREATE INDEX IF NOT EXISTS works on PostgreSQL
        # and SQLite, the two supported database engines here.
        tables = set(connection.introspection.table_names())
        qn = connection.ops.quote_name
        indexes = [
            ('idx_member_active_status_name', 'core_member', ['is_active_record', 'operational_status', 'name', 'id']),
            ('idx_vehicle_member_current', 'core_vehicle', ['member_id', 'is_current', 'id']),
            ('idx_charge_member_status_job', 'core_charge', ['member_id', 'status', 'monthly_job_id']),
            ('idx_settlement_charge_active', 'core_chargesettlement', ['charge_id', 'is_active']),
        ]
        with connection.cursor() as cursor:
            for index_name, table_name, columns in indexes:
                if table_name not in tables:
                    continue
                column_sql = ', '.join(qn(column) for column in columns)
                cursor.execute(
                    f'CREATE INDEX IF NOT EXISTS {qn(index_name)} '
                    f'ON {qn(table_name)} ({column_sql})'
                )
                self.stdout.write(f'성능 인덱스 확인: {index_name}')
