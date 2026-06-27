"""Load user-authored tasks from the DB and convert them to CapabilityTask."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import CustomTask
from .eval_suites import CapabilityTask


def _list(s: str | None) -> list:
    try:
        return json.loads(s) if s else []
    except (ValueError, TypeError):
        return []


def _dict(s: str | None) -> dict:
    try:
        return json.loads(s) if s else {}
    except (ValueError, TypeError):
        return {}


def to_capability(ct: CustomTask) -> CapabilityTask:
    return CapabilityTask(
        id=f"custom-{ct.id}", category=ct.category, name=ct.name, prompt=ct.prompt,
        scorer=ct.scorer, system=ct.system, answer=ct.answer, contains=_list(ct.contains_json),
        numeric_answer=ct.numeric_answer, numeric_tol=ct.numeric_tol, choices=_list(ct.choices_json),
        correct=ct.correct, rubric=ct.rubric, entry_point=ct.entry_point, test_code=ct.test_code,
        code_prefix=ct.code_prefix, tools=_list(ct.tools_json), expected_tool=ct.expected_tool,
        expected_args=_dict(ct.expected_args_json), forbid_tool_call=ct.forbid_tool_call,
        max_tokens=ct.max_tokens,
    )


async def load_custom(session: AsyncSession, categories: list[str]) -> list[CapabilityTask]:
    res = await session.execute(select(CustomTask).where(CustomTask.enabled.is_(True)))
    return [to_capability(c) for c in res.scalars().all() if c.category in categories]


async def custom_categories(session: AsyncSession) -> list[str]:
    res = await session.execute(
        select(CustomTask.category).where(CustomTask.enabled.is_(True)).distinct()
    )
    return sorted({c for c in res.scalars().all()})
