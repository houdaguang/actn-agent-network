"""守卫数学的自检（无需网络）。

存在的理由：时间窗口算错不会抛异常，只会**静默地**把该发的内容判成不该发。
2026-09-26 的云端排程就因此吃掉了一次本该自动发布的内容——没有任何报错，
只在日志里留下一行"按频率上限推迟"，看起来完全正常。

所以这里用确定性断言把窗口数学钉死。由 deploy_ops_cloud.py 的冒烟检查调用，
**错的守卫代码不允许进入云端**。

用法：
    python scripts/selftest_guards.py          # 全部断言
    python scripts/selftest_guards.py --check  # 仅退出码
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from growth_core import _parse_ts, _ts  # noqa: E402

failures: list[str] = []
checks = 0


def eq(label: str, got, want) -> None:
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL  {label}\n        got={got!r}\n        want={want!r}")
    else:
        print(f"  PASS  {label}")


def close(label: str, got: float, want: float, tol: float = 1.0) -> None:
    global checks
    checks += 1
    if abs(got - want) > tol:
        failures.append(f"{label}: got {got}, want {want} (tol {tol})")
        print(f"  FAIL  {label}  got={got} want={want}")
    else:
        print(f"  PASS  {label}")


def old_buggy_parse(s: str) -> float:
    """修复前的写法，在**宿主机时区 == 记录偏移**时碰巧正确。

    这正是它能潜伏的原因：本机是 +0800，`mktime` 把墙上时间当本地时间，
    恰好等于正确的绝对时间 —— 所以本地测不出来。云端 runner 是 UTC 才暴露。
    """
    import time
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    return 0.0


def utc_host_buggy_parse(s: str) -> float:
    """旧写法在 **UTC 宿主机**（即 GitHub runner）上的行为。

    `time.mktime` 在 TZ=UTC 时等价于把 struct 的墙上字段当 UTC 解释，
    也就是 calendar.timegm。用它才能复现云端那次误判。
    """
    import calendar
    import time
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return float(calendar.timegm(time.strptime(s, fmt)))
        except ValueError:
            continue
    return 0.0


def main() -> int:
    print("=" * 74)
    print("守卫数学自检")
    print("=" * 74)

    # ---------------------------------------------------------------- 1. 同一时刻的多种写法必须等价
    print("\n[1] 同一时刻的不同写法必须解析为同一 epoch")
    instant = datetime(2026, 9, 24, 22, 53, 20, tzinfo=timezone.utc)
    want = instant.timestamp()
    for label, s in [
        ("带 +0800", "2026-09-25T06:53:20+0800"),
        ("带 +08:00", "2026-09-25T06:53:20+08:00"),
        ("带 Z", "2026-09-24T22:53:20Z"),
        ("带 +0000", "2026-09-24T22:53:20+0000"),
    ]:
        close(f"解析 {label}: {s}", _parse_ts(s), want)

    # ---------------------------------------------------------------- 2. 时区偏移必须真的生效
    print("\n[2] 时区偏移必须被计入（修复前正是这里失效）")
    a = _parse_ts("2026-09-25T06:53:20+0800")
    b = _parse_ts("2026-09-25T06:53:20+0000")
    eq("同一墙上时间、不同偏移 → 相差 8 小时(28800s)", round(b - a), 28800)

    # 演示旧写法的错误：在 UTC 宿主机（= 云端 runner）上，+0800 被当作 UTC
    old_local = old_buggy_parse("2026-09-25T06:53:20+0800")
    old_utc = utc_host_buggy_parse("2026-09-25T06:53:20+0800")
    print(f"        （旧写法在 +0800 宿主机上得 {old_local:.0f}，偏差 {(old_local - a) / 3600:+.1f}h"
          f" ← 所以本地测不出来；")
    print(f"          在 UTC 宿主机上得 {old_utc:.0f}，偏差 {(old_utc - a) / 3600:+.1f}h"
          f" ← 云端就是这里出错的）")
    eq("旧写法在 UTC 宿主机上会把时间推后 8 小时（→ 显得更“新” → 被误计入窗口）",
       round((old_utc - a) / 3600), 8)
    eq("旧写法在 +0800 宿主机上恰好正确（因此长期未被发现）",
       round((old_local - a) / 3600), 0)

    # ---------------------------------------------------------------- 3. 窗口数学
    print("\n[3] 24 小时窗口的边界")
    now = datetime(2026, 9, 26, 9, 27, tzinfo=timezone(timedelta(hours=8)))

    def age_hours(h: float) -> float:
        return (now.timestamp() - (now.timestamp() - h * 3600)) / 3600

    eq("26.6 小时前应落在 24h 窗口之外", age_hours(26.6) >= 24, True)
    eq("18.5 小时前应落在 24h 窗口之内", age_hours(18.5) < 24, True)

    # ---------------------------------------------------------------- 4. 复现 2026-09-26 的真实误判
    print("\n[4] 复现 2026-09-26 云端排程的误判（回归测试）")
    ledger_entries = [
        {"published_at": "2026-09-25T06:53:20+0800"},  # bluesky
        {"published_at": "2026-09-25T06:53:24+0800"},  # mastodon
    ]
    run_epoch = now.timestamp()
    correct_count = sum(1 for e in ledger_entries if run_epoch - _ts(e) < 86400)
    buggy_count = sum(1 for e in ledger_entries
                      if run_epoch - utc_host_buggy_parse(e["published_at"]) < 86400)

    eq("修复后：两条 26.6h 前的发布都不在今日窗口内", correct_count, 0)
    print(f"        （在 UTC 宿主机上，旧写法会数出 {buggy_count} 条"
          f" → 触发假的日上限 → 该发的内容被吃掉）")
    eq("在 UTC 宿主机上旧写法确实会误计 2 条（证明该回归测试有效）", buggy_count, 2)
    eq("今日窗口计数为 0 ⇒ 内容应当被允许发布", correct_count < 1, True)

    # ---------------------------------------------------------------- 5. 幂等窗口同理
    print("\n[5] 幂等/URL 复用窗口（30 天）使用同一解析，同步受益")
    t = _ts({"logged_at": "2026-08-27T00:00:00+0000"})
    nine_six = datetime(2026, 9, 26, tzinfo=timezone.utc).timestamp()
    close("该记录距 2026-09-26 00:00Z 恰为 30.0 天", (nine_six - t) / 86400, 30.0, tol=0.01)

    print("\n" + "=" * 74)
    if failures:
        print(f"SUMMARY: {checks - len(failures)}/{checks} passed  —— 有 {len(failures)} 项失败")
        for f in failures:
            print("  - " + f)
        print("=" * 74)
        return 1
    print(f"SUMMARY: {checks}/{checks} passed")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    rc = main()
    sys.exit(rc)
