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
│   ├── dev.py               # Jinja2 server-rendered dev debug and settings UI
│   ├── github_sync.py       # PyGithub client, in-memory issue fetcher, pagination sync
│   ├── genai_ranker.py      # Pydantic AI task priority ranker (synchronous run_sync)
│   ├── task.py              # Task Pydantic model & Firestore task operations
│   ├── user.py              # User Pydantic model & auth token extraction
│   └── auth_utils.py        # Token verification & OAuth provider parsing
├── test_backend.py          # Auth & callable endpoint tests
├── test_github.py           # GitHub sync & pagination tests
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
- **Step 2**: Issue pages directly create or update Tasks (`users/{uid}/tasks/task_{doc_id}`) in Firestore. Intermediate issue and comment documents are **NOT** stored in Firestore.
- **Step 3**: Firestore trigger `on_task_written` detects `priority_needs_updated == True` and enqueues ranking via `rank_user_tasks`.
- **Step 4**: `update_task_priority` fetches real-time issue details and comments into memory on-demand via PyGithub (`fetch_issue_in_memory`) and runs the Pydantic AI ranker (`run_ranker`).
- **Step 5**: The ranked priority is written back to the Task document in Firestore.

### 3. AI Ranker (Pydantic AI)
- Uses **Pydantic AI** (`pydantic_ai.Agent`) with structured output model `TaskPriorityOutput(priority, reasoning)`.
- Uses synchronous execution (`agent.run_sync(...)`) with exponential backoff on 429/quota errors to guarantee compatibility with serverless worker lifecycles.

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
