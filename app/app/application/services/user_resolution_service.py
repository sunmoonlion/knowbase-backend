"""按描述查、解析、确认、偏好（SDD 0043 第三节，块 2）。

- 按描述查：只认精确的键，只答「这一份在不在、在哪个范围」；同一用户限流。
- 解析：声明模板 × 任务参数展开成一条条描述，逐条在请的人名下找，每条一个结果
  （已定、你有没放开、待确认、采不来、有歧义、等数据、不能用），存成解析清单；
  运行中冒出的新需求出新修订，已有的条目不动。
- 确认：用户点一次，应用凭本人令牌交来；记清单修订、条目、声明快照、供给条件快照，
  7 天有效；按入口分支告诉应用哪些已经齐了、哪些要交 knowfeed。knowfeed 受理前来核。
- 偏好：只是排序输入，每次仍核当前权利；词表改版就不用。接口先只读。
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from app.application.ports.user_library import UserLibraryRepository
from app.application.ports.user_resolution import ResolutionRepository
from app.application.ports.user_runs import UserRunRepository
from app.application.services.call_rate import RateLimiter
from app.domain.user_data import UserDataError, not_found, refused
from app.domain.user_resolution import (
    CONFIRMATION_TTL,
    CONTRACT_VERSION,
    MAX_ITEMS,
    MAX_NEEDS,
    Candidate,
    Confirmation,
    DescriptionKey,
    NeedSpec,
    Preference,
    Resolution,
    ResultItem,
    TaskParams,
    Wanted,
    expand,
    rank,
)
from app.domain.user_runs import denied
from app.domain.vocabulary import VOCAB_VERSION, collectable

RESOLVING_APPS = ("knowbench", "knowlab")
SUPPLIERS = ("knowfeed",)
LOOKUPS_PER_MINUTE = 60
MAX_SUPPLY_BYTES = 64 * 1024
_URL = re.compile(r"^https?://[^\s]{1,2000}$")
_CONSUMER = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_lookups = RateLimiter(LOOKUPS_PER_MINUTE)


class UserResolutionService:
    def __init__(
        self,
        repo: ResolutionRepository,
        library: UserLibraryRepository,
        runs: UserRunRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        limiter: RateLimiter | None = None,
    ) -> None:
        self.repo = repo
        self.library = library
        self.runs = runs
        self.now = clock
        self.new_id = new_id
        self.limiter = limiter or _lookups

    # —— 按描述查 ——

    async def lookup(
        self, subject: str, app: str, key: DescriptionKey
    ) -> tuple[str, str | None]:
        """（在不在，在哪个范围）。shown：在、放开给这个应用了；held：在、没放开；
        unusable：在、不能用；absent：他名下没有。范围现在只有第一区（own）。"""
        key.checked()
        if not self.limiter.allow(f"lookup:{subject}"):
            raise UserDataError(429, "too_many_lookups", "slow down")
        found = await self.repo.candidates(subject, app, key)
        usable = [c for c in found if c.usable]
        if any(c.shown for c in usable):
            return "shown", "own"
        if usable:
            return "held", "own"
        if found:
            return "unusable", "own"
        return "absent", None

    # —— 解析 ——

    async def resolve(
        self,
        subject: str,
        app: str,
        consumer: str,
        contract_version: str,
        needs: list[NeedSpec],
        params: TaskParams,
        task_ref: str | None = None,
    ) -> Resolution:
        self._resolving_app(app)
        if contract_version != CONTRACT_VERSION:
            raise refused("contract_version_invalid", f"only {CONTRACT_VERSION}")
        if not _CONSUMER.fullmatch(consumer):
            raise refused("consumer_invalid", "consumer is invalid")
        checked = _checked_needs(needs)
        params.checked()
        items = await self._resolve_needs(subject, app, consumer, checked, params)
        return await self.repo.add_resolution(
            Resolution(
                id=self.new_id(),
                revision=0,
                subject=subject,
                app=app,
                consumer=consumer,
                contract_version=contract_version,
                vocab_version=VOCAB_VERSION,
                needs=tuple(checked),
                params=params,
                items=tuple(items),
                created_at=self.now(),
                task_ref=task_ref,
            )
        )

    async def add_needs(
        self, subject: str, app: str, resolution_id: str, needs: list[NeedSpec]
    ) -> Resolution:
        """运行中冒出的新需求：清单出新修订，已有条目原样带过去（冻结部分不动）。"""
        current = await self._own_resolution(subject, app, resolution_id, None)
        extra = _checked_needs(needs)
        known = {n.key for n in current.needs}
        if any(n.key in known for n in extra):
            raise refused("need_key_taken", "a need with this key is already there")
        if len(known) + len(extra) > MAX_NEEDS:
            raise refused("needs_invalid", f"at most {MAX_NEEDS} needs")
        items = await self._resolve_needs(
            subject, app, current.consumer, extra, current.params
        )
        return await self.repo.add_resolution(
            Resolution(
                id=current.id,
                revision=0,
                subject=subject,
                app=app,
                consumer=current.consumer,
                contract_version=current.contract_version,
                vocab_version=VOCAB_VERSION,
                needs=current.needs + tuple(extra),
                params=current.params,
                items=current.items + tuple(items),
                created_at=self.now(),
                task_ref=current.task_ref,
            )
        )

    async def get_resolution(
        self, subject: str, app: str, resolution_id: str, revision: int | None
    ) -> Resolution:
        return await self._own_resolution(subject, app, resolution_id, revision)

    async def _resolve_needs(
        self,
        subject: str,
        app: str,
        consumer: str,
        needs: list[NeedSpec],
        params: TaskParams,
    ) -> list[ResultItem]:
        preferences = {
            p.need_key: p
            for p in await self.repo.preferences(
                subject, consumer, CONTRACT_VERSION, params.params_hash
            )
            if p.vocab_version == VOCAB_VERSION
        }
        waiting = await self._waiting_keys(subject)
        items: list[ResultItem] = []
        for need in needs:
            groups: dict[str, list[tuple[Wanted, ResultItem]]] = {}
            for wanted in expand(need, params):
                result = await self._resolve_one(
                    subject,
                    app,
                    need,
                    wanted,
                    preferences.get(need.key),
                    waiting,
                    params,
                )
                groups.setdefault(wanted.key.subject, []).append((wanted, result))
            for group in groups.values():
                items.extend(_settle_tentative(group))
        if len(items) > MAX_ITEMS:
            raise refused("too_many_items", f"more than {MAX_ITEMS} items")
        return items

    async def _resolve_one(
        self,
        subject: str,
        app: str,
        need: NeedSpec,
        wanted: Wanted,
        preference: Preference | None,
        waiting: set[str],
        params: TaskParams,
    ) -> ResultItem:
        key = wanted.key
        found = [
            c
            for c in await self.repo.candidates(subject, app, key)
            if c.data_format in need.formats
            and (not need.sources or c.content_origin in need.sources)
        ]
        if wanted.tentative:
            # 还没到法定截止的一期：只用任务日期前已经披露、已经拿到的
            found = [
                c
                for c in found
                if c.disclosed_on and c.disclosed_on <= params.task_date
            ]
        usable = [c for c in found if c.usable]
        shown = [c for c in usable if c.shown]
        reading_missing = False
        if need.reading != "original" and shown:
            with_reading = [await self._with_reading(c, need.reading) for c in shown]
            ready = [c for c in with_reading if c is not None]
            reading_missing = not ready
            shown = ready
        if shown:
            chosen, reason = _choose(shown, need, preference)
            if chosen is None:
                return ResultItem(
                    need.key, key, "ambiguous", candidates=tuple(shown), reason=reason
                )
            if len(chosen) >= need.min_count:
                return ResultItem(
                    need.key, key, "resolved", chosen=tuple(chosen), reason=reason
                )
        hidden = [c for c in usable if not c.shown]
        if hidden and not reading_missing:
            return ResultItem(need.key, key, "not_shown", candidates=tuple(hidden))
        if found and not usable:
            return ResultItem(need.key, key, "unusable", reason="disabled")
        if _key_id(need.key, key) in waiting or _key_id(None, key) in waiting:
            return ResultItem(need.key, key, "waiting")
        if reading_missing:
            # 原件在、读法没齐：交 knowfeed 补读法
            return ResultItem(need.key, key, "to_confirm", reason="reading_missing")
        if collectable(key.subject, key.doc_type):
            return ResultItem(need.key, key, "to_confirm")
        reason = "needs_url" if key.doc_type == "webpage" else "private"
        return ResultItem(need.key, key, "not_collectable", reason=reason)

    async def _with_reading(self, c: Candidate, reading: str) -> Candidate | None:
        """要正文、按页文字、表格的：取那一版最新的就绪读法；没有就是 None。"""
        version = await self.library.version(c.material_id, c.version_no)
        if version is None:
            return None
        ready = [
            r
            for r in await self.runs.renditions(version.id)
            if r.kind == reading and r.status == "ready"
        ]
        if not ready:
            return None
        latest = max(ready, key=lambda r: r.rendition_no)
        return Candidate(
            **{
                **c.__dict__,
                "rendition_kind": latest.kind,
                "rendition_no": latest.rendition_no,
                "rendition_sha256": latest.sha256,
            }
        )

    async def _waiting_keys(self, subject: str) -> set[str]:
        """确认过、还在 7 天里的描述条目：在取，解析成「等数据」。"""
        keys: set[str] = set()
        for confirmation in await self.repo.live_confirmations(subject, self.now()):
            if confirmation.kind != "description":
                continue
            for item in confirmation.items:
                key = DescriptionKey.from_dict(item["key"])  # type: ignore[arg-type]
                keys.add(_key_id(item.get("need_key"), key))  # type: ignore[arg-type]
                keys.add(_key_id(None, key))
        return keys

    # —— 确认 ——

    async def confirm(
        self,
        subject: str,
        app: str,
        kind: str,
        items: list[dict[str, object]],
        supply: dict[str, object],
        *,
        task_ref: str | None = None,
        resolution_id: str | None = None,
        revision: int | None = None,
    ) -> tuple[Confirmation, list[dict[str, str]]]:
        """用户点一次；返回确认与入口分支（每条 ready：已经齐了，supply：交 knowfeed）。

        有清单的只认那张清单修订里待确认的条目（可以少确认几条：取消即降级或变更需求）；
        没清单的（给网址、按主题勾候选、直接按描述要）照样能确认。
        """
        self._resolving_app(app)
        if kind not in ("description", "url", "topic"):
            raise refused("kind_invalid", "kind is description, url or topic")
        if not 1 <= len(items) <= MAX_ITEMS:
            raise refused("items_invalid", f"give 1 to {MAX_ITEMS} items")
        if len(json.dumps(supply, ensure_ascii=False)) > MAX_SUPPLY_BYTES:
            raise refused("supply_too_large", "supply snapshot is too large")
        needs: tuple[dict[str, object], ...] = ()
        resolution = None
        if resolution_id is not None:
            if kind != "description":
                raise refused("kind_invalid", "a resolution confirms descriptions")
            resolution = await self._own_resolution(
                subject, app, resolution_id, revision
            )
        cleaned = [
            _confirm_item(kind, i, n + 1, resolution) for n, i in enumerate(items)
        ]
        if resolution is not None:
            used = {str(i["need_key"]) for i in cleaned}
            needs = tuple(n.as_dict() for n in resolution.needs if n.key in used)
        now = self.now()
        confirmation = await self.repo.add_confirmation(
            Confirmation(
                id=self.new_id(),
                subject=subject,
                app=app,
                kind=kind,  # type: ignore[arg-type]
                items=tuple(cleaned),
                supply=dict(supply),
                created_at=now,
                expires_at=now + CONFIRMATION_TTL,
                task_ref=task_ref,
                resolution_id=resolution.id if resolution else None,
                resolution_revision=resolution.revision if resolution else None,
                needs=needs,
            )
        )
        branches = []
        for item in cleaned:
            branch = "supply"
            if kind == "description":
                key = DescriptionKey.from_dict(item["key"])  # type: ignore[arg-type]
                found = await self.repo.candidates(subject, app, key)
                if any(c.usable and c.shown for c in found) and (
                    item.get("reading") in (None, "original")
                ):
                    branch = "ready"
            branches.append({"item_id": str(item["item_id"]), "branch": branch})
        return confirmation, branches

    async def check_confirmation(
        self, caller: str, confirmation_id: str, subject: str
    ) -> Confirmation:
        """knowfeed 受理前核：确认真、是这个用户的、还在 7 天里。"""
        if caller not in SUPPLIERS:
            raise denied("service_scope_denied", "only knowfeed checks confirmations")
        confirmation = await self.repo.confirmation(confirmation_id)
        if confirmation is None or confirmation.subject != subject:
            raise not_found("confirmation")
        if not confirmation.valid(self.now()):
            raise denied(
                "confirmation_expired", "the confirmation is older than 7 days"
            )
        return confirmation

    # —— 偏好 ——

    async def preferences(
        self, subject: str, consumer: str, params: TaskParams
    ) -> list[Preference]:
        params.checked()
        return [
            p
            for p in await self.repo.preferences(
                subject, consumer, CONTRACT_VERSION, params.params_hash
            )
            if p.vocab_version == VOCAB_VERSION
        ]

    async def record_preference(
        self,
        subject: str,
        consumer: str,
        need_key: str,
        params: TaskParams,
        material_ids: list[str],
    ) -> Preference:
        """用户开跑前换过：记成偏好（接口随 knowbench 块 6 开）。"""
        params.checked()
        if not 1 <= len(material_ids) <= MAX_ITEMS:
            raise refused("items_invalid", "give the chosen materials")
        return await self.repo.save_preference(
            Preference(
                subject=subject,
                consumer=consumer,
                contract_version=CONTRACT_VERSION,
                need_key=need_key,
                params_hash=params.params_hash,
                material_ids=tuple(dict.fromkeys(material_ids)),
                vocab_version=VOCAB_VERSION,
                updated_at=self.now(),
            )
        )

    # —— 内部 ——

    async def _own_resolution(
        self, subject: str, app: str, resolution_id: str, revision: int | None
    ) -> Resolution:
        found = await self.repo.resolution(resolution_id, revision)
        if found is None or found.subject != subject or found.app != app:
            raise not_found("resolution")
        return found

    @staticmethod
    def _resolving_app(app: str) -> None:
        if app not in RESOLVING_APPS:
            raise refused("app_invalid", "resolution runs for knowbench or knowlab")


def _checked_needs(needs: list[NeedSpec]) -> list[NeedSpec]:
    if not 1 <= len(needs) <= MAX_NEEDS:
        raise refused("needs_invalid", f"give 1 to {MAX_NEEDS} needs")
    checked = [n.checked() for n in needs]
    if len({n.key for n in checked}) != len(checked):
        raise refused("need_key_taken", "each need needs its own key")
    return checked


def _choose(
    shown: list[Candidate], need: NeedSpec, preference: Preference | None
) -> tuple[list[Candidate] | None, str | None]:
    """挑：偏好优先，其次按优先策略分档；挑不出来（同档多于空位）就是有歧义。"""
    if preference is not None:
        preferred = [c for c in shown if c.material_id in preference.material_ids]
        if preferred:
            return preferred[: need.max_count], "preference"
    chosen: list[Candidate] = []
    for tier in rank(shown, need.priority):
        room = need.max_count - len(chosen)
        if room <= 0:
            break
        if len(tier) > room:
            return None, "several_candidates"
        chosen.extend(tier)
    return chosen, need.priority


def _settle_tentative(
    group: list[tuple[Wanted, ResultItem]],
) -> list[ResultItem]:
    """一个主体的「最近 N 期」：没到截止的那一期拿到了就用它、去掉最老的一期，
    没拿到就不列（不算缺）。"""
    tentative = [r for w, r in group if w.tentative]
    firm = [r for w, r in group if not w.tentative]
    used = [r for r in tentative if r.status == "resolved"]
    if used and firm:
        firm = firm[: len(firm) - len(used)]
    return used + firm


def _key_id(need_key: str | None, key: DescriptionKey) -> str:
    return json.dumps([need_key, key.as_dict()], sort_keys=True)


def _confirm_item(
    kind: str, given: dict[str, object], number: int, resolution: Resolution | None
) -> dict[str, object]:
    item_id = f"c{number}"
    if kind == "description":
        raw = given.get("key")
        if not isinstance(raw, dict):
            raise refused("item_invalid", "a description item has a key")
        key = DescriptionKey.from_dict(raw).checked()
        need_key = given.get("need_key")
        reading = given.get("reading") or "original"
        if reading not in ("original", "text", "pages", "table"):
            raise refused("reading_invalid", "reading is invalid")
        if resolution is not None:
            allowed = {
                (i.need_key, json.dumps(i.key.as_dict(), sort_keys=True))
                for i in resolution.items
                if i.status == "to_confirm"
            }
            if (need_key, json.dumps(key.as_dict(), sort_keys=True)) not in allowed:
                raise denied(
                    "outside_resolution", "an item is not waiting for confirmation"
                )
            need = next(n for n in resolution.needs if n.key == need_key)
            reading = need.reading
            required = need.required
        else:
            required = bool(given.get("required", True))
        return {
            "item_id": item_id,
            "key": key.as_dict(),
            "need_key": need_key,
            "reading": reading,
            "required": required,
        }
    url = given.get("url")
    if not isinstance(url, str) or not _URL.fullmatch(url):
        raise refused("url_invalid", "url is invalid")
    item: dict[str, object] = {"item_id": item_id, "url": url}
    if kind == "topic":
        ref = given.get("candidate_ref")
        if not isinstance(ref, str) or not 1 <= len(ref) <= 255:
            raise refused("candidate_invalid", "a topic pick names its candidate")
        item["candidate_ref"] = ref
    title = given.get("title")
    if isinstance(title, str):
        item["title"] = title[:200]
    return item
