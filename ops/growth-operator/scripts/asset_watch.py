"""公开资产变更监控 + 队列空置告警。

监控什么（只读，绝不修改任何站点）：
1. 两个站点的 robots.txt / sitemap.xml / skill.md / llms.txt / feed.xml 的**内容哈希**。
   `skill.md` 尤其关键：它是外部 Agent 自助接入 ACTN 的入口。平台侧一改，
   我们公开的指南与 API 契约就可能悄悄过期 —— 这是最高杠杆的监控项。
2. content/pending 队列连续为空的天数。整条链在没人投递新内容时会静默"饿死"，
   本监控把它变成一条可见告警。

告警去哪：写入 growth-state/ALERTS.jsonl（随状态提交回仓库，并随
sync_cloud_state.py 同步回本地）。**不向公开仓库开 issue**，避免制造噪音。

用法：
    python scripts/asset_watch.py           # 独立运行
    from asset_watch import watch           # 由 daily_loop 调用
"""
from __future__ import annotations

import hashlib
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from datetime import timezone as _dt_timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from growth_core import append_jsonl, load_config, now_iso, read_jsonl  # noqa: E402

CFG = load_config()
UA = "actn-growth-asset-watch/1.0"

WATCH_PATHS = ["/robots.txt", "/sitemap.xml", "/skill.md", "/llms.txt", "/feed.xml"]

# 连续这么多天队列为空就告警
QUEUE_IDLE_ALERT_DAYS = 3


def fetch(url: str, retries: int = 3) -> tuple[int, bytes]:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            with urllib.request.urlopen(req, timeout=30,
                                        context=ssl.create_default_context()) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 * (i + 1))
    return 0, str(last).encode()


# zoneinfo 拿不到时区库（如 Windows 未装 tzdata）时的兜底偏移。
# 只用于取"本地日历日"，因此固定偏移足够，且不引入新依赖。
TZ_FALLBACK_OFFSETS = {"Asia/Shanghai": 8, "UTC": 0, "Etc/UTC": 0}


def local_tz():
    """配置里的时区对象；zoneinfo 不可用时退回固定偏移。

    不用 `time.mktime(time.strptime(...))`：它按**宿主机**时区解释墙上时间，
    云端 runner(UTC) 与本机(+0800)会得出不同结论（见 growth_core._parse_ts）。
    """
    name = str(CFG.get("timezone") or "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return _dt_timezone(timedelta(hours=TZ_FALLBACK_OFFSETS.get(name, 8)))


def local_date(ts: float | None = None) -> str:
    """配置时区下的 YYYY-MM-DD。空置天数只认日历日，不认运行次数。"""
    d = datetime.fromtimestamp(ts, local_tz()) if ts is not None else datetime.now(local_tz())
    return d.strftime("%Y-%m-%d")


def hash_state_path() -> Path:
    return ROOT / "growth-state" / "asset-hashes.json"


def load_hashes() -> dict:
    p = hash_state_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_hashes(d: dict) -> None:
    hash_state_path().write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def alert(kind: str, severity: str, title: str, detail: dict) -> None:
    append_jsonl("growth-state/ALERTS.jsonl", {
        "at": now_iso(), "kind": kind, "severity": severity,
        "title": title, "detail": detail,
    })
    print(f"  [ALERT:{severity}] {title}")


def watch() -> dict:
    """返回 {changes: [...], assets_degraded: [...], alerts: n, queue_idle_days: n}"""
    print("[watch] 公开资产变更监控（只读）")
    fetched: dict[str, dict] = {}
    changes: list[dict] = []

    for site, cfg in (CFG.get("sites") or {}).items():
        origin = cfg.get("origin")
        if not origin:
            continue
        for path in WATCH_PATHS:
            st, body = fetch(origin + path)
            key = f"{site}{path}"
            if st != 200 or not body:
                fetched[key] = {"status": st, "sha256": None, "bytes": 0}
                continue
            fetched[key] = {"status": st, "bytes": len(body),
                            "sha256": hashlib.sha256(body).hexdigest()[:16]}

    prev = load_hashes()
    merged: dict[str, dict] = {}
    degraded: list[str] = []

    for key, cur in sorted(fetched.items()):
        old_rec = prev.get(key) or {}
        old = old_rec.get("sha256")
        new = cur.get("sha256")

        # 抓取失败 → **保留上次可信基线**，只记一次失败。
        # 修复的问题（2026-09-29）：原实现直接把 {"sha256": None} 写回
        # asset-hashes.json，基线被抹掉；下一次抓取成功时 `old` 为 None，
        # 变更比对无从谈起 —— 停机窗口内发生的真实生产变更被**静默漏报**，
        # 日志里只多一行"首次记录"，与"一切正常"外观相同。
        if new is None and old:
            kept = dict(old_rec)
            kept["last_failed_at"] = now_iso()
            kept["last_failed_status"] = cur.get("status")
            merged[key] = kept
            degraded.append(key)
            continue

        if new is None:  # 从未成功抓取过：没有基线可比，不臆造告警
            merged[key] = cur
            continue

        merged[key] = cur
        if old and old != new:
            changes.append({"asset": key, "from": old, "to": new,
                            "bytes": cur.get("bytes")})
            sev = "high" if key.endswith("/skill.md") else "medium"
            extra = ""
            if key.endswith("/skill.md"):
                extra = ("skill.md 是对外 Agent 自助接入入口；平台侧改动会使我们的"
                         "指南与公开 API 契约过期。需核对 openapi/actn-public-api.yaml "
                         "与 skills/actn-network/ 的内容是否仍准确。")
            alert("asset_changed", sev, f"公开资产变更：{key}", {
                "from": old, "to": new, "bytes": cur.get("bytes"),
                "action_hint": extra or "核对我们的公开文档是否仍与生产一致。",
            })
        elif old is None:
            print(f"  + 首次记录 {key}  sha={new}")

    if not prev:
        print("  首次运行：仅建立基线，不产生告警")
        for key, cur in sorted(merged.items()):
            print(f"    {key}  sha={cur.get('sha256')}  bytes={cur.get('bytes')}")

    save_hashes(merged)
    if degraded:
        print(f"  [warn] {len(degraded)} 项抓取失败，已保留上次基线（未比对、未清空）："
              f"{', '.join(degraded)}")

    # ---- 队列空置 ----
    # 口径：数**日历日**，不数运行次数。
    # 修复的问题（2026-09-29）：原实现每次运行都 idle_days += 1，而云端一天可以
    # 跑多次（实测 2026-09-26 一天 5 次，人工部署会触发额外运行），于是
    # 「连续 3 天为空」可能在同一天内就报出来 —— 报告与事实不符。
    print("[watch] 队列空置检查")
    pending = sorted((ROOT / "content" / "pending").glob("*.json"))
    st_path = ROOT / "growth-state" / "queue-idle.json"
    st = json.loads(st_path.read_text(encoding="utf-8")) if st_path.exists() else {}
    today = local_date()
    last_nonempty = st.get("last_nonempty_date")
    empty_since = st.get("empty_since_date")

    if pending:
        if st.get("idle_days"):
            print(f"  队列已恢复（此前空置 {st.get('idle_days')} 天）")
        last_nonempty, empty_since, idle_days = today, None, 0
    else:
        if not empty_since:
            if last_nonempty:
                base = datetime.strptime(last_nonempty, "%Y-%m-%d").date() + timedelta(days=1)
            else:
                base = datetime.strptime(today, "%Y-%m-%d").date()
            empty_since = base.strftime("%Y-%m-%d")
        empty_since_d = datetime.strptime(empty_since, "%Y-%m-%d").date()
        today_d = datetime.strptime(today, "%Y-%m-%d").date()
        idle_days = (today_d - empty_since_d).days + 1
        print(f"  队列为空（连续 {idle_days} 天）")
        if idle_days >= QUEUE_IDLE_ALERT_DAYS:
            alert("queue_idle", "medium",
                  f"内容队列连续 {idle_days} 天为空",
                  {"idle_days": idle_days,
                   "action_hint": ("整条分发链在静默饿死：没有新内容进队列，云端循环就只会"
                                   "做健康检查与验证，不会产出任何分发。需要起草新内容并"
                                   "投递到 content/pending/（用 scripts/queue_content.py）。")})
    st_path.write_text(json.dumps({"idle_days": idle_days, "checked_at": now_iso(),
                                   "last_nonempty_date": last_nonempty,
                                   "empty_since_date": empty_since},
                                  ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {"assets_tracked": len(merged), "changes": changes,
               "assets_degraded": degraded,
               "queue_pending": len(pending), "queue_idle_days": idle_days}
    print(f"[watch] 完成：跟踪 {len(merged)} 项，变更 {len(changes)} 项，"
          f"抓取失败 {len(degraded)} 项，队列 {len(pending)} 条（空置 {idle_days} 天）")
    return summary


def recent_alerts(n: int = 20) -> list[dict]:
    return read_jsonl("growth-state/ALERTS.jsonl")[-n:]


if __name__ == "__main__":
    out = watch()
    print()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    al = recent_alerts(5)
    if al:
        print(f"\n最近 {len(al)} 条告警：")
        for a in al:
            print(f"  {a['at']}  [{a['severity']}] {a['title']}")
