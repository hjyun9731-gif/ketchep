from django.conf import settings

def system_context(request):
    return {
        'association_name': settings.ASSOCIATION_NAME,
        'association_phone': settings.ASSOCIATION_PHONE,
    }
