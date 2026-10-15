"""休眠能力的声明与校验（REQ-009 的机制部分）。

**问题**：本仓有一批"代码在、没接线"的能力。它们最容易被误读成已可用——
看到 `Outbox` 仓库类就以为事件在投递，看到 `beat_schedule` 入口就以为有定时任务。
投影文档里列了清单，但**文档不会自己变假**：能力接上了、或判据锚点被改名了，
清单照旧写着，读的人照旧被误导。

**机制**：清单以可执行判据的形式声明在这里，每条两个方向都能失败——

  ``anchor_exists``   锚点还在吗。**为 False 说明判据自己失效了**（文件改名、
                      类改名），此时 ``still_dormant`` 会因为"找不到"而假性通过。
                      这是本项目反复踩过的坑：把"没找到"当成"不存在"。
  ``still_dormant``   还休眠着吗。为 False 说明**能力已接线**，声明过期——
                      要删掉本条，并同步更新 `repos/knowbase-app.md`「已知未实现」。

**机制的边界**：它保证**已声明的条目不会变陈旧**，但**发现不了新出现的休眠能力**
——那需要判断"这段代码本该接线却没接"，不可机械判定。新增休眠能力时手工加一条。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _exists(rel: str) -> bool:
    return (ROOT / rel).exists()


@dataclass(frozen=True)
class Dormant:
    name: str
    kind: str  # deliberate=模板有意留白 / pending=欠账
    evidence: str
    anchor_exists: Callable[[], bool]
    still_dormant: Callable[[], bool]


# 前端页在另一个组件仓，从 backend 仓根往上两级
_ADMIN_INGESTIONS_PAGE = (
    ROOT.parents[1]
    / "knowbase-admin-frontend/app/app/[locale]/(dashboard)"
    / "knowledge/ingestions/page.tsx"
)


def _domain_dirs() -> list[Path]:
    return [ROOT / "app/domain" / d for d in ("models", "repositories", "services")]


# 只认**共享（模板）**的 Outbox 符号。实例仓可能有自己的领域 outbox
# （如 knowfeed 的 DeliveryOutboxMessage，它是在用的），不能一见 "outbox" 就算命中。
# 词边界让 \bOutboxMessage\b 不会匹配到 DeliveryOutboxMessage。
_SHARED_OUTBOX = re.compile(
    r"\b(OutboxPublisher|OutboxRepository|SqlOutboxRepository"
    r"|OutboxMessage|InboxMessage)\b"
)


def _shared_outbox_used_by_services() -> bool:
    return any(
        _SHARED_OUTBOX.search(p.read_text(encoding="utf-8"))
        for p in (ROOT / "app/application/services").rglob("*.py")
    )


DORMANT: tuple[Dormant, ...] = (
    Dormant(
        name="domain/{models,repositories,services} 仍是模板空壳",
        kind="pending",
        evidence=(
            "本仓继承自模板的这三个包只有空 __init__.py；实际领域代码在 "
            "infrastructure/models/ 与 application/services/。"
            "即分层目录名与代码实际归属不一致，读目录名会误判"
        ),
        anchor_exists=lambda: all(d.is_dir() for d in _domain_dirs()),
        still_dormant=lambda: all(
            [p.name for p in d.iterdir() if p.suffix == ".py"] == ["__init__.py"]
            for d in _domain_dirs()
        ),
    ),
    Dormant(
        name="web-interaction 运行时",
        kind="deliberate",
        evidence="默认适配器是 UnavailableWebInteractionAdapter，生产必定 503",
        anchor_exists=lambda: (
            "class UnavailableWebInteractionAdapter"
            in _read("app/application/services/web_interaction.py")
        ),
        still_dormant=lambda: (
            "return UnavailableWebInteractionAdapter()"
            in _read("app/application/services/web_interaction.py")
        ),
    ),
    Dormant(
        name="Admin「入库任务」运维页",
        kind="pending",
        evidence=(
            "页面只有 34 行的 API 路径文案，无 fetch、无表格、无操作——"
            "后端 ingestion 接口齐全，运维仍得手敲 curl"
        ),
        anchor_exists=lambda: _ADMIN_INGESTIONS_PAGE.exists(),
        still_dormant=lambda: (
            not any(
                marker in _ADMIN_INGESTIONS_PAGE.read_text(encoding="utf-8")
                for marker in ("fetch(", "useQuery", "useEffect", "<table")
            )
        ),
    ),
)


@pytest.mark.parametrize("item", DORMANT, ids=lambda i: i.name)
def test_dormant_capability_anchor_still_exists(item: Dormant) -> None:
    """锚点消失 → 判据空转。必须先修判据，否则下一条检查是假通过。"""
    assert item.anchor_exists(), (
        f"休眠声明「{item.name}」的锚点不见了——判据已失效，会假性通过。"
        f"先把判据改到新位置；确已删除该能力，则删掉本条声明。"
    )


@pytest.mark.parametrize("item", DORMANT, ids=lambda i: i.name)
def test_declared_dormant_capability_is_still_dormant(item: Dormant) -> None:
    """能力接线了 → 声明过期。删条目，并同步投影文档。"""
    assert item.still_dormant(), (
        f"休眠声明「{item.name}」已不成立——该能力看起来已接线。"
        f"请删掉本条声明，并同步更新 k8s:sunmoonai/docs/project-guide/"
        f"repos/knowbase-app.md 的「已知未实现」一节。依据：{item.evidence}"
    )


def test_every_dormant_entry_declares_why() -> None:
    """kind 只有两种：deliberate（有意留白）与 pending（欠账）。

    这一栏决定读者要不要担心：deliberate 是设计，pending 是债。
    """
    for item in DORMANT:
        assert item.kind in {"deliberate", "pending"}, item.name
        assert item.evidence.strip(), item.name
