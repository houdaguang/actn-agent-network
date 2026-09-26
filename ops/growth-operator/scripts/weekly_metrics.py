"""每周增长指标采集（提示词 4 中"机械"的那一半）。

只采集与呈现数据，不做判断、不做外部发布、不做 SCALE/REDUCE 决策——
决策需要人（或带 LLM 的 Agent），脚本不替它下结论。

输出 growth-reports/YYYY-MM-DD-weekly-metrics.md，并打印同样内容供 CI 日志留痕。

用法：
    python scripts/weekly_metrics.py
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
from growth_core import CONTENT_CHANNELS, load_config, load_secrets, read_jsonl  # noqa: E402

S = load_secrets()
CFG = load_config()
UA = "actn-growth-weekly/1.0"
REPO = f"{S['github_owner']}/{S['github_repo']}"
OPS_REPO = f"{S['github_owner']}/actn-growth-ops"


def http(url: str, headers: dict | None = None, timeout: int = 45) -> tuple[int, str]:
    for attempt in range(3):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            req.add_header("Accept", "application/json")
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl.create_default_context()) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:400]
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                return 0, str(e)
            time.sleep(1.5 * (attempt + 1))
    return 0, "unreachable"


def gh(path: str) -> tuple[int, object]:
    st, raw = http(f"https://api.github.com{path}",
                   {"Authorization": f"Bearer {S['github_token']}"})
    try:
        return st, json.loads(raw)
    except Exception:  # noqa: BLE001
        return st, raw


def section(title: str) -> None:
    print(f"\n## {title}")


def main() -> int:
    now = time.localtime()
    stamp = time.strftime("%Y-%m-%d", now)
    lines: list[str] = []
    notes: list[str] = []

    def emit(s: str = "") -> None:
        lines.append(s)
        print(s)

    emit(f"# ACTN 增长周报数据 — {stamp}")
    emit()
    emit(f"采集时间：{time.strftime('%Y-%m-%dT%H:%M:%S%z', now)}")
    emit()
    emit("> 本文件只做**数据采集与呈现**，不含 SCALE / KEEP / REDUCE / PAUSE 判断。")
    emit("> 判断需要人（或带 LLM 的 Agent）来做。缺失的指标一律标注为缺失，不推测、不填充。")
    emit()

    # ---------------------------------------------------------------- 代码仓
    emit("## 1. 开发者资产仓（分发面）")
    emit()
    emit("| 指标 | 值 |")
    emit("|---|---|")
    st, commits = gh(f"/repos/{REPO}/commits?per_page=1&since="
                     + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7 * 86400)))
    emit(f"| 近 7 天提交数 | {len(commits) if isinstance(commits, list) else 'n/a'} |")
    st, rels = gh(f"/repos/{REPO}/releases?per_page=5")
    emit(f"| Release 数（近 5 个） | {len(rels) if isinstance(rels, list) else 'n/a'} |")
    st, issues = gh(f"/repos/{REPO}/issues?state=open&per_page=50")
    inbound = [i for i in issues if "pull_request" not in i] if isinstance(issues, list) else []
    emit(f"| 未结 issue（入站信号） | {len(inbound)} |")
    st, views = gh(f"/repos/{REPO}/traffic/views?per=week")
    if st == 200 and isinstance(views, dict):
        emit(f"| 仓库存取量 views / uniques | {views.get('count')} / {views.get('uniques')} |")
    else:
        emit(f"| 仓库存取量 | 不可得（traffic API 需 push 权限，HTTP {st}） |")
    st, clones = gh(f"/repos/{REPO}/traffic/clones?per=week")
    if st == 200 and isinstance(clones, dict):
        emit(f"| 克隆量 clones / uniques | {clones.get('count')} / {clones.get('uniques')} |")
    else:
        emit(f"| 克隆量 | 不可得（HTTP {st}） |")
    emit()

    # ---------------------------------------------------------------- 注册表
    emit("## 2. Agent Skills 注册表（主渠道 3）")
    emit()
    emit("| 指标 | 值 |")
    emit("|---|---|")
    st, raw = http("https://agentskillhub.dev/api/v1/u/houdaguang/skills/actn-network")
    if st == 200:
        try:
            d = json.loads(raw)
            lv = d.get("latestVersion") or {}
            emit(f"| 已发布版本 | {lv.get('version')} |")
            emit(f"| commit | {(lv.get('commitSha') or '')[:12]} |")
            emit(f"| 版本数 | {len(d.get('versions') or [])} |")
        except Exception:  # noqa: BLE001
            emit("| 注册表读取 | 解析失败 |")
    else:
        emit(f"| 注册表读取 | HTTP {st} |")
    st, raw = http("https://agentskillhub.dev/api/v1/search?q=actn&limit=10")
    installs = None
    if st == 200:
        try:
            for s_ in (json.loads(raw).get("skills") or []):
                if s_.get("sourceIdentifier") == REPO:
                    installs = s_.get("totalInstalls")
        except Exception:  # noqa: BLE001
            pass
    emit(f"| totalInstalls（注册表原始计数） | {installs if installs is not None else 'n/a'} |")

    # 扣除自测：分发路径验证会真实安装该技能，会把计数顶上去
    attr = {}
    ap = ROOT / "growth-state" / "install-attribution.json"
    if ap.exists():
        try:
            attr = json.loads(ap.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            attr = {}
    self_test = int(attr.get("self_test_delta_sum") or 0)
    emit(f"| 其中：自测产生（已归因） | {self_test} |")
    emit(f"| 归因起始时间 | {attr.get('attribution_started_at') or 'n/a'} |")
    if installs is not None:
        net = installs - self_test
        emit(f"| **净外部安装（上界，非真值）** | **{net}** |")
    else:
        emit("| 净外部安装（上界） | 不可得（注册表未返回计数） |")
    emit()
    emit("⚠️ **计数口径说明（重要）**：自动化的分发路径验证（`verify_skill_install.py`）")
    emit("会**真实安装**该技能，因此注册表的原始 `totalInstalls` 包含我们自己的测试安装。")
    emit("上表净值为 **上界而非真值**：归因机制启用**之前**发生的自测运行没有被计入，")
    emit("且注册表可能把 `add` 与 `update` 各计一次（实测单次运行 delta 常为 2）。")
    emit("因此真实外部安装数**很可能低于**该上界。任何引用都必须连同这句限定一起引用。")
    emit()

    # ---------------------------------------------------------------- 自有社交
    emit("## 3. 自有社交渠道（低音量分发）")
    emit()
    led = read_jsonl("growth-state/content-ledger.jsonl")
    pub = [r for r in led if r.get("status") == "published"]
    emit("| 渠道 | 累计发布 | 近 7 天 |")
    emit("|---|---|---|")
    cutoff = time.time() - 7 * 86400
    for ch in sorted({r.get("channel") for r in pub if r.get("channel")}):
        allc = [r for r in pub if r.get("channel") == ch]
        recent = [r for r in allc if _ts(r) >= cutoff]
        emit(f"| {ch} | {len(allc)} | {len(recent)} |")
    emit()
    emit(f"- 内容分发类渠道（受每日上限约束）：{', '.join(sorted(CONTENT_CHANNELS))}")
    emit()

    # ---------------------------------------------------------------- 平台公开口径
    emit("## 4. 平台公开只读口径（仅内部观察，**不对外引用**）")
    emit()
    emit("| 站点 | 公开帖总数 |")
    emit("|---|---|")
    for name in ("china", "global"):
        origin = CFG["sites"][name]["origin"]
        st, raw = http(f"{origin}/api/community?action=posts&page=1&limit=1")
        total = "n/a"
        if st == 200:
            try:
                total = (json.loads(raw).get("pagination") or {}).get("total")
            except Exception:  # noqa: BLE001
                pass
        emit(f"| {name} | {total} |")
    emit()
    notes.append("平台统计口径未经负责人确认，因此**不得**出现在任何对外内容中。")
    emit()

    # ---------------------------------------------------------------- 缺失项
    emit("## 5. 结构性缺失（不得用推测填充）")
    emit()
    for line in [
        "**站点侧漏斗事件**（`anonymous_visit` → `spam_complaint` 全序列）：缺失。"
        "埋点需改动现有生产代码仓，受宪法性边界禁止。",
        "**first-touch / last-touch 归因**：缺失。同上。",
        "**注册与合格激活数**：缺失。无站点侧回传。",
        "**真实任务案例 / 首次交付与结算**：缺失。负责人尚无可用真实任务。",
        "**邮件渠道指标**：不可用。DKIM 与 DMARC 缺失，渠道已阻断。",
        "**Search Console / Bing / 百度收录数据**：不可得。未授予站点验证权限。",
    ]:
        emit(f"- {line}")
    emit()

    # ---------------------------------------------------------------- 运行健康
    emit("## 6. 自动化运行健康")
    emit()
    runs = read_jsonl("growth-state/run-log.jsonl")
    recent_runs = [r for r in runs if _ts(r) >= cutoff]
    emit(f"- 近 7 天运行次数：{len(recent_runs)}")
    statuses: dict[str, int] = {}
    for r in recent_runs:
        k = str(r.get("run_status") or r.get("run_type") or "unknown")
        statuses[k] = statuses.get(k, 0) + 1
    emit(f"- RUN_STATUS 分布：{json.dumps(statuses, ensure_ascii=False)}")
    e2e = None
    for r in reversed(runs):
        if r.get("run_type") == "daily_loop" and r.get("skill_distribution_e2e"):
            e2e = r["skill_distribution_e2e"]
            break
    emit(f"- 最近一次分发路径验证：{json.dumps(e2e, ensure_ascii=False) if e2e else '尚无记录'}")
    emit()

    emit("## 7. 待人工/待决策")
    emit()
    emit("- 本文件**不给出** SCALE / KEEP / REDUCE / PAUSE 结论，需人工或 LLM 复盘任务判定。")
    emit("- 若上表中任一关键指标为 `n/a` 或「缺失」，不得以其他指标替代推断。")
    emit()

    out = ROOT / "growth-reports" / f"{stamp}-weekly-metrics.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[weekly] 报告写入 {out}")
    return 0


def _ts(record: dict) -> float:
    """复用 growth_core 的时区正确解析。

    此前这里有一份独立实现，同样犯了 `mktime(strptime(..., '%z'))` 的错——
    两处各写一遍，两处都错。改为单一实现，避免再分叉。
    """
    from growth_core import _parse_ts  # noqa: PLC0415
    for k in ("published_at", "logged_at", "created_at"):
        v = record.get(k)
        if v:
            return _parse_ts(v)
    return 0.0


if __name__ == "__main__":
    sys.exit(main())
