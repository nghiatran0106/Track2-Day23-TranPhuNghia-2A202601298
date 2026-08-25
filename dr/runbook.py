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
EDGE = "http://127.0.0.1:8080"
CHAOS = pathlib.Path("chaos/chaos-events.jsonl")
HEALTH = pathlib.Path("reports/health-events.jsonl")


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(),
           "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[runbook {n}/7] {name} " + json.dumps(kw, ensure_ascii=False, default=str),
          flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N.

    KHÔNG bỏ hàm này đi: full-auto failover không có circuit breaker là anti-pattern
    §4 — health check giật một nhịp là traffic bay qua bay lại giữa 2 region.
    """
    if auto:
        print(f"[--auto] {msg} -> YES (khong hoi nguoi that; chi dung cho CI/cham diem)")
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        # Khong co ai o dau kia terminal -> mac dinh KHONG lam gi.
        print("khong doc duoc tra loi -> coi nhu N")
        return False


def _jsonl(p) -> list[dict]:
    p = pathlib.Path(p)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def last_kill(region: str) -> dict | None:
    """Sự kiện kill gần nhất của region này = mốc 0 của đồng hồ RTO."""
    kills = [e for e in _jsonl(CHAOS)
             if e.get("action") == "kill" and e.get("region") == region]
    return kills[-1] if kills else None


def health_alert(region: str, since: float) -> dict | None:
    """Dòng UNHEALTHY đầu tiên mà health_checker ghi cho region này sau mốc `since`."""
    return next((e for e in _jsonl(HEALTH)
                 if e.get("event") == "state_change" and e.get("to") == "UNHEALTHY"
                 and e.get("region") == region and e.get("ts", 0) >= since), None)


def run(primary: str, target: str, backend: str, auto: bool,
        confirm_interval: float = 5.0, confirm_threshold: int = 3,
        probe_timeout: float = 2.0, alert_wait: float = 60.0,
        ready_wait: float = 60.0, golden_n: int = 10) -> dict:
    """7 bước runbook. Trả dict tóm tắt để postmortem trích số."""
    t_run = time.time()
    kill = last_kill(primary)
    t_outage = kill["ts"] if kill else None

    # === 1. Xác nhận outage — KHÔNG tin 1 lần fail ==============================
    fails, probes = 0, []
    deadline = time.time() + confirm_interval * confirm_threshold * 3 + 20
    while time.time() < deadline:
        t = time.time()
        ok, reason = hc.probe(primary, probe_timeout)
        probes.append({"ts": round(t, 3), "ok": ok, "reason": reason})
        fails = 0 if ok else fails + 1
        if fails >= confirm_threshold:
            break
        time.sleep(max(0.0, confirm_interval - (time.time() - t)))
    confirmed = fails >= confirm_threshold
    target_ok, target_reason = hc.probe(target, probe_timeout)
    target_alive = False
    try:
        target_alive = httpx.get(f"{URL[target]}/healthz", timeout=probe_timeout).status_code == 200
    except Exception:
        pass

    # Cho tin cua health checker: cutover PHAI dien ra SAU khi automation phat hien,
    # neu khong thi con so do duoc la do tay nguoi bam nhanh, khong phai do he thong
    # (tools/measure_rto.py canh bao dung truong hop t_cutover < t_detect).
    alert, adl = None, time.time() + alert_wait
    while time.time() < adl:
        alert = health_alert(primary, t_outage or t_run)
        if alert:
            break
        time.sleep(1.0)

    step(1, "xac_nhan_outage", primary=primary, outage_confirmed=confirmed,
         consecutive_fails=fails, probe_count=len(probes), probes=probes[-6:],
         t_outage=t_outage, t_outage_iso=(kill or {}).get("iso"),
         chaos_mode=(kill or {}).get("mode"),
         target=target, target_alive=target_alive, target_ready=target_ok,
         target_reason=target_reason,
         health_alert_ts=(alert or {}).get("ts"),
         health_alert_iso=(alert or {}).get("iso"),
         health_alert_after_outage_s=(None if not (alert and t_outage)
                                      else round(alert["ts"] - t_outage, 2)),
         detect_floor_s=(None if not alert else
                         round(alert.get("interval_s", 0) * alert.get("threshold", 0), 1)),
         note=("da co alert cua health_checker" if alert else
               "KHONG thay alert nao trong health-events.jsonl -> health checker chua chay"))

    if not confirmed:
        step(7, "post_incident", aborted=True,
             reason=f"region-{primary} van tra loi /readyz -> khong phai outage, khong failover",
             elapsed_s=round(time.time() - t_run, 2))
        return {"ok": False, "aborted_at": "1_xac_nhan_outage"}
    if not target_alive:
        step(7, "post_incident", aborted=True,
             reason=f"region-{target} cung khong song -> failover se tao double outage",
             elapsed_s=round(time.time() - t_run, 2))
        return {"ok": False, "aborted_at": "1_xac_nhan_outage"}

    # === 2. Mở incident + bấm giờ ==============================================
    t_ann = time.time()
    step(2, "thong_bao_incident", severity="SEV1", channel="#incident-ai-infra",
         summary=f"region-{primary} down, inference API tra 503 qua edge",
         t_outage=t_outage, t_announced=t_ann,
         notify_delay_s=None if not t_outage else round(t_ann - t_outage, 2),
         note="dong ho RTO tinh tu t_outage, KHONG tinh tu luc operator biet tin")

    ok = confirm(auto, f"Cutover traffic tu region-{primary} sang region-{target}?")
    if not ok:
        step(7, "post_incident", aborted=True, operator_confirmed=False,
             reason="operator tu choi cutover", elapsed_s=round(time.time() - t_run, 2))
        return {"ok": False, "aborted_at": "2_thong_bao_incident"}

    # === 3. Failover — GỌI ĐÚNG MỘT LẦN ========================================
    res = fo.failover(target, backend, ready_wait)
    step(3, "scale_gpu_pool", called="dr.failover.failover", target=target,
         ok=res.get("ok"), aborted_at=res.get("aborted_at"),
         waited_ready_s=res.get("waited_ready_s"), elapsed_s=res.get("elapsed_s"),
         operator_confirmed=True,
         note="5 buoc con nam o reports/failover-events.jsonl")

    # === 4. Verify state replica — CHỈ ĐỌC LẠI kết quả bước 3 ==================
    sa = res.get("state_after") or {}
    step(4, "verify_state_replica", region=target,
         vector_count=sa.get("count"), weights=sa.get("weights"),
         pool_state=sa.get("pool_state"), latest_doc_ts=sa.get("latest_doc_ts"),
         rpo_seconds=res.get("rpo_seconds"), docs_lost=res.get("docs_lost"),
         embed_model_version=res.get("embed_model_version"),
         note="docs_lost = so ticket khach hang gui ma ban KHONG con giu")

    # === 5. DNS cutover — cũng chỉ đọc lại =====================================
    edge_state = None
    try:
        edge_state = httpx.get(f"{EDGE}/edge/state", timeout=2.0).json()
    except Exception as e:
        edge_state = {"error": type(e).__name__}
    step(5, "dns_cutover", cutover_ok=res.get("cutover_ok"), to_region=target,
         edge_state=edge_state,
         note="edge cache TTL: user con thay loi them toi EDGE_TTL_SECONDS nua")

    if not res.get("ok"):
        step(7, "post_incident", aborted=True,
             reason=f"failover abort tai {res.get('aborted_at')} -> KHONG cutover",
             elapsed_s=round(time.time() - t_run, 2))
        return {"ok": False, "aborted_at": res.get("aborted_at"), "failover": res}

    # === 6. Golden signals: 10 request THẬT vào region phụ =====================
    lat, errs = [], 0
    for i in range(golden_n):
        t = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer",
                          params={"q": f"hoa don thang {i % 12 + 1}"}, timeout=5.0)
            body = r.json()
            bad = r.status_code != 200 or "error" in body
        except Exception:
            bad = True
        errs += bad
        lat.append(round((time.time() - t) * 1000, 1))
    srt = sorted(lat)
    p95 = srt[min(len(srt) - 1, int(0.95 * len(srt)))]
    step(6, "verify_golden_signals", region=target, requests=golden_n,
         p95_latency_ms=p95, max_latency_ms=srt[-1],
         error_rate=round(errs / golden_n, 3), errors=errs,
         note="do truc tiep vao region phu, khong qua edge -> loai anh huong cua TTL cache")

    # === 7. Post-incident =====================================================
    elapsed = round(time.time() - t_run, 2)
    rto_cmd = ("python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
               "--target-rto 300")
    step(7, "post_incident", elapsed_s=elapsed,
         t_outage=t_outage, cutover_ok=True,
         time_to_cutover_s=None if not t_outage else round(time.time() - t_outage, 2),
         rpo_seconds=res.get("rpo_seconds"), docs_lost=res.get("docs_lost"),
         rto_command=rto_cmd,
         next_steps=["dien reports/rto-evidence.md", "dien reports/postmortem.md",
                     f"failback ve region-{primary} chi khi /readyz cua no 200 lien tuc"],
         note="RTO that nam trong loadgen JSONL, khong nam trong file nay")

    return {"ok": True, "target": target, "elapsed_s": elapsed,
            "rpo_seconds": res.get("rpo_seconds"), "docs_lost": res.get("docs_lost"),
            "p95_latency_ms": p95, "error_rate": round(errs / golden_n, 3),
            "rto_command": rto_cmd}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
