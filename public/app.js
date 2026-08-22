// Import modular Firebase SDK (v10) via ESM CDN
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js";
import {
  getAuth,
  signInWithPopup,
  signOut,
  onAuthStateChanged,
  GoogleAuthProvider,
  GithubAuthProvider,
  connectAuthEmulator
} from "https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js";
import {
  getFirestore,
  doc,
  onSnapshot,
  connectFirestoreEmulator
} from "https://www.gstatic.com/firebasejs/10.12.2/firebase-firestore.js";
import {
  getFunctions,
  httpsCallable,
  connectFunctionsEmulator
} from "https://www.gstatic.com/firebasejs/10.12.2/firebase-functions.js";

// ============================================================================
// 1. Firebase Configuration & Emulator Connection
// ============================================================================

const isLocalhost = window.location.hostname === "localhost" || 
                    window.location.hostname === "127.0.0.1" || 
                    window.location.hostname.includes("192.168.");

const firebaseConfig = {
  apiKey: "demo-api-key",
  authDomain: "demo-auth-backend.firebaseapp.com",
  projectId: "demo-auth-backend",
  storageBucket: "demo-auth-backend.appspot.com",
  messagingSenderId: "000000000000",
  appId: "1:000000000000:web:000000000000"
};

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const firestore = getFirestore(app);
const functions = getFunctions(app);

// Connect to Local Emulators when running locally
if (isLocalhost) {
  try {
    connectAuthEmulator(auth, "http://127.0.0.1:9099", { disableWarnings: true });
    connectFirestoreEmulator(firestore, "127.0.0.1", 8080);
    connectFunctionsEmulator(functions, "127.0.0.1", 5001);
    console.log("⚡ Connected to Firebase Local Emulators");
  } catch (err) {
    console.warn("Emulators already connected or connection warning:", err);
  }
}

// ============================================================================
// 2. DOM Elements & State
// ============================================================================

let currentUser = null;
let userDocUnsubscribe = null;

// Views
const viewLogin = document.getElementById("view-login");
const viewLanding = document.getElementById("view-landing");
const viewSettings = document.getElementById("view-settings");

// Nav
const navUserProfile = document.getElementById("nav-user-profile");
const navUserAvatar = document.getElementById("nav-user-avatar");
const navUserName = document.getElementById("nav-user-name");
const navProviderBadge = document.getElementById("nav-provider-badge");
const btnLogout = document.getElementById("btn-logout");

// Login buttons
const btnLoginGoogle = document.getElementById("btn-login-google");
const btnLoginGithub = document.getElementById("btn-login-github");

// Landing elements
const landingUserName = document.getElementById("landing-user-name");
const landingProviderName = document.getElementById("landing-provider-name");
const btnGotoSettings = document.getElementById("btn-goto-settings");

// Settings elements
const btnBackToLanding = document.getElementById("btn-back-to-landing");
const inputGithubToken = document.getElementById("input-github-token");
const btnToggleTokenVisibility = document.getElementById("btn-toggle-token-visibility");
const currentTokenDisplay = document.getElementById("current-token-display");
const metaUid = document.getElementById("meta-uid");
const metaEmail = document.getElementById("meta-email");
const metaProvider = document.getElementById("meta-provider");
const metaIssueTime = document.getElementById("meta-issue-time");
const btnSaveToken = document.getElementById("btn-save-token");
const btnRefreshUser = document.getElementById("btn-refresh-user");
const toastContainer = document.getElementById("toast-container");

// ============================================================================
// 3. View Routing Helper
// ============================================================================

function switchView(activeSection) {
  [viewLogin, viewLanding, viewSettings].forEach(view => {
    view.classList.remove("active");
  });
  activeSection.classList.add("active");
}

function showToast(message, type = "success") {
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = (type === "success" ? "✓ " : "✕ ") + message;
  toastContainer.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    setTimeout(() => toast.remove(), 250);
  }, 3500);
}

// ============================================================================
// 4. Authentication & User State
// ============================================================================

onAuthStateChanged(auth, async (user) => {
  currentUser = user;

  if (user) {
    // Determine provider name
    const providerId = user.providerData?.[0]?.providerId || "unknown";
    const isGithub = providerId.includes("github");
    const providerName = isGithub ? "GitHub" : "Google";

    // Update Nav
    navUserProfile.classList.remove("hidden");
    navUserName.textContent = user.displayName || user.email || "Authenticated User";
    navUserAvatar.src = user.photoURL || `https://ui-avatars.com/api/?name=${encodeURIComponent(user.displayName || "User")}&background=6366f1&color=fff`;
    navProviderBadge.textContent = providerName;
    navProviderBadge.className = `provider-badge ${isGithub ? "badge-github" : "badge-google"}`;

    // Update Landing View
    landingUserName.textContent = user.displayName || "User";
    landingProviderName.textContent = providerName;

    // Update Settings Meta
    metaUid.textContent = user.uid;
    metaEmail.textContent = user.email || "No public email";
    metaProvider.textContent = providerName;

    // Initialize backend User association if first sign in
    try {
      const getInfoFn = httpsCallable(functions, "get_user_info");
      await getInfoFn();
    } catch (e) {
      console.log("Auto-sync info:", e.message);
    }

    // Subscribe live to Firestore User Document (users/{uid})
    subscribeToUserDoc(user.uid);

    switchView(viewLanding);
  } else {
    // Clean up
    if (userDocUnsubscribe) {
      userDocUnsubscribe();
      userDocUnsubscribe = null;
    }
    navUserProfile.classList.add("hidden");
    switchView(viewLogin);
  }
});

// Real-time Firestore Document Listener
function subscribeToUserDoc(uid) {
  if (userDocUnsubscribe) {
    userDocUnsubscribe();
  }

  const userDocRef = doc(firestore, "users", uid);
  userDocUnsubscribe = onSnapshot(userDocRef, (snap) => {
    if (snap.exists()) {
      const data = snap.data();
      const token = data.github_access_token;
      
      if (token) {
        // Mask token for security in display
        const masked = token.length > 8 
          ? token.substring(0, 4) + "•".repeat(token.length - 8) + token.substring(token.length - 4)
          : "••••••••";
        currentTokenDisplay.textContent = `${masked} (Length: ${token.length})`;
        currentTokenDisplay.classList.remove("placeholder-text");
      } else {
        currentTokenDisplay.textContent = "None configured";
        currentTokenDisplay.classList.add("placeholder-text");
      }

      // Update issue timestamp
      metaIssueTime.textContent = data.last_assigned_issue_update_time || "None";
    } else {
      currentTokenDisplay.textContent = "No Firestore document yet";
      currentTokenDisplay.classList.add("placeholder-text");
    }
  }, (err) => {
    console.error("Firestore subscription error:", err);
  });
}

// ============================================================================
// 5. User Actions & Event Listeners
// ============================================================================

// Sign In With Google
btnLoginGoogle.addEventListener("click", async () => {
  try {
    const provider = new GoogleAuthProvider();
    await signInWithPopup(auth, provider);
    showToast("Signed in with Google successfully!");
  } catch (err) {
    console.error("Google sign in error:", err);
    showToast(`Google Sign In failed: ${err.message}`, "error");
  }
});

// Sign In With GitHub
btnLoginGithub.addEventListener("click", async () => {
  try {
    const provider = new GithubAuthProvider();
    provider.addScope("repo");
    provider.addScope("read:user");
    
    const result = await signInWithPopup(auth, provider);
    
    // Automatically capture GitHub access token from OAuth result if provided
    const credential = GithubAuthProvider.credentialFromResult(result);
    const token = credential?.accessToken;
    
    if (token) {
      inputGithubToken.value = token;
      // Associate automatically
      const associateFn = httpsCallable(functions, "associate_user_info");
      await associateFn({
        github_access_token: token,
        last_assigned_issue_update_time: new Date().toISOString()
      });
      showToast("Signed in with GitHub and associated GitHub token!");
    } else {
      showToast("Signed in with GitHub successfully!");
    }
  } catch (err) {
    console.error("GitHub sign in error:", err);
    showToast(`GitHub Sign In failed: ${err.message}`, "error");
  }
});

// Sign Out
btnLogout.addEventListener("click", async () => {
  try {
    await signOut(auth);
    showToast("Signed out successfully.");
  } catch (err) {
    showToast(`Sign out error: ${err.message}`, "error");
  }
});

// Navigation: Landing -> Settings
btnGotoSettings.addEventListener("click", () => {
  switchView(viewSettings);
});

// Navigation: Settings -> Landing
btnBackToLanding.addEventListener("click", () => {
  switchView(viewLanding);
});

// Toggle password visibility for token input
btnToggleTokenVisibility.addEventListener("click", () => {
  if (inputGithubToken.type === "password") {
    inputGithubToken.type = "text";
    btnToggleTokenVisibility.textContent = "🔒";
  } else {
    inputGithubToken.type = "password";
    btnToggleTokenVisibility.textContent = "👁️";
  }
});

// Save GitHub Access Token via Cloud Function
btnSaveToken.addEventListener("click", async () => {
  if (!currentUser) return;

  const newToken = inputGithubToken.value.trim();
  if (!newToken) {
    showToast("Please enter a GitHub Access Token.", "error");
    return;
  }

  btnSaveToken.disabled = true;
  btnSaveToken.textContent = "Saving...";

  try {
    const associateFn = httpsCallable(functions, "associate_user_info");
    const response = await associateFn({
      github_access_token: newToken,
      last_assigned_issue_update_time: new Date().toISOString(),
    });

    console.log("Backend response:", response.data);
    showToast("GitHub Access Token saved successfully!");
    inputGithubToken.value = "";
  } catch (err) {
    console.error("Error saving token:", err);
    showToast(`Failed to save token: ${err.message}`, "error");
  } finally {
    btnSaveToken.disabled = false;
    btnSaveToken.textContent = "Save GitHub Access Token";
  }
});

// Refresh from Backend
btnRefreshUser.addEventListener("click", async () => {
  if (!currentUser) return;

  btnRefreshUser.disabled = true;
  btnRefreshUser.textContent = "Fetching...";

  try {
    const getInfoFn = httpsCallable(functions, "get_user_info");
    const res = await getInfoFn();
    console.log("Fetched User from Backend:", res.data);
    showToast("Refreshed user state from backend.");
  } catch (err) {
    showToast(`Refresh error: ${err.message}`, "error");
  } finally {
    btnRefreshUser.disabled = false;
    btnRefreshUser.textContent = "Refresh from Backend";
  }
});
