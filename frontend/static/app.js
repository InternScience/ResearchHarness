(function () {
  var canvas, ctx, t = 0;
  var TILE = 26, GAP = 1;
  var rafId = null;
  var cachedRgb = "10,10,10";
  var lastDraw = 0;
  var FRAME_MS = 1000 / 24;

  function rgb() {
    var th = document.documentElement.getAttribute("data-theme") || "white";
    if (th === "dark") return "220,210,175";
    if (th === "yellow") return "120,85,20";
    if (th === "blue") return "38,88,155";
    return "10,10,10";
  }

  function frame(ts) {
    rafId = requestAnimationFrame(frame);
    if (ts - lastDraw < FRAME_MS) return;
    lastDraw = ts;

    var w = canvas.width, h = canvas.height;
    var cols = Math.ceil(w / TILE) + 1;
    var rows = Math.ceil(h / TILE) + 1;
    var pre = "rgba(" + cachedRgb + ",";

    ctx.clearRect(0, 0, w, h);
    for (var r = 0; r < rows; r++) {
      for (var c = 0; c < cols; c++) {
        var wave = 0.6 * Math.sin(c * 0.21 + t * 0.36) * Math.sin(r * 0.17 + t * 0.28)
          + 0.4 * Math.sin(c * 0.11 - r * 0.13 + t * 0.19);
        var norm = (wave + 1) * 0.5;
        var v = norm * norm * norm;
        var a = Math.round((0.004 + v * 0.186) * 100) / 100;
        if (a < 0.02) continue;
        ctx.fillStyle = pre + a + ")";
        ctx.fillRect(c * TILE + GAP, r * TILE + GAP, TILE - GAP, TILE - GAP);
      }
    }
    t += 0.007;
  }

  var resizeTimer;
  function resize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      var newW = window.innerWidth;
      var newH = window.innerHeight;
      if (newW === canvas.width && Math.abs(newH - canvas.height) <= 90) return;
      canvas.width = newW;
      canvas.height = newH;
    }, 120);
  }

  function onVisibilityChange() {
    if (document.hidden) {
      if (rafId) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
    } else if (!rafId) {
      rafId = requestAnimationFrame(frame);
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    canvas = document.createElement("canvas");
    canvas.style.cssText = "position:fixed;top:0;left:0;width:100%;height:100%;"
      + "z-index:0;pointer-events:none;will-change:transform;"
      + "-webkit-backface-visibility:hidden;backface-visibility:hidden;";
    document.body.insertBefore(canvas, document.body.firstChild);
    ctx = canvas.getContext("2d");
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    cachedRgb = rgb();
    window.addEventListener("resize", resize);
    document.addEventListener("visibilitychange", onVisibilityChange);
    rafId = requestAnimationFrame(frame);
  });

  new MutationObserver(function () { cachedRgb = rgb(); })
    .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
})();

(function () {
  var THEMES = ["white", "yellow", "blue", "dark"];
  var LABELS = { white: "Pure White", yellow: "Warm Yellow", blue: "Cool Blue", dark: "Dark" };

  function applyTheme(theme) {
    if (theme === "white") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", theme);
    }
    try { localStorage.setItem("rh-ui-theme", theme); } catch (e) {}
    document.querySelectorAll(".theme-dot").forEach(function (dot) {
      dot.classList.toggle("active", dot.dataset.theme === theme);
    });
  }

  var saved = "white";
  try { saved = localStorage.getItem("rh-ui-theme") || "white"; } catch (e) {}
  applyTheme(saved);

  document.addEventListener("DOMContentLoaded", function () {
    var switcher = document.createElement("div");
    switcher.id = "theme-switcher";
    switcher.setAttribute("aria-label", "Choose colour theme");
    THEMES.forEach(function (theme) {
      var btn = document.createElement("button");
      btn.className = "theme-dot";
      btn.dataset.theme = theme;
      btn.title = LABELS[theme];
      btn.setAttribute("aria-label", LABELS[theme]);
      btn.addEventListener("click", function () { applyTheme(theme); });
      switcher.appendChild(btn);
    });
    document.body.appendChild(switcher);
    applyTheme(saved);
  });
})();

(function () {
  var ws;
  var running = false;
  var interrupting = false;
  var pendingAskId = "";
  var keepSubmittedMessageOnReset = false;
  var autoFollowTimeline = true;
  var conversationStarted = false;
  var images = [];
  var COLLAPSED_STEP_HEIGHT = 220;

  var workspaceInput = document.getElementById("workspaceInput");
  var workspaceStrip = document.getElementById("workspaceStrip");
  var promptInput = document.getElementById("promptInput");
  var runBtn = document.getElementById("runBtn");
  var newBtn = document.getElementById("newBtn");
  var modelSelect = document.getElementById("modelSelect");
  var modelDropdown = document.getElementById("modelDropdown");
  var modelDropdownButton = document.getElementById("modelDropdownButton");
  var modelSelectLabel = document.getElementById("modelSelectLabel");
  var modelOptions = document.getElementById("modelOptions");
  var pickWorkspaceBtn = document.getElementById("pickWorkspaceBtn");
  var attachBtn = document.getElementById("attachBtn");
  var imageInput = document.getElementById("imageInput");
  var imagePreview = document.getElementById("imagePreview");
  var dropZone = document.getElementById("dropZone");
  var timeline = document.getElementById("timeline");
  var statusPill = document.getElementById("statusPill");
  var workspaceMeta = document.getElementById("workspaceMeta");
  var workspaceModal = document.getElementById("workspaceModal");
  var workspaceCloseBtn = document.getElementById("workspaceCloseBtn");
  var workspacePathInput = document.getElementById("workspacePathInput");
  var workspaceGoBtn = document.getElementById("workspaceGoBtn");
  var workspaceRoots = document.getElementById("workspaceRoots");
  var workspaceList = document.getElementById("workspaceList");
  var workspaceUseBtn = document.getElementById("workspaceUseBtn");
  var workspacePickerHint = document.getElementById("workspacePickerHint");
  var currentWorkspacePath = "";
  var fileToken = "";
  var defaultPromptPlaceholder = promptInput.getAttribute("placeholder") || "Message ResearchHarness";
  var mermaidCounter = 0;

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function protectMathSegments(text) {
    var segments = [];
    var protectedText = String(text || "").replace(/(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\))/g, function (match) {
      var token = "@@RH_MATH_" + segments.length + "@@";
      segments.push({ token: token, text: match });
      return token;
    });
    return { text: protectedText, segments: segments };
  }

  function restoreMathSegments(html, segments) {
    var restored = String(html || "");
    (segments || []).forEach(function (segment) {
      restored = restored.split(segment.token).join(escapeHtml(segment.text));
    });
    return restored;
  }

  function isRemoteOrInlineImageSrc(src) {
    return /^(https?:|data:|blob:|#|\/api\/|\/static\/)/i.test(String(src || "").trim());
  }

  function rewriteWorkspaceImageSources(html) {
    if (!fileToken) return html;
    var template = document.createElement("template");
    template.innerHTML = html;
    template.content.querySelectorAll("img").forEach(function (img) {
      var src = img.getAttribute("src") || "";
      if (!src || isRemoteOrInlineImageSrc(src)) return;
      img.setAttribute("src", "/api/workspace-file?token=" + encodeURIComponent(fileToken) + "&path=" + encodeURIComponent(src));
    });
    return template.innerHTML;
  }

  function normalizeMarkdownImageDestinations(text) {
    return String(text || "").replace(/!\[([^\]\n]*)\]\(([^)\n]+)\)/g, function (match, alt, target) {
      var src = String(target || "").trim();
      if (!src || src[0] === "<" || !/\s/.test(src) || isRemoteOrInlineImageSrc(src)) return match;
      if (/[<>]/.test(src)) return match;
      return "![" + alt + "](<" + src + ">)";
    });
  }

  function unwrapFullMarkdownFence(text) {
    var source = String(text || "").trim();
    var match = /^(```|~~~)[ \t]*(markdown|md|gfm)[^\n]*\n([\s\S]*?)\n\1[ \t]*$/i.exec(source);
    return match ? match[3] : text;
  }

  function renderMathInMarkdown(container) {
    if (!window.renderMathInElement) return;
    container.querySelectorAll(".markdown-body").forEach(function (body) {
      try {
        window.renderMathInElement(body, {
          delimiters: [
            { left: "$$", right: "$$", display: true },
            { left: "\\[", right: "\\]", display: true },
            { left: "\\(", right: "\\)", display: false }
          ],
          ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
          throwOnError: false
        });
      } catch (e) {
        console.warn("Math rendering failed.", e);
      }
    });
  }

  function renderMermaidInMarkdown(container) {
    if (!window.mermaid) return;
    try {
      window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
    } catch (e) {
      console.warn("Mermaid initialization failed.", e);
      return;
    }
    container.querySelectorAll(".markdown-body pre code").forEach(function (code) {
      var className = String(code.className || "").toLowerCase();
      if (!className.split(/\s+/).some(function (name) {
        return name === "language-mermaid" || name === "lang-mermaid" || name === "language-mmd" || name === "lang-mmd";
      })) return;
      var pre = code.closest("pre");
      if (!pre) return;
      var source = code.textContent || "";
      var target = document.createElement("div");
      var id = "rh-mermaid-" + (++mermaidCounter);
      target.className = "mermaid-chart";
      window.mermaid.render(id, source).then(function (result) {
        target.innerHTML = result.svg || "";
        pre.replaceWith(target);
      }).catch(function (e) {
        console.warn("Mermaid rendering failed.", e);
      });
    });
  }

  function renderMarkdown(text) {
    if (!window.marked || !window.DOMPurify) {
      console.warn("Markdown renderer unavailable; falling back to plain text.");
      return "<pre>" + escapeHtml(text) + "</pre>";
    }
    try {
      var normalizedText = normalizeMarkdownImageDestinations(unwrapFullMarkdownFence(text));
      var protectedMath = protectMathSegments(normalizedText);
      var rawHtml = window.marked.parse(protectedMath.text, { gfm: true, breaks: false, async: false });
      rawHtml = rewriteWorkspaceImageSources(rawHtml);
      var safeHtml = window.DOMPurify.sanitize(rawHtml, { USE_PROFILES: { html: true } });
      safeHtml = restoreMathSegments(safeHtml, protectedMath.segments);
      return '<div class="markdown-body">' + safeHtml + "</div>";
    } catch (e) {
      console.warn("Markdown rendering failed; falling back to plain text.", e);
      return "<pre>" + escapeHtml(text) + "</pre>";
    }
  }

  function setStatus(text, kind) {
    statusPill.textContent = text;
    statusPill.className = "status " + (kind || "idle");
  }

  function setWorkspaceSelected(path) {
    workspaceInput.value = path;
    workspaceMeta.textContent = "Workspace selected: " + path;
  }

  function closeModelDropdown() {
    if (!modelDropdown || !modelDropdownButton) return;
    modelDropdown.classList.remove("open");
    if (modelOptions) modelOptions.classList.remove("open");
    modelDropdownButton.setAttribute("aria-expanded", "false");
  }

  function positionModelOptions() {
    if (!modelDropdownButton || !modelOptions) return;
    var rect = modelDropdownButton.getBoundingClientRect();
    modelOptions.style.setProperty("--model-options-top", Math.round(rect.bottom + 8) + "px");
    modelOptions.style.setProperty("--model-options-right", Math.max(12, Math.round(window.innerWidth - rect.right)) + "px");
  }

  function setModelValue(value) {
    if (!modelSelect || !value) return;
    modelSelect.value = value;
    if (modelSelectLabel) modelSelectLabel.textContent = value;
    if (!modelOptions) return;
    modelOptions.querySelectorAll(".model-option").forEach(function (option) {
      var selected = option.getAttribute("data-model-value") === value;
      option.classList.toggle("selected", selected);
      option.setAttribute("aria-selected", selected ? "true" : "false");
    });
  }

  function setModelControlDisabled(disabled) {
    if (modelSelect) modelSelect.disabled = disabled;
    if (modelDropdownButton) modelDropdownButton.disabled = disabled;
    if (disabled) closeModelDropdown();
  }

  function setupModelDropdown() {
    if (!modelDropdown || !modelDropdownButton || !modelOptions || !modelSelect) return;
    if (modelOptions.parentElement !== document.body) document.body.appendChild(modelOptions);
    setModelValue(modelSelect.value || "gpt-5.5");
    modelDropdownButton.addEventListener("click", function () {
      if (modelDropdownButton.disabled) return;
      var shouldOpen = !modelDropdown.classList.contains("open");
      if (shouldOpen) positionModelOptions();
      modelDropdown.classList.toggle("open", shouldOpen);
      modelOptions.classList.toggle("open", shouldOpen);
      modelDropdownButton.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
    });
    modelOptions.querySelectorAll(".model-option").forEach(function (option) {
      option.addEventListener("click", function () {
        setModelValue(option.getAttribute("data-model-value"));
        closeModelDropdown();
      });
    });
    document.addEventListener("click", function (event) {
      if (!modelDropdown.contains(event.target) && !modelOptions.contains(event.target)) closeModelDropdown();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeModelDropdown();
    });
    window.addEventListener("resize", function () {
      if (modelDropdown.classList.contains("open")) positionModelOptions();
    });
  }

  function updateComposerMode() {
    if (pendingAskId) {
      runBtn.disabled = false;
      runBtn.classList.remove("is-running");
      runBtn.textContent = "Reply";
      promptInput.placeholder = defaultPromptPlaceholder;
      setModelControlDisabled(true);
      return;
    }
    runBtn.disabled = running && interrupting;
    runBtn.classList.toggle("is-running", running);
    runBtn.textContent = running ? (interrupting ? "Stopping" : "Stop") : "Run";
    promptInput.placeholder = defaultPromptPlaceholder;
    setModelControlDisabled(running);
  }

  function setRunning(active, statusText) {
    running = active;
    if (!active) interrupting = false;
    updateComposerMode();
    setStatus(statusText || (active ? "Running" : "Idle"), active ? "running" : "idle");
  }

  function clearTimeline() {
    autoFollowTimeline = true;
    timeline.innerHTML = ''
      + '<div class="welcome">'
      + '<h1>What should the agent do?</h1>'
      + '<p>Ask a question, attach images, choose a local workspace, and watch tool calls stream here.</p>'
      + '</div>';
  }

  function ensureTimelineReady() {
    var welcome = timeline.querySelector(".welcome");
    if (welcome) welcome.remove();
  }

  function isNearBottom() {
    return timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
  }

  function scrollTimeline(force) {
    if (!force && !autoFollowTimeline) return;
    requestAnimationFrame(function () {
      timeline.scrollTop = timeline.scrollHeight;
      requestAnimationFrame(function () {
        timeline.scrollTop = timeline.scrollHeight;
        autoFollowTimeline = isNearBottom();
      });
    });
  }

  function syncTimelineFollowMode() {
    autoFollowTimeline = isNearBottom();
  }

  function updateEventToggle(node) {
    var toggle = node.querySelector(".event-toggle");
    if (!toggle) return;
    toggle.setAttribute("aria-expanded", node.classList.contains("collapsed") ? "false" : "true");
  }

  function eventBody(node) {
    return node.querySelector(".event-body");
  }

  function eventCanCollapse(node) {
    return node.classList.contains("can-collapse");
  }

  function refreshEventCollapseCapability(node) {
    var body = eventBody(node);
    var toggle = node.querySelector(".event-toggle");
    if (!body) return;
    var shouldCollapse = body.scrollHeight > COLLAPSED_STEP_HEIGHT + 8;
    node.classList.toggle("can-collapse", shouldCollapse);
    if (toggle) toggle.hidden = !shouldCollapse;
    if (!shouldCollapse) {
      node.classList.remove("collapsed");
      body.style.maxHeight = "none";
    }
    updateEventToggle(node);
  }

  function setEventExpanded(node, expanded, animate) {
    var body = eventBody(node);
    if (!body) {
      node.classList.toggle("collapsed", !expanded);
      updateEventToggle(node);
      return;
    }
    refreshEventCollapseCapability(node);
    if (!eventCanCollapse(node)) return;

    if (expanded) {
      node.classList.remove("collapsed");
      body.style.maxHeight = body.scrollHeight + "px";
      if (!animate) {
        body.style.maxHeight = "none";
      } else {
        body.addEventListener("transitionend", function onEnd(event) {
          if (event.propertyName !== "max-height") return;
          body.removeEventListener("transitionend", onEnd);
          if (!node.classList.contains("collapsed")) {
            body.style.maxHeight = "none";
          }
        });
      }
    } else {
      if (body.style.maxHeight === "none" || !body.style.maxHeight) {
        body.style.maxHeight = body.scrollHeight + "px";
      }
      body.offsetHeight;
      node.classList.add("collapsed");
      body.style.maxHeight = COLLAPSED_STEP_HEIGHT + "px";
    }
    updateEventToggle(node);
  }

  function toggleEvent(node) {
    if (node.classList.contains("latest") || !eventCanCollapse(node)) return;
    setEventExpanded(node, node.classList.contains("collapsed"), true);
  }

  function addEvent(kind, title, bodyHtml, badges) {
    var shouldFollow = autoFollowTimeline || isNearBottom();
    ensureTimelineReady();
    timeline.querySelectorAll(".event.latest").forEach(function (eventNode) {
      eventNode.classList.remove("latest");
      setEventExpanded(eventNode, false, true);
      updateEventToggle(eventNode);
    });
    var badgeHtml = (badges || []).map(function (badge) {
      return '<span class="badge">' + escapeHtml(badge) + "</span>";
    }).join("");
    var node = document.createElement("article");
    node.className = "event event-" + kind + " latest";
    node.innerHTML = ''
      + '<div class="event-head">'
      + '<div class="event-title">' + escapeHtml(title) + badgeHtml + '</div>'
      + '<button class="event-toggle" type="button" aria-label="Toggle step details"></button>'
      + '</div>'
      + '<div class="event-body"><div class="event-body-inner">' + bodyHtml + '</div></div>';
    node.querySelector(".event-toggle").addEventListener("click", function (event) {
      event.stopPropagation();
      toggleEvent(node);
    });
    node.addEventListener("click", function () {
      toggleEvent(node);
    });
    timeline.appendChild(node);
    renderMathInMarkdown(node);
    renderMermaidInMarkdown(node);
    setEventExpanded(node, true, false);
    scrollTimeline(shouldFollow);
  }

  function addMessage(kind, text, attachedImages) {
    autoFollowTimeline = true;
    ensureTimelineReady();
    var node = document.createElement("article");
    node.className = "message " + kind;
    var imageHtml = "";
    (attachedImages || []).forEach(function (image) {
      imageHtml += '<img class="message-image" alt="" src="' + image.data_url + '">';
    });
    node.innerHTML = '<div class="message-body">'
      + (imageHtml ? '<div class="message-images">' + imageHtml + '</div>' : '')
      + '<pre>' + escapeHtml(text) + '</pre>'
      + '</div>';
    timeline.appendChild(node);
    scrollTimeline(true);
  }

  function formatJson(value) {
    try {
      return JSON.stringify(value, null, 2);
    } catch (e) {
      return String(value);
    }
  }

  function renderTrace(row) {
    if (!row || row.capture_type === "llm_call" || row.capture_type === "compaction") return;
    var role = row.role || "";
    var turn = row.turn_index || 0;
    var text = row.text || "";
    if (role === "system") return;
    if (role === "user" && turn === 0) return;

    if (role === "assistant") {
      var tools = Array.isArray(row.tool_names) ? row.tool_names : [];
      var args = Array.isArray(row.tool_arguments) ? row.tool_arguments : [];
      var body = "";
      if (text.trim()) {
        body += (!tools.length && row.termination === "result")
          ? renderMarkdown(text)
          : "<pre>" + escapeHtml(text) + "</pre>";
      }
      if (tools.length) {
        body += '<div class="tool-grid">';
        tools.forEach(function (name, idx) {
          body += '<div class="tool-call"><div class="tool-name">' + escapeHtml(name)
            + '</div><pre>' + escapeHtml(formatJson(args[idx] || {})) + '</pre></div>';
        });
        body += "</div>";
      }
      if (!body) body = '<pre>(empty assistant output)</pre>';
      if (row.error) body += '<pre class="error-text">' + escapeHtml(row.error) + "</pre>";
      addEvent("assistant", "Assistant", body, ["round " + turn]);
      return;
    }

    if (role === "tool") {
      var toolName = Array.isArray(row.tool_names) && row.tool_names.length ? row.tool_names[0] : "Tool";
      var toolBody = "<pre>" + escapeHtml(text) + "</pre>";
      if (row.error) toolBody += '<pre class="error-text">' + escapeHtml(row.error) + "</pre>";
      addEvent("tool", toolName + " result", toolBody, ["round " + turn]);
      return;
    }

    if (role === "runtime") {
      if (!text.trim() && !row.error && !row.termination) return;
      var runtimeBody = "<pre>" + escapeHtml(text || row.termination || "") + "</pre>";
      if (row.error) runtimeBody += '<pre class="error-text">' + escapeHtml(row.error) + "</pre>";
      addEvent("runtime", "Runtime", runtimeBody, turn ? ["round " + turn] : []);
      return;
    }

    if (role === "user") {
      addEvent("runtime", "Runtime message", "<pre>" + escapeHtml(text) + "</pre>", ["round " + turn]);
    }
  }

  function connect() {
    var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(protocol + "//" + window.location.host + "/ws");
    ws.onopen = function () {
      setStatus("Connected", "idle");
    };
    ws.onclose = function () {
      fileToken = "";
      clearAskRequest();
      setRunning(false, "Disconnected");
      setStatus("Disconnected", "error");
    };
    ws.onmessage = function (event) {
      var message = JSON.parse(event.data);
      if (message.type === "ready") {
        setStatus("Connected", "idle");
      } else if (message.type === "conversation_reset") {
        fileToken = "";
        if (keepSubmittedMessageOnReset) {
          keepSubmittedMessageOnReset = false;
          ensureTimelineReady();
        } else {
          clearTimeline();
        }
        conversationStarted = false;
        clearAskRequest();
      } else if (message.type === "uploaded_images") {
        addEvent("runtime", "Uploaded images saved", "<pre>" + escapeHtml((message.paths || []).join("\n")) + "</pre>", []);
      } else if (message.type === "run_started") {
        fileToken = message.file_token || "";
        setRunning(true, "Running");
      } else if (message.type === "interrupt_requested") {
        interrupting = true;
        updateComposerMode();
        setStatus("Interrupting", "running");
      } else if (message.type === "trace") {
        renderTrace(message.row);
      } else if (message.type === "ask_user") {
        showAskRequest(message);
      } else if (message.type === "run_finished") {
        conversationStarted = true;
        setRunning(false, "Done");
        clearAskRequest();
        setStatus("Done", "done");
      } else if (message.type === "run_error") {
        keepSubmittedMessageOnReset = false;
        clearAskRequest();
        setRunning(false, "Error");
        setStatus("Error", "error");
        addEvent("runtime", "Error", '<pre class="error-text">' + escapeHtml(message.error || "unknown error") + "</pre>", []);
      }
    };
  }

  function showAskRequest(message) {
    pendingAskId = message.request_id || "";
    var question = message.question || "Question";
    var context = message.context || "";
    var body = "<pre>" + escapeHtml(question) + "</pre>";
    if (context) body += '<pre class="muted-text">' + escapeHtml(context) + "</pre>";
    addEvent("runtime", "Agent question", body, ["AskUser"]);
    setStatus("Waiting for input", "running");
    updateComposerMode();
    promptInput.focus();
  }

  function clearAskRequest() {
    pendingAskId = "";
    updateComposerMode();
  }

  function sendStart() {
    if (pendingAskId) {
      sendAskUserAnswer();
      return;
    }
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setStatus("Disconnected", "error");
      return;
    }
    if (running) {
      sendInterrupt();
      return;
    }
    var prompt = promptInput.value.trim();
    if (!prompt) return;
    var sentImages = images.slice();
    var continueConversation = conversationStarted;
    if (!continueConversation) clearTimeline();
    addMessage("user", prompt, sentImages);
    keepSubmittedMessageOnReset = !continueConversation;
    setRunning(true, "Starting");
    ws.send(JSON.stringify({
      type: "start",
      prompt: prompt,
      workspace_root: workspaceInput.value,
      model_name: modelSelect ? modelSelect.value : "",
      images: sentImages,
      continue_conversation: continueConversation
    }));
    promptInput.value = "";
    promptInput.style.height = "auto";
    images = [];
    renderImages();
  }

  function sendInterrupt() {
    if (!running || interrupting || !ws || ws.readyState !== WebSocket.OPEN) return;
    interrupting = true;
    updateComposerMode();
    setStatus("Interrupting", "running");
    ws.send(JSON.stringify({ type: "interrupt" }));
  }

  function sendAskUserAnswer() {
    if (!pendingAskId || !ws || ws.readyState !== WebSocket.OPEN) return;
    var answer = promptInput.value.trim();
    if (!answer) return;
    var requestId = pendingAskId;
    addMessage("user", answer, []);
    ws.send(JSON.stringify({ type: "ask_user_answer", request_id: requestId, answer: answer }));
    pendingAskId = "";
    promptInput.value = "";
    promptInput.style.height = "auto";
    updateComposerMode();
    setStatus("Running", "running");
  }

  function addImageFiles(fileList) {
    Array.from(fileList || []).forEach(function (file) {
      if (!file.type || !file.type.startsWith("image/")) return;
      var reader = new FileReader();
      reader.onload = function () {
        images.push({ name: file.name, data_url: String(reader.result || "") });
        renderImages();
      };
      reader.readAsDataURL(file);
    });
  }

  function renderImages() {
    imagePreview.innerHTML = "";
    images.forEach(function (image, idx) {
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "image-chip";
      chip.title = "Remove image";
      chip.innerHTML = '<img alt="" src="' + image.data_url + '"><span>' + escapeHtml(image.name || "image") + "</span>";
      chip.addEventListener("click", function () {
        images.splice(idx, 1);
        renderImages();
      });
      imagePreview.appendChild(chip);
    });
  }

  function openWorkspaceModal() {
    workspaceModal.classList.remove("hidden");
    loadWorkspaceDirectory(workspaceInput.value.trim());
  }

  function closeWorkspaceModal() {
    workspaceModal.classList.add("hidden");
  }

  function setWorkspacePickerBusy(text) {
    workspaceList.innerHTML = '<div class="dir-empty">' + escapeHtml(text || "Loading...") + "</div>";
    workspacePickerHint.textContent = text || "Loading...";
  }

  function renderWorkspaceError(message) {
    workspaceList.innerHTML = '<div class="dir-empty error-text">' + escapeHtml(message) + "</div>";
    workspacePickerHint.textContent = "Paste a valid existing folder path, then press Go.";
  }

  function directoryRow(label, path, actionLabel, onClick) {
    var row = document.createElement("button");
    row.type = "button";
    row.className = "dir-row";
    row.innerHTML = ''
      + '<span class="dir-icon">&rsaquo;</span>'
      + '<span class="dir-main"><strong>' + escapeHtml(label) + '</strong><small>' + escapeHtml(path) + '</small></span>'
      + '<span class="dir-action">' + escapeHtml(actionLabel || "Open") + '</span>';
    row.addEventListener("click", onClick);
    return row;
  }

  function renderWorkspacePicker(payload) {
    currentWorkspacePath = payload.path || "";
    workspacePathInput.value = currentWorkspacePath;
    workspaceRoots.innerHTML = "";
    (payload.roots || []).forEach(function (root) {
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "root-chip";
      chip.textContent = root.label || root.path;
      chip.title = root.path || "";
      chip.addEventListener("click", function () {
        loadWorkspaceDirectory(root.path || "");
      });
      workspaceRoots.appendChild(chip);
    });

    workspaceList.innerHTML = "";
    if (payload.parent) {
      workspaceList.appendChild(directoryRow("..", payload.parent, "Parent", function () {
        loadWorkspaceDirectory(payload.parent);
      }));
    }
    (payload.entries || []).forEach(function (entry) {
      workspaceList.appendChild(directoryRow(entry.name, entry.path, "Open", function () {
        loadWorkspaceDirectory(entry.path);
      }));
    });
    if (!payload.parent && !(payload.entries || []).length) {
      workspaceList.innerHTML = '<div class="dir-empty">No readable child folders.</div>';
    }
    workspacePickerHint.textContent = payload.truncated
      ? "Directory list was truncated. Paste a deeper path if needed."
      : "Current folder will be used when you click Use this folder.";
  }

  async function loadWorkspaceDirectory(path) {
    setWorkspacePickerBusy("Loading folders...");
    try {
      var url = "/api/workspace-directories";
      if (path) url += "?path=" + encodeURIComponent(path);
      var response = await fetch(url);
      var payload = await response.json();
      if (!response.ok || payload.error) {
        renderWorkspaceError(payload.error || "Cannot open this folder.");
        return;
      }
      renderWorkspacePicker(payload);
    } catch (error) {
      renderWorkspaceError(String(error));
    }
  }

  setupModelDropdown();

  runBtn.addEventListener("click", sendStart);
  timeline.addEventListener("scroll", syncTimelineFollowMode);
  timeline.addEventListener("wheel", function (event) {
    if (event.deltaY < 0) autoFollowTimeline = false;
  }, { passive: true });
  timeline.addEventListener("touchmove", function () {
    autoFollowTimeline = false;
  }, { passive: true });
  promptInput.addEventListener("keydown", function (event) {
    if (event.isComposing) return;
    if (event.key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.metaKey) {
      event.preventDefault();
      sendStart();
    }
  });
  promptInput.addEventListener("input", function () {
    promptInput.style.height = "auto";
    promptInput.style.height = Math.min(promptInput.scrollHeight, 180) + "px";
  });
  newBtn.addEventListener("click", function () {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "new" }));
    if (!running) {
      promptInput.value = "";
      images = [];
      renderImages();
      clearTimeline();
      clearAskRequest();
      conversationStarted = false;
      setRunning(false, "Idle");
    }
  });
  attachBtn.addEventListener("click", function () {
    imageInput.click();
  });
  imageInput.addEventListener("change", function (event) { addImageFiles(event.target.files); });

  pickWorkspaceBtn.addEventListener("click", function () {
    openWorkspaceModal();
  });

  workspaceCloseBtn.addEventListener("click", closeWorkspaceModal);
  workspaceModal.addEventListener("click", function (event) {
    if (event.target === workspaceModal) closeWorkspaceModal();
  });
  workspaceGoBtn.addEventListener("click", function () {
    loadWorkspaceDirectory(workspacePathInput.value.trim());
  });
  workspacePathInput.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      loadWorkspaceDirectory(workspacePathInput.value.trim());
    }
  });
  workspaceUseBtn.addEventListener("click", function () {
    if (!currentWorkspacePath) return;
    setWorkspaceSelected(currentWorkspacePath);
    closeWorkspaceModal();
  });

  ["dragenter", "dragover"].forEach(function (name) {
    dropZone.addEventListener(name, function (event) {
      event.preventDefault();
      dropZone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach(function (name) {
    dropZone.addEventListener(name, function (event) {
      event.preventDefault();
      dropZone.classList.remove("dragover");
    });
  });
  dropZone.addEventListener("drop", function (event) {
    addImageFiles(event.dataTransfer.files);
  });
  document.addEventListener("paste", function (event) {
    var files = [];
    Array.from(event.clipboardData ? event.clipboardData.items : []).forEach(function (item) {
      if (item.kind === "file") {
        var file = item.getAsFile();
        if (file) files.push(file);
      }
    });
    if (files.length) addImageFiles(files);
  });

  connect();
})();
