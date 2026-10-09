"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


ACTIVE = pathlib.Path("edge/active_region")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec))
    return rec


def _abort(t0: float, result: dict, step: str, why: str) -> dict:
    emit(step=step, ok=False, aborted=True, error=why, note="ABORT -> KHONG cutover")
    result.update(ok=False, aborted_at=step, error=why, elapsed_s=round(time.time() - t0, 2))
    return result


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước ở trên, đúng thứ tự. Bước nào hỏng -> abort, edge/active_region giữ nguyên."""
    t0 = time.time()
    primary = "a" if target == "b" else "b"
    result = {"ok": False, "target": target, "primary": primary, "backend": backend,
              "steps": []}

    # 1. verify_target: region phụ phải còn API để nhận state. Thiếu weights/vector là
    #    bình thường (warm standby rỗng) — bước 2 sẽ lấp vào.
    try:
        before = httpx.get(f"{URL[target]}/v1/state", timeout=2.0).json()
    except Exception as e:
        return _abort(t0, result, "1_verify_target",
                      f"region-{target} khong phan hoi /v1/state: {type(e).__name__}")
    emit(step="1_verify_target", ok=True, target=target, pool_state=before.get("pool_state"),
         weights=before.get("weights"), vector_count=before.get("count"))
    result["steps"].append("1_verify_target")
    result["target_state_before"] = before

    # 2. restore_snapshot + đo RPO thật (primary có mà bản restore không có).
    try:
        meta = snapshot.get(target, backend)
    except (Exception, SystemExit) as e:  # snapshot.get raise SystemExit khi chưa có put nào
        return _abort(t0, result, "2_restore_snapshot", f"restore that bai: {e}")
    r = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                     pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    emit(step="2_restore_snapshot", ok=True, backend=backend,
         snapshot_at=meta.get("snapshot_at"), rpo_seconds=r["rpo_seconds"],
         docs_lost=r["docs_lost"], embed_model_version=meta.get("embed_model_version"),
         primary_latest_doc_ts=r["primary_latest_doc_ts"],
         restored_latest_doc_ts=r["restored_latest_doc_ts"])
    result["steps"].append("2_restore_snapshot")
    result.update(rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
                  embed_model_version=meta.get("embed_model_version"))

    # 3. scale_pool warm -> full. Warm-up của GPU pool bắt đầu tính từ đây.
    pool = pathlib.Path(f"state/region-{target}/pool_state")
    prev_pool = pool.read_text().strip() if pool.exists() else "cold"
    pool.write_text("full")
    emit(step="3_scale_pool", ok=True, target=target, pool_from=prev_pool, pool_to="full")
    result["steps"].append("3_scale_pool")

    # 4. wait_ready: poll /readyz tới 200. Timeout -> ABORT, KHÔNG cutover.
    t_wait, last = time.time(), "chua_probe"
    while True:
        try:
            resp = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            if resp.status_code == 200:
                break
            last = ",".join(resp.json().get("reasons") or []) or f"http_{resp.status_code}"
        except Exception as e:
            last = type(e).__name__
        if time.time() - t_wait >= wait:
            return _abort(t0, result, "4_wait_ready",
                          f"region-{target} chua ready sau {wait}s (last={last})")
        time.sleep(0.5)
    waited = round(time.time() - t_wait, 2)
    emit(step="4_wait_ready", ok=True, target=target, waited_s=waited)
    result["steps"].append("4_wait_ready")
    result["waited_s"] = waited

    # 5. dns_cutover: chỉ tới đây mới đổi "DNS record". Ghi không newline, không BOM.
    prev_active = ACTIVE.read_text().strip() if ACTIVE.exists() else None
    ACTIVE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE.write_text(target, encoding="utf-8")
    emit(step="5_dns_cutover", ok=True, active_from=prev_active, active_to=target)
    result["steps"].append("5_dns_cutover")

    try:
        result["target_state_after"] = httpx.get(f"{URL[target]}/v1/state", timeout=2.0).json()
    except Exception:
        result["target_state_after"] = None
    result.update(ok=True, elapsed_s=round(time.time() - t0, 2))
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
