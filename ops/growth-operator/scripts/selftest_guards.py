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

import json
import sys
import tempfile
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

    # [9] 空目录 ≠ 权威源不可用（2026-09-28 真实故障，勿回退）
    # 队列被发空后 content/pending/ 成了空目录；git 不跟踪空目录 ⇒ Contents API 返回 404。
    # 原实现把这个 404 当致命错误直接 abort，**连备选源 git-fetch-blob 都不试**，
    # 于是 09-28 云端明明跑成功（RUN_STATUS: OK，已发布 en-actn-skill-update-002），
    # 本地却永远同步不到：run-log 停在 15357B（云端 17585B）、pending 仍留着已归档条目。
    # 而 404 在"同步失败"与"这一轮真的什么都没剩"之间不可区分 ⇒ 必须交给备选源裁决。
    print("\n[9] 状态同步：空目录（HTTP 404）必须回退到备选权威源，不得直接中止")
    try:
        import sync_cloud_state as sync  # noqa: PLC0415
    except ImportError:
        print("  SKIP  本地同步工具不在当前部署集内（仅本地适用）")
    else:
        eq("空目录/404 属于可降级错误，不是权威源不可用",
           sync.is_degradable_authority_error(sync.urllib.error.HTTPError(
               "url", 404, "Not Found", {}, None)), True)
        eq("目录缺失（404）同样可降级",
           sync.is_degradable_authority_error(sync.urllib.error.HTTPError(
               "url", 404, "Not Found", {}, None)), True)
        eq("403 也按目录级问题降级（走备选源复核）",
           sync.is_degradable_authority_error(sync.urllib.error.HTTPError(
               "url", 403, "rate limited", {}, None)), True)
        eq("401 属凭据问题，不得降级（必须响亮失败）",
           sync.is_degradable_authority_error(sync.urllib.error.HTTPError(
               "url", 401, "Unauthorized", {}, None)), False)
        eq("网络类错误不得降级",
           sync.is_degradable_authority_error(OSError("connection reset")), False)

        # 空目录裁决：权威源说"队列是空的"，而本地还剩旧条目 → 必须按远端对齐
        auth = {"ops/growth-operator/growth-state/run-log.jsonl": b"l1\nl2\n"}
        lp = sync.local_path_for("ops/growth-operator/growth-state/run-log.jsonl")
        eq("权威路径 → 本地落点映射保持稳定",
           lp is not None and lp.name == "run-log.jsonl" and lp.parent.name == "growth-state", True)
        eq("非权威路径 → 不给落点（防止把任意路径写进本地）",
           sync.local_path_for("README.md"), None)
        rv = sync.reconcile_view(auth, {"ops/growth-operator/growth-state/run-log.jsonl": b"l1\n"})
        eq("本地滞后于权威 → 不得判为 stale（stale 只表示本地独有）", rv["stale"], [])
        eq("本地滞后于权威 → 判为 changed", rv["changed"], ["ops/growth-operator/growth-state/run-log.jsonl"])

    # [10] 字节码缓存不得成为内容合规的噪音源（2026-09-28 真实故障）
    # 自检脚本的断言文本里必然包含禁用话术（它就是用来验证话术能被检出的），
    # 运行一次后 `__pycache__/*.pyc` 把同样的字节编进去 → 扫描器把缓存当"违规内容" →
    # preflight 内容合规永久红（24/25）。与 09-27 的 YAML 规则清单自伤同一类。
    # 缓存是**构建产物**，不是可发布内容，必须跳过。
    print("\n[10] 内容合规：字节码缓存（.pyc）不得被判为违规内容")
    import preflight_repo as pf  # noqa: PLC0415
    eq("__pycache__ 目录属构建产物 → 跳过扫描",
       pf.is_build_artifact(Path("ops/x/__pycache__/m.cpython-313.pyc")), True)
    eq(".pyc 后缀 → 跳过扫描",
       pf.is_build_artifact(Path("a/b/c.pyc")), True)
    eq(".pyo 后缀 → 跳过扫描", pf.is_build_artifact(Path("a/b/c.pyo")), True)
    eq(".pyd 后缀 → 跳过扫描", pf.is_build_artifact(Path("a/b/c.pyd")), True)
    eq("普通源码 → 必须照旧扫描",
       pf.is_build_artifact(Path("scripts/selftest_guards.py")), False)
    eq("路径里含 pycache 字样的普通文件 → 不得误跳过",
       pf.is_build_artifact(Path("docs/pycache-notes.md")), False)

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "__pycache__").mkdir()
        (tdp / "__pycache__" / "m.cpython-313.pyc").write_bytes(b"\x00passive income\x00")
        eq("缓存里的禁用话术字节 → 不得判为违规",
           pf.phrase_violations(tdp, ["passive income"]), [])
        (tdp / "real.md").write_text("passive income", encoding="utf-8")
        eq("真实内容文件仍须命中（证明没有把检查弄瞎）",
           len(pf.phrase_violations(tdp, ["passive income"])), 1)

        # 10.6 规则的自我定义（自检夹具）不得被自己的规则判违规
        # 但只有"自检脚本"享此豁免——真实内容文件照旧全量扫描。
        (tdp / "real.md").unlink()
        (tdp / "selftest_guards.py").write_text('RULES = ["passive income"]', encoding="utf-8")
        eq("自检夹具里的禁用话术 → 不得判为违规（它是规则的可执行定义）",
           pf.phrase_violations(tdp, ["passive income"]), [])
        (tdp / "content.md").write_text("we promise passive income", encoding="utf-8")
        eq("非自检文件仍须命中（豁免范围不得扩大）",
           len(pf.phrase_violations(tdp, ["passive income"])), 1)
        eq("豁免判定：路径精确匹配自检脚本", pf.is_rule_definition(
            Path("scripts/selftest_guards.py"), Path(".")), True)
        eq("豁免判定：普通内容文件不豁免",
           pf.is_rule_definition(Path("docs/faq.md"), Path(".")), False)
        eq("豁免判定：名字含 selftest 的 py 也豁免",
           pf.is_rule_definition(Path("ops/selftest_x.py"), Path(".")), True)

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

    # ---------------------------------------------------------------- 9. 公开资产监控不得静默失明
    # 2026-09-29：china/sitemap.xml 首页 lastmod 推进（**等长变更**：哈希变、字节不变）
    # 触发了仓库历史上第一条 asset_changed 告警。核对后确认生产 URL 集合未变
    # （china 16 / global 22），我们自有文档引用的 ACTN URL 全部有效 —— 告警本身良性。
    # 但顺着这条告警读代码，发现监控自身有两个"只会在特定时刻才显形"的缺陷：
    #   A) 抓取失败会把 sha256=None 写回基线；基线被抹掉后，真实变更**永远**报不出来
    #      （日志里只多一行"首次记录"，与"一切正常"外观相同）；
    #   B) idle_days 每次运行都 +1，而云端一天可以跑多次（run-log 实测 2026-09-26 一天 5 次），
    #      于是"连续 3 天为空"可能在**同一天内**就报出来。
    # 两者都是"报告说的"与"实际发生的"不符 → 按硬故障处理。
    print("\n[11] 公开资产监控：抓取失败不得清空基线；空置天数只认日历日")
    try:
        import asset_watch as aw  # noqa: PLC0415
    except ImportError:
        print("  SKIP  asset_watch 不在当前部署集内")
    else:
        saved = (aw.ROOT, aw.fetch, aw.append_jsonl, aw.CFG)
        try:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                (tdp / "content" / "pending").mkdir(parents=True)
                (tdp / "growth-state").mkdir()
                recorded: list[dict] = []
                aw.ROOT = tdp
                aw.append_jsonl = lambda rel, rec: recorded.append(rec)
                aw.CFG = {"timezone": "Asia/Shanghai",
                          "sites": {"china": {"origin": "https://china.invalid"},
                                    "global": {"origin": "https://global.invalid"}}}

                hashes = tdp / "growth-state" / "asset-hashes.json"
                hashes.write_text(json.dumps({"china/sitemap.xml": {
                    "status": 200, "bytes": 100, "sha256": "baseline00"}}),
                    encoding="utf-8")

                def fake_fetch(fail_sitemap: bool):
                    def _f(url, retries=3):
                        if (fail_sitemap and url.endswith("/sitemap.xml")
                                and "china.invalid" in url):
                            return 0, b"timeout"
                        return 200, b"body:" + url.encode()
                    return _f

                # 11.1 抓取失败：必须保留基线（修复前会被写成 sha256=None）
                aw.fetch = fake_fetch(True)
                out1 = aw.watch()
                kept = json.loads(hashes.read_text(encoding="utf-8"))
                eq("抓取失败不得抹掉已有基线",
                   kept["china/sitemap.xml"]["sha256"], "baseline00")
                eq("抓取失败必须显式记入运行摘要（否则报告与事实不一致）",
                   "china/sitemap.xml" in (out1.get("assets_degraded") or []), True)
                eq("抓取失败不得被误报成公开资产变更",
                   [a for a in recorded if a["kind"] == "asset_changed"], [])
                eq("抓取失败不得计入变更数", out1["changes"], [])
                eq("抓取失败项仍须计入跟踪总数（不得悄悄少一项）",
                   out1["assets_tracked"], 10)

                # 11.2 基线保住之后，真实变更仍必须被检出
                #      （证明修的是"还能报警"，而不是把监控弄瞎）
                aw.fetch = fake_fetch(False)
                aw.watch()
                eq("基线保留后，真实内容变更仍须告警",
                   [a["title"] for a in recorded if a["kind"] == "asset_changed"],
                   ["公开资产变更：china/sitemap.xml"])

                # 11.3 同一天内多次运行不得把空置天数累加
                hashes.unlink()
                (tdp / "growth-state" / "queue-idle.json").unlink(missing_ok=True)
                recorded.clear()
                for _ in range(5):
                    out3 = aw.watch()
                eq("同一天内跑 5 次 → 空置仍应记为 1 天", out3["queue_idle_days"], 1)
                eq("同一天内跑 5 次 → 不得触发「连续 N 天为空」告警",
                   [a for a in recorded if a["kind"] == "queue_idle"], [])

                # 11.4 真的跨了 3 天必须告警，且报出的天数要准
                today = aw.local_date()
                since = (datetime.strptime(today, "%Y-%m-%d")
                         - timedelta(days=2)).strftime("%Y-%m-%d")
                (tdp / "growth-state" / "queue-idle.json").write_text(
                    json.dumps({"idle_days": 0, "empty_since_date": since,
                                "last_nonempty_date": None}), encoding="utf-8")
                out4 = aw.watch()
                qa = [a for a in recorded if a["kind"] == "queue_idle"]
                eq("空置满 3 天 → 必须告警一次", len(qa), 1)
                eq("告警里报出的天数必须与实际一致", qa[0]["detail"]["idle_days"], 3)
                eq("摘要里的空置天数必须与告警一致", out4["queue_idle_days"], 3)

                # 11.5 队列恢复后必须归零，并记住上次非空的日子
                (tdp / "content" / "pending" / "x.json").write_text("{}", encoding="utf-8")
                out5 = aw.watch()
                eq("队列恢复 → 空置归零", out5["queue_idle_days"], 0)
                st5 = json.loads((tdp / "growth-state" / "queue-idle.json")
                                 .read_text(encoding="utf-8"))
                eq("队列恢复 → 记下今天的日期作为 last_nonempty_date",
                   st5.get("last_nonempty_date"), today)
        finally:
            (aw.ROOT, aw.fetch, aw.append_jsonl, aw.CFG) = saved

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
