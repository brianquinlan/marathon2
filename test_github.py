"""
Unit tests for functions/github_sync.py (PyGithub integration)
Tests GitHub data structures, PyGithub adaptors, single-page issue & comment fetching, pagination chaining,
Firestore persistence, and ensuring Tasks are ONLY created once an Issue and all comments are fully imported.
"""

import os
import sys
import unittest
from unittest.mock import ANY, MagicMock, patch

# Add functions to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "functions"))

from github_sync import (
    IssuePayload,
    enqueue_issue_page_sync,
    fetch_github_user_login,
    fetch_issue_in_memory,
    fetch_single_issue_page,
    process_and_save_issue_page,
    start_user_github_sync,
    sync_closed_issues_for_user,
)
from user import User


class TestGitHubDataStructures(unittest.TestCase):
    def test_issue_payload_model_and_json_serialization(self):
        payload = IssuePayload(
            issue={"id": 1001, "title": "Test Issue", "number": 42},
            comments=[{"id": 501, "body": "LGTM"}],
        )
        self.assertEqual(payload.issue["title"], "Test Issue")
        self.assertEqual(len(payload.comments), 1)

        json_str = payload.model_dump_json()
        self.assertIn('"title":"Test Issue"', json_str)
        self.assertIn('"body":"LGTM"', json_str)


class TestSinglePageFetchingAndPagination(unittest.TestCase):
    def test_fetch_single_issue_page_with_next_page(self):
        mock_client = MagicMock()
        mock_user = MagicMock()
        mock_paginated = MagicMock()
        mock_client.get_user.return_value = mock_user
        mock_user.get_issues.return_value = mock_paginated

        mock_issue = MagicMock()
        mock_issue.number = 1
        mock_issue.title = "Issue 1"
        mock_paginated.get_page.return_value = [mock_issue] * 100

        items, has_next = fetch_single_issue_page(
            client=mock_client,
            filter_name="assigned",
            page=0,
            per_page=100,
        )
        self.assertEqual(len(items), 100)
        self.assertTrue(has_next)


class TestFetchIssueInMemory(unittest.TestCase):
    @patch("github_sync.get_github_client")
    def test_fetch_issue_in_memory_structured_payload(self, mock_get_client):
        mock_client = MagicMock()
        mock_repo = MagicMock()
        mock_issue = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.get_repo.return_value = mock_repo
        mock_repo.get_issue.return_value = mock_issue

        mock_issue.raw_data = {
            "title": "Critical Performance Issue",
            "number": 42,
            "state": "open",
            "html_url": "https://github.com/dart-lang/http/issues/42",
            "user": {"login": "contributor1"},
            "assignees": [{"login": "brianquinlan"}],
            "labels": [{"name": "bug"}, {"name": "p1"}],
            "reactions": {"+1": 7},
        }

        mock_comment1 = MagicMock()
        mock_comment1.raw_data = {
            "id": 901,
            "user": {"login": "maintainer"},
            "body": "Working on a fix.",
            "created_at": "2026-08-22T10:00:00Z",
        }
        mock_issue.get_comments.return_value = [mock_comment1]

        data = fetch_issue_in_memory(
            access_token="ghp_test_token",
            owner="dart-lang",
            repo="http",
            issue_number=42,
            client=mock_client,
        )

        self.assertIsInstance(data, IssuePayload)
        self.assertEqual(data.issue["title"], "Critical Performance Issue")
        self.assertEqual(data.issue["number"], 42)
        self.assertEqual(len(data.comments), 1)
        self.assertEqual(data.comments[0]["body"], "Working on a fix.")


class TestPageProcessingAndDirectTaskCreation(unittest.TestCase):
    def test_process_and_save_issue_page_creates_tasks_directly(self):
        mock_db = MagicMock()

        mock_issue1 = MagicMock()
        mock_issue1.number = 10
        mock_issue1.title = "Bug fix issue"
        mock_issue1.html_url = "https://github.com/org/repo/issues/10"
        mock_issue1.repository.owner.login = "org"
        mock_issue1.repository.name = "repo"

        mock_issue2 = MagicMock()
        mock_issue2.number = 11
        mock_issue2.title = "Feature request"
        mock_issue2.html_url = "https://github.com/org/repo/issues/11"
        mock_issue2.repository.owner.login = "org"
        mock_issue2.repository.name = "repo"

        with patch("github_sync.ensure_task_for_issue") as mock_ensure_task:
            process_and_save_issue_page(uid="user_123", issues=[mock_issue1, mock_issue2], db=mock_db)
            self.assertEqual(mock_ensure_task.call_count, 2)
            # Verify ensure_task_for_issue was called with correct payload
            call1_args = mock_ensure_task.call_args_list[0][1]
            self.assertEqual(call1_args["issue_data"]["owner"], "org")
            self.assertEqual(call1_args["issue_data"]["repo"], "repo")
            self.assertEqual(call1_args["issue_data"]["issue_number"], 10)


class TestInitialSyncDispatcher(unittest.TestCase):
    @patch("github_sync.enqueue_issue_page_sync")
    def test_start_user_github_sync(self, mock_enqueue):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_db.collection.return_value.document.return_value = mock_user_doc

        user = User(
            uid="user_123",
            github_access_token="gho_token_123",
            monitored_repos=["google/jax", "brianquinlan/marathon2"],
        )

        result = start_user_github_sync(user=user, db=mock_db)
        self.assertEqual(result["status"], "enqueued")
        # 3 filters (assigned, mentioned, created) + 2 monitored repos = 5 queues
        self.assertEqual(result["initial_queues_count"], 5)
        self.assertEqual(mock_enqueue.call_count, 5)


class TestGitHubUserLoginDiscovery(unittest.TestCase):
    @patch("github_sync.get_github_client")
    def test_fetch_github_user_login_success(self, mock_get_client):
        mock_client = MagicMock()
        mock_user = MagicMock()
        mock_user.login = "brianquinlan"
        mock_client.get_user.return_value = mock_user
        mock_get_client.return_value = mock_client

        login = fetch_github_user_login("gho_valid_token")
        self.assertEqual(login, "brianquinlan")

    @patch("github_sync.get_github_client")
    def test_fetch_github_user_login_error_returns_none(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_user.side_effect = Exception("Invalid token")
        mock_get_client.return_value = mock_client

        login = fetch_github_user_login("gho_bad_token")
        self.assertIsNone(login)

    @patch("github_sync.fetch_github_user_login")
    @patch("github_sync.enqueue_issue_page_sync")
    def test_start_user_github_sync_discovers_username(self, mock_enqueue, mock_fetch_login):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_db.collection.return_value.document.return_value = mock_user_doc

        mock_fetch_login.return_value = "brianquinlan"

        user = User(
            uid="user_discover_1", github_access_token="gho_tok_discover", github_username=None, monitored_repos=[]
        )

        res = start_user_github_sync(user=user, db=mock_db)
        self.assertEqual(res["status"], "enqueued")
        self.assertEqual(user.github_username, "brianquinlan")
        mock_fetch_login.assert_called_once_with("gho_tok_discover")
        mock_user_doc.set.assert_any_call({"github_username": "brianquinlan", "updated_at": ANY}, merge=True)


class TestClosedIssuesSync(unittest.TestCase):
    @patch("github_sync.fetch_single_issue_page")
    def test_sync_closed_issues_for_user_deletes_tasks(self, mock_fetch):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_tasks_col = MagicMock()

        mock_task_ref = MagicMock()
        mock_task_snap = MagicMock()
        mock_task_snap.exists = True
        mock_task_ref.get.return_value = mock_task_snap

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.collection.return_value = mock_tasks_col
        mock_tasks_col.document.return_value = mock_task_ref

        # Return 1 closed issue from GitHub
        mock_issue = MagicMock()
        mock_issue.number = 99
        mock_issue.repository.owner.login = "brianquinlan"
        mock_issue.repository.name = "marathon2"

        mock_fetch.return_value = ([mock_issue], False)

        user = User(
            uid="user_closed_1", github_access_token="gho_test_closed", monitored_repos=["brianquinlan/marathon2"]
        )

        sync_closed_issues_for_user(user=user, db=mock_db)
        mock_task_ref.delete.assert_called_once()


class TestEnqueueIssuePageSync(unittest.TestCase):
    @patch("queue_utils.is_emulator", return_value=False)
    @patch("firebase_admin.functions.task_queue")
    def test_enqueue_issue_page_sync_production(self, mock_task_queue, mock_is_emu):
        mock_queue = MagicMock()
        mock_queue.enqueue.return_value = "issue_task_123"
        mock_task_queue.return_value = mock_queue

        mock_db = MagicMock()
        enqueue_issue_page_sync(
            uid="user_prod_1",
            db=mock_db,
            filter_name="assigned",
            page=0,
        )
        mock_task_queue.assert_called_once_with("sync_github_issues_page")
        mock_queue.enqueue.assert_called_once()

    @patch("queue_utils.is_emulator", return_value=True)
    @patch("queue_utils.threading.Thread")
    def test_enqueue_issue_page_sync_emulator(self, mock_thread_cls, mock_is_emu):
        mock_thread_instance = MagicMock()
        mock_thread_cls.return_value = mock_thread_instance

        mock_db = MagicMock()
        enqueue_issue_page_sync(
            uid="user_emu_1",
            db=mock_db,
            filter_name="assigned",
            page=1,
        )
        mock_thread_instance.start.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
