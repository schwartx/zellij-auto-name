# zellij-auto-name

Headless Zellij plugin (WASM) that auto-names **tabs** and **panes** from the focused process — no shell hooks.

tmux analogue: **window ≈ tab**, **pane border ≈ pane name**.

## Naming

Driven by `CwdChanged` + `CommandChanged` (true foreground process, like tmux `pane_current_*`).

### Tabs

Focused terminal pane owns the tab title:

| State | Format | Example |
|-------|--------|---------|
| normal | `N:dir:cmd` | `1:zellij-auto-name:fish` |
| `$HOME` | `N:~:cmd` | `1:~:fish` |
| `ssh …` | `N:ssh host` | `2:ssh box` |

`N` is the 1-based tab index.

### Panes

Every terminal pane title:

| State | Format | Example |
|-------|--------|---------|
| normal | `cmd ~/path` | `nvim ~/src/foo` |
| `ssh …` | `SSH host` | `SSH box` |

### Sessions

Not handled here. Background plugins often fail to route `RenameSession`; use a shell hook / CLI if you need it (e.g. fish `zellij_session_name.fish`).

## Install

### From GitHub Release

Push a tag `v*` to trigger CI; the release ships `zellij-auto-name.wasm`.

```bash
mkdir -p ~/.config/zellij/plugins
curl -fsSL -o ~/.config/zellij/plugins/zellij-auto-name.wasm \
  "https://github.com/schwartx/zellij-auto-name/releases/latest/download/zellij-auto-name.wasm"
```

### From source

```bash
rustup target add wasm32-wasip1
./build.sh
```

`build.sh` builds for `wasm32-wasip1` and installs to:

```text
~/.dotfiles/zellij/plugins/zellij-auto-name.wasm
```

Manual install elsewhere:

```bash
cargo build --release --target wasm32-wasip1
cp target/wasm32-wasip1/release/zellij-auto-name.wasm ~/.config/zellij/plugins/
```

### Publish a release

```bash
git tag v0.1.0
git push origin v0.1.0
```

GitHub Actions builds the WASM and creates a Release with the artifact.

## Load

In `config.kdl` (path must match where you installed the wasm):

```kdl
plugins {
    auto-name location="file:~/.dotfiles/zellij/plugins/zellij-auto-name.wasm"
}
load_plugins {
    auto-name
}
```

### Permissions (required)

Without grant, renames are no-ops. Accept the on-screen dialog once, or cache:

`~/.cache/zellij/permissions.kdl`

```kdl
"/home/YOU/.dotfiles/zellij/plugins/zellij-auto-name.wasm" {
    ReadApplicationState
    ChangeApplicationState
}
```

Key must be the absolute plugin path (no `file:` prefix). See `permissions.kdl` in this repo.

### Reload after rebuild

```bash
./build.sh
zellij action start-or-reload-plugin auto-name
# or open a new session
```

## Notes

- Renames use `rename_tab_with_id` / `rename_pane_with_id` (stable ids). Positional `rename_tab` is 1-based and often fails routing for `load_plugins`.
- Desired name is compared against the current UI name before renaming — avoids a TabUpdate feedback loop that can peg the server CPU.

## License

[MIT](LICENSE)
