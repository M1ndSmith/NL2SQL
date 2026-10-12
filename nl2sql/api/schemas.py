"""Request bodies. The role is never accepted from the client."""
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class LoginBody(BaseModel):
    user_id: str = Field(min_length=1)
    password: str = Field(min_length=1)


class QuestionBody(BaseModel):
    question: str = Field(min_length=1)
    thread_id: str | None = None
    provider: str | None = None

    @model_validator(mode="after")
    def blank_provider_is_omitted(self):
        if self.provider is not None:
            cleaned = self.provider.strip().lower()
            self.provider = cleaned or None
        return self


class DecisionBody(BaseModel):
    action: Literal["approve", "reject", "edit"]
    sql: str | None = None

    @model_validator(mode="after")
    def edit_needs_sql(self):
        if self.action == "edit" and not (self.sql or "").strip():
            raise ValueError("edit requires sql")
        return self
