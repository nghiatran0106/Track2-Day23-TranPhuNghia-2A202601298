# RTO/RPO Evidence — Lab 23 (Trần Phú Nghĩa)

Drill chạy **bare mode** (`scripts/up_bare.sh`) + `chaos/kill_region.py --mock`, ngày
2026-08-25, mọi giờ ghi theo **UTC** (đúng như trường `iso` trong log).
Mọi con số dưới đây trỏ về một dòng log thật; kiểm tra lại bằng:

    python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300

Cấu hình drill: health check `--interval 5 --threshold 3 --timeout 2`,
`EDGE_TTL_SECONDS=5`, `WARMUP_SECONDS=6`, replication `--every 30 --backend fs`,
ingest `--rate 0.5` doc/giây.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T08:30:24` | chaos kill, mode `netblock` | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+2.02s` (ReadTimeout, 2026.2ms) | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:18` |
| Số request fail | 15 / 32 | mọi dòng `ok:false` sau t_outage | `reports/drill-1-nodr.jsonl` |
| Request thành công sau đó | không có | không tồn tại dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

Kết luận drill 1: không thành phần nào phát hiện outage, không ai đổi
`edge/active_region`, nên hệ thống **không bao giờ tự phục hồi** — mọi request trong
30.6 giây còn lại của bài test đều 503 cho tới khi tôi restore bằng tay.

Ghi chú về `+2.02s`: `tools/measure_rto.py` chỉ đếm request có `ts >= t_outage`, mà `ts` là
thời điểm request **bắt đầu**. Request đang bay lúc bị `SIGSTOP` khởi hành trước mốc 0
nên không được tính; sau đó mỗi request treo đủ 2 giây (timeout của edge) mới chết, làm
nhịp 2 req/s sập còn 0.5 req/s.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 — `08:33:36` | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.0s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +15.0s | `to:UNHEALTHY, region:a` sau 3 fail liên tiếp | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +15.3s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +21.9s | `step:4_wait_ready`, `waited_s:6.58` | `reports/failover-events.jsonl:4` |
| DNS cutover | +21.9s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+22.6s** | dòng `ok:true` đầu sau lỗi, `served_by:"b"` | `reports/drill-2-withdr.jsonl:36` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `22.6s` | 300s (5 phút) | **PASS** (dư 277.4s) |
| RPO — Vector DB | `4.0s` / `2` doc | 300s (5 phút) | **PASS** |

Tổng 11/166 request fail. `tools/measure_rto.py` trả `"valid": true` với `warnings` **rỗng** —
`reports/measure-drill-2.json`.

RPO lấy từ `step:2_restore_snapshot` (`rpo_seconds`, `docs_lost`) ở
`reports/failover-events.jsonl:2`. Chu kỳ replication cuối cùng trước lúc restore chạy
tại t0+12.8s và chỉ chụp được dữ liệu tới t0+10.8s (`reports/replication.jsonl:2`),
trong khi restore diễn ra ở t0+15.3s — lúc đó region A đã ingest tới t0+14.8s. Chênh
lệch đúng bằng `4.0s`, tương ứng `2` document ở tốc độ 0.5 doc/giây.
Con số này **dao động theo từng lần chạy** vì phụ thuộc chu kỳ `--every 30` rơi vào đâu
so với thời điểm restore; RTO thì ổn định hơn nhiều.

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.0s | `interval_s × threshold` = `5.0 × 3` = 15.0s, ghi ở `reports/health-events.jsonl:2` | Hạ `--interval` xuống 2s → sàn còn 6s; giá phải trả là probe traffic ×2.5 và nguy cơ flapping khi region chỉ chậm chứ chưa chết. Rẻ hơn: giữ `threshold=3` nhưng `--timeout 1` (probe netblock chết sớm hơn ~1s mỗi lần). |
| Snapshot restore | 0.33s | từ lúc health check phát hiện (+15.0s) tới lúc pool được lật (+15.3s): runbook xác nhận + thông báo incident + `1_verify_target` + restore. Riêng phần copy dữ liệu là `restore_seconds:0.01` ở `reports/failover-events.jsonl:2` | Gần như free vì snapshot chỉ 2MB trên local fs. Với vector index thật (chục GB qua S3 cross-region) đây sẽ là phần **lớn nhất** → chuyển sang warm standby: replicate liên tục vào region B thay vì restore lúc sự cố. |
| GPU pool warm-up | 6.58s | `waited_s:6.58` ở `step:4_wait_ready`, `reports/failover-events.jsonl:4` | Giữ region B ở pilot-light (một replica luôn `full`, weights nạp sẵn) → warm-up ≈ 0; giá phải trả là tiền GPU idle 24/7. |
| DNS/LB TTL cache | 0.68s | t_recovered − t_cutover = 22.55 − 21.88, `reports/drill-2-withdr.jsonl:36` | Hạ `EDGE_TTL_SECONDS`, hoặc dùng LB health-check-based (ALB/Envoy) thay vì DNS — LB không có TTL cache ở phía client. |
| **Tổng** | **22.6s** | = RTO đo được từ log | mục tiêu 300s |

Thành phần đắt nhất là **detect floor (15.0s ≈ 66% RTO)** — nó không đến từ code
failover mà đến từ *chính sách* chống flapping. Muốn RTO nhỏ hơn thì phải trả lời được
câu "bao nhiêu lần probe fail thì tôi dám tin là region đã chết", chứ không phải tối ưu
tốc độ copy file. Ba thành phần còn lại cộng lại chỉ 7.59s.

## 4. Tự kiểm chứng

| Kiểm tra | Cách kiểm | Kết quả |
|---|---|---|
| Drill 2 hợp lệ | `tools/measure_rto.py` | `"valid": true`, `"warnings": []` — `reports/measure-drill-2.json` |
| Recovery do region khác phục vụ | trường `served_by` | `"recovered_by_region": "b"` ≠ `"killed_region": "a"` |
| Cutover sau khi phát hiện | so `t_cutover` với `t_detect` | 21.9s > 15.0s → không dính cảnh báo "cutover trước khi phát hiện" |
| Không giết cả hai region | `chaos/chaos-events.jsonl` | mọi `action:kill` đều có `other_alive:true`, `forced_both:false` |
| Golden signals sau cutover | 10 request thật vào region B | p95 39.2ms, error rate 0.0 — `reports/runbook-run.jsonl:6` |
| Thứ tự 5 bước failover | log tuần tự | `1_verify_target → 2_restore_snapshot → 3_scale_pool → 4_wait_ready → 5_dns_cutover`, `reports/failover-events.jsonl` |
