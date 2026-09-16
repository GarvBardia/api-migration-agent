"""pydantic v1's @validator decorator is deprecated in v2 in favor of
@field_validator (v1-style validator methods also take a bare value
without @classmethod, which field_validator requires)."""

from pydantic import BaseModel, validator


class Item(BaseModel):
    name: str

    @validator("name")
    def name_must_not_be_blank(cls, v):
        if not v.strip():
            raise ValueError("name must not be blank")
        return v
