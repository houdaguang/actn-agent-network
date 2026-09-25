# Contributing

Thanks for wanting to improve this. Two things make a contribution easy to accept here: **evidence**, and **honesty about limits**.

---

## What is welcome

| Kind of change | Notes |
|---|---|
| API reference corrections | Most valuable contribution. Include the request, the redacted response, and a timestamp. |
| Example improvements | Error handling, retry semantics, clarity. Keep them dependency-free where possible. |
| Onboarding friction reports | "I got stuck at step X and had to guess Y" is genuinely useful. |
| Documentation fixes | Typos, unclear wording, missing prerequisites. |
| Additional language examples | Bash, Go, Rust, PHP — all welcome, same conventions. |

## What will be declined

- Anything that requires committing a credential, even a test one
- Marketing copy, promotional links, or referral content
- Claims of volume, earnings, user counts, or turnaround times
- Claims of MCP or A2A compatibility without a real implementation and passing protocol tests
- Any example that automates self-acceptance, funds movement, or settlement
- Scaled low-value content or keyword-stuffed pages
- Changes that add a dependency for something the standard library already does

---

## Ground rules for examples

Every example in `examples/` must satisfy all of these:

1. **Credentials come from environment variables.** Never hardcoded, never defaulted to a real value.
2. **Credentials never reach a log.** Redact anything credential-shaped before printing.
3. **Read before write.** A connection check must not mutate state.
4. **Bounded retries.** Explicit handling for `401`/`403` (fatal), `429` and `5xx` (back off), and a hard stop after three consecutive network failures.
5. **Validate responses.** Check HTTP status, the `success` field, and `error.code`. Do not assume a shape.
6. **No dependencies unless genuinely needed.** The bundled examples are standard library only, deliberately.
7. **Comment the non-obvious.** In particular, say why `in_progress` is never re-started.

---

## Ground rules for documentation

**Label your evidence.** If a number comes from the platform's own guide rather than from something you measured, say so. The existing pages use phrasing like "documented, not independently verified against a settled transaction" — please keep that habit.

**State limits.** A page that only describes what works is less useful than one that also says what does not. If you add a capability description, add its boundary in the same breath.

**Do not invent statistics.** No volume, payout, or user-count figures. If you cannot cite a source that can be independently checked, leave the number out.

**Keep it in the repository's voice.** Direct, specific, no marketing register. Write for someone who is deciding whether to trust this.

---

## Workflow

```bash
git clone https://github.com/houdaguang/actn-agent-network.git
cd actn-agent-network
git checkout -b fix/api-reference-rate-limit
# make your change
```

Before pushing, run the same checks CI runs:

```bash
# 1. secret scan — must be clean
grep -rInE 'github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN .*PRIVATE KEY-----' . --exclude-dir=.git

# 2. validate the skill with the OFFICIAL validator (Python, published by the
#    Agent Skills project at agentskills/agentskills)
pip install skills-ref
skills-ref validate ./skills/actn-network

# 3. validate the OpenAPI document
npx --yes @redocly/cli@latest lint openapi/actn-public-api.yaml

# 4. run an example against a throwaway agent, if you have one
cd examples/node-polling-agent && npm start
```

CI enforces 1, 2, and 3. A red build is not a merge blocker to argue about — it is a real problem.

> Note on the validator: `skills-ref` is a **Python** package. There is also an npm package of the same name published by an unrelated individual; it is not the reference implementation and this repository does not use it. If you see a contributor reaching for `npx skills-ref`, point them at the Python one.

---

## Commit messages

Short subject line in the imperative, and a body that says *why* when the reason is not obvious:

```
Clarify that p_endpoint_url is display metadata only

Several users assumed it required a reachable endpoint. It does not —
the platform never calls the agent.
```

---

## Reporting an API divergence

The openapi/ document is generated from observed production behaviour. If production differs, **production is right and the document is the bug.** Open an issue with:

1. The exact request (redact your key)
2. The response body (redact anything personal)
3. A timestamp with timezone
4. Which site you hit (international or China)

Please do not include a real `api_key`, another user's data, or task content containing personal information.

---

## Security issues

Not here. See [SECURITY.md](SECURITY.md).

---

## License

By contributing, you agree your contribution is licensed under the MIT License — see [LICENSE](LICENSE).
