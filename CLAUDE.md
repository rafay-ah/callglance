# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## Git identity (mandatory)

Every commit in this repo must be authored **and** committed as the maintainer:

```sh
git config user.name "rafay-ah"
git config user.email "54492363+rafay-ah@users.noreply.github.com"
```

Run both commands at the start of every session, before making any commit.

Rules:

- No `Co-Authored-By:` trailers, no `Claude-Session:` trailers, and no
  "Generated with Claude Code" lines in commit messages, PR titles or PR bodies.
  `.claude/settings.json` disables Claude Code's automatic attribution; keep it that way.
- Before every push, verify authorship of the commits being pushed:

  ```sh
  git log --format='%an <%ae> | %cn <%ce>'
  ```

  Every line must read
  `rafay-ah <54492363+rafay-ah@users.noreply.github.com> | rafay-ah <54492363+rafay-ah@users.noreply.github.com>`.
  Fix any commit that is not fully the maintainer's before pushing:
  `git commit --amend --reset-author --no-edit` for the tip, or
  `git rebase -r <base> --exec 'git commit --amend --reset-author --no-edit'` for a range.
- Work happens directly on `main` unless told otherwise. Commit often, with clear,
  imperative commit messages.
