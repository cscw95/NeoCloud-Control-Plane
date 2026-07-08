// 공용 네비 라이브 배지 — 미해결 티켓(🎫)·승인 대기(⏳)를 포털 버튼에 표시해
// 어느 화면에서든 처리할 일이 있는 포털이 즉시 눈에 띄게 한다.
(function () {
  async function jf(u) {
    const r = await fetch(u);
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }
  function setBadge(href, cls, text, title) {
    const a = document.querySelector(`.nc-nav a[href="${href}"]`);
    if (!a) return;
    let b = a.querySelector(`.${cls}`);
    if (!text) { if (b) b.remove(); return; }
    if (!b) {
      b = document.createElement("span");
      b.className = `nc-badge ${cls}`;
      a.appendChild(b);
    }
    b.textContent = text;
    b.title = title || "";
  }
  async function tick() {
    try {
      const [tickets, orders] = await Promise.all([
        jf("/api/v1/tickets"), jf("/api/v1/orders")]);
      const open = tickets.filter(t => t.status !== "resolved").length;
      const wait = orders.filter(o => o.approval_mode && o.pending_stage
                                 && !["delivered", "failed", "rejected",
                                      "closed"].includes(o.state)).length;
      setBadge("/ops", "nc-b-tkt", open ? `${open}` : "",
               `미해결 티켓 ${open}건`);
      setBadge("/ops", "nc-b-appr", wait ? `${wait}` : "",
               `승인 대기 주문 ${wait}건`);
      setBadge("/customer", "nc-b-tkt", open ? `${open}` : "",
               `진행 중 티켓 ${open}건`);
    } catch (e) { /* 서버 미기동 시 무시 */ }
  }
  // 말풍선(툴팁) On/Off 스위치 — 모든 말풍선(nvreq·modinfo·단계·prov) 일괄 제어
  function applyTips(on) {
    document.documentElement.dataset.tips = on ? "1" : "0";
    try { localStorage.setItem("nc-tips", on ? "1" : "0"); } catch (e) {}
    if (!on) document.dispatchEvent(new CustomEvent("nc-tips-off"));
  }
  const saved = (() => {
    try { return localStorage.getItem("nc-tips"); } catch (e) { return null; }
  })();
  applyTips(saved !== "0");
  const nav = document.querySelector(".nc-nav");
  if (nav) {
    const sw = document.createElement("label");
    sw.className = "nc-tipsw";
    sw.title = "기능 설명 말풍선 On/Off — NVIDIA Req 요구사항·Control-Plane 모듈 역할·"
      + "주문 파이프라인 단계·Provisioning 절차 안내를 항목 위에 올리면 표시합니다. "
      + "끄면 모든 화면에서 숨겨지며 설정은 유지됩니다.";
    sw.innerHTML = `말풍선 <input type="checkbox" ${saved !== "0" ? "checked" : ""}>`;
    sw.querySelector("input").addEventListener("change",
      e => applyTips(e.target.checked));
    (document.querySelector(".nc-right") || nav).appendChild(sw);
  }

  tick();
  setInterval(tick, 8000);
})();
