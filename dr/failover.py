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
ACTIVE = pathlib.Path("edge/active_region")   # "DNS record" của edge/proxy.py
POLL = 0.5                                    # nhịp poll /readyz ở bước 4


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(),
           "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(rec, ensure_ascii=False), flush=True)
    return rec


def state_of(region: str, timeout: float = 2.0) -> dict:
    """/v1/state của 1 region. Không bao giờ raise — region phụ có thể cũng đang lỗi."""
    try:
        return httpx.get(f"{URL[region]}/v1/state", timeout=timeout).json()
    except Exception as e:
        return {"region": region, "error": type(e).__name__}


def is_ready(region: str, timeout: float = 2.0) -> tuple[bool, dict]:
    """(ready, body) từ /readyz. 503 = chưa serve được, KHÔNG phải lỗi mạng."""
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        try:
            body = r.json()
        except Exception:
            body = {"status_code": r.status_code}
        return r.status_code == 200, body
    except Exception as e:
        return False, {"error": type(e).__name__}


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước theo đúng thứ tự. Trả dict tóm tắt cho dr/runbook.py đọc lại (bước 4-5)."""
    primary = "b" if target == "a" else "a"
    t_start = time.time()
    result = {"ok": False, "target": target, "primary": primary, "backend": backend,
              "aborted_at": None, "rpo_seconds": None, "docs_lost": None,
              "embed_model_version": None, "waited_ready_s": None,
              "state_after": None, "cutover_ok": False}

    # --- 1. Region phụ đang ở trạng thái nào? -------------------------------------
    st = state_of(target)
    emit(step="1_verify_target", region=target, state=st,
         note="chua sua gi ca — chi chup anh pool_state/weights/vector count truoc cutover")

    # --- 2. Kéo state từ object store về region phụ -------------------------------
    t2 = time.time()
    try:
        meta = snapshot.get(target, backend)
    except SystemExit as e:
        # snapshot.py raise SystemExit khi chua tung co `put` nao chay.
        emit(step="2_restore_snapshot", ok=False, error=str(e),
             note="chua co snapshot -> khong the failover. Chay state/replicate.py truoc.")
        result["aborted_at"] = "2_restore_snapshot"
        result["elapsed_s"] = round(time.time() - t_start, 2)
        return result
    r = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                     pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    result["rpo_seconds"] = r["rpo_seconds"]
    result["docs_lost"] = r["docs_lost"]
    result["embed_model_version"] = meta.get("embed_model_version")
    emit(step="2_restore_snapshot", ok=True, backend=backend, region=target,
         snapshot_at=meta.get("snapshot_at"),
         # §3: khong co embed_model_version thi index restore ve co the khong tuong thich
         embed_model_version=meta.get("embed_model_version"),
         rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
         primary_latest_doc_ts=r["primary_latest_doc_ts"],
         restored_latest_doc_ts=r["restored_latest_doc_ts"],
         restore_seconds=round(time.time() - t2, 2))

    # --- 3. warm -> full: bat dong ho GPU pool warm-up ----------------------------
    pool = pathlib.Path(f"state/region-{target}/pool_state")
    pool.parent.mkdir(parents=True, exist_ok=True)
    before = pool.read_text().strip() if pool.exists() else "cold"
    pool.write_text("full")
    emit(step="3_scale_pool", region=target, from_state=before, to_state="full",
         note="serving/app.py chi bat dau dem WARMUP_SECONDS tu dung luc file nay doi")

    # --- 4. Cho toi khi /readyz that su tra 200 -----------------------------------
    t4 = time.time()
    deadline = t4 + wait
    ok, body, attempts = False, {}, 0
    while time.time() < deadline:
        attempts += 1
        ok, body = is_ready(target)
        if ok:
            break
        time.sleep(POLL)
    waited = round(time.time() - t4, 2)
    result["waited_ready_s"] = waited
    emit(step="4_wait_ready", region=target, ok=ok, waited_s=waited, attempts=attempts,
         detail=body, note="day la GPU pool warm-up cua §4 — no nam TRONG RTO")

    if not ok:
        # ABORT: cutover luc nay = user an 503 tu CA HAI region -> RTO dai hon.
        emit(step="abort", after_step="4_wait_ready", region=target, waited_s=waited,
             reason="target_not_ready", detail=body,
             note="KHONG doi edge/active_region — giu traffic o region cu con hon 503 ca hai ben")
        result["aborted_at"] = "4_wait_ready"
        result["elapsed_s"] = round(time.time() - t_start, 2)
        return result

    # --- 5. Chi bay gio moi doi "DNS" --------------------------------------------
    before_region = ACTIVE.read_text().strip() if ACTIVE.exists() else "a"
    ACTIVE.write_text(target)
    emit(step="5_dns_cutover", from_region=before_region, to_region=target,
         file=str(ACTIVE),
         note="user van con thay loi them toi EDGE_TTL_SECONDS nua — TTL cache nam trong RTO")

    result["cutover_ok"] = True
    result["ok"] = True
    result["state_after"] = state_of(target)
    result["elapsed_s"] = round(time.time() - t_start, 2)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
