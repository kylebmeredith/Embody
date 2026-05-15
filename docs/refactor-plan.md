# Embody Fork — Externalize-style refactor

## Context

This fork keeps what Embody does well (TDN JSON format, Envoy MCP server, the UI shell, restore-on-open) and replaces the parts that felt heavy: tag-based discovery, the persistent `externalizations.tsv` tracking table, mirrored folder structure, and auto-injected Build metadata.

The new model takes Externalize's discovery philosophy:
- An op is "externalized" iff TD's own native parameter says so — `par.externaltox != ''` for COMPs, `par.file != ''` for DATs. No tags. No tracking table on disk.
- Folder layout is flat by default — user picks a folder, ops save as `{folder}/{op.name}.tox` (+ `.tdn` sidecar). No hierarchy mirroring. Name collisions warn the user.
- Dirty state lives in memory only — TD's `op.dirty` flag plus an in-memory `ParameterTracker`.

User design decisions (locked):
- **Output**: always emit both `.tox` and `.tdn`. `.tox` is canonical (TD restores it natively via `par.externaltox`). `.tdn` is the diff sidecar. Keep TDN→TOX rebuild as an exposed function/MCP tool, but not automatic at load.
- **Build/Date metadata**: comment out auto-injection. Can be re-enabled later behind a setting.
- **UI**: tree-vs-flat toggle via a new parameter on the Embody COMP.

Additional Externalize features being ported:
- **DAT exclude list** — skip TD-internal DAT types (`eval`, `keyboardin`, `opfind`, `folder`, `examine`, `select`, `udpout`, `udpin`, `script`, `null`, `info`) when scanning by `par.file`.
- **Palette-folder expression trick** — when a save path lands inside `app.userPaletteFolder`, set `par.externaltox.expr` to `app.userPaletteFolder + '/...'` instead of a literal path. Keeps palette toxes portable across machines.
- **Default folder params** — `Defaulttoxfolder` / `Defaultscriptfolder`. If set and exists, save silently. Otherwise prompt.
- **Contextual action menu** (Ctrl+W) — single shortcut opens a dialog whose options depend on what's selected. Replaces scattered shortcuts. Action set evolves with context (see Phase 2.5).
- **Filter by dirty** — UI toggle that hides clean ops from the list.
- **Sync on save** — opt-in toggle that runs Update on every `project.save()`. Replaces Embody's always-on pre-save hook.
- **Save all (force)** — button that bypasses dirty checks and re-saves every externalized op.

TDN-as-source-of-truth controls:
- **MCP `import_network`** (already exists) — LLM closes its own edit loop after modifying a `.tdn`. Document in skills so the agent always reaches for it after a `.tdn` edit.
- **Per-op "Reload from TDN"** — row action and contextual-menu option. Rebuilds one COMP from its sidecar `.tdn`.
- **Global "Rebuild all from TDN"** — toolbar button. Scans the externalization folder for `.tdn` files and rebuilds each. Handles fresh-clone bootstrap and bulk re-sync after `git pull`.
- **Skipped for v1**: passive mtime-mismatch detection on project open. Revisit later.

---

## Phases

Each phase is one commit and leaves the codebase working.

### Phase 1 — Discovery model swap

Replace tag-driven discovery with par-driven discovery. Same return types, so downstream code doesn't break.

**Files:**
- `dev/embody/Embody/EmbodyExt.py`
  - `getTaggedOps()` (line 2041): replace tag scan with par-driven scan. Module-level constant `_DAT_EXCLUDE_TYPES = {'eval','keyboardin','opfind','folder','examine','select','udpout','udpin','script','null','info'}`. Body becomes `[c for c in root.findChildren(type=COMP) if c.par.externaltox.eval() and not _isClone(c)] + [d for d in root.findChildren(type=DAT) if d.par.file.eval() and d.type not in _DAT_EXCLUDE_TYPES and not _isClone(d)]`.
  - Add `_isClone(op)` and `_isInsideClone(op)` helpers — port from `C:\Users\Kyle\dev\touch\Externalize\Externalize.py:649-668`.
  - `getTags()` (line 1984): retain for backward-compat callers but make it return `[]` or only the externaltox-derived list.
  - `isOpEligibleToBeExternalized()` (line 2089): replace tag intersection with par check.
  - `getExternalizedOps(family, strategy=...)` (called at 1868, 1881): collapse — strategy parameter becomes ignored (always-both). Filter by family only.
- `dev/embody/Embody/parexec.py`: no change needed if it just delegates to `Update()`.

**Verification**: Open `dev/Embody-5.toe`. Set `par.externaltox` on a fresh COMP that has no Embody tags. Pulse Update. Confirm it externalizes. Untag a previously-tagged COMP. Confirm it's still externalized as long as `par.externaltox` is set. Create a `scriptDAT` with a `par.file` value — confirm it does NOT appear in the externalization list (exclude list working).

### Phase 2 — Flat path model + always-both output

Stop mirroring the network hierarchy. Emit `.tdn` alongside `.tox` for every COMP save.

**Files:**
- `dev/embody/Embody/EmbodyExt.py`
  - `getOpPaths()` (line 479-546): remove lines 519-520 and 526 (the `parent_components` block). Path becomes `{folder}/{op.name}.tox`. Add collision detection — if a different op already has that filename in `par.externaltox` (or the file exists from a different op), prompt via `ui.messageBox` to confirm overwrite or cancel.
  - `_buildTDNRelPath()` (line 2800): same flattening. Path becomes `{folder}/{op.name}.tdn`.
  - `Save()` (line 2169): after saving the `.tox`, immediately call the TDN exporter (already exists in `TDNExt.py`) to write the `.tdn` sidecar.
  - **Palette-folder special-case**: after computing the absolute save path, if it starts with `app.userPaletteFolder`, set `par.externaltox.expr = f"app.userPaletteFolder + '/{rel_to_palette}'"` instead of assigning a literal value. Port from `Externalize.py:510-515` (`Save_tox`). Use forward slashes in the expression regardless of OS.
- `dev/embody/Embody/TDNExt.py`: expose a `RebuildToxFromTdn(comp_path, tdn_path)` method as a callable (for MCP/manual use) — this functionality already exists inside the strip/restore cycle; just needs to be a public method.

**Verification**: Externalize a COMP at `/project1/scene/btn`. Confirm a single `.tox` and `.tdn` land at `{folder}/btn.tox` and `{folder}/btn.tdn` (not in a nested folder). Externalize a second COMP also named `btn` at `/project1/other/btn` — confirm warn dialog appears. Diff a `.tdn` file before and after a parameter change. Externalize a COMP into `app.userPaletteFolder/Some/Sub` — confirm `par.externaltox` ends up as an expression referencing `app.userPaletteFolder`, not a literal path.

### Phase 2.5 — Save UX (default folder + contextual action menu)

Smoother save workflow ported from Externalize, evolved into a context-aware action menu. No engine changes — pure UX layer.

**Files:**
- Embody COMP custom params: add
  - `Defaulttoxfolder` (Folder, default empty) — if set and exists on disk, COMP saves go here silently. If empty/missing, prompt.
  - `Defaultscriptfolder` (Folder, default empty) — same for DAT externalization.
  - `Synconsave` (Toggle, default `True`) — see Phase 4.
- `dev/embody/Embody/EmbodyExt.py`:
  - Add `OpenActionMenu(op)` method — single dialog whose options depend on op state. Inspired by `Externalize.Prompt_to_save` (`Externalize.py:185-242`) but expanded:

    **COMP, not externalized:**
    - Externalize to default folder
    - Externalize to custom folder

    **COMP, already externalized:**
    - Save existing
    - Reload from TDN  ← rebuilds the COMP from its `.tdn` sidecar via `TDNExt.ImportNetwork`
    - Reveal in Explorer
    - Re-externalize to default folder
    - Re-externalize to custom folder
    - Remove externalization

    **DAT, not externalized / externalized:** same shape with `par.file`. No TDN reload option for DATs.

  - Add `_getSaveLocation(op, use_default, is_tox)` helper — port `Externalize.Get_save_location` (line 326). Returns relative path from project folder, handles palette-folder case, falls back to `ui.chooseFolder` if default is empty/missing.
  - Add `ReloadFromTdn(comp)` method — wraps `TDNExt.ImportNetwork(comp.path, json.load(open(tdn_path)), clear_first=True)`. Used by both the menu action and the per-row button (Phase 4).
- Keyboard shortcut: bind Ctrl+W to `OpenActionMenu(current_op)`. Check Embody's existing keybindings first to avoid conflicts.

**Dialog UI implementation note**: `ui.messageBox` arranges buttons horizontally and becomes unusable past ~4 options. The full action menu has 6+ options for externalized COMPs. Implementation will need a custom popup panel (a `windowCOMP` containing a `listCOMP` of actions) rather than `ui.messageBox`. Design the panel once, reuse it for both Ctrl+W and any future palette-style entries. Layout: vertical list, keyboard navigable (arrow keys + Enter), Esc to cancel, click outside to dismiss. Treat this as a small UI sub-project inside Phase 2.5.

**Verification**: Set `Defaulttoxfolder` to a valid relative path. Select an unexternalized COMP. Hit Ctrl+W. Confirm the menu shows two externalize options. Pick "Default" — confirm save without further prompts. Select the now-externalized COMP. Hit Ctrl+W. Confirm the menu shows the six options including "Reload from TDN". Edit the `.tdn` on disk (change a parameter value). Pick "Reload from TDN". Confirm the change appears in TD.

### Phase 3 — Rip persistent tracking + Build metadata

Drop the tsv on disk. Keep the in-memory `Externalizations` DAT (the UI reads from it) but populate it from a live par-driven scan on refresh. Comment out Build/Date/Touchbuild injection.

**Files:**
- `dev/embody/Embody/EmbodyExt.py`
  - `createExternalizationsTable()` (line 1025): keep, but stop loading from the `.tsv` file. The table is rebuilt by a new `_scanAndPopulate()` method run on Update / refresh.
  - `processAddition()` / `processRemoval()` `appendRow`/`deleteRow` callsites (lines 2916, 2918, 2935, 4214, 4217, 4795, 5273): become no-ops or call the scan refresh.
  - `checkOpsForContinuity()` (line 2998), `_checkExternalToxPar()` (3000), `_findMovedOp()` (3203), `updateMovedOp()` (3770), `cleanupDuplicateRows()` (3874): early-return / delete. TD handles `par.externaltox` continuity natively — if a COMP is renamed or moved, its `par.externaltox` is preserved.
  - `setupBuildParameters()` (line 2942-2961): comment out the body. Leave the function so callers still work. Add a `// TODO: re-enable behind a setting` note.
  - `Save()` (2181-2190) and `SaveTDN()` (2249-2258): comment out the `setupBuildParameters` calls.
- Delete (or stop writing) `dev/embody/externalizations.tsv`.

**Verification**: Close the project. Delete `dev/embody/externalizations.tsv` on disk. Reopen. Confirm the UI list still populates from a fresh scan. Save a tox. Confirm no Build/Date/Touchbuild params get added to its About page.

### Phase 4 — UI tree/flat toggle + filter/sort/sync + TDN reload controls

Add the new UI toggles and behaviors. Tree/flat mode for the list. Filter-by-dirty. Sync-on-save. Save-all-force button. TDN reload (per-op + global).

**Files:**
- Embody COMP custom params: add
  - `Listmode` (Menu, names=`['tree','flat']`, default=`'flat'`).
  - `Filterdirty` (Toggle, default `False`) — when on, hide rows where `dirty == ''` / `False`.
  - `Synconsave` (already added in Phase 2.5) — gates the `onProjectPreSave` Update call.
  - `Saveallforce` (Pulse) — button to re-save every externalized op regardless of dirty state.
  - `Rebuildallfromtdn` (Pulse) — button to scan the externalization folder for `.tdn` files and rebuild each via `ImportNetwork`. Handles fresh-clone bootstrap and bulk re-sync after `git pull`.
- `ui/Embody/list/inject_parents` callbacks (find via grep for `inject_parents`):
  - In `flat` mode, output one row per externalized op directly. Skip `expanded_paths`/`expand_order` logic. `depth=0` always, `has_children='0'` always.
  - In `tree` mode, retain current behavior.
  - Apply `Filterdirty` filter at the same stage as the existing text filter (`inject_parents_callbacks.py:58-85`).
- `list/list1` callbacks:
  - Hide the expand column (`COL_EXPANDO`) when `Listmode='flat'`.
  - Add a "Reload from TDN" row action — either a new column with a reload icon, or accessible via row right-click / a new column added next to the existing delete `x`. Calls `ReloadFromTdn(comp)` from Phase 2.5 for COMPs only (DAT rows hide the action).
- Strategy column (`COL_STRATEGY`): rename header to "Type" or remove — every op now has `.tox + .tdn`, so the column is informational only.
- `dev/embody/Embody/execute.py:114` (`onProjectPreSave`): wrap the existing `Update()` call with `if self.ownerComp.par.Synconsave.eval(): ...`.
- `dev/embody/Embody/EmbodyExt.py`:
  - Add `SaveAllForce()` method — port `Externalize.Save_all_force` (line 258). Iterates every COMP with `par.externaltox != ''`, skips clones, calls `Save()` regardless of dirty state. Same pattern for DATs with `par.file != ''`.
  - Add `RebuildAllFromTdn()` method — iterates COMPs with `par.externaltox != ''`, computes the sidecar `.tdn` path (same folder, same basename), calls `ReloadFromTdn(comp)` for each. Skip if `.tdn` missing. Log a summary.
  - Wire both to their pulse parameters via `parexec.py`.
- Toolbar UI: add a dirty-filter toggle button next to the existing search box (`/ui/Embody/toolbar/container_right`). Add a "Rebuild all from TDN" button to the toolbar.

**Verification**: Toggle `Listmode` between `tree` and `flat`. Toggle `Filterdirty` — confirm clean rows disappear. Toggle `Synconsave` off, save the project — confirm Update does NOT run automatically. Pulse `Saveallforce` — confirm every externalized op is re-saved (check file mtimes). Edit a `.tdn` file on disk, click the row's reload action — confirm the change appears in TD. Delete a tox from the network, pulse `Rebuildallfromtdn` — confirm the COMP is rebuilt from its `.tdn` sidecar.

### Phase 5 — Cleanup

Remove the dead tag-system code now that nothing depends on it.

**Files:**
- `dev/embody/Embody/EmbodyExt.py`: delete `getTags()`, palette-scan helpers, tag-related custom params (`Toxtag`, `Tdntag`, `Pytag`, etc.) — verify no callsites remain via grep before deleting.
- Update `CLAUDE.md` and any docs referencing tags as the discovery mechanism.
- Update `docs/changelog.md` with a v6.0.0 (or fork-versioned) entry describing the philosophical change.

**Verification**: Full project open + save + Update cycle. Run the existing test suite via `/run-tests`. Grep for `Toxtag`, `Tdntag`, `tags` references — confirm cleanup is complete.

---

## Critical files reference

| File | Role | Phases touched |
|---|---|---|
| `dev/embody/Embody/EmbodyExt.py` | Core engine | 1, 2, 2.5, 3, 4, 5 |
| `dev/embody/Embody/TDNExt.py` | TDN export/import | 2 |
| `dev/embody/Embody/parexec.py` | Param callbacks | 1 (read-only check), 4 (Saveallforce wire) |
| `dev/embody/Embody/execute.py` | Lifecycle | 3, 4 (Synconsave gate) |
| Embody COMP custom params | UI controls | 2.5, 4, 5 |
| `ui/Embody/list/inject_parents` callbacks | List feeder | 4 |
| `ui/Embody/list/list1` callbacks | Row interaction | 4 |
| `ui/Embody/toolbar/container_right` | Toolbar | 4 (dirty filter widget) |
| `dev/embody/externalizations.tsv` | Old tracking file | 3 (delete) |
| `CLAUDE.md`, `docs/` | Documentation | 5 |

## Functions / utilities to reuse

All paths below are in `C:\Users\Kyle\dev\touch\Externalize\Externalize.py`:

- `ParameterTracker` (lines 64-128) and `Is_operator_dirty` (lines 244-248) — reference implementation of dirty detection without a tracking table. Port the pattern.
- `is_clone` / `is_inside_clone` (lines 649-668) — proven clone-filtering. Port directly.
- `Get_save_location` (line 326) — folder-picker logic with default-folder fallback and palette-folder handling. Used in Phase 2.5.
- `Prompt_to_save` (line 185) — single-dialog save UX. Port the dialog flow for Phase 2.5.
- `Save_all_force` (line 258) — force-save every externalized op. Port for Phase 4.
- `Save_tox` palette-folder block (lines 510-515) — the `app.userPaletteFolder + '/...'` expression trick. Used in Phase 2.
- `find_all_dats` exclude list (lines 597-620) — DAT types to skip when scanning by `par.file`. Used in Phase 1.
- `Sync_on_save` (line 291) — Ctrl+S gating pattern. Used in Phase 4.
- `TDNExt.ExportNetwork` (inside the fork) — already does the TDN sidecar export. Phase 2 just adds a call to it.

## Future phases / roadmap (post Phase 4)

### Phase 6 — Release / unexternalize

Port `C:\Users\Kyle\dev\touch\Externalize\Release.py` into Embody so the same tool that externalizes a COMP can also export a release-ready copy of it. Two flavors:

**Per-COMP release** (extends `ExportPortableTox`):
- Prompt for new name and version via `TDResources.PopDialog` (Externalize-style two-step dialog: name → version → save folder).
- On a temporary copy of the target COMP, recursively clear `par.externaltox` / `par.enableexternaltox` on COMPs and `par.file` / `par.syncfile` on DATs (port `Externalize.Release.unexternalizeOperator` — `Release.py:15-26`).
- Reset all custom params to their `defaultMode` / `default` / `defaultExpr` / `defaultBindExpr`. Skip the `About` page so version/build/timestamp metadata stays.
- **Before save: set the COMP's current parameter page to its first custom page** so when the user opens the released `.tox` they see the intended page.
- Save the copy as `{Name}_{Version}.tox` to a user-chosen folder. Destroy the copy.
- Hook into the action menu as a new option (probably under "More..."): `Release...`.

**Project-wide release** (new):
- Walk every par-set op in the project, snapshot their external-file pars in memory.
- Clear `par.externaltox` / `par.file` / `par.syncfile` everywhere.
- `project.save(chosen_path)` to a fresh `.toe` location.
- Restore the snapshotted pars so the live session is unaffected (same strip/restore pattern Embody already uses for TDN saves).
- Surface as a toolbar button + dialog (`Release Project...` → choose path + optional name/version metadata).
- Reuse the per-COMP page-reset rule for any embedded COMPs that should land on a particular page.

Retire `C:\Users\Kyle\dev\touch\Externalize\Release.py` once both flavors live inside Embody.

## Risks / open issues

- **Clones / palette-clones**: Embody has special-case logic in `getOpPaths` and `_buildTDNRelPath` for palette-cloned COMPs (`_PALETTE_CLONE_SKIP_PARAMS`). Externalize sidesteps clones entirely by filtering them out. Confirm the fork can also just exclude clones — if a user wants to externalize a clone master, they remove the clone parameter first.
- **Multiple ops with same name**: With flat layout + collision warning, the user must rename. Document this. Don't add an auto-suffix fallback — it creates the "confusing duplicate" problem the user explicitly wants to avoid.
- **Backward compatibility**: Existing Embody projects with tagged ops won't auto-migrate. Either (a) document a one-time migration script that sets `par.externaltox` on all currently-tagged COMPs, or (b) make Phase 1 read both tags AND par.externaltox as the discovery rule, then deprecate tags later. Recommend (a) for cleanliness — the fork is a hard break.
- **MCP tool surface (Envoy)**: `externalize_op`, `save_externalization`, `get_externalizations` MCP tools may have signatures referencing tags or strategy. Audit during Phase 1 — likely need parameter signature changes, which is a breaking API change.

## End-to-end verification

After all phases:

1. Open `dev/Embody-5.toe`.
2. Create a fresh `baseCOMP` at `/project1/test`. Set `par.externaltox = 'externals/test.tox'`.
3. Pulse Update. Confirm `externals/test.tox` and `externals/test.tdn` exist on disk.
4. Open `externals/test.tdn` in a text editor. Modify a parameter value. Save.
5. Reload the COMP. Confirm the change appears in TD.
6. Close project, delete `externalizations.tsv` if present, reopen. Confirm the UI list shows the externalized COMP.
7. Toggle `Listmode` between tree/flat. Confirm both render.
8. Run `/run-tests`. Confirm tests pass (or document which tests need updating).
9. Verify via Envoy MCP tools that `query_network`, `externalize_op`, and TDN export work.
