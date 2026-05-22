# Claude / agent notes for the yutome repo

## Private worktree

A second working copy of this repo is checked out at `../yt-indexer-private`
on the local-only branch `private-dont-push-to-github`. That branch holds
in-flight drafts, internal design reviews, and ephemeral QA notes — files
that should never appear on GitHub. The branch is configured to refuse
push (`remote = no_push`).

If the user asks about an essay draft, design crit, review notes, or
internal QA results, look there first:

- `../yt-indexer-private/docs/essay-draft.md`
- `../yt-indexer-private/docs/replica-design-review.md`
- `../yt-indexer-private/docs/demo-manual-test-results.md`

Future drafts (`*-draft.md`, `*-review.md`, `*-notes.md`, scratch
experiments) follow the same convention — commit them on the
`private-dont-push-to-github` branch via the sibling worktree, not on
`main`. Never copy their content into files under `~/yt-indexer/`
(`main`'s working tree), since that would route them toward a public
push.

## Skill files

`.claude/skills/yutome-retrieval/SKILL.md` is the public skill that
governs how agents query the yutome corpus. Edit it on `main`.
