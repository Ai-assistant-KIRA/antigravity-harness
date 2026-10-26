# Install Antigravity Harness

This guide covers the one-prompt Antigravity install, the manual install, and the
safe upgrade and removal paths.

## Requirements

- Antigravity with plugin support. Tested on Antigravity **44.4.3**.
- Python **3.10 or newer** on `PATH`.
- Git, or the ability to download a ZIP archive.
- Windows, macOS, or Linux. CI runs on Windows and Ubuntu.

The plugin has no third-party Python dependencies.

## Option 1: Ask Antigravity to install it

Open a new Antigravity conversation and paste this:

> Install the Antigravity plugin from
> https://github.com/Ai-assistant-KIRA/antigravity-harness.
>
> Clone the repository to a temporary folder and read `INSTALL.md`.
> Run `python install_plugin.py --dry-run` first, then run
> `python install_plugin.py`.
> Do not use `--reset` or `--force`. Keep the existing memory and rules.
> When it finishes, run `harness doctor` from the installed plugin and show me
> the result. Then tell me to restart Antigravity.

If the agent cannot clone, give it this ZIP URL instead:

```text
https://github.com/Ai-assistant-KIRA/antigravity-harness/archive/refs/heads/main.zip
```

### What the agent should do

1. Clone or download the repository to a temporary directory.
2. Run `python install_plugin.py --dry-run` and check the target path.
3. Run `python install_plugin.py` without `--reset`.
4. Run the installed `doctor` command.
5. Tell you to restart Antigravity.

The installer makes a backup before changing anything. It keeps `memory/`,
`state/`, generated skills, and the global rules file on a normal upgrade.

## Option 2: Manual install

### Windows PowerShell

```powershell
git clone https://github.com/Ai-assistant-KIRA/antigravity-harness.git
cd antigravity-harness
python install_plugin.py --dry-run
python install_plugin.py
```

### macOS / Linux

```bash
git clone https://github.com/Ai-assistant-KIRA/antigravity-harness.git
cd antigravity-harness
python3 install_plugin.py --dry-run
python3 install_plugin.py
```

The default target is:

- Windows: `%USERPROFILE%\.gemini\config\plugins\harness`
- macOS/Linux: `~/.gemini/config/plugins/harness`

Override it with `--target PATH` or `HARNESS_PLUGIN_DIR`.

## Verify the install

Restart Antigravity after the installer finishes. Then run:

```powershell
# Windows
& "$HOME\.gemini\config\plugins\harness\bin\harness.cmd" doctor
& "$HOME\.gemini\config\plugins\harness\bin\harness.cmd" status
```

```bash
# macOS / Linux
~/.gemini/config/plugins/harness/bin/harness doctor
~/.gemini/config/plugins/harness/bin/harness status
```

`doctor` checks the manifest, hooks, hook runner, memory database, FTS search,
skills, rules file, and wrappers. It should end with `All checks passed.`

A healthy install has four hook events:

- `SessionStart`
- `PreInvocation`
- `PostToolUse`
- `Stop`

## Upgrade

Run the same installer again from a fresh checkout. The default path is safe:

```powershell
python install_plugin.py
```

It backs up the current plugin, refreshes bundled code and skills, and keeps your
memory, state, rules, and user-authored skills.

Do **not** use `--reset` unless you want to erase learned data and generated
skills. `--reset` still makes a backup first.

## Uninstall and rollback

```powershell
python install_plugin.py --uninstall
python install_plugin.py --rollback
```

- `--uninstall` backs up the current plugin, including the memory database, then
  removes the installed target.
- `--rollback` backs up the current target and restores the newest valid backup.
- Both support `--dry-run`.

## Where data lives

| Path | Contents |
| :--- | :--- |
| `<plugin>/memory/harness_memory.db` | Global sessions, lessons, candidates, skills index |
| `<plugin>/memory/EXPERIENCE.md` | Generated human-readable experience log |
| `<plugin>/memory/projects/<fingerprint>/harness_memory.db` | Per-project memory (isolated) |
| `<plugin>/memory/projects/<fingerprint>/EXPERIENCE.md` | Per-project experience log |
| `<plugin>/state/events.jsonl` | Hook event journal |
| `<plugin>/state/pending/` | Per-conversation tool observations |
| `<plugin>/state/hook_errors.log` | Hook failures, if any |
| `<plugin>/rules/AGENTS.md` | Curated global rules |
| `<plugin>/skills/` | Bundled and learned skills |
| `<workspace>/AGENTS.md` | Workspace rules and task routing, inside the managed block |
| `<workspace>/.agents/skills/` | Workspace skills |
| `<workspace>/.agents/tools.json` | Optional project tool vocabulary |

The database and state files are local. The plugin does not upload them.

## Configuration

Put `harness.toml` or `harness.json` next to the installed plugin. `harness.toml`
is preferred on Python 3.11+; `harness.json` is the 3.10 fallback.

```toml
[rules]
max_bytes = 20000

[briefing]
max_chars = 7000
max_skills = 8
max_lessons = 5

[skills]
max_per_session = 2
min_quality = 0.55

[memory]
prune_days = 365
candidate_days = 30
max_lessons = 2000

[project]
isolate_memory = true
project_skills_only = true

[routing]
enabled = true
auto_apply = true
max_skills = 5
max_tools = 6

[hooks]
stop_timeout = 90
post_tool_use_timeout = 5
```

Environment variables:

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `HARNESS_PLUGIN_DIR` | `~/.gemini/config/plugins/harness` | Plugin and data root |
| `HARNESS_GEMINI_CONFIG_DIR` | `~/.gemini/config` | Antigravity config root |
| `HARNESS_PROJECT_ISOLATION` | unset | Force per-project memory on (`1`) or off (`0`) |
| `HARNESS_PROJECT_SKILLS_ONLY` | unset | Force project-local skill loading on (`1`) or off (`0`) |
| `ANTIGRAVITY_APP_DATA_DIR` | auto-detected | Conversation transcripts |
| `ANTIGRAVITY_CONVERSATION_ID` | empty | Fallback conversation ID |
| `HARNESS_DEBUG` | unset | Show tracebacks from the CLI |

## Troubleshooting

### `harness` is not found

Use the full wrapper path:

```powershell
& "$HOME\.gemini\config\plugins\harness\bin\harness.cmd" status
```

### The Stop hook does nothing

1. Check `<plugin>/state/hook_errors.log`.
2. Run `doctor`.
3. Confirm `hooks.json` contains all four events.
4. Test the runner directly from the plugin directory:

```powershell
echo {} | python scripts/hook_runner.py stop
```

### The transcript is not found

Pass `--transcript PATH`, or set `ANTIGRAVITY_APP_DATA_DIR` or
`ANTIGRAVITY_CONVERSATION_ID`. Some Antigravity builds truncate hook payloads;
the runner falls back to the brain directory.

### The rules file is near the size limit

The managed block is capped at 20 KB, but your own content can push the file over
Antigravity's 24 KB limit. Shorten your content; Harness will not delete it.

### A bad rule or skill was learned

```powershell
& "$HOME\.gemini\config\plugins\harness\bin\harness.cmd" rules why "rule text"
& "$HOME\.gemini\config\plugins\harness\bin\harness.cmd" rules forget "rule text"
& "$HOME\.gemini\config\plugins\harness\bin\harness.cmd" skills remove NAME --yes
```

## More help

- [README.md](README.md)
- [docs/FAQ.md](docs/FAQ.md)
- [SUPPORT.md](SUPPORT.md)
- [SECURITY.md](SECURITY.md)
