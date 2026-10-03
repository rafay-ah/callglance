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

## Project map

CallGlance tells you whether your connection is good enough for calls, and whether the Wi-Fi or
the ISP is to blame. See README.md for the user-facing story.

- `src/callglance/`: the Python service.
  - Engine: `engine.py`, `monitor.py`, `probes.py`, `loop.py`, `stats.py`, `verdict.py`. It runs in
    its own thread on a `selectors` loop and is stdlib-only, with no `gi` imports, so it stays
    testable without PyGObject.
  - `service.py`: the D-Bus API `io.github.rafay_ah.CallGlance1`, which passes JSON strings.
    Notifications, autostart and first-run integration live here too.
  - `gtkui/`: the GTK 3 tray fallback, started by the service when no shell extension registers.
- `gnome-shell/callglance@rafay-ah.github.io/`: the GJS extension (GNOME 45–51), a thin view over
  the D-Bus API.
- `tests/`: pytest.
  - `tests/netsim/` builds a simulated home network from namespaces plus a userspace link emulator.
    Run it with `sudo python3 -m pytest -m netsim`.
- `tools/shellshot/`: a headless GNOME Shell for screenshots and the README GIF (`record-demo.sh`).
- `packaging/`: the .deb, AppImage and extension zip.
- `.github/workflows/`: CI, and a release build that runs on a published release.

## Commands

```sh
python3 -m pytest                      # unit, loopback and D-Bus tests
sudo python3 -m pytest -m netsim       # end-to-end scenarios (root for namespaces)
ruff check src tests tools && shellcheck -x packaging/*.sh tools/shellshot/*.sh
PYTHONPATH=src python3 -m callglance --demo -v   # the app on a simulated connection
packaging/build-deb.sh && packaging/build-appimage.sh
```

## Releasing

Bump `__version__` in `src/callglance/__init__.py`, and add the version to `CHANGELOG.md` and to the
`<releases>` in `data/io.github.rafay_ah.CallGlance.metainfo.xml`; the Release workflow refuses a tag
they don't all match. Pushing a `vX.Y.Z` tag builds, tests and publishes the GitHub release. Cloud
sessions cannot push tags: run the Release workflow (`workflow_dispatch`) on `main` with the `tag`
input instead, which creates the tag and the release.

## Rules learned the hard way

- No root, ever: unprivileged probes only. Ubuntu does not allow ICMP ping sockets by default, so
  the TCP/UDP/DNS fallbacks are the main path there.
- Probes of a method a target ignores must never count as loss. The preflight picks the methods.
- The extension must never block the compositor, so use only async D-Bus calls. Compatibility:
  - Don't pass `affectsInputRegion` to `addChrome`; GNOME 50 throws.
  - Feature-detect `St.BoxLayout` `orientation`; `vertical` is gone in 51.
  - Read `PopupSwitchMenuItem.state` instead of the signal argument.
- St CSS: no negative lengths. They overflow St's size maths and break the popover layout.
