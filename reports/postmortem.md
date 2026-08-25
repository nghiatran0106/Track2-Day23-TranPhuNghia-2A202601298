# Postmortem — DR Drill Lab 23 (region A down, 2026-08-25)

**Loại:** game day có kế hoạch (§6 Chaos Engineering), không phải sự cố thật.
**Severity giả định:** SEV1 — inference API trả 503 cho 100% traffic đi qua edge.
Blameless: câu hỏi là "hệ thống/process nào cho phép chuyện này xảy ra", không phải
"ai làm sai".

## 1. Timeline (giờ UTC, mỗi dòng đều có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 08:33:34 | health check đã gắn cờ region B `UNHEALTHY` **trước cả sự cố** — `vector_db_empty(count=0)`, tức là region phụ vốn không serve được | `reports/health-events.jsonl:1` |
| 08:33:36 | outage bắt đầu — `SIGSTOP` region A (`mode:netblock`), mốc 0 của đồng hồ RTO | `chaos/chaos-events.jsonl:3` |
| 08:33:36 | user đầu tiên bị ảnh hưởng — request qua edge treo tới timeout rồi 503 (`ReadTimeout`) | `reports/drill-2-withdr.jsonl:25` |
| 08:33:51 | health check alert — `region:a → UNHEALTHY` sau 3 probe `/readyz` fail liên tiếp (+15.0s) | `reports/health-events.jsonl:2` |
| 08:33:51 | operator xác nhận outage + mở incident SEV1 (`notify_delay_s:15.24`), rồi confirm cutover | `reports/runbook-run.jsonl:2` |
| 08:33:51 | restore snapshot vào region B xong — `rpo_seconds:4.0`, `docs_lost:2` | `reports/failover-events.jsonl:2` |
| 08:33:58 | region B `/readyz` trả 200 sau `waited_s:6.58` (GPU pool warm-up) → DNS cutover | `reports/failover-events.jsonl:5` |
| 08:33:58 | **resolved** — request đầu tiên OK, `served_by:"b"` (+22.6s) | `reports/drill-2-withdr.jsonl:36` |
| 08:34:11 | health check xác nhận region B `HEALTHY` (3 probe thành công liên tiếp) | `reports/health-events.jsonl:3` |

Tổng thiệt hại: 11 request fail trên 166 request của drill (`reports/measure-drill-2.json`).

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `22.6s` · gap: `-277.4s` (thấp hơn mục tiêu 277.4s → **PASS**)
- RPO mục tiêu: 300s · đo được: `4.0s` (`2` doc bị mất) · gap: `-296.0s` (**PASS**)
- **Bước tốn nhiều giây nhất:** `health-check detect floor` — 15.0s, chiếm **66%** RTO.
  Vì sao: `interval_s × threshold = 5 × 3 = 15s` là sàn cứng, và với `mode:netblock`
  lần probe cuối còn phải chờ hết `timeout` 2s mới được tính là fail (request treo chứ
  không fail nhanh như `mode:stop`). Đây là *chính sách chống flapping*, không phải chỗ
  code chạy chậm.
- Thứ nhì: **GPU pool warm-up 6.58s** (`reports/failover-events.jsonl:4`).
- Hai thành phần còn lại gần như free trong lab và **sẽ không free ở production**:
  snapshot restore 0.33s (2MB trên local fs — index thật vài chục GB qua S3
  cross-region là chuyện của phút, không phải mili-giây) và DNS TTL 0.68s
  (`EDGE_TTL_SECONDS=5`; resolver thật thường xuyên không tôn trọng TTL).

Baseline để so sánh: cùng cuộc tấn công đó ở drill 1 cho `NO_RECOVERY` — 15/32 request
fail và không một request nào phục hồi (`reports/measure-drill-1.json`). Toàn bộ 22.6
giây này là thứ ta mua được bằng ba file trong `dr/`.

## 3. Root cause (5 whys)

Câu hỏi không phải "vì tôi chạy chaos script", mà "*nếu đây là outage thật, bước nào
trong runbook của tôi sẽ thất bại?*"

1. **Vì sao user thấy 503?** Vì edge vẫn trỏ `active_region=a` trong khi A không trả lời.
2. **Vì sao edge vẫn trỏ A?** Vì edge chỉ đọc một file text — nó không có health check
   riêng, không tự loại upstream chết khỏi pool.
3. **Vì sao không ai đổi file đó sớm hơn?** Vì phần phát hiện nằm ở một tiến trình riêng
   và có sàn 15s do `interval × threshold`; trước mốc đó không ai được phép kết luận A đã chết.
4. **Vì sao vẫn phải chờ thêm 6.58s sau khi restore?** Vì region B chạy ở
   `pool_state=warm`, chưa nạp model weights → phải warm-up khi chuyển sang `full`.
   Đây là đánh đổi chi phí: standby lạnh thì rẻ, nhưng RTO trả bằng giây.
5. **Vì sao B khởi đầu rỗng hoàn toàn (`count:0`, `weights:false` — ghi rõ ở
   `reports/failover-events.jsonl:1`)?** Vì replication là job định kỳ đẩy snapshot lên
   object store, không phải replica sống. Nếu `state/replicate.py` chưa từng chạy thì
   `2_restore_snapshot` **chết ngay** và RTO là vô hạn — đây chính là bước dễ thất bại
   nhất trong một outage thật: *runbook giả định một backup mà chưa ai kiểm tra là
   restore được*.

**Root cause thật sự:** kiến trúc active–passive với state ở dạng snapshot định kỳ. Nó
rẻ, nhưng đặt cả detect floor, warm-up và restore vào đường tới hạn của RTO. Chaos
script chỉ là cái chạm vào; nếu là mất AZ thật thì đúng ba con số này vẫn xuất hiện.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Hạ `--interval` xuống 2s, giữ `threshold=3`, và `--timeout 1` → detect floor 6s + 1s | on-call SRE (Nghĩa) | 2026-09-01 | RTO −8s (15.0 → ~7) |
| 2 | Giữ region B ở pilot-light: một replica luôn `pool_state=full`, weights nạp sẵn trên đĩa | Platform/GPU owner | 2026-09-08 | RTO −6.6s (bỏ warm-up); chi phí +1 GPU idle |
| 3 | Hạ chu kỳ `state/replicate.py --every` từ 30s xuống 10s cho vector DB nóng | Data/Infra owner | 2026-09-08 | RPO worst-case 30s → 10s (docs_lost ~15 → ~5) |
| 4 | Thay edge file-based bằng LB có active health check (tự loại upstream 503), bỏ TTL cache khỏi đường tới hạn | Platform owner | 2026-09-15 | RTO −0.7s ở lab, hàng chục giây ở production |
| 5 | Game day hàng tháng: chạy lại đúng drill này, randomize `stop`/`netblock` + thời điểm kill, báo mean/stddev RTO | Incident commander | 2026-09-30 | 0s trực tiếp — nhưng là thứ duy nhất chứng minh 4 action trên còn đúng |
| 6 | Restore-drill cho backup: mỗi tuần restore snapshot vào region trống, verify vector count + `embed_model_version` | Data/Infra owner | 2026-09-30 | chặn kịch bản RTO = ∞ do backup không restore được |

## 5. Ba câu hỏi bắt buộc trả lời

**1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
5.0s × 3 = **15.0s** detect floor (`reports/health-events.jsonl:2`). Thực tế phát hiện
ở +14.97s, chiếm **66%** của RTO 22.6s. Nói cách khác: hai phần ba thời gian downtime
trôi qua *trước khi* một dòng code failover nào được chạy.

**2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì?**
Sàn còn 1 × 3 = 3s → RTO tụt xuống khoảng **10.6s** (giảm ~12s; vẫn còn warm-up 6.58s +
restore + TTL). Giá phải trả: (a) probe traffic gấp 5 lần lên `/readyz` — endpoint này
đọc SQLite và stat file, không miễn phí khi có hàng trăm replica; (b) mất đệm chống
flapping: một GC pause hay một spike latency 3 giây liên tiếp cũng đủ bị coi là outage,
kéo theo cutover + warm-up + TTL cho một region *vẫn còn sống*, và nếu có failback tự
động thì traffic bay qua bay lại (§4 Anti-Patterns). Lưu ý kỹ thuật: với `mode:netblock`,
`timeout` mới là chặn dưới thật sự — `interval=1s` mà `timeout=2s` thì mỗi probe vẫn mất
2s, sàn thực ≈ 3 × 2 = 6s chứ không phải 3s.

**3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của
bạn có nghĩa gì với khách hàng?**
Trong drill này `docs_lost = 2` và `rpo_seconds = 4.0` — nghe như không có gì. Nhưng con
số đó chỉ đo khoảng cách từ snapshot cuối (`reports/replication.jsonl:2`, chụp tới
t0+10.8s) đến lúc restore (t0+15.3s); nó **không** phải cam kết. Với `--every 30`, worst
case là mất 30s ingest ≈ 15 ticket ở tốc độ 0.5 doc/s. Nếu region A mất vĩnh viễn thì đó
là 15 ticket khách hàng **đã gửi và đã nhận xác nhận** nhưng không hệ thống nào còn giữ:
không tìm được bằng search, không trả lời được, và không có cách nào biết đã mất cái gì
trừ khi có log ingest riêng. Đó là lý do RPO phải báo cáo bằng **cả hai** đơn vị —
"4 giây" là ngôn ngữ của kỹ sư, "15 ticket không ai trả lời" mới là ngôn ngữ của khách hàng.
