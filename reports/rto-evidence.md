# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.

Môi trường chạy: bare mode (`scripts/up_bare.sh`) trong WSL2 Ubuntu, Python 3.12,
chaos `--mode netblock --mock` (SIGSTOP). Ngày drill: 2026-10-09 (UTC).
Số liệu do `tools/measure_rto.py` tính từ log, không phải ước lượng.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T04:12:00` | chaos kill region-a, netblock | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.1 s (ReadTimeout, 2043.9 ms) | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | 16/16 request sau t_outage đều fail, tới request cuối (+30.8 s) | `reports/drill-1-nodr.jsonl:32` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl` | `reports/drill-1-nodr.jsonl:32` |
| Region A được restore | SIGCONT sau drill | `action:restore` | `chaos/chaos-events.jsonl:2` |

Tổng: 32 request, 16 fail. Không có health checker, không có failover → hệ thống không tự hồi phục.

## 2. Drill 2 — có DR

t_outage = `2026-10-09T04:13:04` (ts `1791519184.14`). Mọi cột "+giây" tính từ mốc này.

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.2 s | dòng `ok:false` đầu (ReadTimeout 2043.1 ms) | `reports/drill-2-withdr.jsonl:26` |
| Health check phát hiện | +19.3 s | `to:UNHEALTHY, region:a`, `consecutive_fails:3` | `reports/health-events.jsonl:3` |
| Runbook mở incident | +19.4 s | `step:2 thong_bao_incident` | reports/runbook-run.jsonl (không commit, xem §4) |
| Snapshot restore xong | +19.8 s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +26.2 s | `step:4_wait_ready`, `waited_s:6.45` | `reports/failover-events.jsonl:4` |
| DNS cutover | +26.3 s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+29.0 s** | dòng `ok:true` đầu sau lỗi, `served_by:b` | `reports/drill-2-withdr.jsonl:40` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 29.0 s | 300s (5 phút) | **PASS** (dùng 9.7% budget) |
| RPO — Vector DB | 6.0 s / 3 doc | 300s (5 phút) | **PASS** |

RPO lấy từ `reports/failover-events.jsonl:2`: primary có doc mới nhất ở ts `1791519202.66`,
bản restore chỉ có tới ts `1791519196.66` (snapshot thứ 2 của replication,
`reports/replication.jsonl:2`) → mất 6.0 s dữ liệu = 3 document.
`embed_model_version` được restore cùng: `embed-model=vi-e5-base@v3`.

## 3. RTO của tôi gồm những gì

Bốn thành phần, cộng lại đúng bằng RTO đo được: 19.3 + 0.5 + 6.4 + 2.8 = 29.0 s.

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 19.3 s (cấu hình 15.0 s) | `interval_s:5.0 × threshold:3` trong `reports/health-events.jsonl:3`. Thực tế dài hơn 15 s vì netblock làm mỗi probe fail treo đủ `timeout_s:2.0` (chu kỳ thành 7 s), cộng 3.3 s lệch pha giữa lúc kill và probe kế tiếp | Hạ `timeout` probe xuống 1 s, hoặc interval 2–3 s; giữ threshold 3 để chống flap |
| Snapshot restore (gồm runbook xác nhận + verify target) | 0.5 s | +19.3 → +19.8: alert → `2_restore_snapshot` → `3_scale_pool` (`reports/failover-events.jsonl:3`) | Đã nhỏ; với dữ liệu lớn: restore incremental, hoặc giữ replica ấm liên tục ở region B |
| GPU pool warm-up | 6.4 s | `waited_s:6.45` ở `4_wait_ready` (`reports/failover-events.jsonl:4`); WARMUP_SECONDS=6 của serving | Giữ region B ở `full` (hot standby) — tốn tiền GPU idle |
| DNS/LB TTL cache | 2.8 s | t_recovered − t_cutover = 29.0 − 26.3 (gồm 0.1 s ghi cutover). Request `reports/drill-2-withdr.jsonl:39` lúc +26.9 s vẫn bị edge cache gửi sang region-a | Hạ `EDGE_TTL_SECONDS`; hoặc edge tự loại upstream fail (health-aware LB) |

## 4. Ghi chú về evidence

- reports/runbook-run.jsonl bị `.gitignore` loại khỏi repo theo thiết kế của lab, nên
  các mốc lấy từ đó chỉ dùng để đối chiếu; mọi mốc được chấm điểm ở trên trỏ vào file
  có commit.
- Runbook bắt đầu failover **từ alert** của health checker (`health_alert_ts` trùng
  `reports/health-events.jsonl:3`), nên `t_cutover` (+26.3 s) luôn sau `t_detect`
  (+19.3 s) → `tools/measure_rto.py` trả `warnings: []`.
