"""运行授权的时限上限（不碰数据库）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.user_runs import grant_expiry

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def test_platform_incubation_lasts_up_to_thirty_days():
    """decisions 10-29「孵化第 5 步的三件」①：平台孵化授权最长 30 天，
    和 knowlab 那头一致。"""
    assert grant_expiry("platform", NOW, None, "incubate") == NOW + timedelta(days=30)
    assert grant_expiry("platform", NOW, 3600, "incubate") == NOW + timedelta(hours=1)
    assert grant_expiry("platform", NOW, None, "test") == NOW + timedelta(days=14)
