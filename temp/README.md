# temp/ : the klooien folder

Scratch space for this project. Screenshots taken to show Arend, scraped data, API dumps,
throwaway scripts, spike experiments, and anything Arend drops in here himself.

**Conventions** (`.claude/rules/workspace-hygiene.md` §3):

- Claude date-slugs its own drops: `temp/2026-08-27-<slug>/`. Loose files sitting directly
  in `temp/` are assumed to be Arend's and are never deleted without asking.
- Nothing in here is committed, deployed, or imported by shipped code. `temp/` is
  gitignored. If shipped code needs a file, it was never temp: promote it to its real home
  (`src/`, `docs/`, `public/`, `assets/`) in the same turn.
- Claude sweeps the `temp/` subfolders it created before ending a task, and says what it
  removed.
- Reports that mention an artifact give its full path here, so it is findable and
  disposable.

This README is the only tracked file in `temp/`.
