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

TRẢ LỜI (chi tiết ở reports/postmortem.md §5):
  interval × threshold = 5 × 3 = 15s là *detect floor* — không thể phát hiện nhanh hơn,
  và với --mode netblock còn phải cộng thêm `timeout` của lần probe cuối (2s) vì probe
  treo tới hết timeout mới tính là fail. Muốn RTO ≤ 300s thì detect floor phải chiếm
  một phần nhỏ của 300s; interval ≤ 30s (floor 90s) vẫn còn chỗ cho restore + warm-up
  + TTL. Hạ interval xuống 1s thì floor còn 3s nhưng đổi lại là traffic probe gấp 5 lần
  và rủi ro flapping khi region chỉ chậm chứ chưa chết.
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi.

    /readyz trả 200 = region serve được thật (pool full + hết warm-up + có weights +
    vector DB không rỗng). 503 = process sống nhưng CHƯA serve được -> vẫn tính là fail.
    """
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except Exception as e:
        # netblock -> ReadTimeout, stop -> ConnectError. Cả hai đều là fail.
        return False, f"probe_error:{type(e).__name__}"
    if r.status_code == 200:
        return True, "ready"
    try:
        reasons = ",".join(r.json().get("reasons") or []) or "not_ready"
    except Exception:
        reasons = "not_ready"
    return False, f"http_{r.status_code}:{reasons}"


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL.

    Giả định khởi điểm là HEALTHY và KHÔNG ghi log cho giả định đó: log chỉ chứa
    transition thật, nên dòng đầu tiên trong file luôn là một sự kiện có ý nghĩa.
    Ngưỡng dùng ĐỐI XỨNG cho cả 2 chiều (fail và hồi phục) — hồi phục cũng cần
    `threshold` lần liên tiếp, nếu không thì một lần trả lời may mắn của region đang
    hấp hối đủ để kéo traffic về (§4 Anti-Patterns: flapping 2 chiều).
    """
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    state = {r: "HEALTHY" for r in URL}          # giả định ban đầu, không ghi log
    streak = {r: {"fail": 0, "ok": 0} for r in URL}
    end = time.time() + duration

    with out.open("a") as f:
        def emit(**kw):
            rec = {"ts": time.time(),
                   "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                   "event": "state_change",
                   "interval_s": interval, "threshold": threshold, "timeout_s": timeout,
                   **kw}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            print("HEALTH", json.dumps(rec, ensure_ascii=False), flush=True)

        while time.time() < end:
            cycle = time.time()
            for region in URL:
                ok, reason = probe(region, timeout)
                s = streak[region]
                if ok:
                    s["ok"] += 1
                    s["fail"] = 0
                else:
                    s["fail"] += 1
                    s["ok"] = 0
                want = "UNHEALTHY" if s["fail"] >= threshold else (
                    "HEALTHY" if s["ok"] >= threshold else state[region])
                if want != state[region]:
                    state[region] = want
                    emit(region=region, to=want, reason=reason,
                         consecutive_fails=s["fail"], consecutive_ok=s["ok"])
            # Giữ ĐÚNG chu kỳ `interval`: probe netblock treo hết timeout, nếu sleep
            # nguyên interval thì chu kỳ thật = interval + timeout và detect floor phình ra.
            time.sleep(max(0.0, interval - (time.time() - cycle)))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
