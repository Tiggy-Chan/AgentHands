// AgentHands Console Application Logic

let pollTimer = null;
let currentSessionId = null;

document.addEventListener("DOMContentLoaded", () => {
  loadPlatformStatus();
  loadGateStatus();

  document.getElementById("btn-refresh").addEventListener("click", () => {
    loadPlatformStatus();
    loadGateStatus();
  });

  document.getElementById("btn-freeze").addEventListener("click", freezeAllGates);
});

async function loadPlatformStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    renderPlatforms(data);
  } catch (err) {
    console.error("Failed to load status:", err);
  }
}

function renderPlatforms(platforms) {
  const container = document.getElementById("platforms-container");
  container.innerHTML = "";

  for (const [key, p] of Object.entries(platforms)) {
    const card = document.createElement("div");
    card.className = "card";

    const badgeClass = p.authenticated ? "badge-success" : "badge-inactive";
    const badgeText = p.authenticated ? "Active" : "Offline";

    let metaHtml = "";
    if (p.account) metaHtml += `<div><strong>Account:</strong> ${escapeHtml(p.account)}</div>`;
    if (p.uid) metaHtml += `<div><strong>UID:</strong> ${escapeHtml(String(p.uid))}</div>`;
    if (p.details && p.details.level) metaHtml += `<div><strong>Level:</strong> ${p.details.level}</div>`;
    if (!metaHtml) metaHtml = `<div>${escapeHtml(p.status_text)}</div>`;

    card.innerHTML = `
      <div class="card-header">
        <div class="card-name">${escapeHtml(p.name)}</div>
        <span class="badge ${badgeClass}">${badgeText}</span>
      </div>
      <div class="card-body">
        <div class="card-desc">${escapeHtml(p.status_text)}</div>
        <div class="card-meta">${metaHtml}</div>
      </div>
      <div class="card-actions">
        ${p.key === "bilibili" ? `<button class="btn btn-primary" onclick="openQrModal('${p.key}')">Scan QR Login</button>` : ""}
        <button class="btn" onclick="openManualModal('${p.key}')">Manual Cookie</button>
      </div>
    `;
    container.appendChild(card);
  }
}

async function loadGateStatus() {
  try {
    const res = await fetch("/api/gates");
    const gates = await res.json();
    renderGates(gates);
  } catch (err) {
    console.error("Failed to load gates:", err);
  }
}

function renderGates(gates) {
  const tbody = document.getElementById("gates-tbody");
  tbody.innerHTML = "";

  for (const [gate, enabled] of Object.entries(gates)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><code>${escapeHtml(gate)}</code></td>
      <td>Controlled write action execution authorization</td>
      <td>
        <label class="switch">
          <input type="checkbox" ${enabled ? "checked" : ""} onchange="toggleGate('${gate}', this.checked)">
          <span class="slider"></span>
        </label>
      </td>
    `;
    tbody.appendChild(tr);
  }
}

async function toggleGate(gateName, enabled) {
  try {
    await fetch("/api/gates/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gate: gateName, enabled: enabled }),
    });
  } catch (err) {
    console.error("Failed to toggle gate:", err);
    loadGateStatus();
  }
}

async function freezeAllGates() {
  if (!confirm("Confirm emergency halt? All platform write operations will be immediately revoked.")) {
    return;
  }
  try {
    await fetch("/api/gates/freeze", { method: "POST" });
    loadGateStatus();
  } catch (err) {
    console.error("Failed to freeze gates:", err);
  }
}

// --- QR Login Modal ---

async function openQrModal(platform) {
  const modal = document.getElementById("qr-modal");
  const img = document.getElementById("qr-img");
  const statusEl = document.getElementById("qr-status-text");

  img.src = "";
  statusEl.innerText = "Initializing session...";
  modal.style.display = "flex";

  try {
    const res = await fetch("/api/login/qr/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform: platform }),
    });
    const data = await res.json();
    currentSessionId = data.session_id;

    if (data.qr_image) {
      img.src = data.qr_image;
      statusEl.innerText = "Please scan the QR code with your mobile app";
      startPollingQr(currentSessionId);
    } else {
      statusEl.innerText = "QR code generation not available for this platform.";
    }
  } catch (err) {
    statusEl.innerText = "Failed to start QR session: " + err.message;
  }
}

function startPollingQr(sessionId) {
  if (pollTimer) clearInterval(pollTimer);

  pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/login/qr/status/${sessionId}`);
      const data = await res.json();
      const statusEl = document.getElementById("qr-status-text");

      if (data.status === "confirmed") {
        clearInterval(pollTimer);
        statusEl.innerText = data.message || "Login successful!";
        setTimeout(() => {
          closeQrModal();
          loadPlatformStatus();
        }, 1500);
      } else if (data.status === "expired") {
        clearInterval(pollTimer);
        statusEl.innerText = "QR code expired. Please reopen to refresh.";
      }
    } catch (err) {
      console.error("Polling error:", err);
    }
  }, 2000);
}

function closeQrModal() {
  if (pollTimer) clearInterval(pollTimer);
  document.getElementById("qr-modal").style.display = "none";
}

// --- Manual Cookie Modal ---

function openManualModal(platform) {
  document.getElementById("manual-platform").value = platform;
  document.getElementById("manual-modal-title").innerText = `Configure Cookies - ${platform}`;
  document.getElementById("manual-cookies").value = "";
  document.getElementById("manual-userid").value = "";
  document.getElementById("manual-modal").style.display = "flex";
}

function closeManualModal() {
  document.getElementById("manual-modal").style.display = "none";
}

async function submitManualCookies() {
  const platform = document.getElementById("manual-platform").value;
  const cookies = document.getElementById("manual-cookies").value.trim();
  const userId = document.getElementById("manual-userid").value.trim();

  if (!cookies) {
    alert("Cookie string cannot be empty");
    return;
  }

  try {
    await fetch("/api/login/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform: platform, cookies: cookies, user_id: userId }),
    });
    closeManualModal();
    loadPlatformStatus();
  } catch (err) {
    alert("Failed to save cookies: " + err.message);
  }
}

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
