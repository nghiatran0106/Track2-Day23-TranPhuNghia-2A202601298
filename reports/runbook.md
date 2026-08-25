# Runbook 1 trang — Region chính (A) down

**Điều kiện kích hoạt:** `dr/health_checker.py` ghi `to:UNHEALTHY, region:a` trong
`reports/health-events.jsonl` (3 lần probe `/readyz` fail liên tiếp, ≥15s) **và**
on-call xác nhận lại được bằng bước 1. Một lần probe fail KHÔNG kích hoạt runbook này.

**Chạy tất cả từ thư mục gốc repo.** Toàn bộ 7 bước đã được tự động hoá:
`python3 dr/runbook.py --primary a --target b --backend fs` (mặc định hỏi confirm ở
bước 2; `--auto` chỉ dùng cho CI/chấm điểm). Bảng dưới là bản chạy tay tương đương —
dùng khi automation không chạy được.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | `a.alive=false` (hoặc `a.ready=false`) 3 lần liên tiếp cách nhau 5s, **và** `b.alive=true`. Nếu B cũng chết → KHÔNG failover, escalate. | on-call SRE |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs` rồi trả lời `y` khi được hỏi | Có dòng `step:2 thong_bao_incident` kèm `notify_delay_s` trong `reports/runbook-run.jsonl` | on-call SRE (báo #incident-ai-infra, SEV1) |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py get --region b --backend fs` | Log `step:2_restore_snapshot` có `rpo_seconds` + `docs_lost` + `embed_model_version` khớp weights của B (`state/region-b/weights/VERSION`) | on-call SRE |
| 4 | Scale pool warm→full | `printf full > state/region-b/pool_state` | `curl -s localhost:8002/readyz` trả **200** (`reasons: []`). Chờ hết `WARMUP_SECONDS` (~6s); nếu >60s vẫn 503 → **ABORT, không sang bước 5** | on-call SRE |
| 5 | DNS/LB cutover | `printf b > edge/active_region` | `curl localhost:8080/edge/state` cho `active_region=b`; sau ≤ `ttl_seconds` thì `curl localhost:8080/v1/infer` trả `"region":"b"` | on-call SRE (có xác nhận của incident commander) |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -o /dev/null -w '%{http_code} %{time_total}\n' localhost:8002/v1/infer; done` | p95 < 200ms, error rate = 0 trên 10 request (bản chạy drill: p95 39.2ms, error rate 0.0) | on-call SRE |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `"valid": true` và `rto_verdict` = `PASS`/`FAIL` (≠ null); số này chép thẳng vào `reports/rto-evidence.md` | incident commander |

**Thứ tự là bắt buộc.** Đổi `edge/active_region` trước khi `/readyz` của B trả 200 =
user ăn 503 từ **cả hai** region, RTO dài ra chứ không ngắn lại. `dr/failover.py` tự
abort ở bước 4 nếu quá `--wait` giây mà B chưa ready.

**Rollback (failover ngược về region A):**

- **Điều kiện bắt buộc, đủ cả 3:** (1) `/readyz` của A trả 200 **liên tục ≥ 10 phút**
  (`python3 dr/health_checker.py --interval 5 --threshold 3` không ghi thêm dòng
  `UNHEALTHY` nào cho region a); (2) dữ liệu ingest trong lúc B làm primary đã được
  replicate ngược về A (`python3 state/snapshot.py put --region b` rồi
  `get --region a`) — nếu không sẽ mất toàn bộ ticket phát sinh trong sự cố;
  (3) đang trong giờ hành chính, không phải 3h sáng.
- **Ai quyết định:** incident commander (không phải on-call, không phải automation).
  Failback là một cutover thứ hai với đầy đủ rủi ro của cutover thứ nhất — nó phải là
  một quyết định có người ký, chạy lại đúng 7 bước trên với `--primary b --target a`.
- **Vì sao không tự động:** §4 Anti-Patterns — full-auto hai chiều mà không có circuit
  breaker thì một cú giật health check đủ làm traffic bay qua bay lại giữa 2 region,
  mỗi vòng lại mất thêm một lần warm-up + TTL. Vì vậy `dr/runbook.py` luôn hỏi confirm,
  và `--auto` chỉ tồn tại cho CI.
- **Nếu failback fail:** ở lại region B (nó đang phục vụ tốt), coi A là region phụ,
  mở incident mới.
