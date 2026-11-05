"use strict";

const socket = io({ transports: ["polling"] });

const CURRENT_LAB = "linux";
const HOSTS = ["linux-server"];

const terminalElement = document.getElementById("terminal");
const terminalState = document.getElementById("terminalState");
const hostStatus = document.getElementById("hostStatus");

let activeContainer = HOSTS[0];
let expiryTimestamp = null;
let statusRefreshInProgress = false;
let labCreatedLocally = false;
let resizing = false;
let labCreatingMessageShown = false;
let fastConnectionCheck = null;
let terminalConnectedMessageShown = false;

const terminals = {};
const fitAddons = {};
const terminalSessions = {};
const pollTimers = {};
const inputQueues = {};
const inputFlushTimers = {};
const pollingInProgress = {};
const terminalConnectionsInProgress = {};

const DEFAULT_LAB_DURATION_SECONDS = 3600;
const TERMINAL_POLL_INTERVAL_MS = 50;
const INPUT_FLUSH_DELAY_MS = 5;
const MAX_SAVED_TERMINAL_OUTPUT = 500000;
const TERMINAL_STORAGE_KEY = "linux-terminal-persistent-state-v1";

const labEndpoint = (path) => `/api/${CURRENT_LAB}${path}`;

let savedTerminalState = loadSavedTerminalState();

function loadSavedTerminalState() {
  try {
    return JSON.parse(localStorage.getItem(TERMINAL_STORAGE_KEY)) || {};
  } catch (_) {
    return {};
  }
}

function saveTerminalState(container) {
  try {
    savedTerminalState[container] = {
      session_id: terminalSessions[container] || null,
      output: savedTerminalState[container]?.output || "",
      updated_at: Date.now(),
    };

    localStorage.setItem(
      TERMINAL_STORAGE_KEY,
      JSON.stringify(savedTerminalState),
    );
  } catch (_) {}
}

function saveTerminalOutput(container, output) {
  if (!output) return;

  try {
    if (!savedTerminalState[container]) {
      savedTerminalState[container] = {
        session_id: terminalSessions[container] || null,
        output: "",
        updated_at: Date.now(),
      };
    }

    savedTerminalState[container].output += output;

    if (
      savedTerminalState[container].output.length > MAX_SAVED_TERMINAL_OUTPUT
    ) {
      savedTerminalState[container].output = savedTerminalState[
        container
      ].output.slice(-MAX_SAVED_TERMINAL_OUTPUT);
    }

    savedTerminalState[container].session_id =
      terminalSessions[container] ||
      savedTerminalState[container].session_id ||
      null;

    savedTerminalState[container].updated_at = Date.now();

    localStorage.setItem(
      TERMINAL_STORAGE_KEY,
      JSON.stringify(savedTerminalState),
    );
  } catch (_) {}
}

function restoreTerminalOutput(container) {
  const output = savedTerminalState[container]?.output;

  if (output && terminals[container]) {
    terminals[container].write(output);
  }
}

function clearTerminalScreen(container) {
  clearTimeout(pollTimers[container]);
  clearTimeout(inputFlushTimers[container]);

  delete pollTimers[container];
  delete inputFlushTimers[container];
  delete terminalSessions[container];
  delete terminalConnectionsInProgress[container];

  inputQueues[container] = "";
  pollingInProgress[container] = false;

  delete savedTerminalState[container];

  try {
    localStorage.setItem(
      TERMINAL_STORAGE_KEY,
      JSON.stringify(savedTerminalState),
    );
  } catch (_) {}

  if (terminals[container]) {
    terminals[container].reset();

    if (terminals[container].element) {
      delete terminals[container].element.dataset.outputRestored;
    }
  }

  if (container === activeContainer) {
    terminalConnectedMessageShown = false;
    labCreatingMessageShown = false;

    if (terminalState) {
      terminalState.textContent = "Not connected";
      terminalState.className = "terminal-state";
    }
  }
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    cache: "no-store",
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });

  const data = await response.json().catch(() => ({
    success: false,
    error: `HTTP ${response.status}`,
  }));

  if (!response.ok || data.success === false) {
    throw new Error(
      data.error || data.stderr || data.stdout || `HTTP ${response.status}`,
    );
  }

  return data;
}

function setLoading(button, loading, text = "Working...") {
  if (!button) return;

  if (loading) {
    if (!button.dataset.originalText) {
      button.dataset.originalText = button.textContent;
    }

    button.textContent = text;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalText || button.textContent;
    delete button.dataset.originalText;
    button.disabled = false;
  }
}

function createLocalTerminal(container) {
  if (terminals[container]) return;

  const terminal = new Terminal({
    cursorBlink: true,
    cursorStyle: "block",
    convertEol: true,
    scrollback: 10000,
    fontSize: 13,
    fontFamily: "Consolas, 'Courier New', monospace",
    theme: {
      background: "#080d18",
      foreground: "#e5edf7",
      cursor: "#82aaff",
    },
  });

  const FitAddonClass = window.FitAddon?.FitAddon;
  let fit;

  if (typeof FitAddonClass === "function") {
    fit = new FitAddonClass();
    terminal.loadAddon(fit);
  } else {
    console.error(
      "FitAddon is unavailable. Check that xterm-addon-fit loads before linux.js.",
    );
    fit = { fit: () => {} };
  }

  terminals[container] = terminal;
  fitAddons[container] = fit;
  inputQueues[container] = "";
  pollingInProgress[container] = false;

  terminal.onData((data) => {
    if (!terminalSessions[container]) return;

    inputQueues[container] += data;

    if (!inputFlushTimers[container]) {
      inputFlushTimers[container] = setTimeout(() => {
        inputFlushTimers[container] = null;
        flushTerminalInput(container);
      }, INPUT_FLUSH_DELAY_MS);
    }
  });

  terminal.onResize((size) => {
    const sessionId = terminalSessions[container];

    if (!sessionId) return;

    fetch(`/api/terminal/${sessionId}/resize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cols: size.cols,
        rows: size.rows,
      }),
    }).catch(() => {});
  });
}

function writeTerminal(container, text) {
  if (!terminals[container] || !text) return;

  terminals[container].write(text);
  saveTerminalOutput(container, text);
}

function showCreatingMessage() {
  createLocalTerminal(activeContainer);

  if (labCreatingMessageShown) return;

  labCreatingMessageShown = true;
  terminalConnectedMessageShown = false;

  terminals[activeContainer].reset();

  savedTerminalState[activeContainer] = {
    session_id: null,
    output: "",
    updated_at: Date.now(),
  };

  writeTerminal(
    activeContainer,
    "[lab] Creating Linux server...\r\n" +
      "[lab] The terminal will connect automatically when the server is ready.\r\n" +
      "Please wait.\r\n\r\n",
  );

  terminalState.textContent = "Creating server...";
  terminalState.className = "terminal-state";
}

function showReadyMessage() {
  if (!labCreatingMessageShown) return;

  writeTerminal(
    activeContainer,
    "\r\n[lab] Linux server is ready. Connecting...\r\n\r\n",
  );

  labCreatingMessageShown = false;
}

function showConnectedMessage(container) {
  if (container !== activeContainer) return;
  if (terminalConnectedMessageShown) return;

  terminalConnectedMessageShown = true;

  terminalState.textContent = "Connected";
  terminalState.className = "terminal-state online";

  terminals[container]?.focus();
}

async function flushTerminalInput(container) {
  const sessionId = terminalSessions[container];

  if (!sessionId || !inputQueues[container]) return;

  const data = inputQueues[container];
  inputQueues[container] = "";

  try {
    const response = await fetch(`/api/terminal/${sessionId}/write`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data }),
    });

    if (!response.ok) throw new Error("Terminal input failed.");

    const result = await response.json().catch(() => null);

    if (result && result.success === false) {
      throw new Error(result.error || "Terminal input failed.");
    }
  } catch (_) {
    if (terminalSessions[container] === sessionId) {
      inputQueues[container] = data + inputQueues[container];
    }
  }

  if (inputQueues[container] && terminalSessions[container]) {
    inputFlushTimers[container] = setTimeout(() => {
      inputFlushTimers[container] = null;
      flushTerminalInput(container);
    }, INPUT_FLUSH_DELAY_MS);
  }
}

function showActiveTerminal() {
  if (!terminalElement) return;

  terminalElement.replaceChildren();
  createLocalTerminal(activeContainer);

  const terminal = terminals[activeContainer];

  terminal.open(terminalElement);

  if (!terminal.element.dataset.outputRestored) {
    restoreTerminalOutput(activeContainer);
    terminal.element.dataset.outputRestored = "true";
  }

  requestAnimationFrame(() => {
    resizeActiveTerminal();
    terminal.focus();
  });

  terminalState.textContent = terminalSessions[activeContainer]
    ? "Connected"
    : "Not connected";

  terminalState.className = terminalSessions[activeContainer]
    ? "terminal-state online"
    : "terminal-state";
}

async function validateSavedSession(container, sessionId) {
  if (!sessionId) return false;

  try {
    const response = await fetch(`/api/terminal/${sessionId}/read`, {
      cache: "no-store",
    });

    if (!response.ok) return false;

    const data = await response.json();

    if (data.closed) return false;

    terminalSessions[container] = sessionId;

    if (data.output) {
      writeTerminal(container, data.output);
    }

    return true;
  } catch (_) {
    return false;
  }
}

async function connectTerminal(container) {
  createLocalTerminal(container);

  if (terminalSessions[container]) {
    if (container === activeContainer) {
      showConnectedMessage(container);
    }

    return;
  }

  if (terminalConnectionsInProgress[container]) {
    return terminalConnectionsInProgress[container];
  }

  terminalConnectionsInProgress[container] = (async () => {
    try {
      const savedSessionId = savedTerminalState[container]?.session_id || null;

      if (savedSessionId) {
        const restored = await validateSavedSession(container, savedSessionId);

        if (restored) {
          saveTerminalState(container);

          if (container === activeContainer) {
            showConnectedMessage(container);
          }

          pollTerminal(container);
          resizeActiveTerminal();
          return;
        }

        if (savedTerminalState[container]) {
          savedTerminalState[container].session_id = null;
        }
      }

      const data = await api(labEndpoint("/terminal/open"), {
        method: "POST",
        body: JSON.stringify({ container }),
      });

      if (!data.session_id) {
        throw new Error("Terminal session was not created.");
      }

      terminalSessions[container] = data.session_id;

      saveTerminalState(container);

      if (container === activeContainer) {
        showConnectedMessage(container);
      }

      pollTerminal(container);
      resizeActiveTerminal();
    } catch (_) {
      if (container === activeContainer) {
        terminalState.textContent = "Connection failed";
        terminalState.className = "terminal-state error";
      }
    } finally {
      delete terminalConnectionsInProgress[container];
    }
  })();

  return terminalConnectionsInProgress[container];
}

async function pollTerminal(container) {
  const sessionId = terminalSessions[container];

  if (!sessionId || !terminals[container] || pollingInProgress[container]) {
    return;
  }

  pollingInProgress[container] = true;

  try {
    const response = await fetch(`/api/terminal/${sessionId}/read`, {
      cache: "no-store",
    });

    const data = await response.json();

    if (data.output) {
      writeTerminal(container, data.output);
    }

    if (data.closed || !response.ok) {
      clearTerminalScreen(container);
      return;
    }
  } catch (_) {
  } finally {
    pollingInProgress[container] = false;
  }

  if (terminalSessions[container] === sessionId) {
    pollTimers[container] = setTimeout(
      () => pollTerminal(container),
      TERMINAL_POLL_INTERVAL_MS,
    );
  }
}

async function closeTerminalSession(container) {
  const sessionId = terminalSessions[container];

  clearTimeout(pollTimers[container]);
  clearTimeout(inputFlushTimers[container]);

  delete pollTimers[container];
  delete inputFlushTimers[container];

  inputQueues[container] = "";

  if (sessionId) {
    try {
      await fetch(`/api/terminal/${sessionId}`, { method: "DELETE" });
    } catch (_) {}
  }

  delete terminalSessions[container];

  if (savedTerminalState[container]) {
    savedTerminalState[container].session_id = null;

    try {
      localStorage.setItem(
        TERMINAL_STORAGE_KEY,
        JSON.stringify(savedTerminalState),
      );
    } catch (_) {}
  }
}

function resizeActiveTerminal() {
  if (resizing) return;

  const fit = fitAddons[activeContainer];

  if (!fit) return;

  try {
    fit.fit();
    terminals[activeContainer].scrollToBottom();
  } catch (_) {}
}

function setActiveHost(container) {
  if (!HOSTS.includes(container)) return;

  activeContainer = container;
  terminalConnectedMessageShown = false;

  document.querySelectorAll(".host-item").forEach((button) => {
    button.classList.toggle("selected", button.dataset.container === container);
  });

  const title = document.getElementById("activeHostTitle");
  const subtitle = document.getElementById("activeHostSubtitle");
  const description = document.getElementById("terminalDescription");

  if (title) title.textContent = container;
  if (subtitle) subtitle.textContent = "Linux practice terminal";

  if (description) {
    description.textContent = `Interactive terminal for ${container}`;
  }

  showActiveTerminal();
  connectTerminal(container);
}

function clearCountdown() {
  expiryTimestamp = null;
  labCreatedLocally = false;
  renderTimer();
}

function startCountdown(seconds) {
  expiryTimestamp = Date.now() + Math.ceil(seconds) * 1000;
  labCreatedLocally = true;
  renderTimer();
}

function updateTimerFromStatus(data) {
  const remaining = Number(data.remaining_seconds);

  if (
    data.remaining_seconds !== null &&
    data.remaining_seconds !== undefined &&
    Number.isFinite(remaining) &&
    remaining > 0
  ) {
    expiryTimestamp = Date.now() + remaining * 1000;
    labCreatedLocally = true;
    renderTimer();
    return;
  }

  if (labCreatedLocally && expiryTimestamp !== null) {
    renderTimer();
    return;
  }

  if (data.running && expiryTimestamp === null) {
    startCountdown(DEFAULT_LAB_DURATION_SECONDS);
  } else if (!data.running && !data.creating) {
    clearCountdown();
  }
}

function renderTimer() {
  const timer = document.getElementById("clusterTimer");

  if (!timer) return;

  if (expiryTimestamp === null) {
    timer.textContent = "--:--:--";
    return;
  }

  const seconds = Math.max(0, Math.ceil((expiryTimestamp - Date.now()) / 1000));

  timer.textContent = [
    String(Math.floor(seconds / 3600)).padStart(2, "0"),
    String(Math.floor((seconds % 3600) / 60)).padStart(2, "0"),
    String(seconds % 60).padStart(2, "0"),
  ].join(":");

  if (seconds === 0) {
    expiryTimestamp = null;
    labCreatedLocally = false;
    refreshStatus();
  }
}

async function refreshStatus() {
  if (statusRefreshInProgress) return;

  statusRefreshInProgress = true;

  try {
    try {
      await api("/api/health");

      if (hostStatus) {
        hostStatus.textContent = "Connected";
        hostStatus.className = "status-pill online";
      }
    } catch (_) {
      if (hostStatus) {
        hostStatus.textContent = "Connection failed";
        hostStatus.className = "status-pill";
      }
    }

    const data = await api(labEndpoint("/status"));
    const item = data.containers?.["linux-server"] || {};
    const running = item.state === "running";

    const statusElement = document.getElementById("linux-serverStatus");
    const dot = document.querySelector(
      '.host-item[data-container="linux-server"] .host-dot',
    );

    if (statusElement) {
      statusElement.textContent = running
        ? "Running"
        : ["exited", "dead"].includes(item.state)
          ? "Stopped"
          : item.status || "Not created";
    }

    if (dot) {
      dot.classList.toggle("running", running);
      dot.classList.toggle(
        "stopped",
        !running && ["exited", "dead", "missing"].includes(item.state),
      );
    }

    updateTimerFromStatus(data);

    if (data.creating && !labCreatingMessageShown) {
      showCreatingMessage();
    }

    if (!data.creating && !data.has_containers) {
      clearTerminalScreen(activeContainer);
    }

    if (running && !terminalSessions[activeContainer]) {
      connectTerminal(activeContainer);
    }

    const createButton = document.getElementById("createClusterBtn");
    const stopButton = document.getElementById("stopClusterBtn");
    const destroyButton = document.getElementById("destroyClusterBtn");

    if (createButton && !createButton.dataset.originalText) {
      createButton.disabled = Boolean(data.creating);
    }

    if (stopButton) stopButton.disabled = !data.running;
    if (destroyButton) destroyButton.disabled = !data.has_containers;
  } catch (_) {
    renderTimer();
  } finally {
    statusRefreshInProgress = false;
  }
}

function startFastConnectionCheck() {
  clearInterval(fastConnectionCheck);

  const startedAt = Date.now();

  fastConnectionCheck = setInterval(async () => {
    if (terminalSessions[activeContainer]) {
      clearInterval(fastConnectionCheck);
      fastConnectionCheck = null;
      return;
    }

    if (Date.now() - startedAt > 30000) {
      clearInterval(fastConnectionCheck);
      fastConnectionCheck = null;
      return;
    }

    try {
      const data = await api(labEndpoint("/status"));
      const item = data.containers?.["linux-server"] || {};

      if (item.state === "running") {
        clearInterval(fastConnectionCheck);
        fastConnectionCheck = null;

        showReadyMessage();
        await connectTerminal(activeContainer);
      }
    } catch (_) {}
  }, 250);
}

// -------------------------------------------------------------------------
// Documents
// -------------------------------------------------------------------------

let loadedDocuments = [];
let progressData = null;
let progressBusy = false;
let selectedDocumentName = null;
let documentsLoaded = false;
let documentLoadInProgress = false;

function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[char],
  );
}

function markdownToHtml(markdown) {
  const lines = String(markdown).replace(/\r\n/g, "\n").split("\n");
  const output = [];
  let paragraph = [];
  let codeLines = [];
  let inCode = false;
  let listType = null;
  let listItems = [];

  function inlineMarkdown(value) {
    let text = escapeHtml(value);
    text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
    text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/\*(.+?)\*/g, "<em>$1</em>");
    return text;
  }

  function flushParagraph() {
    if (paragraph.length) {
      output.push(`<p>${inlineMarkdown(paragraph.join(" ").trim())}</p>`);
      paragraph = [];
    }
  }

  function flushList() {
    if (listItems.length) {
      output.push(
        `<${listType}>${listItems.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</${listType}>`,
      );
      listItems = [];
      listType = null;
    }
  }

  function flushCode() {
    output.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
    codeLines = [];
  }

  for (const line of lines) {
    if (line.startsWith("```")) {
      if (inCode) {
        flushCode();
        inCode = false;
      } else {
        flushParagraph();
        flushList();
        inCode = true;
      }
      continue;
    }

    if (inCode) {
      codeLines.push(line);
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);

    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      output.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }

    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+\.\s+(.+)$/);

    if (unordered || ordered) {
      flushParagraph();

      const type = unordered ? "ul" : "ol";

      if (listType !== type) {
        flushList();
        listType = type;
      }

      listItems.push((unordered || ordered)[1]);
      continue;
    }

    flushList();

    if (!line.trim()) {
      flushParagraph();
    } else {
      paragraph.push(line);
    }
  }

  if (inCode) flushCode();

  flushParagraph();
  flushList();

  return `<div class="markdown-body">${output.join("\n")}</div>`;
}

function displayDocumentContent(doc) {
  const content = document.getElementById("documentContent");

  if (!content || !doc) return;

  selectedDocumentName = doc.name;

  if (!String(doc.content || "").trim()) {
    content.textContent = "This document is empty. Add content to this file.";
    return;
  }

  const extension = doc.name.split(".").pop().toLowerCase();

  if (extension === "html" || extension === "htm") {
    const iframe = document.createElement("iframe");

    iframe.className = "document-html-frame";
    iframe.title = doc.title || doc.name;
    iframe.setAttribute("sandbox", "");
    iframe.setAttribute("loading", "lazy");
    iframe.srcdoc = String(doc.content || "");

    content.replaceChildren(iframe);
  } else if (extension === "md") {
    content.innerHTML = markdownToHtml(doc.content || "");
  } else {
    const pre = document.createElement("pre");

    pre.className = "document-text-source";
    pre.textContent = doc.content || "";

    content.replaceChildren(pre);
  }
}

function getCurrentProgressDocument() {
  if (!progressData || !progressData.current) return null;

  return (
    loadedDocuments.find((doc) => doc.name === progressData.current) || null
  );
}

function getDocumentProgressState(doc) {
  const progressDocument = progressData?.documents?.find(
    (item) => item.name === doc.name,
  );

  return progressDocument?.status || "locked";
}

function updateDocumentOptions() {
  const select = document.getElementById("documentSelect");

  if (!select || !documentsLoaded) return;

  const previousSelection = selectedDocumentName || select.value;

  select.replaceChildren();

  loadedDocuments.forEach((doc) => {
    const option = document.createElement("option");

    option.value = doc.name;
    option.textContent = doc.title || doc.name;

    const status = getDocumentProgressState(doc);

    option.disabled = status === "locked";

    if (status === "completed") {
      option.textContent = `✓ ${option.textContent}`;
    } else if (status === "locked") {
      option.textContent = `🔒 ${option.textContent}`;
    } else {
      option.textContent = `● ${option.textContent}`;
    }

    select.appendChild(option);
  });

  const selectableDocument = loadedDocuments.find(
    (doc) =>
      doc.name === previousSelection &&
      getDocumentProgressState(doc) !== "locked",
  );

  if (selectableDocument) {
    select.value = selectableDocument.name;
    selectedDocumentName = selectableDocument.name;
  } else {
    const currentDocument = getCurrentProgressDocument();

    if (currentDocument) {
      select.value = currentDocument.name;
      selectedDocumentName = currentDocument.name;
    } else {
      select.selectedIndex = -1;
      selectedDocumentName = null;
    }
  }
}

function displayCurrentDocument() {
  const select = document.getElementById("documentSelect");
  const content = document.getElementById("documentContent");

  if (!select || !content) return;

  const currentDocument = getCurrentProgressDocument();

  if (!currentDocument) {
    select.selectedIndex = -1;
    selectedDocumentName = null;

    content.textContent = loadedDocuments.length
      ? "All Linux documents have been completed."
      : "No Linux documentation files found in documents/linux.";

    return;
  }

  select.value = currentDocument.name;
  selectedDocumentName = currentDocument.name;
  displayDocumentContent(currentDocument);
}

async function loadDocuments(options = {}) {
  const select = document.getElementById("documentSelect");
  const content = document.getElementById("documentContent");

  if (!select || !content || documentLoadInProgress) return;

  documentLoadInProgress = true;

  const preserveSelection = options.preserveSelection !== false;
  const previousSelection = preserveSelection
    ? selectedDocumentName || select.value
    : null;

  try {
    const data = await api("/api/linux/documents");

    if (!Array.isArray(data.documents)) {
      throw new Error("Invalid document response.");
    }

    loadedDocuments = data.documents;
    documentsLoaded = true;

    if (!loadedDocuments.length) {
      content.textContent =
        "No Linux documentation files found in documents/linux.";

      select.replaceChildren();
      select.disabled = true;
      selectedDocumentName = null;
      return;
    }

    select.disabled = false;

    updateDocumentOptions();

    const selectedDocument = loadedDocuments.find(
      (doc) =>
        doc.name === previousSelection &&
        getDocumentProgressState(doc) !== "locked",
    );

    if (selectedDocument) {
      select.value = selectedDocument.name;
      selectedDocumentName = selectedDocument.name;
      displayDocumentContent(selectedDocument);
    } else {
      displayCurrentDocument();
    }

    select.onchange = () => {
      const selectedName = select.value;

      const selectedDocument = loadedDocuments.find(
        (doc) => doc.name === selectedName,
      );

      if (!selectedDocument) return;

      if (getDocumentProgressState(selectedDocument) === "locked") {
        select.value = selectedDocumentName || "";
        return;
      }

      selectedDocumentName = selectedDocument.name;
      displayDocumentContent(selectedDocument);
    };
  } catch (error) {
    content.textContent = `Unable to load Linux documents: ${error.message}`;
  } finally {
    documentLoadInProgress = false;
  }
}

// -------------------------------------------------------------------------
// Progress tracking
// -------------------------------------------------------------------------

function setProgressMessage(text, kind) {
  const message = document.getElementById("progressMessage");

  if (!message) return;

  message.textContent = text || "";
  message.className = `progress-message${kind ? ` ${kind}` : ""}`;
}

function renderProgress(data) {
  progressData = data;

  const bar = document.getElementById("progressBar");
  const percent = document.getElementById("progressPercent");
  const count = document.getElementById("progressCount");
  const list = document.getElementById("progressList");
  const checkButton = document.getElementById("progressCheckBtn");

  if (bar) {
    bar.style.width = `${data.percent}%`;
  }

  if (percent) {
    percent.textContent = `${data.percent}%`;
  }

  if (count) {
    count.textContent = `${data.completed} / ${data.total} completed`;
  }

  if (list) {
    list.replaceChildren();

    if (!data.documents.length) {
      const empty = document.createElement("li");
      empty.className = "progress-item locked";
      empty.textContent = "No documents found.";
      list.appendChild(empty);
    }

    data.documents.forEach((doc) => {
      const item = document.createElement("li");
      item.className = `progress-item ${doc.status}`;

      const icon = document.createElement("span");
      icon.className = "progress-icon";
      icon.textContent =
        doc.status === "completed"
          ? "✓"
          : doc.status === "current"
            ? "●"
            : "🔒";

      const label = document.createElement("span");
      label.className = "progress-label";
      label.textContent = doc.title;

      item.appendChild(icon);
      item.appendChild(label);
      list.appendChild(item);
    });
  }

  if (checkButton) {
    const currentDoc = data.documents.find((doc) => doc.name === data.current);

    checkButton.disabled = progressBusy || !currentDoc || data.complete;

    checkButton.textContent = data.complete
      ? "All documents completed"
      : "Mark complete";
  }

  const progressTrack = document.querySelector(
    ".linux-progress .progress-track",
  );

  if (progressTrack) {
    progressTrack.setAttribute("aria-valuenow", String(data.percent));
  }

  updateDocumentOptions();
}

async function loadProgress() {
  try {
    const data = await api(labEndpoint("/progress"));
    renderProgress(data);
  } catch (error) {
    setProgressMessage(error.message, "error");
  }
}

async function completeCurrentDocument() {
  if (progressBusy || !progressData || !progressData.current) return;

  const button = document.getElementById("progressCheckBtn");
  const name = progressData.current;

  progressBusy = true;

  setProgressMessage("", "");
  setLoading(button, true, "Completing...");

  try {
    const data = await api(labEndpoint("/progress/complete"), {
      method: "POST",
      body: JSON.stringify({ name }),
    });

    progressBusy = false;
    setLoading(button, false);

    renderProgress(data);

    setProgressMessage(
      data.complete
        ? "All documents completed."
        : "Document completed. Next document unlocked.",
      "success",
    );

    displayCurrentDocument();
  } catch (error) {
    progressBusy = false;
    setLoading(button, false);

    setProgressMessage(error.message, "error");

    if (progressData) {
      renderProgress(progressData);
    }
  }
}

async function resetProgress() {
  if (!confirm("Reset progress and lock all documents except the first?")) {
    return;
  }

  try {
    const data = await api(labEndpoint("/progress/reset"), {
      method: "POST",
    });

    renderProgress(data);

    setProgressMessage("Progress reset.", "success");

    displayCurrentDocument();
  } catch (error) {
    setProgressMessage(error.message, "error");
  }
}

// -------------------------------------------------------------------------
// Lab actions
// -------------------------------------------------------------------------

async function clusterAction(action) {
  const buttonId =
    action === "create"
      ? "createClusterBtn"
      : action === "stop"
        ? "stopClusterBtn"
        : "destroyClusterBtn";

  const button = document.getElementById(buttonId);

  if (
    action === "destroy" &&
    !confirm("Destroy the Linux lab and its network?")
  ) {
    return;
  }

  if (action === "create") {
    showCreatingMessage();
    startFastConnectionCheck();
  }

  setLoading(button, true, action === "create" ? "Creating..." : "Working...");

  try {
    const payload = action === "create" ? { sid: socket.id } : undefined;

    await api(labEndpoint(`/${action}`), {
      method: "POST",
      ...(payload ? { body: JSON.stringify(payload) } : {}),
    });

    if (action === "create") {
      startCountdown(DEFAULT_LAB_DURATION_SECONDS);
    }

    if (action === "stop" || action === "destroy") {
      clearInterval(fastConnectionCheck);
      fastConnectionCheck = null;

      clearCountdown();
      await closeTerminalSession(activeContainer);
      labCreatingMessageShown = false;
      terminalConnectedMessageShown = false;
    }

    if (action === "destroy") {
      clearTerminalScreen(activeContainer);
    }

    await refreshStatus();
  } catch (error) {
    alert(error.message);

    if (action === "create") {
      clearInterval(fastConnectionCheck);
      fastConnectionCheck = null;

      clearCountdown();
      labCreatingMessageShown = false;
    }
  } finally {
    setLoading(button, false);
    await refreshStatus();
  }
}

// -------------------------------------------------------------------------
// Event listeners
// -------------------------------------------------------------------------

document.querySelectorAll(".host-item").forEach((button) => {
  button.addEventListener("click", () => {
    setActiveHost(button.dataset.container);
  });
});

document.getElementById("hostsButton")?.addEventListener("click", () => {
  document.getElementById("hostsButton").classList.add("selected");
  document.getElementById("documentsButton")?.classList.remove("selected");

  document.getElementById("terminalPanel")?.scrollIntoView({
    behavior: "smooth",
    block: "start",
  });

  terminals[activeContainer]?.focus();
});

document
  .getElementById("documentsButton")
  ?.addEventListener("click", async () => {
    document.getElementById("documentsButton").classList.add("selected");
    document.getElementById("hostsButton")?.classList.remove("selected");

    await loadProgress();

    if (!documentsLoaded) {
      await loadDocuments({ preserveSelection: false });
    }

    document.getElementById("documentsPanel")?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  });

document
  .getElementById("createClusterBtn")
  ?.addEventListener("click", () => clusterAction("create"));

document
  .getElementById("stopClusterBtn")
  ?.addEventListener("click", () => clusterAction("stop"));

document
  .getElementById("destroyClusterBtn")
  ?.addEventListener("click", () => clusterAction("destroy"));

document.getElementById("refreshBtn")?.addEventListener("click", refreshStatus);

document
  .getElementById("reloadDocsBtn")
  ?.addEventListener("click", async () => {
    await loadProgress();
    await loadDocuments({ preserveSelection: true });
  });

document
  .getElementById("progressCheckBtn")
  ?.addEventListener("click", completeCurrentDocument);

document
  .getElementById("progressResetBtn")
  ?.addEventListener("click", resetProgress);

document.getElementById("startBtn")?.addEventListener("click", async () => {
  try {
    await api(
      labEndpoint(`/containers/${encodeURIComponent(activeContainer)}/start`),
      { method: "POST" },
    );

    await refreshStatus();
    await connectTerminal(activeContainer);
  } catch (error) {
    alert(error.message);
  }
});

document.getElementById("stopBtn")?.addEventListener("click", async () => {
  try {
    await api(
      labEndpoint(`/containers/${encodeURIComponent(activeContainer)}/stop`),
      { method: "POST" },
    );

    await closeTerminalSession(activeContainer);
    await refreshStatus();
  } catch (error) {
    alert(error.message);
  }
});

document.getElementById("deleteBtn")?.addEventListener("click", async () => {
  if (!confirm(`Destroy ${activeContainer}?`)) return;

  try {
    await api(
      labEndpoint(`/containers/${encodeURIComponent(activeContainer)}`),
      { method: "DELETE" },
    );

    await closeTerminalSession(activeContainer);
    clearTerminalScreen(activeContainer);

    await refreshStatus();
  } catch (error) {
    alert(error.message);
  }
});

terminalElement?.addEventListener("click", () => {
  terminals[activeContainer]?.focus();
});

// -------------------------------------------------------------------------
// Terminal/document splitter
// -------------------------------------------------------------------------

const splitter = document.getElementById("splitter");
const terminalSection = document.querySelector(".terminal-section");

let dragY = 0;
let dragHeight = 0;

if (splitter && terminalSection) {
  splitter.addEventListener("pointerdown", (event) => {
    resizing = true;
    dragY = event.clientY;
    dragHeight = terminalSection.getBoundingClientRect().height;
    splitter.classList.add("dragging");
    splitter.setPointerCapture(event.pointerId);
  });

  splitter.addEventListener("pointermove", (event) => {
    if (!resizing) return;

    const height = Math.max(
      200,
      Math.min(window.innerHeight - 220, dragHeight + event.clientY - dragY),
    );

    const terminal = document.querySelector(".terminal");

    if (terminal) {
      terminal.style.height = `${height - 60}px`;
    }

    resizeActiveTerminal();
  });

  function finishResize() {
    resizing = false;
    splitter.classList.remove("dragging");
    resizeActiveTerminal();
  }

  splitter.addEventListener("pointerup", finishResize);
  splitter.addEventListener("pointercancel", finishResize);
}

// -------------------------------------------------------------------------
// Horizontal splitter and panel maximize
// -------------------------------------------------------------------------

(function initializeWorkspaceControls() {
  const workspace = document.getElementById("workspace");
  const horizontalSplitter = document.getElementById("horizontalSplitter");
  const verticalSplitter = document.getElementById("verticalSplitter");

  const terminalMaximizeBtn = document.getElementById("terminalMaximizeBtn");
  const documentMaximizeBtn = document.getElementById("documentMaximizeBtn");

  const terminalPanel = document.getElementById("terminalPanel");
  const documentsPanel = document.getElementById("documentsPanel");

  if (!workspace) return;

  let maximizedPanel = null;

  function fitTerminal() {
    requestAnimationFrame(() => {
      resizeActiveTerminal();
    });
  }

  function setMaximized(panel) {
    const isAlreadyMaximized = maximizedPanel === panel;

    workspace.classList.remove("terminal-maximized", "documents-maximized");

    maximizedPanel = isAlreadyMaximized ? null : panel;

    if (maximizedPanel) {
      workspace.classList.add(
        maximizedPanel === "terminal"
          ? "terminal-maximized"
          : "documents-maximized",
      );
    }

    if (terminalMaximizeBtn) {
      terminalMaximizeBtn.title =
        maximizedPanel === "terminal"
          ? "Restore terminal layout"
          : "Maximize terminal";

      terminalMaximizeBtn.setAttribute("aria-label", terminalMaximizeBtn.title);
    }

    if (documentMaximizeBtn) {
      documentMaximizeBtn.title =
        maximizedPanel === "documents"
          ? "Restore document layout"
          : "Maximize documents";

      documentMaximizeBtn.setAttribute("aria-label", documentMaximizeBtn.title);
    }

    fitTerminal();
  }

  terminalMaximizeBtn?.addEventListener("click", () => {
    setMaximized("terminal");
  });

  documentMaximizeBtn?.addEventListener("click", () => {
    setMaximized("documents");
  });

  if (horizontalSplitter && terminalPanel && documentsPanel) {
    let dragging = false;
    let startX = 0;
    let startWidth = 0;

    horizontalSplitter.addEventListener("pointerdown", (event) => {
      if (maximizedPanel) return;

      dragging = true;
      startX = event.clientX;
      startWidth = terminalPanel.getBoundingClientRect().width;

      horizontalSplitter.setPointerCapture(event.pointerId);

      document.body.classList.add("resizing-horizontal");

      event.preventDefault();
    });

    horizontalSplitter.addEventListener("pointermove", (event) => {
      if (!dragging || maximizedPanel) return;

      const bounds = workspace.getBoundingClientRect();
      const splitterWidth = horizontalSplitter.getBoundingClientRect().width;

      const availableWidth = bounds.width - 32 - splitterWidth;

      const minWidth = 220;
      const maxWidth = availableWidth - minWidth;

      const leftWidth = Math.max(
        minWidth,
        Math.min(maxWidth, startWidth + event.clientX - startX),
      );

      const rightWidth = availableWidth - leftWidth;

      workspace.style.setProperty("--terminal-width", `${leftWidth}px`);
      workspace.style.setProperty("--documents-width", `${rightWidth}px`);

      fitTerminal();
    });

    function stopHorizontalDragging() {
      dragging = false;
      document.body.classList.remove("resizing-horizontal");
      fitTerminal();
    }

    horizontalSplitter.addEventListener("pointerup", stopHorizontalDragging);
    horizontalSplitter.addEventListener(
      "pointercancel",
      stopHorizontalDragging,
    );
    horizontalSplitter.addEventListener(
      "lostpointercapture",
      stopHorizontalDragging,
    );

    horizontalSplitter.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;

      event.preventDefault();

      const bounds = workspace.getBoundingClientRect();
      const current = terminalPanel.getBoundingClientRect().width;

      const availableWidth =
        bounds.width - horizontalSplitter.getBoundingClientRect().width - 32;

      const minWidth = 220;
      const maxWidth = availableWidth - minWidth;

      const next = Math.max(
        minWidth,
        Math.min(maxWidth, current + (event.key === "ArrowRight" ? 20 : -20)),
      );

      workspace.style.setProperty("--terminal-width", `${next}px`);
      workspace.style.setProperty(
        "--documents-width",
        `${availableWidth - next}px`,
      );

      fitTerminal();
    });
  }

  if (verticalSplitter) {
    let dragging = false;
    let startY = 0;
    let startHeight = 0;

    verticalSplitter.addEventListener("pointerdown", (event) => {
      if (maximizedPanel) return;

      dragging = true;
      startY = event.clientY;
      startHeight = workspace.getBoundingClientRect().height;

      verticalSplitter.setPointerCapture(event.pointerId);

      document.body.classList.add("resizing-vertical");

      event.preventDefault();
    });

    verticalSplitter.addEventListener("pointermove", (event) => {
      if (!dragging || maximizedPanel) return;

      const newHeight = Math.max(300, startHeight + event.clientY - startY);

      workspace.style.height = `${newHeight}px`;

      fitTerminal();
    });

    function stopVerticalDragging() {
      dragging = false;
      document.body.classList.remove("resizing-vertical");
      fitTerminal();
    }

    verticalSplitter.addEventListener("pointerup", stopVerticalDragging);
    verticalSplitter.addEventListener("pointercancel", stopVerticalDragging);
    verticalSplitter.addEventListener(
      "lostpointercapture",
      stopVerticalDragging,
    );

    verticalSplitter.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;

      event.preventDefault();

      const currentHeight = workspace.getBoundingClientRect().height;
      const adjustment = event.key === "ArrowDown" ? 20 : -20;

      workspace.style.height = `${Math.max(300, currentHeight + adjustment)}px`;

      fitTerminal();
    });
  }

  window.addEventListener("resize", fitTerminal);
})();

window.addEventListener("resize", resizeActiveTerminal);

// -------------------------------------------------------------------------
// Socket events
// -------------------------------------------------------------------------

socket.on("cluster_complete", async (data) => {
  if (data.lab && data.lab !== CURRENT_LAB) return;

  if (data.success) {
    showReadyMessage();

    await refreshStatus();

    await connectTerminal(activeContainer);
  } else {
    clearInterval(fastConnectionCheck);
    fastConnectionCheck = null;

    labCreatingMessageShown = false;
    clearCountdown();

    await refreshStatus();
  }

  alert(data.message || (data.success ? "Lab created." : "Creation failed."));
});

socket.on("cluster_expired", async (data) => {
  if (data.lab && data.lab !== CURRENT_LAB) return;

  clearInterval(fastConnectionCheck);
  fastConnectionCheck = null;

  clearCountdown();
  labCreatingMessageShown = false;
  terminalConnectedMessageShown = false;

  await closeTerminalSession(activeContainer);
  clearTerminalScreen(activeContainer);
  await refreshStatus();

  alert(data.message || "The lab has expired.");
});

socket.on("connect", refreshStatus);

// -------------------------------------------------------------------------
// Initialization
// -------------------------------------------------------------------------

createLocalTerminal(activeContainer);
showActiveTerminal();

refreshStatus();
loadProgress();
loadDocuments({ preserveSelection: false });

setInterval(renderTimer, 1000);
setInterval(refreshStatus, 10000);
setInterval(loadProgress, 5000);

window.addEventListener("beforeunload", () => {
  saveTerminalState(activeContainer);
  window.addEventListener("resize", () => {
    requestAnimationFrame(() => {
      if (window.terminal && typeof window.terminal.fit === "function") {
        window.terminal.fit();
      }
    });
  });
});
