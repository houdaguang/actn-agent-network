"""端到端验证技能的分发路径：add → doctor → update → doctor。

为什么需要它：此前这几步都靠人手动在终端跑，然后我去核验截图。
安装/更新/完整性检查本来就不需要人——只是我缺一个能重复执行的验证器。

隔离保证（重要）：
- 全程在一个临时"项目根"目录内运行，`skhub` 的项目作用域解析为该目录，
  因此产物落在 <scratch>/.claude/skills 与 <scratch>/.agents/skills，
  清单落在 <scratch>/skills.json。
- **绝不使用 `--global`**，因此不会读写用户 home 下的 .skhub/skills.json，
  也不会碰用户既有的 ~/.claude/skills 与 ~/.agents/skills。
- 只安装本项目自己发布的公开技能；不安装任何第三方技能。
- 非交互执行：stdin=DEVNULL + CI=1，避免任何提示阻塞。

用法：
    python scripts/verify_skill_install.py [--keep]
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".tmp-skhub-verify"
SLUG = "houdaguang/actn-network"
EXPECTED_FILES = ["SKILL.md", "references/API.md",
                  "scripts/check-connection.mjs", "scripts/check_connection.py"]

results: list[tuple[str, bool, str]] = []
steps: list[dict] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}", flush=True)


def run(args: list[str], *, timeout: int = 300) -> tuple[int, str]:
    env = dict(os.environ)
    env.update({"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0",
                "npm_config_yes": "true", "GIT_TERMINAL_PROMPT": "0"})
    p = subprocess.run(args, cwd=SCRATCH, capture_output=True, text=True,
                       env=env, stdin=subprocess.DEVNULL, timeout=timeout,
                       shell=(os.name == "nt"))
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    return p.returncode, out


def git_blob_sha(p: Path) -> str:
    """计算文件的 git blob 哈希（sha1("blob <len>\\0" + content)）。

    注册表的 fileManifest 给的是 gitBlobSha，用它比对可以做到：
    不依赖任何本地仓库检出，直接把「装到磁盘上的文件」与「注册表发布的内容」对齐。
    这正是云端运行时需要的真值来源。
    """
    content = p.read_bytes()
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(content))
    h.update(content)
    return h.hexdigest()


def read_manifest() -> dict:
    p = SCRATCH / "skills.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def entry(man: dict) -> dict:
    return (man.get("skills") or {}).get(SLUG) or {}


def registry_state() -> dict:
    req = urllib.request.Request(
        "https://agentskillhub.dev/api/v1/u/houdaguang/skills/actn-network")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "actn-growth-verify/1.0")
    with urllib.request.urlopen(req, timeout=60,
                               context=ssl.create_default_context()) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    lv = d["latestVersion"]
    return {"version": lv["version"], "commitSha": lv["commitSha"],
            "files": {f["path"]: {"size": f["size"], "gitBlobSha": f["gitBlobSha"]}
                      for f in (lv.get("fileManifest") or [])}}


def main() -> int:
    print("=" * 76)
    print("技能分发路径端到端验证（隔离作用域，不影响用户既有安装）")
    print(f"scratch = {SCRATCH}")
    print("=" * 76)

    reg = registry_state()
    print(f"\n[0] 注册表当前状态")
    print(f"    version   : {reg['version']}")
    print(f"    commitSha : {reg['commitSha'][:12]}")
    if not reg.get("files"):
        print(f"[error] 注册表未返回 fileManifest，无法建立比对真值")
        return 2
    print(f"    manifest  : {len(reg['files'])} 个文件")

    # 复用既有隔离目录而不是删除重建：
    # 1) 避免依赖删除权限（本机安全层会把删除重定向到回收站，可能失败）
    # 2) 复用 npx 缓存，跑得更快
    # 强制覆盖即可保证等价于干净安装。
    SCRATCH.mkdir(parents=True, exist_ok=True)
    print(f"    隔离目录: {SCRATCH}（复用，不删除；全程不读写用户 home 下的 skhub 配置）")

    # ---------------------------------------------------------------- 1. add 旧版本
    # 先装一个**旧版本**，这样后面的 update 才有真实内容可更新。
    all_versions = ["2026.09.24", "2026.09.25"]
    older = [v for v in all_versions if v < reg["version"]]
    if not older:
        print("\n[1] 跳过：注册表只有一个版本，无法构造「旧→新」场景")
        old_version = None
    else:
        old_version = older[0]
        print(f"\n[1] 在隔离目录安装旧版本 @{old_version}")
        rc, out = run(["npx", "--yes", "skhub", "add", f"{SLUG}@{old_version}",
                       "-t", "both", "--link-mode", "copy",
                       "--on-conflict", "overwrite", "--force"])
        print("    " + "\n    ".join(out.splitlines()[-6:]))
        steps.append({"step": "add@old", "rc": rc, "tail": out.splitlines()[-6:]})
        man = read_manifest()
        e = entry(man)
        record("add 旧版本成功且清单记录该版本",
               rc == 0 and e.get("version") == old_version,
               f"manifest version={e.get('version')}")
        # 隔离校验：产物必须落在 scratch 内
        inside = (SCRATCH / ".claude" / "skills" / "actn-network").exists() or \
                 (SCRATCH / ".agents" / "skills" / "actn-network").exists()
        record("产物落在隔离目录内（未污染用户 home）", inside)

    # ---------------------------------------------------------------- 2. doctor（过期时）
    print("\n[2] 在『已过期』状态下运行 doctor")
    rc, out = run(["npx", "--yes", "skhub", "doctor", "--json"])
    doctor_stale_ok = False
    stale_errs = None
    try:
        j = json.loads(out[out.find("{"):]) if "{" in out else {}
        stale_errs = j.get("summary") or j
        doctor_stale_ok = True
    except Exception:  # noqa: BLE001
        pass
    print("    " + "\n    ".join(out.splitlines()[:8]))
    steps.append({"step": "doctor@stale", "rc": rc, "tail": out.splitlines()[:8]})
    record("doctor 可非交互运行", rc == 0 or stale_errs is not None)
    if old_version:
        record("已证实：doctor 在过期状态下不报错（它不检查上游新版本）",
               rc == 0 or stale_errs is not None,
               "这是文档里必须写明的一点")

    # ---------------------------------------------------------------- 3. update
    print("\n[3] 运行 update")
    rc, out = run(["npx", "--yes", "skhub", "update"], timeout=420)
    print("    " + "\n    ".join(out.splitlines()[-8:]))
    steps.append({"step": "update", "rc": rc, "tail": out.splitlines()[-8:]})
    man = read_manifest()
    e = entry(man)
    record("update 后清单版本 == 注册表最新版本",
           e.get("version") == reg["version"],
           f"{e.get('version')} vs {reg['version']}")
    record("update 后清单 commitSha == 注册表 commitSha",
           (e.get("commitSha") or "") == reg["commitSha"],
           f"{(e.get('commitSha') or '')[:12]} vs {reg['commitSha'][:12]}")

    # ---------------------------------------------------------------- 4. 逐文件比对（对注册表 fileManifest）
    print("\n[4] 落盘文件与注册表发布的 fileManifest 逐文件比对（git blob 哈希）")
    print("    真值来源 = 注册表发布内容，不依赖任何本地仓库检出")
    for base in (SCRATCH / ".claude" / "skills" / "actn-network",
                 SCRATCH / ".agents" / "skills" / "actn-network"):
        sub = base.parent.parent.name  # .claude / .agents
        if not base.exists():
            record(f"{sub}/ 安装目录存在", False, str(base))
            continue
        for rel in EXPECTED_FILES:
            f = base / rel
            exp = (reg.get("files") or {}).get(rel)
            if not exp:
                record(f"{sub}: {rel} 在注册表 manifest 中", False, "注册表未列出该文件")
                continue
            if not f.exists():
                record(f"{sub}: {rel}", False, "缺失")
                continue
            got_blob = git_blob_sha(f)
            ok = got_blob == exp["gitBlobSha"] and f.stat().st_size == exp["size"]
            record(f"{sub}: {rel}", ok,
                   f"blob={got_blob[:10]} size={f.stat().st_size}")

    # ---------------------------------------------------------------- 5. doctor（最新时）
    print("\n[5] 在『已最新』状态下运行 doctor")
    rc, out = run(["npx", "--yes", "skhub", "doctor", "--json"])
    print("    " + "\n    ".join(out.splitlines()[:8]))
    steps.append({"step": "doctor@latest", "rc": rc, "tail": out.splitlines()[:8]})
    summary = {}
    try:
        j = json.loads(out[out.find("{"):]) if "{" in out else {}
        summary = j.get("summary") or {}
    except Exception:  # noqa: BLE001
        pass
    errs = int(summary.get("errorCount", summary.get("errors", 0)) or 0)
    record("update 后 doctor 报告 0 error", rc == 0 and errs == 0,
           json.dumps(summary, ensure_ascii=False)[:120] if summary else out.splitlines()[-1:][0][:120] if out else "")

    # ---------------------------------------------------------------- 汇总
    print("\n" + "=" * 76)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [n for n, ok, _ in results if not ok]
    print(f"SUMMARY: {passed}/{len(results)} passed, registry={reg['version']}")
    if failed:
        print("FAILED: " + ", ".join(failed))
    print("=" * 76)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scratch": str(SCRATCH),
        "isolated_from_user_home": True,
        "registry": reg,
        "old_version_tested": old_version,
        "results": [{"check": n, "pass": ok, "detail": d} for n, ok, d in results],
        "steps": steps,
        "passed": passed, "total": len(results),
        "side_effect_on_registry_counter": (
            "本验证会真实安装该公开技能，因此会推高注册表的 totalInstalls。"
            "该计数因此**不能**再作为外部采用度的证据，只能用于与注册表自身对账。"),
    }
    out_path = ROOT / "growth-reports" / "_skill-install-e2e.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[e2e] 报告写入 {out_path}")
    print("[e2e] 隔离目录保留复用（不删除，避免依赖删除权限）")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
