"""
Comprehensive Unit and Integration Tests for Firebase Functions Python Backend
Tests User dataclass, Google/GitHub auth extraction, monitored_repos, issue syncing, Task ranking, and REST handlers.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch
import json
from datetime import datetime, timezone

# Ensure functions module is in sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "functions"))

from auth_utils import extract_provider_info, verify_bearer_token, fetch_full_user_auth_record
from user import User
from task import Task
from github import Issue, IssueType, Comment
from firebase_functions import https_fn, tasks_fn
import main


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
        user = User(
            github_access_token="gho_test_token_123",
            last_assigned_issue_update_time="2026-08-22T08:00:00Z",
            monitored_repos=["brianquinlan/marathon2", "google/jax"],
            uid="user_abc_123",
            email="developer@example.com",
            email_verified=True,
            display_name="Dev Example",
            photo_url="https://avatar.example.com/1",
            primary_provider="github.com",
            github_id="12345678"
        )
        self.assertEqual(user.github_access_token, "gho_test_token_123")
        self.assertEqual(user.last_assigned_issue_update_time, "2026-08-22T08:00:00Z")
        self.assertEqual(user.monitored_repos, ["brianquinlan/marathon2", "google/jax"])
        self.assertEqual(user.primary_provider, "github.com")
        self.assertTrue(user.email_verified)

        # Test Pydantic JSON serialization
        json_str = user.model_dump_json()
        self.assertIn('"uid":"user_abc_123"', json_str.replace(" ", ""))
        self.assertIn('"github_access_token":"gho_test_token_123"', json_str.replace(" ", ""))
        dumped = user.model_dump()
        self.assertEqual(dumped["uid"], "user_abc_123")

    def test_user_model_dump_and_model_validate(self):
        user = User(
            uid="user_999",
            email="google@domain.com",
            github_access_token="gho_xyz",
            last_assigned_issue_update_time="2026-08-22T10:30:00Z",
            monitored_repos=["owner/repo1"],
            custom_data={"role": "maintainer"}
        )
        data = user.model_dump()
        self.assertEqual(data["uid"], "user_999")
        self.assertEqual(data["github_access_token"], "gho_xyz")
        self.assertEqual(data["monitored_repos"], ["owner/repo1"])
        self.assertEqual(data["custom_data"]["role"], "maintainer")

        # Reconstruct from dict
        reconstructed = User.model_validate(data)
        self.assertEqual(reconstructed.uid, user.uid)
        self.assertEqual(reconstructed.github_access_token, "gho_xyz")
        self.assertEqual(reconstructed.monitored_repos, ["owner/repo1"])


class TestAuthProviderExtraction(unittest.TestCase):

    def test_extract_google_provider_info(self):
        token = {
            "uid": "google-user-123",
            "email": "alex@gmail.com",
            "email_verified": True,
            "name": "Alex Developer",
            "picture": "https://lh3.googleusercontent.com/a/sample",
            "firebase": {
                "sign_in_provider": "google.com",
                "identities": {
                    "google.com": ["google-sub-id-987"],
                    "email": ["alex@gmail.com"]
                }
            }
        }

        info = extract_provider_info(token)
        self.assertEqual(info["primary_provider"], "google.com")
        self.assertEqual(info["primary_provider_name"], "Google")
        self.assertTrue(info["is_google"])
        self.assertFalse(info["is_github"])


class TestCallableFunctionLogic(unittest.TestCase):

    @patch("main.db")
    def test_associate_user_info_with_monitored_repos(self, mock_db):
        handler = get_callable_handler(main.associate_user_info)

        mock_doc_ref = MagicMock()
        mock_doc_snap = MagicMock()
        mock_doc_snap.exists = False
        mock_doc_ref.get.return_value = mock_doc_snap
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        mock_req = MagicMock(spec=https_fn.CallableRequest)
        mock_req.auth = MagicMock()
        mock_req.auth.uid = "user_github_001"
        mock_req.auth.token = {
            "email": "user@github.com",
            "name": "GitHub Dev",
            "firebase": {"sign_in_provider": "github.com", "identities": {"github.com": ["12345"]}}
        }
        mock_req.data = {
            "github_access_token": "gho_sample_token_xyz",
            "monitored_repos": ["brianquinlan/marathon2", "org/awesome-project"],
        }

        result = handler(mock_req)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["user"]["monitored_repos"], ["brianquinlan/marathon2", "org/awesome-project"])
        mock_doc_ref.set.assert_called_once()

    @patch("main.start_user_github_sync")
    @patch("main.db")
    def test_sync_github_issues_callable(self, mock_db, mock_sync_fn):
        handler = get_callable_handler(main.sync_github_issues)

        mock_doc_ref = MagicMock()
        mock_doc_snap = MagicMock()
        mock_doc_snap.exists = True
        mock_doc_snap.to_dict.return_value = {
            "uid": "user_sync_001",
            "github_access_token": "gho_valid_token"
        }
        mock_doc_ref.get.return_value = mock_doc_snap
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        mock_sync_fn.return_value = {
            "status": "enqueued",
            "uid": "user_sync_001",
            "initial_queues_count": 4
        }

        mock_req = MagicMock(spec=https_fn.CallableRequest)
        mock_req.auth = MagicMock()
        mock_req.auth.uid = "user_sync_001"
        mock_req.data = {"state": "all"}

        result = handler(mock_req)
        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(result["initial_queues_count"], 4)

    @patch("main.force_rerank_tasks")
    @patch("main.db")
    def test_force_rerank_all_tasks_callable(self, mock_db, mock_force_fn):
        handler = get_callable_handler(main.force_rerank_all_tasks)
        mock_force_fn.return_value = {
            "status": "enqueued",
            "marked_count": 5,
            "message": "Marked 5 tasks for rerank and enqueued ranking task."
        }

        mock_req = MagicMock(spec=https_fn.CallableRequest)
        mock_req.auth = MagicMock()
        mock_req.auth.uid = "user_rank_002"

        result = handler(mock_req)
        self.assertEqual(result["status"], "enqueued")
        mock_force_fn.assert_called_once_with(uid="user_rank_002", db=mock_db)


class TestTaskQueueSyncHandlers(unittest.TestCase):

    @patch("github.enqueue_issue_page_sync")
    @patch("github.enqueue_comment_page_sync")
    @patch("github.process_and_save_issue_page")
    @patch("github.fetch_single_issue_page")
    @patch("main.db")
    def test_sync_github_issues_page_handler(
        self, mock_db, mock_fetch_page, mock_save_page, mock_enqueue_comment, mock_enqueue_issue
    ):
        handler = get_callable_handler(main.sync_github_issues_page)

        mock_doc_ref = MagicMock()
        mock_doc_snap = MagicMock()
        mock_doc_snap.exists = True
        mock_doc_snap.to_dict.return_value = {
            "uid": "user_q_1",
            "github_access_token": "gho_token_q1"
        }
        mock_doc_ref.get.return_value = mock_doc_snap
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        mock_fetch_page.return_value = (
            [{"number": 100, "comments": 2, "comments_url": "https://api.github.com/comments/100", "repository": {"owner": {"login": "o"}, "name": "r"}}],
            "https://api.github.com/issues?page=2"
        )
        mock_save_page.return_value = ["o_r_100"]

        mock_req = MagicMock(spec=tasks_fn.CallableRequest)
        mock_req.data = {
            "uid": "user_q_1",
            "url": "https://api.github.com/issues",
            "reason": "assigned"
        }

        result = handler(mock_req)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["saved_count"], 1)
        self.assertEqual(result["next_url"], "https://api.github.com/issues?page=2")
        mock_enqueue_comment.assert_called_once()
        mock_enqueue_issue.assert_called_once()

    @patch("github.enqueue_comment_page_sync")
    @patch("github.process_and_save_comment_page")
    @patch("github.fetch_single_comment_page")
    @patch("main.db")
    def test_sync_issue_comments_page_handler(
        self, mock_db, mock_fetch_comments, mock_save_comments, mock_enqueue_next_comments
    ):
        handler = get_callable_handler(main.sync_issue_comments_page)

        mock_doc_ref = MagicMock()
        mock_doc_snap = MagicMock()
        mock_doc_snap.exists = True
        mock_doc_snap.to_dict.return_value = {
            "uid": "user_q_2",
            "github_access_token": "gho_token_q2"
        }
        mock_doc_ref.get.return_value = mock_doc_snap
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        mock_fetch_comments.return_value = (
            [Comment(id=1, user_login="alice", body="Test")],
            "https://api.github.com/comments?page=2"
        )
        mock_save_comments.return_value = 1

        mock_req = MagicMock(spec=tasks_fn.CallableRequest)
        mock_req.data = {
            "uid": "user_q_2",
            "issue_doc_id": "o_r_100",
            "comments_url": "https://api.github.com/comments"
        }

        result = handler(mock_req)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["saved_comments_count"], 1)
        mock_enqueue_next_comments.assert_called_once()


class TestScheduledFunctions(unittest.TestCase):

    @patch("main.sync_all_users_closed_issues")
    @patch("main.db")
    def test_scheduled_sync_closed_issues_execution(self, mock_db, mock_sync_all):
        mock_sync_all.return_value = {
            "users_processed": 2,
            "total_closed_tasks_removed": 3
        }

        handler = get_callable_handler(main.scheduled_sync_closed_issues)
        mock_event = MagicMock()

        handler(mock_event)
        mock_sync_all.assert_called_once_with(db=mock_db)


class TestJinjaMainPageRendering(unittest.TestCase):

    def test_render_main_page_unauthenticated(self):
        handler = get_callable_handler(main.render_main_page)
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.cookies = {}
        mock_req.args = {}
        mock_req.headers = {}

        resp = handler(mock_req)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["Content-Type"])
        html = resp.get_data(as_text=True)
        self.assertIn("Login", html)
        self.assertIn("Sign in with Google", html)

    @patch("main.auth.verify_id_token")
    @patch("main.db")
    def test_render_main_page_authenticated_renders_ranked_tasks(self, mock_db, mock_verify):
        mock_verify.return_value = {
            "uid": "user_jinja_1",
            "email": "tester@example.com",
            "name": "Tester Dev"
        }

        # Mock user document
        mock_user_doc = MagicMock()
        mock_user_snap = MagicMock()
        mock_user_snap.exists = True
        mock_user_snap.to_dict.return_value = {
            "display_name": "Tester Dev",
            "email": "tester@example.com"
        }
        mock_user_doc.get.return_value = mock_user_snap

        # Mock tasks collection
        mock_tasks_col = MagicMock()
        mock_task1 = MagicMock()
        mock_task1.to_dict.return_value = {
            "title": "Lower Priority Task",
            "priority": 0.30,
            "issue_url": "https://github.com/org/repo/issues/1",
            "priority_needs_updated": False
        }
        mock_task2 = MagicMock()
        mock_task2.to_dict.return_value = {
            "title": "Top Priority Task",
            "priority": 0.95,
            "issue_url": "https://github.com/org/repo/issues/2",
            "priority_needs_updated": True
        }
        mock_tasks_col.stream.return_value = [mock_task1, mock_task2]

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.collection.return_value = mock_tasks_col

        handler = get_callable_handler(main.render_main_page)
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.cookies = {"__session": "mock_id_token_123"}
        mock_req.args = {}
        mock_req.headers = {}

        resp = handler(mock_req)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["Content-Type"])
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


class TestJinjaSettingsPageCRUD(unittest.TestCase):

    def test_render_settings_page_unauthenticated_redirects(self):
        handler = get_callable_handler(main.render_settings_page)
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.cookies = {}
        mock_req.args = {}
        mock_req.headers = {}

        resp = handler(mock_req)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")

    @patch("main.auth.verify_id_token")
    @patch("main.db")
    def test_render_settings_page_get_authenticated(self, mock_db, mock_verify):
        mock_verify.return_value = {
            "uid": "user_settings_1",
            "email": "dev@test.com"
        }

        mock_user_doc = MagicMock()
        mock_user_snap = MagicMock()
        mock_user_snap.exists = True
        mock_user_snap.to_dict.return_value = {
            "github_access_token": "ghp_secret12345678",
            "monitored_repos": ["brianquinlan/marathon2", "google/jax"]
        }
        mock_user_doc.get.return_value = mock_user_snap
        mock_db.collection.return_value.document.return_value = mock_user_doc

        handler = get_callable_handler(main.render_settings_page)
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.cookies = {"__session": "valid_token"}
        mock_req.args = {}
        mock_req.headers = {}

        resp = handler(mock_req)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("<h2>Settings</h2>", html)
        self.assertIn("brianquinlan/marathon2, google/jax", html)
        self.assertIn('value="ghp_secret12345678"', html)

    @patch("main.auth.verify_id_token")
    @patch("main.db")
    def test_render_settings_page_post_updates_and_renders_saved(self, mock_db, mock_verify):
        mock_verify.return_value = {
            "uid": "user_settings_2",
            "email": "dev2@test.com"
        }

        mock_user_doc = MagicMock()
        mock_user_snap = MagicMock()
        mock_user_snap.exists = True
        mock_user_snap.to_dict.return_value = {
            "github_access_token": "ghp_new_updated_token_999",
            "monitored_repos": ["org/new-repo"]
        }
        mock_user_doc.get.return_value = mock_user_snap
        mock_db.collection.return_value.document.return_value = mock_user_doc

        handler = get_callable_handler(main.render_settings_page)
        mock_req = MagicMock()
        mock_req.method = "POST"
        mock_req.cookies = {"__session": "valid_token"}
        mock_req.args = {}
        mock_req.headers = {}
        mock_req.form = {
            "github_access_token": "ghp_new_updated_token_999",
            "monitored_repos": "org/new-repo"
        }

        resp = handler(mock_req)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Settings updated successfully", html)
        mock_user_doc.set.assert_called_once()


class TestFirestoreUserSettingsTrigger(unittest.TestCase):

    @patch("main.start_user_github_sync")
    @patch("main.db")
    def test_trigger_invoked_when_settings_newly_created(self, mock_db, mock_start_sync):
        handler = get_callable_handler(main.on_user_settings_changed)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_new_trig_0"}

        # Document didn't exist before
        mock_before = None

        mock_after = MagicMock()
        mock_after.exists = True
        mock_after.to_dict.return_value = {
            "github_access_token": "ghp_brand_new_token_123",
            "monitored_repos": ["brianquinlan/marathon2"]
        }

        mock_event.data.before = mock_before
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_start_sync.assert_called_once()
        call_user = mock_start_sync.call_args[1]["user"]
        self.assertEqual(call_user.uid, "user_new_trig_0")
        self.assertEqual(call_user.github_access_token, "ghp_brand_new_token_123")
        self.assertEqual(call_user.monitored_repos, ["brianquinlan/marathon2"])

    @patch("main.start_user_github_sync")
    @patch("main.db")
    def test_trigger_invoked_when_token_changed(self, mock_db, mock_start_sync):
        handler = get_callable_handler(main.on_user_settings_changed)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_trig_1"}

        mock_before = MagicMock()
        mock_before.to_dict.return_value = {
            "github_access_token": "ghp_old_token",
            "monitored_repos": ["org/repo"]
        }

        mock_after = MagicMock()
        mock_after.to_dict.return_value = {
            "github_access_token": "ghp_new_token",
            "monitored_repos": ["org/repo"]
        }

        mock_event.data.before = mock_before
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_start_sync.assert_called_once()
        call_user = mock_start_sync.call_args[1]["user"]
        self.assertEqual(call_user.uid, "user_trig_1")
        self.assertEqual(call_user.github_access_token, "ghp_new_token")

    @patch("main.start_user_github_sync")
    @patch("main.db")
    def test_trigger_invoked_when_monitored_repos_changed(self, mock_db, mock_start_sync):
        handler = get_callable_handler(main.on_user_settings_changed)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_trig_2"}

        mock_before = MagicMock()
        mock_before.to_dict.return_value = {
            "github_access_token": "ghp_token_same",
            "monitored_repos": ["org/repo1"]
        }

        mock_after = MagicMock()
        mock_after.to_dict.return_value = {
            "github_access_token": "ghp_token_same",
            "monitored_repos": ["org/repo1", "org/repo2"]
        }

        mock_event.data.before = mock_before
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_start_sync.assert_called_once()

    @patch("main.start_user_github_sync")
    @patch("main.db")
    def test_trigger_skipped_when_settings_unchanged(self, mock_db, mock_start_sync):
        handler = get_callable_handler(main.on_user_settings_changed)

        mock_event = MagicMock()
        mock_event.params = {"uid": "user_trig_3"}

        # Only last_assigned_issue_update_time changed
        mock_before = MagicMock()
        mock_before.to_dict.return_value = {
            "github_access_token": "ghp_token_same",
            "monitored_repos": ["org/repo1"],
            "last_assigned_issue_update_time": "2026-08-22T00:00:00Z"
        }

        mock_after = MagicMock()
        mock_after.to_dict.return_value = {
            "github_access_token": "ghp_token_same",
            "monitored_repos": ["org/repo1"],
            "last_assigned_issue_update_time": "2026-08-22T01:00:00Z"
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
        mock_after.to_dict.return_value = {
            "monitored_repos": ["org/repo1"]
        }

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
            "title": "Bugfix issue"
        }

        mock_event.data.before = None
        mock_event.data.after = mock_after

        handler(mock_event)
        mock_enqueue_ranking.assert_called_once_with(
            uid="user_task_trig_1",
            function_name="rank_user_tasks",
            db=mock_db
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
            uid="user_task_trig_2",
            function_name="rank_user_tasks",
            db=mock_db
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
