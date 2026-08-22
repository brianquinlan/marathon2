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
  collection,
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
let issuesColUnsubscribe = null;
let tasksColUnsubscribe = null;

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
const btnSyncIssuesLanding = document.getElementById("btn-sync-issues-landing");
const btnForceRerank = document.getElementById("btn-force-rerank");
const tasksCount = document.getElementById("tasks-count");
const tasksList = document.getElementById("tasks-list");
const issuesCount = document.getElementById("issues-count");
const issuesList = document.getElementById("issues-list");

// Settings elements
const btnBackToLanding = document.getElementById("btn-back-to-landing");
const inputGithubToken = document.getElementById("input-github-token");
const btnToggleTokenVisibility = document.getElementById("btn-toggle-token-visibility");
const currentTokenDisplay = document.getElementById("current-token-display");
const inputMonitoredRepos = document.getElementById("input-monitored-repos");
const metaUid = document.getElementById("meta-uid");
const metaEmail = document.getElementById("meta-email");
const metaProvider = document.getElementById("meta-provider");
const metaIssueTime = document.getElementById("meta-issue-time");
const btnSaveSettings = document.getElementById("btn-save-settings");
const btnSyncIssuesSettings = document.getElementById("btn-sync-issues-settings");
const toastContainer = document.getElementById("toast-container");

// ============================================================================
// 3. View Routing & Toast Helpers
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

function escapeHtml(str) {
  if (!str) return "";
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ============================================================================
// 4. Authentication & Real-time Subscriptions
// ============================================================================

onAuthStateChanged(auth, async (user) => {
  currentUser = user;

  if (user) {
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

    // Subscribe live to Firestore User Document, Issues, and Tasks
    subscribeToUserDoc(user.uid);
    subscribeToIssues(user.uid);
    subscribeToTasks(user.uid);

    switchView(viewLanding);
  } else {
    // Clean up subscriptions
    if (userDocUnsubscribe) {
      userDocUnsubscribe();
      userDocUnsubscribe = null;
    }
    if (issuesColUnsubscribe) {
      issuesColUnsubscribe();
      issuesColUnsubscribe = null;
    }
    if (tasksColUnsubscribe) {
      tasksColUnsubscribe();
      tasksColUnsubscribe = null;
    }
    navUserProfile.classList.add("hidden");
    switchView(viewLogin);
  }
});

// Real-time Firestore Document Listener for User Profile
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
        const masked = token.length > 8 
          ? token.substring(0, 4) + "•".repeat(token.length - 8) + token.substring(token.length - 4)
          : "••••••••";
        currentTokenDisplay.textContent = `${masked} (Length: ${token.length})`;
        currentTokenDisplay.classList.remove("placeholder-text");
      } else {
        currentTokenDisplay.textContent = "None configured";
        currentTokenDisplay.classList.add("placeholder-text");
      }

      const repos = data.monitored_repos || [];
      if (document.activeElement !== inputMonitoredRepos) {
        inputMonitoredRepos.value = repos.join(", ");
      }

      metaIssueTime.textContent = data.last_assigned_issue_update_time || "Never synced";
    }
  }, (err) => {
    console.error("Firestore user subscription error:", err);
  });
}

// Real-time Firestore Subcollection Listener for Issues (users/{uid}/issues)
function subscribeToIssues(uid) {
  if (issuesColUnsubscribe) {
    issuesColUnsubscribe();
  }

  const issuesColRef = collection(firestore, "users", uid, "issues");
  issuesColUnsubscribe = onSnapshot(issuesColRef, (snapshot) => {
    const issues = [];
    snapshot.forEach(docSnap => {
      issues.push(docSnap.data());
    });
    renderIssuesList(issues);
  }, (err) => {
    console.error("Firestore issues subscription error:", err);
  });
}

// Real-time Firestore Subcollection Listener for Tasks (users/{uid}/tasks)
function subscribeToTasks(uid) {
  if (tasksColUnsubscribe) {
    tasksColUnsubscribe();
  }

  const tasksColRef = collection(firestore, "users", uid, "tasks");
  tasksColUnsubscribe = onSnapshot(tasksColRef, (snapshot) => {
    const tasks = [];
    snapshot.forEach(docSnap => {
      tasks.push(docSnap.data());
    });
    
    // Sort from HIGHEST to LOWEST priority (1.0 -> 0.0)
    tasks.sort((a, b) => {
      const pA = typeof a.priority === "number" ? a.priority : 0.0;
      const pB = typeof b.priority === "number" ? b.priority : 0.0;
      return pB - pA;
    });

    renderTasksList(tasks);
  }, (err) => {
    console.error("Firestore tasks subscription error:", err);
  });
}

// Render Task Cards ordered from highest to lowest priority
function renderTasksList(tasks) {
  tasksCount.textContent = tasks.length;

  if (tasks.length === 0) {
    tasksList.innerHTML = `<p class="empty-state">No tasks created yet. Click "Sync from GitHub" below to generate tasks for your issues.</p>`;
    return;
  }

  tasksList.innerHTML = "";

  tasks.forEach((task, index) => {
    const card = document.createElement("div");
    card.className = "task-card";

    const needsUpdate = task.priority_needs_updated === true;
    const statusBadgeClass = needsUpdate ? "badge-needs-rank" : "badge-ranked";
    const statusBadgeText = needsUpdate ? "Needs Rerank" : "Ranked";
    
    const rawPriority = typeof task.priority === "number" ? task.priority : 0.0;
    const priorityVal = rawPriority.toFixed(2);
    const meterPercent = Math.min(100, Math.max(0, Math.round(rawPriority * 100)));

    const rankNum = `#${index + 1}`;
    const issueRefText = task.issue_id ? task.issue_id.replace(/_/g, " / ") : "Issue";

    card.innerHTML = `
      <div class="task-card-header">
        <div class="task-rank-title-group">
          <span class="task-rank-number" title="Priority Rank">${rankNum}</span>
          <a href="${task.issue_url || '#'}" target="_blank" rel="noopener noreferrer" class="task-title">
            ${escapeHtml(task.title || task.issue_id || "Untitled Task")}
          </a>
        </div>
        <div class="task-badge-group">
          <div class="priority-tag">
            <span class="priority-label">Priority</span>
            <span class="priority-value">${priorityVal}</span>
          </div>
          <span class="status-badge ${statusBadgeClass}">${statusBadgeText}</span>
        </div>
      </div>
      
      <div class="priority-meter-container" title="Priority: ${priorityVal}">
        <div class="priority-meter-fill" style="width: ${meterPercent}%;"></div>
      </div>

      <div class="task-card-footer">
        <span class="task-issue-ref">${escapeHtml(issueRefText)}</span>
        <span>${needsUpdate ? "⚠️ Re-rank required" : "✓ Priority up to date"}</span>
      </div>
    `;

    tasksList.appendChild(card);
  });
}

// Render Issue Cards
function renderIssuesList(issues) {
  issuesCount.textContent = issues.length;

  if (issues.length === 0) {
    issuesList.innerHTML = `<p class="empty-state">No issues stored in Firestore yet. Configure your GitHub token and click "Sync from GitHub".</p>`;
    return;
  }

  issuesList.innerHTML = "";

  issues.forEach(issue => {
    const card = document.createElement("div");
    card.className = "issue-card";

    const reasons = issue.association_reasons || ["assigned"];
    const badgesHtml = reasons.map(r => {
      const label = r.replace("_", " ");
      return `<span class="reason-badge reason-${r}">${label}</span>`;
    }).join("");

    const commentsCount = (issue.comments || []).length;
    const isPR = issue.issue_type === "pull_request";
    const typeIcon = isPR ? "🔀" : "🟢";

    card.innerHTML = `
      <div class="issue-card-top">
        <div>
          <span class="issue-repo-num">${typeIcon} ${issue.owner}/${issue.repo} #${issue.issue_number}</span>
          <div>
            <a href="${issue.url}" target="_blank" rel="noopener noreferrer" class="issue-title-link">
              ${escapeHtml(issue.title || "Untitled Issue")}
            </a>
          </div>
        </div>
        <div class="reason-badges">
          ${badgesHtml}
        </div>
      </div>
      <div class="issue-card-bottom">
        <span>Author: <strong>@${escapeHtml(issue.user_login || "unknown")}</strong></span>
        <span>💬 ${commentsCount} comments</span>
      </div>
    `;

    issuesList.appendChild(card);
  });
}

// ============================================================================
// 5. Actions & Cloud Function Handlers
// ============================================================================

// Sync GitHub Issues
async function triggerIssueSync() {
  if (!currentUser) return;

  const btnSyncs = [btnSyncIssuesLanding, btnSyncIssuesSettings];
  btnSyncs.forEach(b => {
    if (b) {
      b.disabled = true;
      b.textContent = "Syncing...";
    }
  });

  try {
    const syncFn = httpsCallable(functions, "sync_github_issues");
    const result = await syncFn({ state: "open" });
    const count = result.data.initial_queues_count || 0;
    showToast(`GitHub sync started in background (${count} queues dispatched)!`);
  } catch (err) {
    console.error("Error syncing issues:", err);
    showToast(`Sync failed: ${err.message}`, "error");
  } finally {
    if (btnSyncIssuesLanding) {
      btnSyncIssuesLanding.disabled = false;
      btnSyncIssuesLanding.textContent = "Sync from GitHub";
    }
    if (btnSyncIssuesSettings) {
      btnSyncIssuesSettings.disabled = false;
      btnSyncIssuesSettings.textContent = "Sync GitHub Issues Now";
    }
  }
}

// Force Re-rank All Tasks
btnForceRerank.addEventListener("click", async () => {
  if (!currentUser) return;

  btnForceRerank.disabled = true;
  btnForceRerank.textContent = "Enqueuing Re-rank...";

  try {
    const forceFn = httpsCallable(functions, "force_rerank_all_tasks");
    const result = await forceFn();
    showToast("Forced re-rank enqueued in background.");
  } catch (err) {
    console.error("Error forcing rerank:", err);
    showToast(`Forced rerank failed: ${err.message}`, "error");
  } finally {
    btnForceRerank.disabled = false;
    btnForceRerank.textContent = "Force Re-rank All Tasks";
  }
});

// Sign In With Google
btnLoginGoogle.addEventListener("click", async () => {
  try {
    const provider = new GoogleAuthProvider();
    await signInWithPopup(auth, provider);
    showToast("Signed in with Google successfully!");
  } catch (err) {
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
    const credential = GithubAuthProvider.credentialFromResult(result);
    const token = credential?.accessToken;
    
    if (token) {
      inputGithubToken.value = token;
      const associateFn = httpsCallable(functions, "associate_user_info");
      await associateFn({
        github_access_token: token,
      });
      showToast("Signed in with GitHub and saved GitHub access token!");
    } else {
      showToast("Signed in with GitHub successfully!");
    }
  } catch (err) {
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

// Navigation
btnGotoSettings.addEventListener("click", () => switchView(viewSettings));
btnBackToLanding.addEventListener("click", () => switchView(viewLanding));

// Token Visibility Toggle
btnToggleTokenVisibility.addEventListener("click", () => {
  if (inputGithubToken.type === "password") {
    inputGithubToken.type = "text";
    btnToggleTokenVisibility.textContent = "🔒";
  } else {
    inputGithubToken.type = "password";
    btnToggleTokenVisibility.textContent = "👁️";
  }
});

// Save Settings
btnSaveSettings.addEventListener("click", async () => {
  if (!currentUser) return;

  const newToken = inputGithubToken.value.trim();
  const rawRepos = inputMonitoredRepos.value.trim();
  const repoList = rawRepos ? rawRepos.split(",").map(r => r.trim()).filter(Boolean) : [];

  btnSaveSettings.disabled = true;
  btnSaveSettings.textContent = "Saving...";

  try {
    const payload = {
      monitored_repos: repoList,
    };
    if (newToken) {
      payload.github_access_token = newToken;
    }

    const associateFn = httpsCallable(functions, "associate_user_info");
    await associateFn(payload);

    showToast("Settings saved successfully!");
    if (newToken) inputGithubToken.value = "";
  } catch (err) {
    showToast(`Save failed: ${err.message}`, "error");
  } finally {
    btnSaveSettings.disabled = false;
    btnSaveSettings.textContent = "Save Settings";
  }
});

// Sync Issue Buttons
btnSyncIssuesLanding.addEventListener("click", triggerIssueSync);
btnSyncIssuesSettings.addEventListener("click", triggerIssueSync);
