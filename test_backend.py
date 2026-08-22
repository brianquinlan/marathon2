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

    def test_user_to_dict_and_from_dict(self):
        user = User(
            uid="user_999",
            email="google@domain.com",
            github_access_token="gho_xyz",
            last_assigned_issue_update_time="2026-08-22T10:30:00Z",
            monitored_repos=["owner/repo1"],
            custom_data={"role": "maintainer"}
        )
        data = user.to_dict(for_firestore=False)
        self.assertEqual(data["uid"], "user_999")
        self.assertEqual(data["github_access_token"], "gho_xyz")
        self.assertEqual(data["monitored_repos"], ["owner/repo1"])
        self.assertEqual(data["custom_data"]["role"], "maintainer")

        # Reconstruct from dict
        reconstructed = User.from_dict(data)
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

    @patch("main.enqueue_task_ranking")
    @patch("main.db")
    def test_update_task_priorities_callable(self, mock_db, mock_enqueue_fn):
        handler = get_callable_handler(main.update_task_priorities)
        mock_enqueue_fn.return_value = {
            "status": "enqueued",
            "uid": "user_rank_001",
            "mode": "async_dispatched"
        }

        mock_req = MagicMock(spec=https_fn.CallableRequest)
        mock_req.auth = MagicMock()
        mock_req.auth.uid = "user_rank_001"

        result = handler(mock_req)
        self.assertEqual(result["status"], "enqueued")
        mock_enqueue_fn.assert_called_once_with(uid="user_rank_001", db=mock_db)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
