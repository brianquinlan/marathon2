# Firebase Functions Python Backend (Google & GitHub Auth)

This repository contains a **Firebase Cloud Functions (2nd Generation) Python Backend** that associates and manages user-specific information for users authenticated via **Firebase Authentication**, with first-class support for **Google** (`google.com`) and **GitHub** (`github.com`) authentication providers.

---

## 🌟 Key Features

- **Python 2nd Gen Cloud Functions**: Built on `firebase-functions` (v2) and `firebase-admin`.
- **Google & GitHub Provider Detection**: Automatically detects sign-in provider, linked accounts, and provider-specific IDs.
- **User Data Association**: Associates custom data (biography, preferences, repository links, roles, tags, etc.) with the user's UID in **Cloud Firestore** (`users/{uid}`).
- **Dual Calling Patterns**:
  - **Callable Cloud Functions** (`on_call`): For native web, iOS, Android, and Flutter Firebase SDKs with built-in authenticated context (`req.auth`).
  - **RESTful HTTPS Functions** (`on_request`): For external/cURL integrations via `Authorization: Bearer <ID_TOKEN>`.
- **Local Emulation & Testing**: Ready for offline development using Firebase Local Emulators (Auth, Functions, Firestore).

---

## 📁 Project Structure

```
├── .firebaserc              # Firebase project configuration
├── firebase.json            # Firebase Functions & Emulator settings
├── firestore.rules          # Firestore security rules
├── firestore.indexes.json   # Firestore database index definitions
├── test_backend.py          # Unit & integration test suite
├── functions/
│   ├── main.py              # Cloud Functions (Callable & REST endpoints)
│   ├── auth_utils.py        # Token validation & OAuth provider inspection
│   ├── requirements.txt     # Python runtime dependencies
│   └── venv/                # Local Python virtual environment
└── README.md
```

---

## 🚀 Getting Started

### 1. Prerequisites
- **Python**: 3.10, 3.11, or 3.12+ installed
- **Node.js & Firebase CLI**:
  ```bash
  npm install -g firebase-tools
  ```

### 2. Install Python Dependencies
```bash
python -m venv functions/venv
# Windows:
.\functions\venv\Scripts\pip install -r functions/requirements.txt
# macOS/Linux:
source functions/venv/bin/activate && pip install -r functions/requirements.txt
```

### 3. Run Automated Tests
```bash
.\functions\venv\Scripts\python test_backend.py
```

---

## 🔧 Configuring Google & GitHub Sign-In in Firebase

1. Go to the [Firebase Console](https://console.firebase.google.com/).
2. Select your project and navigate to **Authentication** > **Sign-in method**.
3. **Enable Google**:
   - Turn on Google provider and set your project support email.
4. **Enable GitHub**:
   - Register a new OAuth App on GitHub: [GitHub Developer Settings](https://github.com/settings/developers).
   - Set **Authorization callback URL** to the URL provided in the Firebase Console (e.g. `https://<PROJECT_ID>.firebaseapp.com/__/auth/handler`).
   - Copy GitHub **Client ID** and **Client Secret** into the Firebase Authentication GitHub provider settings.

---

## 💻 Local Emulators

You can run and test everything locally without deploying to Google Cloud:

```bash
firebase emulators:start
```

- **Hosting (Web UI)**: [http://localhost:5000](http://localhost:5000)
- **Emulator UI Suite**: [http://localhost:4000](http://localhost:4000)
- **Auth Emulator**: `http://localhost:9099`
- **Firestore Emulator**: `http://localhost:8080`
- **Functions Emulator**: `http://localhost:5001`

---

## 🔌 API & Callable Function Reference

### 1. `associate_user_info` (Callable)
Associates or updates data for the authenticated user in Firestore (`users/{uid}`).

#### Web / JavaScript SDK Example:
```typescript
import { initializeApp } from "firebase/app";
import { getAuth, signInWithPopup, GoogleAuthProvider, GithubAuthProvider } from "firebase/auth";
import { getFunctions, httpsCallable } from "firebase/functions";

const firebaseConfig = { /* Your Config */ };
const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const functions = getFunctions(app);

// 1. Sign in with GitHub or Google
const provider = new GithubAuthProvider(); // or new GoogleAuthProvider()
const userCredential = await signInWithPopup(auth, provider);

// 2. Call backend function to associate custom data
const associateUserInfo = httpsCallable(functions, "associate_user_info");
const response = await associateUserInfo({
  associated_data: {
    bio: "AI Engineer & Open Source Contributor",
    github_handle: "octocat",
    skills: ["Python", "Firebase", "TypeScript"],
    preferences: {
      newsletter: true,
      theme: "dark"
    }
  }
});

console.log(response.data);
```

### 2. `get_user_info` (Callable)
Fetches the user's stored Firestore document and verified provider metadata.

```typescript
const getUserInfo = httpsCallable(functions, "get_user_info");
const result = await getUserInfo();
console.log(result.data.profile);
console.log("Signed in with:", result.data.auth_provider.primary_provider_name);
```

### 3. `sync_auth_profile` (Callable)
Synchronizes the latest Firebase Auth user record details (avatar photo, display name, email verification) into Firestore.

```typescript
const syncProfile = httpsCallable(functions, "sync_auth_profile");
const result = await syncProfile();
```

### 4. `delete_user_info` (Callable)
Deletes the user's associated data.

```typescript
const deleteUserInfo = httpsCallable(functions, "delete_user_info");
const result = await deleteUserInfo();
```

### 5. `user_api` (REST Endpoint)
For external clients, servers, and non-Firebase SDK environments using Firebase ID Tokens.

```bash
# 1. Get user profile
curl -X GET "https://<REGION>-<PROJECT_ID>.cloudfunctions.net/user_api" \
  -H "Authorization: Bearer <FIREBASE_ID_TOKEN>"

# 2. Associate information
curl -X POST "https://<REGION>-<PROJECT_ID>.cloudfunctions.net/user_api" \
  -H "Authorization: Bearer <FIREBASE_ID_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"associated_data": {"role": "developer", "location": "San Francisco"}}'

# 3. Delete associated info
curl -X DELETE "https://<REGION>-<PROJECT_ID>.cloudfunctions.net/user_api" \
  -H "Authorization: Bearer <FIREBASE_ID_TOKEN>"
```

---

## 🗄️ Firestore Data Schema

User data is stored at path `users/{uid}` with the following structure:

```json
{
  "uid": "gh_user_abc123",
  "email": "user@domain.com",
  "email_verified": true,
  "display_name": "Jane Doe",
  "photo_url": "https://avatars.githubusercontent.com/u/...",
  "auth_provider": {
    "primary_provider": "github.com",
    "primary_provider_name": "GitHub",
    "is_google": false,
    "is_github": true,
    "linked_providers": ["github.com"],
    "github_id": "12345678",
    "google_id": null
  },
  "associated_data": {
    "bio": "Software Engineer",
    "preferences": {
      "theme": "dark"
    }
  },
  "created_at": "2026-08-22T08:00:00Z",
  "updated_at": "2026-08-22T08:30:00Z"
}
```

---

## 🚢 Deployment

To deploy to your live Firebase project:

1. Log in to Firebase:
   ```bash
   firebase login
   ```
2. Select your Firebase project:
   ```bash
   firebase use <your-project-id>
   ```
3. Deploy functions and Firestore rules:
   ```bash
   firebase deploy --only functions,firestore
   ```
