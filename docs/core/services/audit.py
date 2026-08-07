from __future__ import annotations

from core.models import AuditLog
from core.utils import json_safe_model


def log_action(*, action: str, instance=None, model_name: str | None = None, object_id=None,
               before=None, after=None, reason: str = '', actor: str = 'admin') -> AuditLog:
    if instance is not None:
        model_name = model_name or instance._meta.label_lower
        object_id = object_id or instance.pk
    return AuditLog.objects.create(
        actor=actor,
        action=action,
        model_name=model_name or '',
        object_id=str(object_id or ''),
        before_json=before if before is not None else {},
        after_json=after if after is not None else (json_safe_model(instance) if instance is not None else {}),
        reason=reason,
    )
