"""线上端到端业务流实测（真实打生产环境，不 mock）。

按 skill「现场实测」纪律：进程活着 ≠ 服务可用，必须打**公网 URL**。

流程：注册 → 登录 → 上传 → 预测 → 查结果 → 查明细 → 导出 CSV → 登出 → 旧 Token 复验
每一步都记录 HTTP 状态码与 `body.code`，任何一步失败即打印并继续（不中断，便于一次跑完看清断点）。

用法：python scripts/_probe_live.py [BASE_URL]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else
        "https://sales-forecast-27153.app.workbuddy.host").rstrip("/")
DEMO = Path(__file__).resolve().parents[1] / "demo" / "演示数据_正常.csv"
EMAIL = f"probe_{uuid.uuid4().hex[:8]}@example.com"
PASSWORD = "ProbePassw0rd2026"

_fails: list[str] = []


def call(method: str, path: str, *, body=None, token: str | None = None,
         raw: bytes | None = None, ctype: str | None = None,
         timeout: int = 180) -> tuple[int, object]:
    """用 `curl` 子进程发请求。

    ⚠️ 为什么不用 urllib：本机 `http_proxy` 对 CONNECT 隧道偶发 502
    （`git push` 同款现象），curl 自带重试与更稳的隧道处理。
    """
    import subprocess
    import tempfile

    url = f"{BASE}{path}"
    args = ["curl", "-sS", "-o", "-", "-w", "\n__HTTP__%{http_code}", "-X", method]
    if body is not None:
        args += ["-H", "Content-Type: application/json", "--data-binary",
                 json.dumps(body, ensure_ascii=False)]
    if ctype:
        args += ["-H", f"Content-Type: {ctype}"]
    if token:
        args += ["-H", f"Authorization: Bearer {token}", "-H", f"X-Auth-Token: {token}"]
    if raw is not None:
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(raw)
            raw_path = fh.name
        args += ["--data-binary", f"@{raw_path}"]
    args += ["--max-time", str(timeout), url]

    t0 = time.perf_counter()
    proc = None
    # 🔴 **本机代理会偶发 `CONNECT tunnel failed, response 502`**
    #    （与 `git push` 同款抖动；直连 443 又不通），所以连接层失败要重试。
    #    只重试「拿不到 HTTP 响应」的情况，**不重试**业务错误。
    for attempt in range(3):
        try:
            proc = subprocess.run(args, capture_output=True, timeout=timeout + 20)
        except subprocess.TimeoutExpired:
            continue
        if b"__HTTP__0" not in proc.stdout and b"\n__HTTP__" in proc.stdout:
            break
        if attempt < 2:
            logger_note = f"重试 {attempt + 1}/3（连接层失败）"
            print(f"        ↳ {logger_note}")
    if proc is None:
        return 0, "请求超时"
    dt = (time.perf_counter() - t0) * 1000
    out = proc.stdout.decode("utf-8", "replace")
    if "__HTTP__" not in out:
        return 0, f"curl 失败：{proc.stderr.decode('utf-8', 'replace')[:200]}"
    payload_s, _, code_s = out.rpartition("\n__HTTP__")
    try:
        code = int(code_s.strip())
    except ValueError:
        code = 0
    try:
        parsed = json.loads(payload_s)
    except Exception:  # noqa: BLE001
        parsed = f"<非 JSON，{len(payload_s)} B> {payload_s[:120]!r}"
    _last_ms = dt
    return code, parsed


def step(name: str, code: int, payload, expect_code: int | None = None) -> object:
    body_code = None
    if isinstance(payload, dict):
        body_code = payload.get("code")
    ok = code in (200, 201) and (expect_code is None or body_code == expect_code)
    flag = "OK  " if ok else "FAIL"
    if not ok:
        _fails.append(name)
    desc = ""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            desc = " | ".join(
                f"{k}={data[k]}" for k in
                ("task_id", "status", "series_count", "duration_ms", "error_code")
                if k in data
            )
    print(f"[{flag}] {name}: HTTP {code} body.code={body_code} {desc}")
    if not ok:
        print(f"        ↳ {str(payload)[:300]}")
    return payload


def main() -> int:
    print(f"目标：{BASE}")
    print(f"测试账号：{EMAIL}\n" + "=" * 66)

    code, payload = call("GET", "/health")
    step("健康检查", code, payload, 0)

    code, payload = call("POST", "/api/v1/auth/register",
                          body={"email": EMAIL, "password": PASSWORD, "nickname": "探针"})
    step("注册", code, payload, 0)

    code, payload = call("POST", "/api/v1/auth/login",
                          body={"email": EMAIL, "password": PASSWORD})
    step("登录", code, payload, 0)
    token = ""
    if isinstance(payload, dict):
        data = payload.get("data") or {}
        token = data.get("access_token") or data.get("token") or ""
    if not token:
        print("[FAIL] 未取到 token，后续步骤无法进行")
        return 1

    boundary = ("----probe" + uuid.uuid4().hex[:8])
    csv_bytes = DEMO.read_bytes()
    body_bytes = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="probe.csv"\r\n'
        f"Content-Type: text/csv\r\n\r\n"
    ).encode() + csv_bytes + f"\r\n--{boundary}--\r\n".encode()
    code, payload = call("POST", "/api/v1/datasets", raw=body_bytes,
                         ctype=f"multipart/form-data; boundary={boundary}", token=token)
    step("上传数据集", code, payload, 0)
    dataset_id = 0
    if isinstance(payload, dict):
        data = payload.get("data") or {}
        ds_obj = data.get("dataset") or {}
        dataset_id = (
            data.get("dataset_id") or data.get("id")
            or (ds_obj.get("id") if isinstance(ds_obj, dict) else 0) or 0
        )

    if dataset_id:
        code, payload = call("GET", f"/api/v1/datasets/{dataset_id}/preview", token=token)
        step("预览数据", code, payload, 0)

    t0 = time.perf_counter()
    code, payload = call("POST", "/api/v1/predictions",
                         body={"dataset_id": dataset_id, "horizon": 7}, token=token)
    dt = (time.perf_counter() - t0) * 1000
    payload = step(f"创建预测任务（端到端 {dt:.0f} ms）", code, payload, 0)
    task_id = 0
    if isinstance(payload, dict):
        data = payload.get("data") or {}
        task_id = data.get("task_id") or 0
        if data.get("status") == "failed":
            print(f"        ↳ 任务失败：{data.get('error_code')} / {data.get('error_msg')}")

    if task_id:
        code, payload = call("GET", f"/api/v1/predictions/{task_id}", token=token)
        step("查结果", code, payload, 0)
        code, payload = call("GET", f"/api/v1/predictions/{task_id}/detail?page=1&size=20",
                             token=token)
        step("查明细", code, payload, 0)
        code, payload = call("GET", f"/api/v1/predictions/{task_id}/export", token=token)
        ok = code == 200
        print(f"[{'OK  ' if ok else 'FAIL'}] 导出 CSV: HTTP {code}")
        if not ok:
            _fails.append("导出 CSV")
            print(f"        ↳ {str(payload)[:200]}")

    code, payload = call("GET", "/api/v1/history?page=1&size=10", token=token)
    step("查历史", code, payload, 0)

    code, payload = call("GET", "/api/v1/users/me", token=token)
    step("个人资料", code, payload, 0)

    code, payload = call("POST", "/api/v1/auth/logout", token=token)
    step("登出", code, payload, 0)

    code, payload = call("GET", "/api/v1/users/me", token=token)
    ok = code == 401 or (isinstance(payload, dict) and payload.get("code") in (40101, 40102))
    print(f"[{'OK  ' if ok else 'FAIL'}] 登出后旧 Token 失效: HTTP {code} "
          f"body.code={payload.get('code') if isinstance(payload, dict) else payload}")
    if not ok:
        _fails.append("登出后旧 Token 失效")

    print("=" * 66)
    if _fails:
        print(f"失败步骤 {len(_fails)} 项：{', '.join(_fails)}")
        return 1
    print("全部通过：线上端到端业务流完整可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
