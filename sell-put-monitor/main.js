const SIDES = ["put", "call"];

const modeInput = document.querySelector("#modeInput");
const intervalInput = document.querySelector("#intervalInput");
const refreshButton = document.querySelector("#refreshButton");
const resetSortButton = document.querySelector("#resetSortButton");
const statusText = document.querySelector("#statusText");
const errorPanel = document.querySelector("#errorPanel");
const progressLabel = document.querySelector("#progressLabel");
const progressPercent = document.querySelector("#progressPercent");
const progressBar = document.querySelector("#progressBar");

const sideState = {
  put: {
    body: document.querySelector("#putQuotesBody"),
    status: document.querySelector("#putStatus"),
    lastRows: [],
    lastGroups: [],
  },
  call: {
    body: document.querySelector("#callQuotesBody"),
    status: document.querySelector("#callStatus"),
    lastRows: [],
    lastGroups: [],
  },
};

let timer = null;
let progressTimer = null;
let inFlight = false;
let sortState = { key: null, direction: "desc" };
let syncingScroll = false;

function numberText(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function percentText(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function amountText(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const amount = Number(value);
  if (Math.abs(amount) >= 100000000) return `${numberText(amount / 100000000, 2)}亿`;
  if (Math.abs(amount) >= 10000) return `${numberText(amount / 10000, 2)}万`;
  return numberText(amount, 2);
}

function plainText(value) {
  return value === null || value === undefined || value === "" ? "-" : String(value);
}

function formatBeijingTime(value, assumeUtc = false) {
  if (value === null || value === undefined || value === "") return "-";
  const text = String(value).trim();
  if (!text) return "-";
  const normalized = text.includes("T") ? text : text.replace(" ", "T");
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(normalized);
  const date = new Date(hasTimezone ? normalized : assumeUtc ? `${normalized}Z` : `${normalized}+08:00`);
  if (Number.isNaN(date.getTime())) return `${text} 北京时间`;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date).replace(/\//g, "-") + " 北京时间";
}

function td(value, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = value;
  return cell;
}

function rowValue(row, key) {
  const value = row[key];
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function sortedGroups(groups) {
  if (!sortState.key) return groups;
  const rows = groups.flatMap((group) => group.rows.map((row) => ({ ...row, board: group.board })));
  const direction = sortState.direction === "asc" ? 1 : -1;
  rows.sort((left, right) => {
    const a = rowValue(left, sortState.key);
    const b = rowValue(right, sortState.key);
    if (a === null && b === null) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    return (a - b) * direction;
  });
  return [{ board: `排序：${sortLabel(sortState.key)} ${sortState.direction === "asc" ? "升序" : "降序"}`, rows }];
}

function sortLabel(key) {
  const button = document.querySelector(`.sortButton[data-sort="${key}"]`);
  return button ? button.textContent.replace(/[↑↓]/g, "").trim() : key;
}

function updateSortButtons() {
  document.querySelectorAll(".sortButton").forEach((button) => {
    const key = button.dataset.sort;
    const label = button.textContent.replace(/[↑↓]/g, "").trim();
    button.textContent = key === sortState.key ? `${label} ${sortState.direction === "asc" ? "↑" : "↓"}` : label;
  });
}

function syncTopScrollWidths() {
  document.querySelectorAll(".monitorPanel").forEach((panel) => {
    const table = panel.querySelector("table");
    const inner = panel.querySelector(".topScrollInner");
    if (table && inner) inner.style.width = `${table.scrollWidth}px`;
  });
}

function setupScrollSync() {
  document.querySelectorAll(".monitorPanel").forEach((panel) => {
    const tableWrap = panel.querySelector(".tableWrap");
    const topScroll = panel.querySelector(".topScroll");
    if (!tableWrap || !topScroll) return;
    topScroll.addEventListener("scroll", () => {
      if (syncingScroll) return;
      syncingScroll = true;
      tableWrap.scrollLeft = topScroll.scrollLeft;
      syncingScroll = false;
    });
    tableWrap.addEventListener("scroll", () => {
      if (syncingScroll) return;
      syncingScroll = true;
      topScroll.scrollLeft = tableWrap.scrollLeft;
      syncingScroll = false;
    });
  });
  window.addEventListener("resize", syncTopScrollWidths);
}

function selectionText(row) {
  if (row.selection_mode === "all_index_contracts") return row.selected_source || "股指全部合约";
  if (row.selection_mode !== "option_volume") return row.selected_source || "期货主力";
  const option = row.selected_option_contract ? ` ${row.selected_option_contract}` : "";
  const volume = row.selected_option_volume !== null && row.selected_option_volume !== undefined
    ? ` / ${numberText(row.selected_option_volume, 0)}手`
    : "";
  return `${row.selected_source || "期权成交量最大"}${option}${volume}`;
}

function renderGroups(side, groups) {
  const state = sideState[side];
  state.lastGroups = groups;
  groups = sortedGroups(groups);
  state.body.replaceChildren();
  const rowCount = groups.reduce((sum, group) => sum + group.rows.length, 0);
  if (!rowCount) {
    const tr = document.createElement("tr");
    tr.appendChild(td("没有可显示的数据", "empty"));
    tr.firstChild.colSpan = 23;
    state.body.appendChild(tr);
    updateSortButtons();
    return;
  }

  groups.forEach((group) => {
    const header = document.createElement("tr");
    header.className = "groupRow";
    const headerCell = td(`${group.board} · ${group.rows.length}`, "groupCell");
    headerCell.colSpan = 23;
    header.appendChild(headerCell);
    state.body.appendChild(header);

    group.rows.forEach((row) => {
      const key = `${row.option_side}:${row.underlying_symbol}:${row.future_contract}`;
      const previous = state.lastRows.find((item) => `${item.option_side}:${item.underlying_symbol}:${item.future_contract}` === key);
      const tr = document.createElement("tr");
      const changed = previous && previous.option_bid1 !== row.option_bid1;
      if (changed) tr.classList.add(row.option_bid1 > previous.option_bid1 ? "up" : "down");

      tr.appendChild(td(`${plainText(row.underlying_name)} ${plainText(row.underlying_symbol)}`, "name"));
      tr.appendChild(td(row.future_contract));
      tr.appendChild(td(selectionText(row), "basis"));
      tr.appendChild(td(numberText(row.future_last)));
      tr.appendChild(td(amountText(row.future_turnover)));
      tr.appendChild(td(row.option_contract));
      tr.appendChild(td(numberText(row.strike)));
      tr.appendChild(td(percentText(row.strike_gap_ratio)));
      tr.appendChild(td(numberText(row.option_bid1), "yield"));
      tr.appendChild(td(numberText(row.option_bid1_volume, 0)));
      tr.appendChild(td(numberText(row.option_last)));
      tr.appendChild(td(plainText(row.maturity_date)));
      tr.appendChild(td(plainText(row.days_to_expiry)));
      tr.appendChild(td(numberText(row.contract_multiplier, 0)));
      tr.appendChild(td(numberText(row.option_contracts_per_future, 2)));
      tr.appendChild(td(percentText(row.margin_rate)));
      tr.appendChild(td(amountText(row.margin_amount)));
      tr.appendChild(td(row.future_leverage ? `${numberText(row.future_leverage)}x` : "-"));
      tr.appendChild(td(percentText(row.cash_yield), "yield"));
      tr.appendChild(td(percentText(row.annualized_cash_yield), "yield"));
      tr.appendChild(td(percentText(row.margin_yield), "levered"));
      tr.appendChild(td(percentText(row.annualized_margin_yield), "levered"));
      tr.appendChild(td(formatBeijingTime(row.updated_at, true)));
      state.body.appendChild(tr);
    });
  });
  state.lastRows = groups.flatMap((group) => group.rows);
  updateSortButtons();
  syncTopScrollWidths();
}

function renderErrors(errors) {
  if (!errors.length) {
    errorPanel.hidden = true;
    errorPanel.textContent = "";
    return;
  }
  errorPanel.hidden = false;
  errorPanel.replaceChildren();
  errors.slice(0, 30).forEach((error) => {
    const item = document.createElement("div");
    const sideLabel = error.option_side === "call" ? "Call" : error.option_side === "put" ? "Put" : "";
    const label = [sideLabel, error.board, error.underlying_name || error.underlying_symbol].filter(Boolean).join(" / ");
    item.textContent = `${label || "错误"}: ${error.message}`;
    errorPanel.appendChild(item);
  });
}

async function fetchJson(path) {
  const separator = path.includes("?") ? "&" : "?";
  const response = await fetch(`${path}${separator}t=${Date.now()}`, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || "请求失败");
  return payload;
}

async function loadConfig() {
  const config = await fetchJson("config.json");
  const savedMode = localStorage.getItem("optionMonitor.mode");
  modeInput.value = savedMode || config.default_selection_mode || "dominant";
  intervalInput.value = "300";
  localStorage.setItem("optionMonitor.interval", "300");
  statusText.textContent = "读取静态快照中";
}

function updateProgress(progress) {
  const percent = Math.max(0, Math.min(100, Number(progress?.percent) || 0));
  progressBar.style.width = `${percent}%`;
  progressPercent.textContent = `${percent}%`;
  progressLabel.textContent = progress?.message || "正在加载行情";
}

function stopProgressPolling() {
  if (progressTimer) {
    window.clearInterval(progressTimer);
    progressTimer = null;
  }
}

function startProgressPolling() {
  stopProgressPolling();
  updateProgress({ percent: 45, message: "读取静态快照" });
}

async function fetchAllSides() {
  const mode = modeInput.value === "option_volume" ? "option_volume" : "dominant";
  return fetchJson(`board-quotes-${mode}.json`);
}

async function refresh() {
  if (inFlight) return;
  inFlight = true;
  localStorage.setItem("optionMonitor.mode", modeInput.value);
  localStorage.setItem("optionMonitor.interval", intervalInput.value);
  statusText.textContent = "正在更新板块实时价格";
  SIDES.forEach((side) => {
    sideState[side].status.textContent = "正在更新";
  });
  refreshButton.disabled = true;
  startProgressPolling();
  try {
    const errors = [];
    const payload = await fetchAllSides();
    SIDES.forEach((side) => {
      const section = payload.sections?.[side] || { groups: [], errors: [] };
      renderGroups(side, section.groups || []);
      errors.push(...(section.errors || []).map((error) => ({ ...error, option_side: side })));
      sideState[side].status.textContent = `最近更新 ${formatBeijingTime(payload.updated_at, true)}`;
    });
    if (!errors.length) renderErrors([]);
    else renderErrors(errors);
    statusText.textContent = `最近更新 ${formatBeijingTime(payload.updated_at, true)}`;
    updateProgress({ percent: 100, message: "行情加载完成" });
  } catch (error) {
    renderErrors([{ message: error.message }]);
    SIDES.forEach((side) => {
      renderGroups(side, []);
      sideState[side].status.textContent = "更新失败";
    });
    updateProgress({ percent: 0, message: `行情加载失败：${error.message}` });
    statusText.textContent = "更新失败";
  } finally {
    stopProgressPolling();
    inFlight = false;
    refreshButton.disabled = false;
  }
}

function schedule() {
  if (timer) window.clearInterval(timer);
  const seconds = Math.max(300, Number(intervalInput.value) || 300);
  timer = window.setInterval(refresh, seconds * 1000);
}

function renderAllCachedGroups() {
  SIDES.forEach((side) => renderGroups(side, sideState[side].lastGroups));
}

refreshButton.addEventListener("click", refresh);
intervalInput.addEventListener("change", () => {
  schedule();
  refresh();
});
modeInput.addEventListener("change", refresh);
resetSortButton.addEventListener("click", () => {
  sortState = { key: null, direction: "desc" };
  renderAllCachedGroups();
});
document.querySelectorAll(".sortButton").forEach((button) => {
  button.addEventListener("click", () => {
    const key = button.dataset.sort;
    if (sortState.key === key) {
      sortState.direction = sortState.direction === "desc" ? "asc" : "desc";
    } else {
      sortState = { key, direction: "desc" };
    }
    renderAllCachedGroups();
  });
});

loadConfig()
  .then(() => {
    setupScrollSync();
    syncTopScrollWidths();
    schedule();
    refresh();
  })
  .catch((error) => {
    renderErrors([{ message: error.message }]);
  });
