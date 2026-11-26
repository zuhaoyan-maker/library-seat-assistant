/* 座位标定：在实景画面上框出每个座位，保存到服务端 */
(() => {
  const $ = (s) => document.querySelector(s);
  const cv = $("#cv");
  const ctx = cv.getContext("2d");

  const S = { img: null, points: [], seats: [], layout: null, pool: [], loaded: false };

  /* ---------- 画面 ---------- */
  function loadImage() {
    const img = new Image();
    img.onload = () => {
      S.img = img;
      cv.width = img.naturalWidth;
      cv.height = img.naturalHeight;
      $("#msg").style.display = "none";
      cv.style.display = "block";
      redraw();
    };
    img.onerror = () => {
      $("#msg").innerHTML =
        "拿不到画面。<br>" +
        "· 用 <code>--source none</code> 启动时只能上传录像<br>" +
        "· 用摄像头时确认已授权，且没被其他程序占用";
      $("#msg").style.display = "";
      cv.style.display = "none";
    };
    img.src = "/api/snapshot.jpg?t=" + Date.now();
  }

  function redraw() {
    if (!S.img) return;
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.drawImage(S.img, 0, 0);

    const fs = Math.max(13, Math.round(cv.width / 55));
    ctx.font = `600 ${fs}px -apple-system, "PingFang SC", sans-serif`;
    ctx.textBaseline = "top";

    // 已保存的座位
    S.seats.forEach((seat) => {
      const pts = seat.polygon.map(([x, y]) => [x * cv.width, y * cv.height]);
      ctx.beginPath();
      pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.closePath();
      ctx.fillStyle = "rgba(22, 163, 74, 0.22)";
      ctx.fill();
      ctx.strokeStyle = "#16a34a";
      ctx.lineWidth = 3;
      ctx.stroke();

      const [lx, ly] = pts[0];
      ctx.fillStyle = "rgba(0,0,0,0.62)";
      const w = ctx.measureText(seat.id).width + 14;
      ctx.fillRect(lx, ly, w, fs + 10);
      ctx.fillStyle = "#fff";
      ctx.fillText(seat.id, lx + 7, ly + 5);
    });

    // 正在画的多边形
    if (S.points.length) {
      ctx.beginPath();
      S.points.forEach(([x, y], i) =>
        i ? ctx.lineTo(x * cv.width, y * cv.height) : ctx.moveTo(x * cv.width, y * cv.height)
      );
      ctx.strokeStyle = "#f59e0b";
      ctx.lineWidth = 3;
      ctx.setLineDash([8, 6]);
      ctx.stroke();
      ctx.setLineDash([]);

      S.points.forEach(([x, y]) => {
        ctx.beginPath();
        ctx.arc(x * cv.width, y * cv.height, 6, 0, Math.PI * 2);
        ctx.fillStyle = "#f59e0b";
        ctx.fill();
      });
    }
  }

  /* ---------- 交互 ---------- */
  cv.addEventListener("click", (e) => {
    if (!S.img) return;
    const r = cv.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return;
    S.points.push([Number(x.toFixed(4)), Number(y.toFixed(4))]);
    redraw();
  });

  function finishSeat() {
    if (S.points.length < 3) {
      alert("至少点 3 个角（推荐 4 个角）");
      return;
    }
    // 优先沿用原座位图的编号顺序（C1..C7 / A1..A4 / B1..B4），
    // 这样即使清空重画，首页的分区版式也不会散
    const used = new Set(S.seats.map((s) => s.id));
    const tpl = S.pool.find((t) => !used.has(t.id));
    let id, label, tags;
    if (tpl) {
      id = tpl.id;
      label = tpl.label;
      tags = tpl.tags || [];
    } else {
      let n = S.seats.length + 1;
      while (used.has("A" + n)) n++;
      id = label = "A" + n;
      tags = [];
    }
    S.seats.push({ id, label, polygon: S.points.slice(), tags });
    S.points = [];
    renderList();
    redraw();
  }

  function renderList() {
    const box = $("#list");
    $("#count").textContent = S.seats.length;
    if (!S.seats.length) {
      box.innerHTML = '<span class="muted small">还没画座位</span>';
      return;
    }
    box.innerHTML = S.seats
      .map(
        (s) =>
          `<span class="tag">${s.id}<button data-del="${s.id}" aria-label="删除">×</button></span>`
      )
      .join("");
    box.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", () => {
        S.seats = S.seats.filter((s) => s.id !== b.dataset.del);
        renderList();
        redraw();
      })
    );
  }

  /* ---------- 保存 ---------- */
  async function save() {
    if (!S.seats.length) {
      alert("还没有座位");
      return;
    }
    // 已经用了分区版式（隔断位/A桌/B桌）就原样保留，别被"列数"覆盖掉
    const layout =
      S.layout && Array.isArray(S.layout.groups) && S.layout.groups.length
        ? S.layout
        : {
            cols: Math.max(1, parseInt($("#cols").value, 10) || 5),
            aisle_after: Math.max(0, parseInt($("#aisle").value, 10) || 0),
          };
    const body = {
      room: $("#room").value.trim() || "自习室",
      layout,
      seats: S.seats,
    };
    const r = await fetch("/api/seatmap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      alert("保存失败：" + (d.error || r.status));
      return;
    }
    alert(`已保存 ${d.seats} 个座位，可以回首页看效果了`);
  }

  /* ---------- 调试判分 ---------- */
  let scoresTimer = null;
  async function pollScores() {
    try {
      const r = await fetch("/api/debug/scores");
      if (!r.ok) {
        $("#scores").innerHTML = '<span class="muted small">仅本机可见</span>';
        clearInterval(scoresTimer);
        return;
      }
      const d = await r.json();
      if (d.frame) {
        $("#res").textContent = `画面 ${d.frame.w}×${d.frame.h}`;
      }
      const entries = Object.entries(d.scores || {});
      if (!entries.length) {
        $("#scores").innerHTML = '<span class="muted small">等待第一帧…</span>';
        return;
      }
      entries.sort((a, b) => b[1] - a[1]);
      const hot = d.thresholds.person;
      $("#scores").innerHTML = entries
        .map(
          ([id, v]) =>
            `<div class="score ${v >= hot ? "hot" : ""}"><span>${id}</span><b>${v.toFixed(2)}</b></div>`
        )
        .join("");
    } catch (e) {
      /* 忽略 */
    }
  }

  /* ---------- 视频上传 ---------- */
  function uploadVideo() {
    const input = $("#videoFile");
    const file = input.files && input.files[0];
    if (!file) {
      alert("先选一个视频文件");
      return;
    }
    const status = $("#upStatus");
    const btn = $("#btnUpload");
    btn.disabled = true;
    status.textContent = "上传中 0%";

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload?name=" + encodeURIComponent(file.name));
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        status.textContent = `上传中 ${Math.round((e.loaded / e.total) * 100)}%`;
      }
    };
    xhr.onload = () => {
      btn.disabled = false;
      let d = {};
      try {
        d = JSON.parse(xhr.responseText);
      } catch (e) {}
      if (xhr.status !== 200) {
        status.textContent = "失败：" + (d.error || xhr.status);
        return;
      }
      status.innerHTML = `已开始分析 <b>${d.file}</b> · 正在取第一帧当空镜…`;
      setTimeout(() => {
        loadImage();
        status.innerHTML += `<br><a href="/">→ 去首页看效果</a>`;
      }, 2000);
      setTimeout(pollScores, 2500);
    };
    xhr.onerror = () => {
      btn.disabled = false;
      status.textContent = "上传失败（网络中断）";
    };
    xhr.send(file);
  }

  /* ---------- 启动 ---------- */
  async function boot() {
    try {
      const r = await fetch("/api/seatmap");
      const d = await r.json();
      if (d.seats && d.seats.length) {
        S.seats = d.seats.map((s) => ({ id: s.id, label: s.label, polygon: s.polygon, tags: s.tags || [] }));
        S.pool = d.seats.map((s) => ({ id: s.id, label: s.label, tags: s.tags || [] }));
        $("#room").value = d.room || "自习室";
        S.layout = d.layout || null;
        if (d.layout && Array.isArray(d.layout.groups) && d.layout.groups.length) {
          $("#layoutCard").classList.add("hidden");
        } else if (d.layout) {
          $("#cols").value = d.layout.cols ?? 5;
          $("#aisle").value = d.layout.aisle_after ?? 2;
        }
        renderList();
      }
    } catch (e) {
      /* 忽略 */
    }

    $("#btnShot").addEventListener("click", async () => {
      const r = await fetch("/api/reference", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "capture" }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        alert("拍空镜失败：" + (d.error || r.status));
        return;
      }
      alert("已把当前画面存为空镜。请确保此刻房间是空的。");
      setTimeout(loadImage, 600);
    });

    $("#btnReload").addEventListener("click", () => {
      $("#msg").innerHTML = "正在取一帧画面…<br><span style=\"font-size:11px\">" +
        "（如果是摄像头模式，这一步会用到摄像头）</span>";
      $("#msg").style.display = "";
      loadImage();
    });
    $("#btnFinish").addEventListener("click", finishSeat);
    $("#btnUndo").addEventListener("click", () => {
      S.points.pop();
      redraw();
    });
    $("#btnClearAll").addEventListener("click", () => {
      if (!confirm("清空所有座位？")) return;
      S.seats = [];
      S.points = [];
      renderList();
      redraw();
    });
    $("#btnSave").addEventListener("click", save);
    $("#btnUpload").addEventListener("click", uploadVideo);

    document.addEventListener("keydown", (e) => {
      if (e.key === "Enter") finishSeat();
      else if (e.key === "Backspace") {
        e.preventDefault();
        S.points.pop();
        redraw();
      } else if (e.key === "Escape") {
        S.points = [];
        redraw();
      }
    });

    cv.style.display = "none"; // 没有画面时别让空画布占位
    // 刻意不自动调用 loadImage()：打开页面就抓一张摄像头画面会让人很不舒服。
    // 只有用户点「刷新画面」或上传视频后才取帧。
    renderList();
    scoresTimer = setInterval(pollScores, 2000);
  }

  boot();
})();
