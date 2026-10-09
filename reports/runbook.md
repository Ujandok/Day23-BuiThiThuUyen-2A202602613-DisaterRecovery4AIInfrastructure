# Runbook 1 trang — Region chính (A) down → failover sang Region B

Người đọc: on-call lúc 3h sáng, không phải người viết code. Chạy mọi lệnh từ thư mục gốc
của repo (`cd <repo>`), stack bare mode (`bash scripts/up_bare.sh`). Trên Windows: chạy trong
WSL hoặc dùng `curl.exe` thay `curl` trong PowerShell.

**Trigger:** `reports/health-events.jsonl` có dòng `"region": "a", "to": "UNHEALTHY"`
(health checker cần 3 probe `/readyz` fail liên tiếp, interval 5s → sớm nhất ~15s sau outage).

**Đường tắt (tự động, có hỏi confirm):** `python3 dr/runbook.py --primary a --target b --backend fs`
chạy đủ 7 bước bên dưới và ghi log vào `reports/runbook-run.jsonl`. Chỉ thêm `--auto` khi drill/CI.
Nếu script lỗi giữa chừng, làm tay từ bước đang dở theo bảng.

| # | Bước | Lệnh (copy-paste) | Biết là xong khi | Ai làm | Nếu KHÔNG đạt |
|---|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` (chạy 3 lần, cách nhau ~5s) | 3/3 lần: `a.ready=false` **và** `b.alive=true` | on-call | `a.ready=true` lần nào → không phải outage, đóng alert. `b.alive=false` → **DỪNG**, double outage, escalate incident commander |
| 2 | Mở incident + bấm giờ RTO | Post kênh `#incident`: "SEV1 region-a down từ `<iso>` (lấy `iso` dòng kill/alert cuối)"; `tail -n 3 reports/health-events.jsonl` | Có ts mở incident; ghi chênh lệch ts alert − t_outage | on-call | — |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py lag --backend fs` rồi `python3 state/snapshot.py get --region b --backend fs` | Output có `embed_model_version` và `restored_at`; `curl -s localhost:8002/v1/state` cho `count>0`, `weights:true` | on-call | `khong tim thay MANIFEST.json` → chưa từng có snapshot: **DỪNG**, escalate (không có gì để failover) |
| 4 | Scale pool warm→full + chờ ready | `printf full > state/region-b/pool_state` rồi lặp `curl -s -o /dev/null -w '%{http_code}\n' localhost:8002/readyz` mỗi 1s | `/readyz` của b trả **200** (warm-up ~6s) | on-call | Sau 60s vẫn 503 → **KHÔNG cutover**, đọc `reasons` trong body, escalate |
| 5 | DNS/LB cutover | `printf b > edge/active_region` (KHÔNG dùng `echo`/`Out-File` — thêm newline/BOM) | `curl -s localhost:8080/edge/state` cho `"active_region":"b"` (chờ tối đa TTL 5s) | on-call | Edge vẫn trả `a` sau 10s → kiểm tra nội dung file (`xxd edge/active_region` phải đúng 1 byte `b`) |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -o /dev/null -w '%{http_code} %{time_total}\n' localhost:8080/v1/infer; done` | 10/10 trả 200 với `"region":"b"`; p95 < 500 ms, error rate = 0 | on-call | Error rate > 10% hoặc p95 > 500 ms trong 5 phút → báo incident commander, cân nhắc rollback |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `"valid": true`, `rto_verdict` = `PASS`; ghi số vào `reports/rto-evidence.md` | incident commander | `FAIL`/`NO_RECOVERY` → postmortem ưu tiên cao, action item bắt buộc |

## Rollback / failback về Region A

- **Rollback ngay trong incident** (trả traffic về A): chỉ khi region B lỗi nặng hơn A sau
  cutover (error rate > 10% kéo dài 5 phút) **và** A đã `ready` liên tục ≥ 3 lần probe.
  Người quyết định: **incident commander** (không phải on-call tự quyết, không tự động).
  Lệnh: `printf a > edge/active_region`.
- **Failback có kế hoạch** (khi A đã sửa xong): không làm lúc 3h sáng. Trong giờ làm việc,
  sau khi A healthy ≥ 30 phút: snapshot B → restore vào A (để không mất doc ingest vào B
  trong lúc failover), scale A `full`, chờ `/readyz` 200, rồi mới đổi `edge/active_region`.
  Người quyết định: incident commander + owner của vector DB.
- **Không bao giờ** bật failover 2 chiều tự động: theo §4 Anti-Patterns, full-auto không có
  circuit breaker làm 2 region flap qua lại. Health checker cũng cần 3 kết quả `ok`
  liên tiếp mới đánh dấu region `HEALTHY` lại.

## Khôi phục môi trường drill (sau khi diễn tập)

`python3 chaos/kill_region.py restore --region a --backend bare` → `curl -s localhost:8001/healthz`
phải trả `"alive": true`. Nếu dùng `--mode stop` (SIGKILL) thì chạy lại `bash scripts/up_bare.sh`.
