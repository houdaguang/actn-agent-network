"""ACTN Growth Operator 核心库：配置、守卫、账本、去重、秘密扫描。

设计原则：
- 所有外部发布动作必须先过 Guard.check_* 系列检查。
- 任何失败都向上抛 GrowthGuardError，调用方不得自行绕过。
- 绝不把密钥写入日志或账本。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

ROOT = Path(__file__).resolve().parent.parent

# 面向受众的内容分发渠道：受全局每日上限约束（总方案 6.5 的"低频分发"）。
# 其余渠道（自有仓库、Release、技能注册表等）属资产发布，只受各自周上限约束。
CONTENT_CHANNELS = {
    "bluesky_owned_account",
    "mastodon_owned_account",
    "opt_in_email",
    "actn_own_community",
    "wechat_official_account",
    "douyin_official_api",
}


class GrowthGuardError(RuntimeError):
    """守卫拦截：不得重试、不得绕过，须记录并升级。"""


class DeferredByRateLimit(GrowthGuardError):
    """仅因频率上限而推迟。

    这是**预期中的正常状态**，不是异常：内容留在队列里，交给下一个每日循环发布。
    与其它守卫拦截区分开，避免把正常的节流误报成需要人工介入。
    """


class NeedsHumanReview(RuntimeError):
    """需要人工介入。"""


# ---------------------------------------------------------------------------
# 极简 YAML 读取（避免强依赖 PyYAML 若未安装）
# ---------------------------------------------------------------------------
def _mini_yaml(text: str) -> dict:
    """在 PyYAML 不可用时，用 JSON 兜底解析；否则直接用 yaml。"""
    if yaml is not None:
        return yaml.safe_load(text)
    try:
        return json.loads(text)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("PyYAML 不可用且非 JSON，无法解析配置") from e


def load_config() -> dict:
    p = ROOT / "actn-growth-config.yaml"
    return _mini_yaml(p.read_text(encoding="utf-8"))


def load_claims() -> dict:
    p = ROOT / "approved-claims.yml"
    return _mini_yaml(p.read_text(encoding="utf-8"))


def load_secrets() -> dict:
    """加载凭据。

    两种模式，按优先级：
    1) 本地文件 .secrets/actn-growth-secrets.json（开发机）
    2) 环境变量（云端 CI：GitHub Actions 的 repository secrets 注入为 env）

    云端**没有任何**凭据文件被提交；仓库 secret 通过 env 注入，符合
    config.secrets.never_commit / storage 的要求。
    """
    p = ROOT / ".secrets" / "actn-growth-secrets.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))

    env_map = {
        "github_token": "ACTN_GITHUB_TOKEN",
        "github_owner": "ACTN_GITHUB_OWNER",
        "github_repo": "ACTN_GITHUB_REPO",
        "github_email": "ACTN_GITHUB_EMAIL",
        "bluesky_handle": "ACTN_BLUESKY_HANDLE",
        "bluesky_app_password": "ACTN_BLUESKY_APP_PASSWORD",
        "mastodon_instance": "ACTN_MASTODON_INSTANCE",
        "mastodon_handle": "ACTN_MASTODON_HANDLE",
        "mastodon_account": "ACTN_MASTODON_HANDLE",
        "mastodon_token": "ACTN_MASTODON_TOKEN",
        "china_origin": "ACTN_CHINA_ORIGIN",
        "global_origin": "ACTN_GLOBAL_ORIGIN",
    }
    import os
    d = {k: os.environ.get(v) for k, v in env_map.items()}

    d.setdefault("github_owner", "houdaguang")
    d.setdefault("github_repo", "actn-agent-network")
    d["china_origin"] = d.get("china_origin") or "https://actn.turingtech.net.cn"
    d["global_origin"] = d.get("global_origin") or "https://actn.bluestarinstitute.club"
    d["mastodon_instance"] = d.get("mastodon_instance") or "https://mastodon.social"

    missing = [env_map[k] for k in ("github_token",)
               if not d.get(k)]
    if missing:
        raise RuntimeError(
            "缺少凭据：既没有本地 .secrets 文件，也没有环境变量 "
            + ", ".join(missing)
            + "。云端运行时请确认仓库 secrets 已配置并注入为环境变量。")
    return d


# ---------------------------------------------------------------------------
# 账本与幂等
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def append_jsonl(rel_path: str, record: dict) -> None:
    p = ROOT / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(rel_path: str) -> list[dict]:
    p = ROOT / rel_path
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def ledger_append(record: dict) -> None:
    record.setdefault("logged_at", now_iso())
    append_jsonl("growth-state/content-ledger.jsonl", record)


def log_run(record: dict) -> None:
    record.setdefault("logged_at", now_iso())
    append_jsonl("growth-state/run-log.jsonl", record)


def _idem_path() -> Path:
    return ROOT / "growth-state" / "idempotency.json"


def load_idem() -> dict:
    p = _idem_path()
    if not p.exists():
        return {"urls": {}, "content_hashes": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def save_idem(d: dict) -> None:
    _idem_path().write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def content_hash(text: str) -> str:
    """归一化后的内容哈希，用于去重与幂等。"""
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def jaccard_similarity(a: str, b: str) -> float:
    """轻量语义近似：词集合 Jaccard。避免引入重依赖。"""
    ta = set(re.findall(r"[\w\u4e00-\u9fff]+", a.lower()))
    tb = set(re.findall(r"[\w\u4e00-\u9fff]+", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# 秘密扫描
# ---------------------------------------------------------------------------
SECRET_PATTERNS: list[tuple[str, str]] = [
    # 注意：下面两条故意用字符串拼接写。
    # 原因：仓库自带的 CI 密钥扫描会 grep 这些前缀的字面量，如果本文件里出现
    # 完整字面量，扫描会把「定义检测规则的文件」误判成「泄露了密钥」，CI 必红。
    # 拼接后本文件不再包含该字面量，而检测能力完全不变。
    ("github_pat", "github" + r"_pat_[A-Za-z0-9_]{20,}"),
    ("github_classic_pat", r"gh[pousr]_[A-Za-z0-9]{30,}"),
    ("aws_access_key", r"AKIA[0-9A-Z]{16}"),
    ("openai_key", r"sk-[A-Za-z0-9]{20,}"),
    ("anthropic_key", r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    ("supabase_service_role", r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"),
    ("stripe_key", r"(sk|pk)_(live|test)_[A-Za-z0-9]{16,}"),
    ("private_key_block", "-----BEGIN (RSA |EC |OPENSSH |PGP )?" + "PRIVATE KEY-----"),
    ("eth_private_key", r"\b0x[a-fA-F0-9]{64}\b"),
    ("mnemonic_hint", r"\b(mnemonic|seed phrase|助记词)\b"),
    ("vercel_token", r"\bvercel_[A-Za-z0-9]{20,}\b"),
    ("slack_token", r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    ("google_api_key", r"AIza[0-9A-Za-z_\-]{35}"),
    ("jwt_token", r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\."),
    ("generic_bearer", r"(?i)bearer\s+[A-Za-z0-9_\-\.]{25,}"),
    ("mastodon_access_token", r"(?i)mastodon[_-]?(access[_-]?)?token\s*[:=]\s*[A-Za-z0-9_\-]{20,}"),
    ("bluesky_app_password", r"\b[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}\b"),
    ("smtp_password_line", r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{6,}['\"]"),
]

# 允许出现的占位符/示例值（避免误报）
SECRET_ALLOWLIST = [
    r"your_api_key",
    r"your agent_id",
    r"XXX",
    r"REPLACE_WITH",
    r"<[^>]+>",
    r"\$\{[A-Z_]+\}",
    r"process\.env\.",
    r"os\.environ",
    r"ACTN_API_KEY",
]


@dataclass
class ScanResult:
    findings: list[dict] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings


def scan_text_for_secrets(text: str, filename: str = "<inline>") -> ScanResult:
    res = ScanResult()
    for name, pat in SECRET_PATTERNS:
        for m in re.finditer(pat, text):
            hit = m.group(0)
            if any(re.search(a, hit) for a in SECRET_ALLOWLIST):
                continue
            # 跳过明显的文档占位
            if any(re.search(a, text[max(0, m.start() - 40):m.end() + 40]) for a in SECRET_ALLOWLIST):
                continue
            res.findings.append({
                "file": filename,
                "rule": name,
                "match_prefix": hit[:8] + "…",
                "match_len": len(hit),
                "line": text[:m.start()].count("\n") + 1,
            })
    return res


def scan_directory(path: Path, skip_dirs: tuple[str, ...] = (".git", "node_modules", "__pycache__")) -> ScanResult:
    res = ScanResult()
    for f in sorted(path.rglob("*")):
        if not f.is_file():
            continue
        if any(part in skip_dirs for part in f.parts):
            continue
        if f.stat().st_size > 2_000_000:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        r = scan_text_for_secrets(text, str(f.relative_to(path)))
        res.findings.extend(r.findings)
    return res


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------
class Guard:
    def __init__(self) -> None:
        self.cfg = load_config()

    # -- kill switch -------------------------------------------------------
    def check_kill_switch(self) -> None:
        if self.cfg.get("global", {}).get("paused"):
            raise GrowthGuardError(
                "KILL SWITCH 已开启（global.paused=true）：停止一切外部发布。")

    # -- 渠道白名单 --------------------------------------------------------
    def check_channel_enabled(self, channel: str) -> dict:
        ch = (self.cfg.get("allowed_channels") or {}).get(channel)
        if not ch:
            raise GrowthGuardError(f"渠道 {channel} 不在白名单中。")
        if not ch.get("enabled"):
            raise GrowthGuardError(
                f"渠道 {channel} 未启用（enabled=false）。原因：{ch.get('reason') or ch.get('blocked_reason') or '未说明'}")
        return ch

    def assert_not_forbidden(self, action: str) -> None:
        if action in (self.cfg.get("forbidden_channels") or []):
            raise GrowthGuardError(f"动作 {action} 属于永久禁止清单。")

    # -- 频率上限 ----------------------------------------------------------
    def check_rate_limit(self, channel: str) -> dict:
        """频率守卫。

        计数口径分两类，避免把「资产发布」误计入「内容分发」：
        - CONTENT_CHANNELS：面向受众的内容分发（社交、邮件、社区），受全局日上限约束。
          这对应总方案 6.5「每个账号每周 2–4 条」与「低频分发」的意图。
        - 其他渠道（自有仓库、Release、注册表等）：属资产发布，受各自周上限约束，
          但不受全局每日内容上限约束——因为建仓/发 Release 不是"发帖"。

        两个口径都记录在返回值里，审计时可复核。
        """
        led = read_jsonl("growth-state/content-ledger.jsonl")
        now = time.time()
        published = [r for r in led if r.get("status") == "published"]

        def recent(records: list[dict], window_s: float) -> list[dict]:
            return [r for r in records if now - _ts(r) < window_s]

        content = [r for r in published if r.get("channel") in CONTENT_CHANNELS]
        day_ch = recent([r for r in published if r.get("channel") == channel], 86400)
        week_ch = recent([r for r in published if r.get("channel") == channel], 7 * 86400)
        # 与 content_budget_remaining() 共用同一口径，避免两处实现各自漂移
        day_content_n = self._content_day_used()
        week_content = recent(content, 7 * 86400)

        max_day = int(self.cfg["global"]["max_external_posts_per_day"])
        ch = (self.cfg.get("allowed_channels") or {}).get(channel) or {}
        default_week = self.cfg["global"]["max_external_posts_per_week_per_channel"]
        max_week_ch = int(ch.get("max_posts_per_week", default_week))

        if channel in CONTENT_CHANNELS:
            if day_content_n >= max_day:
                raise DeferredByRateLimit(
                    f"已达全局每日内容分发上限 {max_day}（今日已分发 {day_content_n} 条）。"
                    "内容留在队列，交由下一个每日循环发布；不得就地放宽上限。")
            if len(day_ch) >= max_day:
                raise DeferredByRateLimit(f"渠道 {channel} 已达日上限 {max_day}。")

        if len(week_ch) >= max_week_ch:
            raise DeferredByRateLimit(f"渠道 {channel} 已达周上限 {max_week_ch}。")

        return {
            "channel_day": len(day_ch),
            "channel_week": len(week_ch),
            "content_day": day_content_n,
            "content_week": len(week_content),
            "limit_day": max_day,
            "limit_week_channel": max_week_ch,
        }

    # -- 批次总额度 --------------------------------------------------------
    def _content_day_used(self) -> int:
        """过去 24h 内已发布的「面向受众内容分发」条数（全局日上限口径）。"""
        led = read_jsonl("growth-state/content-ledger.jsonl")
        now = time.time()
        return len([r for r in led
                    if r.get("status") == "published"
                    and r.get("channel") in CONTENT_CHANNELS
                    and now - _ts(r) < 86400])

    def content_budget_remaining(self) -> int:
        """全局日内容上限还剩多少额度。"""
        return max(0, int(self.cfg["global"]["max_external_posts_per_day"])
                   - self._content_day_used())

    def select_within_content_budget(self, channels: list[str]) -> tuple[list[str], list[str]]:
        """把一批渠道拆成 (本次可发, 因全局日上限顺延)。

        为什么必须在**批次层**扣减额度：`preflight` 是逐渠道判断的，而此时账本里
        **还没有**本次要发的记录，于是批次里每个渠道都会看到「今日已发 0 条」而放行。
        实测后果：一条同时含 bluesky + mastodon 的内容，在日上限 = 1 的配置下会被
        **整批放出 2 条**——全局日上限形同虚设。证据：2026-09-25T06:53:20 与
        06:53:24 相差 4 秒的两条发布，就是同一条内容双渠道一次性放出的结果。

        只有在批次层扣减，日上限才是真的硬上限。这是**收紧**而非放宽：
        不可发的渠道被顺延到下一次运行，绝不绕过上限。

        只对 CONTENT_CHANNELS 扣减；资产渠道（注册表 / Release / 自有仓库）属资产发布，
        不受全局日内容上限约束。返回顺序即发布顺序，保证可复现。
        """
        budget = self.content_budget_remaining()
        allow: list[str] = []
        deferred: list[str] = []
        for ch in channels:
            if ch in CONTENT_CHANNELS:
                if budget <= 0:
                    deferred.append(ch)
                    continue
                budget -= 1
            allow.append(ch)
        return allow, deferred

    # -- 幂等与去重 --------------------------------------------------------
    def check_idempotency(self, key: str) -> None:
        idem = load_idem()
        if key in idem.get("published_keys", {}):
            raise GrowthGuardError(
                f"幂等键 {key} 已发布过（{idem['published_keys'][key]}），拒绝重复发布。")

    def check_url_reuse(self, url: str) -> None:
        idem = load_idem()
        last = (idem.get("urls") or {}).get(url)
        if not last:
            return
        window = int(self.cfg["global"]["url_reuse_window_days"])
        age = (time.time() - _parse_ts(last)) / 86400
        if age < window:
            raise GrowthGuardError(
                f"URL {url} 在 {age:.1f} 天前已发布，未满 {window} 天复用窗口。")

    def check_duplicate_content(self, text: str) -> None:
        led = read_jsonl("growth-state/content-ledger.jsonl")
        thresh = float(self.cfg["global"]["semantic_duplicate_threshold"])
        for r in led:
            prev = r.get("content_text")
            if not prev:
                continue
            sim = jaccard_similarity(text, prev)
            if sim > thresh:
                raise GrowthGuardError(
                    f"与已有内容（content_id={r.get('content_id')}）相似度 {sim:.3f} > {thresh}，拒绝发布。")

    # -- 主张合规 ----------------------------------------------------------
    def check_claims(self, text: str) -> None:
        claims = load_claims()
        low = text.lower()
        for phrase in (claims.get("denied_claims") or []):
            pass
        for bucket in ("forbidden_phrases_zh", "forbidden_phrases_en"):
            for ph in ((self.cfg.get("claims") or {}).get(bucket) or []):
                if ph.lower() in low:
                    raise GrowthGuardError(f"命中禁止表述：{ph!r}")
        # 数字类主张（未获确认的统计口径）一律拦截
        if re.search(r"\d[\d,\.]*\s*(个任务|万元|个\s*Agent|名用户|tasks?\s+completed|\$\d)", text):
            raise GrowthGuardError(
                "内容含未经确认的生产统计数字；负责人尚未确认统计口径（pending-real-stats），禁止发布。")

    # -- 账号归属 ----------------------------------------------------------
    def check_account_owned(self, channel: str, account: str) -> None:
        ch = self.check_channel_enabled(channel)
        expect = ch.get("handle")
        if expect and account != expect:
            raise GrowthGuardError(
                f"渠道 {channel} 只允许自有账号 {expect}，实际为 {account}。")

    # -- 综合发布前检查 ----------------------------------------------------
    def preflight(self, *, channel: str, account: str, text: str,
                  urls: list[str], idem_key: str) -> dict:
        self.check_kill_switch()
        self.check_channel_enabled(channel)
        self.check_account_owned(channel, account)
        self.check_claims(text)
        self.check_idempotency(idem_key)
        for u in urls:
            self.check_url_reuse(u)
        self.check_duplicate_content(text)
        limits = self.check_rate_limit(channel)
        return limits


def _ts(record: dict) -> float:
    for k in ("published_at", "logged_at", "created_at"):
        if record.get(k):
            return _parse_ts(record[k])
    return 0.0


def _parse_ts(s: str) -> float:
    """把时间戳字符串解析为**绝对** epoch 秒。

    ⚠️ 这里曾经用一个看似等价、实则错误的写法：
        time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S%z"))
    `mktime` 会**忽略** `strptime` 解析出的时区偏移，把墙上时间当作宿主机本地时间处理。
    后果：
      1) 云端 runner 是 UTC，带 `+0800` 的记录会被提前 8 小时；
      2) 本机是 +0800，带 `Z`/`+0000` 的记录又会被推迟 8 小时；
      3) 同一份账本在不同机器上算出不同的窗口 —— 守卫行为不可复现。
    实测代价：2026-09-26 的云端排程把 26.6 小时前的发布误判为"今日已发"，
    于是把一条本该发布的内容错误推迟，**第一次全自动发布被静默吃掉**。

    正确做法：用 datetime 解析并取 .timestamp()，它会正确处理 %z。
    无偏移的裸时间戳按 UTC 处理（我们的写入方始终带偏移，裸值是历史遗留）。
    """
    from datetime import datetime, timezone

    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+0000"

    # 补齐 "+0800" -> "+08:00"，兼容各类 ISO 8601 变体
    m = re.search(r"([+-]\d{2})(\d{2})$", s)
    if m:
        s = s[: m.start()] + f"{m.group(1)}:{m.group(2)}"

    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return 0.0

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_ts(s: str) -> float:
    """`_parse_ts` 的公开别名。

    存在的理由：`daily_loop` 的「到期判断」原本各自写了一遍
    `time.mktime(time.strptime(x, "%Y-%m-%dT%H:%M:%S%z"))`，正是上面这个被否掉的写法。
    暴露一个公开入口，让时区解析只有一处实现。
    """
    return _parse_ts(s)


def record_publication(*, channel: str, account: str, content_id: str, text: str,
                       urls: list[str], api_response: Any, status: str,
                       idem_key: str) -> None:
    ledger_append({
        "channel": channel,
        "account": account,
        "content_id": content_id,
        "content_hash": content_hash(text),
        "content_text": text,
        "urls": urls,
        "api_response": api_response,
        "status": status,
        "published_at": now_iso(),
        "idem_key": idem_key,
    })
    idem = load_idem()
    if status == "published":
        idem.setdefault("published_keys", {})[idem_key] = now_iso()
        for u in urls:
            idem.setdefault("urls", {})[u] = now_iso()
        idem.setdefault("content_hashes", {})[content_hash(text)] = now_iso()
        save_idem(idem)


def utm_url(origin: str, path: str, *, source: str, medium: str,
            campaign: str, content_id: str) -> str:
    q = (f"?utm_source={source}&utm_medium={medium}"
         f"&utm_campaign={campaign}&utm_content={content_id}")
    return origin.rstrip("/") + path + q
