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

## Vocabulary and writing

Hosted-mode Yutome has a **canonical glossary** at `docs/hosted-glossary.md`. It is the
single source of truth for the domain vocabulary (e.g. `credential_mode`, `subject`,
`EntitlementPolicy` vs `WorkspaceBalance` vs `UsageGate`, connector grant, search store,
bridge/relay/replica). Use the canonical term for every hosted concept; add new concepts
to the glossary rather than coining a synonym.

A short **writing standard** governs hosted code, docs, beads issues, code reviews, commit
messages, and docstrings — including how agents write:

1. Define a term before first use; use the canonical glossary name — one name per concept.
2. Name things for what they do. No overselling (not "always-on" for an offline replica;
   not "deployment verification" for a commit that only closed an issue).
3. Prefer a precise condition over hand-waving ("deny when balance < estimated units",
   not "block obviously unaffordable work").
4. Expand acronyms on first use (BYO, RRF, RLS).
5. Cut marketing prose from technical docs.
6. Be more verbose exactly where a concept is load-bearing or non-obvious; terse elsewhere.

Both are also stored as bd memories (`design-ontology-glossary`, `writing-and-ontology-standard`)
so they surface in `bd` sessions.


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
