"""
Unit tests for functions/github_sync.py (PyGithub integration)
Tests GitHub data structures, PyGithub adaptors, single-page issue & comment fetching, pagination chaining,
Firestore persistence, and ensuring Tasks are ONLY created once an Issue and all comments are fully imported.
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import ANY, MagicMock, patch

# Add functions to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "functions"))

from github_sync import (
    IssuePayload,
    enqueue_issue_page_sync,
    enqueue_user_periodic_sync,
    fetch_github_user_login,
    fetch_issue_in_memory,
    fetch_single_issue_page,
    get_github_client,
    process_and_save_issue_page,
    start_user_github_sync,
    sync_user_periodic,
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
    def test_get_github_client_default_per_page(self):
        client = get_github_client("fake_token")
        self.assertEqual(client.per_page, 100)

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
        self.assertEqual(mock_client.per_page, 100)
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
            monitored_repos={"google/jax": None, "brianquinlan/marathon2": None},
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
            uid="user_discover_1", github_access_token="gho_tok_discover", github_username=None, monitored_repos={}
        )

        res = start_user_github_sync(user=user, db=mock_db)
        self.assertEqual(res["status"], "enqueued")
        self.assertEqual(user.github_username, "brianquinlan")
        mock_fetch_login.assert_called_once_with("gho_tok_discover")
        mock_user_doc.set.assert_any_call({"github_username": "brianquinlan", "updated_at": ANY}, merge=True)


class TestClosedIssuesSync(unittest.TestCase):
    @patch("github_sync.delete_task_for_issue")
    @patch("github_sync.ensure_task_for_issue")
    def test_process_and_save_issue_page_handles_open_and_closed_issues(self, mock_ensure, mock_delete):
        mock_db = MagicMock()

        # 1. Closed issue
        closed_issue = MagicMock()
        closed_issue.number = 101
        closed_issue.state = "closed"
        closed_issue.repository.owner.login = "brianquinlan"
        closed_issue.repository.name = "marathon2"

        # 2. Open issue
        open_issue = MagicMock()
        open_issue.number = 102
        open_issue.state = "open"
        open_issue.title = "Open Feature"
        open_issue.html_url = "https://github.com/brianquinlan/marathon2/issues/102"
        open_issue.repository.owner.login = "brianquinlan"
        open_issue.repository.name = "marathon2"

        process_and_save_issue_page(
            uid="user_closed_test",
            issues=[closed_issue, open_issue],
            db=mock_db,
            source="monitored",
        )

        mock_delete.assert_called_once_with(
            uid="user_closed_test",
            issue_id="brianquinlan_marathon2_101",
            db=mock_db,
        )
        mock_ensure.assert_called_once()
        self.assertEqual(mock_ensure.call_args[1]["issue_id"], "brianquinlan_marathon2_102")
        self.assertEqual(mock_ensure.call_args[1]["source"], "monitored")


class TestPeriodicUserSync(unittest.TestCase):
    @patch("github_sync.start_user_github_sync")
    def test_sync_user_periodic_runs_unified_sync(self, mock_start_sync):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_user_snap = MagicMock()

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.get.return_value = mock_user_snap
        mock_user_snap.exists = True
        mock_user_snap.to_dict.return_value = {
            "github_access_token": "ghp_periodic_token_123",
            "monitored_repos": {"brianquinlan/marathon2": None},
        }

        mock_start_sync.return_value = {"status": "enqueued"}

        result = sync_user_periodic("user_periodic_1", mock_db)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["uid"], "user_periodic_1")
        mock_start_sync.assert_called_once()

    @patch("github_sync.enqueue_issue_page_sync")
    def test_start_user_github_sync_states_open_vs_all(self, mock_enqueue):
        mock_db = MagicMock()

        # User with sync timestamps (incremental sync -> state="all")
        now = datetime.now(timezone.utc)
        user_incremental = User(
            uid="user_inc",
            github_access_token="ghp_token_inc",
            last_assigned_sync=now,
            monitored_repos={"brianquinlan/marathon2": now},
        )
        start_user_github_sync(user=user_incremental, db=mock_db)

        # Assigned filter and monitored repo should have state="all"
        assigned_call = next(c for c in mock_enqueue.call_args_list if c[1].get("filter_name") == "assigned")
        self.assertEqual(assigned_call[1]["state"], "all")
        repo_call = next(
            c for c in mock_enqueue.call_args_list if c[1].get("repo_full_name") == "brianquinlan/marathon2"
        )
        self.assertEqual(repo_call[1]["state"], "all")

        mock_enqueue.reset_mock()

        # User without sync timestamps (initial sync -> state="open")
        user_initial = User(
            uid="user_init",
            github_access_token="ghp_token_init",
            last_assigned_sync=None,
            monitored_repos={"brianquinlan/marathon2": None},
        )
        start_user_github_sync(user=user_initial, db=mock_db)

        assigned_init = next(c for c in mock_enqueue.call_args_list if c[1].get("filter_name") == "assigned")
        self.assertEqual(assigned_init[1]["state"], "open")
        repo_init = next(
            c for c in mock_enqueue.call_args_list if c[1].get("repo_full_name") == "brianquinlan/marathon2"
        )
        self.assertEqual(repo_init[1]["state"], "open")

    def test_sync_user_periodic_skipped_if_no_token(self):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_user_snap = MagicMock()

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.get.return_value = mock_user_snap
        mock_user_snap.exists = True
        mock_user_snap.to_dict.return_value = {"github_username": "brian"}

        result = sync_user_periodic("user_no_token", mock_db)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_github_token")

    @patch("github_sync.dispatch_task")
    def test_enqueue_user_periodic_sync(self, mock_dispatch):
        mock_db = MagicMock()
        enqueue_user_periodic_sync(uid="user_enq_1", db=mock_db)
        mock_dispatch.assert_called_once()
        self.assertEqual(mock_dispatch.call_args[1]["queue_name"], "sync_user_periodic_task")
        self.assertEqual(mock_dispatch.call_args[1]["task_data"], {"uid": "user_enq_1"})


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
