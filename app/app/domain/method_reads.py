"""A material owner's consent to one exact method invocation reading a pinned item."""

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class MethodNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    call_id: UUID
    method_id: UUID
    definition_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    version: int | None = Field(default=None, strict=True, ge=1)


class MethodRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: UUID
    path: tuple[MethodNode, ...] = Field(min_length=1, max_length=16)
    purpose: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    material_id: UUID
    version_no: int = Field(strict=True, ge=1)

    @property
    def parent_ref(self) -> str:
        return f"formal-task:{self.task_id}:{self.material_id}:{self.version_no}"

    @model_validator(mode="after")
    def invocation_tree(self):
        if len({node.call_id for node in self.path}) != len(self.path):
            raise ValueError("A method invocation cannot be its own ancestor")
        if any(node.version is None for node in self.path[1:]):
            raise ValueError("A child method must reference a frozen version")
        return self


class MethodReadConsent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    read: MethodRead
    expires_at: AwareDatetime


class MethodParentRequest(MethodReadConsent):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
