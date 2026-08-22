"""
Unit tests for functions/github.py
Tests GitHub data structures, single-page issue & comment fetching, pagination chaining,
Firestore persistence, and ensuring Tasks are ONLY created once an Issue and all comments are fully imported.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

# Add functions to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "functions"))

from github import (
    Issue,
    IssueType,
    Comment,
    fetch_single_issue_page,
    fetch_single_comment_page,
    process_and_save_issue_page,
    process_and_save_comment_page,
    start_user_github_sync,
    enqueue_issue_page_sync,
    enqueue_comment_page_sync,
    get_user_stored_issues
)
from user import User


class TestGitHubDataStructures(unittest.TestCase):

    def test_comment_dataclass_and_from_api_dict(self):
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

        d = comment.to_dict()
        self.assertEqual(d["id"], 1001)
        self.assertEqual(d["user_login"], "octocat")

    def test_issue_dataclass_and_properties(self):
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
            association_reasons=["assigned", "created"],
        )
        self.assertEqual(issue.doc_id, "brianquinlan_marathon2_42")
        self.assertEqual(issue.issue_type, IssueType.ISSUE)
        self.assertIn("assigned", issue.association_reasons)


class TestSinglePageFetchingAndPagination(unittest.TestCase):

    @patch("requests.get")
    def test_fetch_single_issue_page_with_next_link(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"number": 1, "title": "Issue 1", "repository": {"owner": {"login": "org"}, "name": "repo"}}
        ]
        mock_resp.links = {"next": {"url": "https://api.github.com/issues?page=2"}}
        mock_get.return_value = mock_resp

        items, next_url = fetch_single_issue_page("https://api.github.com/issues", headers={"Authorization": "Bearer test"})
        self.assertEqual(len(items), 1)
        self.assertEqual(next_url, "https://api.github.com/issues?page=2")

    @patch("requests.get")
    def test_fetch_single_comment_page_with_next_link(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"id": 501, "user": {"login": "reviewer"}, "body": "Looks good"}
        ]
        mock_resp.links = {"next": {"url": "https://api.github.com/comments?page=2"}}
        mock_get.return_value = mock_resp

        comments, next_url = fetch_single_comment_page("https://api.github.com/comments", headers={"Authorization": "Bearer test"})
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].id, 501)
        self.assertEqual(next_url, "https://api.github.com/comments?page=2")


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

        with patch("github.ensure_task_for_issue") as mock_ensure_task:
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

        with patch("github.ensure_task_for_issue") as mock_ensure_task:
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

        with patch("github.ensure_task_for_issue") as mock_ensure_task:
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

    @patch("github.enqueue_issue_page_sync")
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


class TestClosedIssuesSync(unittest.TestCase):

    @patch("github.fetch_single_issue_page")
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

        from github import sync_closed_issues_for_user
        res = sync_closed_issues_for_user(user=user, db=mock_db)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["closed_issues_count"], 1)
        mock_issue_ref.set.assert_called_with({"state": "closed", "updated_at": unittest.mock.ANY}, merge=True)
        mock_task_ref.delete.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
