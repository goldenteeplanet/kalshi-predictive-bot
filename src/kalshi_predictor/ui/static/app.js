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
});

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
