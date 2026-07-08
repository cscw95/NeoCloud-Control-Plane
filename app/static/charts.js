/* NeoCloud OS — 공용 캔버스 라인차트 (외부 라이브러리 없음)
   ncChart(canvas, series, opts)
     series: [{label, color, data:[num,...]}]
     opts:   {h:높이px, unit:"%", min, max, fill:true, capLine:숫자(점선)} */
function ncChart(canvas, series, opts = {}) {
  const dpr = window.devicePixelRatio || 2;
  const w = canvas.clientWidth || 560, h = opts.h || 132;
  canvas.width = w * dpr; canvas.height = h * dpr;
  canvas.style.height = h + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const padL = 46, padR = 10, padT = 20, padB = 14;
  const ds = series.filter(s => s.data && s.data.length);
  ctx.font = "10px Menlo, monospace";
  if (!ds.length) {
    ctx.fillStyle = "#5a6b80";
    ctx.fillText("데이터 수집 중… (에뮬레이션 틱 대기)", padL, h / 2);
    return;
  }
  const all = ds.flatMap(s => s.data);
  if (opts.capLine !== undefined) all.push(opts.capLine);
  let min = opts.min !== undefined ? opts.min : Math.min(0, ...all);
  let max = opts.max !== undefined ? opts.max : Math.max(...all);
  if (max === min) max = min + 1;
  max += (max - min) * 0.08;
  const n = Math.max(...ds.map(s => s.data.length));
  const X = i => padL + (w - padL - padR) * (n <= 1 ? 1 : i / (n - 1));
  const Y = v => padT + (h - padT - padB) * (1 - (v - min) / (max - min));
  const fmt = v => Math.abs(v) >= 10000 ? (v / 1000).toFixed(1) + "k"
    : Math.abs(v) >= 100 ? Math.round(v).toString()
    : (Math.round(v * 10) / 10).toString();
  // 그리드 + Y라벨
  ctx.strokeStyle = "#233043"; ctx.fillStyle = "#5a6b80"; ctx.lineWidth = 1;
  for (let g = 0; g <= 3; g++) {
    const v = min + (max - min) * g / 3, y = Y(v);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillText(fmt(v), 4, y + 3);
  }
  // 캡 점선 (예: 전력 캡)
  if (opts.capLine !== undefined) {
    ctx.strokeStyle = "#e8c66a"; ctx.setLineDash([4, 4]);
    const y = Y(opts.capLine);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#e8c66a"; ctx.fillText("cap " + fmt(opts.capLine), w - padR - 62, y - 4);
  }
  // 시리즈
  ds.forEach((s, si) => {
    ctx.strokeStyle = s.color; ctx.lineWidth = 1.7; ctx.beginPath();
    s.data.forEach((v, i) => i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)));
    ctx.stroke();
    if (si === 0 && opts.fill !== false) {           // 첫 시리즈 소프트 필
      ctx.lineTo(X(s.data.length - 1), Y(min)); ctx.lineTo(X(0), Y(min));
      ctx.closePath(); ctx.fillStyle = s.color + "20"; ctx.fill();
    }
    const last = s.data[s.data.length - 1];          // 범례 + 현재값
    ctx.fillStyle = s.color; ctx.font = "10.5px -apple-system, sans-serif";
    ctx.fillText(`● ${s.label} ${fmt(last)}${opts.unit || ""}`,
                 padL + 4 + si * ((w - padL) / Math.min(ds.length, 3)), 12);
  });
}
