"""
Unit tests for functions/github_sync.py (PyGithub integration)
Tests GitHub data structures, PyGithub adaptors, single-page issue & comment fetching, pagination chaining,
Firestore persistence, and ensuring Tasks are ONLY created once an Issue and all comments are fully imported.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch, ANY
from datetime import datetime, timezone

# Add functions to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "functions"))

from github_sync import (
    Issue,
    IssueType,
    AssociationReason,
    Comment,
    get_github_client,
    comment_from_pygithub,
    issue_from_pygithub,
    fetch_single_issue_page_pygithub,
    fetch_single_comment_page_pygithub,
    fetch_single_issue_page,
    fetch_single_comment_page,
    process_and_save_issue_page,
    process_and_save_comment_page,
    start_user_github_sync,
    enqueue_issue_page_sync,
    enqueue_comment_page_sync,
    get_user_stored_issues,
    fetch_github_user_login,
    sync_closed_issues_for_user
)
from user import User


class TestGitHubDataStructures(unittest.TestCase):

    def test_comment_model_and_json_serialization(self):
        raw_api = {
            "id": 1001,
            "user": {"login": "octocat"},
            "body": "LGTM!",
            "created_at": "2026-08-22T10:00:00Z",
            "updated_at": "2026-08-22T10:05:00Z",
        }
        comment = Comment.from_api_dict(raw_api)
        self.assertEqual(comment.id, 1001)
        self.assertEqual(comment.user_login, "octocat")
        self.assertEqual(comment.body, "LGTM!")

        # Test Pydantic JSON serialization
        json_str = comment.model_dump_json()
        self.assertIn('"id":1001', json_str.replace(" ", ""))
        self.assertIn('"user_login":"octocat"', json_str.replace(" ", ""))

        d = comment.model_dump()
        self.assertEqual(d["id"], 1001)
        self.assertEqual(d["user_login"], "octocat")

    def test_issue_model_and_json_serialization(self):
        issue = Issue(
            url="https://github.com/brianquinlan/marathon2/issues/42",
            owner="brianquinlan",
            repo="marathon2",
            issue_number=42,
            issue_type=IssueType.ISSUE,
            comments_url="https://api.github.com/repos/brianquinlan/marathon2/issues/42/comments",
            number=42,
            title="Async task queue pagination",
            body="Implement task queue issue and comment pagination.",
            user_login="brianquinlan",
            assignee_logins=["brianquinlan"],
            created_at=datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 8, 22, 9, 30, tzinfo=timezone.utc),
            association_reasons=[AssociationReason.ASSIGNED, AssociationReason.CREATED],
        )
        self.assertEqual(issue.doc_id, "brianquinlan_marathon2_42")
        self.assertEqual(issue.issue_type, IssueType.ISSUE)
        self.assertIn(AssociationReason.ASSIGNED, issue.association_reasons)
        self.assertIn("assigned", issue.association_reasons)

        # Test Pydantic JSON serialization
        json_str = issue.model_dump_json()
        self.assertIn('"owner":"brianquinlan"', json_str.replace(" ", ""))
        self.assertIn('"issue_type":"issue"', json_str.replace(" ", ""))
        self.assertIn('"association_reasons":["assigned","created"]', json_str.replace(" ", ""))
        dumped = issue.model_dump()
        self.assertEqual(dumped["issue_number"], 42)


class TestPyGithubAdaptors(unittest.TestCase):

    def test_comment_from_pygithub(self):
        mock_pygh_comment = MagicMock()
        mock_pygh_comment.id = 7788
        mock_pygh_comment.user.login = "octo-reviewer"
        mock_pygh_comment.body = "Approved!"
        mock_pygh_comment.created_at = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
        mock_pygh_comment.updated_at = datetime(2026, 8, 22, 10, 5, tzinfo=timezone.utc)

        comment = comment_from_pygithub(mock_pygh_comment)
        self.assertEqual(comment.id, 7788)
        self.assertEqual(comment.user_login, "octo-reviewer")
        self.assertEqual(comment.body, "Approved!")
        self.assertEqual(comment.created_at, datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc))

    def test_issue_from_pygithub(self):
        mock_pygh_issue = MagicMock()
        mock_pygh_issue.number = 105
        mock_pygh_issue.title = "Add PyGithub integration"
        mock_pygh_issue.body = "Refactor GitHub layer to use PyGithub."
        mock_pygh_issue.state = "open"
        mock_pygh_issue.html_url = "https://github.com/brianquinlan/marathon2/issues/105"
        mock_pygh_issue.pull_request = None
        mock_pygh_issue.user.login = "brianquinlan"
        mock_assignee = MagicMock()
        mock_assignee.login = "brianquinlan"
        mock_pygh_issue.assignees = [mock_assignee]
        mock_pygh_issue.repository.owner.login = "brianquinlan"
        mock_pygh_issue.repository.name = "marathon2"
        mock_pygh_issue.created_at = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)
        mock_pygh_issue.updated_at = datetime(2026, 8, 22, 8, 30, tzinfo=timezone.utc)
        mock_pygh_issue.comments_url = "https://api.github.com/repos/brianquinlan/marathon2/issues/105/comments"

        issue = issue_from_pygithub(mock_pygh_issue, reason=AssociationReason.ASSIGNED)
        self.assertEqual(issue.doc_id, "brianquinlan_marathon2_105")
        self.assertEqual(issue.issue_type, IssueType.ISSUE)
        self.assertEqual(issue.owner, "brianquinlan")
        self.assertEqual(issue.repo, "marathon2")
        self.assertEqual(issue.title, "Add PyGithub integration")
        self.assertEqual(issue.assignee_logins, ["brianquinlan"])


class TestSinglePageFetchingAndPagination(unittest.TestCase):

    @patch("github_sync.fetch_single_issue_page_pygithub")
    @patch("github_sync.get_github_client")
    def test_fetch_single_issue_page_with_next_link(self, mock_get_client, mock_fetch_pygh):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_issue = MagicMock()
        mock_issue.number = 1
        mock_issue.title = "Issue 1"
        mock_issue.body = "Body 1"
        mock_issue.state = "open"
        mock_issue.html_url = "https://github.com/org/repo/issues/1"
        mock_issue.comments = 2
        mock_issue.comments_url = "https://api.github.com/repos/org/repo/issues/1/comments"
        mock_issue.created_at = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
        mock_issue.updated_at = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
        mock_issue.user.login = "author1"
        mock_issue.assignees = []
        mock_issue.pull_request = None
        mock_issue.repository.owner.login = "org"
        mock_issue.repository.name = "repo"

        mock_fetch_pygh.return_value = ([mock_issue], True)

        items, next_url = fetch_single_issue_page(
            "https://api.github.com/issues",
            headers={"Authorization": "Bearer test"},
            params={"page": 0, "per_page": 100}
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["number"], 1)
        self.assertEqual(next_url, "https://api.github.com/issues?page=1")

    @patch("github_sync.fetch_single_comment_page_pygithub")
    @patch("github_sync.get_github_client")
    def test_fetch_single_comment_page_with_next_link(self, mock_get_client, mock_fetch_pygh):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_comment = Comment(
            id=501,
            user_login="reviewer",
            body="Looks good",
            created_at=datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
        )
        mock_fetch_pygh.return_value = ([mock_comment], True)

        comments, next_url = fetch_single_comment_page(
            "https://api.github.com/repos/org/repo/issues/10/comments",
            headers={"Authorization": "Bearer test"},
            params={"page": 0, "per_page": 100}
        )
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].id, 501)
        self.assertEqual(next_url, "https://api.github.com/repos/org/repo/issues/10/comments?page=1")


class TestPageProcessingAndTaskCreationTiming(unittest.TestCase):

    def test_process_and_save_issue_page_with_comments_does_not_create_task_yet(self):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_issues_col = MagicMock()
        mock_issue_ref = MagicMock()
        mock_doc_snap = MagicMock()
        mock_doc_snap.exists = False
        mock_issue_ref.get.return_value = mock_doc_snap

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.collection.return_value = mock_issues_col
        mock_issues_col.document.return_value = mock_issue_ref

        raw_items = [
            {
                "number": 10,
                "title": "Bug fix",
                "html_url": "https://github.com/org/repo/issues/10",
                "repository": {"owner": {"login": "org"}, "name": "repo"},
                "user": {"login": "author"},
                "comments": 5,  # Has 5 comments to fetch
                "comments_url": "https://api.github.com/repos/org/repo/issues/10/comments"
            }
        ]

        with patch("github_sync.ensure_task_for_issue") as mock_ensure_task:
            saved_ids = process_and_save_issue_page(
                uid="user_123",
                raw_items=raw_items,
                reason="assigned",
                db=mock_db
            )
            self.assertEqual(saved_ids, ["org_repo_10"])
            mock_issue_ref.set.assert_called_once()
            # Task must NOT be created yet because comments are still pending!
            mock_ensure_task.assert_not_called()

    def test_process_and_save_issue_page_with_zero_comments_creates_task_immediately(self):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_issues_col = MagicMock()
        mock_issue_ref = MagicMock()
        mock_doc_snap = MagicMock()
        mock_doc_snap.exists = False
        mock_issue_ref.get.return_value = mock_doc_snap

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.collection.return_value = mock_issues_col
        mock_issues_col.document.return_value = mock_issue_ref

        raw_items = [
            {
                "number": 11,
                "title": "Zero comments issue",
                "html_url": "https://github.com/org/repo/issues/11",
                "repository": {"owner": {"login": "org"}, "name": "repo"},
                "user": {"login": "author"},
                "comments": 0,  # Zero comments
                "comments_url": "https://api.github.com/repos/org/repo/issues/11/comments"
            }
        ]

        with patch("github_sync.ensure_task_for_issue") as mock_ensure_task:
            saved_ids = process_and_save_issue_page(
                uid="user_123",
                raw_items=raw_items,
                reason="assigned",
                db=mock_db
            )
            self.assertEqual(saved_ids, ["org_repo_11"])
            mock_issue_ref.set.assert_called_once()
            # Task is created immediately because all (0) comments are imported
            mock_ensure_task.assert_called_once()

    def test_process_and_save_comment_page_creates_task_only_on_last_page(self):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_issues_col = MagicMock()
        mock_issue_ref = MagicMock()
        mock_doc_snap = MagicMock()

        mock_doc_snap.exists = True
        mock_doc_snap.to_dict.return_value = {
            "title": "Issue with comments",
            "comments": [{"id": 1, "user_login": "u1", "body": "c1"}]
        }
        mock_issue_ref.get.return_value = mock_doc_snap

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.collection.return_value = mock_issues_col
        mock_issues_col.document.return_value = mock_issue_ref

        new_comments = [
            Comment(id=2, user_login="u2", body="c2", created_at=datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc))
        ]

        with patch("github_sync.ensure_task_for_issue") as mock_ensure_task:
            # Intermediate comment page (is_last_page=False)
            saved_count = process_and_save_comment_page(
                uid="user_123",
                issue_doc_id="org_repo_10",
                new_comments=new_comments,
                db=mock_db,
                is_last_page=False
            )
            self.assertEqual(saved_count, 1)
            mock_ensure_task.assert_not_called()

            # Final comment page (is_last_page=True) -> Task created!
            saved_count2 = process_and_save_comment_page(
                uid="user_123",
                issue_doc_id="org_repo_10",
                new_comments=new_comments,
                db=mock_db,
                is_last_page=True
            )
            self.assertEqual(saved_count2, 1)
            mock_ensure_task.assert_called_once()


class TestInitialSyncDispatcher(unittest.TestCase):

    @patch("github_sync.enqueue_issue_page_sync")
    def test_start_user_github_sync(self, mock_enqueue):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_db.collection.return_value.document.return_value = mock_user_doc

        user = User(
            uid="user_123",
            github_access_token="gho_token_123",
            monitored_repos=["google/jax", "brianquinlan/marathon2"]
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
            uid="user_discover_1",
            github_access_token="gho_tok_discover",
            github_username=None,
            monitored_repos=[]
        )

        res = start_user_github_sync(user=user, db=mock_db)
        self.assertEqual(res["status"], "enqueued")
        self.assertEqual(user.github_username, "brianquinlan")
        mock_fetch_login.assert_called_once_with("gho_tok_discover")
        mock_user_doc.set.assert_any_call(
            {"github_username": "brianquinlan", "updated_at": ANY},
            merge=True
        )


class TestClosedIssuesSync(unittest.TestCase):

    @patch("github_sync.fetch_single_issue_page")
    def test_sync_closed_issues_for_user_deletes_tasks_and_updates_issue(self, mock_fetch):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_issues_col = MagicMock()
        mock_tasks_col = MagicMock()

        mock_issue_ref = MagicMock()
        mock_issue_snap = MagicMock()
        mock_issue_snap.exists = True
        mock_issue_ref.get.return_value = mock_issue_snap

        mock_task_ref = MagicMock()
        mock_task_snap = MagicMock()
        mock_task_snap.exists = True
        mock_task_ref.get.return_value = mock_task_snap

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.collection.side_effect = lambda col_name: mock_issues_col if col_name == "issues" else mock_tasks_col
        mock_issues_col.document.return_value = mock_issue_ref
        mock_tasks_col.document.return_value = mock_task_ref

        # Return 1 closed issue from GitHub
        mock_fetch.return_value = (
            [
                {
                    "number": 99,
                    "state": "closed",
                    "repository": {"owner": {"login": "brianquinlan"}, "name": "marathon2"}
                }
            ],
            None
        )

        user = User(
            uid="user_closed_1",
            github_access_token="gho_test_closed",
            monitored_repos=["brianquinlan/marathon2"]
        )

        res = sync_closed_issues_for_user(user=user, db=mock_db)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["closed_issues_count"], 1)
        mock_issue_ref.set.assert_called_with({"state": "closed", "updated_at": ANY}, merge=True)
        mock_task_ref.delete.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
