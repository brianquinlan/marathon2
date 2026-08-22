"""
Comprehensive unit tests for functions/task.py
Tests Task dataclass, issue reference handling, priority_needs_updated flag management,
ranker execution, asynchronous Firebase task_queue enqueuing, and decoupled forced reranking.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

# Add functions to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "functions"))

from task import (
    Task,
    run_ranker,
    ensure_task_for_issue,
    update_needed_priorities,
    force_rerank_tasks,
    enqueue_task_ranking,
    get_user_tasks
)
from firebase_functions import tasks_fn
import main


def get_callable_handler(func):
    """Helper to extract the original callable / task handler function from Firebase decorators."""
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


class TestTaskModel(unittest.TestCase):

    def test_task_model_defaults_properties_and_json_serialization(self):
        task = Task(
            id="task_owner_repo_1",
            priority=0.85,
            priority_needs_updated=True,
            issue_id="owner_repo_1",
            uid="user_123",
            title="Fix high priority bug",
            issue_url="https://github.com/owner/repo/issues/1"
        )
        self.assertEqual(task.doc_id, "task_owner_repo_1")
        self.assertEqual(task.priority, 0.85)
        self.assertTrue(task.priority_needs_updated)
        self.assertEqual(task.issue_id, "owner_repo_1")

        # Test Pydantic JSON serialization
        json_str = task.model_dump_json()
        self.assertIn('"priority":0.85', json_str.replace(" ", ""))
        self.assertIn('"title":"Fix high priority bug"', json_str)
        
        # Test dictionary conversion via model_dump
        dumped = task.model_dump()
        self.assertEqual(dumped["id"], "task_owner_repo_1")
        self.assertEqual(dumped["priority"], 0.85)
        self.assertTrue(dumped["priority_needs_updated"])
        self.assertEqual(dumped["title"], "Fix high priority bug")

        # Test reconstruction via model_validate
        reconstructed = Task.model_validate(dumped)
        self.assertEqual(reconstructed.doc_id, task.doc_id)
        self.assertEqual(reconstructed.priority, 0.85)
        self.assertTrue(reconstructed.priority_needs_updated)

    def test_task_with_firestore_document_reference(self):
        class FakeDocRef:
            path = "users/user_123/issues/owner_repo_1"

        mock_ref = FakeDocRef()

        task = Task(
            issue_id="owner_repo_1",
            issue_ref=mock_ref,
            uid="user_123"
        )
        self.assertEqual(task.doc_id, "task_owner_repo_1")

        d_dump = task.model_dump()
        self.assertIn("issue_ref", d_dump)
        self.assertEqual(d_dump["issue_ref"], mock_ref)


class TestRankerEngine(unittest.TestCase):

    def test_run_ranker_resets_priority_needs_updated(self):
        t1 = Task(id="t1", priority=0.0, priority_needs_updated=True)
        t2 = Task(id="t2", priority=0.5, priority_needs_updated=True)

        ranked = run_ranker([t1, t2])
        self.assertEqual(len(ranked), 2)
        self.assertFalse(ranked[0].priority_needs_updated)
        self.assertFalse(ranked[1].priority_needs_updated)


class TestTaskFirestoreOperations(unittest.TestCase):

    def test_ensure_task_for_issue_creation_and_update(self):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_tasks_col = MagicMock()
        mock_task_ref = MagicMock()
        mock_doc_snap = MagicMock()

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.collection.return_value = mock_tasks_col
        mock_tasks_col.document.return_value = mock_task_ref

        # Case 1: Task does not exist yet (create)
        mock_doc_snap.exists = False
        mock_task_ref.get.return_value = mock_doc_snap

        task = ensure_task_for_issue(
            uid="user_100",
            issue_id="org_repo_1",
            issue_data={"title": "Brand new issue", "url": "https://github.com/org/repo/issues/1"},
            db=mock_db
        )
        self.assertEqual(task.doc_id, "task_org_repo_1")
        self.assertTrue(task.priority_needs_updated)
        self.assertEqual(task.title, "Brand new issue")
        mock_task_ref.set.assert_called_once()

        # Case 2: Task already exists (modified issue -> sets priority_needs_updated = True)
        mock_doc_snap.exists = True
        mock_doc_snap.to_dict.return_value = {
            "id": "task_org_repo_1",
            "priority": 0.7,
            "priority_needs_updated": False,
            "issue_id": "org_repo_1",
            "title": "Old title"
        }
        mock_task_ref.set.reset_mock()

        updated_task = ensure_task_for_issue(
            uid="user_100",
            issue_id="org_repo_1",
            issue_data={"title": "Updated issue title", "url": "https://github.com/org/repo/issues/1"},
            db=mock_db
        )
        self.assertTrue(updated_task.priority_needs_updated)
        self.assertEqual(updated_task.title, "Updated issue title")
        self.assertEqual(updated_task.priority, 0.7)
        mock_task_ref.set.assert_called_once()

    def test_update_needed_priorities(self):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_tasks_col = MagicMock()
        mock_batch = MagicMock()

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.collection.return_value = mock_tasks_col
        mock_db.batch.return_value = mock_batch

        # Mock query where priority_needs_updated == True
        mock_doc1 = MagicMock()
        mock_doc1.id = "task_1"
        mock_doc1.to_dict.return_value = {
            "id": "task_1",
            "priority": 0.0,
            "priority_needs_updated": True,
            "title": "Needs rank"
        }
        mock_doc1.reference = MagicMock()

        mock_tasks_col.where.return_value.stream.return_value = [mock_doc1]

        result = update_needed_priorities("user_100", mock_db)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["updated_count"], 1)
        mock_batch.commit.assert_called_once()

    @patch("task.enqueue_task_ranking")
    def test_force_rerank_tasks_marks_and_enqueues(self, mock_enqueue):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_tasks_col = MagicMock()
        mock_batch = MagicMock()

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.collection.return_value = mock_tasks_col
        mock_db.batch.return_value = mock_batch

        mock_doc1 = MagicMock()
        mock_doc1.id = "task_a"
        mock_doc1.to_dict.return_value = {"id": "task_a", "priority": 0.4}
        mock_doc1.reference = MagicMock()

        mock_tasks_col.stream.return_value = [mock_doc1]
        mock_enqueue.return_value = {"status": "enqueued", "uid": "user_100"}

        result = force_rerank_tasks("user_100", mock_db)
        self.assertEqual(result["status"], "enqueued")
        self.assertEqual(result["marked_count"], 1)
        mock_enqueue.assert_called_once_with(uid="user_100", function_name="rank_user_tasks", db=mock_db)

    @patch("firebase_admin.functions.task_queue")
    def test_enqueue_task_ranking_with_firebase_admin(self, mock_task_queue):
        mock_queue = MagicMock()
        mock_queue.enqueue.return_value = "task_id_xyz_123"
        mock_task_queue.return_value = mock_queue

        res = enqueue_task_ranking(uid="user_task_queue_1", function_name="rank_user_tasks")
        self.assertEqual(res["status"], "enqueued")
        self.assertEqual(res["task_id"], "task_id_xyz_123")
        self.assertEqual(res["queue"], "rank_user_tasks")
        self.assertEqual(res["uid"], "user_task_queue_1")

        mock_task_queue.assert_called_once_with("rank_user_tasks")
        mock_queue.enqueue.assert_called_once()
        args, kwargs = mock_queue.enqueue.call_args
        self.assertEqual(args[0], {"uid": "user_task_queue_1"})

    def test_enqueue_task_ranking_fallback_dispatch(self):
        mock_db = MagicMock()
        res = enqueue_task_ranking(uid="user_async_1", db=mock_db)
        self.assertEqual(res["status"], "enqueued")
        self.assertEqual(res["uid"], "user_async_1")

    def test_get_user_tasks_sorted_by_priority(self):
        mock_db = MagicMock()
        mock_user_doc = MagicMock()
        mock_tasks_col = MagicMock()

        mock_db.collection.return_value.document.return_value = mock_user_doc
        mock_user_doc.collection.return_value = mock_tasks_col

        doc_low = MagicMock()
        doc_low.id = "task_low"
        doc_low.to_dict.return_value = {"id": "task_low", "priority": 0.2, "priority_needs_updated": False}

        doc_high = MagicMock()
        doc_high.id = "task_high"
        doc_high.to_dict.return_value = {"id": "task_high", "priority": 0.9, "priority_needs_updated": False}

        mock_tasks_col.limit.return_value.stream.return_value = [doc_low, doc_high]

        tasks = get_user_tasks("user_100", mock_db)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["id"], "task_high")
        self.assertEqual(tasks[1]["id"], "task_low")


class TestTaskQueueFunction(unittest.TestCase):

    @patch("main.update_needed_priorities")
    @patch("main.db")
    def test_rank_user_tasks_task_queue_handler(self, mock_db, mock_update_fn):
        handler = get_callable_handler(main.rank_user_tasks)
        mock_update_fn.return_value = {
            "status": "success",
            "updated_count": 4,
            "message": "Updated priorities for 4 tasks."
        }

        mock_req = MagicMock(spec=tasks_fn.CallableRequest)
        mock_req.data = {"uid": "user_queue_001"}

        result = handler(mock_req)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["updated_count"], 4)
        mock_update_fn.assert_called_once_with(uid="user_queue_001", db=mock_db)

    def test_rank_user_tasks_missing_uid_raises_error(self):
        handler = get_callable_handler(main.rank_user_tasks)
        mock_req = MagicMock(spec=tasks_fn.CallableRequest)
        mock_req.data = {}

        with self.assertRaises(tasks_fn.HttpsError) as ctx:
            handler(mock_req)
        self.assertEqual(ctx.exception.code, tasks_fn.FunctionsErrorCode.INVALID_ARGUMENT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
