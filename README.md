# Marathon2 - GitHub Priority Task Tracker

A serverless GitHub issue prioritization and task tracking system built with **Firebase Cloud Functions (Python v2)**, **Cloud Firestore**, and **Pydantic AI**.

---

## 🚀 Local Development Flow

### 1. Prerequisites
- **Python**: 3.10+
- **Node.js & Firebase CLI**:
  ```bash
  npm install -g firebase-tools
  ```

---

### 2. Environment Setup

Create and populate the local virtual environment:

```bash
# Create virtual environment
python -m venv functions/venv

# Install runtime & development dependencies
# Windows (PowerShell):
.\functions\venv\Scripts\pip install -r functions/requirements.txt
.\functions\venv\Scripts\pip install pyright pytest ruff

# macOS / Linux:
source functions/venv/bin/activate
pip install -r functions/requirements.txt
pip install pyright pytest ruff
```

---

### 3. Local Verification Commands

Use the virtual environment tools (or [Hatch](https://hatch.pypa.io/)):

| Task | Direct Command | Hatch Command |
|---|---|---|
| **Style / Linting** | `ruff check .` | `hatch run lint` |
| **Code Formatting** | `ruff format .` | `hatch run format` |
| **Type Checking** | `pyright` | `hatch run types` |
| **Run Unit Tests** | `python -m unittest discover -s . -p "test_*.py"` | `hatch run test` |
| **Run All Checks** | `ruff check . ; pyright ; python -m unittest discover -s . -p "test_*.py"` | `hatch run all` |

---

### 4. Running Firebase Local Emulators

Start the local emulators for Auth, Firestore, Functions, and Hosting:

```bash
firebase emulators:start
```

- **Web App**: [http://localhost:5000](http://localhost:5000)
- **Emulator UI Suite**: [http://localhost:4000](http://localhost:4000)
- **Auth Emulator**: `http://localhost:9099`
- **Firestore Emulator**: `http://localhost:8080`
- **Functions Emulator**: `http://localhost:5001`

---

## 📁 Repository Structure

```
├── functions/
│   ├── main.py              # Cloud Functions (Callable, HTTP, Task Queue, Firestore Triggers)
│   ├── github_sync.py       # PyGithub issue/comment synchronization & pagination
│   ├── genai_ranker.py      # Pydantic AI task priority ranker
│   ├── task.py              # Task Firestore operations and data model
│   ├── user.py              # User Firestore operations and data model
│   ├── auth_utils.py        # Token validation & provider extraction
│   └── requirements.txt     # Backend dependencies
├── test_backend.py          # Backend & auth integration tests
├── test_github.py           # GitHub sync & PyGithub tests
├── test_task.py             # Task lifecycle & ranking tests
├── pyproject.toml           # Hatch project configuration, Ruff rules, scripts
├── pyrightconfig.json       # Pyright configuration targeting functions/venv
├── AGENTS.md                # Key architecture insights for developers & agents
└── README.md
```

---

## 🚢 Deployment

Deploy functions and Firestore rules to production:

```bash
firebase deploy --only functions,firestore,hosting
```
