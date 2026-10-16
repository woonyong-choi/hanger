<p align="center">
  <img src="docs/assets/logo.png" alt="Hanger logo" height="120">
</p>

<h1 align="center">Hanger</h1>

<p align="center">
  Local Markdown document automation with durable workflows<br>
  <a href="README.md">한국어</a> | English<br>
  <a href="#quick-start">Quick start</a> · <a href="#how-it-works">How it works</a> · <a href="#using-it-from-ai-tools">Using it from AI tools</a>
</p>

## Quick start

You need macOS or Linux, Python 3.12 or later, [uv](https://docs.astral.sh/uv/), and a signed-in Claude Code (`claude`) or Codex (`codex`). Hanger uses no API keys; it runs models through the subscription you already have.

```sh
uv tool install hanger
hanger setup
claude mcp add --scope user hanger -- hanger serve
hanger closet add ~/notes
```

`hanger setup` asks no questions. It finds a model provider (Claude Code if present, otherwise Codex) and downloads and installs the search engine (Elasticsearch with the Korean analyzer). When no provider is found, setup stops and says why. You can register your notes folder in the terminal, as on the last line, or let your AI tool ask you for it on first use.

To connect Codex, use `codex mcp add hanger -- hanger serve`. To install the latest unreleased code, use `uv tool install git+https://github.com/woonyong-choi/hanger`.

To update:

```sh
uv tool upgrade hanger
hanger setup
```

## Why

As Markdown notes pile up, the same topic ends up in several notes, and an AI tool asked to tidy them may change things without evidence or touch folders it should leave alone. Hanger sits between your AI tools and your notes folder and handles search, change planning, review and writing. It changes only passages of notes it has read, follows the rule files (`HANGER.md`) and ignore file (`.hangerignore`) you set, and stops instead of overwriting when a note changed in the meantime.

If you keep a handful of notes in one folder and editing them yourself is quicker, an editor is enough.

## How it works

1. **Register a closet**: a notes folder Hanger keeps is called a closet. You can register several. A folder inside, or containing, a registered closet is refused as a duplicate. Registering creates a default `.hangerignore` and `HANGER.md` at the root only when they are missing.
2. **Index**: a background service watches for file changes and keeps the search index current. Files matched by `.hangerignore` (`.gitignore` syntax) and `HANGER.md` files are not indexed. The default file holds only syntax notes and no patterns, so at first every Markdown note is indexed.
3. **Keyword tree**: alongside indexing, each note gets a concept name, other names, a definition and broader, related and prerequisite concepts, forming a tree per closet. See "Keyword tree" below.
4. **Request**: an AI tool searches, or asks to write, edit or organize. Change requests are queued and the service works through them.
5. **Draft and review**: the model gets the related notes together with the `HANGER.md` rules for their folders and drafts a change; a separate call reviews it. A rejected draft is written again with the reasons.
6. **Write**: files change only after Hanger confirms the source is unchanged. Folders with `write: false` and ignored files are never changed.

**Rule files, `HANGER.md`**: one can sit in any folder and applies to that folder and below. For settings between the top `---` lines, the nearest file wins; the written rules all reach the model, from the root down to the nearest folder. The file has its own name so it does not mix with other AI tools' rule files (`AGENTS.md`, `CLAUDE.md` and so on).

| Setting | Default | Meaning |
|---|---|---|
| `write` | `true` | `false` keeps Hanger from creating, changing or deleting notes here |
| `tree` | empty (one tree for the closet) | Keyword tree name. Folders with different names never link concepts |
| `keywords` | `true` | `false` keeps notes here out of the keyword tree (they are still indexed and searched) |
| `merge` | empty | `auto` queues a merge when two notes answer the same central question |

```md
---
write: false
---

Notes in this folder are final. Quote them; do not change them.
```

**Keyword tree**: one note is one concept. Broader concepts the user wrote come first: a `parent` in the front matter (a slug, file name or link), and index notes whose list lines start with a link (that note is the parent of the notes it lists). Where the user wrote nothing, the model reads the note with its 20 nearest notes and proposes a concept name, other names, a definition and broader, related and prerequisite concepts; the program checks and stores them.

- A broader concept must be in the same tree and cannot form a cycle.
- A new broader name joins a note or concept of the same name; otherwise it becomes a concept when a second note proposes it, and goes back to waiting if it drops to one child.
- A concept with more than 15 direct children is split into narrower groups by the model.
- When a note is deleted, its children move up a level and are placed again.
- Notes that answer the same central question are reported, and when both folders set `merge: auto` a merge job is queued. Merges are reviewed like any other change.
- Concept names and other names add lightly to search, so a note is found by names its text never uses. Search results and drafts carry each note's place in the tree.

The tree is built in the background, one note at a time, while no user request waits. `hanger keywords` and `list_closets` show progress.

**The user approves ignore-rule changes**: editing the patterns in `.hangerignore` or deleting the file does not take effect at once; it is recorded as a decision. An edit that only changes comments cannot affect the index and applies at once. While a decision is pending, Hanger refuses every search and change request and asks for the answer first. If a closet's folder is missing or empty (an unplugged drive, say), its decision is withdrawn and asked again when the folder is back. File watching and index updates keep running. Hanger asks because a note taken out of the index may not get the same keywords and links when it is indexed again. For a deleted file, approving recreates the default file and declining restores the previous one.

## Using it from AI tools

Connected as an MCP server, Hanger offers these tools. Its messages and questions are in English, and it tells AI tools to translate them into the user's language.

| Tool | What it does |
|---|---|
| `list_closets` | Shows registered closets and pending decisions; with none, says to ask for a folder |
| `register_closet` | Registers a closet by absolute path |
| `remove_closet` | Always refuses and gives the terminal command for the user to run |
| `list_decisions`, `decide` | Shows pending decisions and applies the user's answer |
| `search_documents`, `read_document` | Search and read; each result shows its place in the keyword tree |
| `keyword_tree` | The top of each tree, or one concept's names, definition, place, broader, narrower, related and prerequisite concepts |
| `issue_key`, `request_change`, `collect` | Write, edit and organize requests; keep a file or text unchanged in `inbox/` |
| `job_status`, `cancel_job`, `resolve_conflict` | Check progress, cancel, reconsider a job stopped by a changed note |

With more than one closet, each request passes `closet` as an id or a path inside the closet. Usage guidance for AI tools is in [skills/hanger/SKILL.md](skills/hanger/SKILL.md).

## Terminal commands

| Command | What it does |
|---|---|
| `hanger setup [--reinstall]` | Checks the model provider and installs the search engine |
| `hanger provider` | Shows the CLI and model for each role (draft, review) |
| `hanger closet add <folder>` / `list` / `remove <id\|path> [--yes]` | Adds, lists and removes closets. Removing keeps the notes and rule files and deletes only the index and job history |
| `hanger ignore show` / `add <pattern>` / `remove <pattern>` `[--closet] [--yes]` | Shows and changes ignore rules, showing how many notes change and asking to confirm |
| `hanger decisions` / `hanger decide <id> approve\|decline` | Lists and answers pending decisions |
| `hanger config list` / `get <key>` / `set <key> <value>` / `unset <key>` | Lists, changes and resets settings |
| `hanger search <query> [--closet] [--folder]` | Searches from the terminal |
| `hanger keywords [concept] [--closet]` | Keyword tree progress and top, or one concept (a note path or name) |
| `hanger stop` | Stops the background service; it starts again on the next request |

Main settings:

| Key | Default | Meaning |
|---|---|---|
| `model.provider` | automatic | `claude` or `codex`. Empty picks an installed one, Claude Code first |
| `model.name` | automatic | Empty uses `haiku` for Claude Code and `gpt-6-luna` for Codex |
| `model.effort` | CLI default | `low`, `medium` or `high` |
| `models.draft.*`, `models.review.*`, `models.keywords.*` | `model.*` | Separate values for the draft, review and keyword tree roles; empty values follow `model.*` |
| `search.candidates` | 20 | Number of search candidates |
| `service.idle_minutes` | 30 | How long the service waits without requests before it exits |
| `elasticsearch.port` | 19200 | Port of the search engine Hanger runs |

Settings, the closet list, applied ignore rules and decisions are stored in `~/.local/share/hanger/state/hanger.db`. Setting changes apply when the service restarts (`hanger stop`).

## Status

Several closets, ignore rules with approval, per-folder rule files, search, the keyword tree, write/edit/organize requests with review, and subscription CLIs (Claude Code, Codex) are supported. Before 1.0, formats and commands may change without notice.

## Development

```sh
uv sync
HANGER_TEST_SEARCH_URL=http://127.0.0.1:19200 uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

Tests that need search use the Elasticsearch (with the Nori analyzer) given in `HANGER_TEST_SEARCH_URL` and are skipped without it.
