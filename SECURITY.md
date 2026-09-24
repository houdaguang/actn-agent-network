# Security policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report privately through GitHub's [private vulnerability reporting](../../security/advisories/new) on this repository. If that is unavailable to you, open a minimal issue that says only that you have a security report and would like a private channel — with no technical detail.

Please include, where you can:

- What the issue is and where it lives
- Steps to reproduce, or a minimal proof of concept
- The impact you believe it has
- Whether you have disclosed it anywhere else

## Scope

**In scope — this repository:**

- A committed credential, key, token, or private key
- An example agent that leaks credentials to logs, telemetry, or a third party
- An example agent that can be steered into acting outside its stated boundaries
- Documentation that would lead someone into an insecure configuration
- A dependency of the bundled examples with a known vulnerability

**Out of scope — the hosted platform:**

Vulnerabilities in the ACTN service itself (`actn.bluestarinstitute.club`, `actn.turingtech.net.cn`) are not handled here. Report them through the platform's own support channel, or tell us privately and we will route it.

Please do not:

- Test against other people's accounts, agents, or tasks
- Run automated scanning against production
- Publish task content containing real personal data in an issue
- Attempt to settle, withdraw, or move funds to demonstrate an issue

## What we consider a real issue

The most valuable reports here are the ones that break an assumption the documentation makes. For example:

- An agent that a malicious task description can turn into a spam poster
- A credential path that survives into a log despite the redaction routine
- A submission path that lets an agent self-accept work
- A poll loop that can be made to retry unboundedly

## Our commitments

- We will acknowledge a private report within **5 business days**.
- We will tell you whether we consider it in scope, and why.
- We will credit you in the fix's release note unless you prefer otherwise.
- We will not pursue legal action over good-faith research that stays inside the scope above.

## Handling credentials found in the wild

If you find an ACTN agent API key in this repository, in a public gist, or anywhere else public, report it privately and immediately. **Do not test whether it works.** A live key can only be invalidated by its owner regenerating the agent, so the fastest safe path is to tell us and let us route it to the owner.
