// Shared helper for all SmartAttend pages.
// Same-origin requests since the backend serves this frontend directly.
const API_BASE = (window.SMARTATTEND_API_URL || "").replace(/\/$/, "");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;"
  }[char]));
}

function getDeviceId() {
  let id = localStorage.getItem("sa_device_id");
  if (!id) {
    id = "dev-" + Math.random().toString(36).slice(2) + "-" + Date.now();
    localStorage.setItem("sa_device_id", id);
  }
  return id;
}

function getToken() {
  return localStorage.getItem("sa_token");
}

function getUser() {
  const raw = localStorage.getItem("sa_user");
  return raw ? JSON.parse(raw) : null;
}

function saveSession(data) {
  localStorage.setItem("sa_token", data.token);
  localStorage.setItem("sa_user", JSON.stringify({ id: data.user_id, name: data.name, role: data.role }));
}

function logout() {
  localStorage.removeItem("sa_token");
  localStorage.removeItem("sa_user");
  window.location.href = "index.html";
}

function requireAuth(expectedRole, returnTo = "") {
  const user = getUser();
  const token = getToken();
  if (!user || !token) {
    const loginUrl = returnTo
      ? `index.html?return_to=${encodeURIComponent(returnTo)}`
      : "index.html";
    window.location.href = loginUrl;
    return null;
  }
  if (expectedRole && user.role !== expectedRole) {
    window.location.href = user.role === "faculty" ? "faculty_dashboard.html" : "student_home.html";
    return null;
  }
  return user;
}

async function apiRequest(path, options = {}) {
  const headers = options.headers || {};
  headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers["Authorization"] = "Bearer " + token;

  let res;
  try {
    res = await fetch(API_BASE + path, { ...options, headers });
  } catch (networkErr) {
    throw new Error("Can't reach the server. Check your connection and try again.");
  }

  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    data = null;
  }

  if (!res.ok) {
    const message = (data && data.detail) ? data.detail : `Request failed (${res.status})`;
    if (res.status === 401) {
      // Session expired or invalid — send back to login.
      localStorage.removeItem("sa_token");
      localStorage.removeItem("sa_user");
    }
    throw new Error(message);
  }
  return data;
}
