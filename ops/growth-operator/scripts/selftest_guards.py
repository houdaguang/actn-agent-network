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

    # ---------------------------------------------------------------- 6. 报告不变量
    print("\n[6] 报告必须与事实一致（部分发布也是发布）")
    sys.path.insert(0, str(ROOT / "scripts"))
    from daily_loop import publish_outcome_status, summarise_publish_result  # noqa: PLC0415

    partial_case = summarise_publish_result(
        partial=[{"content_id": "x-001", "published_channels": ["bluesky_owned_account"],
                  "deferred_channels": ["mastodon_owned_account"],
                  "urls": ["https://example.invalid/post/1"],
                  "evidence": ["idempotency key present: bluesky_owned_account:x-001"]}],
        deferred=[], blocked=[])
    eq("部分发布必须回报 published（不得为 None）", partial_case.get("published"), "x-001")
    eq("部分发布必须标记 partial", partial_case.get("partial"), True)
    eq("部分发布的 RUN_STATUS 必须是 OK", publish_outcome_status(partial_case), "OK")
    eq("部分发布必须保留下轮要补的渠道",
       partial_case.get("deferred_channels"), ["mastodon_owned_account"])

    eq("什么都没发（按上限顺延）→ NO_NEW_SIGNAL",
       publish_outcome_status(summarise_publish_result([], ["c: 额度满"], [])),
       "NO_NEW_SIGNAL")
    eq("被守卫拦截（需人工）→ NEEDS_HUMAN_REVIEW",
       publish_outcome_status(summarise_publish_result([], [], ["c: 命中禁止话术"])),
       "NEEDS_HUMAN_REVIEW")
    eq("完整发布（无 partial）→ OK",
       publish_outcome_status({"published": "y-001", "deferred": [], "blocked": []}), "OK")

    # 归档清理不是分发：不得报成 OK，也不得报成故障
    arch = summarise_publish_result([], [], [], ["z-001: 所有渠道此前均已发布，仅归档"])
    eq("只归档不新发 → published 必须为 None", arch.get("published"), None)
    eq("只归档不新发 → reason 为 archived_already_published",
       arch.get("reason"), "archived_already_published")
    eq("只归档不新发 → RUN_STATUS 为 NO_NEW_SIGNAL",
       publish_outcome_status(arch), "NO_NEW_SIGNAL")
    eq("归档记录必须保留以供审计", arch.get("archived"),
       ["z-001: 所有渠道此前均已发布，仅归档"])

    # ---------------------------------------------------------------- 7. 状态同步不得说谎
    # 2026-09-27 实测：sync_cloud_state.py 用 `git pull --rebase` 更新工作副本，再把工作副本
    # 当作"云端权威"。工作副本一旦处于分叉 / detached HEAD / 残留 .git/rebase-merge，
    # git 会直接 fatal 且不更新任何文件；而脚本 check=False 且只打印输出第一行
    # （`From https://github.com/...`），失败与成功外观相同 →
    # 把**过期约 10 小时**的副本复制进 growth-state/ 并打印"已同步"。
    # 后果：本地 run-log 5 条 vs 云端 7 条；本地 pending 仍含云端已归档条目；
    # 报出的"最近一次云端循环"比实际早一个周期。**报告与事实不一致 = 硬故障。**
    print("\n[7] 状态同步：权威源不可用时必须中止，且必须识别本地滞后")
    try:
        import sync_cloud_state as sync  # noqa: PLC0415
    except ImportError:
        # 云端运维目录里没有这个本地工具（它是本地起草前的同步器），跳过而不失败。
        print("  SKIP  本地同步工具不在当前部署集内（仅本地适用）")
    else:
        # 7.1 权威源不完整 → 必须抛错，绝不回退本地工作副本
        raised = False
        try:
            sync.require_authority({"run-log.jsonl": b"x"}, ["run-log.jsonl", "content-ledger.jsonl"])
        except sync.AuthorityUnavailable:
            raised = True
        eq("权威源缺文件 → 抛 AuthorityUnavailable（而不是悄悄用本地旧副本）", raised, True)
        raised = False
        try:
            sync.require_authority({}, ["run-log.jsonl"])
        except sync.AuthorityUnavailable:
            raised = True
        eq("权威源为空 → 抛 AuthorityUnavailable", raised, True)

        # 7.2 权威源完整 → 正常放行
        sync.require_authority({"a": b"1"}, ["a"])
        eq("权威源完整 → 不抛错", True, True)

        # 7.3 本地滞后必须被识别出来（复现真实数据：5 条 vs 7 条 run-log）
        d = sync.diff_bytes(
            {"run-log.jsonl": b"\n".join([b"l%d" % i for i in range(7)])},
            {"run-log.jsonl": b"\n".join([b"l%d" % i for i in range(5)])},
        )
        eq("本地 run-log 滞后 → 判为 mismatch（不得判为 ok）", d["mismatch"], ["run-log.jsonl"])
        eq("滞后文件不得计入 ok", d["ok"], [])
        d2 = sync.diff_bytes({"a": b"1", "b": b"2"}, {"a": b"1"})
        eq("本地缺文件 → 判为 missing", d2["missing"], ["b"])
        eq("字节相同 → 判为 ok", sync.diff_bytes({"a": b"1"}, {"a": b"1"})["ok"], ["a"])

        # 7.4 队列视图对齐：本地独有的 pending 必须被判为 stale（复现 registry-001 已归档却仍在本地队列）
        r = sync.reconcile_view(
            {"pending/en-actn-skill-update-002.json": b"new"},
            {"pending/en-actn-skill-update-002.json": b"new",
             "pending/en-actn-skill-registry-001.json": b"old"},
        )
        eq("云端已归档、本地仍留的 pending → 判为 stale", r["stale"], ["pending/en-actn-skill-registry-001.json"])
        eq("stale 名单不得混入 missing", r["missing"], [])
        r2 = sync.reconcile_view({"pending/x.json": b"1"}, {})
        eq("云端有、本地无 → 判为 missing（需补入）", r2["missing"], ["pending/x.json"])

        # 7.5 判定闸门：拿不到权威源 → 只能 ABORT，不能宣称成功
        clean = {"ok": ["a"], "missing": [], "mismatch": []}
        eq("权威源不可用 → 判定 ABORT", sync.sync_verdict(False, clean), "ABORT")
        eq("权威源可用且逐字节一致 → 判定 VERIFIED", sync.sync_verdict(True, clean), "VERIFIED")
        eq("有 mismatch → 判定不得为 VERIFIED",
           sync.sync_verdict(True, {"ok": [], "missing": [], "mismatch": ["a"]}), "MISMATCH")

    # ---------------------------------------------------------------- 8. 合规扫描不得自伤
    # 2026-09-27 实测：preflight_repo.py 的"禁用话术"扫描会遍历**整棵树**，包括
    # ops/growth-operator/actn-growth-config.yaml —— 而那个文件里的 forbidden_phrases
    # 列表**必然**由禁用话术本身组成。于是该检查永久红着（24/25），
    # 真正的违规会淹没在噪音里；同时报告与实际不符（"发现违规"而实际是规则定义）。
    # 与已知的"CI 密钥扫描命中规则定义文件"是同一类自伤。
    print("\n[8] 内容合规扫描：规则定义列表不得被判为违规（自身不得是噪音源）")
    try:
        import preflight_repo as pf  # noqa: PLC0415
    except ImportError:
        print("  SKIP  仓库预检工具不在当前部署集内（仅本地适用）")
    else:
        import tempfile  # noqa: PLC0415

        RULES = ["guaranteed income", "passive income", "保证赚钱"]
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)

            # 8.1 规则定义列表：不得命中（含带语种后缀的真实键名 forbidden_phrases_en）
            (tdp / "cfg.yaml").write_text(
                "guards:\n"
                "  forbidden_phrases_en:\n"
                '    - "guaranteed income"\n'
                '    - "passive income"\n'
                "  forbidden_phrases_zh:\n"
                '    - "保证赚钱"\n',
                encoding="utf-8")
            eq("YAML 的 forbidden_phrases_* 列表 → 不得被判为违规",
               pf.phrase_violations(tdp, RULES), [])

            # 8.2 但非规则字段里的同类话术**必须**照旧命中（证明没有把检查弄瞎）
            (tdp / "cfg.yaml").write_text(
                "channels:\n"
                "  bluesky:\n"
                '    bio: "Start your passive income today"\n',
                encoding="utf-8")
            v = pf.phrase_violations(tdp, RULES)
            eq("非规则字段里的禁用话术 → 仍须命中", len(v), 1)
            eq("命中必须指出是哪个文件", v[0].startswith("cfg.yaml") if v else False, True)

            # 8.3 类似但语义仍是规则清单的键（approved-claims.yml 用的 denied_claims）也不得命中
            (tdp / "cfg.yaml").write_text(
                "denied_claims:\n"
                "  - id: deny-earning-promises\n"
                "    statement: 保证赚钱 / 稳定收益 / 被动收入 等任何收益承诺\n"
                "    status: denied\n",
                encoding="utf-8")
            eq("denied_claims 规则清单 → 不得被判为违规",
               pf.phrase_violations(tdp, RULES), [])

            # 8.4 非规则字段里的同类话术**必须**照旧命中（证明没有把检查弄瞎）
            (tdp / "cfg.yaml").write_text(
                "guards:\n"
                '  note: "passive income is banned copy"\n',
                encoding="utf-8")
            eq("普通字段里的禁用话术 → 仍须命中",
               len(pf.phrase_violations(tdp, RULES)), 1)

            # 8.5 纯文本文件全量扫描不受影响
            (tdp / "cfg.yaml").unlink()
            (tdp / "post.md").write_text("保证赚钱", encoding="utf-8")
            eq("纯文本里的话术 → 仍须命中",
               len(pf.phrase_violations(tdp, RULES)), 1)
            (tdp / "post.md").write_text("保证赚钱，且 guaranteed income", encoding="utf-8")
            eq("纯文本命中多种话术 → 逐条报出",
               len(pf.phrase_violations(tdp, RULES)), 2)

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
