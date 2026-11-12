from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class InputModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class LoginInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_.-]+$')
    password: str = Field(min_length=1, max_length=256)


class ProjectInput(InputModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=8000)


class DecisionInput(InputModel):
    decision: Literal['accepted', 'rejected']
    expected_version: int = Field(ge=1)
    reason: str = Field(default='', max_length=4000)


class BatchDecisionInput(InputModel):
    items: list[str] = Field(min_length=1, max_length=100)
    decision: Literal['accepted', 'rejected']
    reason: str = Field(default='', max_length=4000)


class ChatInput(InputModel):
    text: str = Field(min_length=1, max_length=12000)
    mode: Literal['current', 'history', 'period'] = 'current'
