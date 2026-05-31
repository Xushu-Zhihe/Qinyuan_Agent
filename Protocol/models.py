from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


# 全局设置：约束 intent 和 reason 的超时时间。
class Settings(BaseModel):
    # ge=5 表示该值必须大于等于 5。
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)


# Fact 表示一条已经确认的客观事实。
class Fact(BaseModel):
    id: str
    description: str


# Intent 表示一个待探索或已完成的探索方向。
class Intent(BaseModel):
    id: str
    # Python 里 from 是关键字，所以代码里用 from_，对外 JSON 仍然叫 from。
    from_: list[str] = Field(alias="from")
    # to 为 None 表示 intent 还没有结论；有值时表示它产出了哪个 fact。
    to: str | None = None
    description: str
    creator: str
    # 当前执行这个 intent 的 worker；未认领时为空。
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None

    # 允许既用字段名 from_ 初始化，也允许读取/输出别名 from。
    model_config = {"populate_by_name": True}


# Hint 表示图外的人类提示或策略建议。
class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


# ProjectReason 表示项目级 reason lease 的状态。
class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


# ProjectMeta 表示项目的基础元信息。
class ProjectMeta(BaseModel):
    id: str
    title: str
    # 项目状态只能是这三个固定值之一。
    status: Literal["active", "stopped", "completed"]
    created_at: str
    # 当前如果有人占着 reason lease，就会带上这块信息。
    reason: ProjectReason | None = None


# ProjectSummary 在 ProjectMeta 基础上增加统计信息。
class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


# ProjectDetail 表示完整项目详情：项目元信息 + 图数据。
class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]


# 创建项目时可以内联带入一些初始 hints。
class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        # 去掉首尾空白，防止用户传入只有空格的字符串。
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


# 创建项目的请求体。
class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    hints: list[CreateHintInline] | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


# 单独创建 hint 的请求体。
class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


# 创建 intent 的请求体。
class CreateIntentRequest(BaseModel):
    # min_length=1 表示 from 列表至少要有一个来源 fact。
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    # worker 可选；如果带上它，表示创建时就顺便认领。
    worker: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


# 普通 heartbeat 请求体，只需要说明是谁在续约。
class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


# 认领项目级 reason lease 的请求体。
class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


# 结束一个 intent 并提交结论 fact 的请求体。
class ConcludeRequest(BaseModel):
    worker: str
    description: str

    @field_validator("worker", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


# 声明整个项目完成的请求体。
class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


# conclude 接口的响应：会同时返回新 fact 和更新后的 intent。
class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


# 更新项目状态时，只允许在 active 和 stopped 之间切换。
class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


# 修改项目标题的请求体。
class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


# 当项目已完成但后来发现判断有误时，用 reopen 恢复项目。
class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


# reopen 接口会返回恢复后的项目，以及新增的纠错 fact 和 intent。
class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent
