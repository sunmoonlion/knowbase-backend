"""公开原件全市场读、跨全市场查事实的一道门（SDD 0047 第二、四、五节；决定 ②⑪）。

设计写的是「资格、权限、平台总开关」同一道门，现在默认开。三样在这里各指：

- **平台总开关**：按环境配（`PUBLIC_ACCESS_ENABLED`），现在各环境默认开；正式对外开放前
  按律师意见确认开或关（决定 ②）。关着时谁都过不去。
- **资格**：这个用户有没有资格用全市场的能力（决定 ⑪「能不能选股、排名是用户的资格
  问题」）。设计没写资格从哪来、谁审，这里按环境配：`PUBLIC_ACCESS_QUALIFICATION`
  为 `none`（不要资格，现在的默认，对上「现在默认开」）或 `listed`（只认
  `PUBLIC_ACCESS_QUALIFIED_SUBJECTS_JSON` 里登记的用户，资格审查在平台那边做完再登记）。
  不开用户自己声明资格的路。
- **权限**：每条记录「谁能看」（0047 第 5.2 节）：公开原件是公开的，用户的资料只给他
  自己与授权范围。门上核的是这一份确实是公开原件、没禁用没清除；跨全市场查事实只查
  公开原件的行（仓储那边按 `public:disclosure` 限定）。

第二区引用不过这道门：引用是本人确认后记在他名下的，开关关着也能读（0047 第 2.2 节，
第 2 步 2b）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.domain.user_library import PUBLIC_OWNER, Material
from app.domain.user_runs import denied

Qualification = Literal["none", "listed"]


@dataclass(frozen=True)
class PublicGate:
    enabled: bool = True
    qualification: Qualification = "none"
    qualified: frozenset[str] = field(default_factory=frozenset)

    def allows(self, subject: str) -> bool:
        """开关开着、他有资格（列候选、按描述查用：过不去的不算候选，不报错）。"""
        if not self.enabled:
            return False
        return self.qualification == "none" or subject in self.qualified

    def check(self, subject: str, material: Material | None = None) -> None:
        """过门：给了资料的，还核它是公开原件、还能读。"""
        if not self.enabled:
            raise denied("public_access_closed", "public access is closed")
        if self.qualification != "none" and subject not in self.qualified:
            raise denied(
                "qualification_required", "public access needs a qualification"
            )
        if material is not None and (
            material.owner != PUBLIC_OWNER or not material.readable
        ):
            raise denied("not_public", "this is not a readable public original")
