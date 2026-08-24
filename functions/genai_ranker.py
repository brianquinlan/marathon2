"""
GenAI Task Ranking Module using Pydantic AI and Google Gemini Flash.
Evaluates individual tasks with GitHub issue metadata, comments, and username mentions.
"""

from __future__ import annotations

import json
import os
from typing import Protocol, TypeVar

import google.genai as genai
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider


class TaskPriorityOutput(BaseModel):
    priority: float = Field(description="A priority score between 0.0 (lowest) and 1.0 (highest) indicating urgency.")
    reasoning: str | None = Field(
        default=None, description="Brief explanation of why this priority score was assigned."
    )


class IssuePayload(BaseModel):
    """
    Structured container holding the raw GitHub Issue and Comments JSON payloads.
    Directly passable to Pydantic AI / Gemini for ranking without manual field extraction.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    issue: dict[str, object] = Field(default_factory=dict)
    comments: list[dict[str, object]] = Field(default_factory=list)


class TaskProtocol(Protocol):
    @property
    def doc_id(self) -> str: ...

    priority: float
    priority_needs_updated: bool


TTask = TypeVar("TTask", bound=TaskProtocol)

# ============================================================================
# Ranker Engine: Pydantic AI & Gemini Flash
# ============================================================================

DEFAULT_SYSTEM_PROMPT = """You are my executive engineering assistant. Your role is to rank GitHub issues and pull requests (PRs) so that I focus on items that maximize my development and review efficiency.

The most important thing to consider when deciding an issue's priority is how actionable it is. If an issue is not actionable, there is no point in considering it.

An issue is actionable if:
- I am mentioned and have not responded.
- I am assigned a PR and have not provided review feedback. Or if I have provided review feedback and it has been addressed.

An issue is not actionable if:
- It has the "needs-info" or similar label.
- I am waiting for another party to take action, such as respond to a question or address code review comments.
- In general, an issue is not actionable if I was the last person to act.

PRs are higher priority than other issues.

Issues created or commented-on by my usual collaborators are more important than issues created by strangers. Unless the  collaborators indicate that the issue is not important.

Issues with recent activity are higher priority than dormant issues."""


def create_pydantic_ai_agent(
    api_key: str | None = None, system_prompt: str | None = None
) -> Agent[None, TaskPriorityOutput]:
    """
    Creates an ephemeral Pydantic AI Agent instance configured with the Google Gemini model.
    No client or agent state is cached across task invocations,
    preventing any cross-thread asyncio client or event loop contention.
    """
    effective_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "placeholder"
    prompt_str = system_prompt if system_prompt is not None else DEFAULT_SYSTEM_PROMPT

    client = genai.Client(api_key=effective_key)
    provider = GoogleProvider(client=client)
    model = GoogleModel("gemini-3.7-flash", provider=provider)
    return Agent(model=model, output_type=TaskPriorityOutput, system_prompt=prompt_str)


def get_pydantic_ai_agent(
    api_key: str | None = None, system_prompt: str | None = None
) -> Agent[None, TaskPriorityOutput]:
    """
    Backwards-compatible alias for creating an ephemeral Pydantic AI Agent.
    """
    return create_pydantic_ai_agent(api_key=api_key, system_prompt=system_prompt)


def run_ranker(
    task: TTask,
    issue: BaseModel | dict[str, object] | None = None,
    github_username: str | None = None,
    gemini_api_key: str | None = None,
    agent: Agent[None, TaskPriorityOutput] | None = None,
) -> TTask:
    """
    Ranker engine that computes priority for a single task using Pydantic AI
    and the latest Gemini Flash model ('gemini-3.7-flash').
    Accepts full structured issue and comments JSON, the current user's GitHub username,
    and the user's Gemini API key.
    """
    # Serialize issue and comment data to JSON directly for the LLM
    if isinstance(issue, BaseModel):
        issue_json_str = issue.model_dump_json(indent=2)
    elif isinstance(issue, dict):
        issue_json_str = json.dumps(issue, indent=2, default=str)
    else:
        issue_json_str = "{}"

    user_info_str = f"@{github_username}" if github_username else "Unknown (not specified)"

    prompt_text = f"""
Current User GitHub Username: {user_info_str}

GitHub Issue & Comments Data (JSON):
{issue_json_str}

Please evaluate the priority for the user {user_info_str} based on your system instructions and assign a priority score between 0.0 and 1.0.
""".strip()

    active_agent = agent or create_pydantic_ai_agent(api_key=gemini_api_key, system_prompt=DEFAULT_SYSTEM_PROMPT)

    result = active_agent.run_sync(user_prompt=prompt_text)

    computed_priority = 0.5
    output_obj = result.output
    if output_obj is not None:
        if isinstance(output_obj, TaskPriorityOutput):
            computed_priority = output_obj.priority
        elif isinstance(output_obj, dict):
            computed_priority = float(output_obj.get("priority", 0.5))

    computed_priority = max(0.0, min(1.0, float(computed_priority)))
    task.priority = computed_priority
    task.priority_needs_updated = False
    return task
