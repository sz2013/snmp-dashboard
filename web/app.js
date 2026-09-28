const els = {
  host: document.getElementById("host"),
  dot: document.getElementById("status-dot"),
  updated: document.getElementById("updated"),
  alert: document.getElementById("alert"),
  auto: document.getElementById("auto"),
  interval: document.getElementById("interval"),
  refresh: document.getElementById("refresh"),
  cpu: document.getElementById("cpu"),
  mem: document.getElementById("mem"),
  temp: document.getElementById("temp"),
  up: document.getElementById("up"),
  total: document.getElementById("total"),
  sysUpTime: document.getElementById("sysUpTime"),
  ports: document.getElementById("ports"),
  empty: document.getElementById("empty"),
  historyPanel: document.getElementById("history-panel"),
  historyTitle: document.getElementById("history-title"),
  historyChart: document.getElementById("history-chart"),
  historyEmpty: document.getElementById("history-empty"),
  toggleRx: document.getElementById("toggle-rx"),
  toggleTx: document.getElementById("toggle-tx"),
  ranges: document.getElementById("ranges"),
};

let timer = null;
let selectedPort = null;
let rangeSeconds = 3600;
let latestPorts = [];
let lastHistoryAt = 0;
let lastHistory = null;
let showRx = true;
let showTx = true;

function human(bps) {
  if (bps >= 1e9) return (bps / 1e9).toFixed(2) + " Gbps";
  if (bps >= 1e6) return (bps / 1e6).toFixed(2) + " Mbps";
  if (bps >= 1e3) return (bps / 1e3).toFixed(2) + " Kbps";
  return Math.round(bps) + " bps";
}

function setText(el, value) {
  el.textContent = value === null || value === undefined || value === "" ? "--" : value;
}

function operClass(oper) {
  if (oper === "up") return "up";
  if (oper === "down") return "down";
  return "other";
}

function renderPorts(ports) {
  latestPorts = ports;
  const shown = ports.filter((p) => p.oper === "up");
  els.ports.replaceChildren();
  els.empty.style.display = shown.length ? "none" : "block";
  for (const p of shown) {
    const tr = document.createElement("tr");
    tr.className = "port-row";
    if (selectedPort !== null && String(p.idx) === String(selectedPort)) {
      tr.classList.add("selected");
    }

    const tdName = document.createElement("td");
    tdName.textContent = p.name;

    const tdOper = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge " + operClass(p.oper);
    badge.textContent = p.oper;
    tdOper.appendChild(badge);

    const mkRate = (bps) => {
      const td = document.createElement("td");
      td.className = "num";
      td.textContent = human(bps);
      return td;
    };

    tr.append(tdName, tdOper, mkRate(p.rx_bps), mkRate(p.tx_bps));
    tr.addEventListener("click", () => selectPort(p.idx));
    els.ports.appendChild(tr);
  }
}

function selectPort(idx) {
  selectedPort = idx;
  renderPorts(latestPorts);
  els.historyPanel.classList.remove("hidden");
  loadHistory(true);
}

async function loadHistory(force) {
  if (selectedPort === null) return;
  const now = Date.now();
  if (!force && now - lastHistoryAt < 10000) return;
  lastHistoryAt = now;
  try {
    const res = await fetch(
      "/api/history?port=" + encodeURIComponent(selectedPort) + "&seconds=" + rangeSeconds,
      { cache: "no-store" }
    );
    const data = await res.json();
    const current = latestPorts.find((p) => String(p.idx) === String(selectedPort)) || {};
    els.historyTitle.textContent = current.name || data.name || "if" + selectedPort;
    lastHistory = data;
    window.renderHistoryChart(els.historyChart, data, human, { rx: showRx, tx: showTx });
  } catch (err) {
    els.historyEmpty.textContent = "加载历史失败：" + err.message;
  }
}

function toggleSeries(key) {
  const on = key === "rx" ? (showRx = !showRx) : (showTx = !showTx);
  const node = key === "rx" ? els.toggleRx : els.toggleTx;
  node.classList.toggle("off", !on);
  node.setAttribute("aria-pressed", on ? "true" : "false");
  if (lastHistory) {
    window.renderHistoryChart(els.historyChart, lastHistory, human, { rx: showRx, tx: showTx });
  }
}

function bindToggle(node, key) {
  node.addEventListener("click", () => toggleSeries(key));
  node.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      toggleSeries(key);
    }
  });
}

function render(data) {
  if (!data.ok) {
    els.dot.className = "dot err";
    els.updated.textContent = "SNMP 无响应";
    els.alert.textContent = "采集失败：" + (data.error || "未知错误");
    els.alert.classList.remove("hidden");
    return;
  }

  els.alert.classList.add("hidden");
  els.dot.className = "dot ok";
  els.host.textContent = data.host;
  els.updated.textContent = "更新于 " + new Date(data.ts * 1000).toLocaleTimeString();

  const h = data.health || {};
  setText(els.cpu, h.cpu);
  setText(els.mem, h.mem);
  setText(els.temp, h.temp);
  setText(els.up, data.counts.up);
  setText(els.total, "/" + data.counts.total);

  const s = data.system || {};
  setText(els.sysUpTime, s.sysUpTime);

  renderPorts(data.ports || []);
  if (selectedPort === null) {
    const first = (data.ports || []).find((p) => p.oper === "up");
    if (first) {
      selectPort(first.idx);
      return;
    }
  }
  loadHistory(false);
}

async function load() {
  try {
    const res = await fetch("/api/data", { cache: "no-store" });
    render(await res.json());
  } catch (err) {
    els.dot.className = "dot err";
    els.updated.textContent = "连接失败";
    els.alert.textContent = "无法连接后端：" + err.message;
    els.alert.classList.remove("hidden");
  }
}

function schedule() {
  clearInterval(timer);
  if (els.auto.checked) {
    timer = setInterval(load, Number(els.interval.value));
  }
}

els.auto.addEventListener("change", schedule);
els.interval.addEventListener("change", schedule);
els.refresh.addEventListener("click", load);
els.ranges.querySelectorAll("button").forEach((btn) => {
  btn.addEventListener("click", () => {
    rangeSeconds = Number(btn.dataset.seconds);
    els.ranges.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    loadHistory(true);
  });
});
bindToggle(els.toggleRx, "rx");
bindToggle(els.toggleTx, "tx");

load();
schedule();
