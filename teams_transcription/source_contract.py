"""Closed host provenance for SQLite source and derived summary records."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SummaryState(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal[1]
    artifact_kind: Literal['derived_summary']
    revision: int = Field(ge=1)
    published_at: str = Field(max_length=64)
    source_version: int = Field(ge=0)
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_count: int = Field(ge=1)
    generation_mode: Literal['codex','mock']
    model: str | None = Field(default=None, pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
    provider_requirement_met: bool = False
    format: Literal['json-v1'] = 'json-v1'

    @model_validator(mode='after')
    def verify_host_metadata(self):
        if type(self.schema_version) is not int:
            raise ValueError('invalid_summary_metadata')
        if self.model is not None and (self.generation_mode != 'codex'
                or self.model.lower().startswith(('gpt-live','gpt-realtime'))):
            raise ValueError('invalid_summary_model')
        if self.provider_requirement_met != (self.generation_mode == 'codex' and self.model is not None):
            raise ValueError('invalid_summary_acceptance')
        return self
