<h1 align="center">Markdown Notes Agent</h1>

<p align="center">
  Search and edit local Markdown notes through MCP<br>
  <a href="https://github.com/woonyong-choi/markdown-notes-agent/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/version-v0.3.0-blue" alt="Source version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT license"></a><br>
  <a href="README.md">한국어</a> | English
</p>

## Quick start

You need macOS or Linux, Python 3.12 or newer, and [uv](https://docs.astral.sh/uv/). Markdown Notes Agent calls models through an authenticated Claude Code or Codex CLI.

```sh
uv tool install git+https://github.com/woonyong-choi/markdown-notes-agent
markdown-notes-agent setup
markdown-notes-agent vault add ~/notes
```

Replace `~/notes` with an existing, readable and writable Markdown notes folder. Markdown Notes Agent calls a registered notes folder a vault.

Registration creates `.markdown-notes-agentignore` and `VAULT.md` at the root only when they do not exist. It preserves existing files. The default ignore file has no exclusion patterns, so all Markdown notes are eligible for indexing. Markdown Notes Agent does not index `VAULT.md` itself.

`markdown-notes-agent setup` finds a model CLI and installs Elasticsearch with the Korean analyzer. It selects Claude Code first, then Codex. Setup stops if neither CLI is available. It does not check CLI authentication or an actual model response.

### Connect an AI tool

Run the connection command for your tool. A request to the connected MCP server starts the background service.

<details>
<summary>Connect Claude Code</summary>

```sh
claude mcp add --scope user markdown-notes-agent -- markdown-notes-agent serve
```

</details>

<details>
<summary>Connect Codex</summary>

```sh
codex mcp add markdown-notes-agent -- markdown-notes-agent serve
```

</details>

### First request

Ask the connected AI tool to search. Replace the search terms with a topic in your notes.

```text
Find notes about idempotency keys in my registered notes folder.
```

Search does not change notes. Request a change separately after finding the relevant content.

```text
Remove repeated sentences from the note you found. Preserve its sources and folder rules.
```

An `accepted` change request is queued. The AI tool uses `job_status` to check the final result and next action. A job that stops because its source changed requires user consent before reconsideration. See [skills/markdown-notes-agent/SKILL.md](skills/markdown-notes-agent/SKILL.md) for the tool instructions.

<details>
<summary>Updates and source installation</summary>

Update the source installation with these commands.

```sh
uv tool install --force git+https://github.com/woonyong-choi/markdown-notes-agent
markdown-notes-agent setup
```

The [Hanger 0.2.0 tag](https://github.com/woonyong-choi/markdown-notes-agent/releases/tag/v0.2.0) from before the rename is preserved. When moving an existing installation, stop the existing service and move state, search data, request keys and recovery files to the new paths without losing their contents. Preserve folder rules and ignore rules under the new file names, and update explicit relation identifiers and MCP connections before starting. This version does not automatically connect old command or module names.

Install from source with this command.

```sh
uv tool install git+https://github.com/woonyong-choi/markdown-notes-agent
```

</details>

## Why

As Markdown notes grow, the same explanation can spread across several files. Markdown Notes Agent connects AI tools to note search, change proposals, and review under folder rules. Changes to existing notes replace source passages that the model read. Markdown Notes Agent checks the sources again before applying changes.

If you have a few notes and editing them yourself takes less time, an editor is enough.

## How it works

The diagram shows a change request that uses models. Search ends with a result. The `collect` tool stores content unchanged in `inbox/` without a model.

<img src="docs/assets/change-request.svg" width="360" alt="A separate model call reviews the proposal, then source checks allow each file write or stop it on a conflict">

1. The service queues the request and gathers related notes and folder rules for a draft.
2. A separate model call reviews the draft. Rejection leads to another draft with the review feedback. Review does not imply a different model or human approval.
3. Markdown Notes Agent checks the target and reference sources before writing each file. The default mode (`normal`) stops on changed sources. The `bypass` mode makes a new proposal from the current sources. Neither mode forces an overwrite.

The files are not one transaction. A conflict or cancellation leaves files that were already changed in place.

### Rules and search

Markdown Notes Agent watches the registered folder and updates the search index. It follows folder rules in `VAULT.md` and ignore patterns in `.markdown-notes-agentignore`. It does not change notes under `write: false` or ignored files.

<details>
<summary>Per-folder rule files</summary>

`VAULT.md` can sit in any folder and applies to that folder and below. For settings between the top `---` lines, the nearest file wins. For each note path, Markdown Notes Agent selects written rules from the nearest folder first, up to a total of 32 KiB, then sends the selected rules from the root down to the nearest folder. The file has its own name so it does not mix with other AI tools' rule files (`AGENTS.md`, `CLAUDE.md` and so on).

| Setting | Default | Meaning |
|---|---|---|
| `write` | `true` | `false` keeps Markdown Notes Agent from creating, changing or deleting notes here |
| `tree` | empty (one tree for the vault) | Keyword tree name. Folders with different names never link concepts |
| `keywords` | `true` | `false` keeps notes here out of the keyword tree (they are still indexed and searched) |
| `merge` | empty | `auto` queues a merge when two notes answer the same central question |

```markdown
---
write: false
---

Notes in this folder are final. Quote them; do not change them.
```

</details>

<details>
<summary>Keyword trees and merging</summary>

One note is one concept. Broader concepts the user wrote come first: a `parent` in the front matter (a slug, file name or link), and index notes whose list lines start with a link (that note is the parent of the notes it lists). Where the user wrote nothing, the model reads the note with its 20 nearest notes and proposes a concept name, other names, a definition and broader, related and prerequisite concepts; the program checks and stores them.

- A broader concept must be in the same tree and cannot form a cycle.
- A new broader name joins a note or concept of the same name; otherwise it becomes a concept when a second note proposes it, and goes back to waiting if it drops to one child.
- A concept with more than 15 direct children is split into narrower groups by the model.
- When a note is deleted, its children move up a level and are placed again.
- Notes that answer the same central question are reported, and when both folders set `merge: auto` a merge job is queued. Merges are reviewed like any other change.
- Concept names and other names add lightly to search, so a note is found by names its text never uses. Search results and drafts carry a note's place when the note belongs to the tree.

The tree is built in the background, one note at a time, while no user request waits. `markdown-notes-agent keywords` and `list_vaults` show progress.

</details>

<details>
<summary>Approval for ignore-rule changes</summary>

Editing the patterns in `.markdown-notes-agentignore` or deleting the file does not take effect at once; it is recorded as a decision. An edit that only changes comments cannot affect the index and applies at once. While a decision is pending, Markdown Notes Agent refuses every search and change request and asks for the answer first. If a vault's folder is missing or empty (an unplugged drive, say), its decision is withdrawn and asked again when the folder is back. File watching and index updates keep running. Markdown Notes Agent asks because a note taken out of the index may not get the same keywords and links when it is indexed again. For a deleted file, approving recreates the default file and declining restores the previous one.

</details>

### Tools and settings

Expand the reference you need.

<details>
<summary>MCP tool reference</summary>

Connected as an MCP server, Markdown Notes Agent offers these tools. Its messages and questions are in English, and it tells AI tools to translate them into the user's language.

| Tool | What it does |
|---|---|
| `list_vaults` | Shows registered vaults and pending decisions; with none, says to ask for a folder |
| `register_vault` | Registers a vault by absolute path |
| `remove_vault` | Always refuses and gives the terminal command for the user to run |
| `list_decisions`, `decide` | Shows pending decisions and applies the user's answer |
| `search_documents` | Searches; results include a keyword position when the note belongs to the tree |
| `read_document` | Reads the current text and hash |
| `keyword_tree` | The top of each tree, or one concept's names, definition, place, broader, narrower, related and prerequisite concepts |
| `issue_key`, `request_change`, `collect` | Write, edit and organize requests; keep a file or text unchanged in `inbox/` |
| `job_status`, `cancel_job`, `resolve_conflict` | Check progress, cancel, reconsider a job stopped by a changed note |

With more than one vault, each request passes `vault` as an id or a path inside the vault. Usage guidance for AI tools is in [skills/markdown-notes-agent/SKILL.md](skills/markdown-notes-agent/SKILL.md).

</details>

<details>
<summary>Terminal commands and settings</summary>

| Command | What it does |
|---|---|
| `markdown-notes-agent setup [--reinstall]` | Checks the model provider and installs the search engine |
| `markdown-notes-agent provider` | Shows the CLI and model for each role (draft, review, keywords) |
| `markdown-notes-agent vault add <folder>` / `list` / `remove <id\|path> [--yes]` | Adds, lists and removes vaults. Removing keeps the notes and rule files and deletes only the index and job history |
| `markdown-notes-agent ignore show` / `add <pattern>` / `remove <pattern>` `[--vault] [--yes]` | Shows and changes ignore rules, showing how many notes change and asking to confirm |
| `markdown-notes-agent decisions` / `markdown-notes-agent decide <id> approve\|decline` | Lists and answers pending decisions |
| `markdown-notes-agent config list` / `get <key>` / `set <key> <value>` / `unset <key>` | Lists, changes and resets settings |
| `markdown-notes-agent search <query> [--vault] [--folder]` | Searches from the terminal |
| `markdown-notes-agent keywords [concept] [--vault]` | Keyword tree progress and top, or one concept (a note path or name) |
| `markdown-notes-agent stop` | Stops the background service; it starts again on the next request |

Main settings:

| Key | Default | Meaning |
|---|---|---|
| `model.provider` | automatic | `claude` or `codex`. Empty picks an installed one, Claude Code first |
| `model.name` | automatic | Empty uses `haiku` for Claude Code and `gpt-6-luna` for Codex |
| `model.effort` | CLI default | `low`, `medium` or `high` |
| `models.draft.*`, `models.review.*`, `models.keywords.*` | `model.*` | Separate values for the draft, review and keyword tree roles; empty values follow `model.*` |
| `search.candidates` | 20 | Number of search candidates |
| `service.idle_minutes` | 30 | How long the service waits without requests before it exits |
| `elasticsearch.port` | 19200 | Port of the search engine Markdown Notes Agent runs |

Settings, the vault list, applied ignore rules and decisions are stored in `~/.local/share/markdown-notes-agent/state/markdown-notes-agent.db`. Setting changes apply when the service restarts (`markdown-notes-agent stop`).

</details>

## Status

Version 0.3.0 supports multiple vaults, folder rules, search, keyword trees, and change requests with review and recovery.

| Available | Not yet available | Next | Measurement |
|---|---|---|---|
| Search and keyword trees | Verified search recall with the current tree | [Search recall fix](https://github.com/woonyong-choi/markdown-notes-agent/issues/7) | Current tree not remeasured |
| Writing, editing, organizing, and review | A cost and quality comparison of the whole organizing workflow | [Whole workflow measurement](https://github.com/woonyong-choi/markdown-notes-agent/issues/8) | Whole comparison incomplete |

Formats and commands can change without notice before 1.0.

## Development

```sh
uv sync --locked
uv build
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

Search integration tests use Elasticsearch with Nori at `MARKDOWN_NOTES_AGENT_TEST_SEARCH_URL`. Managed-server lifecycle tests use an Elasticsearch installation with Nori at `MARKDOWN_NOTES_AGENT_TEST_ES_HOME`. Tests without the required settings are skipped. Live model tests run only when explicitly enabled.

<details>
<summary>Regenerate the diagram</summary>

[change-request.diagramplay](docs/assets/change-request.diagramplay) is the source; the SVG is generated. Set `DIAGRAMPLAY_CLI` to the CLI path from [DiagramPlay ebdc3e9](https://github.com/woonyong-choi/diagramplay/tree/ebdc3e9c84c3d5443af7f1b6b5d9b2a18e5002ca).

```sh
node "$DIAGRAMPLAY_CLI" check docs/assets/change-request.diagramplay --strict
node "$DIAGRAMPLAY_CLI" render docs/assets/change-request.diagramplay --strict --out docs/assets
```

</details>

## License

[MIT](LICENSE)
