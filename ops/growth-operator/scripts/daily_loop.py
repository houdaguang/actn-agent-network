"""ACTN Growth Operator 每日安全循环（提示词 2 的可执行实现）。

今天只做以下事项：
1. 检查全局 kill switch、两个站点健康、robots、sitemap、公开关键页面；
2. 刷新过去 24 小时的产品变化 / 真实信号 / 入站问题 / 漏斗替代口径；
3. 从 content/pending 队列中取**至多一条**内容，经守卫后发布；无合格内容则不发布；
4. 只把新增或实质更新的 URL 交给已授权的搜索提交接口（当前未授权 → 跳过并记录）；
5. 写入不可变审计日志；
6. 输出固定 RUN_STATUS 报告。

没有新信号时输出 NO_NEW_SIGNAL；不要为了完成任务而制造内容。

用法：
    python scripts/daily_loop.py [--dry-run]
"""
from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from growth_core import (CONTENT_CHANNELS, DeferredByRateLimit, Guard,  # noqa: E402
                         GrowthGuardError, load_config, load_secrets, log_run,
                         now_iso, read_jsonl)

DRY = "--dry-run" in sys.argv
CFG = load_config()
S = load_secrets()
UA = "actn-growth-daily-loop/1.0"


# --------------------------------------------------------------------------- util
def get(url: str, retries: int = 3, timeout: int = 25) -> tuple[int, str, bytes]:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl.create_default_context()) as r:
                return r.status, (r.headers.get("Content-Type") or ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, (e.headers.get("Content-Type") or ""), b""
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 * (i + 1))
    return 0, "", str(last).encode()


def gh(method: str, path: str) -> tuple[int, object]:
    req = urllib.request.Request(f"https://api.github.com{path}", method=method)
    req.add_header("Authorization", f"Bearer {S['github_token']}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=40, context=ssl.create_default_context()) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:  # noqa: BLE001
        return 0, None


def state_path() -> Path:
    return ROOT / "growth-state" / "daily-loop-state.json"


def load_state() -> dict:
    p = state_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_state(d: dict) -> None:
    state_path().write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- 1. kill switch
def step_kill_switch() -> bool:
    paused = bool(CFG.get("global", {}).get("paused"))
    print(f"[1] kill switch: paused={paused}")
    return paused


# --------------------------------------------------------------------------- 2. 站点健康
def step_site_health() -> dict:
    print("[2] 站点健康")
    out: dict = {}
    targets = {
        "china": CFG["sites"]["china"]["origin"],
        "global": CFG["sites"]["global"]["origin"],
    }
    for name, origin in targets.items():
        checks = {}
        for path in ("/robots.txt", "/sitemap.xml", "/feed.xml", "/skill.md", "/for-agent-owners"):
            st, ct, raw = get(origin + path)
            checks[path] = {"status": st, "ct": ct.split(";")[0], "bytes": len(raw),
                            "ok": st == 200 and len(raw) > 90}
        bad = [p for p, v in checks.items() if not v["ok"]]
        out[name] = {"checks": checks, "failing": bad}
        flag = "OK" if not bad else f"FAIL({len(bad)})"
        print(f"  {name:<7} {origin}  {flag}"
              + (f"  失败: {bad}" if bad else ""))
    return out


# --------------------------------------------------------------------------- 3. 信号刷新
def step_signals() -> dict:
    print("[3] 信号刷新（只读）")
    sig: dict = {"generated_at": now_iso()}

    # 自有仓库的新提交 / 入站 issue / Discussion
    repo = f"{S['github_owner']}/{S['github_repo']}"
    st, commits = gh("GET", f"/repos/{repo}/commits?per_page=3")
    if st == 200 and isinstance(commits, list):
        sig["latest_commit"] = {"sha": commits[0]["sha"][:12],
                                "date": commits[0]["commit"]["committer"]["date"]} if commits else None
    st, issues = gh("GET", f"/repos/{repo}/issues?state=open&per_page=20")
    if st == 200 and isinstance(issues, list):
        inbound = [i for i in issues if "pull_request" not in i]
        sig["open_issues"] = len(inbound)
        sig["issue_titles"] = [i["title"][:90] for i in inbound[:5]]
    st, rels = gh("GET", f"/repos/{repo}/releases?per_page=1")
    if st == 200 and isinstance(rels, list) and rels:
        sig["latest_release"] = {"tag": rels[0].get("tag_name"), "at": rels[0].get("published_at")}

    # 技能注册表：公开版本与安装量（安装量只用于观察，不做任何注入）
    st, _, raw = get("https://agentskillhub.dev/api/v1/u/"
                     f"{S['github_owner']}/skills/actn-network")
    if st == 200:
        try:
            d = json.loads(raw)
            sig["registry"] = {"version": (d.get("latestVersion") or {}).get("version"),
                               "commit": ((d.get("latestVersion") or {}).get("commitSha") or "")[:8]}
        except Exception:  # noqa: BLE001
            sig["registry"] = {"error": "unparsable"}
    st, _, raw = get("https://agentskillhub.dev/api/v1/search?q=actn&limit=10")
    if st == 200:
        try:
            hits = [s for s in (json.loads(raw).get("skills") or [])
                    if s.get("sourceIdentifier") == repo]
            sig["registry_installs"] = hits[0].get("totalInstalls") if hits else None
        except Exception:  # noqa: BLE001
            pass

    # ACTN 平台公开只读口径（不引用为对外统计，仅内部观察）
    for name, origin in (("china", CFG["sites"]["china"]["origin"]),
                         ("global", CFG["sites"]["global"]["origin"])):
        st, _, raw = get(origin + "/api/community?action=posts&page=1&limit=1")
        if st == 200:
            try:
                sig[f"{name}_public_posts_total"] = (json.loads(raw).get("pagination") or {}).get("total")
            except Exception:  # noqa: BLE001
                pass

    print(f"  仓库最新提交 : {sig.get('latest_commit')}")
    print(f"  未结 issue   : {sig.get('open_issues')}")
    print(f"  最新 Release : {sig.get('latest_release')}")
    print(f"  注册表版本   : {sig.get('registry')}  安装量={sig.get('registry_installs')}")
    print(f"  公开帖总数   : china={sig.get('china_public_posts_total')} "
          f"global={sig.get('global_public_posts_total')}（仅内部观察，不对外引用）")

    prev = load_state()
    sig["changes_vs_last_run"] = {
        k: (prev.get(k) != sig.get(k))
        for k in ("latest_commit", "latest_release", "registry", "registry_installs")
        if k in sig or k in prev
    }
    print(f"  对比上次运行 : {sig['changes_vs_last_run']}")
    return sig


# --------------------------------------------------------------------------- 4. 队列
def pending_queue() -> list[Path]:
    d = ROOT / "content" / "pending"
    if not d.exists():
        return []
    return sorted([p for p in d.glob("*.json")], key=lambda p: p.stat().st_mtime)


def step_publish_one() -> dict:
    print("[4] 队列处理（至多一条）")
    queue = pending_queue()
    if not queue:
        print("  队列为空")
        return {"published": None, "reason": "queue_empty"}

    guard = Guard()
    print(f"  队列 {len(queue)} 项: {[p.name for p in queue]}")
    deferred: list[str] = []
    blocked: list[str] = []

    for spec_path in queue:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        content_id = spec["content_id"]
        print(f"\n  尝试 {content_id}")
        try:
            channels = spec.get("channels") or {}
            urls = spec.get("urls") or []
            plans = []
            for channel, payload in channels.items():
                account = (spec.get("accounts") or {}).get(channel)
                if not account:
                    ch = guard.check_channel_enabled(channel)
                    account = ch.get("handle") or ch.get("owner")
                limits = guard.preflight(channel=channel, account=account,
                                         text=payload["text"], urls=urls,
                                         idem_key=f"{channel}:{content_id}")
                plans.append((channel, account, payload["text"], limits))
                print(f"    守卫 PASS {channel}  内容日用量 {limits['content_day']}/{limits['limit_day']}")

            if DRY:
                print("    [DRY-RUN] 守卫通过，未发布")
                return {"published": None, "reason": "dry_run_guards_passed",
                        "content_id": content_id}

            # 调发布器（子进程，保持发布逻辑单一来源）
            r = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "publish_signal.py"), str(spec_path)],
                capture_output=True, text=True, cwd=ROOT)
            print("    " + "\n    ".join((r.stdout or "").strip().splitlines()[-6:]))
            if r.returncode != 0:
                print(f"    发布失败 rc={r.returncode}，进入下一个队列项")
                blocked.append(f"{content_id}: publish rc={r.returncode}")
                continue

            done = ROOT / "content" / "published"
            done.mkdir(parents=True, exist_ok=True)
            shutil.move(str(spec_path), str(done / spec_path.name))
            print(f"    已发布并归档 -> content/published/{spec_path.name}")
            return {"published": content_id, "spec": spec_path.name,
                    "urls": urls, "channels": list(channels),
                    "deferred": deferred, "blocked": blocked}

        except DeferredByRateLimit as e:
            # 正常节流：不是异常，不占用人工队列
            print(f"    按频率上限推迟至下一轮: {e}")
            deferred.append(f"{content_id}: {e}")
            continue
        except GrowthGuardError as e:
            print(f"    守卫拦截（需人工检查）: {e}")
            blocked.append(f"{content_id}: {e}")
            continue

    print("  队列中没有一条通过守卫")
    if deferred and not blocked:
        return {"published": None, "reason": "deferred_by_rate_limit",
                "deferred": deferred, "blocked": blocked}
    return {"published": None, "reason": "no_queue_item_passed_guards",
            "deferred": deferred, "blocked": blocked}


# --------------------------------------------------------------------------- 4.5 技能分发端到端验证
def step_skill_e2e(state: dict) -> dict:
    """按周频次验证技能分发路径（add → doctor → update → doctor）。

    为什么放在这里而不是靠人手动跑：安装/更新/完整性检查本来就无需人工。
    此前我在隔离目录里手动跑过 15/15，这里把它固化成定期自动执行，
    确保注册表发布、版本刷新、update 路径不会静默失效。

    隔离保证由 verify_skill_install.py 自身承担：全程在临时项目根目录内，
    不使用 --global，不读写用户 home 下的 skhub 配置。
    """
    print("[4.5] 技能分发端到端验证（隔离作用域）")
    last = state.get("last_skill_e2e_at")
    due = True
    if last:
        try:
            age_days = (time.time() - time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%S%z"))) / 86400
            due = age_days >= 7
            print(f"  上次运行 {last}（{age_days:.1f} 天前）→ {'到期，执行' if due else '未到期，跳过'}")
        except Exception:  # noqa: BLE001
            due = True
    else:
        print("  从未运行过 → 执行")

    if not due:
        return {"ran": False, "reason": "not_due", "last_run": last}

    if DRY:
        print("  [DRY-RUN] 跳过实际执行")
        return {"ran": False, "reason": "dry_run"}

    try:
        p = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "verify_skill_install.py")],
            capture_output=True, text=True, cwd=ROOT, timeout=900)
        tail = (p.stdout or "").strip().splitlines()
        for line in tail[-14:]:
            print("  " + line)
        if p.stderr:
            print("  [stderr] " + p.stderr.strip()[:300])
        summary = next((l for l in tail if l.startswith("SUMMARY:")), "")
        ok = p.returncode == 0 and "FAIL" not in summary
        return {"ran": True, "ok": ok, "rc": p.returncode,
                "summary": summary[9:] if summary else "", "at": now_iso()}
    except Exception as e:  # noqa: BLE001
        print(f"  执行异常: {type(e).__name__}: {e}")
        return {"ran": True, "ok": False, "error": f"{type(e).__name__}: {e}", "at": now_iso()}


# --------------------------------------------------------------------------- 4.6 公开资产监控
def step_asset_watch() -> dict:
    """公开资产变更 + 队列空置监控。

    为什么放在每日循环里：skill.md 是外部 Agent 自助接入 ACTN 的入口，平台侧一改，
    我们公开的指南与 API 契约就可能悄悄过期。这条监控是最高杠杆的一项——
    它保护的是我们对外承诺的准确性，而不是我们自己发的帖子。
    只读，绝不修改任何站点。
    """
    print("[4.6] 公开资产变更 + 队列空置监控")
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from asset_watch import watch  # noqa: PLC0415
        return watch()
    except Exception as e:  # noqa: BLE001
        print(f"  监控异常: {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- 5. 搜索提交
def step_search_submission(new_urls: list[str]) -> dict:
    print("[5] 搜索提交")
    subs = CFG["sites"]["china"].get("search_submissions", {})
    subs_g = CFG["sites"]["global"].get("search_submissions", {})
    enabled = {k: v for k, v in {**subs, **subs_g}.items() if v}
    if not enabled:
        print("  未授权任何搜索提交接口（GSC/Bing/IndexNow/百度 均为 false）→ 跳过")
        return {"submitted": [], "skipped_reason": "no_authorized_search_endpoint"}
    print(f"  已授权: {list(enabled)}  待提交 URL {len(new_urls)} 条")
    return {"submitted": [], "authorized": list(enabled), "candidates": new_urls}


# --------------------------------------------------------------------------- main
def main() -> int:
    print("=" * 74)
    print(f"ACTN Daily Safe Growth Loop — {now_iso()}")
    print(f"时区 {CFG.get('timezone')}   environment {CFG.get('environment')}"
          + ("   [DRY-RUN]" if DRY else ""))
    print("=" * 74)

    paused = step_kill_switch()
    print()
    health = step_site_health()
    print()
    signals = step_signals()
    print()
    watch_summary = step_asset_watch()
    print()

    if paused:
        report = {"run_status": "PAUSED", "reason": "global.paused=true",
                  "site_health": health, "signals": signals}
        log_run({"run_type": "daily_loop", **report})
        print("\nRUN_STATUS: PAUSED")
        return 0

    result = step_publish_one()
    print()
    e2e = step_skill_e2e(load_state())
    print()
    new_urls = result.get("urls") or []
    search = step_search_submission(new_urls)
    print()

    if result.get("published"):
        status = "OK"
    elif result.get("reason") in ("queue_empty", "deferred_by_rate_limit",
                                  "dry_run_guards_passed"):
        # 「按频率推迟」是正常的节流，不是异常，不占用人工队列
        status = "NO_NEW_SIGNAL"
    else:
        status = "NEEDS_HUMAN_REVIEW"

    failing = {k: v["failing"] for k, v in health.items() if v["failing"]}
    risks = []
    if failing:
        risks.append({"site_health_failing": failing})
    if result.get("blocked"):
        risks.append({"queue_blocked_needs_review": result["blocked"]})
    if e2e.get("ran") and not e2e.get("ok"):
        # 分发路径坏了是硬故障：注册表/更新链路失效会静默毁掉整个分发面
        risks.append({"skill_distribution_e2e_failed": e2e})
        status = "NEEDS_HUMAN_REVIEW" if status != "OK" else "NEEDS_HUMAN_REVIEW"
    if (watch_summary or {}).get("changes"):
        risks.append({"public_asset_changed": watch_summary["changes"]})
    if int((watch_summary or {}).get("queue_idle_days") or 0) >= 3:
        risks.append({"content_queue_idle_days": watch_summary["queue_idle_days"]})

    report = {
        "run_type": "daily_loop",
        "run_status": status,
        "paused": paused,
        "site_health": health,
        "signals": signals,
        "asset_watch": watch_summary,
        "publish": result,
        "skill_distribution_e2e": e2e,
        "search_submission": search,
        "risks": risks,
    }
    log_run(report)
    signals["last_skill_e2e_at"] = e2e.get("at") or load_state().get("last_skill_e2e_at")
    signals["last_skill_e2e_ok"] = e2e.get("ok", load_state().get("last_skill_e2e_ok"))
    save_state(signals)

    skipped_bits = [result.get("reason") or ""]
    if result.get("deferred"):
        skipped_bits.append(f"deferred={len(result['deferred'])}（正常节流，下轮再发）")
    if result.get("blocked"):
        skipped_bits.append(f"blocked={len(result['blocked'])}（需人工检查）")

    print("=" * 74)
    print(f"RUN_STATUS: {status}")
    print(f"PUBLISHED: {[result.get('published')] if result.get('published') else []}")
    print(f"SKIPPED: {'; '.join(b for b in skipped_bits if b)}")
    print(f"RISKS: {json.dumps(risks, ensure_ascii=False)}")
    print(f"FUNNEL: 站点侧埋点不可用（禁止改代码）；替代口径 "
          f"github_issues={signals.get('open_issues')} registry_installs={signals.get('registry_installs')}")
    print(f"NEXT_SAFE_ACTION: {'队列为空——等待真实新信号' if not pending_queue() else '下一个每日循环继续处理队列'}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
