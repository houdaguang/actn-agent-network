# Growth Operator (cloud-run)

This directory contains the scheduled growth operations for the ACTN developer assets. It runs entirely in **GitHub Actions** — it does not depend on any local machine being switched on.

- Daily safe growth loop — `.github/workflows/growth-daily.yml` (09:20 Asia/Shanghai)
- Weekly metrics collection — `.github/workflows/growth-weekly-metrics.yml` (Mondays 08:50 Asia/Shanghai)

## Why it lives in this repository

It needs a repository it can be scheduled from. This is the one the operations were authorised against, so the loop lives here rather than in a separate private repository.

It is kept in a single directory (`ops/growth-operator/`) and the scripts resolve all their paths relative to their own parent directory, so the state they write stays inside this folder and does not spill into the repository root.

## What the loop does

Each daily run, in order:

1. **Kill switch** — stop immediately if `global.paused` is set.
2. **Site health** — read-only checks against `robots.txt`, `sitemap.xml`, `feed.xml`, `skill.md` and a key landing page on both sites.
3. **Signal refresh** — new commits, open issues, releases, the published skill version, and the platform's public read-only counters.
4. **Queue publish** — take at most **one** item from `content/pending/`, run the full guard set, and publish it. Nothing is published if nothing new and verifiable exists.
5. **Distribution path verification** — weekly: install a pinned older version of the skill in an isolated scope, run `doctor`, run `update`, and assert the result against the registry's published manifest.
6. **Audit** — append to the ledger and run log, then report a fixed `RUN_STATUS`.

## The guards, and why they are not decorative

Every publish attempt passes all of these or it is refused:

| Guard | Purpose |
|---|---|
| Kill switch | Global stop. |
| Channel allowlist | Only channels explicitly enabled are reachable. |
| Account ownership | Refuses to post to anything that is not the configured own account. |
| Rate limits | Global per-day cap on content distribution, per-channel weekly caps. |
| Idempotency keys | The same content can never be published twice. |
| URL reuse window | The same URL is not re-posted within 30 days. |
| Content similarity | Rejects near-duplicates of anything in the last 90 days. |
| Forbidden phrasing | Blocks income-promise language in Chinese and English. |
| Unverified statistics | Blocks numeric platform claims, because their accounting basis is unconfirmed. |
| Automation disclosure | Refuses to publish from an account that does not disclose automated posting. |

A rate-limit deferral is treated as **normal scheduling**, not a fault: the item stays in the queue for the next run. Only genuine problems are escalated.

## Credentials

Nothing secret is in this tree. The workflow reads three repository secrets:

| Secret | Scope |
|---|---|
| `ACTN_GH_PAT` | Read-only across this repository's own metadata (commits, issues, releases, traffic) |
| `ACTN_BLUESKY_APP_PASSWORD` | The Bluesky account created for this purpose only |
| `ACTN_MASTODON_TOKEN` | The Mastodon account created for this purpose only |

Deliberately **not** held: production database service-role keys, payment or wallet private keys, Vercel tokens, or any GitHub token reaching other product lines. Every credential here is one created for this operation, so a leak cannot reach production data or funds.

## What this deliberately does not do

- No posting into other people's repositories, issues, or communities.
- No automated follows, likes, stars, votes, or install-count inflation.
- No cold or scraped email. The email channel is blocked outright.
- No browser automation against any external platform.
- No published count, payout, or user statistic that has not been verified.

## Honest limits

- **Site-side funnel instrumentation is missing.** It would require changing the production web application, which is outside this operation's authorisation. Funnel metrics therefore read as "unavailable" and must never be substituted with a proxy and presented as if it were the real thing.
- **The registry's install counter is contaminated.** The weekly distribution-path verification really does install the skill, so that number includes our own testing and cannot be used as evidence of adoption.
- **Content generation is not here.** Drafting genuinely new, evidence-backed content needs a language model, which a scheduled shell job is not. This directory handles everything mechanical; new drafts are added to `content/pending/` and this loop publishes them subject to the caps.

## Manual trigger

```bash
gh workflow run growth-daily.yml -R houdaguang/actn-agent-network
gh workflow run growth-daily.yml -R houdaguang/actn-agent-network -f dry_run=true
gh workflow run growth-weekly-metrics.yml -R houdaguang/actn-agent-network
```
