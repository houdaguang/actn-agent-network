"""API 契约漂移巡检：我们的公开文档 vs 生产实际行为。

为什么需要：`openapi/actn-public-api.yaml` 与 `skills/actn-network/references/API.md`
对外承诺的是「生产行为为准，文档是缺陷」。这句话只有在有人定期核对时才成立。
本脚本把核对自动化：用**非破坏性**探测重新观测生产行为，与文档声明的
状态码 / 信封字段 / 错误码比对，不一致即告警。

严格的只读边界（与 probe_public_api.py 一致）：
- 只调用文档中标注为「公开、无需认证」的 GET 接口。
- 对需要认证的接口，只做**不带凭据**的探测，验证其鉴权行为（预期 401/403）。
- **严禁**调用任何写接口：register / posts / comments / like / favorite /
  toggle-duty / rename / update-task。会产生真实数据，一律不碰。

用法：
    python scripts/contract_drift.py            # 巡检并输出报告
    python scripts/contract_drift.py --check    # 仅返回退出码（0=一致, 1=有漂移）
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from growth_core import append_jsonl, load_config, now_iso  # noqa: E402

CFG = load_config()
UA = "actn-growth-contract-drift/1.0"
OPENAPI = ROOT / "repos" / "actn-agent-network" / "openapi" / "actn-public-api.yaml"

# 文档声明的契约（来自 openapi/actn-public-api.yaml 的 x-verification 标注）。
# 这些是"我们对外说的话"，本巡检负责验证它们是否仍然成立。
EXPECTED = {
    "community_posts": {
        "probe": ("GET", "/api/community?action=posts&page=1&limit=1", False),
        "expect_status": 200,
        "expect_success": True,
        "expect_top_keys": {"success", "data", "pagination", "timestamp"},
        "doc": "openapi: GET /api/community?action=posts -> 200, CommunityListResponse",
    },
    "community_comments_missing": {
        "probe": ("GET", "/api/community?action=comments&id=__drift_probe__&page=1&limit=1", False),
        "expect_status": 404,
        "expect_error_code": "NOT_FOUND",
        "doc": "openapi: 非存在 id -> 404 NOT_FOUND",
    },
    "agents_tasks_unauth": {
        "probe": ("GET", "/api/agents?action=tasks&id=__drift_probe__&status=assigned", False),
        "expect_status": 401,
        "expect_error_code": "UNAUTHORIZED",
        "doc": "openapi: 无凭据 -> 401 UNAUTHORIZED",
    },
    "agents_detail_unauth": {
        "probe": ("GET", "/api/agents?action=detail&id=__drift_probe__", False),
        "expect_status": 401,
        "expect_error_code": "UNAUTHORIZED",
        "doc": "openapi: 无凭据 -> 401 UNAUTHORIZED",
    },
    "agents_toggle_duty_unauth": {
        "probe": ("PATCH", "/api/agents?action=toggle-duty&id=__drift_probe__", False),
        "expect_status": 401,
        "expect_error_code": "UNAUTHORIZED",
        "doc": "openapi: 无凭据 -> 401 UNAUTHORIZED",
    },
    "community_posts_unauth": {
        "probe": ("POST", "/api/community?action=posts", False),
        "expect_status": 401,
        "expect_error_code": "UNAUTHORIZED",
        "doc": "openapi: 无凭据 -> 401 UNAUTHORIZED",
    },
}


def call(method: str, url: str, body: bytes | None = None,
         retries: int = 3) -> tuple[int, dict | None, str]:
    for i in range(retries):
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "application/json")
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30,
                                        context=ssl.create_default_context()) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, _parse(raw), raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            return e.code, _parse(raw), raw
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                return 0, None, str(e)
            time.sleep(1.2 * (i + 1))
    return 0, None, "unreachable"


def _parse(raw: str) -> dict | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def declared_ops() -> list[str]:
    """列出 openapi 文档里声明的操作，用于确认文档本身仍可解析。"""
    try:
        import yaml  # type: ignore
        spec = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
        out = []
        for path, item in (spec.get("paths") or {}).items():
            for m in ("get", "post", "patch", "put", "delete"):
                if m in item:
                    out.append(f"{m.upper()} {path}")
        return out
    except Exception:  # noqa: BLE001
        return []


def check() -> dict:
    print("=" * 76)
    print("API 契约漂移巡检（只读，非破坏性）")
    print(f"依据文档: {OPENAPI.relative_to(ROOT)}")
    print("=" * 76)

    ops = declared_ops()
    print(f"\n[0] 文档声明的操作：{len(ops)} 个")
    for o in ops:
        print(f"    {o}")
    if not ops:
        print("    !! 文档无法解析或 paths 为空")
        return {"error": "openapi_unparsable", "drifts": [], "checked": 0}

    drifts: list[dict] = []
    checked = 0

    for site, cfg in (CFG.get("sites") or {}).items():
        origin = cfg.get("origin")
        if not origin:
            continue
        print(f"\n[1] 站点 {site}  {origin}")
        for name, spec in EXPECTED.items():
            method, path, _ = spec["probe"]
            body = b"{}" if method in ("POST", "PATCH") else None
            st, js, raw = call(method, origin + path, body)
            checked += 1

            problems: list[str] = []
            if st != spec["expect_status"]:
                problems.append(f"状态码 {st} != 文档的 {spec['expect_status']}")
            if spec.get("expect_success") is not None:
                got = (js or {}).get("success")
                if got != spec["expect_success"]:
                    problems.append(f"success={got} != 文档的 {spec['expect_success']}")
            if spec.get("expect_error_code"):
                got = ((js or {}).get("error") or {}).get("code")
                if got != spec["expect_error_code"]:
                    problems.append(f"error.code={got!r} != 文档的 {spec['expect_error_code']!r}")
            if spec.get("expect_top_keys") and js:
                missing = spec["expect_top_keys"] - set(js.keys())
                if missing:
                    problems.append(f"信封缺少字段 {sorted(missing)}")

            flag = "OK  " if not problems else "DRIFT"
            print(f"  {flag} {name:<30} HTTP {st}")
            for p in problems:
                print(f"        - {p}")
            if problems:
                drifts.append({
                    "site": site, "check": name, "method": method, "path": path,
                    "problems": problems, "observed_status": st,
                    "doc_reference": spec["doc"],
                    "raw_prefix": raw[:200],
                })

    print(f"\n[2] 结果：检查 {checked} 项，漂移 {len(drifts)} 项")
    if drifts:
        append_jsonl("growth-state/ALERTS.jsonl", {
            "at": now_iso(), "kind": "contract_drift", "severity": "high",
            "title": f"API 契约与生产行为不一致（{len(drifts)} 项）",
            "detail": {
                "count": len(drifts),
                "items": drifts,
                "action_hint": ("我们的公开承诺是『生产为准、文档是缺陷』。"
                                "请核对 openapi/actn-public-api.yaml 与 "
                                "skills/actn-network/references/API.md，"
                                "把文档改成与实际行为一致（不要反过来假设生产错了）。"),
            },
        })
        print("  [ALERT:high] 已写入 growth-state/ALERTS.jsonl")
    else:
        print("  无漂移：文档与生产行为一致")

    report = {
        "generated_at": now_iso(),
        "openapi": str(OPENAPI.relative_to(ROOT)),
        "declared_operations": ops,
        "checked": checked,
        "drifts": drifts,
    }
    out = ROOT / "growth-reports" / "_contract-drift.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[drift] 报告写入 {out}")
    return report


if __name__ == "__main__":
    r = check()
    if "--check" in sys.argv:
        sys.exit(1 if r.get("drifts") or r.get("error") else 0)
