"""
Developer UI and Debugging Routes
Provides server-rendered HTML pages using Jinja2 templates for:
1. Main tasks page with ranked tasks and debugging triggers (sync, rerank).
2. Settings page for configuring GitHub access tokens, Gemini API keys, and monitored repositories.
"""

import os

import firebase_admin
import jinja2
from firebase_admin import auth
from firebase_functions import https_fn, options
from flask import Response
from google.cloud import firestore
from google.cloud.firestore import SERVER_TIMESTAMP

from github_sync import start_user_github_sync
from task import enqueue_task_ranking, force_rerank_tasks
from user import User

# Jinja2 Environment for server-side HTML rendering
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATES_DIR), autoescape=jinja2.select_autoescape(["html", "xml"])
)

# Initialize Firebase Admin App if not already initialized
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db: firestore.Client = firestore.Client()


@https_fn.on_request(cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"]))
def render_main_page(req: https_fn.Request) -> Response:
    """
    Renders the static ranked tasks list for developer debugging using Jinja2 templates.
    Authenticates the user via __session cookie, Authorization header, or ?token= param.
    """
    if req.method == "OPTIONS":
        return Response("", status=204)

    token_str = req.cookies.get("__session") or req.args.get("token")
    if not token_str and req.headers.get("Authorization"):
        auth_hdr = req.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            token_str = auth_hdr.split("Bearer ", 1)[1].strip()

    decoded_token = None
    if token_str:
        try:
            decoded_token = auth.verify_id_token(token_str)
        except Exception:
            pass

    template = jinja_env.get_template("main.html")

    if not decoded_token:
        html = template.render(is_authenticated=False, user=None, tasks=[])
        return Response(html, status=200, headers={"Content-Type": "text/html; charset=utf-8"})

    uid = str(decoded_token.get("uid") or "")
    # Fetch user details
    user_doc = db.collection("users").document(uid).get()
    user_data = user_doc.to_dict() if user_doc.exists else {}
    if not user_data:
        user_data = {"uid": uid, "email": decoded_token.get("email"), "display_name": decoded_token.get("name")}

    if req.method == "POST":
        action = req.form.get("action") if req.form else None
        if action == "sync":
            user_model = User.model_validate({**user_data, "uid": uid})
            if user_model.github_access_token:
                start_user_github_sync(user=user_model, db=db)
        else:
            force_rerank_tasks(uid=uid, db=db)

    # Fetch tasks from users/{uid}/tasks
    tasks_col = db.collection("users").document(uid).collection("tasks")
    tasks_docs = tasks_col.stream()
    tasks_list: list[dict[str, object]] = []
    unranked_task_ids: list[str] = []
    for doc in tasks_docs:
        t_data = doc.to_dict() or {}
        tasks_list.append(t_data)
        if t_data.get("priority_needs_updated"):
            raw_id = doc.id
            if raw_id:
                unranked_task_ids.append(str(raw_id))

    # Auto-dispatch ranking for any tasks that need ranking
    for unranked_id in unranked_task_ids:
        enqueue_task_ranking(uid=uid, task_id=unranked_id, db=db)

    # Sort descending from highest to lowest priority
    tasks_list.sort(key=lambda t: float(t.get("priority") or 0.0), reverse=True)  # type: ignore

    html = template.render(is_authenticated=True, user=user_data, tasks=tasks_list)
    return Response(html, status=200, headers={"Content-Type": "text/html; charset=utf-8"})


# ============================================================================
# Jinja2 Server-Rendered Settings Page (Simple CRUD)
# ============================================================================


@https_fn.on_request(cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"]))
def render_settings_page(req: https_fn.Request) -> Response:
    """
    Simple server-side CRUD settings page for configuring GitHub access token and monitored repos.
    - GET /settings: Renders settings form.
    - POST /settings: Updates User in Firestore and displays success message.
    """
    if req.method == "OPTIONS":
        return Response("", status=204)

    token_str = req.cookies.get("__session") or req.args.get("token")
    if not token_str and req.headers.get("Authorization"):
        auth_hdr = req.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            token_str = auth_hdr.split("Bearer ", 1)[1].strip()

    decoded_token = None
    if token_str:
        try:
            decoded_token = auth.verify_id_token(token_str)
        except Exception:
            pass

    if not decoded_token:
        # Redirect unauthenticated users to / for login
        return Response("", status=302, headers={"Location": "/"})

    uid = str(decoded_token.get("uid") or "")
    user_ref = db.collection("users").document(uid)
    template = jinja_env.get_template("settings.html")

    if req.method == "POST":
        new_token = (req.form.get("github_access_token") or "").strip()
        new_gemini_key = (req.form.get("gemini_api_key") or "").strip()
        raw_repos = (req.form.get("monitored_repos") or "").strip()
        repo_names = [r.strip() for r in raw_repos.split(",") if r.strip()]

        doc_snap = user_ref.get()
        current_data = doc_snap.to_dict() or {}
        existing_repos_raw = current_data.get("monitored_repos")
        existing_repos: dict[str, object] = dict(existing_repos_raw) if isinstance(existing_repos_raw, dict) else {}
        updated_repos: dict[str, object] = {repo: existing_repos.get(repo) for repo in repo_names}

        update_data: dict[str, object] = {
            "github_access_token": new_token if new_token else None,
            "gemini_api_key": new_gemini_key if new_gemini_key else None,
            "monitored_repos": updated_repos,
            "updated_at": SERVER_TIMESTAMP,
        }

        user_ref.set(update_data, merge=True)

        doc_snap = user_ref.get()
        user_data = doc_snap.to_dict() if doc_snap.exists else {"uid": uid}

        html = template.render(user=user_data, saved=True)
        return Response(html, status=200, headers={"Content-Type": "text/html; charset=utf-8"})

    # GET request: render form with current values
    doc_snap = user_ref.get()
    user_data = (
        doc_snap.to_dict()
        if doc_snap.exists
        else {"uid": uid, "email": decoded_token.get("email"), "display_name": decoded_token.get("name")}
    )

    html = template.render(user=user_data, saved=False)
    return Response(html, status=200, headers={"Content-Type": "text/html; charset=utf-8"})
