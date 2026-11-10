"""F25 attestation demo — NOCP reads measured boot / TPM PCRs / DPU attestation from
NICo REST (GET /machine/{id}/attestation, measured_boot.proto report), a forced
measured-boot failure shows up, then is cleared. Self-contained; format follows
app/demo/engine.py. Deterministic: fixed machine, step 03 always clears."""

HOST = "nh-su-1-rack-00-tray-00"


def http(method, path, json=None, params=None):
    spec = {"method": method, "path": path, "as": "operator", "service": "nocp"}
    if json is not None:
        spec["json"] = json
    if params is not None:
        spec["params"] = params
    return spec


def _body(res):
    return res.body if isinstance(res.body, dict) else {}


def _attested(want):
    def expect(res, ctx):
        b = _body(res)
        ok = b.get("attested") is want and b.get("source") in ("nico", "fake-nico")
        return (ok, f"{HOST}: 증명 {b.get('attested')} · measured boot {b.get('measured_boot')} "
                    f"· PCR {len(b.get('tpm') or {})}개 · 출처 {b.get('source')}",
                f"{HOST}: attested {b.get('attested')} · measured boot {b.get('measured_boot')} "
                f"· {len(b.get('tpm') or {})} PCRs · source {b.get('source')}")
    return expect


def _applied(res, ctx):
    return (res.status == 200, f"NICo 주입 응답 HTTP {res.status}", f"NICo hook HTTP {res.status}")


SCENARIO = {
    "id": "f25-attestation",
    "order": 25,
    "feature": "F25",
    "title_ko": "F25 measured boot · TPM 증명을 NICo REST로",
    "title_en": "F25 measured boot · TPM attestation via NICo REST",
    "steps": [
        {"id": "00-clear", "title_ko": "초기화", "title_en": "Reset",
         "call": http("POST", "/api/v1/bringup/inject", {"kind": "clear", "machine": HOST}),
         "expect": _applied},
        {"id": "01-attested", "title_ko": "증명 조회 (정상)", "title_en": "Read attestation (healthy)",
         "call": http("GET", f"/fake-nico/hosts/{HOST}/attestation"), "expect": _attested(True)},
        {"id": "02-inject", "title_ko": "measured boot 실패 주입", "title_en": "Inject measured-boot failure",
         "call": http("POST", "/api/v1/bringup/inject", {"kind": "attestation_fail", "machine": HOST}),
         "expect": _applied},
        {"id": "03-failed", "title_ko": "증명 조회 (실패)", "title_en": "Read attestation (failed)",
         "call": http("GET", f"/fake-nico/hosts/{HOST}/attestation"), "expect": _attested(False)},
        {"id": "04-clear", "title_ko": "해제", "title_en": "Clear",
         "call": http("POST", "/api/v1/bringup/inject", {"kind": "clear", "machine": HOST}),
         "expect": _applied},
    ],
}
