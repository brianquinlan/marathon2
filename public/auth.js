// Minimal Authentication Script for Developer Debug Login
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

if (isLocalhost) {
  try {
    connectAuthEmulator(auth, "http://127.0.0.1:9099", { disableWarnings: true });
  } catch (e) {
    // Emulator already connected
  }
}

// Helper to set cookie for server-side Jinja2 authentication
async function updateSessionCookie(user) {
  if (user) {
    const idToken = await user.getIdToken();
    document.cookie = `__session=${idToken}; path=/; max-age=3600; SameSite=Lax`;
  } else {
    document.cookie = "__session=; path=/; max-age=0; SameSite=Lax";
  }
}

// Watch auth state
onAuthStateChanged(auth, async (user) => {
  const hadCookie = document.cookie.includes("__session=");
  await updateSessionCookie(user);

  // If user state just changed, reload page to fetch server-rendered Jinja2 tasks
  if (user && !hadCookie) {
    window.location.reload();
  }
});

// Google Sign-In
const btnGoogle = document.getElementById("btn-login-google");
if (btnGoogle) {
  btnGoogle.addEventListener("click", async () => {
    try {
      const provider = new GoogleAuthProvider();
      const result = await signInWithPopup(auth, provider);
      await updateSessionCookie(result.user);
      window.location.reload();
    } catch (err) {
      alert(`Google Login failed: ${err.message}`);
    }
  });
}

// GitHub Sign-In
const btnGithub = document.getElementById("btn-login-github");
if (btnGithub) {
  btnGithub.addEventListener("click", async () => {
    try {
      const provider = new GithubAuthProvider();
      provider.addScope("repo");
      provider.addScope("read:user");
      const result = await signInWithPopup(auth, provider);
      await updateSessionCookie(result.user);
      window.location.reload();
    } catch (err) {
      alert(`GitHub Login failed: ${err.message}`);
    }
  });
}

// Sign Out
const btnLogout = document.getElementById("btn-logout");
if (btnLogout) {
  btnLogout.addEventListener("click", async () => {
    try {
      await signOut(auth);
      await updateSessionCookie(null);
      window.location.reload();
    } catch (err) {
      alert(`Logout error: ${err.message}`);
    }
  });
}
