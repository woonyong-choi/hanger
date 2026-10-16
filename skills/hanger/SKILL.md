---
name: hanger
description: Use when the user asks to find, write, edit, merge or organize their Markdown notes and the Hanger MCP tools are connected.
---

# Hanger

Hanger keeps the user's Markdown notes in registered folders called closets.
Use its MCP tools instead of editing note files yourself: Hanger reads the
closet's rules, drafts the change, has it reviewed and writes it only if the
notes did not change meanwhile.

## Language

Hanger answers in English. Some messages come in two parts:

- `Tell the user: ...` is for the user. Relay it in full, translated into the
  language the user writes in or has set for you. Do not shorten or reword it.
- `Then: ...` is for you. Do it; do not show it to the user.

Keep commands, paths, ids and the names closet, Hanger, `.hangerignore` and
`HANGER.md` as they are when you translate.

## First use

1. Call `list_closets`.
2. If no closet is registered, relay the question Hanger returns and call
   `register_closet` with the absolute path the user gives. Do not guess a
   folder.
3. With more than one closet, pass `closet` on every call: an id from
   `list_closets`, or a path inside the closet (the current working folder
   often works).

## Decisions come first

When a call fails with `decision_required`, Hanger is waiting for the user,
usually because `.hangerignore` was edited or deleted.

1. Show the question and both choices to the user, translated.
2. Wait for the user's answer. Never choose for them, even if one choice looks
   obvious.
3. Call `decide` with the decision id and `approve` or `decline`, then retry
   the original request.

## Finding notes

- `search_documents` with the user's question as `query`. Narrow it with
  `folders` (paths relative to the closet) when the user names a place.
- Top results come with full text; others are listed with matching lines. Use
  `read_document` for a listed path you need.
- If a note the user mentions is missing, search again with
  `fallback_reason: "user_reported_missing"`.
- Each result carries `keywords`: the note's concept name, its place in the
  keyword tree and its definition. `keyword_tree` with a note path or name
  shows the concept's broader, narrower, related and prerequisite concepts;
  without one it shows the top of each tree and how far building has come.

## Changing notes

1. Call `issue_key` for every new request; a key cannot be reused for a
   different request.
2. Call `request_change` with an `intent`:
   - `kind`: `write` (new content), `edit` (change existing notes) or
     `organize` (merge, split, link).
   - `instruction`: what the user wants, in their words.
   - `query`: what to search for to find related notes.
   - `paths`: notes that must be read, when the user named them.
   - `folders`, `profile` (`note`, `wiki`, `work_record`, `blog`, `creative`)
     and `allow_delete` only when the user asked for them.
3. `accepted` means queued, not done. Poll `job_status` until the status is
   `completed`, `deferred`, `conflict`, `failed`, `cancelled` or `expired`, and
   tell the user the result and its message.
4. On `conflict`, a note changed while the job ran. Offer to reconsider; with
   the user's agreement call `resolve_conflict` with a new key.

To keep text or a file exactly as given, use `collect`; it is saved under
`inbox/` without a model.

## Never

- Never remove a closet. `remove_closet` always refuses; give the user the
  terminal command it returns.
- Never edit `.hangerignore` or `HANGER.md` files on the user's behalf unless
  they ask; those are the user's rules.
