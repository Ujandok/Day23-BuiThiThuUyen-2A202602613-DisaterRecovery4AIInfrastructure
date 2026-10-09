"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


HEALTH_LOG = pathlib.Path("reports/health-events.jsonl")
CHAOS_LOG = pathlib.Path("chaos/chaos-events.jsonl")
ACTIVE = pathlib.Path("edge/active_region")


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:  # không có stdin (chạy nền) -> coi như KHÔNG đồng ý
        return False


def _jsonl(p: pathlib.Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _last_kill(region: str) -> dict | None:
    kills = [e for e in _jsonl(CHAOS_LOG) if e.get("action") == "kill" and e.get("region") == region]
    return kills[-1] if kills else None


def _alert(region: str, since: float) -> dict | None:
    """Alert = state_change UNHEALTHY từ health checker (process riêng) cho region này."""
    ev = [e for e in _jsonl(HEALTH_LOG) if e.get("event") == "state_change"
          and e.get("region") == region and e.get("to") == "UNHEALTHY" and e["ts"] >= since]
    return ev[0] if ev else None


def _alive(region: str) -> bool:
    try:
        return httpx.get(f"{URL[region]}/healthz", timeout=2.0).status_code == 200
    except Exception:
        return False


def run(primary: str, target: str, backend: str, auto: bool,
        probes: int = 3, probe_gap: float = 2.0, alert_wait: float = 60.0) -> dict:
    """7 bước ở trên. Trả về dict tóm tắt; mọi bước đều có 1 dòng trong LOG."""
    t_start = time.time()
    kill = _last_kill(primary)
    t_outage = kill["ts"] if kill else None

    # 1. Xác nhận outage: (a) tự probe primary nhiều lần — 1 lần fail không phải outage;
    #    (b) chờ alert của health checker. Runbook bắt đầu TỪ alert, không đi trước nó.
    results = []
    for i in range(probes):
        results.append(hc.probe(primary, 2.0))
        if i < probes - 1:
            time.sleep(probe_gap)
    target_alive = _alive(target)
    primary_down = not any(ok for ok, _ in results)
    alert = _alert(primary, since=(t_outage or t_start) - 1)
    t_wait = time.time()
    while primary_down and alert is None and time.time() - t_wait < alert_wait:
        time.sleep(0.5)
        alert = _alert(primary, since=(t_outage or t_start) - 1)
    step(1, "xac_nhan_outage", primary=primary, target=target,
         primary_probes=[{"ok": ok, "reason": r} for ok, r in results],
         primary_down=primary_down, target_alive=target_alive,
         health_alert_ts=alert["ts"] if alert else None,
         health_alert_reason=alert.get("reason") if alert else None)
    if not primary_down:
        step(7, "post_incident", outcome="aborted_no_outage",
             note=f"region-{primary} van ready trong {probes} probe -> KHONG failover")
        return {"ok": False, "aborted_at": "xac_nhan_outage", "reason": "primary_still_ready"}
    if not target_alive:
        step(7, "post_incident", outcome="aborted_target_down",
             note=f"region-{target} khong song -> failover se la double outage, escalate")
        return {"ok": False, "aborted_at": "xac_nhan_outage", "reason": "target_not_alive"}

    # 2. Thông báo incident — mốc "operator biết tin", luôn SAU t_outage.
    t_incident = time.time()
    step(2, "thong_bao_incident", severity="SEV1",
         msg=f"region-{primary} down, chuan bi failover sang region-{target}",
         t_outage=t_outage, t_outage_iso=kill.get("iso") if kill else None,
         t_alert=alert["ts"] if alert else None, t_incident=t_incident,
         notify_delay_s=round(t_incident - t_outage, 2) if t_outage else None,
         alert_delay_s=round(alert["ts"] - t_outage, 2) if (alert and t_outage) else None)

    # 3. Scale GPU pool + toàn bộ failover: gọi failover() MỘT LẦN DUY NHẤT.
    if not confirm(auto, f"Failover region-{primary} -> region-{target}?"):
        step(3, "scale_gpu_pool", ok=False, confirmed=False, note="operator tu choi")
        step(7, "post_incident", outcome="aborted_by_operator",
             elapsed_s=round(time.time() - t_incident, 2))
        return {"ok": False, "aborted_at": "scale_gpu_pool", "reason": "not_confirmed"}
    res = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", ok=res.get("ok"), confirmed=True, auto=auto,
         failover_steps=res.get("steps"), waited_s=res.get("waited_s"),
         failover_elapsed_s=res.get("elapsed_s"), aborted_at=res.get("aborted_at"),
         error=res.get("error"))

    # 4. Verify state replica — chỉ ĐỌC kết quả bước 3, không gọi lại failover.
    after = res.get("target_state_after") or {}
    step(4, "verify_state_replica", vector_count=after.get("count"),
         weights=after.get("weights"), pool_state=after.get("pool_state"),
         rpo_seconds=res.get("rpo_seconds"), docs_lost=res.get("docs_lost"),
         embed_model_version=res.get("embed_model_version"))

    # 5. DNS cutover — chỉ đọc lại kết quả.
    active = ACTIVE.read_text().strip() if ACTIVE.exists() else None
    cut_ok = bool(res.get("ok")) and active == target
    step(5, "dns_cutover", ok=cut_ok, active_region=active,
         note="edge cache TTL ~5s truoc khi traffic thuc su sang region moi")
    if not cut_ok:
        step(7, "post_incident", outcome="failover_failed", error=res.get("error"),
             elapsed_s=round(time.time() - t_incident, 2),
             rollback="giu nguyen active_region, escalate on-call lead")
        return {"ok": False, "aborted_at": res.get("aborted_at"), "failover": res}

    # 6. Golden signals: 10 request thật vào region phụ.
    lat, errors = [], 0
    with httpx.Client(timeout=3.0) as c:
        for i in range(10):
            t = time.time()
            try:
                r = c.get(f"{URL[target]}/v1/infer", params={"q": f"golden {i}"})
                if r.status_code != 200 or r.json().get("region") != target:
                    errors += 1
            except Exception:
                errors += 1
            lat.append((time.time() - t) * 1000)
    lat.sort()
    p95 = round(lat[min(len(lat) - 1, int(round(0.95 * len(lat))) - 1)], 1)
    err_rate = errors / len(lat)
    step(6, "verify_golden_signals", requests=len(lat), errors=errors, error_rate=err_rate,
         p50_ms=round(lat[len(lat) // 2], 1), p95_ms=p95,
         healthy=err_rate == 0 and p95 < 500)

    # 7. Post-incident.
    elapsed = round(time.time() - t_incident, 2)
    step(7, "post_incident", outcome="failover_ok", elapsed_s=elapsed,
         since_outage_s=round(time.time() - t_outage, 2) if t_outage else None,
         measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300")
    return {"ok": True, "primary": primary, "target": target, "elapsed_s": elapsed,
            "rpo_seconds": res.get("rpo_seconds"), "docs_lost": res.get("docs_lost"),
            "golden": {"p95_ms": p95, "error_rate": err_rate}, "failover": res}



if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
