from django.urls import path

from core import views

app_name = 'core'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('initial-import/', views.initial_data_import, name='initial_data_import'),
    path('bank-paste/', views.bank_paste, name='bank_paste'),
    path('bank-reset/', views.bank_reconciliation_reset, name='bank_reconciliation_reset'),
    path('jobs/new/', views.job_create, name='job_create'),
    path('jobs/<int:pk>/', views.job_detail, name='job_detail'),
    path('jobs/<int:pk>/upload/', views.job_upload, name='job_upload'),
    path('files/<int:pk>/mapping/', views.file_mapping, name='file_mapping'),
    path('files/<int:pk>/process/', views.file_process, name='file_process'),
    path('jobs/<int:pk>/analyze/', views.job_analyze, name='job_analyze'),
    path('jobs/<int:pk>/charges/', views.job_generate_charges, name='job_generate_charges'),
    path('jobs/<int:pk>/make-current/', views.job_make_current, name='job_make_current'),
    path('jobs/<int:pk>/finalize/', views.job_finalize, name='job_finalize'),
    path('jobs/<int:pk>/export/', views.job_export, name='job_export'),
    path('exports/all/', views.export_all, name='export_all'),

    path('members/', views.member_list, name='member_list'),
    path('members/lookup/', views.member_lookup, name='member_lookup'),
    path('closed-members/', views.closed_member_list, name='closed_member_list'),
    path('members/export/', views.member_export, name='member_export'),
    path('members/new/', views.member_create, name='member_create'),
    path('members/<int:pk>/', views.member_detail, name='member_detail'),
    path('members/<int:pk>/payment-history/', views.member_payment_history, name='member_payment_history'),
    path('members/<int:pk>/edit/', views.member_edit, name='member_edit'),
    path('members/<int:pk>/close/', views.member_close, name='member_close'),
    path('members/<int:pk>/manual-payment/', views.member_manual_payment, name='member_manual_payment'),
    path('members/<int:pk>/reopen/', views.member_reopen, name='member_reopen'),
    path('members/<int:pk>/join/', views.member_join, name='member_join'),
    path('members/<int:pk>/leave/', views.member_leave, name='member_leave'),
    path('members/<int:pk>/transfer/', views.member_transfer, name='member_transfer'),
    path('members/<int:pk>/address-check-clear/', views.address_check_clear, name='address_check_clear'),

    path('bank-transactions/', views.bank_transaction_list, name='bank_transaction_list'),
    path('card-transactions/', views.card_transaction_list, name='card_transaction_list'),
    path('card-transactions/upload/<str:provider>/', views.card_upload, name='card_upload'),
    path('payments/<int:pk>/allocate/', views.payment_allocate, name='payment_allocate'),
    path('payments/<int:pk>/certificate-candidate/', views.payment_certificate_candidate, name='payment_certificate_candidate'),
    path('charges/', views.charge_list, name='charge_list'),

    path('refunds/', views.refund_list, name='refund_list'),
    path('members/<int:member_pk>/refunds/new/', views.refund_create, name='refund_create'),
    path('refunds/<int:pk>/complete/', views.refund_complete, name='refund_complete'),

    path('messages/', views.message_list, name='message_list'),
    path('messages/arrears/', views.arrears_compose, name='arrears_compose'),
    path('messages/refunds/', views.refund_message_compose, name='refund_message_compose'),
    path('messages/batches/<int:pk>/', views.message_batch_detail, name='message_batch_detail'),
    path('messages/batches/<int:pk>/edit/', views.message_batch_edit, name='message_batch_edit'),

    path('legal-notices/', views.legal_notice_list, name='legal_notice_list'),
    path('members/<int:member_pk>/legal-notices/new/', views.legal_notice_create, name='legal_notice_create'),
    path('legal-notices/<int:pk>/edit/', views.legal_notice_edit, name='legal_notice_edit'),

    path('audit/', views.audit_list, name='audit_list'),
]
