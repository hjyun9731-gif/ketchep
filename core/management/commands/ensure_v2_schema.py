from django.core.management.base import BaseCommand
from django.db import connection

from core.models import Member, PayerAlias


class Command(BaseCommand):
    help = '기존 v1 syncdb 데이터베이스에 v2 필드와 테이블을 안전하게 추가합니다.'

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
                field = Member._meta.get_field('receivable_account_type')
                if field.column not in columns:
                    schema_editor.add_field(Member, field)
                    self.stdout.write(self.style.SUCCESS('Member.receivable_account_type 컬럼 추가'))
                else:
                    self.stdout.write('Member.receivable_account_type 컬럼 이미 존재')

            if alias_table not in tables:
                schema_editor.create_model(PayerAlias)
                self.stdout.write(self.style.SUCCESS('PayerAlias 테이블 생성'))
            else:
                self.stdout.write('PayerAlias 테이블 이미 존재')
