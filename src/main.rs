//! Headless auto-namer for Zellij tabs and panes (tmux-like).
//!
//! Tab:  focused pane → `N:dir:cmd` / `N:ssh host` (N = 1-based index)
//! Pane: every terminal → `cmd ~/path` / `SSH host`
//!
//! Session rename is handled by fish (`zellij_session_name.fish`) via CLI —
//! background plugins often cannot route RenameSession/RenameTab through client.
//!
//! Critical: never clear rename caches on every TabUpdate, and never rename when
//! the desired name already matches `tab.name` — that caused a TabUpdate loop
//! that pegged the zellij server at 300%+ CPU.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use zellij_tile::prelude::*;

#[derive(Default)]
struct State {
    ready: bool,
    pane_cwds: HashMap<PaneId, PathBuf>,
    pane_cmds: HashMap<PaneId, Vec<String>>,
    /// 0-based tab position → focused terminal pane
    focused_terminal_by_tab: HashMap<usize, PaneId>,
    /// 0-based tab position → stable tab id
    tab_id_by_position: HashMap<usize, u64>,
    /// 0-based tab position → last observed tab name from TabUpdate
    tab_name_by_position: HashMap<usize, String>,
    /// pane → 0-based tab position
    pane_tab_position: HashMap<PaneId, usize>,
    /// tab_id → last name we successfully decided to apply
    last_tab_name: HashMap<u64, String>,
    last_pane_name: HashMap<PaneId, String>,
}

register_plugin!(State);

impl ZellijPlugin for State {
    fn load(&mut self, _configuration: BTreeMap<String, String>) {
        request_permission(&[
            PermissionType::ReadApplicationState,
            PermissionType::ChangeApplicationState,
        ]);
        subscribe(&[
            EventType::PermissionRequestResult,
            EventType::PaneUpdate,
            EventType::TabUpdate,
            EventType::CwdChanged,
            EventType::CommandChanged,
            EventType::PaneClosed,
        ]);
        set_selectable(false);
    }

    fn update(&mut self, event: Event) -> bool {
        match event {
            Event::PermissionRequestResult(status) => {
                self.ready = matches!(status, PermissionStatus::Granted);
                // No get_focused_pane_info / no git here — both were noisy/broken
                // for load_plugins plugins. Wait for PaneUpdate / CwdChanged.
            }
            Event::TabUpdate(tabs) => {
                self.on_tab_update(tabs);
            }
            Event::PaneUpdate(manifest) => {
                self.on_pane_update(manifest);
            }
            Event::CwdChanged(pane_id, cwd, _clients) => {
                if !matches!(pane_id, PaneId::Terminal(_)) {
                    return false;
                }
                let changed = self.pane_cwds.get(&pane_id) != Some(&cwd);
                self.pane_cwds.insert(pane_id, cwd);
                if changed {
                    self.refresh_pane_name(pane_id);
                    self.refresh_tab_for_pane(pane_id);
                }
            }
            Event::CommandChanged(pane_id, argv, is_fg, _clients) => {
                if !matches!(pane_id, PaneId::Terminal(_)) || !is_fg {
                    return false;
                }
                let changed = self.pane_cmds.get(&pane_id) != Some(&argv);
                self.pane_cmds.insert(pane_id, argv);
                if changed {
                    self.refresh_pane_name(pane_id);
                    self.refresh_tab_for_pane(pane_id);
                }
            }
            Event::PaneClosed(pane_id) => {
                self.pane_cwds.remove(&pane_id);
                self.pane_cmds.remove(&pane_id);
                self.last_pane_name.remove(&pane_id);
                self.pane_tab_position.remove(&pane_id);
                self.focused_terminal_by_tab.retain(|_, p| *p != pane_id);
            }
            _ => {}
        }
        false
    }

    fn render(&mut self, _rows: usize, _cols: usize) {}
}

impl State {
    fn on_tab_update(&mut self, tabs: Vec<TabInfo>) {
        if !self.ready {
            // Still refresh maps so we are warm after permission grant
        }

        let mut new_ids: HashMap<usize, u64> = HashMap::new();
        let mut new_names: HashMap<usize, String> = HashMap::new();
        for tab in &tabs {
            new_ids.insert(tab.position, tab.tab_id as u64);
            new_names.insert(tab.position, tab.name.clone());
        }

        // Drop cache entries for gone tab ids only — do NOT clear everything
        let live_ids: std::collections::HashSet<u64> = new_ids.values().copied().collect();
        self.last_tab_name.retain(|id, _| live_ids.contains(id));

        self.tab_id_by_position = new_ids;
        self.tab_name_by_position = new_names;

        // Recompute names when positions shifted (index prefix in title).
        // refresh_tab_at_position skips if desired == current tab.name.
        if self.ready {
            let positions: Vec<usize> = self.focused_terminal_by_tab.keys().copied().collect();
            for pos in positions {
                self.refresh_tab_at_position(pos);
            }
        }
    }

    fn on_pane_update(&mut self, manifest: PaneManifest) {
        self.pane_tab_position.clear();

        for (tab_index, panes) in manifest.panes {
            for pane in panes {
                if pane.is_plugin {
                    continue;
                }
                let pane_id = PaneId::Terminal(pane.id);
                self.pane_tab_position.insert(pane_id, tab_index);

                // Seed command only if we have nothing yet (don't thrash on every update)
                if let Some(ref cmd) = pane.terminal_command {
                    if !cmd.is_empty() {
                        self.pane_cmds
                            .entry(pane_id)
                            .or_insert_with(|| vec![cmd.clone()]);
                    }
                }

                if pane.is_focused {
                    let prev = self.focused_terminal_by_tab.insert(tab_index, pane_id);
                    if self.ready && prev != Some(pane_id) {
                        self.refresh_tab_at_position(tab_index);
                    }
                }

                // Only rename pane if we don't already match last applied name
                if self.ready {
                    self.refresh_pane_name(pane_id);
                }
            }
        }

        // Drop focus map entries for tabs that disappeared
        let live_tabs: std::collections::HashSet<usize> =
            self.pane_tab_position.values().copied().collect();
        self.focused_terminal_by_tab
            .retain(|pos, _| live_tabs.contains(pos));
    }

    fn refresh_tab_for_pane(&mut self, pane_id: PaneId) {
        let Some(&tab_pos) = self.pane_tab_position.get(&pane_id) else {
            return;
        };
        if self.focused_terminal_by_tab.get(&tab_pos) != Some(&pane_id) {
            return;
        }
        self.refresh_tab_at_position(tab_pos);
    }

    fn refresh_tab_at_position(&mut self, tab_position: usize) {
        if !self.ready {
            return;
        }
        let Some(pane_id) = self.focused_terminal_by_tab.get(&tab_position).copied() else {
            return;
        };
        let name = self.tab_name_for_pane(pane_id, tab_position);

        // Skip if UI already shows this name (breaks TabUpdate feedback loop)
        if self.tab_name_by_position.get(&tab_position) == Some(&name) {
            if let Some(tab_id) = self.tab_id_by_position.get(&tab_position) {
                self.last_tab_name.insert(*tab_id, name);
            }
            return;
        }

        let tab_id = self.tab_id_by_position.get(&tab_position).copied();
        let cache_key = tab_id.unwrap_or(tab_position as u64 | 0xFFFF_0000_0000_0000);
        if self.last_tab_name.get(&cache_key) == Some(&name) {
            return;
        }
        self.last_tab_name.insert(cache_key, name.clone());

        // Only rename_tab_with_id — positional rename_tab is 1-based and often
        // fails routing for load_plugins ("failed to route action for client").
        if let Some(tab_id) = tab_id {
            rename_tab_with_id(tab_id, &name);
            // Optimistically update observed name so a follow-up TabUpdate
            // does not rename again before the event arrives.
            self.tab_name_by_position.insert(tab_position, name);
        }
    }

    fn refresh_pane_name(&mut self, pane_id: PaneId) {
        if !self.ready || !matches!(pane_id, PaneId::Terminal(_)) {
            return;
        }
        let name = self.pane_label_for_pane(pane_id);
        if self.last_pane_name.get(&pane_id) == Some(&name) {
            return;
        }
        self.last_pane_name.insert(pane_id, name.clone());
        rename_pane_with_id(pane_id, &name);
    }

    /// `N:dir:cmd` / `N:ssh host`
    fn tab_name_for_pane(&self, pane_id: PaneId, tab_position: usize) -> String {
        let idx = tab_position.saturating_add(1);
        let path_base = self
            .pane_cwds
            .get(&pane_id)
            .map(|p| path_basename(p))
            .unwrap_or_else(|| "?".into());

        let (cmd, argv) = self.cmd_of(pane_id);

        if cmd == "ssh" {
            let host = argv
                .map(|a| ssh_host_from_argv(a))
                .unwrap_or_else(|| "?".into());
            return format!("{idx}:ssh {host}");
        }

        format!("{idx}:{path_base}:{cmd}")
    }

    /// `cmd ~/path` / `SSH host`
    fn pane_label_for_pane(&self, pane_id: PaneId) -> String {
        let path = self
            .pane_cwds
            .get(&pane_id)
            .map(|p| path_display(p))
            .unwrap_or_else(|| "?".into());

        let (cmd, argv) = self.cmd_of(pane_id);

        if cmd == "ssh" {
            let host = argv
                .map(|a| ssh_host_from_argv(a))
                .unwrap_or_else(|| "?".into());
            return format!("SSH {host}");
        }

        format!("{cmd} {path}")
    }

    fn cmd_of(&self, pane_id: PaneId) -> (String, Option<&Vec<String>>) {
        let argv = self.pane_cmds.get(&pane_id);
        let cmd = argv
            .and_then(|a| a.first())
            .map(|s| {
                Path::new(s)
                    .file_name()
                    .and_then(|f| f.to_str())
                    .unwrap_or(s)
                    .to_string()
            })
            .unwrap_or_else(|| "shell".into());
        (cmd, argv)
    }
}

fn path_basename(path: &Path) -> String {
    if let Some(home) = std::env::var_os("HOME") {
        if path == Path::new(&home) {
            return "~".into();
        }
    }
    path.file_name()
        .and_then(|s| s.to_str())
        .map(|s| s.to_string())
        .unwrap_or_else(|| path.display().to_string())
}

fn path_display(path: &Path) -> String {
    if let Some(home) = std::env::var_os("HOME") {
        let home = PathBuf::from(home);
        if path == home.as_path() {
            return "~".into();
        }
        if let Ok(rest) = path.strip_prefix(&home) {
            return format!("~/{}", rest.display());
        }
    }
    path.display().to_string()
}

fn ssh_host_from_argv(argv: &[String]) -> String {
    let mut skip_next = false;
    for arg in argv.iter().skip(1) {
        if skip_next {
            skip_next = false;
            continue;
        }
        if arg.len() == 2
            && arg.starts_with('-')
            && "bBcDEeFIiJLlmOopQRSWw".contains(arg.chars().nth(1).unwrap_or('\0'))
        {
            skip_next = true;
            continue;
        }
        if arg.starts_with('-') {
            continue;
        }
        if let Some((_, host)) = arg.rsplit_once('@') {
            return host.to_string();
        }
        return arg.clone();
    }
    "?".into()
}
