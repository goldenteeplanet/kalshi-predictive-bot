document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.matches("form[data-action]")) {
    return;
  }
  event.preventDefault();
  const button = form.querySelector("button[type='submit']");
  const originalButtonText = button ? button.textContent : "";
  const isLearningAction = new URL(form.action, window.location.origin).pathname === "/learning/run-once";
  const messagePanel = ensureActionMessage(form);
  setActionMessage(messagePanel, "", "");
  if (button) {
    button.disabled = true;
    if (isLearningAction) {
      button.textContent = "Running...";
    }
  }
  try {
    const url = new URL(form.action, window.location.origin);
    const formData = new FormData(form);
    formData.forEach((value, key) => {
      if (value !== "") {
        url.searchParams.set(key, value);
      }
    });
    const response = await fetch(url, {
      method: "POST",
      headers: { Accept: "application/json" },
    });
    const payload = await parseActionResponse(response, url);
    if (!response.ok || payload.ok === false) {
      console.error("Action returned failure JSON", {
        url: url.toString(),
        status: response.status,
        payload,
      });
      throw new Error(actionFailureMessage(payload, response));
    }
    const successMessage = payload.message || payload.path || payload.ticker || "Done";
    setActionMessage(messagePanel, successMessage, "success");
    alert(`${payload.status || "OK"}: ${successMessage}`);
    window.location.reload();
  } catch (error) {
    console.error("Action failed", {
      action: form.action,
      error,
    });
    setActionMessage(messagePanel, userFacingActionError(error), "error");
  } finally {
    if (button) {
      button.disabled = false;
      if (isLearningAction) {
        button.textContent = originalButtonText;
      }
    }
  }
});

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-payout-calculator]").forEach(updatePayoutCalculator);
  setupPageAutoRefresh();
  setupProgressDashboard();
});

function setupProgressDashboard() {
  const root = document.querySelector("[data-progress-dashboard]");
  if (!root) return;
  const interval = Number(root.dataset.pollSeconds || 15) * 1000;
  const timeout = Number(root.dataset.timeoutSeconds || 5) * 1000;
  const maxFailures = Number(root.dataset.maxFailures || 3);
  let failures = 0;
  let paused = false;
  let timerId = null;
  let previousSummary = "";
  const toggle = root.querySelector("[data-progress-poll-toggle]");
  const announcer = root.querySelector("[data-progress-announcer]");
  const schedule = () => {
    if (!paused) timerId = window.setTimeout(poll, interval);
  };
  const poll = async () => {
    if (paused) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeout);
    root.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(root.dataset.endpoint, {signal: controller.signal, headers: {Accept: "application/json"}});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      renderProgressDashboard(root, data);
      failures = 0;
      root.querySelector("[data-progress-connection]").textContent = "CONNECTED";
      const summary = `${data.active_process.state}: ${data.active_process.name}. Execution ${data.execution.label}.`;
      if (announcer && previousSummary && summary !== previousSummary) announcer.textContent = summary;
      previousSummary = summary;
    } catch (error) {
      failures += 1;
      const connection = root.querySelector("[data-progress-connection]");
      connection.textContent = failures >= maxFailures ? "POLLING PAUSED" : `RETRY ${failures}/${maxFailures}`;
      if (announcer && failures >= maxFailures) announcer.textContent = "Automatic status polling paused after repeated failures.";
      if (failures >= maxFailures) paused = true;
    } finally {
      root.setAttribute("aria-busy", "false");
      window.clearTimeout(timer);
    }
    schedule();
  };
  if (toggle) toggle.addEventListener("click", () => {
    paused = !paused;
    toggle.setAttribute("aria-pressed", String(paused));
    toggle.textContent = paused ? "Resume automatic updates" : "Pause automatic updates";
    if (paused) {
      if (timerId !== null) window.clearTimeout(timerId);
      root.querySelector("[data-progress-connection]").textContent = "PAUSED";
      if (announcer) announcer.textContent = "Automatic status updates paused.";
    } else {
      failures = 0;
      root.querySelector("[data-progress-connection]").textContent = "RESUMING";
      if (announcer) announcer.textContent = "Automatic status updates resumed.";
      poll();
    }
  });
  schedule();
}

function renderProgressDashboard(root, data) {
  const values = {
    execution: data.execution.label, generated_at: data.generated_at || "unknown",
    process_state: data.active_process.state, process_name: data.active_process.name,
    pid: data.active_process.pid || "none", runtime: data.active_process.runtime,
    stage: data.active_process.stage, eta: data.active_process.estimated_remaining || "unknown",
    completed_work: data.active_process.completed_units === null ? "unknown" : `${data.active_process.completed_units} / ${data.active_process.total_units}`,
    eta_reason: `ETA evidence: ${data.active_process.eta_reason}`,
    writer_state: data.writer.state, writer_pid: data.writer.pid || "none",
    locks: data.writer.lock_status, safe_write: data.writer.safe_to_start_write ? "YES" : "NO",
    backup_state: data.backup.state, backup_integrity: data.backup.integrity,
    backup_path: data.backup.path || "none", backup_sha: data.backup.sha256_status || "unknown",
    scheduler_state: data.scheduler.state, scheduler_cycle: data.scheduler.cycle,
    scheduler_stage: data.scheduler.stage || "unknown", scheduler_eta: data.scheduler.estimated_remaining || "not calculable",
    verification_state: data.backup_verification.state,
    verification_stage: data.backup_verification.stage,
    verification_pid: data.backup_verification.pid || "none",
    verification_elapsed: data.backup_verification.elapsed_label,
    verification_progress: data.backup_verification.progress_percent_lower_bound === null ? "unknown" : `${data.backup_verification.progress_percent_lower_bound}%`,
    verification_eta: data.backup_verification.estimated_remaining_label,
    verification_eta_confidence: data.backup_verification.eta_confidence,
    verification_integrity: data.backup_verification.integrity_status,
    verification_sha: data.backup_verification.sha256_status,
    verification_blocked: data.backup_verification.deployment_blocked ? "YES" : "NO",
    pipeline_state: data.prov14b_pipeline.state,
    pipeline_current_stage: data.prov14b_pipeline.current_stage,
    pipeline_age: data.prov14b_pipeline.age_seconds === null ? "unavailable" : `${data.prov14b_pipeline.age_seconds}s`,
    pipeline_runtime_certified: data.prov14b_pipeline.runtime_certified ? "YES" : "NO",
    pipeline_deployment_blocked: data.prov14b_pipeline.deployment_blocked ? "YES" : "NO",
  };
  Object.entries(values).forEach(([key, value]) => {
    const node = root.querySelector(`[data-progress-field="${key}"]`);
    if (node) node.textContent = value;
  });
  const stateChip = root.querySelector('[data-progress-field="process_state"]');
  if (stateChip) stateChip.className = `state-chip state-${String(data.active_process.state).toLowerCase()}`;
  const progressBar = root.querySelector("[data-progress-bar]");
  if (progressBar) {
    const percent = data.active_process.progress_percent;
    if (percent === null) progressBar.removeAttribute("aria-valuenow");
    else progressBar.setAttribute("aria-valuenow", String(percent));
    const fill = progressBar.querySelector("div");
    if (fill) fill.style.width = `${percent || 0}%`;
  }
  const verificationBar = root.querySelector("[data-verification-progress-bar]");
  if (verificationBar) {
    const percent = data.backup_verification.progress_percent_lower_bound;
    if (percent === null) verificationBar.removeAttribute("aria-valuenow");
    else verificationBar.setAttribute("aria-valuenow", String(percent));
    const fill = verificationBar.querySelector("div");
    if (fill) fill.style.width = `${percent || 0}%`;
  }
  renderProv14bPipeline(root, data.prov14b_pipeline, data.prov14b_timeline, data.timeline_export);
  const alerts = root.querySelector("[data-progress-alerts]");
  alerts.replaceChildren(...(data.alerts.length ? data.alerts.map((item) => {
    const node = document.createElement("div"); node.className = `ops-alert severity-${String(item.severity).toLowerCase()}`;
    const title = document.createElement("strong"); title.textContent = item.code;
    const message = document.createElement("span"); message.textContent = item.message;
    node.append(title, message); return node;
  }) : [Object.assign(document.createElement("div"), {className: "ops-alert severity-info", textContent: "No active alerts"})]));
  const workstreams = root.querySelector("[data-progress-workstreams]");
  workstreams.replaceChildren(...data.workstreams.map((item) => {
    const card = document.createElement("article"); card.className = "workstream-card"; card.dataset.workstreamId = item.id;
    const head = document.createElement("div"), title = document.createElement("h3"), chip = document.createElement("span");
    title.textContent = item.name; chip.className = `state-chip state-${String(item.state).toLowerCase()}`; chip.textContent = item.state; head.append(title, chip);
    const phase = document.createElement("p"), strong = document.createElement("strong"); strong.textContent = item.current_phase; phase.append(strong);
    const next = document.createElement("p"); next.className = "muted"; next.textContent = `Next safe: ${item.next_safe_phase}`;
    const counts = document.createElement("small"); counts.textContent = `${item.completed.length} completed · ${item.blocked.length} blocked${item.reported ? "" : " · status unreported"}`;
    card.append(head, phase, next, counts); return card;
  }));
  const reports = root.querySelector("[data-progress-reports]");
  reports.replaceChildren(...data.reports.map((item) => {
    const row = document.createElement("tr");
    [item.phase, item.state, item.path, item.generated_at].forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 1) { const chip = document.createElement("span"); chip.className = `state-chip state-${String(value).toLowerCase()}`; chip.textContent = value; cell.append(chip); }
      else if (index === 2) { const code = document.createElement("code"); code.textContent = value; cell.append(code); }
      else cell.textContent = value;
      row.append(cell);
    }); return row;
  }));
  renderRoadmapSummary(root, data.roadmap_summary);
  renderCiCertification(root, data.ci_certification);
}

function renderProv14bPipeline(root, pipeline, timeline, timelineExport) {
  const values = {
    current_stage: pipeline.current_stage,
    age: pipeline.age_seconds === null ? "unavailable" : `${pipeline.age_seconds}s`,
    runtime_certified: pipeline.runtime_certified ? "YES" : "NO",
    deployment_blocked: pipeline.deployment_blocked ? "YES" : "NO",
  };
  Object.entries(values).forEach(([key, value]) => {
    const node = root.querySelector(`[data-pipeline-field="${key}"]`);
    if (node) node.textContent = value;
  });
  const state = root.querySelector('[data-pipeline-field="state"]');
  if (state) state.textContent = pipeline.state;
  if (state) state.className = `state-chip state-${String(pipeline.state).toLowerCase()}`;
  const alerts = root.querySelector("[data-pipeline-alerts]");
  if (alerts) alerts.replaceChildren(...pipeline.alerts.map((item) => {
    const card = document.createElement("article");
    card.className = `alert-card state-${item.severity === "CRITICAL" ? "failed" : "waiting"}`;
    const title = document.createElement("strong"); title.textContent = `${item.severity} · ${item.gate}`;
    const message = document.createElement("span"); message.textContent = item.message;
    const code = document.createElement("code"); code.textContent = item.code;
    card.append(title, message, code); return card;
  }));
  const stages = root.querySelector("[data-pipeline-backup-stages]");
  if (stages) stages.replaceChildren(...pipeline.backup_stages.map((item) => {
    const row = document.createElement("li"); row.className = `state-${String(item.state).toLowerCase()}`;
    const label = document.createElement("span"), strong = document.createElement("strong"); strong.textContent = item.label; label.append(strong);
    if (item.detail) { const detail = document.createElement("small"); detail.textContent = item.detail; label.append(detail); }
    const value = document.createElement("strong"); value.textContent = item.state; row.append(label, value); return row;
  }));
  const gates = root.querySelector("[data-pipeline-gates]");
  if (gates) gates.replaceChildren(...pipeline.gates.map((item) => {
    const card = document.createElement("article"); card.className = "workstream-card"; card.dataset.pipelineGate = item.id;
    const head = document.createElement("div"), title = document.createElement("h3"), chip = document.createElement("span");
    title.textContent = item.label; chip.className = `state-chip state-${String(item.state).toLowerCase()}`; chip.textContent = item.state; head.append(title, chip);
    const failures = document.createElement("p"); failures.textContent = `Failed gates: ${item.failed_count === null ? "unknown" : item.failed_count}`;
    const age = document.createElement("p"); age.textContent = `Evidence age: ${item.evidence_age_seconds === null ? "unavailable" : `${item.evidence_age_seconds}s`}`;
    const detail = document.createElement("p"); detail.className = "muted"; detail.textContent = item.detail || "No detail reported";
    const hash = document.createElement("code"); hash.className = "truncate"; hash.textContent = item.report_sha256 || "No report hash";
    const evidence = document.createElement("dl"); evidence.className = "evidence-detail-list";
    item.evidence_details.forEach((entry) => {
      const row = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = entry.label; dd.textContent = entry.value; row.append(dt, dd); evidence.append(row);
    });
    const artifact = item.artifact_href ? document.createElement("a") : document.createElement("small");
    artifact.textContent = item.artifact_href ? "Open local evidence" : "Local evidence link unavailable";
    if (item.artifact_href) { artifact.className = "text-link"; artifact.href = item.artifact_href; }
    card.append(head, failures, age, detail, hash, evidence, artifact); return card;
  }));
  if (!timeline) return;
  const timelineValues = {
    state: timeline.state,
    duration: timeline.duration_seconds === null ? "unavailable" : `${timeline.duration_seconds}s`,
    duration_state: timeline.duration_state,
    event_count: timeline.event_count,
  };
  Object.entries(timelineValues).forEach(([key, value]) => {
    const node = root.querySelector(`[data-pipeline-timeline-field="${key}"]`);
    if (node) node.textContent = value;
    if (node && key === "state") node.className = `state-chip state-${String(value).toLowerCase()}`;
  });
  const timelineRows = root.querySelector("[data-pipeline-timeline-events]");
  if (timelineRows) timelineRows.replaceChildren(...(timeline.events.length ? timeline.events.map((item) => {
    const row = document.createElement("tr"); if (item.resolved) row.className = "resolved-row";
    [item.timestamp, item.subject, item.event_type, item.before === null ? "—" : item.before, item.after].forEach((value) => {
      const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
    }); return row;
  }) : [(() => { const row = document.createElement("tr"), cell = document.createElement("td"); cell.colSpan = 5; cell.textContent = "No retained certification transitions yet."; row.append(cell); return row; })()]));
  if (!timelineExport) return;
  const exportValues = {
    status: timelineExport.status,
    transition_count: timelineExport.transition_count,
    entry_count: `${timelineExport.entry_count} / ${timelineExport.retention_limit || "unknown"}`,
    bundle_sha256: timelineExport.bundle_sha256 || "unavailable",
  };
  Object.entries(exportValues).forEach(([key, value]) => {
    const node = root.querySelector(`[data-timeline-export-field="${key}"]`);
    if (node) node.textContent = value;
    if (node && key === "status") node.className = `state-chip state-${String(value).toLowerCase()}`;
  });
  const files = root.querySelector("[data-timeline-export-files]");
  if (files) files.replaceChildren(...timelineExport.exports.map((item) => {
    const card = document.createElement("article"); card.className = "workstream-card";
    const head = document.createElement("div"), title = document.createElement("h3"), chip = document.createElement("span");
    title.textContent = item.kind; chip.textContent = item.verified ? "VERIFIED" : "FAILED"; chip.className = `state-chip state-${item.verified ? "passed" : "failed"}`; head.append(title, chip);
    const hash = document.createElement("code"); hash.className = "truncate"; hash.textContent = item.sha256 || "No hash"; card.append(head, hash);
    if (item.href) { const link = document.createElement("a"); link.className = "text-link"; link.href = item.href; link.textContent = `Download ${item.name}`; card.append(link); }
    return card;
  }));
}

function renderRoadmapSummary(root, summary) {
  if (!summary) return;
  const status = root.querySelector("[data-roadmap-state]");
  if (status) {
    status.textContent = summary.state;
    status.className = `state-chip state-${String(summary.state).toLowerCase()}`;
  }
  const lanes = root.querySelector("[data-roadmap-lanes]");
  if (!lanes) return;
  lanes.replaceChildren(...summary.lanes.map((lane) => {
    const card = document.createElement("article");
    card.className = "workstream-card"; card.dataset.roadmapId = lane.id;
    const head = document.createElement("div"), title = document.createElement("h3"), chip = document.createElement("span");
    title.textContent = lane.name; chip.className = `state-chip state-${String(lane.state).toLowerCase()}`; chip.textContent = lane.state; head.append(title, chip);
    const phase = document.createElement("p"), strong = document.createElement("strong"); strong.textContent = lane.current_phase; phase.append(strong);
    const progress = document.createElement("p"); progress.textContent = lane.progress_label;
    const blocker = document.createElement("p"); blocker.className = "muted"; blocker.textContent = `Blocker: ${lane.blocker}`;
    const next = document.createElement("p"); next.className = "muted"; next.textContent = `Next: ${lane.next_phase}`;
    const metrics = document.createElement("dl");
    lane.metrics.forEach((metric) => { const row = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = metric.label; dd.textContent = metric.value; row.append(dt, dd); metrics.append(row); });
    const evidence = document.createElement("small"); evidence.textContent = lane.evidence.length ? `${lane.evidence.length} evidence artifact(s)` : "No evidence artifact reported";
    card.append(head, phase, progress, blocker, next, metrics, evidence); return card;
  }));
}

function renderCiCertification(root, certification) {
  if (!certification) return;
  const gate = certification.gate || {};
  const workflow = certification.workflow || {};
  const values = {
    status: certification.status, gate_status: gate.status || "MISSING",
    gate_sha: gate.sha256 || "none", bundle_sha: gate.bundle_digest || "none",
    workflow_status: workflow.status || "MISSING", workflow_sha: workflow.workflow_sha256 || "none",
    drift_count: `${certification.drift_failures.length} failures`,
    drift_summary: certification.drift_failures.length ? "Review failed certification evidence" : "No unexplained drift",
  };
  Object.entries(values).forEach(([key, value]) => {
    const node = root.querySelector(`[data-ci-field="${key}"]`);
    if (node) node.textContent = value;
  });
  const status = root.querySelector('[data-ci-field="status"]');
  if (status) status.className = `state-chip state-${String(certification.status).toLowerCase()}`;
  const history = root.querySelector("[data-ci-history]");
  if (!history) return;
  history.replaceChildren(...certification.history.map((item) => {
    const row = document.createElement("tr");
    [item.phase, item.status, item.sha256, item.generated_at].forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 1) { const chip = document.createElement("span"); chip.className = `state-chip state-${String(value).toLowerCase()}`; chip.textContent = value; cell.append(chip); }
      else if (index === 2) { const code = document.createElement("code"); code.textContent = value; cell.append(code); }
      else cell.textContent = value;
      row.append(cell);
    });
    return row;
  }));
}

function setupPageAutoRefresh() {
  const refreshRoot = document.querySelector("[data-auto-refresh-seconds]");
  if (!refreshRoot) {
    return;
  }
  const seconds = Number.parseInt(refreshRoot.dataset.autoRefreshSeconds || "0", 10);
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return;
  }
  window.setTimeout(() => {
    if (document.visibilityState === "hidden") {
      return;
    }
    window.location.reload();
  }, seconds * 1000);
}

async function parseActionResponse(response, url) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    const text = await response.text();
    console.error("Action returned non-JSON response", {
      url: url.toString(),
      status: response.status,
      statusText: response.statusText,
      contentType,
      bodyPreview: text.slice(0, 1000),
    });
    throw new Error(
      `Server returned ${response.status || "an error"} instead of JSON. Check server logs.`
    );
  }
  try {
    return await response.json();
  } catch (error) {
    console.error("Action JSON parse failed", {
      url: url.toString(),
      status: response.status,
      contentType,
      error,
    });
    throw new Error("Server returned malformed JSON. Check server logs.");
  }
}

function userFacingActionError(error) {
  const message = error.message || String(error);
  if (message === "Failed to fetch") {
    return "Could not reach the learning action endpoint. Refresh the page and check that the UI server is still running.";
  }
  return message;
}

function actionFailureMessage(payload, response) {
  const base =
    payload.error ||
    payload.message ||
    `Server returned ${response.status || "an error"} for this action.`;
  if (payload.next_action) {
    return `${base} Next action: ${payload.next_action}`;
  }
  return base;
}

function ensureActionMessage(form) {
  let panel = form.querySelector("[data-action-message]");
  if (!panel) {
    panel = document.createElement("div");
    panel.dataset.actionMessage = "";
    panel.setAttribute("role", "alert");
    panel.className = "action-message";
    form.appendChild(panel);
  }
  return panel;
}

function setActionMessage(panel, message, kind) {
  if (!panel) {
    return;
  }
  panel.textContent = message;
  panel.classList.toggle("visible", Boolean(message));
  panel.classList.toggle("error", kind === "error");
  panel.classList.toggle("success", kind === "success");
}

document.addEventListener("click", (event) => {
  const commandOpen = event.target.closest("[data-command-open]");
  if (commandOpen) {
    const dialog = document.querySelector("[data-command-dialog]");
    if (dialog && typeof dialog.showModal === "function") {
      dialog.showModal();
      const input = dialog.querySelector("[data-command-filter]");
      if (input) {
        input.focus();
      }
    }
    return;
  }

  const copyButton = event.target.closest("[data-copy-text]");
  if (copyButton) {
    copyToClipboard(copyButton.dataset.copyText || "", copyButton);
    return;
  }

  const tab = event.target.closest("[data-tab-target]");
  if (!tab) {
    return;
  }
  const shell = tab.closest("[data-tabs]");
  if (!shell) {
    return;
  }
  const target = tab.dataset.tabTarget;
  shell.querySelectorAll("[data-tab-target]").forEach((item) => {
    item.classList.toggle("active", item === tab);
  });
  shell.querySelectorAll("[data-tab-panel]").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.tabPanel === target);
  });
});

document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    const dialog = document.querySelector("[data-command-dialog]");
    if (dialog && typeof dialog.showModal === "function") {
      dialog.showModal();
      const input = dialog.querySelector("[data-command-filter]");
      if (input) {
        input.focus();
      }
    }
  }
});

document.addEventListener("input", (event) => {
  const payoutInput = event.target.closest("[data-payout-input]");
  if (payoutInput) {
    updatePayoutCalculator(payoutInput.closest("[data-payout-calculator]"));
    return;
  }

  const input = event.target.closest("[data-command-filter]");
  if (!input) {
    return;
  }
  const query = input.value.trim().toLowerCase();
  const dialog = input.closest("[data-command-dialog]");
  if (!dialog) {
    return;
  }
  dialog.querySelectorAll("[data-command-item]").forEach((item) => {
    item.hidden = query && !item.textContent.toLowerCase().includes(query);
  });
});

async function copyToClipboard(value, button) {
  if (!value) {
    return;
  }
  const originalText = button ? button.textContent : "";
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = value;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
    }
    if (button) {
      button.textContent = "Copied";
      window.setTimeout(() => {
        button.textContent = originalText;
      }, 1400);
    }
  } catch (error) {
    console.error("Copy failed", error);
    if (button) {
      button.textContent = "Copy failed";
      window.setTimeout(() => {
        button.textContent = originalText;
      }, 1800);
    }
  }
}

function updatePayoutCalculator(calculator) {
  if (!calculator) {
    return;
  }
  const input = calculator.querySelector("[data-payout-input]");
  const amount = Number.parseFloat(input ? input.value : "0");
  const price = parsePayoutPrice(calculator.dataset.price);
  if (!Number.isFinite(amount) || amount <= 0 || price === null) {
    setPayoutText(calculator, {
      contracts: "--",
      cost: "--",
      unused: "--",
      gross: "--",
      profit: "--",
      loss: "--",
      roi: "--",
      breakeven: "--",
    });
    return;
  }
  const contracts = Math.floor(amount / price);
  const cost = contracts * price;
  const gross = contracts;
  const profit = gross - cost;
  const unused = amount - cost;
  const roi = cost > 0 ? (profit / cost) * 100 : 0;
  setPayoutText(calculator, {
    contracts: contracts.toLocaleString(),
    cost: formatCurrency(cost),
    unused: formatCurrency(unused),
    gross: formatCurrency(gross),
    profit: formatCurrency(profit),
    loss: formatCurrency(cost),
    roi: `${formatNumber(roi)}%`,
    breakeven: `${formatNumber(price * 100)}%`,
  });
}

function parsePayoutPrice(rawPrice) {
  const price = Number.parseFloat(String(rawPrice || "").replace(/[^0-9.-]/g, ""));
  if (!Number.isFinite(price) || price <= 0) {
    return null;
  }
  if (price > 1 && price <= 100) {
    return price / 100;
  }
  if (price >= 1) {
    return null;
  }
  return price;
}

function setPayoutText(calculator, values) {
  const selectors = {
    contracts: "[data-payout-contracts]",
    cost: "[data-payout-cost]",
    unused: "[data-payout-unused]",
    gross: "[data-payout-gross]",
    profit: "[data-payout-profit]",
    loss: "[data-payout-loss]",
    roi: "[data-payout-roi]",
    breakeven: "[data-payout-breakeven]",
  };
  Object.entries(selectors).forEach(([key, selector]) => {
    const node = calculator.querySelector(selector);
    if (node) {
      node.textContent = values[key];
    }
  });
}

function formatCurrency(value) {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

function formatNumber(value) {
  return new Intl.NumberFormat(undefined, {
    maximumFractionDigits: 1,
  }).format(value);
}
