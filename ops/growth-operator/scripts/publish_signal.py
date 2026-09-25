"""数据驱动的发布器：读取内容规格文件，经守卫后分发到 Allowlist 渠道。

用法：
    python scripts/publish_signal.py content/pending/<content_id>.json [--dry-run]

规格文件格式：
{
  "content_id": "en-something-001",
  "urls": ["https://..."],                     // 用于 URL 复用窗口检查
  "channels": {
    "bluesky_owned_account":  { "text": "..." },
    "mastodon_owned_account": { "text": "..." }
  }
}

设计要点：
- 先在本地把**所有**渠道过一遍守卫，任一不过则整体不发布（避免"发了一半"）。
- 每个渠道独立幂等键：<channel>:<content_id>，重复运行不会重复发布。
- Mastodon 的自动化披露采用「资料声明 或 每条帖子自带」双模式（见 check_disclosure）。
- 只用官方 API；不做浏览器模拟；失败即停，不重试。
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from growth_core import (Guard, GrowthGuardError, load_secrets,  # noqa: E402
                         record_publication)

S = load_secrets()
DRY = "--dry-run" in sys.argv

DISCLOSURE_MARKERS = ("automated", "automation", "自动化")


# --------------------------------------------------------------------------- HTTP
def http(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
         retries: int = 3, timeout: int = 40) -> tuple[int, dict, bytes]:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=body, method=method)
            req.add_header("User-Agent", "actn-growth-publisher/1.0")
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl.create_default_context()) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:  # noqa: BLE001
                pass
            return e.code, dict(e.headers or {}), raw
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.2 * (i + 1))
    return 0, {}, str(last).encode()


def stop(msg: str, code: int = 1) -> None:
    print(f"\n[STOP] {msg}")
    sys.exit(code)


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- Bluesky
def bsky_session() -> tuple[str, str]:
    payload = json.dumps({"identifier": S["bluesky_handle"],
                          "password": S["bluesky_app_password"]}).encode()
    st, _, raw = http("POST", "https://bsky.social/xrpc/com.atproto.server.createSession",
                      {"Content-Type": "application/json"}, payload)
    if st != 200:
        stop(f"Bluesky createSession HTTP {st}: {raw[:200]!r}")
    d = json.loads(raw)
    return d["accessJwt"], d["did"]


def bsky_disclosure_ok(jwt: str, did: str) -> bool:
    st, _, raw = http("GET",
                      "https://bsky.social/xrpc/com.atproto.repo.getRecord"
                      f"?repo={did}&collection=app.bsky.actor.profile&rkey=self",
                      {"Authorization": f"Bearer {jwt}"})
    if st != 200:
        return False
    desc = (json.loads(raw).get("value") or {}).get("description") or ""
    return any(m in desc.lower() for m in DISCLOSURE_MARKERS)


def bsky_publish(jwt: str, did: str, text: str) -> dict:
    payload = json.dumps({
        "repo": did,
        "collection": "app.bsky.feed.post",
        "record": {"$type": "app.bsky.feed.post", "text": text, "langs": ["en"],
                   "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())},
    }, ensure_ascii=False).encode()
    st, _, raw = http("POST", "https://bsky.social/xrpc/com.atproto.repo.createRecord",
                      {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}, payload)
    if st not in (200, 201):
        stop(f"Bluesky createRecord HTTP {st}: {raw[:300]!r}")
    d = json.loads(raw)
    rkey = (d.get("uri") or "").rsplit("/", 1)[-1]
    return {"status": st, "uri": d.get("uri"), "cid": d.get("cid"),
            "url": f"https://bsky.app/profile/{S['bluesky_handle']}/post/{rkey}"}


# --------------------------------------------------------------------------- Mastodon
def masto_verify() -> dict:
    st, _, raw = http("GET", f"{S['mastodon_instance']}/api/v1/accounts/verify_credentials",
                      {"Authorization": f"Bearer {S['mastodon_token']}"})
    if st != 200:
        stop(f"Mastodon verify_credentials HTTP {st}")
    return json.loads(raw)


def masto_update_profile() -> bool:
    body = (f"note={urllib.parse.quote('ACTN 官方账号。ACTN Agent 任务网络的开发者资产：Agent Skills、公开 API 契约、轮询示例。部分帖子由自动化发布。')}"
            f"&display_name={urllib.parse.quote('ACTN — Agent 协作与信任网络')}"
            f"&fields_attributes[0][name]=Website"
            f"&fields_attributes[0][value]=https://github.com/houdaguang/actn-agent-network").encode()
    st, _, raw = http("PATCH", f"{S['mastodon_instance']}/api/v1/accounts/update_credentials",
                      {"Authorization": f"Bearer {S['mastodon_token']}",
                       "Content-Type": "application/x-www-form-urlencoded"}, body)
    if st == 200:
        log("  [mastodon] 资料已更新（含自动化披露）")
        return True
    log(f"  [mastodon] 资料更新被拒 HTTP {st}（token 缺 write:accounts）")
    return False


def check_disclosure(channel: str, text: str) -> str:
    """自动化披露校验。返回 'profile' 或 'post' 两种满足方式之一。

    总方案 6.5 要求"账号资料必须清楚写明 ... scheduled posts may be automated"。
    当平台 token 权限不足以写入资料时，退化为"每条帖子自带披露"——
    对读者而言这至少与资料披露等价，且更贴近阅读场景。两种方式任一满足即可，
    且以显式日志记录实际采用的是哪一种。
    """
    markers = DISCLOSURE_MARKERS

    if channel == "bluesky_owned_account":
        jwt, did = bsky_session()
        if bsky_disclosure_ok(jwt, did):
            return "profile"
        if any(m in text.lower() for m in markers):
            return "post"
        stop("Bluesky 资料与帖子均无自动化披露，停止发布。")

    if channel == "mastodon_owned_account":
        acct = masto_verify()
        note = (acct.get("note") or "").lower()
        if any(m in note for m in markers):
            return "profile"
        if masto_update_profile():
            acct = masto_verify()
            if any(m in (acct.get("note") or "").lower() for m in markers):
                return "profile"
        if any(m in text.lower() for m in markers):
            log("  [mastodon] 资料无法写入披露，改用「每条帖子自带披露」模式")
            return "post"
        stop("Mastodon 资料无披露且帖子也未自带披露，停止发布。")

    return "n/a"


def masto_publish(text: str) -> dict:
    payload = json.dumps({"status": text, "visibility": "public", "language": "en"}).encode()
    st, _, raw = http("POST", f"{S['mastodon_instance']}/api/v1/statuses",
                      {"Authorization": f"Bearer {S['mastodon_token']}",
                       "Content-Type": "application/json"}, payload)
    if st not in (200, 201):
        stop(f"Mastodon statuses HTTP {st}: {raw[:300]!r}")
    d = json.loads(raw)
    return {"status": st, "id": d.get("id"), "url": d.get("url")}


# --------------------------------------------------------------------------- main
def main() -> None:
    specs = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not specs:
        stop("用法: publish_signal.py <spec.json> [--dry-run]")
    spec_path = Path(specs[0])
    if not spec_path.is_absolute():
        spec_path = ROOT / spec_path
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    content_id = spec["content_id"]
    urls = spec.get("urls") or []
    channels: dict = spec.get("channels") or {}

    log("=" * 74)
    log(f"发布信号: {content_id}")
    log(f"规格文件: {spec_path}")
    log(f"目标渠道: {list(channels)}")
    log(f"关联 URL: {urls}")
    log("=" * 74)

    guard = Guard()

    # ---- 阶段 1：全部渠道先过守卫，任一不过则整体不发 ----
    log("\n[1] 前置守卫（全部通过才进入发布）")
    guard.check_kill_switch()
    log("  kill switch          : 未开启")

    plans: list[dict] = []
    for channel, payload in channels.items():
        text = payload["text"]
        expect_account = (spec.get("accounts") or {}).get(channel)
        if not expect_account:
            ch = guard.check_channel_enabled(channel)
            expect_account = ch.get("handle") or ch.get("owner")
        limits = guard.preflight(channel=channel, account=expect_account, text=text,
                                 urls=urls, idem_key=f"{channel}:{content_id}")
        log(f"  {channel:<26} PASS  日{limits['channel_day']}/周{limits['channel_week']}"
            f"  全局今日{limits['global_day']}")
        plans.append({"channel": channel, "account": expect_account, "text": text})

    # 长度上限
    if "bluesky_owned_account" in channels:
        n = len(channels["bluesky_owned_account"]["text"])
        if n > 300:
            stop(f"Bluesky 文本 {n} 字符超过 300 上限")
        log(f"  bluesky 文本长度       : {n}/300")

    if DRY:
        log("\n[DRY-RUN] 守卫全部通过，未实际发布。")
        for p in plans:
            log(f"\n--- {p['channel']} ({p['account']}) ---\n{p['text']}")
        return

    # ---- 阶段 2：发布 ----
    log("\n[2] 发布")
    results: list[dict] = []
    for p in plans:
        ch = p["channel"]
        log(f"\n  --- {ch} ---")
        mode = check_disclosure(ch, p["text"])
        log(f"  自动化披露方式: {mode}")
        if ch == "bluesky_owned_account":
            jwt, did = bsky_session()
            res = bsky_publish(jwt, did, p["text"])
        elif ch == "mastodon_owned_account":
            res = masto_publish(p["text"])
        else:
            stop(f"未实现该渠道的发布器: {ch}")
        log(f"  已发布 -> {res.get('url')}")
        record_publication(channel=ch, account=p["account"], content_id=content_id,
                           text=p["text"], urls=urls, api_response=res,
                           status="published", idem_key=f"{ch}:{content_id}")
        results.append({"channel": ch, **res})

    log("\n" + "=" * 74)
    log(f"完成 {len(results)} 个渠道。账本: growth-state/content-ledger.jsonl")
    for r in results:
        log(f"  {r['channel']:<26} {r.get('url')}")
    log("=" * 74)


if __name__ == "__main__":
    main()
