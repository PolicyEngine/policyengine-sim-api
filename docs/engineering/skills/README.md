# Engineering Skills

This directory is the canonical source for AI-facing engineering rules.

Tool-specific instruction files such as `AGENTS.md`, `CLAUDE.md`, and
`.github/copilot-instructions.md` should point here instead of duplicating
implementation-specific guidance. When a rule changes, update the skill here
first, then keep adapters thin.

Current skills:

- `github-prs.md`: same-repository draft PR workflow, issue linkage, PR head
  verification, and title conventions.
- `testing.md`: test placement, dependency boundaries, and expected validation
  commands.
- `observability.md`: identifier ownership, HTTP and Modal transport,
  persistence, registered runtime stages, trace boundaries, and failure
  isolation.
