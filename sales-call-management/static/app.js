"use strict";

// One session id per page load, shared by every chat request.
const SESSION_ID = "s-" + Math.random().toString(36).slice(2) + Date.now().toString(36);

// Guards concurrent chat sends so Send stays disabled while streaming.
let streaming = false;

function el(id) {
  return document.getElementById(id);
}

function formatMoney(value) {
  const n = Number(value) || 0;
  return "$" + Math.round(n).toLocaleString("en-US");
}

function formatPercent(fraction) {
  const n = Number(fraction) || 0;
  return (n * 100).toFixed(1).replace(/\.0$/, "") + "%";
}

function formatProbability(value) {
  const n = Number(value);
  if (!isFinite(n)) {
    return "-";
  }
  // Probability may arrive as 0..1 or 0..100; normalize to a percent.
  const pct = n <= 1 ? n * 100 : n;
  return Math.round(pct) + "%";
}

// -- Dashboard rendering ----------------------------------------------------

function renderStats(stats) {
  const container = el("stat-tiles");
  container.replaceChildren();
  const tiles = [
    { label: "Open pipeline", value: formatMoney(stats.open_pipeline) },
    { label: "Weighted pipeline", value: formatMoney(stats.weighted_pipeline) },
    { label: "Open deals", value: String(stats.open_count ?? 0) },
    { label: "Win rate", value: formatPercent(stats.win_rate) },
  ];
  for (const tile of tiles) {
    const card = document.createElement("div");
    card.className = "stat-tile";
    const label = document.createElement("div");
    label.className = "stat-label";
    label.textContent = tile.label;
    const value = document.createElement("div");
    value.className = "stat-value";
    value.textContent = tile.value;
    card.append(label, value);
    container.appendChild(card);
  }
}

function renderPipeline(byStage) {
  const container = el("pipeline-by-stage");
  container.replaceChildren();
  const rows = Array.isArray(byStage) ? byStage : [];
  if (rows.length === 0) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No pipeline data.";
    container.appendChild(empty);
    return;
  }
  const maxAmount = rows.reduce((m, r) => Math.max(m, Number(r.amount) || 0), 0);
  for (const row of rows) {
    const amount = Number(row.amount) || 0;
    const width = maxAmount > 0 ? (amount / maxAmount) * 100 : 0;

    const barRow = document.createElement("div");
    barRow.className = "bar-row";

    const head = document.createElement("div");
    head.className = "bar-head";
    const stage = document.createElement("span");
    stage.className = "bar-stage";
    stage.textContent = row.stage;
    const meta = document.createElement("span");
    meta.className = "bar-meta";
    meta.textContent = formatMoney(amount);
    const count = document.createElement("span");
    count.className = "bar-count";
    count.textContent = (Number(row.count) || 0) + " deal" + ((Number(row.count) || 0) === 1 ? "" : "s");
    meta.appendChild(count);
    head.append(stage, meta);

    const track = document.createElement("div");
    track.className = "bar-track";
    const fill = document.createElement("div");
    fill.className = "bar-fill";
    fill.style.width = width.toFixed(1) + "%";
    fill.setAttribute("role", "img");
    fill.setAttribute(
      "aria-label",
      row.stage + ": " + formatMoney(amount) + " across " + (Number(row.count) || 0) + " deals"
    );
    track.appendChild(fill);

    barRow.append(head, track);
    container.appendChild(barRow);
  }
}

function renderOpportunities(opps) {
  const tbody = el("open-opportunities").querySelector("tbody");
  tbody.replaceChildren();
  const rows = Array.isArray(opps) ? opps : [];
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    tr.className = "empty-row";
    const td = document.createElement("td");
    td.colSpan = 6;
    td.textContent = "No open opportunities.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  for (const opp of rows) {
    const tr = document.createElement("tr");

    const account = document.createElement("td");
    account.textContent = opp.account_name || "-";

    const name = document.createElement("td");
    name.textContent = opp.name || "-";

    const stage = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "stage-badge";
    badge.textContent = opp.stage || "-";
    stage.appendChild(badge);

    const amount = document.createElement("td");
    amount.className = "num";
    amount.textContent = formatMoney(opp.amount);

    const close = document.createElement("td");
    close.textContent = opp.close_date || "-";

    const prob = document.createElement("td");
    prob.className = "num";
    prob.textContent = formatProbability(opp.probability);

    tr.append(account, name, stage, amount, close, prob);
    tbody.appendChild(tr);
  }
}

function renderActivity(activities) {
  const container = el("recent-activity");
  container.replaceChildren();
  const rows = Array.isArray(activities) ? activities : [];
  if (rows.length === 0) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "No recent activity.";
    container.appendChild(li);
    return;
  }
  for (const act of rows) {
    const li = document.createElement("li");
    li.className = "activity-item";

    const top = document.createElement("div");
    top.className = "activity-top";
    const date = document.createElement("span");
    date.className = "activity-date";
    date.textContent = act.date || "";
    const account = document.createElement("span");
    account.className = "activity-account";
    account.textContent = act.account_name || "Unknown account";
    const type = document.createElement("span");
    type.className = "activity-type";
    type.textContent = act.type || "note";
    top.append(date, account, type);

    const summary = document.createElement("div");
    summary.className = "activity-summary";
    summary.textContent = act.summary || "";

    li.append(top, summary);
    container.appendChild(li);
  }
}

async function loadDashboard() {
  try {
    const res = await fetch("/api/dashboard");
    if (!res.ok) {
      throw new Error("HTTP " + res.status);
    }
    const data = await res.json();
    renderStats(data.stats || {});
    renderPipeline(data.pipeline_by_stage);
    renderOpportunities(data.open_opportunities);
    renderActivity(data.recent_activity);
  } catch (err) {
    const container = el("stat-tiles");
    container.replaceChildren();
    const msg = document.createElement("p");
    msg.className = "muted";
    msg.textContent = "Dashboard unavailable: " + err.message;
    container.appendChild(msg);
  }
}

// -- Calls / ingest ---------------------------------------------------------

async function loadCalls() {
  const select = el("call-select");
  try {
    const res = await fetch("/api/calls");
    if (!res.ok) {
      throw new Error("HTTP " + res.status);
    }
    const calls = await res.json();
    select.replaceChildren();
    if (calls.length === 0) {
      const option = document.createElement("option");
      option.textContent = "No transcripts";
      option.disabled = true;
      select.appendChild(option);
      return;
    }
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

function setIngestStatus(text, isError) {
  const status = el("ingest-status");
  status.textContent = text || "";
  status.classList.toggle("error", Boolean(isError));
}

async function ingestSelectedCall() {
  const select = el("call-select");
  const call = select.value;
  if (!call) {
    return;
  }
  const button = el("ingest-button");
  button.disabled = true;
  setIngestStatus("processing...", false);
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
    const result = await res.json();
    setIngestStatus("done", false);
    await loadDashboard();
    appendNote(summarizeIngest(call, result));
  } catch (err) {
    setIngestStatus("failed", true);
    appendChatMessage("error", "Assistant", "Ingest failed: " + err.message);
  } finally {
    button.disabled = false;
  }
}

// Render the ingest result as a readable assistant-style note.
function summarizeIngest(call, result) {
  if (result && typeof result.summary === "string" && result.summary.trim()) {
    return "Ingested " + call + ":\n" + result.summary.trim();
  }
  if (result && typeof result === "object") {
    try {
      return "Ingested " + call + ":\n" + JSON.stringify(result, null, 2);
    } catch (_err) {
      // fall through
    }
  }
  return "Ingested " + call + ".";
}

// -- Chat -------------------------------------------------------------------

function clearChatEmpty() {
  const existing = el("chat-messages").querySelector(".chat-empty");
  if (existing) {
    existing.remove();
  }
}

function scrollChat() {
  const box = el("chat-messages");
  box.scrollTop = box.scrollHeight;
}

// Append a full chat message and return its text node for later mutation.
function appendChatMessage(kind, role, text) {
  clearChatEmpty();
  const wrapper = document.createElement("div");
  wrapper.className = "chat-message " + kind;
  const label = document.createElement("span");
  label.className = "role";
  label.textContent = role;
  const body = document.createElement("span");
  body.className = "text";
  body.textContent = text || "";
  wrapper.append(label, body);
  el("chat-messages").appendChild(wrapper);
  scrollChat();
  return body;
}

function appendNote(text) {
  appendChatMessage("note", "Assistant", text);
}

async function sendChat(message) {
  if (streaming) {
    return;
  }
  streaming = true;
  const sendButton = el("chat-send");
  sendButton.disabled = true;

  appendChatMessage("user", "You", message);
  const assistantBody = appendChatMessage("assistant", "Assistant", "");
  assistantBody.classList.add("streaming");

  let errored = false;
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
        const payload = frame.replace(/^data:\s?/, "");
        if (payload === "[DONE]") {
          done = true;
          break;
        }
        if (payload.startsWith("[ERROR]")) {
          errored = true;
          const detail = payload.slice("[ERROR]".length).trim();
          assistantBody.closest(".chat-message").className = "chat-message error";
          assistantBody.textContent = detail || "The assistant reported an error.";
          done = true;
          break;
        }
        assistantBody.textContent += payload;
        scrollChat();
        boundary = buffer.indexOf("\n\n");
      }
    }
    if (!errored && assistantBody.textContent === "") {
      assistantBody.textContent = "(no response)";
    }
  } catch (err) {
    assistantBody.closest(".chat-message").className = "chat-message error";
    assistantBody.textContent = "Request failed: " + err.message;
  } finally {
    assistantBody.classList.remove("streaming");
    streaming = false;
    sendButton.disabled = false;
    scrollChat();
  }
}

// -- Init -------------------------------------------------------------------

function init() {
  const chat = el("chat-messages");
  const empty = document.createElement("p");
  empty.className = "chat-empty";
  empty.textContent = "Ask the reporting assistant about your pipeline.";
  chat.appendChild(empty);

  el("ingest-button").addEventListener("click", ingestSelectedCall);
  el("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (streaming) {
      return;
    }
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
