/**
 * Example Firebase Web Client Integration
 * Demonstrates:
 * 1. Signing in with Google and GitHub (OAuth).
 * 2. Calling Firebase Cloud Functions to associate and manage User data.
 * 3. Direct Firestore access from client (authenticated with security rules).
 */

import { initializeApp } from "firebase/app";
import {
  getAuth,
  signInWithPopup,
  GoogleAuthProvider,
  GithubAuthProvider,
  UserCredential
} from "firebase/auth";
import { getFunctions, httpsCallable } from "firebase/functions";
import { getFirestore, doc, getDoc, onSnapshot } from "firebase/firestore";

// 1. Initialize Firebase App
const firebaseConfig = {
  apiKey: "YOUR_API_KEY",
  authDomain: "YOUR_PROJECT.firebaseapp.com",
  projectId: "YOUR_PROJECT_ID",
  storageBucket: "YOUR_PROJECT.appspot.com",
  messagingSenderId: "YOUR_SENDER_ID",
  appId: "YOUR_APP_ID"
};

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const functions = getFunctions(app);
const firestore = getFirestore(app);

// 2. Sign In with GitHub and associate GitHub Access Token & Issue update time
export async function signInGitHubAndAssociate(): Promise<void> {
  const provider = new GithubAuthProvider();
  provider.addScope("repo");
  provider.addScope("read:user");

  const credential: UserCredential = await signInWithPopup(auth, provider);
  
  // Extract the GitHub OAuth credential token if needed
  const githubCredential = GithubAuthProvider.credentialFromResult(credential);
  const githubAccessToken = githubCredential?.accessToken || null;

  console.log("GitHub User signed in:", credential.user.uid);

  // Call the backend Cloud Function to store User data
  await associateUserInfo({
    github_access_token: githubAccessToken,
    last_assigned_issue_update_time: new Date().toISOString(),
    custom_data: {
      github_username: credential.user.displayName,
      preferences: { theme: "dark" }
    }
  });
}

// 3. Sign In with Google
export async function signInGoogleAndAssociate(): Promise<void> {
  const provider = new GoogleAuthProvider();
  const credential = await signInWithPopup(auth, provider);
  console.log("Google User signed in:", credential.user.uid);

  await associateUserInfo({
    github_access_token: null,
    last_assigned_issue_update_time: null,
    custom_data: {
      role: "developer",
      preferences: { theme: "light" }
    }
  });
}

// 4. Call Firebase Cloud Function: associate_user_info
export async function associateUserInfo(userData: {
  github_access_token?: string | null;
  last_assigned_issue_update_time?: string | null;
  custom_data?: Record<string, any>;
}) {
  const associateFunction = httpsCallable(functions, "associate_user_info");
  const result = await associateFunction(userData);
  console.log("Associate response:", result.data);
  return result.data;
}

// 5. Call Firebase Cloud Function: get_user_info
export async function fetchUserInfo() {
  const getInfoFunction = httpsCallable(functions, "get_user_info");
  const result = await getInfoFunction();
  console.log("User Model & Provider Info:", result.data);
  return result.data;
}

// 6. Direct Client Read from Firestore (Permitted by firestore.rules for authenticated user)
export async function listenToCurrentUserDoc(uid: string, callback: (data: any) => void) {
  const userDocRef = doc(firestore, "users", uid);
  return onSnapshot(userDocRef, (snapshot) => {
    if (snapshot.exists()) {
      console.log("Live User Document from Firestore:", snapshot.data());
      callback(snapshot.data());
    }
  });
}
