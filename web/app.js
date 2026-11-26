/* 座位地图前端：SSE 实时推送 + 空位趋势 + 纠错 */
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const S = {
    snap: null,
    layout: { cols: 5, aisle_after: 2 },
    nodes: new Map(),
    sel: null,
  };

  const STATE_TEXT = { free: "空", occupied: "占用", away: "暂离", unknown: "未知" };
  const MODE_TEXT = { diff: "差分模式", yolo: "视觉模型", mock: "演示数据" };

  /* ---------- 工具 ---------- */
  function ago(ts) {
    if (!ts) return "";
    const d = Math.max(0, Date.now() / 1000 - ts);
    if (d < 25) return "刚刚";
    if (d < 90) return "1 分钟前";
    if (d < 3600) return Math.round(d / 60) + " 分钟前";
    return Math.round(d / 3600) + " 小时前";
  }

  function setLive(cls) {
    const el = $("#live");
    el.className = "live" + (cls ? " " + cls : "");
  }

  /* ---------- 数据源 ---------- */
  function connect() {
    // ?once=1：只取一次快照，不建立长连接。
    // 用于截图、打印、以及在没有 SSE 的代理后面排障。
    if (new URLSearchParams(location.search).has("once")) {
      fetch("/api/state")
        .then((r) => r.json())
        .then((snap) => {
          S.snap = snap;
          if (snap.layout && snap.layout.cols) S.layout = snap.layout;
          render();
        })
        .catch(() => setLive("off"));
      return;
    }

    const es = new EventSource("/events");
    es.onopen = () => setLive("on");
    es.onerror = () => setLive("warn");
    es.onmessage = (ev) => {
      let snap;
      try {
        snap = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      S.snap = snap;
      if (snap.layout && snap.layout.cols) S.layout = snap.layout;
      render();
    };
  }

  /* ---------- 渲染 ---------- */
  function render() {
    const snap = S.snap;
    if (!snap) return;

    $("#room").textContent = snap.room || "自习室";
    $("#mode").textContent = MODE_TEXT[snap.mode] || snap.mode || "—";
    if (snap.source) $("#sub").textContent = `实时分析 · ${snap.source}`;

    const sm = snap.summary || {};
    $("#nFree").textContent = sm.free ?? "-";
    $("#nOcc").textContent = sm.occupied ?? "-";
    $("#nAway").textContent = sm.away ?? "-";
    $("#nUnk").textContent = sm.unknown ?? "-";

    renderAlert(snap);
    renderAdvice(snap);
    renderGrid(snap.seats || []);

    const live = snap.camera === "ok" || snap.camera === "mock";
    setLive(live ? "on" : "warn");
  }

  function renderAlert(snap) {
    const el = $("#alert");
    let msg = "";
    switch (snap.camera) {
      case "offline":
        msg = "<strong>相机离线。</strong>当前没有任何数据，请检查电源和连接线。";
        break;
      case "stalled":
        msg = "<strong>画面卡住了。</strong>超过 8 秒没有新画面，下面的状态可能已经过时。";
        break;
      case "disabled":
        msg =
          "<strong>没有接相机。</strong>可以在 <a href=\"/calibrate\">标定页</a> 上传一段录像，" +
          "或者用 <code>--source 0</code> 接上摄像头。";
        break;
      case "no_reference":
        msg =
          '<strong>还没拍空镜。</strong>先打开 <a href="/calibrate">标定页</a>：' +
          "房间空着时点「拍空镜」；没有相机的话，也可以在那里上传一段录像。";
        break;
    }
    if (msg) {
      el.innerHTML = msg;
      el.classList.remove("hidden");
    } else {
      el.classList.add("hidden");
    }
  }

  function renderAdvice(snap) {
    const a = snap.advice || {};
    const box = $("#advice");
    box.className = "advice" + (a.level ? " " + a.level : "");
    $("#adviceText").textContent = a.text || "—";
    const parts = [];
    if (typeof a.free === "number") parts.push(`当前 ${a.free}/${a.total} 个空位`);
    if (a.unknown) parts.push(`${a.unknown} 个座位暂时看不到`);
    $("#adviceSub").textContent = parts.join(" · ");
  }

  // 只有座位图变了才重建 DOM，否则每 2 秒重建会让动画和点击都发虚
  let gridSig = null;

  function buildCard(seat) {
    const el = document.createElement("button");
    el.className = "seat";
    el.innerHTML = '<span class="sid"></span><span class="st"></span><span class="ago"></span>';
    el.addEventListener("click", () => openSheet(seat.id));
    S.nodes.set(seat.id, el);
    return el;
  }

  function rebuildGrid(seats) {
    const root = $("#grid");
    root.innerHTML = "";
    S.nodes.clear();
    const layout = S.layout || {};

    if (Array.isArray(layout.groups) && layout.groups.length) {
      // 真实房间版式：每个分区独立成块（隔断位 / A 桌 / B 桌）
      const byId = new Map(seats.map((s) => [s.id, s]));
      const placed = new Set();

      // 同一个 row 的分组并排放（比如 A 桌、B 桌本来就是并排的两张桌子）
      const rows = [];
      layout.groups.forEach((g, i) => {
        const r = Number.isInteger(g.row) ? g.row : 1000 + i;
        let bucket = rows.find((x) => x.row === r);
        if (!bucket) {
          bucket = { row: r, items: [] };
          rows.push(bucket);
        }
        bucket.items.push(g);
      });
      rows.sort((a, b) => a.row - b.row);

      rows.forEach(({ items }) => {
        const rowEl = document.createElement("div");
        rowEl.className = "group-row";
        items.forEach((g) => {
          const wrap = document.createElement("div");
          wrap.className = "group";
          if (g.title) {
            const t = document.createElement("div");
            t.className = "group-title";
            t.textContent = g.title;
            wrap.appendChild(t);
          }
          const n = g.cols || 4;
          const inner = document.createElement("div");
          inner.className = "seat-grid";
          inner.style.gridTemplateColumns = `repeat(${n}, minmax(0, 1fr))`;
          if (n <= 3) inner.style.maxWidth = n * 118 + "px"; // 两人/四人桌别拉成横条
          (g.seat_ids || []).forEach((id) => {
            const seat = byId.get(id);
            if (!seat) return;
            placed.add(id);
            inner.appendChild(buildCard(seat));
          });
          wrap.appendChild(inner);
          rowEl.appendChild(wrap);
        });
        root.appendChild(rowEl);
      });

      const rest = seats.filter((s) => !placed.has(s.id));
      if (rest.length) {
        const wrap = document.createElement("div");
        wrap.className = "group";
        const inner = document.createElement("div");
        inner.className = "seat-grid";
        inner.style.gridTemplateColumns = "repeat(5, minmax(0, 1fr))";
        rest.forEach((s) => inner.appendChild(buildCard(s)));
        wrap.appendChild(inner);
        root.appendChild(wrap);
      }
      return;
    }

    // 兜底：均匀网格
    const { cols = 5, aisle_after = 2 } = layout;
    const inner = document.createElement("div");
    inner.className = "seat-grid";
    inner.style.gridTemplateColumns = `repeat(${cols + 1}, minmax(0, 1fr))`;
    seats.forEach((seat, i) => {
      const el = buildCard(seat);
      const c = i % cols;
      const r = Math.floor(i / cols);
      el.style.gridColumn = String(c + 1 + (c > aisle_after ? 1 : 0));
      el.style.gridRow = String(r + 1);
      inner.appendChild(el);
    });
    root.appendChild(inner);
  }

  function renderGrid(seats) {
    const sig = JSON.stringify(S.layout) + "|" + seats.map((s) => s.id).join(",");
    if (sig !== gridSig) {
      gridSig = sig;
      rebuildGrid(seats);
    }

    seats.forEach((seat) => {
      const el = S.nodes.get(seat.id);
      if (!el) return;
      el.dataset.state = seat.state;
      el.dataset.id = seat.id;
      $(".sid", el).textContent = seat.label;
      $(".st", el).textContent = STATE_TEXT[seat.state] || seat.state;
      $(".ago", el).textContent = seat.state === "unknown" ? "无数据" : ago(seat.updated);
      el.title = `${seat.label}${
        seat.tags && seat.tags.length ? " · " + seat.tags.join(" / ") : ""
      }`;
    });
  }

  /* ---------- 空位趋势 ---------- */
  async function loadTrend() {
    try {
      const r = await fetch("/api/history?minutes=60");
      const d = await r.json();
      drawSpark(d.records || []);
    } catch (e) {
      /* 忽略 */
    }
  }

  function drawSpark(records) {
    const cv = $("#spark");
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = cv.clientWidth || 320;
    const h = 90;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    if (records.length < 2) {
      ctx.fillStyle = "#94a3b8";
      ctx.font = "12px -apple-system, sans-serif";
      ctx.fillText("数据还不够，等一会儿再来看", 4, h / 2);
      return;
    }

    const total = records[records.length - 1].total || 15;
    const maxV = Math.max(total, ...records.map((r) => r.free), 1);
    const pad = 6;
    const x = (i) => (i / (records.length - 1)) * (w - pad * 2) + pad;
    const y = (v) => h - pad - (v / maxV) * (h - pad * 2);

    // 参考线
    ctx.strokeStyle = "#eef2f7";
    ctx.lineWidth = 1;
    for (const f of [0.5, 1]) {
      ctx.beginPath();
      ctx.moveTo(pad, y(maxV * f));
      ctx.lineTo(w - pad, y(maxV * f));
      ctx.stroke();
    }

    // 面积
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, "rgba(22,163,74,0.22)");
    grad.addColorStop(1, "rgba(22,163,74,0.01)");
    ctx.beginPath();
    ctx.moveTo(x(0), y(records[0].free));
    records.forEach((r, i) => ctx.lineTo(x(i), y(r.free)));
    ctx.lineTo(x(records.length - 1), h - pad);
    ctx.lineTo(x(0), h - pad);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // 折线
    ctx.beginPath();
    records.forEach((r, i) => (i ? ctx.lineTo(x(i), y(r.free)) : ctx.moveTo(x(i), y(r.free))));
    ctx.strokeStyle = "#16a34a";
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();

    // 当前值
    const last = records[records.length - 1];
    ctx.fillStyle = "#16a34a";
    ctx.beginPath();
    ctx.arc(x(records.length - 1), y(last.free), 3.5, 0, Math.PI * 2);
    ctx.fill();

    $("#sparkNote").textContent = `最近一小时空位变化，现在 ${last.free} 个`;
  }

  /* ---------- 接下来几小时 ---------- */
  async function loadForecast() {
    try {
      const r = await fetch("/api/forecast?hours=3");
      const d = await r.json();
      const box = $("#fc");
      if (d.status !== "ok" || !d.points || !d.points.length) {
        box.innerHTML = `<span class="muted small">${
          d.message || "数据还不够，先积累几天历史，才能预测"
        }</span>`;
        return;
      }
      const now = d.points_now && typeof d.points_now.free === "number" ? d.points_now.free : null;
      let html = "";
      if (now !== null) {
        html += `<div class="chip now"><div class="t">现在</div><div class="v">${now}</div><div class="p">空位</div></div>`;
      }
      html += d.points
        .map(
          (p) =>
            `<div class="chip"><div class="t">${p.iso}</div><div class="v">${Math.round(
              p.pred_free
            )}</div><div class="p">${Math.round(p.p_find * 100)}% 有座</div></div>`
        )
        .join("");
      box.innerHTML = html;
    } catch (e) {
      /* 忽略 */
    }
  }

  /* ---------- 纠错弹层 ---------- */
  function openSheet(id) {
    const seat = (S.snap.seats || []).find((s) => s.id === id);
    if (!seat) return;
    S.sel = id;
    $("#sheetTitle").textContent = seat.label;
    const st = STATE_TEXT[seat.state] || seat.state;
    $("#sheetMeta").textContent = `系统判断：${st} · ${ago(seat.updated)}更新`;
    $("#sheet").classList.remove("hidden");
  }

  function closeSheet() {
    $("#sheet").classList.add("hidden");
    S.sel = null;
  }

  async function report(says) {
    if (!S.sel) return;
    try {
      await fetch("/api/report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seat_id: S.sel, says }),
      });
    } catch (e) {
      /* 忽略 */
    }
    closeSheet();
  }

  /* ---------- 启动 ---------- */
  function boot() {
    $("#sheetClose").addEventListener("click", closeSheet);
    $("#sheet").addEventListener("click", (e) => {
      if (e.target.id === "sheet") closeSheet();
    });
    document.querySelectorAll("[data-says]").forEach((b) =>
      b.addEventListener("click", () => report(b.dataset.says))
    );
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeSheet();
    });

    connect();
    loadTrend();
    loadForecast();
    setInterval(loadTrend, 60_000);
    setInterval(loadForecast, 300_000);
    window.addEventListener("resize", () => {
      if (S.snap) loadTrend();
    });
  }

  boot();
})();
