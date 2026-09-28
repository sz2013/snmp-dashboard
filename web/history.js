(function () {
  const NS = "http://www.w3.org/2000/svg";
  const RX_COLOR = "#3da9fc";
  const TX_COLOR = "#f59e0b";

  function el(name, attrs) {
    const node = document.createElementNS(NS, name);
    for (const key in attrs) node.setAttribute(key, attrs[key]);
    return node;
  }

  function fmtTime(t) {
    const d = new Date(t * 1000);
    return d.toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  window.renderHistoryChart = function (container, data, human, opts) {
    container.replaceChildren();
    const empty = document.getElementById("history-empty");
    const series = (data && data.series) || [];
    const vis = { rx: !opts || opts.rx !== false, tx: !opts || opts.tx !== false };

    if (!series.length) {
      if (empty) empty.textContent = "暂无历史数据";
      return;
    }
    if (empty) empty.textContent = "";

    const W = Math.max(320, container.clientWidth || 900);
    const H = 240;
    const padL = 78, padR = 18, padT = 16, padB = 30;
    const iw = W - padL - padR;
    const ih = H - padT - padB;

    const ts = series.map((d) => d.t);
    const tMin = Math.min.apply(null, ts);
    const tMax = Math.max.apply(null, ts);
    let maxY = 0;
    for (const d of series) {
      if (vis.rx) maxY = Math.max(maxY, d.rx);
      if (vis.tx) maxY = Math.max(maxY, d.tx);
    }
    if (maxY <= 0) maxY = 1;

    const x = (t) => padL + (tMax === tMin ? iw / 2 : ((t - tMin) / (tMax - tMin)) * iw);
    const y = (v) => padT + ih - (v / maxY) * ih;

    const svg = el("svg", { viewBox: "0 0 " + W + " " + H, width: "100%", height: H });

    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const yy = y((maxY * i) / ticks);
      svg.appendChild(el("line", { x1: padL, y1: yy, x2: W - padR, y2: yy, stroke: "#2a3a4d", "stroke-width": 1 }));
      const label = el("text", { x: padL - 8, y: yy + 4, fill: "#8aa0b4", "font-size": 11, "text-anchor": "end" });
      label.textContent = human((maxY * i) / ticks);
      svg.appendChild(label);
    }

    let xlabels;
    if (tMax === tMin) {
      xlabels = [tMin];
    } else {
      const tickCount = Math.min(20, Math.max(2, Math.floor(iw / 130) + 1));
      xlabels = [];
      for (let i = 0; i < tickCount; i++) {
        xlabels.push(tMin + ((tMax - tMin) * i) / (tickCount - 1));
      }
    }
    xlabels.forEach((t, i) => {
      const txt = el("text", {
        x: x(t), y: H - 8, fill: "#8aa0b4", "font-size": 11,
        "text-anchor": i === 0 ? "start" : i === xlabels.length - 1 ? "end" : "middle",
      });
      txt.textContent = fmtTime(t);
      svg.appendChild(txt);
    });

    const smoothPath = (pts) => {
      if (pts.length < 2) {
        return pts.length ? "M" + pts[0][0].toFixed(1) + " " + pts[0][1].toFixed(1) : "";
      }
      const n = pts.length;
      const delta = [];
      for (let i = 0; i < n - 1; i++) {
        const dx = pts[i + 1][0] - pts[i][0];
        delta[i] = dx === 0 ? 0 : (pts[i + 1][1] - pts[i][1]) / dx;
      }
      const m = [];
      m[0] = delta[0];
      m[n - 1] = delta[n - 2];
      for (let i = 1; i < n - 1; i++) {
        m[i] = delta[i - 1] * delta[i] <= 0 ? 0 : (delta[i - 1] + delta[i]) / 2;
      }
      for (let i = 0; i < n - 1; i++) {
        if (delta[i] === 0) {
          m[i] = m[i + 1] = 0;
          continue;
        }
        const a = m[i] / delta[i];
        const b = m[i + 1] / delta[i];
        const s = a * a + b * b;
        if (s > 9) {
          const t = 3 / Math.sqrt(s);
          m[i] = t * a * delta[i];
          m[i + 1] = t * b * delta[i];
        }
      }
      let d = "M" + pts[0][0].toFixed(1) + " " + pts[0][1].toFixed(1);
      for (let i = 0; i < n - 1; i++) {
        const x0 = pts[i][0], y0 = pts[i][1], x1 = pts[i + 1][0], y1 = pts[i + 1][1];
        const h = (x1 - x0) / 3;
        d += " C" + (x0 + h).toFixed(1) + " " + (y0 + m[i] * h).toFixed(1) +
          " " + (x1 - h).toFixed(1) + " " + (y1 - m[i + 1] * h).toFixed(1) +
          " " + x1.toFixed(1) + " " + y1.toFixed(1);
      }
      return d;
    };

    const drawLine = (key, color) => {
      const pts = series.map((d) => [x(d.t), y(d[key])]);
      svg.appendChild(el("path", {
        d: smoothPath(pts), fill: "none", stroke: color, "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": "round",
      }));
      if (series.length <= 120) {
        for (const p of pts) {
          svg.appendChild(el("circle", {
            cx: p[0].toFixed(1), cy: p[1].toFixed(1), r: 2.4, fill: color,
          }));
        }
      }
    };
    if (vis.tx) drawLine("tx", TX_COLOR);
    if (vis.rx) drawLine("rx", RX_COLOR);

    const guide = el("line", {
      x1: 0, y1: padT, x2: 0, y2: padT + ih, stroke: "#8aa0b4",
      "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden",
    });
    const markRx = el("circle", { r: 3.5, fill: RX_COLOR, stroke: "#0f1720", "stroke-width": 1.5, visibility: "hidden" });
    const markTx = el("circle", { r: 3.5, fill: TX_COLOR, stroke: "#0f1720", "stroke-width": 1.5, visibility: "hidden" });
    svg.appendChild(guide);
    svg.appendChild(markTx);
    svg.appendChild(markRx);

    const tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.style.display = "none";
    container.appendChild(tip);

    const fmtFull = (t) => new Date(t * 1000).toLocaleString([], {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    });

    const point = svg.createSVGPoint();
    const toUser = (clientX, clientY) => {
      point.x = clientX;
      point.y = clientY;
      return point.matrixTransform(svg.getScreenCTM().inverse());
    };

    const nearest = (ux) => {
      let best = series[0];
      let bestD = Infinity;
      for (const d of series) {
        const dist = Math.abs(x(d.t) - ux);
        if (dist < bestD) {
          bestD = dist;
          best = d;
        }
      }
      return best;
    };

    const show = (ev) => {
      const rect = container.getBoundingClientRect();
      const u = toUser(ev.clientX, ev.clientY);
      const d = nearest(u.x);
      const px = x(d.t);
      guide.setAttribute("x1", px.toFixed(1));
      guide.setAttribute("x2", px.toFixed(1));
      guide.setAttribute("visibility", "visible");
      if (vis.rx) {
        markRx.setAttribute("cx", px.toFixed(1));
        markRx.setAttribute("cy", y(d.rx).toFixed(1));
        markRx.setAttribute("visibility", "visible");
      }
      if (vis.tx) {
        markTx.setAttribute("cx", px.toFixed(1));
        markTx.setAttribute("cy", y(d.tx).toFixed(1));
        markTx.setAttribute("visibility", "visible");
      }
      let html = '<div class="tip-time">' + fmtFull(d.t) + "</div>";
      if (vis.rx) html += '<div class="tip-rx">RX ' + human(d.rx) + "</div>";
      if (vis.tx) html += '<div class="tip-tx">TX ' + human(d.tx) + "</div>";
      tip.innerHTML = html;
      let left = ev.clientX - rect.left + 14;
      let top = ev.clientY - rect.top - 12;
      tip.style.display = "block";
      if (left + tip.offsetWidth > rect.width) left = ev.clientX - rect.left - tip.offsetWidth - 14;
      if (top + tip.offsetHeight > rect.height) top = rect.height - tip.offsetHeight;
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = Math.max(0, top) + "px";
    };

    const hide = () => {
      guide.setAttribute("visibility", "hidden");
      markRx.setAttribute("visibility", "hidden");
      markTx.setAttribute("visibility", "hidden");
      tip.style.display = "none";
    };

    svg.addEventListener("mousemove", show);
    svg.addEventListener("mouseleave", hide);

    container.appendChild(svg);
  };
})();
