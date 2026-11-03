"use strict";

// One session id per page load, shared by every chat request.
const SESSION_ID = "s-" + Math.random().toString(36).slice(2) + Date.now().toString(36);

function el(id) {
  return document.getElementById(id);
}

// Fetch the dashboard payload and render it as raw JSON into each container.
// A later chunk will replace the raw dumps with real tiles, charts, and tables.
async function loadDashboard() {
  const targets = {
    statTiles: el("stat-tiles"),
    pipeline: el("pipeline-by-stage"),
    activity: el("recent-activity"),
    opportunities: el("open-opportunities").querySelector("tbody"),
  };
  try {
    const res = await fetch("/api/dashboard");
    if (!res.ok) {
      throw new Error("HTTP " + res.status);
    }
    const data = await res.json();
    const dump = JSON.stringify(data, null, 2);
    targets.statTiles.textContent = dump;
    targets.pipeline.textContent = JSON.stringify(data.pipeline ?? data, null, 2);
    targets.activity.textContent = JSON.stringify(data.recent_activity ?? data, null, 2);
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.textContent = JSON.stringify(data.open_opportunities ?? data, null, 2);
    row.appendChild(cell);
    targets.opportunities.replaceChildren(row);
  } catch (err) {
    const message = "Dashboard unavailable: " + err.message;
    targets.statTiles.textContent = message;
  }
}

// Populate the transcript <select> from the calls listing endpoint.
async function loadCalls() {
  const select = el("call-select");
  try {
    const res = await fetch("/api/calls");
    if (!res.ok) {
      throw new Error("HTTP " + res.status);
    }
    const calls = await res.json();
    select.replaceChildren();
    for (const name of calls) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      select.appendChild(option);
    }
  } catch (err) {
    select.replaceChildren();
    const option = document.createElement("option");
    option.textContent = "Calls unavailable: " + err.message;
    option.disabled = true;
    select.appendChild(option);
  }
}

// Ingest the selected transcript, then refresh the dashboard.
async function ingestSelectedCall() {
  const select = el("call-select");
  const call = select.value;
  if (!call) {
    return;
  }
  const button = el("ingest-button");
  button.disabled = true;
  try {
    const res = await fetch("/api/calls/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ call }),
    });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error("HTTP " + res.status + ": " + detail);
    }
    await loadDashboard();
  } catch (err) {
    window.alert("Ingest failed: " + err.message);
  } finally {
    button.disabled = false;
  }
}

function appendMessage(role, text) {
  const wrapper = document.createElement("div");
  wrapper.className = "chat-message";
  const label = document.createElement("span");
  label.className = "role";
  label.textContent = role;
  const body = document.createElement("span");
  body.className = "text";
  body.textContent = text;
  wrapper.appendChild(label);
  wrapper.appendChild(body);
  el("chat-messages").appendChild(wrapper);
  el("chat-messages").scrollTop = el("chat-messages").scrollHeight;
  return body;
}

// Send a chat message and consume the SSE stream from /api/chat.
// The stream is read as raw bytes, split on the blank-line SSE frame
// delimiter, and each "data:" payload is appended until "[DONE]" arrives.
async function sendChat(message) {
  appendMessage("You", message);
  const assistantBody = appendMessage("Assistant", "");
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: SESSION_ID, message }),
    });
    if (!res.ok || !res.body) {
      throw new Error("HTTP " + res.status);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let done = false;
    while (!done) {
      const { value, done: streamDone } = await reader.read();
      if (streamDone) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const line = frame.replace(/^data:\s?/, "");
        if (line === "[DONE]") {
          done = true;
          break;
        }
        assistantBody.textContent += line;
        el("chat-messages").scrollTop = el("chat-messages").scrollHeight;
        boundary = buffer.indexOf("\n\n");
      }
    }
  } catch (err) {
    assistantBody.textContent += "\n[error: " + err.message + "]";
  }
}

function init() {
  el("ingest-button").addEventListener("click", ingestSelectedCall);
  el("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = el("chat-input");
    const message = input.value.trim();
    if (!message) {
      return;
    }
    input.value = "";
    sendChat(message);
  });
  loadCalls();
  loadDashboard();
}

document.addEventListener("DOMContentLoaded", init);
