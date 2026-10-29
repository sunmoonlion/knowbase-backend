"""按环境配置拼出公开原件的那道门。

资格、权限、平台总开关各指什么，见 `app/domain/public_access.py`。
"""

from __future__ import annotations

from app.domain.public_access import PublicGate
from core.config import Settings


def public_gate(settings: Settings) -> PublicGate:
    return PublicGate(
        enabled=settings.public_access_enabled,
        qualification=settings.public_access_qualification,
        qualified=settings.public_access_qualified_subjects,
    )
