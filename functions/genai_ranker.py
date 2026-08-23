"""
GenAI Task Ranking Module using Pydantic AI and Google Gemini Flash.
Evaluates individual tasks with GitHub issue metadata, comments, and username mentions.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from typing import Protocol, TypeVar

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

logger = logging.getLogger(__name__)


class TaskPriorityOutput(BaseModel):
    priority: float = Field(description="A priority score between 0.0 (lowest) and 1.0 (highest) indicating urgency.")
    reasoning: str | None = Field(
        default=None, description="Brief explanation of why this priority score was assigned."
    )


class TaskProtocol(Protocol):
    @property
    def doc_id(self) -> str: ...

    priority: float
    priority_needs_updated: bool
    github_issue_title: str | None
    github_issue_upvotes: int


TTask = TypeVar("TTask", bound=TaskProtocol)

# ============================================================================
# Ranker Engine: Pydantic AI & Gemini Flash
# ============================================================================

_pydantic_ai_agents: dict[tuple[str, str], Agent[None, TaskPriorityOutput]] = {}
_agent_lock = threading.Lock()


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


def get_pydantic_ai_agent(
    api_key: str | None = None, system_prompt: str | None = None
) -> Agent[None, TaskPriorityOutput]:
    """
    Lazily initializes and caches Pydantic AI Agent instances configured with Google Gemini model.
    """
    effective_key = api_key or os.environ.get("GEMINI_API_KEY") or ""
    prompt_str = system_prompt if system_prompt is not None else DEFAULT_SYSTEM_PROMPT
    cache_key = (effective_key, prompt_str)

    with _agent_lock:
        if cache_key not in _pydantic_ai_agents:
            provider = GoogleProvider(api_key=effective_key)
            model = GoogleModel("gemini-3.7-flash", provider=provider)
            _pydantic_ai_agents[cache_key] = Agent(
                model=model, output_type=TaskPriorityOutput, system_prompt=prompt_str
            )
        return _pydantic_ai_agents[cache_key]


def run_ranker(
    task: TTask,
    issue: BaseModel | dict[str, object] | None = None,
    github_username: str | None = None,
    gemini_api_key: str | None = None,
    agent: Agent[None, TaskPriorityOutput] | None = None,
    ai: object | None = None,  # Backwards compatibility alias for mock injection
) -> TTask:
    """
    Ranker engine that computes priority for a single task using Pydantic AI
    and the latest Gemini Flash model ('gemini-3.7-flash').
    Accepts full structured issue and comments JSON, the current user's GitHub username,
    and the user's Gemini API key.
    """
    logger.info(
        f"[RANKER] Starting task ranking for doc={task.doc_id}: "
        f"github_username={github_username}, has_gemini_key={bool(gemini_api_key)}, "
        f"current_priority={task.priority}, current_needs_updated={task.priority_needs_updated}"
    )

    if not gemini_api_key and not os.environ.get("GEMINI_API_KEY"):
        logger.warning(
            f"[RANKER] No Gemini API key provided for task {task.doc_id}. "
            f"Please set a Gemini API key in user settings or GEMINI_API_KEY in environment."
        )

    # Serialize issue and comment data to JSON directly for the LLM
    if isinstance(issue, BaseModel):
        issue_json_str = issue.model_dump_json(indent=2)
    elif isinstance(issue, dict):
        issue_json_str = json.dumps(issue, indent=2, default=str)
    else:
        issue_json_str = "{}"

    user_info_str = f"@{github_username}" if github_username else "Unknown (not specified)"

    logger.info(f"[RANKER] Issue payload for {task.doc_id}: JSON length={len(issue_json_str)} characters")

    prompt_text = f"""
Current User GitHub Username: {user_info_str}

GitHub Issue & Comments Data (JSON):
{issue_json_str}

Please evaluate the priority for the user {user_info_str} based on your system instructions and assign a priority score between 0.0 and 1.0.
""".strip()

    active_agent = agent or get_pydantic_ai_agent(api_key=gemini_api_key, system_prompt=DEFAULT_SYSTEM_PROMPT)
    logger.info(f"[RANKER] Executing synchronous run_sync with Pydantic AI for task {task.doc_id}...")

    max_attempts = 4
    result = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = active_agent.run_sync(user_prompt=prompt_text)
            break
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = (
                "429" in err_str
                or "resource_exhausted" in err_str
                or "resourceexhausted" in err_str
                or "quota" in err_str
                or "too many requests" in err_str
                or "rate" in err_str
            )
            if is_rate_limit and attempt < max_attempts:
                sleep_secs = (2 ** (attempt - 1)) + random.uniform(0.5, 1.5)
                logger.warning(
                    f"[RANKER] Rate limit hit for {task.doc_id} (attempt {attempt}/{max_attempts}). "
                    f"Retrying in {sleep_secs:.2f}s: {e}"
                )
                time.sleep(sleep_secs)
            else:
                raise

    if result is None:
        raise RuntimeError(f"[RANKER] Failed to get response for task {task.doc_id} after {max_attempts} attempts.")

    logger.info(f"[RANKER] Pydantic AI call succeeded for {task.doc_id}. Output: {result.output}")

    computed_priority = 0.5
    output_obj = result.output
    if output_obj is not None:
        if isinstance(output_obj, TaskPriorityOutput):
            computed_priority = output_obj.priority
            logger.info(
                f"[RANKER] Parsed TaskPriorityOutput: priority={computed_priority}, reasoning='{output_obj.reasoning}'"
            )
        elif isinstance(output_obj, dict):
            computed_priority = float(output_obj.get("priority", 0.5))
            logger.info(f"[RANKER] Parsed dict output: priority={computed_priority}")

    computed_priority = max(0.0, min(1.0, float(computed_priority)))
    task.priority = computed_priority
    logger.info(f"[RANKER] Successfully assigned priority {task.priority:.2f} to task {task.doc_id}")

    task.priority_needs_updated = False
    return task
