"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except httpx.TimeoutException:
        return False, f"timeout>{timeout}s"
    except Exception as e:  # ConnectError khi process chết (mode stop), v.v.
        return False, type(e).__name__
    if r.status_code == 200:
        return True, "ready"
    try:
        reasons = r.json().get("reasons") or []
    except Exception:
        reasons = []
    return False, f"http_{r.status_code}" + (f":{','.join(reasons)}" if reasons else "")


def _write(f, rec: dict):
    f.write(json.dumps(rec) + "\n")
    f.flush()
    print("HEALTH", json.dumps(rec))


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL.

    Giả định ban đầu mỗi region HEALTHY. Chỉ đổi trạng thái khi có `threshold` kết quả
    NGƯỢC chiều LIÊN TIẾP (fail để xuống UNHEALTHY, ok để lên lại HEALTHY) — cả 2 chiều
    đều chống flapping. Chỉ ghi log lúc đổi trạng thái, không ghi mỗi lần poll.

    Sau mỗi vòng probe ngủ đủ `interval` giây: khi region treo (netblock) mỗi vòng dài
    thêm `timeout`, nên thời gian phát hiện luôn >= interval * threshold (detect floor).
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    state = {r: "HEALTHY" for r in URL}
    streak = {r: 0 for r in URL}       # số kết quả liên tiếp trái với state hiện tại
    last_reason = {r: "" for r in URL}
    end = time.time() + duration
    with out.open("a") as f:
        _write(f, {"ts": time.time(), "event": "checker_start", "regions": list(URL),
                   "interval_s": interval, "timeout_s": timeout, "threshold": threshold,
                   "detect_floor_s": round(interval * threshold, 1)})
        while time.time() < end:
            for region in URL:
                ok, reason = probe(region, timeout)
                last_reason[region] = reason
                bad = (state[region] == "HEALTHY" and not ok) or \
                      (state[region] == "UNHEALTHY" and ok)
                streak[region] = streak[region] + 1 if bad else 0
                if streak[region] >= threshold:
                    new = "UNHEALTHY" if state[region] == "HEALTHY" else "HEALTHY"
                    _write(f, {"ts": time.time(),
                               "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                               "event": "state_change", "region": region,
                               "from": state[region], "to": new, "reason": reason,
                               "consecutive_fails": streak[region] if not ok else 0,
                               "consecutive_ok": streak[region] if ok else 0,
                               "interval_s": interval, "timeout_s": timeout,
                               "threshold": threshold})
                    state[region], streak[region] = new, 0
            time.sleep(interval)
        _write(f, {"ts": time.time(), "event": "checker_stop", "final_state": state,
                   "last_reason": last_reason})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
