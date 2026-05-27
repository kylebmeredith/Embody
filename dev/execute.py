# me - this DAT
# 
# frame - the current frame
# state - True if the timeline is paused
# 
# Make sure the corresponding toggle is enabled in the Execute DAT.

def init():
	# Log version info for debugging user issues
	parent.Embody.Log(
		f"Embody v{parent.Embody.par.Version.eval()} | "
		f"TouchDesigner {app.version}.{app.build} | "
		f"{parent.Embody.ext.Embody._osLabel()}"
	)
	# Prevent Envoy from auto-starting before init completes.
	# The release .tox may bake in Envoyenable=True and Envoystatus=Running;
	# reset both so the git dialog doesn't fire before the externalizations
	# table is ready and Start() isn't blocked by a stale status.
	# Do NOT store _init_complete here -- keep parexec suppressed until
	# _restoreSettings completes.  TD defers onValueChange callbacks to the
	# next cook cycle, so if _init_complete is stored before those callbacks
	# fire, parexec processes init()'s Envoyenable=False and calls Stop(),
	# disabling Envoy on every startup.  _restoreSettings stores
	# _init_complete when it finishes (or immediately if it returns early).
	parent.Embody.par.Envoyenable = False
	parent.Embody.par.Envoystatus = 'Disabled'


def onStart():
	init()
	# Restore settings from .embody/config.json -- recovers user config after
	# crash, force-quit, or any unsaved session. On normal open where
	# .toe was saved, values match and this is a no-op.
	run(f"op('{parent.Embody}').ext.Embody._restoreSettings(kick_envoy=True)", delayFrames=5)
	# Load op-type defaults + palette catalog (from .embody/catalog_<build>.json
	# if present, otherwise async scan). Needed for TDN export compaction
	# and palette-clone detection.
	run(f"op('{parent.Embody}').ext.CatalogManager.EnsureCatalogs()", delayFrames=10)
	# On project open, silently extract CLAUDE.md if Envoy is
	# enabled but the file is missing (handles upgrades from older versions)
	run(f"op('{parent.Embody}').ext.Embody._upgradeEnvoy()", delayFrames=30)
	# TD handles native .tox auto-load via par.externaltox on every externalized
	# COMP, so no Embody-side restoration cycle is needed. Manual reload from
	# .tdn via ReloadFromTdn(comp_path) or RebuildAllFromTdn() remains available.
	# Rebuild the externalizations table from live par state. The .toe snapshot
	# the table holds at load time is stale -- _scanAndPopulate re-derives it
	# from current par.externaltox / par.file, so the lister shows the correct
	# set of ops without needing a manual Refresh after every project open.
	run(f"op('{parent.Embody}').Refresh()", delayFrames=45)
	# Pin current TD build into .embody/project.json so the Envoy bridge can
	# pick a matching install on fresh clones (committed; survives git clone).
	run(f"op('{parent.Embody}').ext.Embody._writeProjectJson()", delayFrames=60)
	# Pre-warm the Python venv + import mcp.server in the background.  The
	# first `import mcp.server` pulls in uvicorn / starlette / anyio /
	# pydantic_core and costs 2-5 seconds.  If we wait until the user toggles
	# Envoy on, that cost lands on the main thread mid-click and looks like a
	# freeze.  Doing it during boot (after the heavy startup work clears)
	# spreads the cost into idle time so the toggle is snappy.  No-op when
	# already imported.  Skipped when Envoy is intentionally disabled in
	# perform mode -- see Envoyoffinperform.
	run(f"op('{parent.Embody}').ext.Embody._warmEnvoyEnvironment()", delayFrames=120)
	return

def onCreate():
	init()
	# Auto-create (or reconnect) the externalizations table before Verify()
	run(f"op('{parent.Embody}').ext.Embody.CreateExternalizationsTable()", delayFrames=15)
	# Verify handles update-scenario detection and Envoy opt-in
	run(f"op('{parent.Embody}').Verify()", delayFrames=30)
	# Ensure catalogs load on fresh-project drops too, not just onStart.
	# Delayed past Verify() so the setup dialog isn't fighting the scan.
	run(f"op('{parent.Embody}').ext.CatalogManager.EnsureCatalogs()", delayFrames=45)
	return

def onExit():
	return

def onFrameStart(frame):
	# Phase 7 main-thread pumps: previously these were RefreshHook callbacks
	# fired by op.TDResources.ThreadManager. With stdlib threading we drive
	# them ourselves from the Execute DAT's per-frame callback so the tool
	# works on TD 2022.x+ (ThreadManager was first released in 2025.30000).
	#
	# Both are no-ops when no worker is running -- early return is cheap.
	# Defensive try/except: a bug in one pump must not break the other or
	# stall the main thread.
	try:
		parent.Embody.ext.Envoy._onRefresh()
	except Exception as e:
		# Use debug() so a chronic error doesn't spam the log on every frame.
		debug(f'Embody onFrameStart Envoy pump failed: {e}')
	try:
		parent.Embody.ext.TDN._onExportRefresh()
	except Exception as e:
		debug(f'Embody onFrameStart TDN pump failed: {e}')
	try:
		# Mirror project.performMode to Envoy state when Envoyoffinperform
		# is toggled on. Cheap -- boolean compare in steady state, only
		# acts on the edge transitions.
		parent.Embody.ext.Embody._syncEnvoyToPerformMode()
	except Exception as e:
		debug(f'Embody onFrameStart perform-mode sync failed: {e}')
	return

def onFrameEnd(frame):
	return

def onPlayStateChange(state):
	return

def onDeviceChange():
	return

def onProjectPreSave():
	# Clear runtime-only storage that must not bake into the .tox.
	# _git_root is computed fresh at Start() time -- baking it in would cause
	# every user's project to inherit the dev repo path from the release .tox.
	parent.Embody.unstore('_git_root')
	parent.Embody.unstore('_init_complete')
	parent.Embody.unstore('_perform_state')

	# Skip pre-save processing in Perform Mode.
	if parent.Embody.ext.Embody._performMode:
		parent.Embody.ext.Embody.Log('Perform Mode -- skipping pre-save', 'INFO')
		return

	# Sync the table state (detect additions / removals / dirty) so the
	# SaveAllDirty call below sees an accurate picture. Sync is cheap and
	# never writes files. The save itself is gated by Synconsave.
	parent.Embody.ext.Embody.Update(suppress_refresh=True)
	if getattr(parent.Embody.par, 'Synconsave', None) is None \
			or parent.Embody.par.Synconsave.eval():
		parent.Embody.ext.Embody.SaveAllDirty()
	return

def onProjectPostSave():
	# Re-store _init_complete -- pre-save cleared it to avoid baking into
	# the .tox, but the running session still needs it for parexec.
	parent.Embody.store('_init_complete', True)

	# Refresh td_build pin -- the TD that just saved is the one downstream
	# users should launch with on a fresh clone.
	try:
		parent.Embody.ext.Embody._writeProjectJson()
	except Exception as e:
		print(f'Embody > project.json update failed: {e}')

	# Walk the envoy.json registry forward across TD's save-time version
	# bump (e.g. Foo-5.398.toe -> Foo-5.399.toe). Idempotent when basename
	# hasn't changed.
	try:
		if parent.Embody.par.Envoyenable.eval():
			parent.Embody.ext.Envoy.RefreshRegistry()
	except Exception as e:
		print(f'Embody > RefreshRegistry failed: {e}')

	if parent.Embody.ext.Embody._performMode:
		return

	run(f"op('{parent.Embody}').par.Refresh.pulse()", delayFrames=1)
	return
