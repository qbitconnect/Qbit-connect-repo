/* QBIT Connect — dependency-free SVG chart renderer (Phase 10 §30).
 *
 * Renders charts from declarative JSON embedded in the page:
 *   <div class="qchart" data-chart='{"type":"line|bar|donut|funnel",
 *        "labels":[...], "series":[{"name":"n","values":[...]}]}'></div>
 *
 * Design rules: no external libraries; dark-theme tokens from qbit.css;
 * readable legends; hover tooltips; "No data available" empty state.
 */
(function () {
  "use strict";

  var CSS = getComputedStyle(document.documentElement);
  function token(name, fallback) {
    var v = CSS.getPropertyValue(name).trim();
    return v || fallback;
  }
  var COLORS = [
    token("--accent", "#4f8ef7"), "#35d07f", "#f5a623", "#e05252",
    "#9b6ef3", "#2dd4bf", "#f472b6", "#a3e635", "#60a5fa", "#fbbf24"
  ];
  var GRID = "rgba(139,152,169,.15)";
  var TEXT = token("--muted", "#8b98a9");

  function fmt(n) {
    if (n === null || n === undefined) return "—";
    if (typeof n !== "number") return String(n);
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "k";
    if (Math.abs(n) >= 1 && n % 1 !== 0) return n.toFixed(2);
    return String(n);
  }

  function el(tag, attrs) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in (attrs || {})) node.setAttribute(k, attrs[k]);
    return node;
  }

  function emptyState(box) {
    box.innerHTML = "";
    var empty = document.createElement("div");
    empty.className = "qchart-empty";
    empty.textContent = "No data available";
    box.appendChild(empty);
  }

  function tooltip(svg) {
    var tip = document.createElement("div");
    tip.className = "qchart-tip";
    tip.style.display = "none";
    svg.parentNode.appendChild(tip);
    function show(evt, html) {
      tip.innerHTML = html;
      tip.style.display = "block";
      var rect = svg.parentNode.getBoundingClientRect();
      tip.style.left = Math.min(evt.clientX - rect.left + 12, rect.width - 130) + "px";
      tip.style.top = (evt.clientY - rect.top - 30) + "px";
    }
    function hide() { tip.style.display = "none"; }
    return { show: show, hide: hide };
  }

  /* ------------------------------------------------------------- line/area */
  function renderLine(box, cfg, area) {
    var series = cfg.series.filter(function (s) {
      return (s.values || []).some(function (v) { return v !== null && v !== undefined && v !== 0; });
    });
    if (!cfg.labels.length || !series.length) return emptyState(box);

    var W = 720, H = 260, padL = 44, padR = 12, padT = 14, padB = 30;
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, "class": "qchart-svg" });
    var tip = tooltip(svg);
    var maxV = 0;
    series.forEach(function (s) {
      s.values.forEach(function (v) { if (v !== null && v > maxV) maxV = v; });
    });
    if (maxV <= 0) maxV = 1;
    var niceMax = Math.ceil(maxV * 1.1) || 1;
    var n = cfg.labels.length;
    var step = n > 1 ? (W - padL - padR) / (n - 1) : 0;

    // grid + y labels
    for (var g = 0; g <= 4; g++) {
      var y = padT + (H - padT - padB) * (1 - g / 4);
      svg.appendChild(el("line", { x1: padL, x2: W - padR, y1: y, y2: y,
        stroke: GRID, "stroke-width": 1 }));
      var lbl = el("text", { x: padL - 6, y: y + 4, "text-anchor": "end",
        fill: TEXT, "font-size": 10 });
      lbl.textContent = fmt(niceMax * g / 4);
      svg.appendChild(lbl);
    }

    // x labels (max 8 shown)
    var every = Math.max(1, Math.ceil(n / 8));
    cfg.labels.forEach(function (label, i) {
      if (i % every !== 0 && i !== n - 1) return;
      var x = padL + step * i;
      var t = el("text", { x: x, y: H - 8, "text-anchor": "middle",
        fill: TEXT, "font-size": 10 });
      t.textContent = String(label).slice(5); // MM-DD
      svg.appendChild(t);
    });

    series.forEach(function (s, si) {
      var color = COLORS[si % COLORS.length];
      var points = [];
      s.values.forEach(function (v, i) {
        if (v === null || v === undefined) return;
        points.push([padL + step * i,
          padT + (H - padT - padB) * (1 - v / niceMax), v, i]);
      });
      if (!points.length) return;
      var path = points.map(function (p, i) {
        return (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1);
      }).join(" ");
      if (area) {
        var areaPath = path +
          " L" + points[points.length - 1][0].toFixed(1) + " " + (H - padB) +
          " L" + points[0][0].toFixed(1) + " " + (H - padB) + " Z";
        svg.appendChild(el("path", { d: areaPath, fill: color, opacity: 0.12 }));
      }
      svg.appendChild(el("path", { d: path, fill: "none", stroke: color,
        "stroke-width": 2, "stroke-linejoin": "round" }));
      points.forEach(function (p) {
        var dot = el("circle", { cx: p[0], cy: p[1], r: 3, fill: color });
        dot.addEventListener("mousemove", function (evt) {
          tip.show(evt, "<b>" + s.name + "</b><br>" + cfg.labels[p[3]] + ": " + fmt(p[2]));
        });
        dot.addEventListener("mouseleave", tip.hide);
        svg.appendChild(dot);
      });
    });

    box.appendChild(svg);
    legend(box, series);
  }

  /* ------------------------------------------------------------------- bar */
  function renderBar(box, cfg, horizontal) {
    var series = cfg.series || [];
    var hasData = series.some(function (s) {
      return (s.values || []).some(function (v) { return v !== null && v !== undefined && v > 0; });
    });
    if (!cfg.labels.length || !hasData) return emptyState(box);

    var W = 720, rowH = horizontal ? 34 : 0, H = horizontal
      ? Math.max(80, cfg.labels.length * rowH + 16)
      : 260;
    var padL = horizontal ? 130 : 44, padR = 14, padT = 12, padB = 26;
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, "class": "qchart-svg" });
    var tip = tooltip(svg);
    var maxV = 0;
    series.forEach(function (s) {
      s.values.forEach(function (v) { if (v !== null && v > maxV) maxV = v; });
    });
    if (maxV <= 0) maxV = 1;
    var n = cfg.labels.length;

    if (horizontal) {
      cfg.labels.forEach(function (label, i) {
        var y = padT + i * rowH;
        var t = el("text", { x: padL - 8, y: y + 16, "text-anchor": "end",
          fill: TEXT, "font-size": 11 });
        t.textContent = String(label).slice(0, 20);
        svg.appendChild(t);
        series.forEach(function (s, si) {
          var v = s.values[i];
          if (v === null || v === undefined) return;
          var w = Math.max(1, (W - padL - padR) * (v / maxV));
          var h = Math.max(6, 14 / Math.max(1, series.length) - 3);
          var bar = el("rect", { x: padL, y: y + si * h + 2, width: w, height: h,
            fill: COLORS[si % COLORS.length], rx: 2 });
          bar.addEventListener("mousemove", function (evt) {
            tip.show(evt, "<b>" + label + "</b><br>" + s.name + ": " + fmt(v));
          });
          bar.addEventListener("mouseleave", tip.hide);
          svg.appendChild(bar);
        });
      });
    } else {
      var inner = W - padL - padR;
      var groupW = inner / Math.max(1, n);
      var barW = Math.max(3, Math.min(38, groupW * 0.62 / series.length));
      for (var gy = 0; gy <= 4; gy++) {
        var yy = padT + (H - padT - padB) * (1 - gy / 4);
        svg.appendChild(el("line", { x1: padL, x2: W - padR, y1: yy, y2: yy,
          stroke: GRID, "stroke-width": 1 }));
      }
      cfg.labels.forEach(function (label, i) {
        var x0 = padL + groupW * i;
        series.forEach(function (s, si) {
          var v = s.values[i];
          if (v === null || v === undefined) return;
          var h = (H - padT - padB) * (v / maxV);
          var bar = el("rect", {
            x: x0 + groupW * 0.19 + si * barW, y: H - padB - h,
            width: barW, height: Math.max(h, 1), fill: COLORS[si % COLORS.length], rx: 2,
          });
          bar.addEventListener("mousemove", function (evt) {
            tip.show(evt, "<b>" + label + "</b><br>" + s.name + ": " + fmt(v));
          });
          bar.addEventListener("mouseleave", tip.hide);
          svg.appendChild(bar);
        });
        var t = el("text", { x: x0 + groupW / 2, y: H - 8, "text-anchor": "middle",
          fill: TEXT, "font-size": 10 });
        t.textContent = String(label).slice(0, 10);
        svg.appendChild(t);
      });
    }

    box.appendChild(svg);
    legend(box, series);
  }

  /* ----------------------------------------------------------------- donut */
  function renderDonut(box, cfg) {
    var values = [];
    cfg.labels.forEach(function (label, i) {
      var v = (cfg.series[0] || { values: [] }).values[i];
      if (v !== null && v !== undefined && v > 0) values.push([label, v]);
    });
    if (!values.length) return emptyState(box);

    var total = values.reduce(function (a, p) { return a + p[1]; }, 0);
    var W = 300, H = 300, cx = W / 2, cy = H / 2, r = 105, rIn = 62;
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, "class": "qchart-svg" });
    var tip = tooltip(svg);
    var angle = -Math.PI / 2;
    values.forEach(function (pair, i) {
      var frac = pair[1] / total;
      var a2 = angle + frac * Math.PI * 2;
      var large = frac > 0.5 ? 1 : 0;
      var x1 = cx + r * Math.cos(angle), y1 = cy + r * Math.sin(angle);
      var x2 = cx + r * Math.cos(a2), y2 = cy + r * Math.sin(a2);
      var x3 = cx + rIn * Math.cos(a2), y3 = cy + rIn * Math.sin(a2);
      var x4 = cx + rIn * Math.cos(angle), y4 = cy + rIn * Math.sin(angle);
      var d = "M" + x1 + " " + y1 + " A" + r + " " + r + " 0 " + large + " 1 " +
        x2 + " " + y2 + " L" + x3 + " " + y3 + " A" + rIn + " " + rIn + " 0 " +
        large + " 0 " + x4 + " " + y4 + " Z";
      var slice = el("path", { d: d, fill: COLORS[i % COLORS.length], opacity: 0.92 });
      slice.addEventListener("mousemove", function (evt) {
        tip.show(evt, "<b>" + pair[0] + "</b><br>" + fmt(pair[1]) +
          " (" + Math.round(frac * 100) + "%)");
      });
      slice.addEventListener("mouseleave", tip.hide);
      svg.appendChild(slice);
      angle = a2;
    });
    box.appendChild(svg);
    legend(box, values.map(function (p) {
      return { name: p[0], values: [p[1]] };
    }));
  }

  /* ---------------------------------------------------------------- funnel */
  function renderFunnel(box, cfg) {
    var labels = cfg.labels, values = cfg.series[0] ? cfg.series[0].values : [];
    if (!labels.length || !values.length) return emptyState(box);
    var W = 720, rowH = 40, H = labels.length * rowH + 8;
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, "class": "qchart-svg" });
    var maxV = Math.max.apply(null, values) || 1;
    labels.forEach(function (label, i) {
      var v = values[i];
      if (v === null || v === undefined) return;
      var w = Math.max(4, (W - 220) * (v / maxV));
      var y = i * rowH + 6;
      var rect = el("rect", { x: 110, y: y, width: w, height: rowH - 10,
        fill: COLORS[i % COLORS.length], opacity: 0.9, rx: 3 });
      var name = el("text", { x: 100, y: y + 20, "text-anchor": "end",
        fill: TEXT, "font-size": 12 });
      name.textContent = label;
      var count = el("text", { x: 110 + w + 10, y: y + 20, fill: TEXT,
        "font-size": 12 });
      count.textContent = fmt(v);
      svg.appendChild(rect); svg.appendChild(name); svg.appendChild(count);
    });
    box.appendChild(svg);
    if (cfg.series[0] && cfg.series[0].name) legend(box, [cfg.series[0]]);
  }

  function legend(box, series) {
    var wrap = document.createElement("div");
    wrap.className = "qchart-legend";
    series.forEach(function (s, i) {
      var item = document.createElement("span");
      item.className = "qchart-key";
      var dot = document.createElement("i");
      dot.style.background = COLORS[i % COLORS.length];
      item.appendChild(dot);
      item.appendChild(document.createTextNode(s.name));
      wrap.appendChild(item);
    });
    box.appendChild(wrap);
  }

  var RENDERERS = {
    line: function (b, c) { renderLine(b, c, false); },
    area: function (b, c) { renderLine(b, c, true); },
    bar: function (b, c) { renderBar(b, c, false); },
    "bar-h": function (b, c) { renderBar(b, c, true); },
    donut: function (b, c) { renderDonut(b, c); },
    funnel: function (b, c) { renderFunnel(b, c); },
  };

  function renderAll(root) {
    (root || document).querySelectorAll(".qchart[data-chart]").forEach(function (box) {
      if (box.dataset.rendered) return;
      box.dataset.rendered = "1";
      var cfg;
      try { cfg = JSON.parse(box.dataset.chart); } catch (e) { return; }
      (RENDERERS[cfg.type] || RENDERERS.bar)(box, cfg);
    });
  }

  window.QBITCharts = { renderAll: renderAll };
  document.addEventListener("DOMContentLoaded", function () { renderAll(); });
})();
