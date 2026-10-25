# Security Policy

## Supported versions

| Version | Supported |
| :--- | :--- |
| 2.1.x | Yes |
| 2.0.x and older | No |

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Use GitHub's private vulnerability reporting:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Include the plugin version, operating system, Python version, and a minimal
   reproduction.

If private reporting is unavailable, contact the maintainer through
[@Ai-assistant-KIRA](https://github.com/Ai-assistant-KIRA) and ask for a private
channel. Do not include secrets in the first message.

I aim to acknowledge a report within **5 business days** and to publish a fix or
a mitigation plan when the issue is confirmed.

## Scope

In scope:

- the installer and rollback paths;
- the hook runner and hook input/output handling;
- transcript parsing and memory storage;
- rule and skill synthesis;
- redaction and prompt-injection scanning;
- the CLI and its file handling.

Out of scope:

- vulnerabilities in Antigravity itself (report those to the Antigravity maintainers);
- vulnerabilities in third-party skills you install yourself;
- a malicious local user who can already edit the plugin files or database;
- prompt injection that only changes model behavior without crossing a
  documented trust boundary.

## Data handling

Harness² is local-first. It does not call an LLM or upload memory, rules, skills,
or transcripts to a service.

It does read Antigravity transcripts to learn from a session. Before persistence:

- credentials and common secret formats are redacted;
- project-specific lessons are kept in the workspace scope;
- unresolved failures are stored as candidates, not rules;
- learned text is scanned for prompt-injection and exfiltration patterns.

Redaction reduces risk. It is not a guarantee. Do not put secrets in
`AGENTS.md`, skills, transcripts, or issue reports. Review learned content before
you rely on it.

The full threat model, retention notes, and review workflow are in
[docs/SECURITY.md](docs/SECURITY.md).

## Safe harbor

I will not pursue legal action for good-faith security research that follows this
policy, avoids privacy violations, and does not disrupt other users. Please give
me a reasonable window to fix the issue before public disclosure.
