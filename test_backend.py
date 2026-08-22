"""
Comprehensive Unit and Integration Tests for Firebase Functions Python Backend
Tests User dataclass, Google and GitHub authentication provider extraction, data association logic, and REST handlers.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch
import json

# Ensure functions module is in sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "functions"))

from auth_utils import extract_provider_info, verify_bearer_token, fetch_full_user_auth_record
from user import User
from firebase_functions import https_fn
import main


def get_callable_handler(func):
    """Helper to extract the original callable handler function from Firebase decorators."""
    if hasattr(func, "__wrapped__") and func.__wrapped__.__closure__:
        for cell in func.__wrapped__.__closure__:
            if callable(cell.cell_contents):
                return cell.cell_contents
    return func


class TestUserModel(unittest.TestCase):

    def test_user_dataclass_defaults_and_fields(self):
        user = User(
            github_access_token="gho_test_token_123",
            last_assigned_issue_update_time="2026-08-22T08:00:00Z",
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
        self.assertEqual(user.primary_provider, "github.com")
        self.assertTrue(user.email_verified)

    def test_user_to_dict_and_from_dict(self):
        user = User(
            uid="user_999",
            email="google@domain.com",
            github_access_token="gho_xyz",
            last_assigned_issue_update_time="2026-08-22T10:30:00Z",
            custom_data={"role": "maintainer"}
        )
        data = user.to_dict(for_firestore=False)
        self.assertEqual(data["uid"], "user_999")
        self.assertEqual(data["github_access_token"], "gho_xyz")
        self.assertEqual(data["last_assigned_issue_update_time"], "2026-08-22T10:30:00Z")
        self.assertEqual(data["custom_data"]["role"], "maintainer")

        # Reconstruct from dict
        reconstructed = User.from_dict(data)
        self.assertEqual(reconstructed.uid, user.uid)
        self.assertEqual(reconstructed.github_access_token, "gho_xyz")
        self.assertEqual(reconstructed.last_assigned_issue_update_time, "2026-08-22T10:30:00Z")
        self.assertEqual(reconstructed.custom_data["role"], "maintainer")

    def test_user_from_auth_token(self):
        token_dict = {
            "uid": "gh_user_555",
            "email": "octocat@github.com",
            "email_verified": True,
            "name": "Mona Lisa Octocat",
            "picture": "https://avatars.githubusercontent.com/u/583231"
        }
        provider_info = {
            "primary_provider": "github.com",
            "github_id": "583231",
            "google_id": None,
            "linked_providers": ["github.com"]
        }

        user = User.from_auth_token(
            token_dict=token_dict,
            provider_info=provider_info,
            github_access_token="gho_secret123",
            last_assigned_issue_update_time="2026-08-22T12:00:00Z"
        )
        self.assertEqual(user.uid, "gh_user_555")
        self.assertEqual(user.primary_provider, "github.com")
        self.assertEqual(user.github_id, "583231")
        self.assertEqual(user.github_access_token, "gho_secret123")
        self.assertEqual(user.last_assigned_issue_update_time, "2026-08-22T12:00:00Z")


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
        self.assertEqual(info["google_id"], "google-sub-id-987")
        self.assertIsNone(info["github_id"])

    def test_extract_github_provider_info(self):
        token = {
            "uid": "github-user-456",
            "email": "dev@octocat.com",
            "email_verified": True,
            "name": "Octo Cat",
            "picture": "https://avatars.githubusercontent.com/u/12345",
            "firebase": {
                "sign_in_provider": "github.com",
                "identities": {
                    "github.com": ["github-sub-id-54321"],
                    "email": ["dev@octocat.com"]
                }
            }
        }

        info = extract_provider_info(token)
        self.assertEqual(info["primary_provider"], "github.com")
        self.assertEqual(info["primary_provider_name"], "GitHub")
        self.assertFalse(info["is_google"])
        self.assertTrue(info["is_github"])
        self.assertEqual(info["github_id"], "github-sub-id-54321")
        self.assertIsNone(info["google_id"])

    def test_extract_linked_multiple_providers(self):
        token = {
            "uid": "multi-auth-789",
            "email": "poweruser@example.com",
            "firebase": {
                "sign_in_provider": "google.com",
                "identities": {
                    "google.com": ["g-100"],
                    "github.com": ["gh-200"]
                }
            }
        }
        info = extract_provider_info(token)
        self.assertEqual(info["primary_provider"], "google.com")
        self.assertTrue(info["is_google"])
        self.assertTrue(info["is_github"])
        self.assertEqual(len(info["supported_linked"]), 2)


class TestBearerTokenValidation(unittest.TestCase):

    def test_missing_header(self):
        with self.assertRaises(https_fn.HttpsError) as ctx:
            verify_bearer_token(None)
        self.assertEqual(ctx.exception.code, https_fn.FunctionsErrorCode.UNAUTHENTICATED)

    def test_invalid_header_format(self):
        with self.assertRaises(https_fn.HttpsError) as ctx:
            verify_bearer_token("Basic 123456")
        self.assertEqual(ctx.exception.code, https_fn.FunctionsErrorCode.UNAUTHENTICATED)

    @patch("auth_utils.auth.verify_id_token")
    def test_valid_bearer_token(self, mock_verify):
        mock_verify.return_value = {
            "uid": "test-user-1",
            "email": "test@example.com",
            "firebase": {"sign_in_provider": "google.com", "identities": {}}
        }
        result = verify_bearer_token("Bearer valid_token_abc")
        self.assertEqual(result["uid"], "test-user-1")
        mock_verify.assert_called_once_with("valid_token_abc")


class TestCallableFunctionLogic(unittest.TestCase):

    @patch("main.db")
    def test_associate_user_info_callable(self, mock_db):
        handler = get_callable_handler(main.associate_user_info)

        mock_doc_ref = MagicMock()
        mock_doc_snap = MagicMock()
        mock_doc_snap.exists = False
        mock_doc_ref.get.return_value = mock_doc_snap
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        # Mock request object
        mock_req = MagicMock(spec=https_fn.CallableRequest)
        mock_req.auth = MagicMock()
        mock_req.auth.uid = "user_github_001"
        mock_req.auth.token = {
            "email": "user@github.com",
            "name": "GitHub Dev",
            "picture": "https://avatars.github.com/1",
            "firebase": {"sign_in_provider": "github.com", "identities": {"github.com": ["12345"]}}
        }
        mock_req.data = {
            "github_access_token": "gho_sample_token_xyz",
            "last_assigned_issue_update_time": "2026-08-22T08:30:00Z",
            "custom_data": {
                "bio": "Full-stack developer",
                "skills": ["python", "firebase", "typescript"]
            }
        }

        result = handler(mock_req)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["provider"], "GitHub")
        self.assertEqual(result["user"]["github_access_token"], "gho_sample_token_xyz")
        self.assertEqual(result["user"]["last_assigned_issue_update_time"], "2026-08-22T08:30:00Z")
        self.assertEqual(result["user"]["custom_data"]["bio"], "Full-stack developer")
        mock_doc_ref.set.assert_called_once()

    def test_unauthenticated_call_raises_error(self):
        handler = get_callable_handler(main.associate_user_info)
        mock_req = MagicMock(spec=https_fn.CallableRequest)
        mock_req.auth = None

        with self.assertRaises(https_fn.HttpsError) as ctx:
            handler(mock_req)
        self.assertEqual(ctx.exception.code, https_fn.FunctionsErrorCode.UNAUTHENTICATED)

    @patch("main.db")
    def test_get_user_info_callable(self, mock_db):
        handler = get_callable_handler(main.get_user_info)

        mock_doc_ref = MagicMock()
        mock_doc_snap = MagicMock()
        mock_doc_snap.exists = True
        mock_doc_snap.to_dict.return_value = {
            "uid": "google_user_002",
            "email": "google@example.com",
            "github_access_token": "gho_stored_123",
            "last_assigned_issue_update_time": "2026-08-22T09:00:00Z",
            "custom_data": {"theme": "dark"}
        }
        mock_doc_ref.get.return_value = mock_doc_snap
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        mock_req = MagicMock(spec=https_fn.CallableRequest)
        mock_req.auth = MagicMock()
        mock_req.auth.uid = "google_user_002"
        mock_req.auth.token = {
            "email": "google@example.com",
            "name": "Google User",
            "firebase": {"sign_in_provider": "google.com", "identities": {"google.com": ["999"]}}
        }

        result = handler(mock_req)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["user"]["github_access_token"], "gho_stored_123")
        self.assertEqual(result["user"]["last_assigned_issue_update_time"], "2026-08-22T09:00:00Z")
        self.assertEqual(result["user"]["custom_data"]["theme"], "dark")

    @patch("main.fetch_full_user_auth_record")
    @patch("main.db")
    def test_sync_auth_profile(self, mock_db, mock_fetch_auth):
        handler = get_callable_handler(main.sync_auth_profile)

        mock_doc_ref = MagicMock()
        mock_doc_snap = MagicMock()
        mock_doc_snap.exists = True
        mock_doc_snap.to_dict.return_value = {
            "uid": "sync_user_003",
            "github_access_token": "preserved_token"
        }
        mock_doc_ref.get.return_value = mock_doc_snap
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        mock_fetch_auth.return_value = {
            "uid": "sync_user_003",
            "email": "sync@example.com",
            "email_verified": True,
            "display_name": "Synced User",
            "photo_url": "https://photo.url",
            "providers": [{"provider_id": "google.com", "provider_name": "Google"}]
        }

        mock_req = MagicMock(spec=https_fn.CallableRequest)
        mock_req.auth = MagicMock()
        mock_req.auth.uid = "sync_user_003"
        mock_req.auth.token = {
            "firebase": {"sign_in_provider": "google.com", "identities": {}}
        }

        result = handler(mock_req)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["user"]["display_name"], "Synced User")
        self.assertEqual(result["user"]["github_access_token"], "preserved_token")
        mock_doc_ref.set.assert_called_once()

    @patch("main.db")
    def test_delete_user_info_callable(self, mock_db):
        handler = get_callable_handler(main.delete_user_info)

        mock_doc_ref = MagicMock()
        mock_doc_snap = MagicMock()
        mock_doc_snap.exists = True
        mock_doc_ref.get.return_value = mock_doc_snap
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        mock_req = MagicMock(spec=https_fn.CallableRequest)
        mock_req.auth = MagicMock()
        mock_req.auth.uid = "delete_target_001"

        result = handler(mock_req)
        self.assertEqual(result["status"], "success")
        mock_doc_ref.delete.assert_called_once()


class TestRestEndpoint(unittest.TestCase):

    @patch("main.db")
    @patch("main.verify_bearer_token")
    def test_user_api_get_post_delete(self, mock_verify_token, mock_db):
        raw_api = main.user_api.__wrapped__

        mock_verify_token.return_value = {
            "uid": "rest_user_1",
            "email": "rest@example.com",
            "name": "REST User",
            "firebase": {"sign_in_provider": "google.com", "identities": {"google.com": ["111"]}}
        }

        mock_doc_ref = MagicMock()
        mock_doc_snap = MagicMock()
        mock_doc_snap.exists = True
        mock_doc_snap.to_dict.return_value = {
            "uid": "rest_user_1",
            "github_access_token": "rest_token_abc",
            "last_assigned_issue_update_time": "2026-08-22T08:45:00Z",
            "custom_data": {"role": "admin"}
        }
        mock_doc_ref.get.return_value = mock_doc_snap
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        # 1. Test OPTIONS (CORS preflight)
        mock_req_opt = MagicMock()
        mock_req_opt.method = "OPTIONS"
        resp_opt = raw_api(mock_req_opt)
        self.assertEqual(resp_opt.status_code, 204)

        # 2. Test GET
        mock_req_get = MagicMock()
        mock_req_get.method = "GET"
        mock_req_get.headers = {"Authorization": "Bearer token_xyz"}

        resp_get = raw_api(mock_req_get)
        self.assertEqual(resp_get.status_code, 200)
        body = json.loads(resp_get.response[0].decode("utf-8") if isinstance(resp_get.response[0], bytes) else resp_get.response[0])
        self.assertEqual(body["uid"], "rest_user_1")
        self.assertEqual(body["user"]["github_access_token"], "rest_token_abc")

        # 3. Test POST
        mock_req_post = MagicMock()
        mock_req_post.method = "POST"
        mock_req_post.headers = {"Authorization": "Bearer token_xyz"}
        mock_req_post.get_json.return_value = {
            "github_access_token": "updated_token_123",
            "last_assigned_issue_update_time": "2026-08-22T09:00:00Z"
        }

        resp_post = raw_api(mock_req_post)
        self.assertEqual(resp_post.status_code, 200)
        body_post = json.loads(resp_post.response[0].decode("utf-8") if isinstance(resp_post.response[0], bytes) else resp_post.response[0])
        self.assertEqual(body_post["status"], "success")
        self.assertEqual(body_post["user"]["github_access_token"], "updated_token_123")

        # 4. Test DELETE
        mock_req_del = MagicMock()
        mock_req_del.method = "DELETE"
        mock_req_del.headers = {"Authorization": "Bearer token_xyz"}

        resp_del = raw_api(mock_req_del)
        self.assertEqual(resp_del.status_code, 200)
        body_del = json.loads(resp_del.response[0].decode("utf-8") if isinstance(resp_del.response[0], bytes) else resp_del.response[0])
        self.assertEqual(body_del["status"], "success")
        mock_doc_ref.delete.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
