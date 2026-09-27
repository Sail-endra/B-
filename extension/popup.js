const siteEl = document.getElementById("site");
const toggle = document.getElementById("toggle");
const statusEl = document.getElementById("status");
const copilotButton = document.getElementById("copilot");
const copilotStatus = document.getElementById("copilot-status");
const copilotOrigin = "http://127.0.0.1:8471/*";

let tab = null;
let origin = null;
let enabled = false;
let copilotConnected = false;

function status(message, type = "") {
  statusEl.textContent = message;
  statusEl.className = type;
}

function patternFor(origin) {
  return `${origin}/*`;
}

async function initialize() {
  const cpPermission = await chrome.permissions.contains({ origins: [copilotOrigin] });
  copilotConnected = cpPermission;
  copilotButton.textContent = cpPermission ? "Disconnect Course Copilot" : "Connect Course Copilot";
  copilotButton.className = cpPermission ? "secondary" : "";
  if (cpPermission) {
    const cpReply = await chrome.runtime.sendMessage({ type: "BBX_CP_STATE" });
    copilotStatus.textContent = cpReply?.ok
      ? "Connected to Course Copilot."
      : (cpReply?.error || "Permission is on, but Course Copilot is not responding.");
    copilotStatus.className = cpReply?.ok ? "small ok" : "small error";
  } else {
    copilotStatus.textContent = "Optional: connect to sync materials and ask questions from Blackboard.";
    copilotStatus.className = "small";
  }

  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  tab = tabs[0];

  if (!tab?.url) {
    siteEl.textContent = "Open Blackboard in a normal https:// tab first.";
    return;
  }

  const url = new URL(tab.url);
  if (url.protocol !== "https:") {
    siteEl.textContent = "This prototype only enables on HTTPS sites.";
    return;
  }

  origin = url.origin;
  siteEl.textContent = origin;

  const reply = await chrome.runtime.sendMessage({
    type: "BBX_GET_STATUS",
    origin
  });

  enabled = Boolean(reply?.enabled);
  refreshButton();
}

function refreshButton() {
  toggle.disabled = !origin;
  toggle.textContent = enabled
    ? "Disable on this site"
    : "Enable on this Blackboard";
  toggle.className = enabled ? "secondary" : "";
}

toggle.addEventListener("click", async () => {
  toggle.disabled = true;
  status("");

  try {
    if (!enabled) {
      const granted = await chrome.permissions.request({
        origins: [patternFor(origin)]
      });

      if (!granted) {
        status("Site access was not granted.", "error");
        refreshButton();
        return;
      }

      const reply = await chrome.runtime.sendMessage({
        type: "BBX_ENABLE_ORIGIN",
        origin,
        tabId: tab.id
      });

      if (!reply?.ok) throw new Error(reply?.error || "Could not enable extension.");
      enabled = true;
      status("Enabled. Blackboard is reloading…", "ok");
    } else {
      const reply = await chrome.runtime.sendMessage({
        type: "BBX_DISABLE_ORIGIN",
        origin,
        tabId: tab.id
      });

      if (!reply?.ok) throw new Error(reply?.error || "Could not disable extension.");
      enabled = false;
      status("Disabled for this site.", "ok");
    }
  } catch (error) {
    status(error?.message || String(error), "error");
  }

  refreshButton();
});

copilotButton.addEventListener("click", async () => {
  copilotButton.disabled = true;
  copilotStatus.textContent = "";
  try {
    if (copilotConnected) {
      await chrome.permissions.remove({ origins: [copilotOrigin] });
      copilotConnected = false;
      copilotStatus.textContent = "Disconnected from Course Copilot.";
      copilotStatus.className = "small";
      copilotButton.textContent = "Connect Course Copilot";
      copilotButton.className = "secondary";
    } else {
      const accepted = await chrome.permissions.request({ origins: [copilotOrigin] });
      if (!accepted) throw new Error("Local Course Copilot access was not granted.");
      copilotConnected = true;
      copilotButton.textContent = "Disconnect Course Copilot";
      copilotButton.className = "secondary";
      const reply = await chrome.runtime.sendMessage({ type: "BBX_CP_STATE" });
      if (!reply?.ok) {
        copilotStatus.textContent = reply?.error || "Course Copilot did not respond. Start the local server, then retry in the drawer.";
        copilotStatus.className = "small error";
        return;
      }
      copilotStatus.textContent = "Connected. Course data stays on this computer.";
      copilotStatus.className = "small ok";
    }
  } catch (error) {
    copilotStatus.textContent = error?.message || String(error);
    copilotStatus.className = "small error";
  } finally {
    copilotButton.disabled = false;
  }
});

initialize().catch((error) => {
  siteEl.textContent = "Could not inspect the current tab.";
  status(error?.message || String(error), "error");
});
