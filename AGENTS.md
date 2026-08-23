# AGENTS.md - Marathon2 Architecture & Development Insights

Essential context, architectural rules, and operational workflows for AI agents and developers working on Marathon2.

---

## 🔒 Critical Policies

1. **Git Policy**: **NEVER** run `git commit` or `git push` without explicit user permission.
2. **Type Safety & Linting**: Maintain zero errors on `pyright` and `ruff check .`. Run `hatch run all` before concluding tasks.

---

## 🏗️ Architecture & Key Components

```
marathon2/
├── functions/
│   ├── main.py              # Cloud Functions (Callable, HTTP, Task Queue, Firestore Triggers)
│   ├── github_sync.py       # PyGithub client, issue/comment adaptors, pagination sync
│   ├── genai_ranker.py      # Pydantic AI task priority ranker (synchronous run_sync)
│   ├── task.py              # Task Pydantic model & Firestore task operations
│   ├── user.py              # User Pydantic model & auth token extraction
│   └── auth_utils.py        # Token verification & OAuth provider parsing
├── test_backend.py          # Auth & callable endpoint tests
├── test_github.py           # GitHub sync & PyGithub adaptor tests
├── test_task.py             # Task lifecycle & Pydantic AI ranking tests
├── pyproject.toml           # Hatch project configuration, Ruff settings, test scripts
└── pyrightconfig.json       # Pyright configuration using functions/venv
```

---

## 💡 Core Insights & Gotchas

### 1. PyGithub Module Disambiguation
- **PyPI `PyGithub`** installs the top-level Python module `github` (`import github`).
- The internal service file is named [`functions/github_sync.py`](file:///c:/Users/brian/marathon2/functions/github_sync.py) (NOT `github.py`) to prevent Python `sys.path` collisions where the local file shadows the library.

### 2. Task & Issue Lifecycle
- **Step 1**: `start_user_github_sync` schedules initial Task Queue jobs (`sync_github_issues_page`) for assigned, mentioned, created, and monitored repo issues.
- **Step 2**: Issue pages are processed in Firestore (`users/{uid}/issues/{doc_id}`).
- **Step 3**: Comment pages are chained via `sync_issue_comments_page`.
- **Step 4**: Tasks (`users/{uid}/tasks/task_{doc_id}`) are created **ONLY** after all comments are fully imported (or immediately if 0 comments exist).
- **Step 5**: Firestore trigger `on_task_written` detects `priority_needs_updated == True` and enqueues ranking via `rank_user_tasks`.

### 3. AI Ranker (Pydantic AI)
- Uses **Pydantic AI** (`pydantic_ai.Agent`) with structured output model `TaskPriorityOutput(priority, reasoning)`.
- Uses synchronous execution (`agent.run_sync(...)`) to guarantee compatibility with serverless worker lifecycles without event loop conflicts.

### 4. Direct Property Access Over Defensive Introspection
- Do **NOT** use `getattr(doc_snap, "exists")`, `hasattr(doc_snap, "to_dict")`, or `getattr(issue, "comments_url")`.
- Use direct, typed access (`doc_snap.exists`, `doc_snap.to_dict()`, `issue.comments_url`, `user.login`).

---

## 🛠️ Local Development & Validation Commands

```powershell
# Run style checking and formatting
ruff check .
ruff format .

# Run static type checking (must be 0 errors)
pyright

# Run test suite
python -m unittest discover -s . -p "test_*.py"

# Or run all in one command via Hatch:
hatch run all
```
