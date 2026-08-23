"""
GenAI Task Ranking Module using Pydantic AI and Google Gemini Flash.
Evaluates individual tasks with GitHub issue metadata, comments, and username mentions.
"""

from __future__ import annotations
import os
import threading
import logging
from typing import Dict, List, Optional, Protocol, Tuple, TypeVar
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

logger = logging.getLogger(__name__)


class TaskPriorityOutput(BaseModel):
    priority: float = Field(
        description="A priority score between 0.0 (lowest) and 1.0 (highest) indicating urgency."
    )
    reasoning: Optional[str] = Field(
        default=None,
        description="Brief explanation of why this priority score was assigned."
    )


class TaskProtocol(Protocol):
    @property
    def doc_id(self) -> str: ...
    priority: float
    priority_needs_updated: bool
    github_issue_title: Optional[str]


TTask = TypeVar("TTask", bound=TaskProtocol)

# ============================================================================
# Ranker Engine: Pydantic AI & Gemini Flash
# ============================================================================

_pydantic_ai_agents: Dict[Tuple[str, str], Agent[None, TaskPriorityOutput]] = {}
_agent_lock = threading.Lock()


def get_pydantic_ai_agent(
    api_key: Optional[str] = None,
    system_prompt: Optional[str] = None
) -> Agent[None, TaskPriorityOutput]:
    """
    Lazily initializes and caches Pydantic AI Agent instances configured with Google Gemini model.
    """
    effective_key = api_key or os.environ.get("GEMINI_API_KEY") or ""
    prompt_str = system_prompt or ""
    cache_key = (effective_key, prompt_str)

    with _agent_lock:
        if cache_key not in _pydantic_ai_agents:
            provider = GoogleProvider(api_key=effective_key)
            model = GoogleModel("gemini-3.7-flash", provider=provider)
            _pydantic_ai_agents[cache_key] = Agent(
                model=model,
                output_type=TaskPriorityOutput,
                system_prompt=system_prompt or ""
            )
        return _pydantic_ai_agents[cache_key]


def run_ranker(
    task: TTask,
    issue: Optional[Dict[str, object]] = None,
    github_username: Optional[str] = None,
    gemini_api_key: Optional[str] = None,
    agent: Optional[Agent[None, TaskPriorityOutput]] = None,
    ai: Optional[object] = None  # Backwards compatibility alias for mock injection
) -> TTask:
    """
    Ranker engine that computes priority for a single task using Pydantic AI
    and the latest Gemini Flash model ('gemini-3.7-flash').
    Takes the GitHub issue metadata, comments, the current user's GitHub username,
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

    issue_data = issue or {}
    raw_comments = issue_data.get("comments")
    comments: List[object] = raw_comments if isinstance(raw_comments, list) else []

    # Construct prompt context
    user_info_str = f"@{github_username}" if github_username else "Unknown (not specified)"

    comments_text_list: List[str] = []
    for idx, c in enumerate(comments, 1):
        if isinstance(c, dict):
            c_author = c.get("user_login") or "unknown"
            c_body = c.get("body") or ""
            c_time = c.get("created_at") or ""
            comments_text_list.append(f"Comment {idx} by @{c_author} ({c_time}):\n{c_body}")
        elif isinstance(c, str):
            comments_text_list.append(f"Comment {idx}:\n{c}")

    comments_formatted = "\n\n".join(comments_text_list) if comments_text_list else "No comments on this issue."

    issue_title = issue_data.get("title") or getattr(task, "github_issue_title", None) or "Untitled Issue"
    issue_body = issue_data.get("body") or "No issue description provided."
    issue_state = issue_data.get("state") or "open"
    issue_author = issue_data.get("user") or "unknown"
    issue_labels = issue_data.get("labels") or []
    issue_assignees = issue_data.get("assignees") or []

    logger.info(
        f"[RANKER] Issue details for {task.doc_id}: title='{issue_title}', state='{issue_state}', "
        f"author='{issue_author}', comments_count={len(comments)}"
    )

    system_instruction = (
        "You are an expert AI developer productivity assistant that assigns a priority score "
        "between 0.0 (lowest) and 1.0 (highest) to GitHub issues/tasks for a software engineer.\n"
        "Evaluation Criteria:\n"
        "- Direct user mentions or requests for action: If the current user is @mentioned in the issue body or comments, or explicitly asked for input/review/action, assign HIGH priority (0.80 - 1.00).\n"
        "- Directly assigned or blocker bugs: High priority bugs, regressions, or issues assigned to the user should be rated 0.70 - 0.90.\n"
        "- Active discussions or questions: Active ongoing discussions where the user is involved or monitored repo issues should be rated 0.40 - 0.70.\n"
        "- Informational / low urgency: Low impact feature requests, minor discussions, or items not requiring immediate attention should be rated 0.10 - 0.40.\n"
        "- Closed or resolved: 0.00 - 0.10.\n"
        "Always return a structured response conforming to the TaskPriorityOutput schema with a numerical priority float."
    )

    prompt_text = f"""
Current User GitHub Username: {user_info_str}

GitHub Issue Details:
- Title: {issue_title}
- State: {issue_state}
- Author: @{issue_author}
- Assignees: {issue_assignees}
- Labels: {issue_labels}

Issue Description:
{issue_body}

Chronological Comments ({len(comments)} comments):
{comments_formatted}

Please evaluate the priority for the user {user_info_str} and assign a priority score between 0.0 and 1.0.
""".strip()

    try:
        active_agent = agent or (ai if hasattr(ai, "run_sync") else None) or get_pydantic_ai_agent(api_key=gemini_api_key, system_prompt=system_instruction)
        logger.info(f"[RANKER] Executing synchronous run_sync with Pydantic AI for task {task.doc_id}...")

        result = getattr(active_agent, "run_sync")(user_prompt=prompt_text)
        logger.info(f"[RANKER] Pydantic AI call succeeded for {task.doc_id}. Output: {getattr(result, 'output', getattr(result, 'data', None))}")

        computed_priority = 0.5
        output_obj = getattr(result, "output", getattr(result, "data", None))
        if output_obj is not None:
            if isinstance(output_obj, TaskPriorityOutput):
                computed_priority = output_obj.priority
                logger.info(f"[RANKER] Parsed TaskPriorityOutput: priority={computed_priority}, reasoning='{output_obj.reasoning}'")
            elif isinstance(output_obj, dict):
                computed_priority = float(output_obj.get("priority", 0.5))
                logger.info(f"[RANKER] Parsed dict output: priority={computed_priority}")
            elif hasattr(output_obj, "priority"):
                computed_priority = float(output_obj.priority)
                logger.info(f"[RANKER] Parsed object output: priority={computed_priority}")

        computed_priority = max(0.0, min(1.0, float(computed_priority)))
        task.priority = computed_priority
        logger.info(f"[RANKER] Successfully assigned priority {task.priority:.2f} to task {task.doc_id}")

    except Exception as e:
        logger.error(
            f"[RANKER] Error running Pydantic AI ranker for task {task.doc_id}: {e}. Preserving existing priority {task.priority}.",
            exc_info=True
        )

    task.priority_needs_updated = False
    return task
