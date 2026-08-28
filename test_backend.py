import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from flask import Response

# Ensure functions module is in sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "functions"))

from firebase_functions import https_fn, tasks_fn

import dev
import main
from user import User


def get_callable_handler(func):
    """Helper to extract the original callable handler function from Firebase decorators."""
    target = getattr(func, "__wrapped__", func)
    if hasattr(target, "__closure__") and target.__closure__:
        for cell in target.__closure__:
            if callable(cell.cell_contents) and not getattr(cell.cell_contents, "__closure__", None):
                return cell.cell_contents
    if hasattr(func, "__closure__") and func.__closure__:
        for cell in func.__closure__:
            if callable(cell.cell_contents) and not getattr(cell.cell_contents, "__closure__", None):
                return cell.cell_contents
    return target


class TestUserModel(unittest.TestCase):
    def test_user_dataclass_defaults_and_fields(self):
        dt_sync = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)
        user = User(
            github_access_token="gho_test_token_123",
            github_username="brianquinlan",
            gemini_api_key="AIzaSyTestKey123",
            last_assigned_sync=dt_sync,
            monitored_repos={"brianquinlan/marathon2": None, "google/jax": None},
            uid="user_abc_123",
        )
        self.assertEqual(user.github_access_token, "gho_test_token_123")
        self.assertEqual(user.github_username, "brianquinlan")
        self.assertEqual(user.gemini_api_key, "AIzaSyTestKey123")
        self.assertEqual(user.last_assigned_sync, dt_sync)
        self.assertEqual(user.monitored_repos, {"brianquinlan/marathon2": None, "google/jax": None})
        self.assertEqual(user.uid, "user_abc_123")

        # Test Pydantic JSON serialization
        json_str = user.model_dump_json()
        self.assertIn('"uid":"user_abc_123"', json_str.replace(" ", ""))
        self.assertIn('"github_username":"brianquinlan"', json_str.replace(" ", ""))
        self.assertIn('"github_access_token":"gho_test_token_123"', json_str.replace(" ", ""))
        dumped = user.model_dump()
        self.assertEqual(dumped["uid"], "user_abc_123")
        self.assertEqual(dumped["github_username"], "brianquinlan")

    def test_user_model_dump_and_model_validate(self):
        dt_sync = datetime(2026, 8, 22, 10, 30, tzinfo=timezone.utc)
        user = User(
            uid="user_999",
            github_access_token="gho_xyz",
            github_username="octocat",
            gemini_api_key="AIzaSyOctoKey",
            last_assigned_sync=dt_sync,
            monitored_repos={"owner/repo1": None},
        )
        data = user.model_dump()
        self.assertEqual(data["uid"], "user_999")
        self.assertEqual(data["github_access_token"], "gho_xyz")
        self.assertEqual(data["monitored_repos"], {"owner/repo1": None})

        # Reconstruct from dict
        reconstructed = User.model_validate(data)
        self.assertEqual(reconstructed.uid, user.uid)
        self.assertEqual(reconstructed.github_access_token, "gho_xyz")
        self.assertEqual(reconstructed.monitored_repos, {"owner/repo1": None})

    def test_user_extra_fields_gracefully_ignored(self):
        # Ensure extra fields from legacy Firestore documents do not cause validation errors
        user = User.model_validate(
            {
                "uid": "user_legacy",
                "email": "dev@legacy.com",
                "custom_data": {"legacy_key": "val"},
                "github_access_token": "ghp_tok",
            }
        )
        self.assertEqual(user.uid, "user_legacy")
        self.assertEqual(user.github_access_token, "ghp_tok")
        self.assertFalse(hasattr(user, "email"))


class TestCallableFunctionLogic(unittest.TestCase):
    @patch("main.force_rerank_tasks")
    @patch("main.db")
    def test_force_rerank_all_tasks_callable(self, mock_db, mock_force_fn):
        handler = get_callable_handler(main.force_rerank_all_tasks)
        mock_force_fn.return_value = {
            "status": "enqueued",
            "marked_count": 5,
            "message": "Marked 5 tasks for rerank and enqueued ranking task.",
        }

        mock_req = MagicMock(spec=https_fn.CallableRequest)
        mock_req.auth = MagicMock()
        mock_req.auth.uid = "user_rank_002"

        result = handler(mock_req)
        assert isinstance(result, dict)
        self.assertEqual(result["status"], "enqueued")
        mock_force_fn.assert_called_once_with(uid="user_rank_002", db=mock_db)


class TestTaskQueueSyncHandlers(unittest.TestCase):
    @patch("github_sync.enqueue_issue_page_sync")
    @patch("github_sync.process_and_save_issue_page")
    @patch("github_sync.fetch_single_issue_page")
    @patch("main.db")
    def test_sync_github_issues_page_handler(self, mock_db, mock_fetch_page, mock_save_page, mock_enqueue_issue):
        handler = get_callable_handler(main.sync_github_issues_page)

        mock_doc_ref = MagicMock()
        mock_doc_snap = MagicMock()
        mock_doc_snap.exists = True
        mock_doc_snap.to_dict.return_value = {"uid": "user_q_1", "github_access_token": "gho_token_q1"}
        mock_doc_ref.get.return_value = mock_doc_snap
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        mock_mock_issue = MagicMock()
        mock_fetch_page.return_value = ([mock_mock_issue], True)

        mock_req = MagicMock(spec=tasks_fn.CallableRequest)
        mock_req.data = {"uid": "user_q_1", "filter_name": "assigned", "page": 0}

        result = handler(mock_req)
        self.assertIsNone(result)
        mock_save_page.assert_called_once()
        mock_enqueue_issue.assert_called_once()


class TestJinjaMainPageRendering(unittest.TestCase):
    def test_render_main_page_unauthenticated(self):
        handler = get_callable_handler(dev.render_main_page)
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.cookies = {}
        mock_req.args = {}
        mock_req.headers = {}

        resp = handler(mock_req)
        assert isinstance(resp, Response)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", str(resp.headers.get("Content-Type")))
        html = resp.get_data(as_text=True)
        self.assertIn("Login", html)
        self.assertIn("Sign in with Google", html)

    @patch("dev.auth.verify_id_token")
    @patch("dev.db")
    def test_render_main_page_authenticated_renders_ranked_tasks(self, mock_db, mock_verify):
        mock_verify.return_value = {"uid": "user_jinja_1", "email": "tester@example.com", "name": "Tester Dev"}

        # Mock user document
        mock_user_doc = MagicMock()
        mock_user_snap = MagicMock()
        mock_user_snap.exists = True
        mock_user_snap.to_dict.return_value = {"display_name": "Tester Dev", "email": "tester@example.com"}
        mock_user_doc.get.return_value = mock_user_snap

        # Mock tasks collection
        mock_tasks_col = MagicMock()
        mock_task1 = MagicMock()
        mock_task1.to_dict.return_value = {
            "github_issue_title": "Lower Priority Task",
            "priority": 0.30,
            "github_issue_url": "https://github.com/org/repo/issues/1",
            "priority_needs_updated": False,
        }
        mock_task2 = MagicMock()
        mock_task2.to_dict.return_value = {
            "github_issue_title": "Top Priority Task",
            "priority": 0.95,
            "github_issue_url": "https://github.com/org/repo/issues/2",
            "priority_needs_updated": True,
        }
        mock_tasks_col.stream.return_value = [mock_task1, mock_task2]

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.collection.return_value = mock_tasks_col

        handler = get_callable_handler(dev.render_main_page)
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.cookies = {"__session": "mock_id_token_123"}
        mock_req.args = {}
        mock_req.headers = {}

        resp = handler(mock_req)
        assert isinstance(resp, Response)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", str(resp.headers.get("Content-Type")))
        html = resp.get_data(as_text=True)

        # Verify static ranked task order
        self.assertIn("Tasks (2)", html)
        self.assertIn("Top Priority Task", html)
        self.assertIn("Lower Priority Task", html)
        self.assertIn("0.95", html)
        self.assertIn("0.30", html)
        self.assertIn("Needs Rerank", html)
        self.assertIn("Ranked", html)
        self.assertIn("/settings", html)

        # Check Top Priority comes first in rendered HTML
        idx_top = html.find("Top Priority Task")
        idx_lower = html.find("Lower Priority Task")
        self.assertTrue(idx_top < idx_lower, "Top priority task should appear before lower priority task")

    @patch("dev.start_user_github_sync")
    @patch("dev.auth.verify_id_token")
    @patch("dev.db")
    def test_render_main_page_post_sync(self, mock_db, mock_verify, mock_sync):
        mock_verify.return_value = {"uid": "user_sync_post", "email": "sync@example.com"}
        mock_user_doc = MagicMock()
        mock_user_snap = MagicMock()
        mock_user_snap.exists = True
        mock_user_snap.to_dict.return_value = {
            "github_access_token": "ghp_valid_token",
            "monitored_repos": {},
        }
        mock_user_doc.get.return_value = mock_user_snap
        mock_tasks_col = MagicMock()
        mock_tasks_col.stream.return_value = []

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.collection.return_value = mock_tasks_col

        handler = get_callable_handler(dev.render_main_page)
        mock_req = MagicMock()
        mock_req.method = "POST"
        mock_req.cookies = {"__session": "mock_id_token"}
        mock_req.args = {}
        mock_req.headers = {}
        mock_req.form = {"action": "sync"}

        resp = handler(mock_req)
        assert isinstance(resp, Response)
        self.assertEqual(resp.status_code, 200)
        mock_sync.assert_called_once()

    @patch("dev.force_rerank_tasks")
    @patch("dev.auth.verify_id_token")
    @patch("dev.db")
    def test_render_main_page_post_rerank(self, mock_db, mock_verify, mock_rerank):
        mock_verify.return_value = {"uid": "user_rerank_post", "email": "rerank@example.com"}
        mock_user_doc = MagicMock()
        mock_user_snap = MagicMock()
        mock_user_snap.exists = True
        mock_user_snap.to_dict.return_value = {"email": "rerank@example.com"}
        mock_user_doc.get.return_value = mock_user_snap
        mock_tasks_col = MagicMock()
        mock_tasks_col.stream.return_value = []

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.collection.return_value = mock_tasks_col

        handler = get_callable_handler(dev.render_main_page)
        mock_req = MagicMock()
        mock_req.method = "POST"
        mock_req.cookies = {"__session": "mock_id_token"}
        mock_req.args = {}
        mock_req.headers = {}
        mock_req.form = {"action": "rerank"}

        resp = handler(mock_req)
        assert isinstance(resp, Response)
        self.assertEqual(resp.status_code, 200)
        mock_rerank.assert_called_once_with(uid="user_rerank_post", db=mock_db)


class TestJinjaSettingsPageCRUD(unittest.TestCase):
    def test_render_settings_page_unauthenticated_redirects(self):
        handler = get_callable_handler(dev.render_settings_page)
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.cookies = {}
        mock_req.args = {}
        mock_req.headers = {}

        resp = handler(mock_req)
        assert isinstance(resp, Response)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("Location"), "/")

    @patch("dev.auth.verify_id_token")
    @patch("dev.db")
    def test_render_settings_page_get_authenticated(self, mock_db, mock_verify):
        mock_verify.return_value = {"uid": "user_settings_1", "email": "dev@test.com"}

        mock_user_doc = MagicMock()
        mock_user_snap = MagicMock()
        mock_user_snap.exists = True
        mock_user_snap.to_dict.return_value = {
            "github_access_token": "ghp_secret12345678",
            "gemini_api_key": "AIzaSySecretGeminiKey",
            "monitored_repos": {"brianquinlan/marathon2": None, "google/jax": None},
        }
        mock_user_doc.get.return_value = mock_user_snap
        mock_db.collection.return_value.document.return_value = mock_user_doc

        handler = get_callable_handler(dev.render_settings_page)
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.cookies = {"__session": "valid_token"}
        mock_req.args = {}
        mock_req.headers = {}

        resp = handler(mock_req)
        assert isinstance(resp, Response)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("<h2>Settings</h2>", html)
        self.assertIn("brianquinlan/marathon2, google/jax", html)
        self.assertIn('value="ghp_secret12345678"', html)
        self.assertIn('value="AIzaSySecretGeminiKey"', html)

    @patch("dev.auth.verify_id_token")
    @patch("dev.db")
    def test_render_settings_page_post_updates_and_renders_saved(self, mock_db, mock_verify):
        mock_verify.return_value = {"uid": "user_settings_2", "email": "dev2@test.com"}

        mock_user_doc = MagicMock()
        mock_user_snap = MagicMock()
        mock_user_snap.exists = True
        mock_user_snap.to_dict.return_value = {
            "github_access_token": "ghp_new_updated_token_999",
            "gemini_api_key": "AIzaSyNewGeminiKey",
            "monitored_repos": {"org/new-repo": None},
        }
        mock_user_doc.get.return_value = mock_user_snap
        mock_db.collection.return_value.document.return_value = mock_user_doc

        handler = get_callable_handler(dev.render_settings_page)
        mock_req = MagicMock()
        mock_req.method = "POST"
        mock_req.cookies = {"__session": "valid_token"}
        mock_req.args = {}
        mock_req.headers = {}
        mock_req.form = {
            "github_access_token": "ghp_new_updated_token_999",
            "gemini_api_key": "AIzaSyNewGeminiKey",
            "monitored_repos": "org/new-repo",
        }

        resp = handler(mock_req)
        assert isinstance(resp, Response)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Settings updated successfully", html)
        mock_user_doc.set.assert_called_once()


class TestFirestoreUserSettingsTrigger(unittest.TestCase):
    @patch("main.delete_all_user_tasks")
    @patch("main.start_user_github_sync")
    @patch("main.db")
    def test_trigger_invoked_when_settings_newly_created(self, mock_db, mock_start_sync, mock_delete_tasks):
        handler = get_callable_handler(main.on_user_settings_changed)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_new_trig_0"}

        # Document didn't exist before
        mock_before = None

        mock_after = MagicMock()
        mock_after.exists = True
        mock_after.to_dict.return_value = {
            "github_access_token": "ghp_brand_new_token_123",
            "monitored_repos": {"brianquinlan/marathon2": None},
        }

        mock_event.data.before = mock_before
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_delete_tasks.assert_called_once_with(uid="user_new_trig_0", db=mock_db)
        mock_start_sync.assert_called_once()
        call_user = mock_start_sync.call_args[1]["user"]
        self.assertEqual(call_user.uid, "user_new_trig_0")
        self.assertEqual(call_user.github_access_token, "ghp_brand_new_token_123")
        self.assertEqual(call_user.monitored_repos, {"brianquinlan/marathon2": None})

    @patch("main.delete_all_user_tasks")
    @patch("main.start_user_github_sync")
    @patch("main.db")
    def test_trigger_invoked_when_token_changed(self, mock_db, mock_start_sync, mock_delete_tasks):
        handler = get_callable_handler(main.on_user_settings_changed)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_trig_1"}

        mock_before = MagicMock()
        mock_before.to_dict.return_value = {
            "github_access_token": "ghp_old_token",
            "monitored_repos": {"org/repo": None},
        }

        mock_after = MagicMock()
        mock_after.to_dict.return_value = {
            "github_access_token": "ghp_new_token",
            "monitored_repos": {"org/repo": None},
        }

        mock_event.data.before = mock_before
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_delete_tasks.assert_called_once_with(uid="user_trig_1", db=mock_db)
        mock_start_sync.assert_called_once()
        call_user = mock_start_sync.call_args[1]["user"]
        self.assertEqual(call_user.uid, "user_trig_1")
        self.assertEqual(call_user.github_access_token, "ghp_new_token")

    @patch("main.enqueue_issue_page_sync")
    @patch("main.db")
    def test_trigger_invoked_when_monitored_repo_added(self, mock_db, mock_enqueue_issue):
        handler = get_callable_handler(main.on_user_settings_changed)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_trig_2"}

        mock_before = MagicMock()
        mock_before.to_dict.return_value = {
            "github_access_token": "ghp_token_same",
            "monitored_repos": {"org/repo1": None},
        }

        mock_after = MagicMock()
        mock_after.to_dict.return_value = {
            "github_access_token": "ghp_token_same",
            "monitored_repos": {"org/repo1": None, "org/repo2": None},
        }

        mock_event.data.before = mock_before
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_enqueue_issue.assert_called_once_with(
            uid="user_trig_2",
            db=mock_db,
            repo_full_name="org/repo2",
            state="open",
            since=None,
            page=0,
            per_page=100,
            owner_fallback="org",
            repo_fallback="repo2",
        )

    @patch("main.cleanup_repo_tasks")
    @patch("main.db")
    def test_trigger_invoked_when_monitored_repo_removed(self, mock_db, mock_cleanup):
        handler = get_callable_handler(main.on_user_settings_changed)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_trig_remove"}

        mock_before = MagicMock()
        mock_before.to_dict.return_value = {
            "github_access_token": "ghp_token_same",
            "monitored_repos": {"org/repo1": None, "org/repo2": None},
        }

        mock_after = MagicMock()
        mock_after.to_dict.return_value = {
            "github_access_token": "ghp_token_same",
            "monitored_repos": {"org/repo1": None},
        }

        mock_event.data.before = mock_before
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_cleanup.assert_called_once_with(uid="user_trig_remove", repo_full_name="org/repo2", db=mock_db)

    @patch("main.mark_all_tasks_for_reranking")
    @patch("main.db")
    def test_trigger_invoked_when_gemini_key_changed(self, mock_db, mock_mark_rerank):
        handler = get_callable_handler(main.on_user_settings_changed)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_trig_gemini"}

        mock_before = MagicMock()
        mock_before.to_dict.return_value = {
            "github_access_token": "ghp_token_same",
            "gemini_api_key": "AIzaOldKey",
            "monitored_repos": {"org/repo1": None},
        }

        mock_after = MagicMock()
        mock_after.to_dict.return_value = {
            "github_access_token": "ghp_token_same",
            "gemini_api_key": "AIzaNewKey",
            "monitored_repos": {"org/repo1": None},
        }

        mock_event.data.before = mock_before
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_mark_rerank.assert_called_once_with(uid="user_trig_gemini", db=mock_db)

    @patch("main.start_user_github_sync")
    @patch("main.db")
    def test_trigger_skipped_when_settings_unchanged(self, mock_db, mock_start_sync):
        handler = get_callable_handler(main.on_user_settings_changed)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_trig_3"}

        # Only last_assigned_sync changed
        mock_before = MagicMock()
        mock_before.to_dict.return_value = {
            "github_access_token": "ghp_token_same",
            "monitored_repos": {"org/repo1": None},
            "last_assigned_sync": "2026-08-22T00:00:00Z",
        }

        mock_after = MagicMock()
        mock_after.to_dict.return_value = {
            "github_access_token": "ghp_token_same",
            "monitored_repos": {"org/repo1": None},
            "last_assigned_sync": "2026-08-22T01:00:00Z",
        }

        mock_event.data.before = mock_before
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_start_sync.assert_not_called()

    @patch("main.start_user_github_sync")
    @patch("main.db")
    def test_trigger_skipped_when_token_is_missing(self, mock_db, mock_start_sync):
        handler = get_callable_handler(main.on_user_settings_changed)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_trig_4"}

        mock_before = MagicMock()
        mock_before.to_dict.return_value = {}

        mock_after = MagicMock()
        mock_after.to_dict.return_value = {"monitored_repos": {"org/repo1": None}}

        mock_event.data.before = mock_before
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_start_sync.assert_not_called()


class TestFirestoreTaskTrigger(unittest.TestCase):
    @patch("main.enqueue_task_ranking")
    @patch("main.db")
    def test_task_trigger_invoked_when_new_task_created_with_needs_update(self, mock_db, mock_enqueue_ranking):
        handler = get_callable_handler(main.on_task_written)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_task_trig_1", "task_id": "task_issue_1"}

        mock_after = MagicMock()
        mock_after.to_dict.return_value = {
            "id": "task_issue_1",
            "priority": 0.0,
            "priority_needs_updated": True,
            "title": "Bugfix issue",
        }

        mock_event.data.before = None
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_enqueue_ranking.assert_called_once_with(
            uid="user_task_trig_1", task_id="task_issue_1", function_name="rank_user_tasks", db=mock_db
        )

    @patch("main.enqueue_task_ranking")
    @patch("main.db")
    def test_task_trigger_invoked_when_task_updated_to_needs_update(self, mock_db, mock_enqueue_ranking):
        handler = get_callable_handler(main.on_task_written)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_task_trig_2", "task_id": "task_issue_2"}

        mock_before = MagicMock()
        mock_before.to_dict.return_value = {
            "id": "task_issue_2",
            "priority": 0.7,
            "priority_needs_updated": False,
        }

        mock_after = MagicMock()
        mock_after.to_dict.return_value = {
            "id": "task_issue_2",
            "priority": 0.7,
            "priority_needs_updated": True,
        }

        mock_event.data.before = mock_before
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_enqueue_ranking.assert_called_once_with(
            uid="user_task_trig_2", task_id="task_issue_2", function_name="rank_user_tasks", db=mock_db
        )

    @patch("main.enqueue_task_ranking")
    @patch("main.db")
    def test_task_trigger_skipped_when_priority_needs_updated_is_false(self, mock_db, mock_enqueue_ranking):
        """Ensures loop prevention when ranker updates tasks and sets priority_needs_updated=False."""
        handler = get_callable_handler(main.on_task_written)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_task_trig_3", "task_id": "task_issue_3"}

        mock_before = MagicMock()
        mock_before.to_dict.return_value = {
            "id": "task_issue_3",
            "priority": 0.0,
            "priority_needs_updated": True,
        }

        # Ranker just finished and saved priority=0.85, priority_needs_updated=False
        mock_after = MagicMock()
        mock_after.to_dict.return_value = {
            "id": "task_issue_3",
            "priority": 0.85,
            "priority_needs_updated": False,
        }

        mock_event.data.before = mock_before
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_enqueue_ranking.assert_not_called()

    @patch("main.enqueue_task_ranking")
    @patch("main.db")
    def test_task_trigger_skipped_when_task_deleted(self, mock_db, mock_enqueue_ranking):
        handler = get_callable_handler(main.on_task_written)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_task_trig_4", "task_id": "task_issue_4"}
        mock_event.data.before = MagicMock()
        mock_event.data.after = None

        handler(mock_event)
        mock_enqueue_ranking.assert_not_called()


class TestPeriodicGithubSyncSchedulerAndWorker(unittest.TestCase):
    @patch("main.sync_user_periodic")
    @patch("main.db")
    def test_sync_user_periodic_task_worker(self, mock_db, mock_sync_fn):
        handler = get_callable_handler(main.sync_user_periodic_task)

        mock_req = MagicMock(spec=tasks_fn.CallableRequest)
        mock_req.data = {"uid": "user_periodic_worker_1"}

        result = handler(mock_req)
        self.assertIsNone(result)
        mock_sync_fn.assert_called_once_with(uid="user_periodic_worker_1", db=mock_db)

    def test_sync_user_periodic_task_worker_missing_uid_raises_error(self):
        handler = get_callable_handler(main.sync_user_periodic_task)

        mock_req = MagicMock(spec=tasks_fn.CallableRequest)
        mock_req.data = {}

        with self.assertRaises(tasks_fn.HttpsError) as ctx:
            handler(mock_req)
        self.assertEqual(ctx.exception.code, tasks_fn.FunctionsErrorCode.INVALID_ARGUMENT)

    @patch("main.enqueue_user_periodic_sync")
    @patch("main.db")
    def test_periodic_github_sync_scheduler_enqueues_users_with_token(self, mock_db, mock_enqueue_sync):
        handler = get_callable_handler(main.periodic_github_sync_scheduler)

        mock_users_col = MagicMock()
        mock_db.collection.return_value = mock_users_col

        user_with_token = MagicMock()
        user_with_token.id = "user_with_tok"
        user_with_token.to_dict.return_value = {"github_access_token": "ghp_tok_123"}

        user_no_token = MagicMock()
        user_no_token.id = "user_no_tok"
        user_no_token.to_dict.return_value = {"github_username": "bob"}

        mock_users_col.stream.return_value = [user_with_token, user_no_token]

        mock_event = MagicMock()
        handler(mock_event)

        mock_enqueue_sync.assert_called_once_with(uid="user_with_tok", db=mock_db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
