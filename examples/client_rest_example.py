"""
Example Python REST client for Firebase Functions backend.
Demonstrates calling the backend REST endpoint with a Firebase ID token.
"""

import requests

FUNCTION_BASE_URL = "http://127.0.0.1:5001/demo-auth-backend/us-central1/user_api"
# Or deployed URL: https://<REGION>-<PROJECT_ID>.cloudfunctions.net/user_api


def associate_user_info(id_token: str, user_data: dict):
    headers = {"Authorization": f"Bearer {id_token}", "Content-Type": "application/json"}
    payload = {"associated_data": user_data}
    response = requests.post(FUNCTION_BASE_URL, headers=headers, json=payload)
    return response.status_code, response.json()


def get_user_info(id_token: str):
    headers = {"Authorization": f"Bearer {id_token}"}
    response = requests.get(FUNCTION_BASE_URL, headers=headers)
    return response.status_code, response.json()


def delete_user_info(id_token: str):
    headers = {"Authorization": f"Bearer {id_token}"}
    response = requests.delete(FUNCTION_BASE_URL, headers=headers)
    return response.status_code, response.json()


if __name__ == "__main__":
    print("Replace <MOCK_OR_REAL_ID_TOKEN> with an actual Firebase ID token.")
    # Example usage:
    # token = "eyJhbGci..."
    # status, data = associate_user_info(token, {"bio": "Pythonista", "github_handle": "coder"})
    # print(status, data)
