"""
Embody - Automatic TOX and DAT Externalization for TouchDesigner

Embody automatically creates, maintains and updates tox and DAT file
externalizations for your project, supporting a variety of file formats.

Simply add your preferred tags for COMPs/DATs to be saved, and on ctrl-s
external file references will automatically be created and/or updated.

Author: Dylan Roscover
"""

from __future__ import annotations

import os
import subprocess
import sys
import shutil
import inspect
from collections import deque
from datetime import datetime
from pathlib import Path
from glob import glob
from typing import Optional, Union, Any


class EmbodyExt:
    """
    Main extension class for Embody - manages externalization of
    TouchDesigner COMPs and DATs to external files.
    """

    # Rule DAT name -> slug (shared across all AI clients)
    _TEMPLATE_MAP_RULES = {
        'text_rule_network_layout':          'network-layout',
        'text_rule_td_python':               'td-python',
        'text_rule_mcp_safety':              'mcp-safety',
        'text_rule_parameters':              'parameters',
    }

    # Skill DAT name -> slug (Claude Code only)
    _TEMPLATE_MAP_SKILLS = {
        'text_skill_create_operator':     'create-operator',
        'text_skill_debug_operator':      'debug-operator',
        'text_skill_externalize':         'externalize-operator',
        'text_skill_create_extension':    'create-extension',
        'text_skill_manage_annotations':  'manage-annotations',
        'text_skill_td_api_reference':    'td-api-reference',
        'text_skill_mcp_tools_reference': 'mcp-tools-reference',
    }

    # Parameters persisted to .embody/config.json across upgrades.
    # Explicit whitelist -- new params default to "not persisted" until added.
    _PERSISTED_PARAMS = frozenset({
        # Core
        'Envoyenable', 'Envoyport', 'Aiclient',
        # Behavior
        'Logfolder', 'Logtofile', 'Verbose', 'Print',
        'Localtimestamps',
        # Action menu / save UX (Phase 2.5)
        'Defaulttoxfolder', 'Defaultscriptfolder', 'Synconsave',
        # Manager filters + view mode (Phase 4)
        'Filterdirty', 'Filterdats', 'Listmode',
        # TDN sidecar behavior
        'Embeddatsintdns', 'Embedstorageintdns', 'Tdndatsafety',
        'Tdnstriponsave', 'Filecleanup',
        # Performance-mode envoy gating
        'Envoyoffinperform',
        # Auto-deploy release tox to consumer projects
        'Releasetargets',
        # Node coloring for externalized ops
        'Colorexternalized',
        'Compcolorr', 'Compcolorg', 'Compcolorb',
        'Datcolorr', 'Datcolorg', 'Datcolorb',
        # Externalization defaults applied on _setupCompForExternalization
        'Reloadcustom', 'Reloadbuiltin', 'Savebackup',
    })

    # ==========================================================================
    # INITIALIZATION
    # ==========================================================================

    def __init__(self, ownerComp: COMP) -> None:
        self.my = ownerComp

        # Suppress TD ThreadManager's benign "fallback strategy" warning that
        # fires on every standalone EnqueueTask call (used by Envoy and TDN).
        import logging
        logging.getLogger('TDAppLogger.threadManager_logger').setLevel(logging.ERROR)

        self.lister = self.my.op('list/list1')
        self.tagging_menu_window = self.my.op('window_tagging_menu')
        self.tagger = self.my.op('tagger')
        self.root = op('/')
        self._tagger_mode = 'tag'  # 'tag' or 'manage'
        
        # Logging configuration
        self.header = 'Embody >'
        self.debug_mode = False  # Set to True for verbose path logging
        self._log_buffer = deque(maxlen=200)
        self._log_counter = 0
        self._fifo = self.my.op('fifo1')

        # Enable file logging by default
        if not self.my.par.Logfolder.eval():
            self.my.par.Logfolder = 'logs'
        if not self.my.par.Logtofile:
            self.my.par.Logtofile = True
        
        # Supported operator types for DAT externalization
        self.supported_dat_types = [
            'text', 'table', 'execute', 'parexec', 'pargroupexec',
            'chopexec', 'datexec', 'opexec', 'panelexec'
        ]

        # Default file extension for each supported DAT type.
        # 'table' picks .tsv (TouchDesigner's native tab-separated format);
        # everything else is Python source by default.
        self.dat_type_to_extension = {
            'text': 'py',
            'table': 'tsv',
            'execute': 'py',
            'parexec': 'py',
            'pargroupexec': 'py',
            'chopexec': 'py',
            'datexec': 'py',
            'opexec': 'py',
            'panelexec': 'py',
        }

        # Parameter tracker for detecting COMP changes
        self.param_tracker = ParameterTracker(self.my)

        # Network fingerprints for TDN COMPs -- used instead of oper.dirty
        # (which is always True when externaltox is empty)
        self._tdn_fingerprints = {}

        # NOTE: _setupEnvironment() is NOT called here.
        # It runs inside EnvoyExt.Start(), which is invoked after init() and
        # _restoreSettings() have run. Calling it here (based on the baked
        # Envoyenable value) would bypass the opt-in prompt on fresh .tox drop.

    # ==========================================================================
    # PYTHON ENVIRONMENT SETUP (uv)
    # ==========================================================================

    def _setupEnvironment(self):
        """
        Set up a Python virtual environment using uv for Envoy dependencies.
        Installs uv if not found, creates .venv, installs packages.
        Adds the venv's site-packages to sys.path so TD can import from it.

        Returns True if the environment is ready (mcp.server importable),
        False if any step failed. Callers (e.g. EnvoyExt.Start) MUST gate on
        this -- continuing past a False return produces an inscrutable
        'No module named mcp.server' traceback at server-start time.
        """
        project_dir = project.folder
        venv_dir = os.path.join(project_dir, '.venv')

        # Platform-aware paths
        # Use sys.executable to get the current Python interpreter (cross-platform)
        python_exe = sys.executable
        if sys.platform.startswith('win'):
            site_packages = os.path.join(venv_dir, 'Lib', 'site-packages')
            venv_python = os.path.join(venv_dir, 'Scripts', 'python.exe')
        else:
            py_ver = f'python{sys.version_info.major}.{sys.version_info.minor}'
            site_packages = os.path.join(venv_dir, 'lib', py_ver, 'site-packages')
            venv_python = os.path.join(venv_dir, 'bin', 'python')

        # Dependencies - pywin32 is Windows-only
        # Bump MCP_MIN_VERSION when a new release is tested and verified
        MCP_MIN_VERSION = '1.26.0'
        deps = [f'mcp>={MCP_MIN_VERSION}', 'attrs<25']
        if sys.platform.startswith('win'):
            deps.append('pywin32>=306')

        # Fast path: if deps already installed and version sufficient, just add to sys.path
        if os.path.isdir(os.path.join(site_packages, 'mcp')):
            self._addSitePackages(site_packages)
            if sys.platform.startswith('win'):
                self._fixPywin32Dlls(site_packages)
            # Check installed version meets minimum
            try:
                from importlib.metadata import version as pkg_version
                installed = pkg_version('mcp')
                if tuple(int(x) for x in installed.split('.')) >= tuple(int(x) for x in MCP_MIN_VERSION.split('.')):
                    # Check for attrs 25.x which conflicts with TD's bundled attr module
                    try:
                        installed_attrs = pkg_version('attrs')
                        if tuple(int(x) for x in installed_attrs.split('.')) >= (25,):
                            self.Log(f'attrs {installed_attrs} may conflict with TD -- downgrading...')
                        else:
                            self._checkMCPUpdate(installed)
                            return self._verifyMcpImportable(site_packages)
                    except Exception:
                        self._checkMCPUpdate(installed)
                        return self._verifyMcpImportable(site_packages)
                self.Log(f'MCP {installed} installed, upgrading to >={MCP_MIN_VERSION}...')
            except Exception as e:
                # mcp dir exists but version metadata unreadable -- accept and verify import
                self.Log(f'Could not read mcp version ({e}); proceeding with import check', 'WARNING')
                return self._verifyMcpImportable(site_packages)

        try:
            uv = self._findOrInstallUv(python_exe)
            if not uv:
                self.Log(
                    'uv not found and could not be installed -- Envoy cannot bootstrap. '
                    'Install uv manually (https://docs.astral.sh/uv/) and ensure it is on PATH '
                    'visible to TouchDesigner (macOS GUI apps do not inherit shell PATH).',
                    'ERROR',
                )
                return False

            # Create venv if it doesn't exist.
            # stdin=DEVNULL: subprocess.run from inside TD on Windows raises
            # [WinError 50] without it -- subprocess.py's stdin=None path
            # calls DuplicateHandle on TD's stdin handle, which is not
            # duplicatable for a GUI process. DEVNULL routes through NUL.
            if not os.path.isdir(venv_dir):
                self.Log('Creating virtual environment...')
                subprocess.run(
                    [uv, 'venv', venv_dir, '--python', python_exe],
                    check=True, capture_output=True, text=True,
                    stdin=subprocess.DEVNULL,
                )

            # Install dependencies
            self.Log('Installing dependencies...')
            subprocess.run(
                [uv, 'pip', 'install'] + deps + ['--python', venv_python],
                check=True, capture_output=True, text=True,
                stdin=subprocess.DEVNULL,
            )

            self._addSitePackages(site_packages)
            if sys.platform.startswith('win'):
                self._fixPywin32Dlls(site_packages)
            self.Log('Python environment ready', 'SUCCESS')
            return self._verifyMcpImportable(site_packages)

        except subprocess.CalledProcessError as e:
            self.Log(f'Environment setup failed: {e.stderr or e}', 'ERROR')
            return False
        except Exception as e:
            self.Log(f'Environment setup failed: {e}', 'ERROR')
            return False

    def _warmEnvoyEnvironment(self):
        """Pre-load mcp.server so toggling Envoy on is snappy.

        `import mcp.server` pulls in uvicorn / starlette / anyio / pydantic_core
        and costs 2-5 seconds the first time.  Without warming, that cost lands
        on the main thread inside Start() the moment the user clicks Envoyenable
        on -- showing up as a ~10 s TD freeze.  Calling _setupEnvironment here
        during boot pre-populates sys.path and seats mcp.server in sys.modules,
        so the later _verifyMcpImportable in Start() short-circuits.

        Runs on the main thread on purpose: _setupEnvironment uses self.Log
        (FIFO DAT writes), and importing mcp twice from different threads can
        trigger a pydantic_core panic.  A few seconds added to boot is much
        less jarring than a freeze mid-click.  Idempotent.

        No-op when no venv exists yet (Envoy never enabled in this project).
        """
        if getattr(self, '_envoy_env_warmed', False):
            return
        if 'mcp.server' in sys.modules:
            self._envoy_env_warmed = True
            return
        # Skip if the venv doesn't exist yet -- user hasn't bootstrapped Envoy
        # in this project, and we don't want boot to trigger an unsolicited
        # `uv pip install`.  The first Envoy enable will handle setup.
        venv_dir = os.path.join(project.folder, '.venv')
        if sys.platform.startswith('win'):
            mcp_dir = os.path.join(venv_dir, 'Lib', 'site-packages', 'mcp')
        else:
            py_ver = f'python{sys.version_info.major}.{sys.version_info.minor}'
            mcp_dir = os.path.join(venv_dir, 'lib', py_ver, 'site-packages', 'mcp')
        if not os.path.isdir(mcp_dir):
            self._envoy_env_warmed = True
            return
        self._envoy_env_warmed = True
        try:
            self._setupEnvironment()
        except Exception as e:
            self.Log(f'Envoy pre-warm failed: {e}', 'WARNING')

    def _verifyMcpImportable(self, site_packages):
        """Final gate: confirm mcp.server actually imports inside TD's process.

        A populated site-packages is necessary but not sufficient -- a partial
        install or load-time failure (missing native dep, etc.) would still
        leave the server unable to start. Catching it here yields a useful
        textport message instead of an inscrutable traceback at run time.

        Fast path: if mcp.server is already in sys.modules, a previous Start()
        in this session already imported it successfully -- return True without
        touching sys.modules.  Tearing down and re-importing mcp.* on top of an
        already-loaded pydantic_core (Rust C extension) can panic the
        validator and abort() the process with no Python traceback -- the
        "TD just closes on Envoy toggle off/on" crash users hit on 5.0.393+.
        """
        if 'mcp.server' in sys.modules:
            return True
        try:
            import importlib
            # First import attempt of this session, or recovery from a prior
            # failed import: clear any half-loaded mcp.* entries so the loader
            # genuinely re-runs (a failed import leaves the parent package
            # behind but not the submodule).
            for mod in list(sys.modules):
                if mod == 'mcp' or mod.startswith('mcp.'):
                    del sys.modules[mod]
            importlib.import_module('mcp.server')
            return True
        except Exception as e:
            self.Log(
                f'Dependencies installed but mcp.server failed to import: {e}. '
                f'Inspect {site_packages} for partial installs and try deleting '
                f'.venv/ to force a clean rebuild.',
                'ERROR',
            )
            return False

    def _findOrInstallUv(self, python_exe):
        """Find uv on PATH, or install it via pip --user. Returns path to uv executable or None."""
        # Check PATH first
        uv = shutil.which('uv')
        if uv:
            return uv

        # Install uv via pip --user (avoids needing admin for Program Files)
        self.Log('uv not found - installing via pip...')
        try:
            subprocess.run(
                [python_exe, '-m', 'pip', 'install', '--user', 'uv'],
                check=True, capture_output=True, text=True,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as e:
            self.Log(f'Failed to install uv: {e.stderr or e}', 'ERROR')
            return None

        # Find the installed uv binary in user Scripts directories
        uv = shutil.which('uv')
        if uv:
            return uv

        # Search common --user install locations (platform-specific)
        if sys.platform.startswith('win'):
            appdata = os.environ.get('APPDATA', '')
            if appdata:
                candidates = glob(os.path.join(appdata, 'Python', 'Python*', 'Scripts', 'uv.exe'))
                for candidate in candidates:
                    if os.path.isfile(candidate):
                        return candidate
        else:
            # macOS: check common user-local bin directories
            home = os.path.expanduser('~')
            mac_candidates = (
                glob(os.path.join(home, 'Library', 'Python', '3.*', 'bin', 'uv'))
                + [os.path.join(home, '.local', 'bin', 'uv')]
            )
            for candidate in mac_candidates:
                if os.path.isfile(candidate):
                    return candidate

        self.Log('Could not find uv after install - is Python user Scripts on PATH?', 'ERROR')
        return None

    def _addSitePackages(self, site_packages):
        """Add venv site-packages (and pywin32 subdirs on Windows) to sys.path."""
        paths = [site_packages]
        if sys.platform.startswith('win'):
            paths.append(os.path.join(site_packages, 'win32'))
            paths.append(os.path.join(site_packages, 'win32', 'lib'))
        for p in paths:
            if os.path.isdir(p) and p not in sys.path:
                sys.path.insert(0, p)

    def _fixPywin32Dlls(self, site_packages):
        """Copy pywin32 DLLs to win32/ so they're importable without post-install."""
        src_dir = os.path.join(site_packages, 'pywin32_system32')
        dst_dir = os.path.join(site_packages, 'win32')
        if not os.path.isdir(src_dir) or not os.path.isdir(dst_dir):
            return
        for dll in os.listdir(src_dir):
            if dll.endswith('.dll'):
                src = os.path.join(src_dir, dll)
                dst = os.path.join(dst_dir, dll)
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)

    def _checkMCPUpdate(self, installed: str):
        """Check PyPI for a newer MCP version in a background thread. Logs a
        notice if an update is available - never blocks the main thread."""
        import threading

        owner_path = self.my.path

        def _check():
            try:
                import urllib.request
                import json
                req = urllib.request.Request(
                    'https://pypi.org/pypi/mcp/json',
                    headers={'Accept': 'application/json'}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                latest = data['info']['version']
                if tuple(int(x) for x in latest.split('.')) > tuple(int(x) for x in installed.split('.')):
                    msg = (
                        f'MCP update available: {installed} -> {latest}. '
                        f'Update MCP_MIN_VERSION in EmbodyExt._setupEnvironment() '
                        f'and delete dev/.venv to upgrade.'
                    )
                    # self.Log() touches TD objects (FIFO DAT, parameters,
                    # absTime.frame). Marshal to the main thread via run().
                    # Guarded so a rename/move between spawn and fire becomes
                    # a silent no-op rather than a None.Log() script error.
                    run("o = op(args[0])\nif o: o.Log(args[1], 'WARNING')",
                        owner_path, msg, delayFrames=1)
            except Exception:
                pass  # Network unavailable, not critical

        threading.Thread(target=_check, daemon=True).start()

    # ==========================================================================
    # PROPERTIES
    # ==========================================================================

    @property
    def Externalizations(self) -> Optional[DAT]:
        """Returns the externalizations table DAT.

        The table lives inside the Embody COMP as a child named
        'externalizations' -- it's internal state, not a user-configurable
        reference. The old par.Externalizations DAT-ref parameter was
        removed (cleanup pass 2026-05-19).
        """
        return self.my.op('externalizations')

    def _folderFor(self, oper: 'OP') -> str:
        """Return the default save folder for an operator based on family.

        COMPs use Defaulttoxfolder; DATs use Defaultscriptfolder. Both
        default to '' (project root) if the param is unset. The old
        global Folder param was removed (cleanup pass 2026-05-19) because
        changing it triggered a destructive Disable(prev) cycle that
        deleted every tracked external file.
        """
        if oper.family == 'COMP':
            par = getattr(self.my.par, 'Defaulttoxfolder', None)
        else:
            par = getattr(self.my.par, 'Defaultscriptfolder', None)
        return par.eval() if par is not None else ''

    @property
    def ExternalizationsFolder(self) -> str:
        """Back-compat shim: returns Defaulttoxfolder.

        Most callers want the COMP folder. DAT callers that need the
        script folder should call _folderFor(oper) directly.
        """
        par = getattr(self.my.par, 'Defaulttoxfolder', None)
        return par.eval() if par is not None else ''

    @property
    def TDNBackupDir(self) -> Path:
        """Returns the .tdn_backup directory path (under the project root)."""
        return Path(project.folder) / '.tdn_backup'

    # ==========================================================================
    # PATH UTILITIES - Cross-Platform Support
    # ==========================================================================

    def normalizePath(self, path_str: Union[str, Path, None]) -> str:
        """
        Normalize path separators to forward slashes for cross-platform compatibility.
        Forward slashes work on both Windows and macOS.
        """
        return str(path_str).replace('\\', '/') if path_str else path_str

    def _safeSyncFile(self, op_path, value):
        """Set syncfile on an operator if it still exists."""
        o = op(op_path)
        if o:
            o.par.syncfile = value

    def _safeAllowCooking(self, op_path, value):
        """Set allowCooking on an operator if it still exists."""
        o = op(op_path)
        if o:
            o.allowCooking = value

    def getExternalPath(self, oper: OP) -> str:
        """Get the normalized external file path from an operator."""
        if oper.family == 'COMP':
            return self.normalizePath(oper.par.externaltox.eval())
        elif oper.family == 'DAT':
            return self.normalizePath(oper.par.file.eval())
        return ''

    def setExternalPath(self, oper: OP, path_str: str, readonly: bool = False) -> None:
        """Set the external file path on an operator (normalized).

        Default is editable -- users can manually retarget par.externaltox /
        par.file without Embody fighting them. Callers can pass readonly=True
        for special cases (e.g. palette-derived COMPs that shouldn't be
        retargeted).
        """
        normalized = self.normalizePath(path_str)
        if oper.family == 'COMP':
            oper.par.externaltox.readOnly = False
            oper.par.externaltox = normalized
            oper.par.externaltox.readOnly = readonly
        elif oper.family == 'DAT':
            oper.par.file.readOnly = False
            oper.par.file = normalized
            oper.par.file.readOnly = readonly

    def buildAbsolutePath(self, rel_path: Union[str, Path]) -> Path:
        """Build absolute path from relative path, handling cross-platform issues."""
        return Path(project.folder) / self.normalizePath(rel_path)

    def getOpPaths(self, opToExternalize: OP, externalizationsFolder: Optional[str] = None) -> tuple[Optional[Path], Optional[Path], Optional[str], Optional[str]]:
        """
        Generate file paths for an operator's externalization.

        Returns:
            tuple: (abs_folder_path, save_file_path, rel_directory, rel_file_path)
                   or (None, None, None, None) on error
        """
        if externalizationsFolder is None or externalizationsFolder is False:
            # Family-specific default: COMPs -> Defaulttoxfolder,
            # DATs -> Defaultscriptfolder. Falls back to '' (project root).
            externalizationsFolder = self._folderFor(opToExternalize)

        # Normalize folder path
        if externalizationsFolder:
            externalizationsFolder = self.normalizePath(externalizationsFolder)

        # If operator already has an external path, use it
        existing_path = self.getExternalPath(opToExternalize)
        if existing_path:
            rel_file_path = existing_path
            abs_folder_path = self.buildAbsolutePath(rel_file_path).parent
            save_file_path = self.buildAbsolutePath(rel_file_path)
            rel_directory = self.normalizePath(str(Path(rel_file_path).parent))
            return abs_folder_path, save_file_path, rel_directory, rel_file_path

        # Determine file extension
        if opToExternalize.family == 'COMP':
            file_extension = '.tox'
        elif opToExternalize.family == 'DAT':
            existing = opToExternalize.par.file.eval()
            file_extension = os.path.splitext(existing)[1] if existing else None
        else:
            file_extension = None

        if file_extension is None:
            self.Log("File extension not found", "ERROR")
            return None, None, None, None

        # Build paths -- flat layout: {folder}/{op.name}.{ext}
        # No mirroring of the network hierarchy. The user's chosen folder
        # is the file's home; if two ops share a name the user picks unique
        # names or accepts an overwrite warning at save time.
        filename = opToExternalize.name + file_extension

        if externalizationsFolder:
            rel_directory = externalizationsFolder
            rel_file_path = f'{externalizationsFolder}/{filename}'
        else:
            rel_directory = ''
            rel_file_path = filename
        
        abs_folder_path = Path(project.folder) / rel_directory if rel_directory else Path(project.folder)
        save_file_path = Path(project.folder) / rel_file_path
        
        if self.debug_mode:
            self.Log(f"getOpPaths for {opToExternalize.path}:", "INFO")
            self.Log(f"  rel_directory: {rel_directory}", "INFO")
            self.Log(f"  rel_file_path: {rel_file_path}", "INFO")
            self.Log(f"  abs_folder_path: {abs_folder_path}", "INFO")
            self.Log(f"  save_file_path: {save_file_path}", "INFO")
        
        return abs_folder_path, save_file_path, rel_directory, rel_file_path

    # ==========================================================================
    # ENVOY ONBOARDING
    # ==========================================================================

    def _messageBox(self, title, message, buttons):
        """ui.messageBox with auto-response support for headless testing.

        Seed responses via:
            op.Embody.store('_smoke_test_responses', {'Dialog Title': button_index})

        A list value answers multiple invocations of the same title in
        order (one button_index per invocation):
            op.Embody.store('_smoke_test_responses', {'Dialog Title': [1, 2]})

        Single-int values are consumed on first use; list values are
        consumed front-to-back until empty. The key is removed once
        its responses are exhausted; the store is cleared when no
        keys remain.
        """
        responses = self.my.fetch('_smoke_test_responses', None, search=False)
        if responses is not None and title in responses:
            value = responses[title]
            if isinstance(value, list):
                choice = value.pop(0) if value else None
                if choice is None:
                    return ui.messageBox(title, message, buttons=buttons)
                if not value:
                    responses.pop(title)
            else:
                choice = responses.pop(title)
            self.Log(f'[test] Auto-responded to "{title}" -> button {choice}')
            if not responses:
                self.my.unstore('_smoke_test_responses')
            return choice
        return ui.messageBox(title, message, buttons=buttons)

    def _promptEnvoy(self):
        """Prompt user to enable Envoy (AI coding assistant integration)."""
        choice = self._messageBox('Embody - AI Coding Assistant Integration',
            'Enable Envoy?\n\n'
            'Envoy is an MCP server that lets AI coding assistants\n'
            'create, modify, and query TouchDesigner operators.\n\n'
            'This will:\n'
            '  - Install Python dependencies (~30 MB)\n'
            '  - Start a local MCP server on port '
            f'{self.my.par.Envoyport.eval()}\n'
            '  - Generate AI config files in your project root\n'
            '    (CLAUDE.md, AGENTS.md, .mcp.json, .claude/ rules + skills)\n\n'
            'All Envoy MCP tools are auto-authorized for convenience.\n'
            'To adjust permissions, edit .claude/settings.local.json\n'
            'in your project root after setup.\n\n'
            'Works with Claude Code, Cursor, Windsurf, and other MCP clients.\n'
            'You can change this later via the Envoyenable parameter.\n\n'
            'Note: TD will be unresponsive for a few seconds while\n'
            'dependencies are installed.',
            buttons=['Skip', 'Enable Envoy'])

        if choice == 1:
            self._enableEnvoy()
        else:
            self.my.par.Envoyenable = False
            self.Log('Envoy skipped. Enable later via Envoyenable parameter.', 'INFO')

    def _enableEnvoy(self):
        """Enable Envoy: git check, install deps, extract AI config, start server."""
        self.Log('Setting up Envoy...', 'INFO')

        # Git check runs FIRST -- immediately after the user clicks "Enable Envoy",
        # before the slow deps install. This keeps all dialogs at the start of the
        # setup flow so nothing surprising appears after TD goes unresponsive.
        git_root = self.my.ext.Envoy._checkOrInitGitRepo()
        if git_root is None:
            # User cancelled -- abort Envoy setup entirely.
            self.Log('Envoy setup cancelled.', 'INFO')
            return
        # Store so Start() skips re-prompting for git.
        self.my.store('_git_root', str(git_root))

        # Install Python dependencies
        self._setupEnvironment()

        # Extract AI coding assistant config files to project/repo root
        self._extractAIConfig()

        # Enable Envoy (triggers Start() via parexec.py)
        self.my.par.Envoyenable = True
        self.my.par.Envoystatus = 'Starting...'

        client_label = self.my.par.Aiclient.label
        self.Log(
            f'Envoy enabled! Config generated for {client_label}. '
            f'Connect your AI coding assistant via MCP.',
            'SUCCESS'
        )

    def _findProjectRoot(self):
        """Find the git root, or fall back to project.folder.

        Checks the stored git root first (set by Start/InitGit), then
        walks up from project.folder but stops before the home directory
        to avoid picking up unrelated repos (e.g. dotfiles in ~).
        """
        git_root = self.my.fetch('_git_root', None, search=False)
        if git_root and git_root != 'no-git':
            return Path(git_root) if not isinstance(git_root, Path) else git_root
        project_dir = Path(project.folder)
        home_dir = Path.home()
        for parent_dir in [project_dir] + list(project_dir.parents):
            if parent_dir == home_dir or len(parent_dir.parts) <= len(home_dir.parts):
                break
            if (parent_dir / '.git').exists():
                return parent_dir
        return project_dir

    def _extractAIConfig(self):
        """Extract AI coding assistant config files based on par.Aiclient."""
        target_dir = self._findProjectRoot()
        client = self.my.par.Aiclient.eval()

        # Always: AGENTS.md (universal standard, read by all major AI tools)
        self._writeAgentsMd(target_dir)

        if client == 'claudecode':
            self._writeClaudeCodeConfig(target_dir)
        elif client == 'cursor':
            self._writeCursorRules(target_dir)
        elif client == 'copilot':
            self._writeCopilotInstructions(target_dir)
        elif client == 'windsurf':
            self._writeWindsurfRules(target_dir)
        # 'none': AGENTS.md only (already written above)

    def _writeAgentsMd(self, target_dir):
        """Write AGENTS.md -- universal AI instructions read by all major AI tools."""
        templates_comp = self.my.op('templates')
        agents_md_dat = templates_comp.op('text_agents_md') if templates_comp else None

        if agents_md_dat and agents_md_dat.text:
            content = agents_md_dat.text
        else:
            # Assemble from the 3 rule templates as a fallback
            self.Log('text_agents_md DAT not found -- assembling AGENTS.md from rules', 'DEBUG')
            parts = ['<!-- Generated by Embody/Envoy -- do not edit manually -->\n']
            parts.append('# Embody + Envoy -- AI Instructions\n\n')
            parts.append(
                'This project uses [Embody](https://github.com/dylanroscover/Embody) '
                '(TouchDesigner externalization) and Envoy (MCP server for AI coding tools).\n\n'
                '---\n\n'
            )
            if templates_comp:
                for dat_name in self._TEMPLATE_MAP_RULES:
                    dat = templates_comp.op(dat_name)
                    if dat and dat.text:
                        # Strip frontmatter from each rule before embedding
                        parts.append(self._stripFrontmatter(dat.text).strip())
                        parts.append('\n\n---\n\n')
            content = ''.join(parts)

        self._writeTemplate(target_dir, 'AGENTS.md', content)

    def _writeClaudeCodeConfig(self, target_dir):
        """Write Claude Code config: CLAUDE.md + .claude/rules/ + .claude/skills/"""
        # 1. CLAUDE.md (with ENVOY.md fallback if user already has one)
        self._writeClaudeMd(target_dir)

        # 2. .claude/rules/ and .claude/skills/ from template DATs
        templates_comp = self.my.op('templates')
        if not templates_comp:
            self.Log('Templates COMP not found -- skipping .claude/ generation', 'DEBUG')
            return

        written = 0
        for dat_name, slug in self._TEMPLATE_MAP_RULES.items():
            template_dat = templates_comp.op(dat_name)
            if not template_dat or not template_dat.text:
                continue
            # Claude Code doesn't use YAML frontmatter -- strip it.
            # Keep the generated-by marker for overwrite protection.
            content = self._stripFrontmatter(template_dat.text)
            if self._writeTemplate(target_dir, f'.claude/rules/{slug}.md', content):
                written += 1

        for dat_name, slug in self._TEMPLATE_MAP_SKILLS.items():
            template_dat = templates_comp.op(dat_name)
            if not template_dat or not template_dat.text:
                continue
            if self._writeTemplate(target_dir, f'.claude/skills/{slug}/SKILL.md', template_dat.text):
                written += 1

        if written > 0:
            self.Log(f'Generated {written} .claude/ files at {target_dir}', 'SUCCESS')

    def _stripFrontmatter(self, content):
        """Strip leading YAML frontmatter (---...---) from content if present.

        Returns the content after the closing --- block, with leading whitespace
        trimmed. Handles BOM-prefixed content.
        """
        # Strip BOM that TD may add to externalized files
        content = content.lstrip('\ufeff')
        if not content.startswith('---\n'):
            return content
        close_idx = content.find('\n---\n', 4)
        if close_idx == -1:
            return content
        return content[close_idx + 5:].lstrip('\n')

    def _writeCursorRules(self, target_dir):
        """Write Cursor rules: .cursor/rules/{slug}.mdc with YAML frontmatter.

        Templates already embed a 'description:' field. This injects 'globs: []'
        and 'alwaysApply: true' into the existing frontmatter rather than
        prepending a duplicate block.
        """
        templates_comp = self.my.op('templates')
        if not templates_comp:
            self.Log('Templates COMP not found -- skipping .cursor/ generation', 'DEBUG')
            return

        written = 0
        for dat_name, slug in self._TEMPLATE_MAP_RULES.items():
            template_dat = templates_comp.op(dat_name)
            if not template_dat or not template_dat.text:
                continue
            raw = template_dat.text.lstrip('\ufeff')
            # Inject globs/alwaysApply into existing frontmatter
            SEP = '\n---\n'
            if raw.startswith('---\n') and SEP in raw[4:]:
                close_idx = raw.find(SEP, 4)
                fm_lines = raw[4:close_idx]
                rest = raw[close_idx + len(SEP):]
                if 'alwaysApply:' not in fm_lines:
                    fm_lines += '\nglobs: []\nalwaysApply: true'
                content = '---\n' + fm_lines + SEP + rest
            else:
                # No frontmatter -- build one from first H1
                description = slug.replace('-', ' ').title()
                for line in raw.splitlines():
                    if line.startswith('# '):
                        description = line[2:].strip()
                        break
                content = (
                    f'---\ndescription: "{description}"\n'
                    f'globs: []\nalwaysApply: true\n---\n\n{raw}'
                )
            if self._writeTemplate(target_dir, f'.cursor/rules/{slug}.mdc', content):
                written += 1

        if written > 0:
            self.Log(f'Generated {written} .cursor/rules/ files at {target_dir}', 'SUCCESS')

    def _writeCopilotInstructions(self, target_dir):
        """Write GitHub Copilot config: combined instructions + per-rule files."""
        templates_comp = self.my.op('templates')
        if not templates_comp:
            self.Log('Templates COMP not found -- skipping .github/ generation', 'DEBUG')
            return

        written = 0
        rule_parts = ['<!-- Generated by Embody/Envoy -- do not edit manually -->\n\n']
        individual_contents = {}

        for dat_name, slug in self._TEMPLATE_MAP_RULES.items():
            template_dat = templates_comp.op(dat_name)
            if not template_dat or not template_dat.text:
                continue
            # Strip template frontmatter -- Copilot uses its own applyTo format
            rule_content = self._stripFrontmatter(template_dat.text).strip()
            # Extract heading for section label
            heading = slug.replace('-', ' ').title()
            for line in rule_content.splitlines():
                if line.startswith('# '):
                    heading = line[2:].strip()
                    break
            rule_parts.append(f'## {heading}\n\n{rule_content}\n\n---\n\n')
            # Individual file with applyTo frontmatter + generated marker
            individual_contents[slug] = (
                f'---\n'
                f'applyTo: "**"\n'
                f'---\n\n'
                f'<!-- Generated by Embody/Envoy -- do not edit manually -->\n\n'
                f'{rule_content}'
            )

        # Combined file (.github/copilot-instructions.md)
        combined = ''.join(rule_parts)
        if self._writeTemplate(target_dir, '.github/copilot-instructions.md', combined):
            written += 1

        # Individual per-rule files (.github/instructions/{slug}.instructions.md)
        for slug, content in individual_contents.items():
            if self._writeTemplate(target_dir, f'.github/instructions/{slug}.instructions.md', content):
                written += 1

        if written > 0:
            self.Log(f'Generated {written} .github/ files at {target_dir}', 'SUCCESS')

    def _writeWindsurfRules(self, target_dir):
        """Write Windsurf rules: .windsurf/rules/{slug}.md (plain markdown)."""
        templates_comp = self.my.op('templates')
        if not templates_comp:
            self.Log('Templates COMP not found -- skipping .windsurf/ generation', 'DEBUG')
            return

        written = 0
        for dat_name, slug in self._TEMPLATE_MAP_RULES.items():
            template_dat = templates_comp.op(dat_name)
            if not template_dat or not template_dat.text:
                continue
            if self._writeTemplate(target_dir, f'.windsurf/rules/{slug}.md', template_dat.text):
                written += 1

        if written > 0:
            self.Log(f'Generated {written} .windsurf/rules/ files at {target_dir}', 'SUCCESS')

    def _writeClaudeMd(self, target_dir):
        """Write CLAUDE.md from the text_claude template DAT."""
        templates_comp = self.my.op('templates')
        template_dat = templates_comp.op('text_claude') if templates_comp else None
        if not template_dat:
            self.Log('CLAUDE.md template DAT not found inside Embody/templates', 'WARNING')
            return None

        content = template_dat.text
        if not content:
            self.Log('CLAUDE.md template DAT is empty', 'WARNING')
            return None

        claude_md_path = target_dir / 'CLAUDE.md'

        if claude_md_path.exists():
            existing = claude_md_path.read_text(encoding='utf-8')
            if '<!-- Generated by Embody/Envoy' in existing:
                claude_md_path.write_text(content, encoding='utf-8')
                self.Log(f'Updated CLAUDE.md at {claude_md_path}', 'SUCCESS')
            else:
                fallback = target_dir / 'ENVOY.md'
                fallback.write_text(content, encoding='utf-8')
                self.Log(
                    f'CLAUDE.md already exists (not generated by Embody). '
                    f'Wrote MCP reference to {fallback} instead.',
                    'WARNING'
                )
                return fallback
        else:
            claude_md_path.write_text(content, encoding='utf-8')
            self.Log(f'Created CLAUDE.md at {claude_md_path}', 'SUCCESS')

        return claude_md_path

    def _writeTemplate(self, target_dir, rel_path, content):
        """Write a single template file, respecting the Embody/Envoy marker.

        Returns True if the file was written, False if skipped.
        """
        target_path = target_dir / rel_path
        if target_path.exists():
            existing = target_path.read_text(encoding='utf-8')
            if '<!-- Generated by Embody/Envoy' not in existing:
                return False
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding='utf-8')
        return True

    def _upgradeEnvoy(self):
        """Silently extract AI config if Envoy is enabled but files are missing."""
        if not self.my.par.Envoyenable.eval():
            return
        target_dir = self._findProjectRoot()
        client = self.my.par.Aiclient.eval()
        agents_md_missing = not (target_dir / 'AGENTS.md').exists()
        if agents_md_missing or self._clientFilesMissing(target_dir, client):
            self._extractAIConfig()

    def _clientFilesMissing(self, target_dir, client):
        """Return True if the primary config files for the selected client are absent."""
        checks = {
            'claudecode': lambda d: (
                not (d / 'CLAUDE.md').exists() and not (d / 'ENVOY.md').exists()
            ) or not (d / '.claude' / 'rules').exists(),
            'cursor':     lambda d: not (d / '.cursor' / 'rules').exists(),
            'copilot':    lambda d: not (d / '.github' / 'copilot-instructions.md').exists(),
            'windsurf':   lambda d: not (d / '.windsurf' / 'rules').exists(),
            'none':       lambda d: False,
        }
        return checks.get(client, lambda d: False)(target_dir)

    def InitEnvoy(self) -> None:
        """(Re)generate all Envoy and AI client config files.

        Writes MCP config (.mcp.json, .embody/envoy.json, bridge script,
        settings.local.json) and AI client files (CLAUDE.md, AGENTS.md,
        .claude/rules/, .claude/skills/, or equivalent for Cursor/Copilot/
        Windsurf) to the git root or project folder.

        Safe to call at any time -- idempotent. Use this after initializing
        a git repo, changing the AI client setting, or updating Embody to
        refresh generated files.

        Requires Envoy to be enabled (par.Envoyenable = True).
        """
        if not self.my.par.Envoyenable.eval():
            self.Log('Envoy is not enabled. Set Envoyenable = True first.', 'WARNING')
            return

        target_dir = self._findProjectRoot()

        # MCP config (port comes from the running server, or the parameter)
        envoy = self.my.ext.Envoy
        if self.my.fetch('envoy_running', False):
            # Extract port from current status string
            status = str(self.my.par.Envoystatus.eval())
            import re
            match = re.search(r'port\s+(\d+)', status)
            port = int(match.group(1)) if match else self.my.par.Envoyport.eval()
        else:
            port = self.my.par.Envoyport.eval()

        envoy._configureMCPClient(port, target_dir=target_dir)

        # AI client config (CLAUDE.md, AGENTS.md, rules, skills, etc.)
        self._extractAIConfig()

        client_label = self.my.par.Aiclient.label
        self.Log(
            f'Envoy config regenerated for {client_label} at {target_dir}',
            'SUCCESS')

    def InitGit(self) -> None:
        """Initialize or reconnect to a git repository, then generate
        git-related config files (.gitignore, .gitattributes).

        If no git repo exists, prompts the user to initialize one.
        After git is available, also regenerates MCP and AI client config
        so paths point to the git root.

        Safe to call at any time. Use this after creating a git repo
        manually, or to refresh .gitignore/.gitattributes entries.

        Requires Envoy to be enabled (par.Envoyenable = True).
        """
        if not self.my.par.Envoyenable.eval():
            self.Log('Envoy is not enabled. Set Envoyenable = True first.', 'WARNING')
            return

        envoy = self.my.ext.Envoy
        git_root = envoy._checkOrInitGitRepo()

        if git_root is None:
            return  # User cancelled

        if git_root == 'no-git':
            self.Log('No git repo -- .gitignore/.gitattributes skipped.', 'INFO')
            return

        # Store git root so Envoy can find it later (e.g. for deregistration)
        self.my.store('_git_root', git_root)

        # Git-specific config
        envoy._configureGitignore(git_root)
        envoy._configureGitattributes(git_root)
        self.Log(f'Git config generated at {git_root}', 'SUCCESS')

        # Regenerate MCP + AI config so paths point to git root
        self.InitEnvoy()

    # ==========================================================================
    # INITIALIZATION & RESET
    # ==========================================================================

    def Reset(self) -> None:
        """Reset Embody to initial state."""
        parent.Embody.Disable(False)
        run(f"op('{self.my}').UpdateHandler()", delayFrames=10)
        self.createExternalizationsTable()
        self.my.par.externaltox = ''

    def createExternalizationsTable(self) -> None:
        """Create or reset the externalizations tracking table.

        Lives inside the Embody COMP (not as a sibling) so the release .tox
        is self-contained when dropped into another project. With the
        par-driven model the table is purely a derived view -- rebuilt by
        _scanAndPopulate() on every Update -- so there's nothing to preserve
        across upgrades that justifies the legacy sibling-survives pattern.
        """
        table_name = 'externalizations'
        externalizations_dat = self.Externalizations

        # Migration: an older Embody put the table as a sibling. Find and
        # adopt it (move inside) so we don't leave a stale outside.
        if not externalizations_dat:
            for candidate in (self.my.op(table_name),
                              self.my.parent().op(table_name)):
                if candidate and candidate.family == 'DAT':
                    externalizations_dat = candidate
                    if candidate.parent() is not self.my:
                        # Adopt sibling: move inside the Embody COMP.
                        try:
                            new_inside = self.my.copy(candidate)
                            new_inside.name = table_name
                            externalizations_dat = new_inside
                            candidate.destroy()
                            self.Log(
                                f"Moved '{table_name}' tableDAT inside "
                                f"{self.my.path}", 'SUCCESS')
                        except Exception as e:
                            self.Log(
                                f"Could not move sibling '{table_name}' "
                                f"inside: {e}", 'WARNING')
                    break

        if not externalizations_dat:
            # Fresh install -- create inside the Embody COMP.
            externalizations_dat = self.my.create(tableDAT, table_name)
            externalizations_dat.nodeX = -1200
            externalizations_dat.nodeY = 0
            externalizations_dat.color = (0.55, 0.55, 0.55)
            externalizations_dat.clear()
            externalizations_dat.appendRow([
                'path', 'type', 'rel_file_path', 'timestamp',
                'dirty', 'build', 'touch_build'
            ])
            self.Log(f"Created '{table_name}' tableDAT", "SUCCESS")
        else:
            externalizations_dat.clear(keepFirstRow=True)
            self.Log(f"Reset '{table_name}' tableDAT", "INFO")

    def CreateExternalizationsTable(self) -> None:
        """Recovery/init method: create or reconnect the externalizations table.

        Safe to call at any time. No-op if the child tableDAT exists.
        Adopts a stale sibling left over from older Embody versions.
        """
        if self.Externalizations:
            self.Log('Externalizations table already exists', 'INFO')
            return
        # Adopt a stale sibling left by older Embody versions.
        sibling = self.my.parent().op('externalizations')
        if sibling and sibling.family == 'DAT':
            try:
                moved = self.my.copy(sibling)
                moved.name = 'externalizations'
                sibling.destroy()
                self.Log(
                    f'Moved sibling externalizations tableDAT inside '
                    f'{self.my.path}', 'SUCCESS')
                return
            except Exception as e:
                self.Log(
                    f'Could not move sibling externalizations: {e}',
                    'WARNING')
        self.createExternalizationsTable()

    def _migrateTableSchema(self) -> None:
        """Migrate externalizations table schema to current version.

        Drops the legacy 'strategy' column (every COMP now writes both
        .tox and .tdn; the column was a holdover from the old per-op
        strategy split). Adds node_x/node_y/node_color columns if missing.
        Also deletes any legacy strategy='tdn' companion rows.
        """
        table = self.Externalizations
        if not table or table.numRows < 1:
            return

        headers = [table[0, c].val for c in range(table.numCols)]
        migrations = []

        # Drop legacy strategy column + companion TDN rows (was v5.0.176+).
        if 'strategy' in headers:
            strategy_col = headers.index('strategy')
            # Collect legacy TDN companion rows to remove first
            rows_to_delete = [
                i for i in range(1, table.numRows)
                if table[i, 'strategy'].val == 'tdn'
            ]
            for i in reversed(rows_to_delete):
                table.deleteRow(i)
            table.deleteCol(strategy_col)
            if rows_to_delete:
                migrations.append(
                    f'dropped strategy column (removed '
                    f'{len(rows_to_delete)} legacy TDN row(s))')
            else:
                migrations.append('dropped strategy column')
            headers = [table[0, c].val for c in range(table.numCols)]

        # Add position/color columns (v5.0.189+)
        if 'node_x' not in headers:
            table.appendCol('node_x')
            table.appendCol('node_y')
            table.appendCol('node_color')
            table[0, table.numCols - 3] = 'node_x'
            table[0, table.numCols - 2] = 'node_y'
            table[0, table.numCols - 1] = 'node_color'
            migrations.append('node_x/node_y/node_color columns')

        if migrations:
            self.Log(f'Schema migration: {", ".join(migrations)}', 'SUCCESS')

    @staticmethod
    def _resolveOsLabel(os_name: str, os_version: str, win_build) -> str:
        """Pure OS-label resolution, isolated from TD globals for testability.

        TouchDesigner's ``app.osVersion`` reports ``"10"`` on Windows 11 -- both
        Windows 10 and 11 share NT kernel version 10.0, so the only reliable
        discriminator is the build number: 22000+ means Windows 11. ``win_build``
        is ``sys.getwindowsversion().build`` (an int), or ``None`` when that
        probe is unavailable (i.e. not running on Windows). On macOS / genuine
        Windows 10 the label passes through unchanged.
        """
        label = f'{os_name} {os_version}'.strip()
        if 'Windows' in os_name and '11' not in label:
            if win_build is not None and win_build >= 22000:
                label = 'Windows 11'
        return label

    @staticmethod
    def _osLabel() -> str:
        """Human-readable OS label for logs and diagnostics, fixed for Win 11.

        See _resolveOsLabel for why this can't just trust app.osName/osVersion.
        """
        try:
            win_build = sys.getwindowsversion().build
        except (AttributeError, OSError):
            win_build = None  # Not Windows, or the probe isn't available.
        return EmbodyExt._resolveOsLabel(app.osName, app.osVersion, win_build)

    # ==========================================================================
    # SETTINGS PERSISTENCE
    # ==========================================================================

    def _settingsPath(self) -> Path:
        """Path to .embody/config.json -- consistent with _findProjectRoot()."""
        return self._findProjectRoot() / '.embody' / 'config.json'

    def _projectJsonPath(self) -> Path:
        """Path to .embody/project.json -- committed project metadata.

        Unlike .embody/config.json (user-local settings) and .embody/envoy.json
        (live runtime registry), project.json is intended to be checked into git
        so the same metadata travels with the repo to every machine.
        """
        return self._findProjectRoot() / '.embody' / 'project.json'

    def _writeProjectJson(self) -> None:
        """Pin the current TouchDesigner build into .embody/project.json.

        The Envoy bridge reads td_build to pick a matching TD install when
        launching on a fresh clone, where envoy.json is gitignored and its
        td_executable path may not exist locally. Idempotent -- skips the
        write when td_build is already current.
        """
        import json, os
        path = self._projectJsonPath()
        # app.build is the build proper (e.g. '2025.32460'). app.version is
        # the long-lived major branch ('099') and would only be noise here.
        current_build = app.build

        existing = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    existing = loaded
            except (json.JSONDecodeError, OSError):
                pass  # Treat unreadable as empty -- we'll overwrite.

        if existing.get('td_build') == current_build:
            return

        existing['td_build'] = current_build

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(path) + '.tmp')
            content = json.dumps(existing, indent=2) + '\n'
            for attempt in range(3):
                try:
                    tmp.write_text(content, encoding='utf-8')
                    os.replace(str(tmp), str(path))
                    self.Log(
                        f'Pinned td_build={current_build} in '
                        f'.embody/project.json',
                        'DEBUG')
                    return
                except PermissionError:
                    if attempt < 2:
                        import time as _time
                        _time.sleep(0.1)
                    else:
                        raise
        except Exception as e:
            self.Log(f'Failed to write project.json: {e}', 'WARNING')

    def _saveSettings(self) -> None:
        """Persist whitelisted parameter values to .embody/config.json."""
        self._settings_save_pending = False
        params = {}
        # Sort names so JSON output is stable across TD sessions. _PERSISTED_PARAMS
        # is a frozenset, and Python's hash randomization gives each process a
        # different iteration order -- producing noisy diffs on every save.
        for name in sorted(self._PERSISTED_PARAMS):
            par = getattr(self.my.par, name, None)
            if par is None:
                continue
            entry = {'val': par.eval()}
            if par.mode != ParMode.CONSTANT:
                entry['mode'] = str(par.mode)
                if par.expr:
                    entry['expr'] = par.expr
                if par.bindExpr:
                    entry['bindExpr'] = par.bindExpr
            params[name] = entry
        data = {'version': 1, 'params': params}
        try:
            import json, os
            path = self._settingsPath()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(path) + '.tmp')
            content = json.dumps(data, indent=2, sort_keys=True) + '\n'
            for attempt in range(3):
                try:
                    tmp.write_text(content, encoding='utf-8')
                    os.replace(str(tmp), str(path))
                    return
                except PermissionError:
                    if attempt < 2:
                        import time as _time
                        _time.sleep(0.1)
                    else:
                        raise
        except Exception as e:
            self.Log(f'Failed to save settings: {e}', 'WARNING')

    def _deferSaveSettings(self) -> None:
        """Schedule a settings save on the next frame. Coalesces rapid changes."""
        if not getattr(self, '_settings_save_pending', False):
            self._settings_save_pending = True
            run(f"op('{self.my}').ext.Embody._saveSettings()", delayFrames=1)

    def _restoreSettings(self, kick_envoy: bool = False) -> bool:
        """Restore parameter values from .embody/config.json. Returns True if restored.
        Sets _restoring_settings flag to suppress onValueChange side effects.

        Also stores _init_complete when done -- init() no longer stores it because
        TD defers onValueChange callbacks to the next cook, and storing _init_complete
        in init() allowed parexec to process init()'s Envoyenable=False change.

        kick_envoy: if True and Envoyenable is restored to True, defer Start().
        Only set this on the onStart() path -- Verify() owns startup on onCreate()."""
        path = self._settingsPath()
        if not path.is_file():
            # Migrate: check old root-level .embody.json
            old_path = self._findProjectRoot() / '.embody.json'
            if old_path.is_file():
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    import shutil
                    shutil.move(str(old_path), str(path))
                    self.Log('Migrated .embody.json → .embody/config.json', 'INFO')
                except Exception as e:
                    self.Log(f'Could not migrate .embody.json: {e}', 'WARNING')
                    self.my.store('_init_complete', True)
                    return False
            else:
                self.my.store('_init_complete', True)
                return False
        try:
            import json
            data = json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError) as e:
            self.Log(f'Settings file corrupt or unreadable: {e}', 'WARNING')
            self.my.store('_init_complete', True)
            return False
        if not isinstance(data, dict) or 'params' not in data:
            self.my.store('_init_complete', True)
            return False
        params = data['params']
        restored = 0
        self._restoring_settings = True
        try:
            for name, entry in params.items():
                par = getattr(self.my.par, name, None)
                if par is None or name not in self._PERSISTED_PARAMS:
                    continue
                try:
                    mode = entry.get('mode')
                    if mode and 'expr' in entry:
                        par.expr = entry['expr']
                    elif mode and 'bindExpr' in entry:
                        par.bindExpr = entry['bindExpr']
                    else:
                        par.val = entry['val']
                    restored += 1
                except Exception:
                    pass
        finally:
            self._restoring_settings = False
        # Signal parexec that init + restore is complete -- safe to process
        # param changes.  Must be stored AFTER _restoring_settings is cleared
        # so deferred onValueChange callbacks from init() are still suppressed.
        self.my.store('_init_complete', True)
        self.Log(f'Restored {restored} settings from config.json', 'INFO')
        # If Envoyenable was restored to True, kick Start() -- parexec was
        # suppressed during restore so onValueChange never fired.
        # Only set this on the onStart() path (kick_envoy=True).
        # Verify() owns Envoy startup on the onCreate() path.
        if kick_envoy and self.my.par.Envoyenable.eval():
            run(f"op('{self.my}').ext.Envoy.Start()", delayFrames=3)
        return restored > 0

    def Verify(self) -> None:
        """Initialize or reconnect Embody on install or update.

        Called from execute.py onCreate() after CreateExternalizationsTable()
        has already run.  Two scenarios:

        - Fresh install: table exists but is empty (just created) -- skip dialog,
          run UpdateHandler quietly, then offer Envoy opt-in.
        - Update install: table has prior data -- offer a re-scan to validate
          tracked operators after upgrading Embody.
        """
        # Restore saved settings from a previous install before any dialogs.
        settings_restored = self._restoreSettings()

        embodies = op('/').findChildren(name='Embody', parName='Addtagshort')
        other_embody = next((e for e in embodies if e != self.my), None)

        if other_embody:
            self._messageBox('Embody',
                f'An instance of Embody already exists:\n{other_embody}\n'
                'Please remove it first.', buttons=['Ok'])
            return

        table = self.Externalizations
        has_prior_data = table and table.numRows > 1

        if has_prior_data:
            # UPDATE scenario: reconnected to a surviving table with prior entries.
            # Only prompt when this looks like a genuine upgrade -- i.e., no
            # restored settings (so we're not just a returning user re-opening
            # an established project). Without this gate the dialog fires every
            # time the user opens a project that has Embody loaded via
            # par.externaltox, because onCreate re-fires on every .tox restore.
            if not settings_restored:
                choice = self._messageBox('Embody',
                    f'{table.numRows - 1} externalized operator(s) found.\n\n'
                    'Re-scan to validate tracked operators?\n'
                    '(Recommended after upgrading Embody)',
                    buttons=['Skip', 'Re-scan'])
                if choice in (1,):  # Re-scan
                    self.Reset()
        else:
            # FRESH INSTALL: table was just created (empty). No dialog needed --
            # just run UpdateHandler quietly; it will find nothing yet.
            run(f"op('{self.my}').UpdateHandler()", delayFrames=10)

        # Defer Envoy opt-in until after the full init/update cycle completes.
        if settings_restored and has_prior_data:
            # Returning user: settings exist AND table has prior data -- this is
            # a genuine re-install or upgrade into an established project. Skip
            # the prompt; kick Envoy start if the restored settings have it
            # enabled (onValueChange was suppressed during restore).
            if self.my.par.Envoyenable.eval():
                # Longer delay on the upgrade path (onCreate → Verify) to give
                # the old server thread time to release its port.  onDestroyTD
                # signals the old shutdown_event, but uvicorn can take 1-3s to
                # fully close its listener socket.  delayFrames=10 (~0.17s) was
                # too short, causing EADDRINUSE → auto-restart exhaustion →
                # Envoyenable stuck.  60 frames (~1s) is a safer window.
                run(f"op('{self.my}').ext.Envoy.Start()", delayFrames=60)
        else:
            # Fresh install (empty table). Always prompt -- even if a leftover
            # config.json from a previous install in the same folder was
            # restored, the user must explicitly opt in for this new project.
            # Reset Envoyenable so the prompt is the gate, not old settings.
            self.my.par.Envoyenable = False
            self._pending_envoy_prompt = True

    # ==========================================================================
    # SAFE FILE TRACKING
    # ==========================================================================

    def getTrackedFilePaths(self) -> set[Path]:
        """
        Get a set of all file paths that Embody has created/is tracking.
        These are the ONLY files Embody should ever delete.

        Returns:
            set: Absolute Path objects of all tracked files
        """
        tracked = set()
        
        if not self.Externalizations:
            return tracked
            
        for i in range(1, self.Externalizations.numRows):
            rel_file_path = self.Externalizations[i, 'rel_file_path'].val
            if rel_file_path:
                abs_path = self.buildAbsolutePath(self.normalizePath(rel_file_path)).resolve()
                tracked.add(abs_path)
        
        return tracked

    def isTrackedFile(self, file_path: Union[str, Path]) -> bool:
        """
        Check if a file path is tracked by Embody.

        Args:
            file_path: Path object or string to check

        Returns:
            bool: True if this file is in our externalizations table
        """
        if isinstance(file_path, str):
            file_path = Path(file_path)
        
        resolved = file_path.resolve()
        return resolved in self.getTrackedFilePaths()

    def safeDeleteFile(self, file_path: Union[str, Path], force: bool = False) -> bool:
        """
        Safely delete a file, but ONLY if it's tracked by Embody.

        Args:
            file_path: Path object or string of the file to delete
            force: If True, delete even if not tracked (use with extreme caution!)

        Returns:
            bool: True if file was deleted, False otherwise
        """
        if isinstance(file_path, str):
            file_path = Path(file_path)
        
        resolved = file_path.resolve()
        
        if not resolved.is_file():
            return False
        
        if not force and not self.isTrackedFile(resolved):
            self.Log(f"SAFETY: Refusing to delete untracked file: {resolved}", "WARNING")
            return False
        
        try:
            resolved.unlink()
            self.Log(f"Deleted tracked file: {resolved}", "INFO")
            return True
        except Exception as e:
            self.Log(f"Error deleting file: {resolved}", "ERROR", str(e))
            return False

    def safeDeleteTrackedFiles(self, folder_path: Union[str, Path]) -> tuple[int, int]:
        """
        Delete only the files in a folder that Embody is tracking.
        Non-Embody files are left untouched.

        Args:
            folder_path: Path to scan for tracked files

        Returns:
            tuple: (deleted_count, skipped_count)
        """
        if isinstance(folder_path, str):
            folder_path = Path(folder_path)
        
        if not folder_path.exists():
            return (0, 0)
        
        tracked_files = self.getTrackedFilePaths()
        deleted = 0
        skipped = 0
        
        # Walk through folder and delete only tracked files
        for file_path in folder_path.rglob('*'):
            if file_path.is_file():
                resolved = file_path.resolve()
                if resolved in tracked_files:
                    try:
                        resolved.unlink()
                        self.Log(f"Deleted tracked file: {resolved}", "INFO")
                        deleted += 1
                    except Exception as e:
                        self.Log(f"Error deleting: {resolved}", "ERROR", str(e))
                else:
                    skipped += 1
        
        if skipped > 0:
            self.Log(f"SAFETY: Preserved {skipped} untracked file(s) in {folder_path}", "INFO")
        
        return (deleted, skipped)

    # ==========================================================================
    # ENABLE / DISABLE
    # ==========================================================================

    def _cleanupEmptyDirectories(self, folder, prevFolder):
        """
        Helper to clean up empty directories after disable.
        SAFETY: Only removes directories that are completely empty.
        Never uses rmtree or deletes directories with contents.
        """
        if not folder:
            return
            
        # Remove empty top-level comp directories (skip SCM dirs)
        for comp in self.root.findChildren(depth=1, type=COMP):
            if comp.name in self._SCM_DIRS or comp.name in ['local', 'perform']:
                continue
            comp_path = Path(f'{folder}/{comp.name}')
            if comp_path.is_dir():
                try:
                    # rmdir() only succeeds if directory is empty - this is safe
                    comp_path.rmdir()
                except OSError:
                    # Directory not empty - this is expected and safe to ignore
                    pass
                except Exception as e:
                    self.Log(f"Error removing directory: {comp_path}", "ERROR", str(e))

        # Try to remove main externalization folder only if empty
        # SAFETY: Never remove project.folder itself
        try:
            if folder:
                folder_path = Path(folder).resolve()
                project_path = Path(project.folder).resolve()
                if folder_path != project_path and folder_path.is_dir():
                    folder_path.rmdir()  # Only succeeds if empty
        except OSError:
            # Directory not empty - this is expected and safe
            pass
        except Exception as e:
            self.Log(f"Unexpected error removing directory {folder}: {e}", "WARNING")
            pass

        # Handle previous folder - SAFELY remove only if empty
        # NEVER use shutil.rmtree here!
        if prevFolder:
            prev_path = Path(prevFolder)
            project_path = Path(project.folder)
            if prev_path.is_dir() and prev_path != project_path:
                try:
                    # Only remove if empty - safe operation
                    prev_path.rmdir()
                    self.Log(f"Removed empty previous folder: {prev_path}", "INFO")
                except OSError:
                    # Not empty - preserve it!
                    self.Log(f"Previous folder not empty, preserving: {prev_path}", "INFO")
                except Exception as e:
                    self.Log(f"Error with previous folder: {prev_path}", "ERROR", str(e))

    def UpdateHandler(self) -> None:
        """'Sync All' entry point: migrate schema, normalize paths, run Update.

        Called by the toolbar's Update button and the Ctrl+Shift+U shortcut.
        Embody is always-on under the par-driven model -- no Enabled/Disabled
        state to flip.
        """
        self._migrateTableSchema()
        self.normalizeAllPaths()
        run(f"op('{self.my}').Update()", delayFrames=1)

    def normalizeAllPaths(self) -> None:
        """Normalize all paths in table and on operators for cross-platform support."""
        if not self.Externalizations:
            return
            
        paths_fixed = 0
        for i in range(1, self.Externalizations.numRows):
            rel_file_path = self.Externalizations[i, 'rel_file_path'].val
            normalized = self.normalizePath(rel_file_path)
            
            if rel_file_path != normalized:
                self.Externalizations[i, 'rel_file_path'] = normalized
                paths_fixed += 1
                
            # Update operator parameter if needed
            op_path = self.Externalizations[i, 'path'].val
            oper = op(op_path)
            if oper:
                current = self.getExternalPath(oper)
                if current and current != self.normalizePath(current):
                    self.setExternalPath(oper, self.normalizePath(current))
        
        if paths_fixed > 0:
            self.Log(f"Normalized {paths_fixed} path(s) for cross-platform compatibility", "SUCCESS")

    # ==========================================================================
    # MAIN UPDATE LOOP
    # ==========================================================================

    def Update(self, suppress_refresh: bool = False) -> None:
        """Main update method - process additions, subtractions, and dirty ops.

        Args:
            suppress_refresh: If True, skip the delayed Refresh pulse. Used by
                onProjectPreSave() to prevent the continuity check from firing
                during the TDN strip/restore window.
        """
        # Perform Mode is the only kill switch -- Embody is otherwise always
        # active. par.Status was removed in the cleanup pass (2026-05-19).
        if self._performMode:
            return

        # Detect a .toe basename change since the last Update and
        # propagate to the envoy.json registry. This is a defensive
        # backstop for execute.py's onProjectPostSave RefreshRegistry
        # call -- if execute.py wasn't reloaded after a source edit,
        # or the save took an Off/Export path that skipped Envoy
        # restart, this catches the rename on the next Update tick.
        # Idempotent: _writeEnvoyConfig short-circuits when the
        # registry is already current.
        try:
            current_name = project.name
            if getattr(self, '_last_toe_name', None) != current_name:
                self._last_toe_name = current_name
                if self.my.par.Envoyenable.eval():
                    self.my.ext.Envoy.RefreshRegistry()
        except Exception as e:
            self.Log(f'registry rename-detect failed: {e}', 'WARNING')

        # Rename/move continuity is handled natively by TD now -- par.externaltox
        # and par.file follow ops through renames.

        # Detect dirty COMPs (par or structural change since last snapshot).
        # MARK them dirty in the table. NO SAVES -- saving is an explicit
        # action (Save row button or SaveAllDirty). Sync is detection-only.
        # Per-COMP try/except: one operator with a broken expression (e.g.
        # a stale op() reference inside a parameter) must not abort the
        # dirty-detection loop for every COMP after it.
        # Precompute the externalized-COMP boundary once so each
        # _isTDNDirty call doesn't redo the full-tree walk.
        boundary = self._getExternalizedCompPaths()
        for comp in self.getExternalizedOps(COMP):
            try:
                par_dirty = self.param_tracker.compareParameters(comp)
                struct_dirty = self._isTDNDirty(comp, boundary)
                if par_dirty or struct_dirty:
                    self.Externalizations[comp.path, 'dirty'] = (
                        'Par' if par_dirty else 'True')
            except Exception as e:
                self.Log(
                    f'Skipped dirty-check for {comp.path} '
                    f'({type(e).__name__}: {e})', 'WARNING')

        # Get operator lists -- discovery is par-driven now.
        # An op is "to be externalized" iff its native parameter says so:
        # par.externaltox for COMPs, par.file for DATs.
        ops_to_externalize = self.getOpsByPar(COMP) + self.getOpsByPar(DAT)
        externalized_ops = self.getExternalizedOps(COMP) + self.getExternalizedOps(DAT)
        externalized_paths = {ext.path for ext in externalized_ops}
        ops_to_externalize_paths = {o.path for o in ops_to_externalize}

        # Additions: par-declared but not yet tracked.
        # getOpsByPar already enforces isOpProcessable.
        additions = [
            oper for oper in ops_to_externalize
            if oper.path not in externalized_paths
        ]

        # Subtractions: tracked in last scan but par was cleared.
        subtractions = [
            oper for oper in externalized_ops
            if oper.path not in ops_to_externalize_paths
            and not oper.warnings()
            and not oper.scriptErrors()
            and self.isOpProcessable(oper)
        ]

        # Process changes -- handleAddition / handleSubtraction now only
        # adjust par flags + apply node tint. They do NOT write files.
        # Files only get written via explicit Save() / SaveAllDirty().
        additions.sort(key=lambda x: (self.Externalizations.path in x.path, x.path), reverse=True)

        for oper in additions:
            self.handleAddition(oper)
        for oper in subtractions:
            self.handleSubtraction(oper)

        # Refresh the table view from live par state.
        self._scanAndPopulate()

        # Report results (additions/subtractions only -- nothing was saved).
        self._reportResults([], additions, subtractions)
        if not suppress_refresh:
            run(f"op('{self.my}').par.Refresh.pulse()", delayFrames=1)

        # Chain the Envoy opt-in prompt AFTER init completes.
        # Verify() sets this flag; we consume it here so the Envoy dialog
        # appears only after deprecated-pattern and re-scan dialogs resolve.
        if getattr(self, '_pending_envoy_prompt', False):
            self._pending_envoy_prompt = False
            run(f"op('{self.my}').ext.Embody._promptEnvoy()", delayFrames=5)

    def _reportResults(self, dirties, additions, subtractions):
        """Report update results to log."""
        plural = any(len(lst) > 1 for lst in [dirties, additions, subtractions])
        if dirties:
            self.Log(f"Saved {len(dirties)} externalization{'s' if plural else ''}", "SUCCESS")
        if additions:
            self.Log(f"Added {len(additions)} operator{'s' if plural else ''} in total", "SUCCESS")
        if subtractions:
            self.Log(f"Removed {len(subtractions)} operator{'s' if plural else ''} in total", "SUCCESS")

    def Refresh(self) -> None:
        """Refresh Embody state and UI."""
        if self._performMode:
            return
        # Rebuild the table view from live par state. Renames, moves, and
        # duplicate accumulation all evaporate as concerns: the table is
        # derived from current par.externaltox / par.file each refresh.
        self._scanAndPopulate()
        self.updateDirtyStates(self.ExternalizationsFolder)
        self.my.op('list/inject_parents').cook(force=True)
        self.lister.reset()

        self.Debug("Refreshed")
        
        if not me.time.play:
            self.Log("ALERT! TIMELINE IS PAUSED. RESUME FOR EMBODY TO FUNCTION", "ERROR")

    # ==========================================================================
    # OPERATOR QUERIES
    # ==========================================================================

    def getExternalizedOps(self, opFamily: type) -> list[OP]:
        """Get all externalized operators of a given family from the table.

        Args:
            opFamily: COMP or DAT
        """
        if not self.Externalizations:
            return []

        family_str = 'COMP' if opFamily == COMP else 'DAT'
        ops = []

        for i in range(1, self.Externalizations.numRows):
            path = self.Externalizations[i, 'path'].val
            oper = op(path)
            if oper and oper.family == family_str:
                if not oper.path.startswith('/local/') and oper.path != '/local':
                    ops.append(oper)

        return sorted(ops, key=lambda x: -x.path.count('/'))

    def getOpsByPar(self, opFamily: type) -> list[OP]:
        """Get operators that are externalized, by inspection of native TD parameters.

        A COMP is externalized iff par.externaltox != ''.
        A DAT is externalized iff par.file != '' and its type is in supported_dat_types.

        Excludes clones, replicants, /local, and engine/time/annotate types via
        isOpProcessable.
        """
        # Fault-tolerant key callback: a single broken op (e.g. a COMP
        # whose parameter expression raises during evaluation) must not
        # poison the entire scan.  Return False on any exception so the
        # op is silently skipped; the scan continues to the next one.
        def _safe_comp_key(x):
            try:
                return (x.par.externaltox.eval() != ''
                        and self.isOpProcessable(x))
            except Exception:
                return False

        def _safe_dat_key(x):
            try:
                return (x.par.file.eval() != ''
                        and x.type in self.supported_dat_types
                        and self.isOpProcessable(x))
            except Exception:
                return False

        if opFamily == COMP:
            return self.root.findChildren(type=COMP, key=_safe_comp_key)
        else:
            return self.root.findChildren(
                type=DAT, parName='file', key=_safe_dat_key)

    def isOpEligibleToBeExternalized(self, oper: OP) -> bool:
        """Check if an operator can be externalized.

        Tag membership is no longer required -- discovery is par-driven
        (par.externaltox for COMPs, par.file for DATs). This predicate
        now only enforces that the operator type itself is supportable.
        """
        if oper.family == 'COMP':
            return True
        if oper.type not in self.supported_dat_types:
            return False
        return True

    def isOpProcessable(self, oper: OP) -> bool:
        """Check if operator should be processed (not clone/replicant/local)."""
        return (
            not self.isReplicant(oper) and
            not self.isInsideClone(oper) and
            not oper.path.startswith('/local/') and
            oper.path != '/local' and
            oper.type not in ['engine', 'time', 'annotate']
        )

    def isInsideClone(self, oper: OP) -> bool:
        """True if oper or any ancestor COMP is an active clone instance.

        A COMP whose par.clone self-references (a common pattern for
        reusable UI components using iop.* expressions) is treated as
        a master, not a clone.
        """
        p = oper
        while p is not None and p.path != '/':
            if p.family == 'COMP':
                clone_par = getattr(p.par, 'clone', None)
                enable_par = getattr(p.par, 'enablecloning', None)
                if clone_par is not None and enable_par is not None:
                    try:
                        clone_val = clone_par.eval()
                        if (clone_val and clone_val is not p
                                and enable_par.eval()):
                            return True
                    except Exception:
                        pass
            p = p.parent()
        return False

    def isClone(self, oper: OP) -> bool:
        """Check if operator is a clone COMP (not master).

        A COMP whose par.clone self-references is treated as a master.
        """
        if oper.family != 'COMP':
            return False
        clone_par = getattr(oper.par, 'clone', None)
        enable_par = getattr(oper.par, 'enablecloning', None)
        if clone_par is None or enable_par is None:
            return False
        try:
            clone_val = clone_par.eval()
            if clone_val and clone_val is not oper and enable_par.eval():
                return True
        except Exception:
            pass
        return False

    def isReplicant(self, oper: OP) -> bool:
        """Check if operator is inside a replicator."""
        while oper:
            if oper.family == 'COMP' and oper.replicator:
                return True
            oper = oper.parent()
        return False

    # ==========================================================================
    # SAVE & DIRTY HANDLING
    # ==========================================================================

    def Save(self, opPath: str) -> None:
        """Save an externalized COMP to both .tox and .tdn."""
        if self._performMode:
            return
        try:
            oper = op(opPath)
            if not oper or oper.family != 'COMP':
                self.Log(f"Save() requires a COMP, got {oper.family if oper else 'None'}: {opPath}", "ERROR")
                return
            oper.par.enableexternaltox = True

            # Build/Date/Touchbuild auto-injection is disabled (see
            # setupBuildParameters). When re-enabled behind a setting, the
            # bump-on-Save logic moves back here.

            oper.saveExternalTox()

            # Write .tdn sidecar alongside the .tox so diffs reflect the latest state.
            self._writeTdnSidecar(oper)

            # Update timestamp
            if hasattr(oper.par, 'externalTimeStamp') and oper.externalTimeStamp != 0:
                utc_time = datetime.utcfromtimestamp(oper.externalTimeStamp / 10000000 - 11644473600)
                timestamp = utc_time.strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

            self.Externalizations[opPath, 'timestamp'] = timestamp
            self.param_tracker.updateParamStore(oper)
            self.Externalizations[opPath, 'dirty'] = False
            # Refresh position/color metadata
            self._updatePositionInTable(oper, opPath)
            # Snapshot the network structure so _isTDNDirty returns False
            self._storeTDNFingerprint(oper)

            self.Log(f"Saved {opPath}", "SUCCESS")
        except Exception as e:
            self.Log("Save failed", "ERROR", str(e))

    def ExportPortableTox(self, target: 'OP' = None,
                          save_path: Optional[str] = None) -> bool:
        """Export a self-contained .tox with all external file references stripped.

        Temporarily strips file, syncfile, and externaltox parameters from
        all descendants of the target COMP, saves the .tox, then restores
        everything. The resulting .tox has no external file dependencies
        and can be opened on a machine without Embody installed.

        Warns (but does not strip) about non-system absolute paths that won't
        be portable to other machines.

        Args:
            target: The COMP to export. Defaults to the Embody COMP itself.
            save_path: Absolute path for the output .tox. If None, uses the
                       default release path (release/{name}-v{version}.tox).

        Returns:
            True if the .tox was saved successfully, False otherwise.
        """
        if target is None:
            target = self.my
        if save_path is None:
            version = self.my.par.Version.eval()
            save_path = str(
                Path(project.folder).parents[0] / 'release'
                / f"{target.name}-v{version}.tox"
            )

        # Phase 1: Collect file references and externalization params to strip.
        # Include the target itself -- its externaltox/enableexternaltox would
        # be baked into the .tox and confuse recipients.
        saved_state = []

        for op_ref in [target] + target.findChildren():
            if op_ref.family == 'DAT' and hasattr(op_ref.par, 'file'):
                file_val = op_ref.par.file.eval()
                sync_val = op_ref.par.syncfile.eval()
                if not file_val and not sync_val:
                    continue
                if file_val and (file_val.startswith('/') or (len(file_val) > 1 and file_val[1] == ':')):
                    # Absolute path -- warn if not a TD system path
                    if not file_val.startswith('/sys/'):
                        self.Log(
                            f"Absolute path won't be portable: "
                            f"{op_ref.path} -> {file_val}", "WARNING")
                else:
                    saved_state.append({
                        'op': op_ref,
                        'family': 'DAT',
                        'file': file_val,
                        'file_readonly': op_ref.par.file.readOnly,
                        'syncfile': sync_val,
                    })

            elif op_ref.family == 'COMP' and hasattr(op_ref.par, 'externaltox'):
                tox_val = op_ref.par.externaltox.eval()
                enable_val = op_ref.par.enableexternaltox.eval()
                if not tox_val and not enable_val:
                    continue
                if tox_val and (tox_val.startswith('/') or (len(tox_val) > 1 and tox_val[1] == ':')):
                    if not tox_val.startswith('/sys/'):
                        self.Log(
                            f"Absolute path won't be portable: "
                            f"{op_ref.path} -> {tox_val}", "WARNING")
                else:
                    saved_state.append({
                        'op': op_ref,
                        'family': 'COMP',
                        'externaltox': tox_val,
                        'externaltox_readonly': op_ref.par.externaltox.readOnly,
                        'enableexternaltox': enable_val,
                    })

        self.Log(
            f"Exporting portable .tox: stripping {len(saved_state)} "
            f"file reference(s) from {target.path}", "INFO")

        # Phase 2: Strip all collected relative references.
        for entry in saved_state:
            try:
                op_ref = entry['op']
                if entry['family'] == 'DAT':
                    op_ref.par.file.readOnly = False
                    op_ref.par.file = ''
                    op_ref.par.syncfile = False
                elif entry['family'] == 'COMP':
                    op_ref.par.externaltox.readOnly = False
                    op_ref.par.externaltox = ''
                    op_ref.par.enableexternaltox = False
            except Exception as e:
                self.Log(f"Failed to strip {entry['op'].path}: {e}", "WARNING")

        # Phase 3: Save the .tox.
        success = False
        try:
            target.save(str(save_path))
            try:
                rel_path = Path(save_path).relative_to(
                    Path(project.folder).parents[0])
            except ValueError:
                rel_path = save_path
            self.Log(f"Exported portable .tox: {rel_path}", "SUCCESS")
            success = True
        except Exception as e:
            self.Log(f"Portable .tox export failed: {e}", "ERROR")

        # Phase 4: Restore all references (always, even on failure).
        for entry in saved_state:
            try:
                op_ref = entry['op']
                if entry['family'] == 'DAT':
                    op_ref.par.file = entry['file']
                    op_ref.par.file.readOnly = entry['file_readonly']
                    op_ref.par.syncfile = entry['syncfile']
                elif entry['family'] == 'COMP':
                    op_ref.par.externaltox = entry['externaltox']
                    op_ref.par.externaltox.readOnly = entry['externaltox_readonly']
                    op_ref.par.enableexternaltox = entry['enableexternaltox']
            except Exception as e:
                self.Log(
                    f"Failed to restore {entry['op'].path}: {e}", "WARNING")

        return success

    def _deployReleaseTargets(self, source: Path) -> None:
        """Copy a release .tox to every Releasetargets path.

        Releasetargets is a Str parameter holding one absolute path per
        line (newline- or semicolon-separated). Each path is the FULL
        destination filename, so consumers can name the file differently
        per-target.

        Failures per-target are logged but don't abort the rest -- a
        missing destination drive shouldn't block the other deploys.
        """
        par = getattr(self.my.par, 'Releasetargets', None)
        if par is None:
            return 0
        raw = str(par.eval() or '').strip()
        if not raw:
            return 0
        import shutil
        targets = [
            self.normalizePath(line.strip())
            for line in raw.replace(';', '\n').splitlines()
            if line.strip()
        ]
        deployed = 0
        for dest in targets:
            try:
                dest_path = Path(dest)
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(source), str(dest_path))
                self.Log(f'Deployed release to {dest_path}', 'SUCCESS')
                deployed += 1
            except Exception as e:
                self.Log(f'Releasetarget {dest} failed: {e}', 'WARNING')
        return deployed

    def DeployToTargets(self) -> dict:
        """Manual deploy: copy the latest release .tox to every Releasetargets path.

        Called from the Deploytotargets pulse. Picks the most recent
        release/Embody-v*.tox file (whatever par.Version says is
        current) and copies it to all configured consumers.
        """
        release_dir = Path(project.folder).parent / 'release'
        version_par = getattr(self.my.par, 'Version', None)
        candidate = None
        if version_par is not None:
            candidate = release_dir / f'Embody-v{version_par.eval()}.tox'
        if candidate is None or not candidate.is_file():
            # Fall back to newest Embody-v*.tox in release/
            try:
                candidates = sorted(
                    release_dir.glob('Embody-v*.tox'),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                candidate = candidates[0] if candidates else None
            except Exception:
                candidate = None
        if candidate is None or not candidate.is_file():
            self.Log(
                f'DeployToTargets: no release .tox found in {release_dir}',
                'WARNING')
            return {'error': 'no release tox', 'deployed': 0}
        par = getattr(self.my.par, 'Releasetargets', None)
        raw = (par.eval() or '').strip() if par is not None else ''
        if not raw:
            self.Log(
                'DeployToTargets: Releasetargets is empty -- nothing to copy',
                'WARNING')
            return {'error': 'no targets', 'deployed': 0}
        n = self._deployReleaseTargets(candidate)
        return {'deployed': n, 'source': str(candidate)}

    # ==========================================================================
    # RELEASE (Phase 6)
    # ==========================================================================

    def Release(self, target: OP, name: Optional[str] = None,
                 version: Optional[str] = None, save_path: Optional[str] = None
                 ) -> dict[str, Any]:
        """Export a self-contained, unexternalized copy of a COMP.

        Mirrors Externalize.Release.py: copies the target, recursively
        strips every external file reference, resets custom pars on the
        copy to their defaults (skipping the About page so version /
        build metadata survives), and saves to {Name}_{Version}.tox in
        the chosen folder.

        Args:
            target: The COMP to release. Must be a COMP.
            name: Release name. Defaults to target.name.
            version: Release version string. Defaults to target.par.Version
                if present, else "1.0.0".
            save_path: Absolute path for the released .tox. If omitted,
                writes to {project.folder}/{name}_{version}.tox.

        Returns:
            dict with 'success' and 'path' on success or 'error' on failure.
        """
        if self._performMode:
            return {'error': 'Perform Mode active'}
        if target is None or target.family != 'COMP':
            return {'error': 'Release requires a COMP target'}

        name = name or target.name
        if version is None:
            try:
                version = (str(target.par.Version.eval())
                           if hasattr(target.par, 'Version') else '1.0.0')
            except Exception:
                version = '1.0.0'
            version = version or '1.0.0'
        if save_path is None:
            save_path = f'{project.folder}/{name}_{version}.tox'

        # Pre-save the original so its .tox on disk is current. Without
        # this, the copy could reflect stale .tox content if the user has
        # made unsaved par edits since the last Save().
        try:
            ext_tox = target.par.externaltox.eval()
            if ext_tox:
                target.save(ext_tox)
        except Exception as e:
            self.Log(
                f'Release: pre-save of {target.path} failed (continuing): '
                f'{e}', 'WARNING')

        parent_comp = target.parent()
        try:
            copy = parent_comp.copy(target)
        except Exception as e:
            return {'error': f'copy failed: {e}'}

        copy.name = name
        copy.nodeX = 0
        copy.nodeY = 0

        success = False
        try:
            # Reset every custom par to its default, EXCEPT pars on the
            # About page (those carry user-facing release metadata).
            for par in copy.customPars:
                try:
                    if par.page is not None and par.page.name == 'About':
                        continue
                    if par.defaultMode == ParMode.CONSTANT:
                        par.mode = ParMode.CONSTANT
                        par.val = par.default
                    elif par.defaultMode == ParMode.EXPRESSION:
                        par.mode = ParMode.EXPRESSION
                        par.expr = par.defaultExpr
                    elif par.defaultMode == ParMode.BIND:
                        par.mode = ParMode.BIND
                        par.bindExpr = par.defaultBindExpr
                except Exception as e:
                    self.Log(
                        f'Release: failed to reset par {par.name} on '
                        f'copy: {e}', 'DEBUG')

            # Strip every external file reference on the copy. After this,
            # the copy is fully self-contained -- no .tox / .py / .json
            # links to anything on disk.
            self._unexternalizeOperator(copy)

            # Set the released copy's current parameter page to its first
            # custom page so the released .tox opens to a useful view in
            # TD's parameter dialog.
            try:
                if copy.customPages:
                    copy.currentPage = copy.customPages[0]
            except Exception as e:
                self.Log(
                    f'Release: could not set current page on {copy.path}: {e}',
                    'DEBUG')

            try:
                copy.save(save_path)
                success = True
                self.Log(f'Released {target.path} -> {save_path}', 'SUCCESS')
            except Exception as e:
                self.Log(f'Release save failed: {e}', 'ERROR')
                return {'error': f'save failed: {e}'}

        finally:
            try:
                copy.destroy()
            except Exception as e:
                self.Log(
                    f'Release: failed to destroy temp copy: {e}', 'WARNING')

        return {'success': success, 'path': save_path}

    def _unexternalizeOperator(self, op_ref: OP) -> None:
        """Recursively clear external-file refs from an op and its children.

        Ported from Externalize.Release.unexternalizeOperator. For COMPs
        clears par.externaltox + par.enableexternaltox and recurses into
        children. For DATs clears par.file + par.syncfile.
        """
        try:
            if op_ref.isCOMP:
                if hasattr(op_ref.par, 'externaltox'):
                    op_ref.par.externaltox.readOnly = False
                    op_ref.par.externaltox = ''
                if hasattr(op_ref.par, 'enableexternaltox'):
                    op_ref.par.enableexternaltox = False
                for child in op_ref.children:
                    self._unexternalizeOperator(child)
            elif op_ref.isDAT:
                if hasattr(op_ref.par, 'file'):
                    op_ref.par.file.readOnly = False
                    op_ref.par.file = ''
                if hasattr(op_ref.par, 'syncfile'):
                    op_ref.par.syncfile = False
        except Exception as e:
            self.Log(
                f'_unexternalizeOperator on {op_ref.path}: {e}', 'WARNING')

    def _releaseProjectPickFolder(self) -> None:
        """Open the chooseFolder dialog at top of stack, then run release.

        Two-step deferral: ReleaseProject schedules this via run(), then
        this method blocks on chooseFolder and re-enters ReleaseProject
        with the chosen path. Avoids the parexec-inside-modal-dialog
        gotcha that swallowed the dialog instantly.
        """
        chosen = ui.chooseFolder(title='Release Project - destination folder')
        if not chosen:
            self.Log('ReleaseProject: cancelled by user', 'INFO')
            return
        save_path = str(Path(chosen) / project.name)
        # Wrap the work so an unexpected exception above ReleaseProject's
        # own try/except still produces a popup, not a silent textport drop.
        try:
            result = self.ReleaseProject(save_path=save_path)
        except Exception as e:
            result = {'error': f'{type(e).__name__}: {e}'}
        if isinstance(result, dict) and result.get('error'):
            self._messageBox(
                'Embody -- Release Project Failed',
                f'Could not release project:\n\n{result["error"]}\n\n'
                f'Live session is restored.  See textport for details.',
                buttons=['OK'])
        elif isinstance(result, dict) and result.get('success'):
            self._messageBox(
                'Embody -- Release Project Complete',
                f'Released project to:\n{result.get("path", save_path)}\n\n'
                f'Stripped {result.get("stripped_count", "?")} '
                f'externalization(s).\n\n'
                f'Working path is now the release path -- use File > Open '
                f'to return to your original .toe.',
                buttons=['OK'])

    def ReleaseProject(self, save_path: Optional[str] = None) -> dict[str, Any]:
        """Save a self-contained, unexternalized copy of the entire project.

        Snapshots every par-set op's external-file state, strips it all,
        saves the .toe to a user-chosen path (working path becomes the
        release path), then restores the originals in the LIVE SESSION.
        The original on-disk .toe is not touched; only in-memory state
        is shuffled. After the save, the working path stays at the
        release path -- use File > Open or project.load() to switch back.

        Args:
            save_path: Absolute path for the released .toe. If omitted,
                prompts via ui.chooseFile.

        Returns:
            dict with 'success' and 'path', or 'error'.
        """
        if self._performMode:
            return {'error': 'Perform Mode active'}

        if save_path is None:
            # Defer the folder picker to the next frame so it doesn't open
            # inside a parexec onPulse callback chain -- TD's modal dialogs
            # can get dismissed by the cook tick when opened from inside a
            # parexec handler. Calling via run() pops us back up to the
            # top of the call stack.
            self.Log(
                'ReleaseProject: opening folder picker (look for the '
                'native folder dialog, it may appear behind other '
                'windows)', 'INFO')
            run("op.Embody.ext.Embody._releaseProjectPickFolder()",
                delayFrames=1)
            return {'deferred': True}

        # Snapshot every par-set op so we can restore after the save.
        snapshot: list[dict] = []
        try:
            for comp in self.root.findChildren(type=COMP, parName='externaltox'):
                if not comp.par.externaltox.eval():
                    continue
                snapshot.append({
                    'op': comp,
                    'family': 'COMP',
                    'externaltox': comp.par.externaltox.eval(),
                    'externaltox_expr': comp.par.externaltox.expr,
                    'externaltox_readonly': comp.par.externaltox.readOnly,
                    'enableexternaltox': comp.par.enableexternaltox.eval(),
                })
            for dat in self.root.findChildren(type=DAT, parName='file'):
                if not dat.par.file.eval():
                    continue
                snapshot.append({
                    'op': dat,
                    'family': 'DAT',
                    'file': dat.par.file.eval(),
                    'file_readonly': dat.par.file.readOnly,
                    'syncfile': dat.par.syncfile.eval(),
                })
        except Exception as e:
            return {'error': f'snapshot failed: {e}'}

        self.Log(
            f'ReleaseProject: stripping {len(snapshot)} externalizations '
            f'and saving to {save_path}', 'INFO')

        save_success = False
        try:
            for entry in snapshot:
                ref = entry['op']
                try:
                    if entry['family'] == 'COMP':
                        ref.par.externaltox.readOnly = False
                        ref.par.externaltox.expr = ''
                        ref.par.externaltox = ''
                        ref.par.enableexternaltox = False
                    elif entry['family'] == 'DAT':
                        ref.par.file.readOnly = False
                        ref.par.file = ''
                        ref.par.syncfile = False
                except Exception as e:
                    self.Log(
                        f'ReleaseProject strip failed for {ref.path}: {e}',
                        'WARNING')

            try:
                project.save(save_path)
                save_success = True
                self.Log(f'Released project to {save_path}', 'SUCCESS')
            except Exception as e:
                self.Log(f'ReleaseProject save failed: {e}', 'ERROR')
                return {'error': f'save failed: {e}'}

        finally:
            # Restore live session even on failure -- never leave the
            # running project in a stripped state.
            for entry in snapshot:
                ref = entry['op']
                try:
                    if entry['family'] == 'COMP':
                        if entry.get('externaltox_expr'):
                            ref.par.externaltox.expr = entry['externaltox_expr']
                        else:
                            ref.par.externaltox = entry['externaltox']
                        ref.par.externaltox.readOnly = entry['externaltox_readonly']
                        ref.par.enableexternaltox = entry['enableexternaltox']
                    elif entry['family'] == 'DAT':
                        ref.par.file = entry['file']
                        ref.par.file.readOnly = entry['file_readonly']
                        ref.par.syncfile = entry['syncfile']
                except Exception as e:
                    self.Log(
                        f'ReleaseProject restore failed for {ref.path}: {e}',
                        'WARNING')
            self.Log(
                'ReleaseProject: live session externalizations restored. '
                'Working path is now the release path -- File > Open the '
                'original to keep developing.',
                'INFO')

        return {
            'success': save_success,
            'path': save_path,
            'stripped_count': len(snapshot),
        }

    @staticmethod
    def _computeTDNFingerprint(comp, tdn_paths: set = None) -> tuple:
        """Compute a hashable fingerprint of a TDN COMP's network structure.

        Used instead of oper.dirty for TDN COMPs (which always reads True
        because externaltox is empty). Captures all visual and metadata
        properties that a TDN export records: name, type, position, size,
        color, tags, flags, comment, connections, and annotations.

        Recurses into child COMPs that are NOT separately TDN-externalized,
        so changes deep inside nested COMPs (e.g. editing a POP inside a
        geometryCOMP) are detected by the parent's fingerprint.
        """
        parts = []
        for c in sorted(comp.children, key=lambda c: c.name):
            # Skip annotations -- they're fingerprinted separately below
            if c.type == 'annotate':
                continue
            # Per-child try/except: if an operator's properties throw
            # (e.g. a broken parameter expression read while accessing
            # color or tags), record a sentinel and keep walking.  The
            # sentinel changes if the error message changes, so subsequent
            # edits still register as a dirty diff.
            try:
                color = tuple(round(v, 4) for v in c.color)
                tags = tuple(sorted(c.tags))
                flags = (c.bypass, c.lock, c.display, c.render,
                         c.viewer, c.current, c.expose)
                parts.append((
                    c.name, c.type,
                    c.nodeX, c.nodeY, c.nodeWidth, c.nodeHeight,
                    color, tags, flags, c.comment,
                ))
                for i, conn in enumerate(c.inputConnectors):
                    for link in conn.connections:
                        parts.append((c.name, 'in', i, link.owner.name))
                # Recurse into child COMPs that don't have their own TDN file
                if c.isCOMP and (tdn_paths is None or c.path not in tdn_paths):
                    child_fp = EmbodyExt._computeTDNFingerprint(c, tdn_paths)
                    parts.append((c.name, 'children', child_fp))
            except Exception as e:
                parts.append((c.name, 'error', type(e).__name__, str(e)))
        # All annotations (utility=True or False) -- uses annotation-specific attrs
        for ann in sorted(comp.findChildren(type=annotateCOMP, depth=1,
                                            includeUtility=True),
                          key=lambda a: a.name):
            try:
                ann_color = tuple(round(v, 4) for v in (
                    ann.par.Backcolorr.eval(), ann.par.Backcolorg.eval(),
                    ann.par.Backcolorb.eval()))
                parts.append((
                    ann.name, 'annotation',
                    ann.par.Mode.eval(),
                    ann.par.Titletext.eval(),
                    ann.par.Bodytext.eval(),
                    ann.nodeX, ann.nodeY, ann.nodeWidth, ann.nodeHeight,
                    ann_color,
                    round(ann.par.Opacity.eval(), 4),
                ))
            except Exception as e:
                parts.append((ann.name, 'annotation_error',
                              type(e).__name__, str(e)))
        return tuple(parts)

    def _getExternalizedCompPaths(self) -> set:
        """Return the set of all par-externalized COMP paths.

        Used as the fingerprint-boundary set: when computing the structural
        fingerprint of one COMP, recursion stops at any descendant COMP that
        is itself externalized (its own fingerprint covers it).
        """
        return {c.path for c in self.getOpsByPar(COMP)}

    def _isTDNDirty(self, comp, boundary=None) -> bool:
        """Check if a COMP's network has changed since last save.

        boundary -- optional pre-computed set of externalized COMP paths.
        Callers in a loop should compute it once and pass it in;
        _getExternalizedCompPaths walks the whole project tree, and
        recomputing it per COMP was the dominant cost in dirtyHandler
        on large projects (9 COMPs * full-tree walk = ~3 s lag on
        Refresh).
        """
        if boundary is None:
            boundary = self._getExternalizedCompPaths()
        current = self._computeTDNFingerprint(comp, boundary)
        stored = self._tdn_fingerprints.get(comp.path)
        if stored is None:
            self._tdn_fingerprints[comp.path] = current
            return False
        return current != stored

    def _storeTDNFingerprint(self, comp) -> None:
        """Snapshot a COMP's network structure after save."""
        boundary = self._getExternalizedCompPaths()
        self._tdn_fingerprints[comp.path] = self._computeTDNFingerprint(
            comp, boundary)

    def _getAllTrackedTDNFiles(self, exclude_path: Optional[str] = None) -> list[str]:
        """Collect absolute paths of every .tdn sidecar Embody manages.

        Used as the "protected" list for stale-file cleanup during a TDN
        export so a single-COMP save doesn't delete sibling sidecars.

        Args:
            exclude_path: Skip this op_path (the one being exported).
        """
        protected: list[str] = []
        seen: set[str] = set()
        try:
            for comp in self.getOpsByPar(COMP):
                if comp.path == exclude_path:
                    continue
                rel = self._buildTDNRelPath(comp)
                abs_path = str(self.buildAbsolutePath(rel))
                if abs_path not in seen:
                    seen.add(abs_path)
                    protected.append(abs_path)
        except Exception as e:
            self.Log(
                f'_getAllTrackedTDNFiles: par-driven scan failed: {e}',
                'WARNING')
        return protected

    def SaveCurrentComp(self) -> None:
        """Save the COMP we're currently working inside of (Ctrl/Cmd+Alt+U).

        Walks up from the current pane until it finds a COMP with
        par.externaltox set, then saves it.
        """
        if self._performMode:
            return
        current_comp = None
        try:
            pane = ui.panes.current
            if pane and pane.owner:
                current_comp = pane.owner
        except Exception as e:
            self.Log(f"Failed to get current pane: {e}", "DEBUG")

        if not current_comp:
            self.Log("Could not determine current COMP", "WARNING")
            return

        comp = current_comp
        while comp:
            if (comp.family == 'COMP'
                    and hasattr(comp.par, 'externaltox')
                    and comp.par.externaltox.eval()):
                self.Save(comp.path)
                return
            comp = comp.parent()

        self.Log(
            f"No externalized COMP found at or above '{current_comp.path}'",
            "WARNING")

    def dirtyHandler(self, update: bool = False) -> list[str]:
        """Detect dirty externalized COMPs and update the table flag.

        Detection-only. The `update` argument is kept for backward
        compatibility but ignored -- saving is now exclusively done via
        SaveAllDirty() or per-row Save buttons. Returns the list of
        paths flagged dirty.
        """
        dirties = []
        # Compute the externalized-COMP boundary set once and pass it
        # to every _isTDNDirty call.  Without this, each call walks the
        # entire project tree to rebuild the same set -- O(N) COMPs *
        # O(N) tree walks = O(N^2), and we were seeing ~3 s Refresh lag
        # on Lightpath-scale projects.
        boundary = self._getExternalizedCompPaths()
        for oper in self.getExternalizedOps(COMP):
            # Per-COMP try/except: a broken expression on one operator
            # must not abort dirty-detection for the rest of the project.
            try:
                par_dirty = self.param_tracker.compareParameters(oper)
                struct_dirty = self._isTDNDirty(oper, boundary)
                dirty = par_dirty or struct_dirty
                if dirty:
                    self.Externalizations[oper.path, 'dirty'] = (
                        'Par' if par_dirty else 'True')
                    dirties.append(oper.path)
                else:
                    # Preserve a 'Par' marker until Save clears it
                    if str(self.Externalizations[oper.path, 'dirty'].val) != 'Par':
                        self.Externalizations[oper.path, 'dirty'] = False
            except Exception as e:
                self.Log(
                    f'Skipped dirty-check for {oper.path} '
                    f'({type(e).__name__}: {e})', 'DEBUG')
        return dirties

    def updateDirtyStates(self, externalizationsFolder: str) -> None:
        """Update dirty states and check for path drift.

        dirtyHandler() already iterates every externalized COMP and
        runs the full compareParameters + _isTDNDirty pass -- and
        flags ParChange rows on its own.  This second pass is purely
        about catching rel_file_path drift (the path on the operator
        no longer matches the value the table is showing).  Running
        compareParameters again here doubled Refresh() time on large
        projects for no benefit.
        """
        dirties = self.dirtyHandler(False)
        # Read ParChange flags dirtyHandler already wrote so the log
        # summary below stays accurate without recomputing them.
        param_changes = [
            self.Externalizations[i, 'path'].val
            for i in range(1, self.Externalizations.numRows)
            if self.Externalizations[i, 'dirty'].val == 'Par'
        ]
        for oper in self.getExternalizedOps(COMP) + self.getExternalizedOps(DAT):
            try:
                current_path = self.getExternalPath(oper)
                table_path = self.normalizePath(
                    self.Externalizations[oper.path, 'rel_file_path'].val)
                if current_path != table_path:
                    self.Externalizations[oper.path, 'rel_file_path'] = current_path
                    self.Log(f"Updated path for {oper.path}", "SUCCESS")
            except Exception as e:
                self.Log(
                    f'Skipped path-update for {oper.path} '
                    f'({type(e).__name__}: {e})', 'WARNING')

        if dirties or param_changes:
            msgs = []
            if dirties:
                msgs.append(f"{len(dirties)} unsaved tox{'es' if len(dirties) > 1 else ''}")
            if param_changes:
                msgs.append(f"{len(param_changes)} COMP{'s' if len(param_changes) > 1 else ''} with param changes")
            self.Log(f"Found {' and '.join(msgs)}", "INFO")

    # ==========================================================================
    # ADDITION / SUBTRACTION HANDLING
    # ==========================================================================

    def handleAddition(self, oper: OP) -> None:
        """Process a newly par-set operator for externalization.

        Side-effect-light: only adjusts the operator's externalization
        parameters and applies the family tint. Does NOT create files,
        directories, or sidecars. The user explicitly saves (per-row
        Save button or SaveAllDirty) to actually write to disk.
        """
        # getOpPaths picks the right default folder based on op.family
        # (Defaulttoxfolder for COMPs, Defaultscriptfolder for DATs).
        abs_folder_path, save_file_path, rel_directory, rel_file_path = \
            self.getOpPaths(oper)

        if save_file_path is None:
            self.Log(f"Could not generate paths for {oper.path}", "ERROR")
            return

        if oper.family == 'COMP':
            self._setupCompForExternalization(oper, rel_file_path, save_file_path)
            self.param_tracker.updateParamStore(oper)
        else:  # DAT
            self._setupDatForExternalization(oper, rel_file_path, save_file_path)

        # Tint the node by family so externalizations are visually obvious.
        # Cyan for COMPs (TOX), magenta for DATs. Controlled by Colorexternalized.
        self._applyExternalizedColor(oper)

        self.Log(f"Added '{oper.path}'", "SUCCESS")

    def _applyExternalizedColor(self, oper: 'OP') -> None:
        """Tint a node by family (cyan for COMPs, magenta for DATs).

        Reads the Compcolor / Datcolor RGB groups on the Embody
        COMP. No-op when the Colorexternalized toggle is off, or when
        the colors haven't been configured yet (graceful fallback for
        older releases that didn't ship these params).
        """
        toggle = getattr(self.my.par, 'Colorexternalized', None)
        if toggle is not None and not toggle.eval():
            return
        if oper.family == 'COMP':
            prefix = 'Compcolor'
        elif oper.family == 'DAT':
            prefix = 'Datcolor'
        else:
            return
        try:
            r = getattr(self.my.par, prefix + 'r', None)
            g = getattr(self.my.par, prefix + 'g', None)
            b = getattr(self.my.par, prefix + 'b', None)
            if r is None or g is None or b is None:
                return  # Params not present (older release)
            oper.color = (r.eval(), g.eval(), b.eval())
        except Exception as e:
            self.Log(
                f'Could not color {oper.path}: {e}', 'DEBUG')

    def RecolorAllExternalized(self) -> dict:
        """Apply the configured tint colors to every par-driven externalization.

        Useful when you change Compcolor / Datcolor and want
        existing nodes to pick up the new values, or after dropping a
        fresh Embody into a project that had its operators pre-existing.
        """
        n_comp = 0
        n_dat = 0
        for comp in self.getOpsByPar(COMP):
            self._applyExternalizedColor(comp)
            n_comp += 1
        for dat in self.getOpsByPar(DAT):
            self._applyExternalizedColor(dat)
            n_dat += 1
        self.Log(
            f'Recolored {n_comp} COMP(s) and {n_dat} DAT(s)', 'SUCCESS')
        return {'comps': n_comp, 'dats': n_dat}

    def _buildTDNRelPath(self, oper: OP) -> Path:
        """Compute the .tdn sidecar path for a COMP.

        The sidecar always lives directly beside the .tox -- same folder,
        same basename, .tdn extension. That keeps diffs, manual edits,
        and orphan-file cleanup intuitive (one folder per externalization).

        If the COMP doesn't have a par.externaltox value yet (rare edge:
        sidecar requested before externalization is set up), fall back
        to the project's default externalizations folder.
        """
        # COMP family: derive .tdn from par.externaltox so the sidecar
        # sits next to the .tox no matter where the user routed it.
        tox_par = getattr(oper.par, 'externaltox', None)
        if tox_par is not None:
            tox_rel = tox_par.eval()
            if tox_rel:
                norm = self.normalizePath(tox_rel)
                if norm.lower().endswith('.tox'):
                    return Path(norm[:-4] + '.tdn')
                return Path(norm + '.tdn')

        # Fallback: flat layout under the global externalizations folder.
        ext_folder = self.ExternalizationsFolder
        filename = oper.name + '.tdn'
        if ext_folder:
            return Path(ext_folder) / filename
        return Path(filename)

    def _writeTdnSidecar(self, comp: COMP) -> None:
        """Write the .tdn sidecar file for a COMP saved as .tox.

        Always-both-formats: every TOX externalization gets a .tdn sidecar
        alongside it for diff-friendly version control. Failures are
        non-fatal -- the .tox is the canonical artifact, the .tdn is bonus.

        cleanup_stale=False: sidecar writes never sweep other .tdn files.
        ExportNetwork's stale-file cleanup defaults to deleting every
        .tdn in the project folder that isn't in the protected list, and
        under par-driven discovery the protected list doesn't cover
        orphan / legacy .tdn files we have no record of -- so the cleanup
        was destroying sibling sidecars on every save (bug #8). Single-
        file sidecar writes don't need any cleanup; they just write
        their own .tdn and leave everything else alone.
        """
        try:
            tdn_path = self.buildAbsolutePath(self._buildTDNRelPath(comp))
            tdn_path.parent.mkdir(parents=True, exist_ok=True)
            self.my.ext.TDN.ExportNetwork(
                root_path=comp.path, output_file=str(tdn_path),
                cleanup_stale=False)
        except Exception as e:
            self.Log(f"TDN sidecar export failed for {comp.path}", "WARNING", str(e))

    def _setupCompForExternalization(self, oper, rel_file_path, save_file_path):
        """Configure a COMP's externalization parameters.

        Sets par.externaltox, par.enableexternaltox, and the reload/backup
        defaults from Embody's config toggles. Does NOT write the .tox
        file -- the user must explicitly Save (per-row Save button or
        SaveAllDirty). Setting par.externaltox just marks the row dirty
        so the user can see what would be written.
        """
        # Set external path. When the save path falls inside the user palette
        # folder, prefer an expression form so the .tox stays portable across
        # machines whose palette folders live in different absolute locations.
        oper.par.externaltox.readOnly = False
        palette_root = os.path.normpath(app.userPaletteFolder) if app.userPaletteFolder else ''
        abs_path = os.path.normpath(str(save_file_path))
        in_palette = bool(palette_root) and abs_path.startswith(palette_root + os.sep)

        if in_palette:
            rel_to_palette = os.path.relpath(abs_path, palette_root).replace('\\', '/')
            oper.par.externaltox.expr = f"app.userPaletteFolder + '/{rel_to_palette}'"
        elif not oper.par.externaltox.eval():
            oper.par.externaltox = rel_file_path
        else:
            oper.par.externaltox = self.normalizePath(oper.par.externaltox.eval())

        oper.par.enableexternaltox = True
        # Apply Embody's reload/backup defaults so externalized COMPs reload
        # the right slice of state when their .tox changes on disk.
        self._applyReloadDefaults(oper)

    def _setupDatForExternalization(self, oper, rel_file_path, save_file_path):
        """Configure a DAT's externalization parameters.

        Sets par.file and par.syncfile. Does NOT write the file -- the
        user must explicitly Save. Saving the DAT is what creates the
        file on disk.
        """
        if not oper.par.file.eval():
            oper.par.file = str(rel_file_path)
        else:
            oper.par.file = self.normalizePath(oper.par.file.eval())
        oper.par.syncfile = True

    def _applyReloadDefaults(self, comp: OP) -> None:
        """Apply Embody's Reload/Backup defaults to an externalized COMP.

        Sets:
          - par.reloadcustom (from Embody.par.Reloadcustom, default On)
          - par.reloadbuiltin (from Embody.par.Reloadbuiltin, default Off)
          - par.savebackup (from Embody.par.Savebackup, default Off)

        Each Embody param is optional; if missing (older release), the
        corresponding COMP par is left untouched.
        """
        mapping = (
            ('Reloadcustom', 'reloadcustom'),
            ('Reloadbuiltin', 'reloadbuiltin'),
            ('Savebackup', 'savebackup'),
        )
        for embody_par_name, comp_par_name in mapping:
            cfg = getattr(self.my.par, embody_par_name, None)
            if cfg is None:
                continue
            target_par = getattr(comp.par, comp_par_name, None)
            if target_par is None:
                continue
            try:
                target_par.val = bool(cfg.eval())
            except Exception:
                pass

    def SaveAllDirty(self, include_missing_sidecar: bool = True) -> dict:
        """Explicitly save every dirty externalized COMP / DAT.

        Iterates par-driven externalizations and saves any that:
          - Have a 'dirty' table flag set, OR
          - (For COMPs, when include_missing_sidecar=True) lack a .tdn
            sidecar on disk -- this fills gaps from imports that
            registered without a sidecar.

        Save() writes both .tox and .tdn for COMPs; DAT save writes the
        text/table content. No-op for non-externalized rows or those
        clean and already-paired with a sidecar.
        """
        saved_comps = []
        saved_dats = []

        for comp in self.getExternalizedOps(COMP):
            try:
                dirty_val = str(self.Externalizations[comp.path, 'dirty'].val)
            except Exception:
                dirty_val = ''
            is_dirty = dirty_val not in ('', 'False', 'false', '0',
                                          'Clean', 'Saved')
            missing_sidecar = False
            if include_missing_sidecar:
                try:
                    rel_tdn = self._buildTDNRelPath(comp)
                    abs_tdn = self.buildAbsolutePath(rel_tdn)
                    missing_sidecar = not abs_tdn.is_file()
                except Exception:
                    missing_sidecar = False
            if is_dirty or missing_sidecar:
                try:
                    self.Save(comp.path)
                    saved_comps.append(comp.path)
                except Exception as e:
                    self.Log(f'SaveAllDirty: {comp.path} failed: {e}',
                             'WARNING')

        for dat in self.getExternalizedOps(DAT):
            try:
                dirty_val = str(self.Externalizations[dat.path, 'dirty'].val)
            except Exception:
                dirty_val = ''
            is_dirty = dirty_val not in ('', 'False', 'false', '0',
                                          'Clean', 'Saved')
            if is_dirty:
                try:
                    if hasattr(dat.par, 'file') and dat.par.file.eval():
                        dat.save(dat.par.file.eval())
                        saved_dats.append(dat.path)
                except Exception as e:
                    self.Log(f'SaveAllDirty: {dat.path} failed: {e}',
                             'WARNING')

        if saved_comps or saved_dats:
            self.Log(
                f'SaveAllDirty: saved {len(saved_comps)} COMP(s) and '
                f'{len(saved_dats)} DAT(s)', 'SUCCESS')
        else:
            self.Log('SaveAllDirty: nothing to save', 'INFO')
        return {'comps': saved_comps, 'dats': saved_dats}
        # Leave par.file editable so users can retarget the externalized file.
        
        save_path_str = str(save_file_path)
        try:
            oper.save(save_path_str)
        except Exception as e:
            self.Log(f"Failed to save DAT {oper.path}", "ERROR", f"Path: {save_path_str}, Error: {e}")

    def _addToTable(self, oper, rel_file_path, timestamp, dirty,
                     build_num, touch_build):
        """Add or update operator entry in externalizations table."""
        normalized_path = self.normalizePath(rel_file_path)
        has_position_cols = self.Externalizations[0, 'node_x'] is not None

        # Build position/color strings from the operator
        node_x = str(int(oper.nodeX)) if has_position_cols else ''
        node_y = str(int(oper.nodeY)) if has_position_cols else ''
        node_color = ''
        if has_position_cols:
            c = oper.color
            node_color = f'{c[0]:.4f},{c[1]:.4f},{c[2]:.4f}'

        # Update existing row if present
        for row in range(1, self.Externalizations.numRows):
            if self.Externalizations[row, 'path'] == oper.path:
                self.Externalizations[row, 'rel_file_path'] = normalized_path
                if has_position_cols:
                    self.Externalizations[row, 'node_x'] = node_x
                    self.Externalizations[row, 'node_y'] = node_y
                    self.Externalizations[row, 'node_color'] = node_color
                return

        # Append new row
        row_data = [
            oper.path, oper.type, normalized_path, timestamp,
            dirty, build_num, touch_build,
        ]
        if has_position_cols:
            row_data.extend([node_x, node_y, node_color])
        self.Externalizations.appendRow(row_data)

    def _updatePositionInTable(self, oper: 'OP', op_path: str) -> None:
        """Update position/color metadata for an operator in the table."""
        if self.Externalizations[0, 'node_x'] is None:
            return
        self.Externalizations[op_path, 'node_x'] = str(int(oper.nodeX))
        self.Externalizations[op_path, 'node_y'] = str(int(oper.nodeY))
        c = oper.color
        self.Externalizations[op_path, 'node_color'] = (
            f'{c[0]:.4f},{c[1]:.4f},{c[2]:.4f}')

    def _scanAndPopulate(self) -> None:
        """Rebuild the Externalizations table from a live par-driven scan.

        Replaces the legacy externalizations.tsv persistence -- the table is
        now a pure view of in-TD state, refreshed on demand from
        par.externaltox / par.file. Build/Date/Touchbuild metadata is left
        blank (auto-injection is disabled; see setupBuildParameters).

        Also performs a one-time migration off the .tsv: clears par.file
        on the table so TD stops auto-syncing it to disk, and removes the
        stale externalizations.tsv file. Both operations are idempotent.

        Safe to call repeatedly. Preserves the header row.
        """
        table = self.Externalizations
        if not table:
            return

        # One-time migration: stop persisting the table to .tsv.
        if table.par.file.eval():
            stale_rel = table.par.file.eval()
            try:
                table.par.file = ''
                table.par.syncfile = False
            except Exception as e:
                self.Log(f'Could not clear table.par.file: {e}', 'WARNING')
            try:
                stale_abs = self.buildAbsolutePath(stale_rel)
                if stale_abs.is_file():
                    stale_abs.unlink()
                    self.Log(f'Removed legacy {stale_rel}', 'INFO')
            except Exception as e:
                self.Log(f'Could not remove legacy tsv: {e}', 'WARNING')

        table.clear(keepFirstRow=True)
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        for oper in self.getOpsByPar(COMP) + self.getOpsByPar(DAT):
            # Per-op fault isolation: a single broken op (e.g. an
            # invalidated parameter expression that throws on .eval()) must
            # not abort the rest of the scan.  Skip it and continue; the
            # table will simply be missing that one row.
            try:
                if oper.family == 'COMP':
                    rel = oper.par.externaltox.eval()
                    dirty = bool(oper.dirty)
                else:
                    rel = oper.par.file.eval()
                    dirty = ''
                self._addToTable(
                    oper, rel, timestamp, dirty,
                    build_num='', touch_build='',
                )
            except Exception as e:
                self.Log(
                    f'Skipped {oper.path} in scan ({type(e).__name__}: {e})',
                    'WARNING')

    def handleSubtraction(self, oper: OP) -> None:
        """Process removal of an operator from externalization.

        Table-state cleanup is no longer done inline -- _scanAndPopulate()
        at the end of Update() rebuilds the view from live par state. This
        method now only handles the par-side teardown (clear readOnly so
        downstream UI / RemoveListerRow can edit par.externaltox / par.file).
        """
        if oper.family == 'COMP':
            oper.par.externaltox.readOnly = False
        elif oper.family == 'DAT':
            oper.par.file.readOnly = False
        self.Log(f"Removed '{oper.path}'", "SUCCESS")

    def setupBuildParameters(self, oper: COMP, build_page: Any, build_num: int, touch_build: Union[str, int]) -> None:
        """Setup build tracking parameters on a COMP.

        Disabled in the fork -- Build/Date/Touchbuild auto-injection on
        externalized COMPs added noise to user networks. Signature kept
        intact so existing callers (handleAddition) continue to work.

        TODO: re-enable behind an opt-in setting (e.g. a Toggle on the
        Embody COMP named "Autoinjectbuildinfo"). For now, the body is
        a no-op.
        """
        return

    # ==========================================================================
    # PROJECT-WIDE EXTERNALIZATION
    # ==========================================================================

    def ExternalizeProject(self) -> None:
        """Externalize all compatible COMPs and DATs in project.

        With par-driven discovery, "externalize" means: set par.externaltox
        on every compatible COMP (defaults each to {folder}/{name}.tox via
        handleAddition), and set par.file on every supported DAT type.
        Every COMP also gets a .tdn sidecar via Phase 2's _writeTdnSidecar.
        """
        if self._performMode:
            return
        choice = ui.messageBox('Embody -- Externalize Full Project',
            'Add all compatible COMPs and DATs to Embody?\n'
            '(Palette components, clones, and replicants will be ignored)\n\n'
            'Each COMP saves as both .tox and .tdn. Each supported DAT\n'
            'saves to its file extension. Optionally also export a single\n'
            'project-wide .tdn snapshot (Ctrl+Shift+E).',
            buttons=['Cancel', 'Externalize',
                     'Externalize + Project TDN'])

        if choice < 1:
            return

        export_project_tdn = choice == 2

        # Find system COMPs to exclude
        sys_comps = self.root.findChildren(
            type=COMP, parName='clone',
            key=lambda x: any(s in (str(x.par.clone.expr) or '') for s in ['TDTox', 'TDBasicWidgets'])
        )

        paths_to_exclude = set()
        for sys_comp in sys_comps:
            paths_to_exclude.add(sys_comp.path)
            for desc in sys_comp.findChildren():
                paths_to_exclude.add(desc.path)

        folder = self.ExternalizationsFolder or ''

        # Track successes and per-op failures so the final popup can
        # tell the user what actually happened.  Without this the user
        # only sees the textport log and has no idea if the run worked.
        n_dat_ok = 0
        n_comp_ok = 0
        errors: list[tuple[str, str]] = []

        # Process DATs -- assign par.file based on type's default extension
        for oper in self.root.findChildren(type=DAT, parName='file'):
            if self._shouldSkipOp(oper, paths_to_exclude):
                continue
            if oper.type not in self.supported_dat_types:
                continue
            if oper.par.file.eval():
                continue  # already externalized
            ext = self.dat_type_to_extension.get(oper.type, 'py')
            try:
                oper.par.file.readOnly = False
                oper.par.file = f"{folder}/{oper.name}.{ext}" if folder else f"{oper.name}.{ext}"
                n_dat_ok += 1
            except Exception as e:
                self.Log(f'Failed to set par.file on {oper.path}: {e}', 'WARNING')
                errors.append((oper.path, f'{type(e).__name__}: {e}'))

        # Process COMPs -- assign par.externaltox so Update picks them up
        for oper in self.root.findChildren(type=COMP, parName='externaltox'):
            if self._shouldSkipOp(oper, paths_to_exclude):
                continue
            if oper.par.externaltox.eval():
                continue  # already externalized
            try:
                oper.par.externaltox.readOnly = False
                oper.par.externaltox = f"{folder}/{oper.name}.tox" if folder else f"{oper.name}.tox"
                n_comp_ok += 1
            except Exception as e:
                self.Log(f'Failed to set par.externaltox on {oper.path}: {e}', 'WARNING')
                errors.append((oper.path, f'{type(e).__name__}: {e}'))

        try:
            self.UpdateHandler()
        except Exception as e:
            self.Log(f'ExternalizeProject: UpdateHandler failed: {e}', 'ERROR')
            errors.append(('<UpdateHandler>', f'{type(e).__name__}: {e}'))

        # Export project-wide TDN snapshot if requested
        if export_project_tdn:
            try:
                self.my.ext.TDN.ExportNetworkAsync(
                    output_file='auto', embed_all=True)
            except Exception as e:
                self.Log(f'ExternalizeProject: TDN export failed: {e}', 'ERROR')
                errors.append(('<ExportNetworkAsync>',
                              f'{type(e).__name__}: {e}'))

        # Final summary popup. Always shown so the user gets confirmation
        # of what was processed; lists the first 10 failures inline.
        summary = (
            f'Externalized {n_comp_ok} COMP(s) and {n_dat_ok} DAT(s).')
        if errors:
            top = '\n'.join(f'  • {p}: {msg}' for p, msg in errors[:10])
            more = ('' if len(errors) <= 10
                    else f'\n  ... and {len(errors) - 10} more (see textport)')
            summary += (
                f'\n\nSkipped {len(errors)} item(s) due to errors:\n{top}{more}')
        self._messageBox('Embody -- Externalize Project',
                         summary, buttons=['OK'])

    def _shouldSkipOp(self, oper, paths_to_exclude):
        """Check if operator should be skipped in project externalization."""
        return (
            oper.path in paths_to_exclude or
            self.isReplicant(oper) or
            self.isInsideClone(oper) or
            oper.path.startswith('/local/') or
            oper.path == '/local'
        )

    # ==========================================================================
    # LISTER ROW REMOVAL
    # ==========================================================================

    def RemoveListerRow(self, op_path: str, rel_file_path: str, delete_file: bool = True) -> None:
        """
        Remove an operator from externalization tracking.
        SAFETY: Only deletes the file if it's tracked by Embody and not referenced elsewhere.
        When delete_file=False, the table row and tags are removed but the file is preserved on disk.
        """
        try:
            oper = op(op_path)
            if oper:
                if oper.family == 'COMP':
                    oper.par.externaltox = ''
                    oper.par.externaltox.readOnly = False
                elif oper.family == 'DAT':
                    oper.par.syncfile = False
                    oper.par.file = ''
                    oper.par.file.readOnly = False

                oper.cook(force=True)
                self.param_tracker.removeComp(op_path)
        except Exception as e:
            self.Log(f"Error handling operator '{op_path}'", "ERROR", str(e))

        # Check if file is still referenced by other operators
        normalized_path = self.normalizePath(rel_file_path)
        other_references = self._checkFileReferences(op_path, normalized_path)

        # Compute the .tdn sidecar path for COMP rows so it gets cleaned up
        # alongside the .tox. DAT rows don't have a sidecar.
        sidecar_rel = None
        is_comp_removal = (oper and oper.family == 'COMP') if 'oper' in dir() else False
        # The op may already be in an unsettled state -- recompute family
        # from the path string by inspecting the .tox extension instead.
        if normalized_path.lower().endswith('.tox'):
            sidecar_rel = normalized_path[:-4] + '.tdn'

        # Delete file only if:
        # 1. delete_file is True (caller wants file removed)
        # 2. No other operators reference it
        # 3. It's a file we're tracking (implicit - we got rel_file_path from our table)
        if delete_file and normalized_path and not other_references:
            full_path = self.buildAbsolutePath(normalized_path).resolve()
            sidecar_path = (
                self.buildAbsolutePath(sidecar_rel).resolve()
                if sidecar_rel else None)

            def _do_delete():
                for target in (full_path, sidecar_path):
                    if target is None:
                        continue
                    try:
                        if target.is_file():
                            target.unlink()
                    except Exception as e:
                        self.Log(f"Error removing {target}: {e}", "ERROR")

                # Clean up empty parent directories of the primary file
                parent_dir = full_path.parent
                while parent_dir.exists() and parent_dir != Path(project.folder):
                    try:
                        if not any(parent_dir.iterdir()):
                            parent_dir.rmdir()
                            parent_dir = parent_dir.parent
                        else:
                            break
                    except OSError:
                        break

            run(_do_delete, delayFrames=5)
        elif other_references:
            self.Log(f"Preserved file '{normalized_path}' (still in use)", "INFO")

        # Remove from table -- match on both path and rel_file_path to avoid
        # deleting sibling rows (e.g. a TDN row when removing the TOX row)
        removed = False
        for i in range(1, self.Externalizations.numRows):
            if (self.Externalizations[i, 'path'].val == op_path
                    and self.normalizePath(self.Externalizations[i, 'rel_file_path'].val) == normalized_path):
                try:
                    self.Externalizations.deleteRow(i)
                    self.Log(f"Removed '{op_path}'", "SUCCESS")
                    removed = True
                except Exception as e:
                    self.Log(f"Error removing from table", "ERROR", str(e))
                break
        if not removed:
            self.Debug(f"No table row for '{op_path}' with file '{normalized_path}' - already removed or never added")

    def _checkFileReferences(self, op_path, normalized_path):
        """Check if any other operators reference a file path."""
        if not normalized_path:
            return False
            
        for comp in self.root.findChildren(type=COMP, parName='externaltox'):
            if comp.path != op_path and self.normalizePath(comp.par.externaltox.eval()) == normalized_path:
                self.Log(f"File still referenced by '{comp.path}'", "INFO")
                return True
        
        for dat in self.root.findChildren(type=DAT, parName='file'):
            if dat.path != op_path and self.normalizePath(dat.par.file.eval()) == normalized_path:
                self.Log(f"File still referenced by '{dat.path}'", "INFO")
                return True
        
        return False

    # Perform Mode removed (cleanup 2026-05-19) -- under the par-driven
    # passive model, Embody has nothing meaningful to suspend during a
    # show. The Envoy toggle covers the only legitimate concern (an MCP
    # agent firing a tool call mid-render). _performMode is now a constant
    # False so the existing early-return guards collapse to no-ops; we
    # can sweep them out in a follow-up pass.
    _performMode = False

    # Tracks the last seen project.performMode so onFrameStart can detect
    # transitions without polling work between transitions.
    _last_perform_mode = False

    def _syncEnvoyToPerformMode(self) -> None:
        """Stop / restart Envoy when TD enters / exits perform mode.

        Gated by the Envoyoffinperform toggle. Called from execute.py's
        onFrameStart, so the check is per-frame -- but only the boolean
        comparison runs in the steady state. The Envoy Stop/Start path
        only fires on the rising / falling edge of project.performMode.

        Use case: shipping a project to a client where Envoy must not
        listen during a show. With the toggle on, entering TD perform
        mode automatically stops the MCP server; exiting restores it.
        """
        try:
            par = getattr(self.my.par, 'Envoyoffinperform', None)
            if par is None or not par.eval():
                return
            current = bool(project.performMode)
            if current == self._last_perform_mode:
                return  # No edge
            self._last_perform_mode = current
            if current:
                # Entered perform mode -- stop Envoy if running
                if self.my.fetch('envoy_running', False, search=False):
                    self.my.store('_envoy_was_running_before_perform', True)
                    self.my.ext.Envoy.Stop()
                    self.Log(
                        'Perform mode entered -- Envoy stopped '
                        '(Envoyoffinperform on)', 'INFO')
            else:
                # Exited perform mode -- restart if we stopped it
                if self.my.fetch('_envoy_was_running_before_perform', False,
                                 search=False):
                    self.my.unstore('_envoy_was_running_before_perform')
                    run("op.Embody.ext.Envoy.Start()", delayFrames=5)
                    self.Log(
                        'Perform mode exited -- Envoy restarting', 'INFO')
        except Exception as e:
            # Never let this break the per-frame pump
            try:
                self.Log(
                    f'_syncEnvoyToPerformMode error: {e}', 'DEBUG')
            except Exception:
                pass

    # ==========================================================================
    # FILE UTILITIES
    # ==========================================================================

    def deleteFile(self, oper: OP, externalizationsFolder: str) -> None:
        """
        Delete externalized file for an operator.
        SAFETY: This only deletes files at paths we generate for tracked operators.
        """
        abs_folder_path, save_file_path, _, _ = self.getOpPaths(oper, externalizationsFolder)
        if save_file_path is None:
            return

        save_file = save_file_path.resolve()
        try:
            if save_file.exists():
                save_file.unlink()
                self.Log(f"Deleted file: {save_file}", "INFO")
                try:
                    # Only remove directory if empty
                    abs_folder_path.rmdir()
                except OSError:
                    pass  # Directory not empty - this is fine
        except FileNotFoundError:
            self.Log(f"File not found: {save_file}", "WARNING")
        except PermissionError as e:
            self.Log(f"Permission denied deleting file {save_file}: {e}", "WARNING")
            pass
        except Exception as e:
            self.Log(f"Unexpected error deleting file {save_file}: {e}", "WARNING")
            pass

    # Directories that must never be touched by empty-dir cleanup
    _SCM_DIRS = {'.git', '.svn', '.hg'}

    def deleteEmptyDirectories(self, path: Union[str, Path]) -> None:
        """
        Recursively delete empty directories only.
        SAFETY: rmdir() only succeeds on empty directories.
        Skips version-control directories (.git, .svn, .hg).
        Never operates on project.folder or its parents.
        """
        path = Path(path)
        if not path.is_dir():
            return

        # SAFETY: Never walk project.folder -- too broad, can delete
        # unrelated empty directories (e.g. newly-created target folders)
        try:
            if path.resolve() == Path(project.folder).resolve():
                return
        except Exception:
            pass

        empty_dir_found = True
        iteration = 0

        while empty_dir_found and iteration < 10:
            empty_dir_found = False
            iteration += 1

            for root, dirs, files in os.walk(str(path), topdown=False):
                # Skip version-control internals entirely
                if any(part in self._SCM_DIRS for part in Path(root).parts):
                    continue
                for dir_name in dirs:
                    if dir_name in self._SCM_DIRS:
                        continue
                    dir_path = str(Path(root) / dir_name)
                    if not list(Path(dir_path).iterdir()):
                        try:
                            Path(dir_path).rmdir()
                            self.Log(f"Deleted empty directory: {dir_path}", "INFO")
                            empty_dir_found = True
                        except OSError as e:
                            self.Log(f"Error deleting directory: {dir_path}", "ERROR", str(e))

    # ==========================================================================
    # UI HELPERS
    # ==========================================================================

    def DirtyCount(self) -> int:
        """Return the number of dirty externalized operators.

        Checks live oper.dirty for COMPs (TD's native dirty flag updates
        immediately when a COMP is modified, but the Externalizations table
        is only refreshed during Refresh/Update). Falls back to the cached
        table value for DATs and 'Par' (parameter change) state.
        """
        if self._performMode:
            return 0
        table = self.Externalizations
        if not table:
            return 0
        count = 0
        for i in range(1, table.numRows):
            op_path = str(table[i, 'path'].val)
            oper = op(op_path)
            if oper and oper.valid and oper.family == 'COMP':
                if oper.dirty:
                    count += 1
                    continue
                # Check table for 'Par' state (parameter changes detected
                # during Refresh, not reflected in oper.dirty)
                val = str(table[i, 'dirty'].val)
                if val == 'Par':
                    count += 1
                continue
            # For DATs or missing operators, use cached table value
            val = str(table[i, 'dirty'].val)
            if val and val not in ('', 'False', 'Clean', 'Saved'):
                count += 1
        return count

    def Manager(self, action: str) -> None:
        """Open or close the manager window."""
        win = self.my.op('window_manager')
        if action == 'open':
            win.par.winopen.pulse()
            self.Refresh()
        elif action == 'close':
            win.par.winclose.pulse()

    # ==========================================================================
    # CONTEXTUAL ACTION MENU (Phase 2.5)
    # ==========================================================================

    def OpenActionMenu(self, op_path: Optional[str] = None) -> None:
        """Open the contextual Embody action menu for an operator.

        If op_path is None, picks the target from the user's current pane:
        the rollover op first, then the first selected op, then the pane
        owner as a fallback. The menu's options vary by op family and by
        whether the op is already externalized.
        """
        if self._performMode:
            return
        target = op(op_path) if op_path else self._resolveActionTarget()
        if target is None:
            self.Log('Action menu: no operator under cursor', 'WARNING')
            return
        if target.family not in ('COMP', 'DAT'):
            self.Log(f'Action menu: {target.family} not supported', 'WARNING')
            return

        if self._isOpExternalized(target):
            self._actionMenuExternalized(target)
        else:
            self._actionMenuNotExternalized(target)

    def _popDialog(self, *, text: str, title: str, buttons: list,
                    callback, esc_button: Optional[int] = None,
                    enter_button: Optional[int] = None,
                    text_entry: Optional[str] = None) -> None:
        """Open a TDResources.PopDialog (Externalize-style).

        Wraps op.TDResources.PopDialog.OpenDefault with sensible defaults
        and a graceful fallback to ui.messageBox if PopDialog isn't
        available (e.g. on TD builds without TDResources, in headless test
        contexts).
        """
        # Default escape/enter to last/first button if not specified
        if esc_button is None:
            esc_button = len(buttons)  # 1-indexed: last button
        if enter_button is None:
            enter_button = 1  # 1-indexed: first non-cancel button
        try:
            pop = op.TDResources.op('popDialog') or op.TDResources.PopDialog
        except Exception:
            pop = None
        if pop is None:
            # Synchronous fallback -- ui.messageBox returns the button index.
            kwargs = {'title': title, 'buttons': buttons}
            choice = ui.messageBox(title, text, buttons=buttons)
            callback({
                'button': buttons[choice] if 0 <= choice < len(buttons) else buttons[esc_button - 1],
                'enteredText': text_entry or '',
            })
            return
        kwargs = {
            'text': text,
            'title': title,
            'buttons': buttons,
            'callback': callback,
            'escButton': esc_button,
            'enterButton': enter_button,
            'escOnClickAway': True,
        }
        if text_entry is not None:
            kwargs['textEntry'] = text_entry
        try:
            pop.OpenDefault(**kwargs)
        except Exception as e:
            self.Log(f'PopDialog failed, falling back to messageBox: {e}', 'WARNING')
            choice = ui.messageBox(title, text, buttons=buttons)
            callback({
                'button': buttons[choice] if 0 <= choice < len(buttons) else buttons[esc_button - 1],
                'enteredText': text_entry or '',
            })

    def _resolveActionTarget(self) -> Optional[OP]:
        """Pick the operator to act on from the active pane context."""
        try:
            pane = ui.panes.current
        except Exception:
            pane = None
        if pane is None or not getattr(pane, 'owner', None):
            return None
        target = None
        # Rollover (cursor-over op) takes priority
        try:
            target = pane.rolloverOp
        except Exception:
            pass
        # Otherwise the first selected op in the pane's owner
        if target is None:
            try:
                sel = list(pane.owner.selectedChildren)
                if sel:
                    target = sel[0]
            except Exception:
                pass
        # Otherwise the pane's owner itself
        if target is None:
            target = pane.owner
        return target

    def _isOpExternalized(self, target: OP) -> bool:
        """True iff the operator's native external-file par is set."""
        try:
            if target.family == 'COMP':
                return bool(target.par.externaltox.eval())
            elif target.family == 'DAT':
                return bool(target.par.file.eval())
        except Exception:
            pass
        return False

    def _actionMenuExternalized(self, target: OP) -> None:
        """Async menu for an op that is already externalized."""
        is_comp = (target.family == 'COMP')
        rel = (target.par.externaltox.eval() if is_comp
               else target.par.file.eval())
        # PopDialog caps at 4 buttons -- the other actions are covered by
        # row clicks: File-column click = Reveal, x-column click = Remove.
        # Re-externalize is a power-user operation reachable by clearing
        # par.externaltox manually + saving to a new folder.
        buttons = ['Save']
        if is_comp:
            buttons.append('Reload')
            buttons.append('Release')
        buttons.append('Cancel')

        def on_choice(info, t=target):
            btn = info.get('button')
            if btn == 'Save':
                self._saveOpFromMenu(t)
            elif btn == 'Reload':
                self._actionMenuReload(t)
            elif btn == 'Release':
                self._actionMenuReleaseName(t)
            # Cancel / unknown -> no-op

        self._popDialog(
            text=f'{target.path}\nexternalized to: {rel}',
            title=f'Embody: {target.name}',
            buttons=buttons,
            callback=on_choice,
            esc_button=len(buttons),  # Cancel
            enter_button=1,           # Save
        )

    def _actionMenuReload(self, target: OP) -> None:
        """Sub-menu: choose how to reload an externalized COMP from disk.

        Only offers the .tdn option when the sidecar actually exists on
        disk -- showing a .tdn button that resolves to "no file found"
        is a worse UX than silently skipping it.
        """
        if target.family != 'COMP':
            self.Log('Reload only applies to COMPs', 'WARNING')
            return

        rel_tdn = self._buildTDNRelPath(target)
        tdn_exists = self.buildAbsolutePath(rel_tdn).is_file()

        # Common case: .tox is always there for externalized COMPs.
        # If no .tdn sidecar exists, skip the chooser and reload from .tox.
        if not tdn_exists:
            self._reloadFromTox(target)
            return

        buttons = ['.tdn', '.tox', 'Cancel']

        def on_choice(info, t=target):
            btn = info.get('button')
            if btn == '.tdn':
                self.ReloadFromTdn(t.path)
            elif btn == '.tox':
                self._reloadFromTox(t)

        self._popDialog(
            text=f'Reload {target.path} from disk.\n\n'
                 f'.tdn = JSON sidecar (agent-edited, diff-friendly)\n'
                 f'.tox = binary (TD native, faster)',
            title=f'Embody: Reload {target.name}',
            buttons=buttons,
            callback=on_choice,
            esc_button=len(buttons),
            enter_button=1,
        )

    def _reloadFromTox(self, target: OP) -> None:
        """Reload a COMP from its .tox file via TD's native pulse parameter.

        After a successful reload, the live content matches disk so the
        row should display as Saved -- _markCleanAfterReload resets the
        dirty markers.

        Note on getattr-vs-or: Par objects are falsy when their value is
        0 (which a pulse param always is at rest), so
        `getattr(...) or getattr(...)` skips a valid pulse param and
        falls through to the next branch. Always check `is None` against
        getattr-returned Par objects.
        """
        try:
            if not target.par.externaltox.eval():
                self.Log(f'No .tox path set for {target.path}', 'WARNING')
                return
            pulse_par = getattr(target.par, 'enableexternaltoxpulse', None)
            if pulse_par is None:
                pulse_par = getattr(target.par, 'reloadtoxpulse', None)
            if pulse_par is not None:
                pulse_par.pulse()
                self.Log(f'Reloaded {target.path} from .tox', 'SUCCESS')
                self._markCleanAfterReload(target)
                return
            self.Log(
                f'No reload pulse on {target.path} '
                f'(enableexternaltoxpulse / reloadtoxpulse missing)',
                'WARNING')
        except Exception as e:
            self.Log(
                f'Reload from .tox failed for {target.path}: {e}', 'ERROR')

    def _markCleanAfterReload(self, comp: OP) -> None:
        """Reset dirty markers after a successful reload from disk.

        Live content now matches the file, so the row should read as
        Saved. Update the table dirty cell, snapshot the parameter
        store, and re-store the network fingerprint.
        """
        try:
            self.Externalizations[comp.path, 'dirty'] = False
        except Exception:
            pass
        try:
            self.param_tracker.updateParamStore(comp)
        except Exception:
            pass
        try:
            self._storeTDNFingerprint(comp)
        except Exception:
            pass

    def _actionMenuReleaseName(self, target: OP) -> None:
        """First step of the Release cascade: prompt for the release name."""
        def on_choice(info, t=target):
            if info.get('button') != 'OK':
                return
            new_name = (info.get('enteredText') or '').strip() or t.name
            self._actionMenuReleaseVersion(t, new_name)

        self._popDialog(
            text='Release name (defaults to the operator name).',
            title=f'Embody: Release {target.name} (1/3)',
            buttons=['OK', 'Cancel'],
            callback=on_choice,
            esc_button=2,
            enter_button=1,
            text_entry=target.name,
        )

    def _actionMenuReleaseVersion(self, target: OP, new_name: str) -> None:
        """Second step: prompt for version. Auto-adds par.Version if missing."""
        if not hasattr(target.par, 'Version'):
            try:
                about_page = next(
                    (p for p in target.customPages if p.name == 'About'),
                    None)
                if about_page is None:
                    about_page = target.appendCustomPage('About')
                pg = about_page.appendStr('Version', label='Version')
                p = pg[0]
                p.default = '1.0.0'
                p.val = '1.0.0'
                p.help = ('Release version (set by Embody Release). Stored '
                          'on the COMP for future releases.')
            except Exception as e:
                self.Log(
                    f'Release: could not add Version par to {target.path}: {e}',
                    'WARNING')

        current_version = (
            str(target.par.Version.eval())
            if hasattr(target.par, 'Version') else '1.0.0'
        ) or '1.0.0'

        def on_choice(info, t=target, name=new_name):
            if info.get('button') != 'OK':
                return
            new_version = (info.get('enteredText') or '').strip() or '1.0.0'
            try:
                if hasattr(t.par, 'Version'):
                    t.par.Version.val = new_version
            except Exception:
                pass
            self._actionMenuReleaseFolder(t, name, new_version)

        self._popDialog(
            text=f'Release version for "{new_name}".',
            title=f'Embody: Release {target.name} (2/3)',
            buttons=['OK', 'Cancel'],
            callback=on_choice,
            esc_button=2,
            enter_button=1,
            text_entry=current_version,
        )

    def _actionMenuReleaseFolder(self, target: OP, new_name: str,
                                  new_version: str) -> None:
        """Final step: pick folder, then run Release."""
        start = self.ExternalizationsFolder or project.folder
        chosen = ui.chooseFolder(
            title=f'Release {new_name}_{new_version}.tox - choose folder',
            start=start)
        if not chosen:
            return
        save_path = f'{chosen}/{new_name}_{new_version}.tox'
        # Catch exceptions Release didn't already convert into the
        # {'error': ...} return dict (e.g. unexpected bugs above the
        # try/except boundary).  Either way, surface to the user.
        try:
            result = self.Release(target, name=new_name, version=new_version,
                                  save_path=save_path)
        except Exception as e:
            result = {'error': f'{type(e).__name__}: {e}'}
        if isinstance(result, dict) and result.get('error'):
            self._messageBox(
                'Embody -- Release Failed',
                f'Could not release {target.path}:\n\n{result["error"]}\n\n'
                f'See textport for full traceback.',
                buttons=['OK'])
        else:
            self._messageBox(
                'Embody -- Release Complete',
                f'Released {target.path} to:\n{save_path}',
                buttons=['OK'])

    def _actionMenuNotExternalized(self, target: OP) -> None:
        """Async menu for an op that is not yet externalized."""
        is_comp = (target.family == 'COMP')
        if not is_comp and target.type not in self.supported_dat_types:
            self.Log(
                f'Cannot externalize DAT type {target.type!r}', 'WARNING')
            return
        default_par = ('Defaulttoxfolder' if is_comp
                       else 'Defaultscriptfolder')
        has_default = bool(
            getattr(self.my.par, default_par, None)
            and getattr(self.my.par, default_par).eval())
        buttons = []
        if has_default:
            buttons.append('To default')
        buttons.append('Choose...')
        buttons.append('Cancel')

        def on_choice(info, t=target):
            btn = info.get('button')
            if btn == 'To default':
                self._externalizeViaMenu(t, use_default=True)
            elif btn == 'Choose...':
                self._externalizeViaMenu(t, use_default=False)

        self._popDialog(
            text=f'Externalize {target.path}?',
            title=f'Embody: {target.name}',
            buttons=buttons,
            callback=on_choice,
            esc_button=len(buttons),  # Cancel
            enter_button=1,
        )

    def _externalizeViaMenu(self, target: OP, use_default: bool) -> None:
        """Externalize via Ctrl+W menu: set path par, register, and save.

        Ctrl+W is the explicit "externalize this op" gesture, so the
        file gets written immediately -- unlike par-driven discovery
        which just registers the op and leaves saving to the user. The
        sequence: pick folder -> set par -> Update (registers in table,
        applies tint and reload defaults) -> Save (writes .tox / file
        + .tdn sidecar for COMPs).
        """
        is_comp = (target.family == 'COMP')
        folder = self._getSaveLocation(target, use_default, is_tox=is_comp)
        if folder is None:
            return
        if is_comp:
            rel = f'{folder}/{target.name}.tox' if folder else f'{target.name}.tox'
            try:
                target.par.externaltox.readOnly = False
            except Exception:
                pass
            target.par.externaltox = rel
        else:
            ext = self.dat_type_to_extension.get(target.type, 'py')
            rel = (f'{folder}/{target.name}.{ext}' if folder
                   else f'{target.name}.{ext}')
            try:
                target.par.file.readOnly = False
            except Exception:
                pass
            target.par.file = rel

        # Sync first (registers the row + applies reload/tint defaults
        # via handleAddition). Then explicitly Save so the file lands on
        # disk -- the whole point of an externalize gesture.
        self.Update(suppress_refresh=True)
        try:
            if is_comp:
                self.Save(target.path)
            else:
                abs_path = self.buildAbsolutePath(rel)
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                target.save(str(abs_path))
                self.Log(f'Saved {target.path}', 'SUCCESS')
        except Exception as e:
            self.Log(
                f'Externalize save failed for {target.path}: {e}', 'ERROR')

    def _reexternalizeViaMenu(self, target: OP) -> None:
        """Clear and re-set the external path so the user can pick a new folder."""
        is_comp = (target.family == 'COMP')
        try:
            if is_comp:
                target.par.externaltox.readOnly = False
                target.par.externaltox = ''
            else:
                target.par.file.readOnly = False
                target.par.file = ''
                target.par.syncfile = False
        except Exception as e:
            self.Log(f'Re-externalize: failed to clear par: {e}', 'WARNING')
            return
        # Re-route through the not-yet-externalized branch so the user picks
        # default vs custom folder.
        self._actionMenuNotExternalized(target)

    def _getSaveLocation(self, target: OP, use_default: bool, is_tox: bool
                          ) -> Optional[str]:
        """Resolve the externalization folder for an operator.

        If use_default is True and the matching Defaulttoxfolder /
        Defaultscriptfolder param is set and exists on disk, returns its
        normalized value. Otherwise prompts the user via ui.chooseFolder.
        Returns the folder as a string (possibly empty for project root),
        or None if the user cancels.
        """
        if use_default:
            par_name = 'Defaulttoxfolder' if is_tox else 'Defaultscriptfolder'
            par = getattr(self.my.par, par_name, None)
            default_val = str(par.eval()) if par else ''
            if default_val:
                # Resolve relative paths against project.folder for the existence check
                check_path = (Path(default_val) if Path(default_val).is_absolute()
                              else Path(project.folder) / default_val)
                if check_path.is_dir():
                    return self.normalizePath(default_val)
                self.Log(
                    f'Default folder missing on disk: {default_val} -- prompting',
                    'WARNING')
        start = self.ExternalizationsFolder or project.folder
        chosen = ui.chooseFolder(
            title=f'Externalize {target.name} - choose folder',
            start=start)
        if not chosen:
            return None
        # Convert absolute → relative-to-project where possible
        try:
            rel = str(Path(chosen).resolve().relative_to(
                Path(project.folder).resolve()))
            return self.normalizePath(rel if rel != '.' else '')
        except Exception:
            return self.normalizePath(chosen)

    def _saveOpFromMenu(self, target: OP) -> None:
        """Save an already-externalized operator."""
        try:
            if target.family == 'COMP':
                self.Save(target.path)
            elif target.family == 'DAT':
                abs_path = self.buildAbsolutePath(target.par.file.eval())
                target.save(str(abs_path))
                self.Log(f'Saved {target.path}', 'SUCCESS')
        except Exception as e:
            self.Log(f'Save failed for {target.path}: {e}', 'ERROR')

    def RebuildAllFromTdn(self) -> None:
        """Rebuild every externalized COMP from its .tdn sidecar.

        Walks all COMPs with par.externaltox set, computes the sidecar
        .tdn path via _buildTDNRelPath, and calls ReloadFromTdn on each.
        Skips COMPs whose .tdn is missing. Logs a per-COMP success/fail
        and a summary at the end.

        Use case: fresh git clone, bulk re-sync after `git pull`, or
        recovery when in-TD state has drifted from the on-disk .tdn
        files.
        """
        if self._performMode:
            return
        comps = self.getOpsByPar(COMP)
        if not comps:
            self.Log('RebuildAllFromTdn: nothing to do', 'INFO')
            return
        ok = 0
        missing = 0
        failed = 0
        for comp in comps:
            try:
                rel_tdn = self._buildTDNRelPath(comp)
                abs_tdn = self.buildAbsolutePath(rel_tdn)
                if not abs_tdn.is_file():
                    missing += 1
                    continue
                result = self.my.ext.TDN.ImportNetworkFromFile(
                    str(abs_tdn), target_path=comp.path, clear_first=True)
                if isinstance(result, dict) and result.get('error'):
                    self.Log(
                        f'RebuildAllFromTdn: failed for {comp.path}: '
                        f'{result["error"]}', 'WARNING')
                    failed += 1
                else:
                    ok += 1
            except Exception as e:
                self.Log(
                    f'RebuildAllFromTdn: failed for {comp.path}: {e}',
                    'WARNING')
                failed += 1
        self.Log(
            f'RebuildAllFromTdn: rebuilt {ok}, missing .tdn {missing}, '
            f'failed {failed}',
            'SUCCESS' if failed == 0 else 'WARNING')

    def ReloadFromTdn(self, comp_path: str) -> None:
        """Rebuild a COMP from its .tdn sidecar on disk.

        Wraps TDNExt.ImportNetworkFromFile with clear_first=True so the
        on-disk .tdn becomes the source of truth. The natural use case
        is `agent edits .tdn -> user clicks Reload from .tdn -> live
        network reflects the edit`.
        """
        if self._performMode:
            return
        target = op(comp_path)
        if not target or target.family != 'COMP':
            self.Log(
                f'ReloadFromTdn requires a COMP: {comp_path}', 'ERROR')
            return
        rel_tdn = self._buildTDNRelPath(target)
        abs_tdn = self.buildAbsolutePath(rel_tdn)
        if not abs_tdn.is_file():
            self.Log(
                f'No .tdn sidecar found for {comp_path} at {rel_tdn}',
                'WARNING')
            return
        try:
            result = self.my.ext.TDN.ImportNetworkFromFile(
                str(abs_tdn), target_path=comp_path, clear_first=True)
            if isinstance(result, dict) and result.get('error'):
                self.Log(
                    f'Reload from .tdn failed for {comp_path}: '
                    f'{result["error"]}', 'ERROR')
            else:
                self.Log(
                    f'Reloaded {comp_path} from {rel_tdn}', 'SUCCESS')
                self._markCleanAfterReload(target)
        except Exception as e:
            self.Log(
                f'Reload from .tdn failed for {comp_path}: {e}', 'ERROR')

    def getProjectFolder(self) -> str:
        """Absolute folder path used as the externalization root.

        The legacy `par.Folder` is gone. Each op family now uses its
        own default (Defaulttoxfolder for COMPs, Defaultscriptfolder
        for DATs). This helper returns the project root, kept as a
        compatibility shim for callers that need *somewhere* to point.
        """
        return str(Path(project.folder))

    def getSaveFolder(self) -> str:
        """Folder shown by OpenSaveFolder. Prefers Defaulttoxfolder
        (most users keep their .tox files there). Falls back to project
        root.
        """
        default = self.ExternalizationsFolder
        if default:
            base = Path(project.folder) / default
            if base.is_dir():
                return str(base)
        return str(Path(project.folder))

    def OpenSaveFolder(self) -> None:
        """Open externalization folder in file browser."""
        save_folder = str(Path(self.getSaveFolder()).resolve())

        try:
            if sys.platform.startswith('darwin'):
                result = subprocess.call(['open', save_folder])
                if result != 0:
                    self.Log(f'Failed to open folder: {save_folder}', 'WARNING')
            elif sys.platform.startswith('win'):
                os.startfile(save_folder)
        except Exception as e:
            self.Log(f'Failed to open folder: {e}', 'ERROR')

    def OpenSaveFile(self, rel_file_path: str) -> None:
        """Open file location in file browser."""
        filepath = str(self.buildAbsolutePath(self.normalizePath(rel_file_path)).resolve())

        try:
            if sys.platform.startswith('darwin'):
                result = subprocess.call(['open', '-R', filepath])
                if result != 0:
                    self.Log(f'Failed to open file location: {filepath}', 'WARNING')
            elif sys.platform.startswith('win'):
                # explorer.exe /select,<path> returns exit code 1 even on
                # success (by design -- the launcher detaches). Don't gate
                # on the return code or every successful click logs a
                # false-positive warning.
                filepath = filepath.replace('/', '\\')
                subprocess.Popen(['explorer', f'/select,{filepath}'])
        except Exception as e:
            self.Log(f'Failed to open file location: {e}', 'ERROR')

    def OpenTable(self) -> None:
        """Open externalizations table viewer (debug helper).

        The table is internal state -- normal usage flows through the
        manager window. This stays as a convenience for development.
        """
        t = self.Externalizations
        if t:
            t.openViewer()

    def ImportTDNFromDialog(self) -> None:
        """Open file dialog and import selected .tdn file.

        Auto-detects the target COMP from the file's location relative to
        project.folder using Embody's bijective naming convention. If the
        target exists and has children, prompts Replace/Keep Both/Cancel.
        Falls back to Current Network/Project Root dialog when the target
        cannot be inferred.
        """
        path = ui.chooseFile(fileTypes=['tdn'], title='Import TDN File')
        if not path:
            return

        clear_first = False
        network_path = self._inferTargetFromPath(str(path))

        if network_path:
            target_comp = op(network_path)
            if target_comp and hasattr(target_comp, 'create'):
                child_count = len(target_comp.children)
                if child_count > 0:
                    choice = ui.messageBox('Import TDN',
                        f'Target: {network_path}\n'
                        f'Contains {child_count} operator{"s" if child_count != 1 else ""}.\n\n'
                        f'Existing contents will be replaced.',
                        buttons=['Replace', 'Keep Both', 'Cancel'])
                    if choice == 0:
                        clear_first = True
                    elif choice == 1:
                        clear_first = False
                    else:
                        return
                # else: empty target, import silently
            else:
                network_path = None  # COMP doesn't exist, fall through

        if not network_path:
            choice = ui.messageBox('Import TDN',
                f'Import into which network?\n\nFile: {path}',
                buttons=['Current Network', 'Project Root', 'Cancel'])
            if choice == 0:
                pane = ui.panes.current
                network_path = pane.owner.path if pane and pane.owner else '/'
            elif choice == 1:
                network_path = '/'
            else:
                return

        self._import_clear_first = clear_first
        self.my.par.Tdnfile = str(path)
        self.my.par.Networkpath = network_path
        self.my.par.Importtdn.pulse()

    def _inferTargetFromPath(self, file_path: str) -> Optional[str]:
        """Derive a TD COMP path from a .tdn file's location relative to project.folder.

        Uses Embody's bijective naming convention:
            {project.folder}/embody/base1.tdn → /embody/base1

        Returns the TD path string, or None if the file is outside the project.
        """
        try:
            rel = Path(file_path).relative_to(project.folder)
        except ValueError:
            return None  # File is outside project folder
        stem = str(rel).replace('\\', '/').removesuffix('.tdn')
        if not stem:
            return None
        # Check if this is a project-root export (filename matches project name)
        project_name = project.name.removesuffix('.toe')
        if stem == project_name:
            return '/'
        return '/' + stem

    # ==========================================================================
    # LOGGING
    # ==========================================================================

    def Log(self, message: str, level: str = 'INFO', details: Optional[str] = None, _depth: int = 1) -> None:
        """
        Centralized logging with auto caller detection, FIFO DAT storage,
        ring buffer for MCP access, and optional file logging.

        Accessible globally as op.Embody.Log(message, level).

        Args:
            message: Main message
            level: 'INFO', 'WARNING', 'ERROR', 'SUCCESS', or 'DEBUG'
            details: Optional additional details
            _depth: Stack frame depth for caller detection (internal use)
        """
        # Auto-detect caller via inspect
        frame = inspect.currentframe()
        for _ in range(_depth):
            frame = frame.f_back
        caller_locals = frame.f_locals
        caller_info = None

        if 'self' in caller_locals and hasattr(caller_locals['self'], '__class__'):
            ext = caller_locals['self']
            caller_info = f"{ext.__class__.__name__}"
        elif 'me' in caller_locals:
            caller_info = f"{caller_locals['me'].path}"
        else:
            frame_info = inspect.getframeinfo(frame)
            caller_info = f"{os.path.basename(frame_info.filename)}:{frame_info.lineno}"

        time_str = datetime.now().strftime("%H:%M:%S")
        current_frame = absTime.frame

        # Append structured entry to ring buffer for MCP access (all levels)
        self._log_counter += 1
        self._log_buffer.append({
            'id': self._log_counter,
            'timestamp': datetime.now().isoformat(),
            'frame': current_frame,
            'level': level,
            'source': caller_info,
            'message': message,
            'details': details,
        })

        # Skip DEBUG output to FIFO/textport/file unless Verbose is enabled
        if level == 'DEBUG' and not self.my.par.Verbose:
            return

        # Structured log entry string
        log_entry = f"{time_str} {current_frame:>7} {level:<7} {caller_info}: {message}"
        if details:
            log_entry += f"\n    Details: {details}"

        # Output to FIFO DAT
        if self._fifo:
            self._fifo.appendRow([log_entry])

        # Print to textport if enabled
        if self.my.par.Print:
            print(log_entry)

        # File logging if enabled
        if self.my.par.Logtofile and self.my.par.Logfolder:
            try:
                self._write_log_to_file(log_entry)
            except Exception as e:
                print(f"Error writing to log file: {e}")

    def Debug(self, msg: str) -> None:
        """Log a DEBUG level message."""
        self.Log(msg, level='DEBUG', _depth=2)

    def Info(self, msg: str) -> None:
        """Log an INFO level message."""
        self.Log(msg, level='INFO', _depth=2)

    def Warn(self, msg: str) -> None:
        """Log a WARNING level message."""
        self.Log(msg, level='WARNING', _depth=2)

    def Error(self, msg: str) -> None:
        """Log an ERROR level message."""
        self.Log(msg, level='ERROR', _depth=2)

    # --- File Logging Helpers ---

    LOG_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

    def _get_log_file_path(self):
        """
        Build the current log file path.
        Format: <Logfolder>/<project.name>_YYMMDD.log
        Rotates to _001, _002, etc. when file exceeds LOG_MAX_FILE_SIZE.
        """
        log_folder = self.my.par.Logfolder.eval()
        if not log_folder:
            return None

        # Ensure folder exists (relative path OK)
        os.makedirs(log_folder, exist_ok=True)

        date_str = datetime.now().strftime('%y%m%d')
        proj_name = project.name
        base_name = f'{proj_name}_{date_str}'

        # Check base file first
        base_path = os.path.join(log_folder, f'{base_name}.log')
        if not os.path.exists(base_path) or os.path.getsize(base_path) < self.LOG_MAX_FILE_SIZE:
            return base_path

        # Find next rotation index
        idx = 1
        while True:
            rotated_path = os.path.join(log_folder, f'{base_name}_{idx:03d}.log')
            if not os.path.exists(rotated_path) or os.path.getsize(rotated_path) < self.LOG_MAX_FILE_SIZE:
                return rotated_path
            idx += 1

    def _write_log_to_file(self, log_entry):
        """Write a log entry to the current log file."""
        file_path = self._get_log_file_path()
        if file_path:
            with open(file_path, 'a', encoding='utf-8') as f:
                f.write(log_entry + '\n')


# ==============================================================================
# PARAMETER TRACKER
# ==============================================================================

class ParameterTracker:
    """Tracks parameter changes on COMPs to detect dirty state."""

    def __init__(self, ownerComp):
        self.my = ownerComp
        self.param_store = {}
        
    def captureParameters(self, comp):
        """Capture all parameters of a COMP.

        Reads each parameter inside its own try/except: a broken
        expression on one parameter (e.g. a stale op() reference)
        must not abort the entire capture and leave the COMP's
        dirty-state un-checkable.  The broken parameter is recorded
        as a sentinel so a later edit on it still flips the COMP to
        dirty, and the rest of the parameters are captured cleanly.
        """
        params = {}
        for page in comp.pages + comp.customPages:
            for par in page.pars:
                if par.name in ['externaltox', 'file']:
                    continue
                try:
                    params[par.name] = {
                        'value': par.eval(),
                        'expr': par.expr if par.expr else None,
                        'bindExpr': par.bindExpr if par.bindExpr else None,
                        'mode': par.mode
                    }
                except Exception as e:
                    # Sentinel marks the param as "unreadable but present";
                    # the str(e) lets compareParameters detect a change if
                    # the error message itself shifts.
                    params[par.name] = {
                        'value': f'<unreadable: {type(e).__name__}>',
                        'expr': None,
                        'bindExpr': None,
                        'mode': None,
                    }
        return params
    
    def updateParamStore(self, comp):
        """Update stored parameters for a COMP."""
        self.param_store[comp.path] = self.captureParameters(comp)
        
    def compareParameters(self, comp):
        """Compare current parameters with stored. Returns True if changed."""
        if comp.path not in self.param_store:
            self.updateParamStore(comp)
            return False
            
        stored = self.param_store[comp.path]
        current = self.captureParameters(comp)
        
        # Check for additions/removals
        if set(current.keys()) != set(stored.keys()):
            return True
        
        # Check values
        for name in stored:
            if name not in current:
                return True
            if (stored[name]['value'] != current[name]['value'] or
                stored[name]['expr'] != current[name]['expr'] or
                stored[name].get('bindExpr') != current[name].get('bindExpr') or
                stored[name]['mode'] != current[name]['mode']):
                return True
        
        return False
    
    def removeComp(self, comp_path):
        """Remove a COMP from tracking."""
        self.param_store.pop(comp_path, None)

    def initializeTracking(self, embody):
        """Initialize tracking for all externalized COMPs."""
        self.param_store = {}
        for comp in embody.getExternalizedOps(COMP):
            self.updateParamStore(comp)
            embody.Log(f"Initialized tracking for {comp.path}", "INFO")