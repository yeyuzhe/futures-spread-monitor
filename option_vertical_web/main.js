const symbolsInput = document.querySelector("#symbolsInput");
const intervalInput = document.querySelector("#intervalInput");
const minProfitInput = document.querySelector("#minProfitInput");
const minVolumeInput = document.querySelector("#minVolumeInput");
const refreshButton = document.querySelector("#refreshButton");
const statusText = document.querySelector("#statusText");
const errorPanel = document.querySelector("#errorPanel");
const progressPanel = document.querySelector("#progressPanel");
const progressText = document.querySelector("#progressText");
const progressPercent = document.querySelector("#progressPercent");
const progressBar = document.querySelector("#progressBar");
const futureContract = document.querySelector("#futureContract");
const putCount = document.querySelector("#putCount");
const callCount = document.querySelector("#callCount");
const putCandidateCount = document.querySelector("#putCandidateCount");
const callCandidateCount = document.querySelector("#callCandidateCount");
const verifiedCount = document.querySelector("#verifiedCount");
const updatedAt = document.querySelector("#updatedAt");
const putOpportunitiesBody = document.querySelector("#putOpportunitiesBody");
const callOpportunitiesBody = document.querySelector("#callOpportunitiesBody");
const quotesPanel = document.querySelector("#quotesPanel");
const quotesTitle = document.querySelector("#quotesTitle");
const quotesBody = document.querySelector("#quotesBody");
const positionCount = document.querySelector("#positionCount");
const positionMarginTotal = document.querySelector("#positionMarginTotal");
const positionProfitTotal = document.querySelector("#positionProfitTotal");
const clearPositionsButton = document.querySelector("#clearPositionsButton");
const positionsBody = document.querySelector("#positionsBody");
const tradeCount = document.querySelector("#tradeCount");
const tradeProfitTotal = document.querySelector("#tradeProfitTotal");
const clearTradesButton = document.querySelector("#clearTradesButton");
const tradesBody = document.querySelector("#tradesBody");

let timer = null;
let inFlight = false;
let lastPayload = null;
let progressTimer = null;
let positions = [];
let trades = [];

const POSITIONS_STORAGE_KEY = "verticalArb.positions";
const TRADES_STORAGE_KEY = "verticalArb.trades";

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
  return Number(value).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function plainText(value) {
  return value === null || value === undefined || value === "" ? "-" : String(value);
}

function td(value, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = value;
  return cell;
}

function positionKey(row) {
  return [
    row.underlying_symbol,
    row.option_type,
    row.future_contract,
    row.buy_contract,
    row.sell_contract,
    row.maturity_date,
  ].map((value) => plainText(value)).join("|");
}

function safeNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function signedAmountText(value) {
  const number = safeNumber(value);
  if (number === null) return "-";
  return `${number > 0 ? "+" : ""}${amountText(number)}`;
}

function positionEntrySpread(row) {
  const stored = safeNumber(row.entry_spread);
  if (stored !== null) return stored;
  const buyPrice = safeNumber(row.buy_price);
  const sellPrice = safeNumber(row.sell_price);
  if (buyPrice === null || sellPrice === null) return null;
  return sellPrice - buyPrice;
}

function positionFromOpportunity(row) {
  const openMarginAmount = row.tick_open_margin_amount ?? row.open_margin_amount;
  const marginReturn = row.tick_annualized_margin_expiry_return
    ?? row.annualized_margin_expiry_return
    ?? row.tick_annualized_leveraged_expiry_return
    ?? row.annualized_leveraged_expiry_return;
  const buyPrice = row.tick_buy_ask1 ?? row.buy_price;
  const sellPrice = row.tick_sell_bid1 ?? row.sell_price;
  const buyNumber = safeNumber(buyPrice);
  const sellNumber = safeNumber(sellPrice);
  const entrySpread = buyNumber !== null && sellNumber !== null ? sellNumber - buyNumber : null;

  return {
    key: positionKey(row),
    filled_at: new Date().toLocaleString("zh-CN", { hour12: false }),
    underlying_symbol: row.underlying_symbol,
    option_type: row.option_type,
    future_contract: row.future_contract,
    buy_contract: row.buy_contract,
    buy_price: safeNumber(buyPrice),
    sell_contract: row.sell_contract,
    sell_price: safeNumber(sellPrice),
    entry_spread: entrySpread,
    maturity_date: row.maturity_date,
    days_to_expiry: row.days_to_expiry,
    open_margin_amount: safeNumber(openMarginAmount),
    annualized_margin_return: safeNumber(marginReturn),
    multiplier: safeNumber(row.multiplier) || 1,
    current_spread: safeNumber(row.current_spread),
    position_profit: safeNumber(row.position_profit),
    source_quote_time: [row.tick_buy_time, row.tick_sell_time].filter(Boolean).join(" / "),
  };
}

function loadPositions() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(POSITIONS_STORAGE_KEY) || "[]");
    positions = Array.isArray(parsed) ? parsed.filter((row) => row && row.key) : [];
  } catch {
    positions = [];
  }
}

function savePositions() {
  window.localStorage.setItem(POSITIONS_STORAGE_KEY, JSON.stringify(positions));
}

function loadTrades() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(TRADES_STORAGE_KEY) || "[]");
    trades = Array.isArray(parsed) ? parsed.filter((row) => row && row.key) : [];
  } catch {
    trades = [];
  }
}

function saveTrades() {
  window.localStorage.setItem(TRADES_STORAGE_KEY, JSON.stringify(trades));
}

function renderTrades() {
  if (!tradesBody) return;
  tradesBody.replaceChildren();

  const totalProfit = trades.reduce((sum, row) => sum + (safeNumber(row.realized_profit) || 0), 0);
  tradeCount.textContent = String(trades.length);
  tradeProfitTotal.textContent = trades.length ? signedAmountText(totalProfit) : "-";
  tradeProfitTotal.className = totalProfit >= 0 ? "gain" : "loss";
  clearTradesButton.disabled = trades.length === 0;

  if (!trades.length) {
    const tr = document.createElement("tr");
    tr.appendChild(td("尚未产生交易记录", "empty"));
    tr.firstChild.colSpan = 10;
    tradesBody.appendChild(tr);
    return;
  }

  trades.forEach((row) => {
    const profit = safeNumber(row.realized_profit);
    const tr = document.createElement("tr");
    tr.appendChild(td(plainText(row.entry_time)));
    tr.appendChild(td(plainText(row.exit_time)));
    tr.appendChild(td(plainText(row.underlying_symbol)));
    tr.appendChild(td(row.option_type === "C" ? "Call" : "Put"));
    tr.appendChild(td(plainText(row.buy_contract)));
    tr.appendChild(td(plainText(row.sell_contract)));
    tr.appendChild(td(numberText(row.entry_spread)));
    tr.appendChild(td(numberText(row.exit_spread)));
    tr.appendChild(td(signedAmountText(profit), profit === null || profit >= 0 ? "gain" : "loss"));
    tr.appendChild(td(plainText(row.exit_reason)));
    tradesBody.appendChild(tr);
  });
}

function clearTrades() {
  trades = [];
  saveTrades();
  renderTrades();
}

function renderPositions() {
  if (!positionsBody) return;
  positionsBody.replaceChildren();

  const totalMargin = positions.reduce((sum, row) => sum + (safeNumber(row.open_margin_amount) || 0), 0);
  const totalProfit = positions.reduce((sum, row) => sum + (safeNumber(row.position_profit) || 0), 0);
  positionCount.textContent = String(positions.length);
  positionMarginTotal.textContent = positions.length ? amountText(totalMargin) : "-";
  positionProfitTotal.textContent = positions.length ? signedAmountText(totalProfit) : "-";
  positionProfitTotal.className = totalProfit >= 0 ? "gain" : "loss";
  clearPositionsButton.disabled = positions.length === 0;

  if (!positions.length) {
    const tr = document.createElement("tr");
    tr.appendChild(td("尚未记录持仓", "empty"));
    tr.firstChild.colSpan = 15;
    positionsBody.appendChild(tr);
    return;
  }

  positions.forEach((row) => {
    const tr = document.createElement("tr");
    tr.appendChild(td(plainText(row.filled_at || row.recorded_at)));
    tr.appendChild(td(plainText(row.underlying_symbol)));
    tr.appendChild(td(row.option_type === "C" ? "Call" : "Put"));
    tr.appendChild(td(plainText(row.future_contract)));
    tr.appendChild(td(plainText(row.buy_contract)));
    tr.appendChild(td(numberText(row.buy_price)));
    tr.appendChild(td(plainText(row.sell_contract)));
    tr.appendChild(td(numberText(row.sell_price)));
    tr.appendChild(td(numberText(positionEntrySpread(row)), "gain"));
    const profit = safeNumber(row.position_profit);
    tr.appendChild(td(signedAmountText(profit), profit === null || profit >= 0 ? "gain" : "loss"));
    tr.appendChild(td(plainText(row.maturity_date)));
    tr.appendChild(td(row.days_to_expiry === null || row.days_to_expiry === undefined ? "-" : `${row.days_to_expiry}d`));
    tr.appendChild(td(amountText(row.open_margin_amount)));
    tr.appendChild(td(percentText(row.annualized_margin_return), "gain"));

    const actionCell = document.createElement("td");
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "actionButton dangerButton";
    removeButton.textContent = "移除";
    removeButton.addEventListener("click", () => removePosition(row.key));
    actionCell.appendChild(removeButton);
    tr.appendChild(actionCell);
    positionsBody.appendChild(tr);
  });
}

function addPosition(row, { replace = false } = {}) {
  const nextPosition = positionFromOpportunity(row);
  const existingIndex = positions.findIndex((position) => position.key === nextPosition.key);
  if (existingIndex >= 0) {
    if (!replace) return false;
    positions[existingIndex] = {
      ...nextPosition,
      filled_at: positions[existingIndex].filled_at || positions[existingIndex].recorded_at || nextPosition.filled_at,
    };
  } else {
    positions.unshift(nextPosition);
  }
  savePositions();
  renderPositions();
  renderOpportunityActionStates();
  return true;
}

function syncPositionsFromOpportunities(rows) {
  const activeKeys = new Set(positions.map((position) => position.key));
  const newPositions = [];
  rows.forEach((row) => {
    const key = positionKey(row);
    if (activeKeys.has(key)) return;
    activeKeys.add(key);
    newPositions.push(positionFromOpportunity(row));
  });
  if (!newPositions.length) return 0;
  positions = [...newPositions, ...positions];
  savePositions();
  renderPositions();
  renderOpportunityActionStates();
  return newPositions.length;
}

function closePositionTrade(position, reason) {
  const entrySpread = positionEntrySpread(position);
  const exitSpread = safeNumber(position.current_spread);
  const multiplier = safeNumber(position.multiplier) || 1;
  const realizedProfit = entrySpread !== null && exitSpread !== null
    ? (entrySpread - exitSpread) * multiplier
    : safeNumber(position.position_profit);

  return {
    key: `${position.key}|${new Date().toISOString()}`,
    position_key: position.key,
    entry_time: position.filled_at || position.recorded_at,
    exit_time: new Date().toLocaleString("zh-CN", { hour12: false }),
    underlying_symbol: position.underlying_symbol,
    option_type: position.option_type,
    future_contract: position.future_contract,
    buy_contract: position.buy_contract,
    sell_contract: position.sell_contract,
    entry_spread: entrySpread,
    exit_spread: exitSpread,
    multiplier,
    realized_profit: realizedProfit,
    exit_reason: reason,
  };
}

function canClosePosition(position) {
  const profit = safeNumber(position.position_profit);
  const exitSpread = safeNumber(position.current_spread);
  return profit !== null && exitSpread !== null && profit >= 0;
}

function prunePositionsByOpportunities(rows, scannedSymbols) {
  const activeKeys = new Set(rows.map((row) => positionKey(row)));
  const scannedSet = new Set(scannedSymbols || []);
  const closedTrades = [];
  const nextPositions = [];

  positions.forEach((position) => {
    const inScannedScope = scannedSet.has(position.underlying_symbol);
    if (!inScannedScope || activeKeys.has(position.key)) {
      nextPositions.push(position);
      return;
    }
    if (!canClosePosition(position)) {
      nextPositions.push(position);
      return;
    }
    closedTrades.push(closePositionTrade(position, "不再满足套利条件"));
  });

  if (!closedTrades.length) return 0;
  positions = nextPositions;
  trades = [...closedTrades, ...trades];
  savePositions();
  saveTrades();
  renderPositions();
  renderTrades();
  renderOpportunityActionStates();
  return closedTrades.length;
}

function quoteMapFromPayload(payload) {
  const map = new Map();
  [
    ...(payload?.puts || []),
    ...(payload?.calls || []),
  ].forEach((row) => {
    if (row?.contract) map.set(row.contract, row);
  });
  return map;
}

function opportunityLegQuote(opportunity, contract) {
  if (!opportunity || !contract) return null;
  if (contract === opportunity.low_contract) {
    return {
      bid1: opportunity.low_bid1,
      ask1: opportunity.low_ask1,
      quote_time: opportunity.quote_time,
    };
  }
  if (contract === opportunity.high_contract) {
    return {
      bid1: opportunity.high_bid1,
      ask1: opportunity.high_ask1,
      quote_time: opportunity.quote_time,
    };
  }
  return null;
}

function updatePositionMarks(payload) {
  const opportunityMap = new Map([
    ...(payload?.put_opportunities || []),
    ...(payload?.call_opportunities || []),
  ].map((row) => [positionKey(row), row]));
  const quotes = quoteMapFromPayload(payload);
  let changed = false;

  positions = positions.map((position) => {
    const entrySpread = positionEntrySpread(position);
    const opportunity = opportunityMap.get(position.key);
    let currentSpread = null;
    let quoteTime = "";

    const buyLeg = quotes.get(position.buy_contract) || opportunityLegQuote(opportunity, position.buy_contract);
    const sellLeg = quotes.get(position.sell_contract) || opportunityLegQuote(opportunity, position.sell_contract);
    const buyClosePrice = safeNumber(buyLeg?.bid1);
    const sellClosePrice = safeNumber(sellLeg?.ask1);
    currentSpread = buyClosePrice !== null && sellClosePrice !== null ? sellClosePrice - buyClosePrice : null;
    quoteTime = [buyLeg?.quote_time, sellLeg?.quote_time].filter(Boolean).join(" / ");

    const multiplier = safeNumber(position.multiplier) || 1;
    const positionProfit = entrySpread !== null && currentSpread !== null
      ? (entrySpread - currentSpread) * multiplier
      : null;
    const next = {
      ...position,
      entry_spread: entrySpread,
      current_spread: currentSpread,
      position_profit: positionProfit,
      mark_quote_time: quoteTime,
    };
    changed = changed
      || next.entry_spread !== position.entry_spread
      || next.current_spread !== position.current_spread
      || next.position_profit !== position.position_profit
      || next.mark_quote_time !== position.mark_quote_time;
    return next;
  });

  if (changed) {
    savePositions();
    renderPositions();
  }
}

function scannedSymbolsFromPayload(payload, requestedSymbols) {
  if (requestedSymbols !== "ALL") return requestedSymbols.split(",").filter(Boolean);
  return Array.from(new Set([
    ...(payload?.puts || []),
    ...(payload?.calls || []),
    ...(payload?.put_opportunities || []),
    ...(payload?.call_opportunities || []),
  ].map((row) => row?.underlying_symbol).filter(Boolean)));
}

function removePosition(key) {
  positions = positions.filter((position) => position.key !== key);
  savePositions();
  renderPositions();
  renderOpportunityActionStates();
}

function clearPositions() {
  positions = [];
  savePositions();
  renderPositions();
  renderOpportunityActionStates();
}

function renderOpportunityActionStates() {
  const activeKeys = new Set(positions.map((position) => position.key));
  document.querySelectorAll("[data-position-key]").forEach((button) => {
    const isRecorded = activeKeys.has(button.dataset.positionKey);
    button.textContent = isRecorded ? "已持有" : "补入持仓";
    button.classList.toggle("recorded", isRecorded);
  });
}

function selectedSymbols() {
  const allInput = symbolsInput.querySelector('[data-all-symbols="true"]');
  if (allInput?.checked && !allInput.indeterminate) return "ALL";
  const selected = Array.from(symbolsInput.querySelectorAll('input[name="symbol"]:checked')).map((input) => input.value);
  if (!selected.length || selected.includes("__ALL__")) return "ALL";
  return selected.join(",");
}

function selectedSymbolCount(symbols) {
  if (symbols !== "ALL") return symbols.split(",").filter(Boolean).length || 1;
  return symbolsInput.querySelectorAll('input[name="symbol"]').length || 1;
}

function setProgress(percent, text) {
  progressPanel.hidden = false;
  const value = Math.max(0, Math.min(100, Math.round(percent)));
  progressBar.style.width = `${value}%`;
  progressPercent.textContent = `${value}%`;
  progressText.textContent = text;
}

function startProgress(symbols) {
  if (progressTimer) window.clearInterval(progressTimer);
  const startedAt = Date.now();
  const count = selectedSymbolCount(symbols);
  const estimatedMs = Math.max(12000, Math.min(180000, count * 3500));
  setProgress(3, symbols === "ALL" ? `正在扫描全部品种，预计 ${Math.ceil(estimatedMs / 1000)} 秒以上` : `正在扫描 ${symbols}`);
  progressTimer = window.setInterval(() => {
    const elapsed = Date.now() - startedAt;
    const percent = Math.min(95, 5 + (elapsed / estimatedMs) * 90);
    const elapsedSeconds = Math.floor(elapsed / 1000);
    setProgress(percent, `已等待 ${elapsedSeconds} 秒，行情和 tick 复验仍在进行`);
  }, 700);
}

function finishProgress(text) {
  if (progressTimer) window.clearInterval(progressTimer);
  progressTimer = null;
  setProgress(100, text);
  window.setTimeout(() => {
    if (!progressTimer) progressPanel.hidden = true;
  }, 1800);
}

function buildSymbolOptions(boards, defaultSymbol) {
  symbolsInput.replaceChildren();
  const allLabel = document.createElement("label");
  allLabel.className = "symbolOption allSymbolOption";
  const allInput = document.createElement("input");
  allInput.type = "checkbox";
  allInput.value = "__ALL__";
  allInput.checked = defaultSymbol === "ALL";
  allInput.dataset.allSymbols = "true";
  allLabel.appendChild(allInput);
  allLabel.appendChild(document.createTextNode("全部品种"));
  symbolsInput.appendChild(allLabel);

  Object.entries(boards || {}).forEach(([board, symbols]) => {
    const group = document.createElement("div");
    group.className = "symbolGroup";
    const title = document.createElement("div");
    title.className = "symbolGroupTitle";
    title.textContent = board;
    group.appendChild(title);
    (symbols || []).forEach((symbol) => {
      const label = document.createElement("label");
      label.className = "symbolOption";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "symbol";
      input.value = symbol;
      input.checked = symbol === defaultSymbol && defaultSymbol !== "ALL";
      label.appendChild(input);
      label.appendChild(document.createTextNode(symbol));
      group.appendChild(label);
    });
    symbolsInput.appendChild(group);
  });
  normalizeSymbolSelection();
}

function normalizeSymbolSelection() {
  const allInput = symbolsInput.querySelector('[data-all-symbols="true"]');
  const symbolInputs = Array.from(symbolsInput.querySelectorAll('input[name="symbol"]'));
  if (!allInput) return;
  const selectedSpecific = symbolInputs.filter((input) => input.checked);
  allInput.checked = selectedSpecific.length === 0 || selectedSpecific.length === symbolInputs.length;
  allInput.indeterminate = selectedSpecific.length > 0 && selectedSpecific.length < symbolInputs.length;
}

function handleSymbolSelectionChange(event) {
  const target = event.target;
  if (!(target instanceof HTMLInputElement)) return;
  const symbolInputs = Array.from(symbolsInput.querySelectorAll('input[name="symbol"]'));
  if (target.dataset.allSymbols === "true") {
    symbolInputs.forEach((input) => {
      input.checked = target.checked;
    });
    if (!target.checked && symbolInputs.length) {
      symbolInputs[0].checked = true;
    }
  }
  normalizeSymbolSelection();
  refresh();
}

function renderErrors(errors) {
  const messages = errors.map((error) => {
    if (typeof error === "string") return error;
    const prefix = error.underlying_symbol ? `${error.underlying_symbol}: ` : "";
    return `${prefix}${error.message || "扫描失败"}`;
  });
  if (!messages.length) {
    errorPanel.hidden = true;
    errorPanel.textContent = "";
    return;
  }
  errorPanel.hidden = false;
  errorPanel.replaceChildren();
  messages.slice(0, 30).forEach((message) => {
    const item = document.createElement("div");
    item.textContent = message;
    errorPanel.appendChild(item);
  });
}

function renderSummary(payload) {
  futureContract.textContent = plainText(payload.future_contract);
  putCount.textContent = plainText(payload.put_count);
  callCount.textContent = plainText(payload.call_count);
  putCandidateCount.textContent = plainText(payload.put_candidate_count);
  callCandidateCount.textContent = plainText(payload.call_candidate_count);
  verifiedCount.textContent = plainText(payload.verified_count);
  updatedAt.textContent = plainText(payload.updated_at);
}

function renderOpportunityRows(body, rows, emptyText) {
  body.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.appendChild(td(emptyText, "empty"));
    tr.firstChild.colSpan = 20;
    body.appendChild(tr);
    return;
  }

  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.className = row.verified ? "verified selectableRow" : "candidate selectableRow";
    tr.addEventListener("click", () => renderQuotesForOpportunity(row));
    const tickTime = [row.tick_buy_time, row.tick_sell_time].filter(Boolean).join(" / ");
    const expiryReturn = row.tick_expiry_return ?? row.expiry_return;
    const annualizedReturn = row.tick_annualized_expiry_return ?? row.annualized_expiry_return;
    const leveragedReturn = row.tick_annualized_margin_expiry_return
      ?? row.annualized_margin_expiry_return
      ?? row.tick_annualized_leveraged_expiry_return
      ?? row.annualized_leveraged_expiry_return;
    const openMarginAmount = row.tick_open_margin_amount ?? row.open_margin_amount;

    tr.appendChild(td(row.verified ? "tick通过" : row.verification_message, row.verified ? "good" : "warn"));
    tr.appendChild(td(row.underlying_symbol));
    tr.appendChild(td(row.future_contract));
    tr.appendChild(td(row.buy_contract));
    tr.appendChild(td(numberText(row.buy_strike)));
    tr.appendChild(td(numberText(row.buy_price)));
    tr.appendChild(td(row.sell_contract));
    tr.appendChild(td(numberText(row.sell_strike)));
    tr.appendChild(td(numberText(row.sell_price)));
    tr.appendChild(td(numberText(row.profit_per_unit), "gain"));
    tr.appendChild(td(numberText(row.profit_per_lot), "gain"));
    tr.appendChild(td(row.days_to_expiry === null || row.days_to_expiry === undefined ? "-" : `${row.days_to_expiry}d`));
    tr.appendChild(td(amountText(openMarginAmount)));
    tr.appendChild(td(percentText(expiryReturn), "gain"));
    tr.appendChild(td(percentText(annualizedReturn), "gain"));
    tr.appendChild(td(percentText(leveragedReturn), "gain"));
    tr.appendChild(td(numberText(row.tick_profit_per_unit), row.verified ? "gain" : ""));
    tr.appendChild(td(plainText(tickTime)));
    tr.appendChild(td(plainText(row.maturity_date)));

    const actionCell = document.createElement("td");
    const recordButton = document.createElement("button");
    recordButton.type = "button";
    recordButton.className = "actionButton";
    recordButton.dataset.positionKey = positionKey(row);
    recordButton.textContent = positions.some((position) => position.key === recordButton.dataset.positionKey) ? "已持有" : "补入持仓";
    recordButton.addEventListener("click", (event) => {
      event.stopPropagation();
      addPosition(row);
    });
    actionCell.appendChild(recordButton);
    tr.appendChild(actionCell);
    body.appendChild(tr);
  });
  renderOpportunityActionStates();
}

function clearQuotes() {
  quotesPanel.hidden = true;
  quotesTitle.textContent = "点击套利机会后显示对应品种和对应 Call/Put 盘口";
  quotesBody.replaceChildren();
  const tr = document.createElement("tr");
  tr.appendChild(td("点击套利机会后显示对应盘口", "empty"));
  tr.firstChild.colSpan = 11;
  quotesBody.appendChild(tr);
}

function renderQuotesForOpportunity(opportunity) {
  if (!lastPayload || !opportunity) return;
  const rows = [
    ...(lastPayload.puts || []),
    ...(lastPayload.calls || []),
  ].filter((row) => row.underlying_symbol === opportunity.underlying_symbol && row.option_type === opportunity.option_type);
  quotesTitle.textContent = `${opportunity.underlying_symbol} ${opportunity.option_type === "C" ? "Call" : "Put"} 盘口`;
  quotesPanel.hidden = false;
  renderQuotes(rows);
}

function renderQuotes(rows) {
  const sortedRows = [...rows].sort((left, right) => Number(left.strike) - Number(right.strike));
  quotesBody.replaceChildren();
  if (!sortedRows.length) {
    const tr = document.createElement("tr");
    tr.appendChild(td("没有找到对应期权盘口", "empty"));
    tr.firstChild.colSpan = 11;
    quotesBody.appendChild(tr);
    return;
  }

  sortedRows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.appendChild(td(plainText(row.underlying_symbol)));
    tr.appendChild(td(row.option_type === "C" ? "Call" : "Put"));
    tr.appendChild(td(row.contract));
    tr.appendChild(td(numberText(row.strike)));
    tr.appendChild(td(numberText(row.bid1)));
    tr.appendChild(td(numberText(row.bid_volume, 0)));
    tr.appendChild(td(numberText(row.ask1)));
    tr.appendChild(td(numberText(row.ask_volume, 0)));
    tr.appendChild(td(numberText(row.last)));
    tr.appendChild(td(plainText(row.maturity_date)));
    tr.appendChild(td(plainText(row.quote_time)));
    quotesBody.appendChild(tr);
  });
}

async function fetchJson(path) {
  const separator = path.includes("?") ? "&" : "?";
  const response = await fetch(`${path}${separator}t=${Date.now()}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`读取数据失败: ${response.status}`);
  return response.json();
}

function symbolsFilter(symbols) {
  if (!symbols || symbols === "ALL" || symbols === "__ALL__") return null;
  const values = String(symbols)
    .split(/[,，\s]+/)
    .map((item) => item.trim().toUpperCase())
    .filter(Boolean);
  return values.length ? new Set(values) : null;
}

function rowUnderlyingSymbol(row) {
  return plainText(row?.underlying_symbol || row?.symbol).toUpperCase();
}

function filterRowsBySymbols(rows, selected) {
  if (!selected) return rows || [];
  return (rows || []).filter((row) => selected.has(rowUnderlyingSymbol(row)));
}

function filterStaticPayload(payload, symbols) {
  const selected = symbolsFilter(symbols);
  if (!selected) return payload;

  const results = filterRowsBySymbols(payload.results || [], selected);
  const puts = filterRowsBySymbols(payload.puts || [], selected);
  const calls = filterRowsBySymbols(payload.calls || [], selected);
  const putOpportunities = filterRowsBySymbols(payload.put_opportunities || [], selected);
  const callOpportunities = filterRowsBySymbols(payload.call_opportunities || [], selected);
  const opportunities = [...putOpportunities, ...callOpportunities];

  return {
    ...payload,
    symbols: [...selected],
    results,
    puts,
    calls,
    put_opportunities: putOpportunities,
    call_opportunities: callOpportunities,
    opportunities,
    future_contract: results.map((item) => item.future_contract).filter(Boolean).slice(0, 3).join(" / ") + (results.length > 3 ? " ..." : ""),
    future_last: results.length === 1 ? results[0].future_last : null,
    put_count: puts.length,
    call_count: calls.length,
    candidate_count: opportunities.length,
    put_candidate_count: putOpportunities.length,
    call_candidate_count: callOpportunities.length,
    verified_count: opportunities.filter((item) => item.verified).length,
    put_verified_count: putOpportunities.filter((item) => item.verified).length,
    call_verified_count: callOpportunities.filter((item) => item.verified).length,
  };
}

async function loadConfig() {
  const config = await fetchJson("config.json");
  buildSymbolOptions(config.option_boards || {}, config.underlying_symbol || "AG");
  intervalInput.value = String(config.poll_seconds || 300);
  minProfitInput.value = String(config.min_profit || 0);
  minVolumeInput.value = String(config.min_volume || 1);
  statusText.textContent = "读取静态数据中";
}

async function refresh() {
  if (inFlight) return;
  inFlight = true;
  refreshButton.disabled = true;
  normalizeSymbolSelection();
  const symbols = selectedSymbols();
  statusText.textContent = symbols === "ALL" ? "正在扫描全部品种" : `正在扫描 ${symbols}`;
  startProgress(symbols);
  renderErrors([]);
  try {
    const rawPayload = await fetchJson("opportunities.json");
    const payload = filterStaticPayload(rawPayload, symbols);
    renderErrors(payload.errors || []);
    if (!payload.ok) throw new Error(payload.message || "扫描失败");
    lastPayload = payload;
    const allOpportunities = [
      ...(payload.put_opportunities || []),
      ...(payload.call_opportunities || []),
    ];
    updatePositionMarks(payload);
    const removedPositionCount = prunePositionsByOpportunities(allOpportunities, scannedSymbolsFromPayload(payload, symbols));
    const addedPositionCount = syncPositionsFromOpportunities(allOpportunities);
    updatePositionMarks(payload);
    renderSummary(payload);
    renderOpportunityRows(putOpportunitiesBody, payload.put_opportunities || [], "暂无 Put 垂直价差套利机会");
    renderOpportunityRows(callOpportunitiesBody, payload.call_opportunities || [], "暂无 Call 垂直价差套利机会");
    clearQuotes();
    const addedText = addedPositionCount ? `，新增持仓 ${addedPositionCount} 对` : "";
    const removedText = removedPositionCount ? `，平仓出场 ${removedPositionCount} 对` : "";
    statusText.textContent = symbols === "ALL" ? `全部品种扫描完成${addedText}${removedText}` : `${symbols} 扫描完成${addedText}${removedText}`;
    finishProgress("刷新完成");
  } catch (error) {
    renderErrors([error.message]);
    statusText.textContent = "扫描失败";
    finishProgress("刷新失败");
  } finally {
    inFlight = false;
    refreshButton.disabled = false;
  }
}

function schedule() {
  if (timer) window.clearInterval(timer);
  const seconds = Math.max(30, Number(intervalInput.value) || 300);
  timer = window.setInterval(refresh, seconds * 1000);
}

refreshButton.addEventListener("click", refresh);
clearPositionsButton.addEventListener("click", clearPositions);
clearTradesButton.addEventListener("click", clearTrades);
symbolsInput.addEventListener("change", handleSymbolSelectionChange);
intervalInput.addEventListener("change", () => {
  schedule();
  refresh();
});

loadConfig()
  .then(() => {
    loadPositions();
    loadTrades();
    renderPositions();
    renderTrades();
    schedule();
    refresh();
  })
  .catch((error) => {
    renderErrors([error.message]);
    statusText.textContent = "配置读取失败";
  });
