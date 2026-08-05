from django.contrib import admin

from core import models


@admin.register(models.Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ('name', 'current_vehicle_display', 'region', 'membership_status', 'operational_status', 'phone')
    list_filter = ('membership_status', 'operational_status', 'region', 'address_needs_check', 'phone_needs_check', 'sms_opt_out')
    search_fields = ('name', 'birth6', 'phone', 'address', 'official_address', 'vehicles__vehicle_no')

    @admin.display(description='현재 차량')
    def current_vehicle_display(self, obj):
        return obj.current_vehicle.vehicle_no if obj.current_vehicle else '-'


@admin.register(models.Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ('vehicle_no', 'member', 'purpose_char', 'is_current', 'start_date', 'end_date')
    search_fields = ('vehicle_no', 'member__name')
    list_filter = ('purpose_char', 'is_current')


@admin.register(models.MonthlyJob)
class MonthlyJobAdmin(admin.ModelAdmin):
    list_display = ('year', 'month', 'version', 'version_name', 'status', 'is_current', 'updated_at')
    list_filter = ('year', 'month', 'status', 'is_current')


@admin.register(models.BankTransaction)
class BankTransactionAdmin(admin.ModelAdmin):
    list_display = ('transaction_at', 'payer_text', 'amount', 'bank_account_label', 'status', 'job')
    list_filter = ('status', 'job', 'bank_account_label', 'is_card_settlement')
    search_fields = ('payer_text', 'txn_key')


@admin.register(models.Charge)
class ChargeAdmin(admin.ModelAdmin):
    list_display = ('charge_date', 'member', 'account_type', 'amount', 'status', 'monthly_job')
    list_filter = ('account_type', 'status', 'charge_date')
    search_fields = ('member__name', 'member__vehicles__vehicle_no')


@admin.register(models.Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('payment_date', 'source_type', 'amount', 'allocated_amount_display', 'status')
    list_filter = ('source_type', 'status')

    @admin.display(description='배정액')
    def allocated_amount_display(self, obj):
        return obj.allocated_amount


@admin.register(models.Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ('member', 'account_type', 'amount', 'status', 'refund_date', 'bank', 'holder')
    list_filter = ('status', 'account_type')


@admin.register(models.MessageBatch)
class MessageBatchAdmin(admin.ModelAdmin):
    list_display = ('message_type', 'status', 'scheduled_at', 'sent_at', 'provider_job_no')
    list_filter = ('message_type', 'status')


@admin.register(models.LegalNotice)
class LegalNoticeAdmin(admin.ModelAdmin):
    list_display = ('sent_date', 'member', 'registered_no', 'delivery_status', 'result_date')
    list_filter = ('delivery_status', 'address_type')
    search_fields = ('registered_no', 'member__name')


@admin.register(models.AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'actor', 'action', 'model_name', 'object_id')
    search_fields = ('action', 'model_name', 'object_id', 'reason')
    readonly_fields = ('actor', 'action', 'model_name', 'object_id', 'before_json', 'after_json', 'reason', 'created_at')


for model in [
    models.MembershipEvent, models.ClosureEvent, models.MemberLink, models.UploadedFile,
    models.ParsedRow, models.ImportIssue, models.CardTransaction, models.PaymentAllocationLine,
    models.ChargeSettlement, models.Prepayment, models.PrepaymentMovement, models.MessageTemplate,
    models.MessageRecipient, models.SystemSetting,
]:
    admin.site.register(model)
