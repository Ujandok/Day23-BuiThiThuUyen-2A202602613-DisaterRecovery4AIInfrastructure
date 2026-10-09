# Postmortem — DR Drill Lab 23: Region A outage (netblock)

Blameless: câu hỏi là "hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

- **Ngày:** 2026-10-09 · **Severity:** SEV1 (diễn tập) · **Trạng thái:** resolved
- **Tóm tắt:** Region A bị netblock (SIGSTOP) lúc 04:13:04 UTC trong khi đang serve 2 req/s.
  Health checker phát hiện sau 19.3 s, runbook tự động failover sang Region B, user nhận
  request thành công đầu tiên từ B sau **29.0 s**. RPO tại thời điểm restore: **6.0 s / 3 doc**.
- **So với baseline (drill 1):** không có DR thì `NO_RECOVERY` — 16/16 request sau outage
  fail tới hết drill (`reports/drill-1-nodr.jsonl:32`).

## 1. Timeline (mọi dòng có evidence path:line)

| ISO time (UTC) | +s | Sự kiện | Evidence |
|---|---|---|---|
| 04:12:46 | −17.5 | Replication chu kỳ 1 (snapshot đầu tiên tồn tại) | `reports/replication.jsonl:1` |
| 04:13:02 | −2.0 | Health checker đánh dấu B `UNHEALTHY` (B rỗng — đúng thiết kế warm standby) | `reports/health-events.jsonl:2` |
| 04:13:04 | 0 | **Outage bắt đầu** — kill region-a, netblock | `chaos/chaos-events.jsonl:3` |
| 04:13:04 | +0.2 | User đầu tiên bị ảnh hưởng (ReadTimeout ~2 s ở edge) | `reports/drill-2-withdr.jsonl:26` |
| 04:13:16 | +12.5 | Snapshot dùng để restore được chụp (chu kỳ 2 — replication vẫn chạy vì ingest/replicate không bị kill) | `reports/replication.jsonl:2` |
| 04:13:23 | +19.3 | **Health check alert**: A `UNHEALTHY`, 3 lần `timeout>2.0s` liên tiếp | `reports/health-events.jsonl:3` |
| 04:13:23 | +19.5 | Operator (runbook `--auto`) confirm outage + mở incident → failover bước 1 `verify_target` | `reports/failover-events.jsonl:1` |
| 04:13:23 | +19.8 | Restore snapshot vào B xong (rpo 6.0 s, 3 doc) | `reports/failover-events.jsonl:2` |
| 04:13:30 | +26.2 | B ready sau warm-up 6.45 s | `reports/failover-events.jsonl:4` |
| 04:13:30 | +26.3 | DNS cutover `a → b` | `reports/failover-events.jsonl:5` |
| 04:13:31 | +26.9 | Request cuối vẫn bị edge cache gửi sang A (TTL) | `reports/drill-2-withdr.jsonl:39` |
| 04:13:33 | +29.0 | **Resolved** — request đầu tiên OK, `served_by: b` | `reports/drill-2-withdr.jsonl:40` |
| 04:13:44 | +40.7 | Health checker đánh dấu B `HEALTHY` (3 ok liên tiếp) | `reports/health-events.jsonl:4` |

Ghi chú: thời điểm runbook tự xác nhận (3 probe A đều timeout), độ trễ thông báo
(`notify_delay_s: 19.43`) và golden signals sau cutover (10/10 OK, p95 70.3 ms) nằm trong
reports/runbook-run.jsonl — file này bị `.gitignore` theo thiết kế lab nên không dùng làm evidence chấm điểm.

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300 s · đo được: **29.0 s** · gap: **−271.0 s** (đạt, dùng 9.7% budget)
- RPO mục tiêu: 300 s · đo được: **6.0 s** (**3** doc bị mất tại thời điểm restore) · gap: **−294.0 s**
- **Bước tốn nhiều giây nhất:** phát hiện bằng health check — **19.3 s = 66.6% RTO.**
  Vì sao: cấu hình `interval 5 s × threshold 3` cho floor lý thuyết 15 s, nhưng với
  netblock mỗi probe fail phải chờ đủ timeout 2 s, nên chu kỳ probe khi A treo là 7 s, cộng
  3.3 s lệch pha giữa lúc kill và probe kế tiếp: 3.3 + 2 × 7 + 2 = 19.3 s.
  Các phần còn lại: restore 0.5 s, GPU warm-up 6.4 s, DNS TTL cache 2.8 s
  (chi tiết: `reports/rto-evidence.md`).

**Gap không nằm trong con số:** RPO 6.0 s / 3 doc chỉ đúng *tại thời điểm restore*.
Pipeline ingest không nằm trong failover, nên vẫn ghi tiếp vào DB của region A sau outage:
khi kiểm tra DB sau drill, region A có **53 doc ingest sau cutover** mà region B không có
(B dừng ở doc của snapshot `reports/replication.jsonl:2`). Nếu A mất vĩnh viễn, mất mát thực
là 3 + 53 = 56 doc và còn tăng theo thời gian outage — đây là gap lớn nhất của drill này.

## 3. Root cause (5 whys)

Câu hỏi: *nếu đây là outage thật, bước nào trong runbook sẽ thất bại?*

1. **Vì sao user bị lỗi 29 s?** Vì edge vẫn gửi traffic sang A cho tới khi DNS cutover
   xong và cache TTL hết hạn; trước đó mọi request treo 2 s rồi 503.
2. **Vì sao cutover mất 26.3 s?** Vì failover chỉ bắt đầu sau alert của health checker
   (19.3 s), rồi phải chờ B warm-up 6.4 s — B là warm standby rỗng, không phải hot standby.
3. **Vì sao phát hiện mất 19.3 s?** Vì health checker chỉ có 1 tín hiệu (`/readyz` poll 5 s)
   và mỗi probe treo đủ timeout; không có tín hiệu nhanh hơn như tỉ lệ lỗi thật ở edge.
4. **Vì sao dữ liệu tiếp tục mất sau khi failover thành công?** Vì runbook chỉ chuyển
   *read path* (edge → serving); *write path* (ingest) được cấu hình cứng vào region A, không
   có ai/cái gì chuyển nó sang B, và không có alert nào khi ghi vào region đã bị đánh dấu chết.
5. **Vì sao không ai nhận ra?** Vì định nghĩa "khôi phục" của runbook và của
   `tools/measure_rto.py` chỉ là "request đọc đầu tiên OK"; không có golden signal nào cho
   write path, và RPO chỉ đo 1 lần lúc restore.

**Root cause:** kiến trúc DR coi "region" là lớp serving; state ingest không có cơ chế
chuyển vùng, nên failover thành công về RTO nhưng âm thầm tiếp tục mất dữ liệu.
Trong outage thật, bước 7 (post-incident) sẽ báo PASS trong khi khách hàng đang mất ticket.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Đưa ingest vào failover: ingest đọc region đích từ `edge/active_region` (hoặc ghi qua edge), runbook bước 5 verify write path | Platform/DR on-call lead | 2026-10-23 | RPO: chặn 53+ doc mất thêm sau cutover (mất mát dừng ở 6.0 s thay vì tăng theo thời gian outage) |
| 2 | Thêm golden signal cho write path vào runbook bước 6 (ghi 1 doc test vào B, đọc lại) và đo lại RPO sau khi failback | DR on-call lead | 2026-10-23 | Phát hiện gap RPO ngay trong incident thay vì sau postmortem |
| 3 | Hạ timeout probe 2 s → 1 s, interval 5 s → 3 s, giữ threshold 3 | SRE health-check owner | 2026-10-16 | RTO detect từ ~19.3 s xuống ~11 s (≈ −8 s) |
| 4 | Edge health-aware: tự bỏ cache khi upstream trả lỗi liên tiếp thay vì đợi hết TTL 5 s | Edge/network owner | 2026-10-30 | RTO −2 đến −3 s (phần TTL 2.8 s) |
| 5 | Hạ chu kỳ replication 30 s → 10 s (hoặc stream WAL/CDC) | Data platform owner | 2026-10-30 | RPO tối đa từ ~30 s xuống ~10 s |
| 6 | Đánh giá chi phí giữ B ở `full` (hot standby) cho giờ cao điểm | Eng manager + FinOps | 2026-11-06 | RTO −6.4 s (bỏ warm-up), đổi lấy chi phí GPU idle |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   5 s × 3 = **15 s** detect floor (cấu hình) = 51.7% của RTO 29.0 s. Thời gian phát hiện
   thực tế là 19.3 s = **66.6%** RTO, vì với netblock mỗi probe fail treo thêm timeout 2 s.
   Với netblock, floor đúng phải tính là `(interval + timeout) × (threshold − 1) + timeout` = 16 s.
   Muốn RTO 5 phút thì về lý thuyết interval có thể lên tới ~90 s (90 × 3 = 270 s, còn ~30 s
   cho restore + warm-up + TTL), nhưng như vậy là phí gần hết budget vào việc chờ.

2. **Nếu hạ interval xuống 1 s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   Phát hiện còn khoảng (0..1) + 2 × (1 + 2) + 2 ≈ 8–9 s thay vì 19.3 s → RTO giảm ~10 s
   (còn ~19 s). Cái giá: 3 lần fail liên tiếp chỉ còn trải dài ~6–9 s, nên một GC pause,
   một đợt deploy, hay vài request chậm > 2 s là đủ để kích hoạt failover giả. Mỗi lần failover
   giả tốn ~6.4 s warm-up, đổi DNS, và **mất dữ liệu thật** (docs_lost) vì restore từ snapshot cũ;
   nếu failback cũng tự động thì 2 region flap qua lại. Ngoài ra tải probe tăng 5 lần.
   Vì vậy action item #3 chỉ hạ xuống 3 s và giữ threshold 3, và failover vẫn bán tự động.

3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   `docs_lost = 3` nghĩa là 3 ticket khách hàng gửi trong 6 giây cuối trước restore sẽ
   không bao giờ được tìm thấy: hệ thống RAG trả lời như thể chúng chưa từng tồn tại, và khách
   phải gửi lại. Nhưng con số đó **chưa phải toàn bộ thiệt hại**: vì ingest vẫn ghi vào region A,
   ở tốc độ 0.5 doc/s, 6 giờ outage = ~10,800 doc được "nhận" (client thấy ghi thành công) rồi
   mất vĩnh viễn cùng region A. Với khách hàng, đó là mất dữ liệu đã được xác nhận lưu, nặng hơn
   nhiều so với downtime 29 s; vì thế action item #1 là ưu tiên cao nhất.
