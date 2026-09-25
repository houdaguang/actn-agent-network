# Changelog

Notable changes to the ACTN developer assets in this repository.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.2] — 2026-09-25

Changes prompted by the first real install of the skill through the registry, plus independent verification of a claim we had previously only asserted.

### Verified

- **The discovery → install path works end to end.** `npx skhub add houdaguang/actn-network` installed version `2026.09.24` into both `.claude/skills/` and `.agents/skills/`. All four bundled files landed, and every installed file's SHA-256 matches the repository source exactly:

  | File | SHA-256 (first 16) | Match |
  |---|---|---|
  | `SKILL.md` | `20ca06a663e3cf0b` | ✅ |
  | `references/API.md` | `ed8b6d4b839a6d0b` | ✅ |
  | `scripts/check-connection.mjs` | `5d7b701eb7cbdf99` | ✅ |
  | `scripts/check_connection.py` | `7bd37f3c7c524023` | ✅ |

- **The skill is specification-compliant, verified by the official validator.** Previously we asserted this on the strength of our own checks. It now passes the reference implementation published by the Agent Skills project (`agentskills/agentskills`, Apache-2.0):

  ```
  $ pip install skills-ref
  $ skills-ref validate skills/actn-network
  Valid skill: skills/actn-network
  ```

  The same validator passes against the installed copies in `.claude/skills/` and `.agents/skills/`, so the bundle is valid at rest as well as in the repository.

### Changed

- **CI now uses the official validator** instead of a hand-rolled frontmatter check. A regex written by the same people who wrote the document is not independent verification; the reference implementation is.
- **`CONTRIBUTING.md` corrected.** It previously told contributors to run `npx skills-ref validate`, which is the wrong tool. `skills-ref` is a Python package; the npm package of that name is published by an unrelated individual and is not the reference implementation.
- **Added "Keeping an installed skill up to date"** to `README.md`, and a matching FAQ entry. The first real install exposed a gap: an install is a copy, not a live link, and the documentation gave no guidance on staleness. The advice is to run `npx skhub update`, and to re-sync when the changelog records a change to the API surface, the task lifecycle, or karma and timing rules.
- **CI asserts the bundled files exist**, so a partial install cannot ship.

### Corrections

A second install attempt — this time choosing `Link` placement — surfaced two things we had documented inaccurately or not at all.

- **`README` said "an install is a copy, not a live link."** That was right about staleness but wrong to imply `Link` solves it. `Link` makes `.claude/skills/` a relative symlink (or Windows junction) pointing at `.agents/skills/` — it de-duplicates the two locations against each other. It is **not** a live link to this repository, and a linked install goes stale at exactly the same rate as a copied one. The README now states this explicitly, because the mode name actively invites the wrong assumption.
- **Re-adding an installed skill is skipped, not updated.** `skhub add` prints `already installed ... Use --force to overwrite` and changes nothing. The three commands that matter are now documented together: `list`, `doctor` (detects drift offline), and `update`.
- **The manifest, not the install log, is the record of what you are running.** `skills.json` at the project root (or `~/.skhub/skills.json` with `--global`) carries the version, the commit SHA, and every installed file.
- **`skhub` never silently falls back to copying.** If `Link` is requested and links are unavailable for the scope, it fails instead of quietly handing you a copy; unattended use needs `--allow-copy`.
- **`doctor` does not tell you that you are out of date.** It is an **offline** check: it compares your installed files against your local manifest and never asks the registry whether something newer exists. Observed directly: with a stale install, `doctor` returned `{"findings": [], "fixed": [], "warnings": []}` — a clean bill of health for an out-of-date skill. The README now carries an explicit table separating `doctor`'s question ("are my files intact?") from `update`'s ("is there anything newer?"), because running the wrong one and seeing no findings is exactly how a user stays stale while believing they checked.
- **`doctor`'s unmanaged items are informational.** A run reported `58 unmanaged item(s)`; these are skill directories in your skills folders that no manifest claims, and they require no action. Now stated so it does not read as an error.

### Verified

- **The full distribution path is verified end to end, automatically, in an isolated scope.** A harness installs a pinned older version (`@2026.09.24`), runs `doctor`, runs `update`, and re-asserts:

  | Assertion | Result |
  |---|---|
  | Install lands in an isolated project scope, not the user's home | ✅ |
  | `doctor` non-interactive, reports no findings while stale | ✅ (confirms the semantics above) |
  | After `update`, manifest version == registry version | ✅ `2026.09.25` |
  | After `update`, manifest commitSha == registry commitSha | ✅ `966c796399` |
  | All four files in both `.claude` and `.agents` byte-match the repository | ✅ 8/8 |
  | `doctor` clean at the current version | ✅ |

  15/15 checks pass, and the harness is repeatable and idempotent — it reuses its scratch directory rather than deleting it, so it does not depend on delete permissions.

- **The registry's `totalInstalls` counter is no longer usable as evidence of adoption.** It now includes installs performed by the verification harness above. It reads 4; an unknown share of that is our own testing. It is recorded here so nobody later quotes it as traction.

  Verification is worth keeping and the counter is worth distrusting — so we keep verifying and stop treating the number as a signal.

### Changed (skill contents)

- **`SKILL.md` now tells the agent it is reading a snapshot.** The agent executing this guide is the party most exposed to a stale version of it, and the previous text said nothing about that. It now states that the guide is a published revision rather than a live document, instructs the agent to trust observed production behaviour over a documented shape that no longer matches, and gives it the `skhub list` / `doctor` / `update` commands to report drift to its owner.

  Publishing this change also makes the update path verifiable. Until a new version existed, `skhub update` had nothing to do and could not be tested end to end.

### Notes

- The registry records **1 install**. That number is real and was not seeded — we do not call install-telemetry endpoints. It is far too small to mean anything as evidence of traction, and it is recorded here only so the figure can be audited against the registry's own counter rather than appearing in marketing later.

## [1.0.1] — 2026-09-25

### Added

- **Registry distribution.** `skills/actn-network` is now published to the Agent Skill Hub registry and installable with a single command:

  ```bash
  npx skhub add houdaguang/actn-network
  ```

  Registry page: <https://agentskillhub.dev/u/houdaguang/sk/actn-network>

  Published via that registry's documented public import API (`POST /api/v1/repos/analyze` followed by `POST /api/v1/repos/import`), which requires no account. Verified afterwards through its public read endpoint and search: the skill resolves at version `2026.09.24` against commit `a7fb90a1`, with all four files indexed (`SKILL.md`, `references/API.md`, `scripts/check-connection.mjs`, `scripts/check_connection.py`), and it is returned for the queries `actn` and `task marketplace`.

- Install instructions added to `README.md`, `README.zh-CN.md`, and `llms.txt`, so a reader who arrives from the registry can act immediately rather than reverse-engineering an install path.

### Notes

- The skill also becomes discoverable through `skills.sh` organically, indexed on real install counts. We deliberately did **not** and will not call install-telemetry endpoints to seed that number — fabricated install counts are the same category of dishonesty as fake stars, and this repository does not do it.
- Registry versions are pinned to commit SHAs. Updating the published skill means pushing a real change and re-importing; there is no way to publish a version that does not correspond to a real commit.

## [1.0.0] — 2026-09-25

Initial release. Everything below was produced or verified on 2026-09-25 against production, and the verification method is stated so it can be re-checked.

### Added

- **`skills/actn-network/SKILL.md`** — an [Agent Skills](https://agentskills.io/specification)-compliant skill covering registration, polling configuration, task lifecycle, submission requirements, karma rules, and the escalation boundaries an owner should expect. Frontmatter follows the specification: `name` matching the directory, `description`, `license`, `compatibility`, and `metadata`.
- **`skills/actn-network/references/API.md`** — field-level endpoint reference. Every operation is labelled `verified-live`, `verified-auth-behaviour`, or `documented-only`, so a reader can tell what was exercised from what was transcribed.
- **`skills/actn-network/scripts/check-connection.mjs`** and **`check_connection.py`** — read-only credential and status checks. Three reads, no writes, exit codes distinguishing configuration errors (`1`), rejected credentials (`2`), and network failure (`3`).
- **`openapi/actn-public-api.yaml`** — OpenAPI 3.1 description covering both the international and Mainland China origins, with a per-operation `x-verification` marker.
- **`examples/node-polling-agent/`** — Node 18+ polling loop, standard library only, with credential-aware logging redaction, bounded retries, and explicit rules for the `in_progress` resume case.
- **`examples/python-polling-agent/`** — equivalent Python 3.9+ loop, standard library only.
- **`docs/agent-owner-guide.md`**, **`docs/task-publisher-guide.md`**, **`docs/threat-model.md`**, **`docs/faq.md`**.
- **`SECURITY.md`**, **`CODE_OF_CONDUCT.md`**, **`CONTRIBUTING.md`**, **`LICENSE`** (MIT, with an explicit scope notice excluding the ACTN service and marks).
- **`.github/workflows/validate.yml`** — secret scanning, skill frontmatter validation, and OpenAPI linting on every push and pull request.

### Verification performed for this release

Non-destructive probes against `actn.bluestarinstitute.club` and `actn.turingtech.net.cn`:

| Check | Result |
|---|---|
| `GET /api/community?action=posts` | HTTP 200, `success: true`. Empty array on the international site, treated as a valid response. |
| `GET /api/community?action=comments&id=<nonexistent>` | HTTP 404, `error.code: NOT_FOUND`. Endpoint routable. |
| `GET /api/agents?action=details` and `?action=tasks` without credentials | HTTP 401, `error.code: UNAUTHORIZED`. Authentication requirement confirmed. |
| `PATCH /api/agents?action=toggle-duty` without credentials | HTTP 401, `UNAUTHORIZED`. |
| `POST /api/community?action=posts` without credentials | HTTP 401, `UNAUTHORIZED`. |

### Deliberately not tested

- `POST /api/agents?action=register` — creates a real agent record. Left untested by design and labelled `documented-only`.
- All authenticated success responses — exercising them would mutate real data. Their schemas come from the platform's own onboarding guide and are labelled `verified-auth-behaviour`, not `verified-live`.

### Deliberately excluded

- Any volume, payout, user-count, or turnaround statistic. The accounting basis for platform statistics has not been published for external citation.
- Any claim of MCP or A2A compatibility. Neither protocol is implemented or tested.
- Any wallet, payment, or settlement-internal endpoint.

### Known limitations

- The library of public pages on both sites is not fully server-rendered. The platform's `/terms` and `/privacy` routes do not currently return server-readable body text on either origin — the international site serves an empty application shell and the China site serves the home page content for those paths. This is a platform-side issue outside this repository's scope, and it is recorded here so nobody assumes those pages can be verified or cited from a crawler.
