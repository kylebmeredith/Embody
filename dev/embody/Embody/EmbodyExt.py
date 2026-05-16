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
        'Folder', 'Envoyenable', 'Envoyport', 'Aiclient',
        # Behavior
        'Logfolder', 'Logtofile', 'Verbose', 'Print',
        'Detectduplicatepaths', 'Localtimestamps',
        # Action menu / save UX (Phase 2.5)
        'Defaulttoxfolder', 'Defaultscriptfolder', 'Synconsave',
        # Manager filters + view mode (Phase 4)
        'Filterdirty', 'Filterdats', 'Listmode',
        # TDN
        'Tdnmode',
        'Embeddatsintdns', 'Embedstorageintdns', 'Tdndatsafety',
        'Tdncreateonstart', 'Tdnstriponsave',
        'Toxrestoreonstart', 'Datrestoreonstart', 'Filecleanup',
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
        """Returns the externalizations table DAT."""
        return self.my.par.Externalizations.eval()

    @property
    def ExternalizationsFolder(self) -> str:
        """Returns the configured externalization folder, or empty string."""
        return self.my.par.Folder.eval() or ''

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

    def setExternalPath(self, oper: OP, path_str: str, readonly: bool = True) -> None:
        """Set the external file path on an operator (normalized)."""
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
            externalizationsFolder = self.ExternalizationsFolder
        
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
        """Create or reset the externalizations tracking table."""
        table_name = 'externalizations'
        externalizations_dat = self.Externalizations

        # Update scenario: par reference is lost but the sibling table survived
        # Embody deletion (undocked tables are not deleted with their host).
        if not externalizations_dat:
            existing_sibling = self.my.parent().op(table_name)
            if existing_sibling and existing_sibling.family == 'DAT':
                externalizations_dat = existing_sibling
                self.my.par.Externalizations.val = externalizations_dat
                self.Log(f"Re-connected to existing '{table_name}' tableDAT", "INFO")

        if not externalizations_dat:
            # Truly fresh install -- create new table as a regular sibling.
            # NOTE: not docked to Embody so the table survives when Embody is
            # deleted during an upgrade (delete old → drag new .tox).
            externalizations_dat = self.my.parent().create(tableDAT, table_name)
            externalizations_dat.nodeX = self.my.nodeX - 200
            externalizations_dat.nodeY = self.my.nodeY
            externalizations_dat.color = (0.55, 0.55, 0.55)
            externalizations_dat.clear()
            externalizations_dat.appendRow([
                'path', 'type', 'strategy', 'rel_file_path', 'timestamp',
                'dirty', 'build', 'touch_build'
            ])
            self.Log(f"Created '{table_name}' tableDAT", "SUCCESS")
        else:
            externalizations_dat.clear(keepFirstRow=True)
            self.Log(f"Reset '{table_name}' tableDAT", "INFO")

        self.my.par.Externalizations.val = externalizations_dat

    def CreateExternalizationsTable(self) -> None:
        """Recovery/init method: create or reconnect the externalizations table.

        Safe to call at any time. No-op if the table already exists and is
        connected via par.Externalizations. If the parameter is empty but a
        sibling named 'externalizations' exists (e.g. after an Embody upgrade),
        reconnects to it without creating a duplicate.
        """
        externalizations_dat = self.Externalizations
        if not externalizations_dat:
            existing_sibling = self.my.parent().op('externalizations')
            if existing_sibling and existing_sibling.family == 'DAT':
                self.my.par.Externalizations.val = existing_sibling
                self.Log('Re-connected to existing externalizations tableDAT', 'INFO')
                return
        if externalizations_dat:
            self.Log('Externalizations table already exists', 'INFO')
            return
        self.createExternalizationsTable()

    def _migrateTableSchema(self) -> None:
        """Migrate externalizations table schema to current version.

        Adds missing columns (strategy, node_x, node_y, node_color),
        populates them from existing data, and removes legacy rows.
        """
        table = self.Externalizations
        if not table or table.numRows < 1:
            return

        headers = [table[0, c].val for c in range(table.numCols)]

        migrations = []

        # Migration 1: Add strategy column (v5.0.176+)
        if 'strategy' not in headers:
            type_idx = headers.index('type') if 'type' in headers else 1
            strategy_col = type_idx + 1
            table.insertCol('', strategy_col)
            table[0, strategy_col] = 'strategy'

            # Collect TDN companion rows to remove (iterate backwards)
            rows_to_delete = []
            for i in range(1, table.numRows):
                row_type = table[i, 'type'].val
                rel_path = table[i, 'rel_file_path'].val

                if row_type == 'tdn':
                    rows_to_delete.append(i)
                    continue

                oper = op(table[i, 'path'].val)
                if oper and oper.family == 'COMP':
                    table[i, 'strategy'] = 'tox'
                elif rel_path:
                    ext = rel_path.rsplit('.', 1)[-1] if '.' in rel_path else ''
                    table[i, 'strategy'] = ext
                else:
                    table[i, 'strategy'] = row_type

            for i in reversed(rows_to_delete):
                table.deleteRow(i)

            count = len(rows_to_delete)
            if count:
                migrations.append(f'strategy column (removed {count} legacy TDN row(s))')
            else:
                migrations.append('strategy column')

            # Refresh headers after modification
            headers = [table[0, c].val for c in range(table.numCols)]

        # Migration 2: Add position/color columns (v5.0.189+)
        if 'node_x' not in headers:
            table.appendCol('node_x')
            table.appendCol('node_y')
            table.appendCol('node_color')
            table[0, table.numCols - 3] = 'node_x'
            table[0, table.numCols - 2] = 'node_y'
            table[0, table.numCols - 1] = 'node_color'
            migrations.append('node_x/node_y/node_color columns')

        if migrations:
            self.Log(f'Schema migration: added {", ".join(migrations)}', 'SUCCESS')

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
        # TDN mode migration detection: an upgrading user will have
        # 'Tdnenable' in their persisted params but not 'Tdnmode'. Defer
        # the nudge dialog so init can complete cleanly first.
        # Guarded by a schedule-time flag so a second _restoreSettings in
        # the same session (e.g. onCreate then onStart) can't queue a
        # second dialog before the first one fires.
        already_scheduled = self.my.fetch(
            '_tdn_migration_scheduled', False, search=False)
        if ('Tdnenable' in params and 'Tdnmode' not in params
                and not already_scheduled):
            prev_tdn_enable = bool(params.get('Tdnenable', {}).get('val', True))
            self.my.store('_tdn_migration_prev_enable', prev_tdn_enable)
            self.my.store('_tdn_migration_scheduled', True)
            run(f"op('{self.my}').ext.Embody._showTDNMigrationNudge()",
                delayFrames=60)
        # If Envoyenable was restored to True, kick Start() -- parexec was
        # suppressed during restore so onValueChange never fired.
        # Only set this on the onStart() path (kick_envoy=True).
        # Verify() owns Envoy startup on the onCreate() path.
        if kick_envoy and self.my.par.Envoyenable.eval():
            run(f"op('{self.my}').ext.Envoy.Start()", delayFrames=3)
        return restored > 0

    def _showTDNMigrationNudge(self) -> None:
        """One-time dialog after upgrading from the binary Tdnenable toggle.

        Fires when a user opens a project previously saved with the old
        Tdnenable toggle and no Tdnmode selection yet. Offers a choice
        between restoring Full bidirectional sync (their prior behavior)
        or adopting the new Export-on-Save default (recommended).

        Guarded by _tdn_mode_migration_shown so it only fires once per
        project across sessions (the flag is persisted via param write
        into config.json on next save).
        """
        if self.my.fetch('_tdn_mode_migration_shown', False, search=False):
            return
        prev_enable = self.my.fetch('_tdn_migration_prev_enable', True,
                                    search=False)
        self.my.unstore('_tdn_migration_prev_enable')

        tdn_comps = []
        try:
            tdn_comps = self._getTDNStrategyComps()
        except Exception:
            pass

        if not tdn_comps:
            # No TDN COMPs tracked -- silently accept the new default.
            self.my.store('_tdn_mode_migration_shown', True)
            return

        prev_label = ('Full (bidirectional)' if prev_enable
                      else 'Off (TDN disabled)')
        msg = (
            f'TDN default changed in this release.\n\n'
            f'Your project was previously saved with the legacy Tdnenable '
            f'toggle ({prev_label}). The new system has three modes:\n\n'
            f'  \u2022 Off -- no TDN runtime\n'
            f'  \u2022 Export-on-Save -- recommended; .toe is truth, '
            f'.tdn files are rewritten on save\n'
            f'  \u2022 Roundtrip (Experimental) -- bidirectional '
            f'strip/restore on save and reconstruction on open (previous '
            f'behavior)\n\n'
            f'Currently set to Export-on-Save. Your {len(tdn_comps)} '
            f'tracked TDN COMP(s) will stop round-tripping on save.\n\n'
            f'Keep the new default, or restore Full?'
        )
        choice = self._messageBox(
            'Embody - TDN Mode Changed',
            msg,
            buttons=['Keep Export-on-Save (recommended)',
                     'Restore Full (previous behavior)'])
        if choice == 1:
            try:
                self.my.par.Tdnmode = 'full'
                self._applyTdnModeGating()
                self.Log('TDN mode restored to Full per user choice', 'INFO')
            except Exception as e:
                self.Log(f'Could not restore Full mode: {e}', 'WARNING')
        else:
            self.Log('TDN mode kept at Export-on-Save (new default)', 'INFO')
        self.my.store('_tdn_mode_migration_shown', True)

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
            # Offer a re-scan so Embody validates/updates all tracked operators.
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

    def Disable(self, prevFolder: Union[str, bool, None] = False) -> None:
        """
        Disable Embody: clear external paths and delete tracked files.
        SAFETY: Only deletes files that Embody is tracking - never deletes
        untracked files that may exist in the externalization folder.
        """
        folder = self.ExternalizationsFolder if prevFolder is None else prevFolder
        if prevFolder == '':
            folder = project.folder

        # Collect all tracked file paths BEFORE clearing operator references
        tracked_files = self.getTrackedFilePaths()
        self.Log(f"Disable: Found {len(tracked_files)} tracked file(s) to clean up", "INFO")

        # Clear COMP externalizations
        for oper in self.getExternalizedOps(COMP):
            oper.par.externaltox = ''

        # Clear DAT externalizations
        for oper in self.getExternalizedOps(DAT):
            try:
                oper.par.syncfile = False
                oper.par.file = ''
            except Exception as e:
                self.Log(f"Failed to clear file params on {oper.path}: {e}", "DEBUG")
                pass

        # SAFELY delete only tracked files
        deleted_count = 0
        for tracked_file in tracked_files:
            if tracked_file.is_file():
                try:
                    tracked_file.unlink()
                    deleted_count += 1
                except Exception as e:
                    self.Log(f"Error deleting tracked file: {tracked_file}", "ERROR", str(e))
        
        if deleted_count > 0:
            self.Log(f"Deleted {deleted_count} tracked file(s)", "SUCCESS")

        # Clean up empty directories only (safe operation)
        # SAFETY: Never clean directories outside the externalization folder.
        # When prevFolder is empty, folder falls back to project.folder -- which
        # is far too broad and can delete unrelated empty directories (issue #3).
        if folder and folder != project.folder:
            self._cleanupEmptyDirectories(folder, prevFolder)

        # Clear externalizations table synchronously (no delay -- delayed clear
        # creates a race condition if re-enabled before the callback fires)
        if self.Externalizations:
            self.Externalizations.clear(keepFirstRow=True)

        self.my.par.Status = 'Disabled'

        # Schedule deferred empty-dir cleanup only for the specific externalization
        # folder -- never for project.folder or empty paths (prevents deleting
        # newly-created target folders when changing the Folder parameter).
        if folder and folder != project.folder:
            run(lambda: self.deleteEmptyDirectories(folder), delayFrames=60)

        self.Log("Disabled", "SUCCESS")

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
        if prevFolder and prevFolder != self.getProjectFolder():
            prev_path = Path(prevFolder)
            if prev_path.is_dir() and prev_path != Path(self.getProjectFolder()):
                try:
                    # Only remove if empty - safe operation
                    prev_path.rmdir()
                    self.Log(f"Removed empty previous folder: {prev_path}", "INFO")
                except OSError:
                    # Not empty - preserve it!
                    self.Log(f"Previous folder not empty, preserving: {prev_path}", "INFO")
                except Exception as e:
                    self.Log(f"Error with previous folder: {prev_path}", "ERROR", str(e))

    def DisableHandler(self) -> None:
        """Handle disable button with confirmation dialog."""
        choice = ui.messageBox('Embody Warning',
            'Disable Embody?\nOnly files created by Embody will be deleted.\n'
            '(Non-Embody files in the folder will be preserved)',
            buttons=['No', 'Yes'])
        if choice == 1:
            self.Disable(self.ExternalizationsFolder)

    def UpdateHandler(self) -> None:
        """Enable/Update handler - main entry point for initialization."""
        if self.my.par.Status == 'Disabled':
            self.Log("Enabled", "SUCCESS")
            self.my.par.Status = 'Enabled'
            self.param_tracker.initializeTracking(self)
            
            # Create externalization folder (makedirs handles missing parents)
            folder = self.getProjectFolder()
            try:
                os.makedirs(folder, exist_ok=True)
                self.Log(f"Created folder '{folder}'", "SUCCESS")
            except Exception as e:
                self.Log(f"Failed to create folder '{folder}': {e}", "ERROR")

        # Migrate table schema if needed (adds strategy column)
        self._migrateTableSchema()

        # Normalize paths for cross-platform compatibility
        self.normalizeAllPaths()

        # Apply UI gating for the TDN mode menu (greys out dependent
        # parameters based on Off / Export / Full).
        self._applyTdnModeGating()

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
        # Skip ONLY when Embody is explicitly Disabled. Status takes other
        # transient values during normal operation -- 'Scanning defaults (X/N)'
        # and 'Scanning palette (X/N)' from CatalogManager.EnsureCatalogs(),
        # 'Testing' from EnvoyExt port-test -- and Update must still run during
        # those windows. The previous `!= 'Enabled'` check raced with the
        # catalog scan that fires on fresh-project drops: the scan started
        # one frame before Update was scheduled, set Status to 'Scanning
        # defaults (0/N)', and Update returned early -- never consuming
        # _pending_envoy_prompt, so the Envoy opt-in dialog never appeared.
        if self.my.par.Status == 'Disabled':
            return
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
        # and par.file follow ops through renames. The post-additions
        # _scanAndPopulate() at the tail of this method captures the new state.

        # Check for parameter changes on TOX-strategy COMPs
        for comp in self.getExternalizedOps(COMP, strategy='tox'):
            if self.param_tracker.compareParameters(comp):
                self.Externalizations[comp.path, 'dirty'] = 'Par'
                self.Save(comp.path)

        # Check for parameter or structural changes on TDN-strategy COMPs.
        # Skip root "/" -- it's a Full Project export, not a managed COMP.
        # SaveTDN("/") would trigger root-level stale cleanup that deletes
        # other tracked .tdn files.
        # Guard: when Tdnmode is Off, skip the entire TDN export branch.
        # In Export and Full modes we still run the export.
        # tdn_paths still gets populated below from the table so the
        # "subtractions" filter continues to exclude tracked TDN COMPs.
        tdn_comps = self.getExternalizedOps(COMP, strategy='tdn')
        tdn_paths = {comp.path for comp in tdn_comps}
        if self._tdnEnabled():
            for comp in tdn_comps:
                if comp.path == '/':
                    continue
                par_dirty = self.param_tracker.compareParameters(comp)
                struct_dirty = self._isTDNDirty(comp)
                if par_dirty or struct_dirty:
                    self.Externalizations[comp.path, 'dirty'] = (
                        'Par' if par_dirty else 'True')
                    self.SaveTDN(comp.path)
        elif tdn_comps:
            self.Log(
                f'TDN disabled -- skipping export for {len(tdn_comps)} '
                f'tracked TDN COMP(s)', 'INFO')

        # Check for duplicates
        if self.my.par.Detectduplicatepaths:
            self.checkForDuplicates()

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

        # Subtractions: tracked but par was cleared. TDN-strategy COMPs are
        # excluded -- their lifecycle is managed via RemoveTDNEntry, not
        # par-presence detection. Full Project TDN exports track "/" in
        # the table without setting par.externaltox on the root.
        subtractions = [
            oper for oper in externalized_ops
            if oper.path not in tdn_paths
            and oper.path not in ops_to_externalize_paths
            and not oper.warnings()
            and not oper.scriptErrors()
            and self.isOpProcessable(oper)
        ]

        # Process changes
        additions.sort(key=lambda x: (self.Externalizations.path in x.path, x.path), reverse=True)

        for oper in additions:
            self.handleAddition(oper)
        for oper in subtractions:
            self.handleSubtraction(oper)

        # Handle dirty COMPs (TOX + TDN)
        dirties = self.dirtyHandler(True)

        # Refresh the table view from live par state -- supersedes the
        # legacy .tsv persistence and inline appendRow / deleteRow calls.
        self._scanAndPopulate()

        # Report results
        self._reportResults(dirties, additions, subtractions)
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

        if self.my.par.Detectduplicatepaths:
            self.checkForDuplicates()

        self.Debug("Refreshed")
        
        if not me.time.play:
            self.Log("ALERT! TIMELINE IS PAUSED. RESUME FOR EMBODY TO FUNCTION", "ERROR")

    # ==========================================================================
    # OPERATOR QUERIES
    # ==========================================================================

    def getExternalizedOps(self, opFamily: type, strategy: Optional[str] = None) -> list[OP]:
        """Get all externalized operators of a given family from the table.

        Args:
            opFamily: COMP or DAT
            strategy: Optional filter -- 'tox', 'tdn', or None for all.
        """
        if not self.Externalizations:
            return []

        family_str = 'COMP' if opFamily == COMP else 'DAT'
        has_strategy_col = 'strategy' in [
            self.Externalizations[0, c].val
            for c in range(self.Externalizations.numCols)
        ]
        ops = []

        for i in range(1, self.Externalizations.numRows):
            # Filter by strategy if requested
            if has_strategy_col and strategy:
                row_strategy = self.Externalizations[i, 'strategy'].val
                if row_strategy != strategy:
                    continue
            elif not has_strategy_col:
                # Legacy table without strategy column -- skip TDN rows
                if self.Externalizations[i, 'type'].val == 'tdn':
                    continue

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
        if opFamily == COMP:
            return self.root.findChildren(
                type=COMP,
                key=lambda x: (
                    x.par.externaltox.eval() != '' and
                    self.isOpProcessable(x)
                )
            )
        else:
            return self.root.findChildren(
                type=DAT,
                parName='file',
                key=lambda x: (
                    x.par.file.eval() != '' and
                    x.type in self.supported_dat_types and
                    self.isOpProcessable(x)
                )
            )

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
        """Save a TOX-strategy COMP and update tracking."""
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

            self.Log(f"Saved {opPath}", "SUCCESS")
        except Exception as e:
            self.Log("Save failed", "ERROR", str(e))

    def SaveTDN(self, opPath: str) -> None:
        """Save a TDN-strategy COMP by re-exporting its .tdn file."""
        if self._performMode:
            return
        if not self._tdnEnabled():
            self.Log(f'TDN disabled -- skipping SaveTDN for {opPath}', 'INFO')
            return
        try:
            oper = op(opPath)
            if not oper:
                self.Log(f"Operator not found: {opPath}", "ERROR")
                return

            # Get the TDN file path from the table
            rel_path = self._getStrategyFilePath(opPath, 'tdn')
            if not rel_path:
                self.Log(f"No TDN entry found for {opPath}", "ERROR")
                return

            # For root /, re-derive filename from current project name
            # so it stays in sync when the .toe is renamed/versioned
            if opPath == '/':
                from pathlib import Path
                raw_name = project.name.removesuffix('.toe')
                safe_name = self.my.ext.TDN._stripBuildSuffix(raw_name)
                ext_folder = self.ExternalizationsFolder or ''
                new_rel = self.normalizePath(
                    str(Path(ext_folder) / f'{safe_name}.tdn'))
                if new_rel != rel_path:
                    old_abs = self.buildAbsolutePath(rel_path)
                    if old_abs.is_file():
                        self.safeDeleteFile(str(old_abs))
                    rel_path = new_rel
                    self.Externalizations[opPath, 'rel_file_path'] = rel_path
                    self.Log(f"Updated root TDN path: {rel_path}", "INFO")

            # Build/Date/Touchbuild auto-injection is disabled (see
            # setupBuildParameters). When re-enabled behind a setting, the
            # bump-on-SaveTDN logic moves back here.

            # Export TDN -- protect .tdn files belonging to OTHER tracked
            # TDN COMPs so the stale-file cleanup doesn't delete them.
            abs_path = str(self.buildAbsolutePath(rel_path))
            protected = self._getAllTrackedTDNFiles(exclude_path=opPath)
            result = self.my.ext.TDN.ExportNetwork(
                root_path=opPath, output_file=abs_path,
                cleanup_protected=protected)

            if result.get('success'):
                timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                self.Externalizations[opPath, 'timestamp'] = timestamp
                self.param_tracker.updateParamStore(oper)
                self.Externalizations[opPath, 'dirty'] = ''
                # Refresh position/color metadata
                self._updatePositionInTable(oper, opPath)
                # Snapshot the network structure so _isTDNDirty returns False
                self._storeTDNFingerprint(oper)
                self.Log(f"Exported TDN for {opPath}", "SUCCESS")
            else:
                self.Log(f"TDN export failed for {opPath}: {result.get('error')}", "ERROR")
        except Exception as e:
            self.Log(f"SaveTDN failed for {opPath}", "ERROR", str(e))

    def ExportPortableTox(self, target: 'OP' = None,
                          save_path: Optional[str] = None) -> bool:
        """Export a self-contained .tox with all external file references
        and Embody tags stripped.

        Temporarily strips file, syncfile, and externaltox parameters plus
        all Embody tags from all descendants of the target COMP, saves the
        .tox, then restores everything. The resulting .tox has no external
        file dependencies and no Embody metadata.

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

        # Phase 1b: Embody no longer applies tags to operators (par-driven
        # discovery replaced the tag system), so no tag-strip step is needed.

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
            chosen = ui.chooseFile(
                load=False,
                fileTypes=['toe'],
                title='Release Project - choose .toe path',
                start=str(Path(project.folder).parent),
            )
            if not chosen:
                return {'error': 'cancelled'}
            save_path = str(chosen)

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

        prev_folder = ''
        try:
            prev_folder = str(self.my.par.Folder.eval())
        except Exception:
            pass

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

            # Clear Embody's Folder so the released .toe doesn't try to
            # re-externalize on next open. The Embody COMP stays in place
            # (user can re-enable by setting the Folder back), but it's
            # inert by default.
            try:
                self.my.par.Folder = ''
            except Exception:
                pass

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
            try:
                if prev_folder:
                    self.my.par.Folder = prev_folder
            except Exception:
                pass
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
        # All annotations (utility=True or False) -- uses annotation-specific attrs
        for ann in sorted(comp.findChildren(type=annotateCOMP, depth=1,
                                            includeUtility=True),
                          key=lambda a: a.name):
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
        return tuple(parts)

    def _getTDNPaths(self) -> set:
        """Return the set of all TDN-externalized COMP paths."""
        return {path for path, _ in self._getTDNStrategyComps()}

    def _isTDNDirty(self, comp) -> bool:
        """Check if a TDN COMP's network has changed since last export."""
        tdn_paths = self._getTDNPaths()
        current = self._computeTDNFingerprint(comp, tdn_paths)
        stored = self._tdn_fingerprints.get(comp.path)
        if stored is None:
            # No stored fingerprint -- assume clean (just initialized)
            self._tdn_fingerprints[comp.path] = current
            return False
        return current != stored

    def _storeTDNFingerprint(self, comp) -> None:
        """Snapshot the TDN COMP's network structure after export."""
        tdn_paths = self._getTDNPaths()
        self._tdn_fingerprints[comp.path] = self._computeTDNFingerprint(
            comp, tdn_paths)

    def _getStrategyFilePath(self, op_path: str, strategy: str) -> Optional[str]:
        """Return the rel_file_path for a given operator + strategy, or None."""
        table = self.Externalizations
        if not table:
            return None
        has_strategy_col = table[0, 'strategy'] is not None
        for i in range(1, table.numRows):
            if table[i, 'path'].val == op_path:
                if has_strategy_col and table[i, 'strategy'].val == strategy:
                    return table[i, 'rel_file_path'].val
                elif not has_strategy_col:
                    return table[i, 'rel_file_path'].val
        return None

    def _getAllTrackedTDNFiles(self, exclude_path: Optional[str] = None) -> list[str]:
        """Collect absolute paths of ALL .tdn files Embody is responsible for.

        Used as the "protected" list for stale-file cleanup during a single-
        COMP TDN export so we don't delete sibling sidecars. Under the
        par-driven model, every externalized COMP gets a .tdn sidecar via
        Phase 2's _writeTdnSidecar, so the protected set is the union of:

        - Legacy strategy='tdn' rows in the externalizations table (rare
          now, but kept for back-compat with .toes that still hold them)
        - The .tdn path of every COMP with par.externaltox set
          (computed via _buildTDNRelPath)

        Without this union, every save of a single COMP's .tdn would
        delete every other COMP's .tdn -- the Phase 2 always-both design
        creates many sidecars in the same folder and they all need
        protection from each other.

        Args:
            exclude_path: Skip this op_path (the one being exported).
        """
        protected: list[str] = []
        seen: set[str] = set()

        # Source 1: legacy strategy='tdn' table entries.
        table = self.Externalizations
        if table and table[0, 'strategy'] is not None:
            for i in range(1, table.numRows):
                if table[i, 'strategy'].val != 'tdn':
                    continue
                path = table[i, 'path'].val
                if path == exclude_path:
                    continue
                rel = table[i, 'rel_file_path'].val
                if not rel:
                    continue
                abs_path = str(self.buildAbsolutePath(rel))
                if abs_path not in seen:
                    seen.add(abs_path)
                    protected.append(abs_path)

        # Source 2: par-driven COMP sidecars. Every par.externaltox-set
        # COMP gets a .tdn sidecar at _buildTDNRelPath(comp). Add them all
        # so a single-COMP export doesn't wipe its siblings' sidecars.
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

    def _getCompStrategy(self, comp: OP) -> Optional[str]:
        """Determine if a COMP uses 'tox' or 'tdn' strategy from the table."""
        table = self.Externalizations
        if not table:
            return None
        if table[0, 'strategy'] is None:
            return 'tox'  # Legacy table without strategy column
        for i in range(1, table.numRows):
            if table[i, 'path'].val == comp.path:
                s = table[i, 'strategy'].val
                if s in ('tox', 'tdn'):
                    return s
        return None

    def SaveCurrentComp(self) -> None:
        """Update only the COMP we're currently working inside of (Ctrl/Cmd+Alt+U)."""
        if self._performMode:
            return
        current_comp = None
        
        try:
            pane = ui.panes.current
            if pane and pane.owner:
                current_comp = pane.owner
        except Exception as e:
            self.Log(f"Failed to get current pane: {e}", "DEBUG")
            pass
        
        if not current_comp:
            self.Log("Could not determine current COMP", "WARNING")
            return
        
        # Check if this COMP is externalized
        comp_path = current_comp.path
        match = self._findExternalizedComp(comp_path)
        if match:
            self._saveByStrategy(*match)
            return

        # Check if any parent is externalized
        parent_comp = current_comp.parent()
        while parent_comp:
            match = self._findExternalizedComp(parent_comp.path)
            if match:
                self._saveByStrategy(*match)
                return
            parent_comp = parent_comp.parent()

        self.Log(f"No externalized COMP found at or above '{comp_path}'", "WARNING")

    def _findExternalizedComp(self, comp_path: str) -> Optional[tuple[str, str]]:
        """Find a COMP in the externalizations table and return (path, strategy)."""
        has_strategy_col = self.Externalizations[0, 'strategy'] is not None
        for i in range(1, self.Externalizations.numRows):
            if self.Externalizations[i, 'path'].val == comp_path:
                if has_strategy_col:
                    s = self.Externalizations[i, 'strategy'].val
                    if s in ('tox', 'tdn'):
                        return (comp_path, s)
                else:
                    return (comp_path, 'tox')
        return None

    def _saveByStrategy(self, op_path: str, strategy: str) -> None:
        """Save a COMP using the appropriate strategy."""
        if strategy == 'tdn':
            self.SaveTDN(op_path)
        else:
            self.Save(op_path)

    def dirtyHandler(self, update: bool) -> list[str]:
        """Check and optionally update dirty COMPs (both TOX and TDN)."""
        updates = []

        # TOX-strategy COMPs
        for oper in self.getExternalizedOps(COMP, strategy='tox'):
            dirty = oper.dirty
            try:
                # Preserve 'Par' dirty state when oper.dirty is False --
                # parameter changes are tracked independently from TD's
                # native dirty flag and should only be cleared on Save.
                if dirty or str(self.Externalizations[oper.path, 'dirty'].val) != 'Par':
                    self.Externalizations[oper.path, 'dirty'] = dirty
            except Exception as e:
                self.Log(f"Failed to update dirty state for {oper.path}: {e}", "DEBUG")
            if dirty and update:
                self.Save(oper.path)
                updates.append(oper.path)

        # TDN-strategy COMPs -- use network fingerprint instead of oper.dirty
        # (oper.dirty is always True when externaltox is empty)
        for oper in self.getExternalizedOps(COMP, strategy='tdn'):
            dirty = self._isTDNDirty(oper)
            if dirty:
                try:
                    self.Externalizations[oper.path, 'dirty'] = 'True'
                except Exception as e:
                    self.Log(f"Failed to update dirty state for {oper.path}: {e}", "DEBUG")
                if update:
                    self.SaveTDN(oper.path)
                    updates.append(oper.path)

        return updates

    def updateDirtyStates(self, externalizationsFolder: str) -> None:
        """Update dirty states and check for path/parameter changes."""
        dirties = self.dirtyHandler(False)
        param_changes = []

        for oper in self.getExternalizedOps(COMP) + self.getExternalizedOps(DAT):
            # TDN-strategy COMPs don't use externaltox -- their rel_file_path
            # tracks the .tdn sidecar. Skip them here to avoid overwriting
            # the .tdn path with "".
            if oper.family == 'COMP' and self._getCompStrategy(oper) == 'tdn':
                if self.param_tracker.compareParameters(oper):
                    param_changes.append(oper.path)
                    self.Externalizations[oper.path, 'dirty'] = 'Par'
                continue

            current_path = self.getExternalPath(oper)
            try:
                table_path = self.normalizePath(self.Externalizations[oper.path, 'rel_file_path'].val)
                if current_path != table_path:
                    self.Externalizations[oper.path, 'rel_file_path'] = current_path
                    if oper.family == 'COMP':
                        oper.par.externaltox.readOnly = True
                    else:
                        oper.par.file.readOnly = True
                    self.Log(f"Updated path for {oper.path}", "SUCCESS")
            except Exception as e:
                self.Log(f"Failed to update path for {oper.path}: {e}", "WARNING")
                pass
            
            if oper.family == 'COMP' and self.param_tracker.compareParameters(oper):
                param_changes.append(oper.path)
                self.Externalizations[oper.path, 'dirty'] = 'Par'

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

        Every COMP gets both .tox (canonical) and .tdn (diffable sidecar).
        DATs are written based on their par.file extension. Routing by
        tag is gone -- discovery is purely par-driven.
        """
        abs_folder_path, save_file_path, rel_directory, rel_file_path = \
            self.getOpPaths(oper, self.my.par.Folder.val)

        if save_file_path is None:
            self.Log(f"Could not generate paths for {oper.path}", "ERROR")
            return

        # Create directory
        try:
            Path(abs_folder_path).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.Log(f"Error creating directory {abs_folder_path}", "ERROR", str(e))

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        dirty = ''
        build_num = ''
        touch_build = ''
        strategy = ''

        if oper.family == 'COMP':
            strategy = 'tox'
            self._setupCompForExternalization(oper, rel_file_path, save_file_path)
            # Always-both: write the .tdn sidecar alongside the freshly-saved .tox.
            self._writeTdnSidecar(oper)
            dirty = oper.dirty
            build_num = int(oper.par.Build.eval()) if hasattr(oper.par, 'Build') else 1
            touch_build = str(oper.par.Touchbuild.eval()) if hasattr(oper.par, 'Touchbuild') else app.build
            self.param_tracker.updateParamStore(oper)
        else:  # DAT
            ext = str(save_file_path).rsplit('.', 1)[-1] if '.' in str(save_file_path) else ''
            strategy = ext
            self._setupDatForExternalization(oper, rel_file_path, save_file_path)

        # Table mutation is no longer done inline -- the table is rebuilt
        # from a live par-driven scan via _scanAndPopulate() at end of Update.
        self.Log(f"Added '{oper.path}'", "SUCCESS")

    def _buildTDNRelPath(self, oper: OP) -> Path:
        """Generate a flat relative .tdn file path for a COMP.

        Mirrors getOpPaths() flat layout: {folder}/{op.name}.tdn.
        """
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
        """Configure a COMP for TOX externalization."""
        # Setup build info page
        build_page = next((p for p in oper.customPages if p.name == 'Build Info'), None)
        if not build_page:
            build_page = oper.appendCustomPage('About')
        
        current_build = 1
        if hasattr(oper.par, 'Build'):
            current_build = oper.par.Build.eval()
        else:
            for row in range(1, self.Externalizations.numRows):
                if self.Externalizations[row, 'path'].val == oper.path:
                    try:
                        current_build = int(self.Externalizations[row, 'build'].val)
                    except (ValueError, TypeError) as e:
                        self.Log(f"Failed to parse build number for {oper.path}: {e}", "DEBUG")
                        pass
                    break
        
        self.setupBuildParameters(oper, build_page, current_build, app.build)

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

        oper.par.externaltox.readOnly = True
        oper.par.enableexternaltox = True
        
        # Save file
        save_path_str = str(save_file_path)
        try:
            oper.save(save_path_str)
        except Exception as e:
            self.Log(f"Failed to save COMP {oper.path}", "ERROR", f"Path: {save_path_str}, Error: {e}")

        if "Cannot load external tox from path" in oper.scriptErrors():
            oper.allowCooking = False
            run(lambda: self._safeAllowCooking(str(oper), True), delayFrames=1)

    def _setupDatForExternalization(self, oper, rel_file_path, save_file_path):
        """Configure a DAT for externalization."""
        if not oper.par.file.eval():
            oper.par.file = str(rel_file_path)
        else:
            oper.par.file = self.normalizePath(oper.par.file.eval())
        
        oper.par.syncfile = True
        op_path = str(oper)
        run(lambda: self._safeSyncFile(op_path, False), delayFrames=1)
        run(lambda: self._safeSyncFile(op_path, True), delayFrames=2)
        oper.par.file.readOnly = True
        
        save_path_str = str(save_file_path)
        try:
            oper.save(save_path_str)
        except Exception as e:
            self.Log(f"Failed to save DAT {oper.path}", "ERROR", f"Path: {save_path_str}, Error: {e}")

    def _addToTable(self, oper, rel_file_path, timestamp, dirty,
                     build_num, touch_build, strategy: str = ''):
        """Add or update operator entry in externalizations table."""
        normalized_path = self.normalizePath(rel_file_path)

        has_strategy_col = self.Externalizations[0, 'strategy'] is not None
        has_position_cols = self.Externalizations[0, 'node_x'] is not None

        # Build position/color strings from the operator
        node_x = str(int(oper.nodeX)) if has_position_cols else ''
        node_y = str(int(oper.nodeY)) if has_position_cols else ''
        node_color = ''
        if has_position_cols:
            c = oper.color
            node_color = f'{c[0]:.4f},{c[1]:.4f},{c[2]:.4f}'

        # Check if row already exists for this operator + strategy
        for row in range(1, self.Externalizations.numRows):
            if self.Externalizations[row, 'path'] == oper.path:
                if has_strategy_col:
                    row_strategy = self.Externalizations[row, 'strategy'].val
                    if row_strategy != strategy:
                        continue
                self.Externalizations[row, 'rel_file_path'] = normalized_path
                # Update position/color on existing rows too
                if has_position_cols:
                    self.Externalizations[row, 'node_x'] = node_x
                    self.Externalizations[row, 'node_y'] = node_y
                    self.Externalizations[row, 'node_color'] = node_color
                return

        # Add new row
        if has_strategy_col:
            row_data = [
                oper.path, oper.type, strategy, normalized_path, timestamp,
                dirty, build_num, touch_build
            ]
            if has_position_cols:
                row_data.extend([node_x, node_y, node_color])
            self.Externalizations.appendRow(row_data)
        else:
            self.Externalizations.appendRow([
                oper.path, oper.type, normalized_path, timestamp,
                dirty, build_num, touch_build
            ])

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
            if oper.family == 'COMP':
                strategy = 'tox'
                rel = oper.par.externaltox.eval()
                dirty = bool(oper.dirty)
            else:
                rel = oper.par.file.eval()
                strategy = rel.rsplit('.', 1)[-1] if '.' in rel else ''
                dirty = ''
            self._addToTable(
                oper, rel, timestamp, dirty,
                build_num='', touch_build='', strategy=strategy,
            )

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
    # DUPLICATE HANDLING
    # ==========================================================================

    def _buildPathGroups(self) -> dict:
        """Map normalized external paths to lists of operators sharing them.

        Includes par-set externalizations only. Excludes clones, replicants.
        """
        path_groups = {}

        for oper in self.root.findChildren(type=COMP, parName='externaltox'):
            if not oper.par.externaltox.eval():
                continue
            if self.isInsideClone(oper) or self.isReplicant(oper):
                continue
            path = self.normalizePath(oper.par.externaltox.eval())
            if path:
                path_groups.setdefault(path, []).append(oper)

        for oper in self.root.findChildren(type=DAT, parName='file'):
            if not oper.par.file.eval():
                continue
            if oper.type not in self.supported_dat_types:
                continue
            if self.isInsideClone(oper) or self.isReplicant(oper):
                continue
            path = self.normalizePath(oper.par.file.eval())
            if path:
                path_groups.setdefault(path, []).append(oper)

        return path_groups

    def checkForDuplicates(self) -> None:
        """Check for and handle duplicate external file paths.

        Groups all operators sharing the same external path, then:
        - For replicants: auto-tags all replicants (master is the template)
        - For COMPs with TD clone relationships: auto-tags clones
        - For DATs inside cloned COMPs: auto-tags DATs in clone COMPs
        - For others: collects unresolved groups. When 2+ groups
          remain, offers a single batch prompt (auto-resolve all /
          review individually / skip); a single group goes straight
          to the per-group prompt.
        """
        unresolved = []
        for path, ops in self._buildPathGroups().items():
            if len(ops) < 2:
                continue
            if any('clone' in o.tags for o in ops):
                continue
            if self._resolveReplicants(ops):
                continue
            if self._resolveClonesByCloningAPI(ops):
                continue
            if self._resolveDATsInClonedCOMPs(ops):
                continue
            unresolved.append((path, ops))

        if not unresolved:
            return

        if len(unresolved) == 1:
            path, ops = unresolved[0]
            self._promptForDuplicateGroup(path, ops)
            return

        choice = self._promptForBatchResolution(unresolved)
        if choice == 'dismiss':
            return
        if choice == 'auto':
            for path, ops in unresolved:
                self._autoResolveFirstAsMaster(path, ops)
            return
        for path, ops in unresolved:
            self._promptForDuplicateGroup(path, ops)

    def _resolveClonesByCloningAPI(self, ops: list) -> bool:
        """Try to resolve master/clone using TD's native clone API.

        Returns True if resolution succeeded (all clones tagged),
        False if the API doesn't apply (DATs, or COMPs without
        clone relationships).
        """
        if not all(o.family == 'COMP' for o in ops):
            return False

        master = None
        ops_set = set(ops)

        # Check .clones property -- master is the op whose clones overlap
        for o in ops:
            try:
                clones = o.clones
                if clones and ops_set.intersection(clones):
                    master = o
                    break
            except Exception:
                pass

        # Fallback: check par.clone -- it points FROM clone TO master
        if not master:
            for o in ops:
                clone_ref = o.par.clone.eval()
                if clone_ref and clone_ref in ops_set and clone_ref is not o:
                    master = clone_ref
                    break

        if not master:
            return False

        for o in ops:
            if o is not master:
                self._handleDuplicateAsReference(o)

        self.Log(
            f"Auto-resolved clone master '{master.path}' for path "
            f"shared by {len(ops)} operators", "SUCCESS")
        return True

    def _resolveDATsInClonedCOMPs(self, ops: list) -> bool:
        """Auto-resolve DATs inside cloned COMPs.

        When DATs share an external path and their ancestor COMPs are in
        a clone relationship, auto-tag DATs inside clone COMPs.

        Returns True if resolution succeeded, False if not applicable.
        """
        if not all(o.family == 'DAT' for o in ops):
            return False

        masters = []
        clones = []
        for dat in ops:
            if self.isInsideClone(dat):
                clones.append(dat)
            else:
                masters.append(dat)

        if not masters or not clones:
            return False

        for dat in clones:
            self._handleDuplicateAsReference(dat)

        self.Log(
            f"Auto-resolved {len(clones)} DAT{'s' if len(clones) > 1 else ''} "
            f"inside cloned COMPs (master: "
            f"{', '.join(d.path for d in masters)})", "SUCCESS")
        return True

    def _resolveReplicants(self, ops: list) -> bool:
        """Auto-resolve replicant groups without prompting.

        If any op in the group is a replicant (has a replicator ancestor),
        tag all replicants as clones. The non-replicant op (if any) is
        treated as master.

        Returns True if any replicants were found and tagged.
        """
        replicants = [o for o in ops if self.isReplicant(o)]
        if not replicants:
            return False

        for o in replicants:
            self._handleDuplicateAsReference(o)

        non_replicants = len(ops) - len(replicants)
        self.Log(
            f"Auto-tagged {len(replicants)} replicant{'s' if len(replicants) != 1 else ''} "
            f"as clones ({non_replicants} master{'s' if non_replicants != 1 else ''} retained)",
            "SUCCESS")
        return True

    def _promptForDuplicateGroup(self, path: str, ops: list) -> None:
        """Show a single dialog for a group of operators sharing the same path.

        The user picks which operator is the master; all others get
        clone tags. Dismiss skips without tagging (will re-prompt on
        next cycle).
        """
        op_list = '\n'.join(
            f"  {i+1}. {o.path} ({o.family})" for i, o in enumerate(ops))
        buttons = ['Dismiss'] + [o.name for o in ops]

        choice = self._messageBox(
            'Duplicate Path Detected',
            f"Multiple operators share the external path:\n"
            f"  {path}\n\n"
            f"Operators:\n{op_list}\n\n"
            f"Select the MASTER (others will be tagged as clones).\n"
            f"'Dismiss' to skip for now.",
            buttons=buttons)

        if choice == 0:
            return

        master_idx = choice - 1
        if 0 <= master_idx < len(ops):
            for i, o in enumerate(ops):
                if i != master_idx:
                    self._handleDuplicateAsReference(o)
            self.Log(
                f"User selected '{ops[master_idx].path}' as master "
                f"for '{path}'", "SUCCESS")

    def _promptForBatchResolution(self, unresolved: list) -> str:
        """Ask how to handle multiple unresolved duplicate groups.

        Returns 'dismiss', 'review', or 'auto'.
        """
        n = len(unresolved)
        preview_limit = 5
        preview_lines = [f"  - {path}" for path, _ in unresolved[:preview_limit]]
        if n > preview_limit:
            preview_lines.append(f"  ... and {n - preview_limit} more")
        preview = '\n'.join(preview_lines)

        choice = self._messageBox(
            'Duplicate Paths Detected',
            f"{n} groups of operators share external file paths:\n\n"
            f"{preview}\n\n"
            f"How would you like to resolve them?\n\n"
            f"  * Auto-resolve all: in each group, keep the first\n"
            f"    listed operator as master; tag the rest as clones.\n"
            f"  * Review individually: prompt once per group.\n"
            f"  * Dismiss: skip for now (will re-prompt next cycle).",
            buttons=['Dismiss', 'Review individually',
                     f'Auto-resolve all ({n})'])

        if choice == 0:
            return 'dismiss'
        if choice == 1:
            return 'review'
        return 'auto'

    def _autoResolveFirstAsMaster(self, path: str, ops: list) -> None:
        """Tag all but the first op in the group as clones.

        Applied when the user opts into batch resolution. Matches the
        common case where the first-listed operator is the desired
        master and the rest are copy-paste or drag-in duplicates.
        """
        if not ops:
            return
        master = ops[0]
        clones = ops[1:]
        for o in clones:
            self._handleDuplicateAsReference(o)
        plural = 's' if len(clones) != 1 else ''
        self.Log(
            f"Auto-resolved '{master.path}' as master for '{path}' "
            f"({len(clones)} clone{plural})", "SUCCESS")

    def _handleDuplicateAsReference(self, oper):
        """Mark duplicate as intentional clone reference.

        Adds the TD-native 'clone' tag so the duplicate-detection scan
        can skip this op on future runs. Table mutation is no longer
        done here -- _scanAndPopulate() at end of Update rebuilds the
        table view.
        """
        oper.tags.add('clone')
        self.Log(f"Added 'clone' tag to {oper.path}", "SUCCESS")


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
            except Exception as e:
                self.Log(f'Failed to set par.file on {oper.path}: {e}', 'WARNING')

        # Process COMPs -- assign par.externaltox so Update picks them up
        for oper in self.root.findChildren(type=COMP, parName='externaltox'):
            if self._shouldSkipOp(oper, paths_to_exclude):
                continue
            if oper.par.externaltox.eval():
                continue  # already externalized
            try:
                oper.par.externaltox.readOnly = False
                oper.par.externaltox = f"{folder}/{oper.name}.tox" if folder else f"{oper.name}.tox"
            except Exception as e:
                self.Log(f'Failed to set par.externaltox on {oper.path}: {e}', 'WARNING')

        self.UpdateHandler()

        # Export project-wide TDN snapshot if requested
        if export_project_tdn:
            self.my.ext.TDN.ExportNetworkAsync(
                output_file='auto', embed_all=True)

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
        is_clone = False
        
        try:
            oper = op(op_path)
            if oper:
                if 'clone' in oper.tags:
                    is_clone = True
                    self.Log(f"Skipping file deletion for clone: {op_path}", "INFO")

                # Clear parameters
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

        # Delete file only if:
        # 1. delete_file is True (caller wants file removed)
        # 2. It's not a clone reference
        # 3. No other operators reference it
        # 4. It's a file we're tracking (implicit - we got rel_file_path from our table)
        if delete_file and normalized_path and not other_references and not is_clone:
            full_path = self.buildAbsolutePath(normalized_path).resolve()
            
            def _do_delete():
                try:
                    if full_path.is_file():
                        full_path.unlink()

                        # Clean up empty parent directories
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
                    else:
                        self.Log(f"No file found: {normalized_path}", "WARNING")
                except Exception as e:
                    self.Log(f"Error removing file", "ERROR", str(e))

            run(_do_delete, delayFrames=5)
        elif is_clone or other_references:
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

    def RemoveTDNEntry(self, op_path: str) -> None:
        """Remove a TDN-strategy entry and delete the .tdn file from disk.

        Par-driven: clears par.externaltox on the target COMP (which also
        removes its .tdn sidecar association) and deletes the .tdn file
        on disk. _scanAndPopulate() on the next Update will remove the
        row from the in-memory table.
        """
        target = op(op_path)
        if not target:
            self.Log(f"RemoveTDNEntry: operator not found at {op_path}", "WARNING")
            return
        rel_tdn = self._buildTDNRelPath(target)
        try:
            tdn_abs = self.buildAbsolutePath(rel_tdn)
            if tdn_abs.is_file():
                tdn_abs.unlink()
                self.Log(f"Removed {rel_tdn}", "SUCCESS")
        except Exception as e:
            self.Log(f"Failed to remove {rel_tdn}: {e}", "WARNING")
        if target.family == 'COMP':
            try:
                target.par.externaltox.readOnly = False
                target.par.externaltox = ''
                target.par.enableexternaltox = False
            except Exception as e:
                self.Log(f"Failed to clear externaltox on {op_path}: {e}", "WARNING")
        self.lister.reset()

    # ==========================================================================
    # TDN RECONSTRUCTION ON START
    # ==========================================================================

    def ReconstructTDNComps(self) -> None:
        """Reconstruct all TDN-strategy COMPs from .tdn files on project open."""
        mode = self._tdnMode()
        if mode == 'off':
            self.Log('TDN mode=off -- skipping reconstruction', 'INFO')
            return
        if mode == 'export':
            self.Log('TDN mode=export -- .toe is source of truth, skipping '
                     'reconstruction', 'INFO')
            return
        # mode == 'full'
        if not self.my.par.Tdncreateonstart.eval():
            return

        tdn_comps = self._getTDNStrategyComps()
        if not tdn_comps:
            return

        self.Log(f'Reconstructing {len(tdn_comps)} TDN COMP(s)...', 'INFO')
        errors_total = 0

        for comp_path, rel_tdn_path in tdn_comps:
            abs_path = self.buildAbsolutePath(rel_tdn_path)
            if not abs_path.is_file():
                self.Log(f'TDN file not found: {rel_tdn_path}', 'WARNING')
                continue

            try:
                import json
                tdn_doc = json.loads(abs_path.read_text(encoding='utf-8'))
            except Exception as e:
                self.Log(f'Failed to read TDN for {comp_path}: {e}', 'ERROR')
                errors_total += 1
                continue

            comp = op(comp_path)
            if comp is None:
                # COMP was tagged but .toe wasn't saved -- create the shell.
                # Prefer type from TDN file (v1.1+), then table, then 'base'.
                tdn_type = tdn_doc.get('type')
                comp = self._createMissingCompShell(
                    comp_path, 'tdn', comp_type_override=tdn_type)
                if comp is None:
                    errors_total += 1
                    continue

            # Import from TDN (phases 1-7 + phase 8 file-link restore)
            result = self.my.ext.TDN.ImportNetwork(
                target_path=comp_path,
                tdn=tdn_doc,
                clear_first=True,
                restore_file_links=True,
            )

            if result.get('error'):
                self.Log(f'Reconstruction failed for {comp_path}: {result["error"]}', 'ERROR')
                # Attempt rollback from backup .tdn
                try:
                    backup_path = self.my.ext.TDN._get_backup_path_instance(
                        str(abs_path))
                    if backup_path.is_file():
                        import json as _json
                        backup_tdn = _json.loads(
                            backup_path.read_text(encoding='utf-8'))
                        rb_result = self.my.ext.TDN.ImportNetwork(
                            target_path=comp_path, tdn=backup_tdn,
                            clear_first=True, restore_file_links=True)
                        if rb_result.get('success'):
                            self.Log(
                                f'Rolled back {comp_path} from backup',
                                'WARNING')
                            continue
                        else:
                            self.Log(
                                f'Rollback failed for {comp_path}: '
                                f'{rb_result.get("error")}', 'ERROR')
                except Exception as rb_e:
                    self.Log(
                        f'Rollback error for {comp_path}: {rb_e}', 'ERROR')
                errors_total += 1
                continue

            created = result.get('created_count', 0)
            restored = result.get('restored_file_links', 0)
            msg = f'Reconstructed {comp_path} ({created} ops'
            if restored:
                msg += f', {restored} file links'
            msg += ')'
            self.Log(msg, 'SUCCESS')

            # Phase E: Post-reconstruction error checking
            comp_errors = self._verifyReconstructedComp(comp)
            if comp_errors:
                errors_total += len(comp_errors)

        # Build report
        self._logReconstructionReport(tdn_comps, errors_total)

    # Params visible only in 'full' mode (strip/reconstruction concepts).
    _TDN_FULL_ONLY_PARAMS = {'Tdnstriponsave', 'Tdncreateonstart'}

    def _tdnMode(self) -> str:
        """Return 'off' | 'export' | 'full' from Tdnmode menu.

        Defaults to 'export' if the parameter is missing (legacy .tox).
        """
        par = getattr(self.my.par, 'Tdnmode', None)
        if par is None:
            return 'export'
        try:
            val = par.eval()
            return val if val in ('off', 'export', 'full') else 'export'
        except Exception:
            return 'export'

    def _tdnEnabled(self) -> bool:
        """Return True if the TDN subsystem is NOT in Off mode.

        Thin wrapper for call sites that only need to know whether any
        TDN runtime behavior should fire (export OR strip). Callers that
        need to distinguish export vs full should use _tdnMode().
        """
        return self._tdnMode() != 'off'

    # ==========================================================================
    # PERFORM MODE
    # ==========================================================================

    @property
    def _performMode(self) -> bool:
        """True when Perform Mode is active -- all compute suppressed."""
        par = getattr(self.my.par, 'Performmode', None)
        return bool(par.eval()) if par is not None else False

    def _enterPerformMode(self) -> None:
        """Suspend all Embody features for live performance."""
        # Snapshot state so we can restore on exit
        state = {
            'envoy_was_running': bool(self.my.fetch('envoy_running', False, search=False)),
            'kb_active': self.my.op('keyboardin1').par.active.eval(),
            'exit_tagger_active': self.my.op('chopexec_exit_tagger').par.active.eval(),
        }
        self.my.store('_perform_state', state)

        # Stop Envoy directly (do NOT touch Envoyenable -- that would corrupt config.json)
        self.my.ext.Envoy.Stop()

        # Disable keyboard shortcuts and exit tagger
        self.my.op('keyboardin1').par.active = False
        self.my.op('chopexec_exit_tagger').par.active = False

        # Close manager window if open
        self.my.op('window_manager').par.winclose.pulse()

        # Update status display
        self.my.par.Envoystatus = 'Perform Mode'

        # Grey out Envoy parameters so user sees they're frozen
        for p in ('Envoyenable', 'Envoyport', 'Aiclient'):
            par = getattr(self.my.par, p, None)
            if par is not None:
                par.enable = False

        self.Log('Perform Mode ON -- features suspended', 'INFO')

    def _exitPerformMode(self) -> None:
        """Restore all Embody features after live performance."""
        state = self.my.fetch('_perform_state', {}, search=False)

        # Re-enable keyboard shortcuts and exit tagger
        self.my.op('keyboardin1').par.active = state.get('kb_active', True)
        self.my.op('chopexec_exit_tagger').par.active = state.get('exit_tagger_active', True)

        # Restore Envoy parameter enable state
        for p in ('Envoyenable', 'Envoyport', 'Aiclient'):
            par = getattr(self.my.par, p, None)
            if par is not None:
                par.enable = True

        # Restart Envoy if it was running before
        if state.get('envoy_was_running'):
            run("parent.Embody.ext.Envoy.Start()", delayFrames=5)

        # Clean up snapshot
        self.my.unstore('_perform_state')

        # Trigger Refresh to restore UI state
        run("parent.Embody.par.Refresh.pulse()", delayFrames=10)

        self.Log('Perform Mode OFF -- features restored', 'INFO')

    def _applyTdnModeGating(self) -> None:
        """Three-way UI gating for TDN-page parameters based on Tdnmode.

        - Off: all params greyed except Tdnmode itself.
        - Export: strip/reconstruction params (Tdnstriponsave, Tdncreateonstart)
          greyed; remaining Embed/cascade/picker params stay live.
        - Full: all params live.
        """
        master = getattr(self.my.par, 'Tdnmode', None)
        if master is None:
            return
        mode = self._tdnMode()
        try:
            for page in self.my.customPages:
                if page.name != 'TDN':
                    continue
                for p in page.pars:
                    if p.name == 'Tdnmode':
                        continue
                    try:
                        if mode == 'off':
                            p.enable = False
                        elif mode == 'export':
                            p.enable = p.name not in self._TDN_FULL_ONLY_PARAMS
                        else:  # full
                            p.enable = True
                    except Exception:
                        pass
        except Exception as e:
            self.Log(f'Could not apply Tdnmode gating: {e}', 'DEBUG')

    # Backward-compat alias (old name used inside Update / parexec history).
    _applyTdnEnableGating = _applyTdnModeGating

    def _onTdnModeChanged(self, mode: str) -> None:
        """Handle a Tdnmode change from parexec.

        Transitions surface the impact so the user isn't surprised:
        - TO off with tracked TDN COMPs: confirmation dialog (preserve files).
        - export -> full: INFO log that Full is experimental.
        - full -> export: INFO log that reconstruction will be skipped.
        - off -> full: no dialog here (cold flip).

        Always refreshes gating last.
        """
        if mode == 'off':
            existing = []
            try:
                existing = self._getTDNStrategyComps()
            except Exception as e:
                self.Log(f'Could not enumerate TDN COMPs: {e}', 'DEBUG')
            if existing:
                count = len(existing)
                choice = self._messageBox(
                    'Embody - Disable TDN',
                    f'Switching TDN to Off with {count} tracked TDN COMP(s).\n\n'
                    f'Their .tdn files on disk will be preserved. Embody will\n'
                    f'simply stop reconstructing, stripping, or re-exporting\n'
                    f'them until you switch back.\n\n'
                    f'Continue?',
                    buttons=['Cancel', 'Keep .tdn files (disable only)'])
                if choice != 1:
                    # User cancelled -- restore to Export (the safe default)
                    # with parexec suppressed so _onTdnModeChanged doesn't
                    # re-fire and log a misleading "mode: Export-on-Save".
                    parexec = self.my.op('parexec')
                    was_active = (parexec.par.active.eval()
                                  if parexec else None)
                    if parexec:
                        parexec.par.active = False
                    try:
                        self.my.par.Tdnmode = 'export'
                    finally:
                        if parexec:
                            parexec.par.active = was_active
                    self._applyTdnModeGating()
                    self.Log('TDN mode change cancelled by user', 'INFO')
                    return
                self.Log('TDN disabled (.tdn files preserved on disk)',
                         'INFO')
            # else: no tracked COMPs -- flip is silent, nothing to preserve
        elif mode == 'full':
            self.Log(
                'TDN mode: Roundtrip (Experimental). Strip/restore '
                'runs on save; children are reconstructed from .tdn on open. '
                'Watch for edge cases with extension reload timing on '
                'deeply-nested TDN COMPs.', 'INFO')
        elif mode == 'export':
            self.Log(
                'TDN mode: Export-on-Save. .toe is the source of truth; '
                '.tdn files are rewritten on save. Reconstruction on open '
                'is skipped.', 'INFO')
        self._applyTdnModeGating()

    # Backward-compat alias (old name referenced by parexec pre-rename).
    _onTdnEnableChanged = _onTdnModeChanged

    def _getTDNStrategyComps(self) -> list[tuple[str, str]]:
        """Get all TDN-strategy COMPs from the externalizations table.

        Returns list of (comp_path, rel_tdn_path) tuples.
        Never includes Embody itself, its ancestors, or its descendants --
        reconstructing or stripping anything inside Embody would be
        self-destruction.
        """
        table = self.Externalizations
        if not table:
            return []
        if table[0, 'strategy'] is None:
            return []  # Legacy table without strategy column -- no TDN entries
        embody_path = self.my.path  # e.g. /embody/Embody -- skip regardless of location
        result = []
        for i in range(1, table.numRows):
            if table[i, 'strategy'].val == 'tdn':
                comp_path = table[i, 'path'].val
                # Never include root "/" -- stripping it destroys the entire project.
                # Never include Embody, its ancestors, or its descendants.
                if (comp_path == '/'
                        or comp_path == embody_path
                        or embody_path.startswith(comp_path + '/')
                        or comp_path.startswith(embody_path + '/')):
                    continue
                result.append((
                    comp_path,
                    table[i, 'rel_file_path'].val,
                ))
        # Sort by path depth (fewest segments first) so parents are
        # imported before their children during reconstruction. Each
        # child's own .tdn file then overwrites the parent's snapshot.
        result.sort(key=lambda x: x[0].count('/'))
        return result

    # ------------------------------------------------------------------
    # DAT Content Safety
    # ------------------------------------------------------------------

    # DAT operator types whose `text`/table content is fully derived by
    # TouchDesigner from inputs, parameters, or runtime state. The user
    # cannot author this content -- TD regenerates it on cook -- so
    # warning that it "will be lost on save" is noise. Compared against
    # `dat.type` (short form, e.g. 'info' not 'infoDAT'), matching the
    # convention used by self.supported_dat_types.
    #
    # Callback DATs (execute, parexec, chopexec, datexec, opexec,
    # panelexec, pargroupexec, keyboardin, mousein, oscin, etc.) are
    # NOT in this set -- their content IS user-authored Python and must
    # continue to surface in the at-risk warning.
    _TD_MANAGED_DAT_TYPES = {
        'info',           # Info DAT -- introspection of another op
        'webrtc',         # Per-connection signaling state
        'folder',         # Filesystem listing
        'opfind',         # Network search results
        'monitors',       # Monitor hardware state
        'audiodevices',   # Audio device enumeration
        'videodevices',   # Video device enumeration
        'serialdevices',  # Serial device enumeration
        'mididevices',    # MIDI device enumeration
        'midievent',      # Project-wide MIDI event log
        'error',          # FIFO of recent TD errors
        'perform',        # Cook/draw timing log
        'examine',        # Inspector view of another op
        'mediafileinfo',  # Metadata extracted from a media file
        'tuioin',         # Inbound TUIO event table
        'multitouchin',   # Inbound Windows multi-touch events
        'ndi',            # Discovered NDI sources
        'mpcdi',          # Calibration data parsed from .mpcdi
        'indices',        # Generated number series
    }

    def _findAtRiskDATs(self) -> list:
        """Find DATs inside TDN COMPs that will lose content during save.

        Returns list of (comp_path, [dat_ops]) tuples for TDN COMPs where
        Embed DATs is OFF and unexternalized DATs have non-empty content.
        """
        tdn_comps = self._getTDNStrategyComps()
        if not tdn_comps:
            return []

        tdn_paths = {path for path, _ in tdn_comps}
        result = []

        for comp_path, _ in tdn_comps:
            comp = op(comp_path)
            if not comp:
                continue

            # Resolve embed_dats: per-COMP override → global parameter
            per_comp = comp.fetch('embed_dats_in_tdn', None, search=False)
            embed_on = (per_comp if per_comp is not None
                        else self.my.par.Embeddatsintdns.eval())
            if embed_on:
                continue  # Content will be preserved in TDN

            at_risk = []
            for dat in comp.findChildren(type=DAT):
                # Skip DATs inside a deeper TDN COMP -- covered by that
                # COMP's own settings
                inside_nested = False
                parent_op = dat.parent()
                while parent_op and parent_op.path != comp_path:
                    if parent_op.path in tdn_paths:
                        inside_nested = True
                        break
                    parent_op = parent_op.parent()
                if inside_nested:
                    continue

                # Skip DATs with a file parameter already set
                if hasattr(dat.par, 'file') and dat.par.file.eval():
                    continue

                # Skip DATs whose content TD generates and regenerates
                # on cook (info, webrtc, folder, monitors, devices, etc.)
                # The user did not author this content and cannot preserve
                # it -- warning would be noise. Callback DATs (execute,
                # parexec, etc.) are intentionally absent from this set.
                if dat.type in self._TD_MANAGED_DAT_TYPES:
                    continue

                # Check for non-empty content
                try:
                    if dat.isTable:
                        if dat.numRows > 0:
                            at_risk.append(dat)
                    else:
                        if dat.text and dat.text.strip():
                            at_risk.append(dat)
                except Exception:
                    pass  # Unreadable DAT -- skip

            if at_risk:
                result.append((comp_path, at_risk))

        return result

    # Storage keys preserved even when Embedstorageintdns is off
    # (mirrors TDNExt logic that exports these as control metadata).
    _STORAGE_CONTROL_KEYS = {'embed_dats_in_tdn', 'embed_storage_in_tdn'}
    # Storage keys never surfaced as at-risk -- superset of
    # TDNExt.SKIP_STORAGE_KEYS covering additional Embody runtime state
    # (mode migration flags, pane restore, init completion, etc.) that
    # TDNExt also does not serialize meaningfully. Only user-owned keys
    # should reach _findAtRiskStorage.
    _STORAGE_SKIP_KEYS = {
        '_tdn_stripped_paths', '_git_root',
        'envoy_running', 'envoy_shutdown_event',
        'expanded_paths', 'expand_order',
        'manage_file_path', 'visible_count', 'hover',
        '_tdn_external_wires', '_tdn_pane_restore',
        '_tdn_palette_handling',
        '_init_complete', '_smoke_test_responses',
        '_tdn_restore_failures',
        '_tdn_mode_migration_shown', '_tdn_migration_scheduled',
        '_tdn_migration_prev_enable',
        'pressed',
    }

    def _findAtRiskStorage(self) -> list:
        """Find operators inside TDN COMPs whose comp.storage entries will
        be lost on save. Mirrors _findAtRiskDATs.

        Returns list of (comp_path, [(op_path, [keys])]) tuples for TDN
        COMPs where Embed Storage is OFF and any op inside has non-control,
        non-runtime storage keys.
        """
        tdn_comps = self._getTDNStrategyComps()
        if not tdn_comps:
            return []

        tdn_paths = {path for path, _ in tdn_comps}
        result = []

        for comp_path, _ in tdn_comps:
            comp = op(comp_path)
            if not comp:
                continue

            # Resolve embed_storage: per-COMP override -> global parameter
            per_comp = comp.fetch('embed_storage_in_tdn', None, search=False)
            embed_on = (per_comp if per_comp is not None
                        else self.my.par.Embedstorageintdns.eval())
            if embed_on:
                continue  # Storage preserved in TDN

            at_risk = []
            # Check comp itself and all descendants (depth is unbounded;
            # excluded descendants are only those inside a nested TDN COMP,
            # which that COMP's own settings handle).
            candidates = [comp] + list(comp.findChildren())
            for target in candidates:
                # Skip ops inside a nested TDN COMP
                if target is not comp:
                    inside_nested = False
                    parent_op = target.parent()
                    while parent_op and parent_op.path != comp_path:
                        if parent_op.path in tdn_paths:
                            inside_nested = True
                            break
                        parent_op = parent_op.parent()
                    if inside_nested:
                        continue

                try:
                    storage = target.storage
                except Exception:
                    continue
                if not storage:
                    continue

                risky_keys = [
                    k for k in storage.keys()
                    if k not in self._STORAGE_CONTROL_KEYS
                    and k not in self._STORAGE_SKIP_KEYS
                ]
                if risky_keys:
                    at_risk.append((target.path, sorted(risky_keys)))

            if at_risk:
                result.append((comp_path, at_risk))

        return result

    def _promptTDNContentSafety(
            self, at_risk_dats: list, at_risk_storage: list) -> str:
        """Show combined dialog for at-risk DATs + storage.

        Returns 'externalize' or 'skip'. Note: 'externalize' applies only
        to DATs; storage has no externalization path, skip logs a summary.
        """
        all_dats = [d for _, dats in at_risk_dats for d in dats]
        dat_count = len(all_dats)
        storage_entries = [
            (op_path, keys)
            for _, entries in at_risk_storage
            for op_path, keys in entries
        ]
        storage_count = sum(len(keys) for _, keys in storage_entries)

        sections = []

        if dat_count:
            noun = 'DAT' if dat_count == 1 else 'DATs'
            lines = []
            for dat in all_dats[:10]:
                fmt = 'table' if dat.isTable else 'text'
                lines.append(f'  \u2022 {dat.path} ({fmt})')
            if dat_count > 10:
                lines.append(f'  \u2026 and {dat_count - 10} more')
            sections.append(
                f'{dat_count} {noun} will lose content (Embed DATs OFF):\n'
                + '\n'.join(lines))

        if storage_count:
            key_noun = 'key' if storage_count == 1 else 'keys'
            lines = []
            shown = 0
            for op_path, keys in storage_entries:
                for k in keys:
                    if shown >= 10:
                        break
                    lines.append(f'  \u2022 {op_path} \u2192 "{k}"')
                    shown += 1
                if shown >= 10:
                    break
            if storage_count > 10:
                lines.append(f'  \u2026 and {storage_count - 10} more')
            sections.append(
                f'{storage_count} storage {key_noun} will be lost '
                f'(Embed Storage OFF):\n' + '\n'.join(lines))

        body = '\n\n'.join(sections)
        externalize_verb = 'Externalize DATs' if dat_count else 'Continue'
        msg = (f'TDN content will be dropped on next save.\n\n'
               f'{body}\n\n'
               f'Note: storage has no externalization path -- enable Embed '
               f'Storage in TDNs to preserve it, or dismiss to proceed.')

        buttons = [externalize_verb, 'Skip', 'Always Externalize']
        choice = self._messageBox(
            'TDN Content at Risk', msg, buttons=buttons)

        if choice == 0:
            return 'externalize'
        elif choice == 2:
            self.my.par.Tdndatsafety = 'externalize'
            self.Log('TDN content safety preference set to Always '
                     'Externalize', 'INFO')
            return 'externalize'
        return 'skip'

    def _externalizeDATs(self, dats: list) -> int:
        """Bulk-externalize a list of DAT operators. Returns success count.

        Par-driven: sets par.file based on DAT type's default extension and
        triggers handleAddition to write the file to disk.
        """
        count = 0
        folder = self.ExternalizationsFolder or ''
        for dat in dats:
            try:
                if dat.type not in self.supported_dat_types:
                    continue
                if dat.par.file.eval():
                    continue
                ext = self.dat_type_to_extension.get(dat.type, 'py')
                rel = f"{folder}/{dat.name}.{ext}" if folder else f"{dat.name}.{ext}"
                dat.par.file.readOnly = False
                dat.par.file = rel
                self.handleAddition(dat)
                count += 1
            except Exception as e:
                self.Log(f'Failed to externalize {dat.path}: {e}', 'WARNING')
        return count

    def _checkTDNContentSafety(self) -> None:
        """Check for at-risk DATs AND storage in TDN COMPs.

        Called from onProjectPreSave() before the TDN export/strip cycle.
        Prompts user or auto-externalizes per Tdndatsafety preference.
        On skip, logs a SUCCESS summary naming what was dropped.
        """
        safety_par = getattr(self.my.par, 'Tdndatsafety', None)
        preference = safety_par.eval() if safety_par else 'ask'

        if preference == 'ignore':
            return

        at_risk_dats = self._findAtRiskDATs()
        at_risk_storage = self._findAtRiskStorage()
        if not at_risk_dats and not at_risk_storage:
            return

        all_dats = [d for _, dats in at_risk_dats for d in dats]

        if preference == 'externalize':
            count = self._externalizeDATs(all_dats)
            if count:
                self.Log(f'Auto-externalized {count} at-risk DAT(s)',
                         'SUCCESS')
            if at_risk_storage:
                self._logSkippedStorage(at_risk_storage)
            return

        # preference == 'ask'
        choice = self._promptTDNContentSafety(at_risk_dats, at_risk_storage)
        if choice == 'externalize':
            count = self._externalizeDATs(all_dats)
            self.Log(f'Externalized {count} at-risk DAT(s)', 'SUCCESS')
            if at_risk_storage:
                self._logSkippedStorage(at_risk_storage)
        else:
            if all_dats:
                self._logSkippedDATs(all_dats)
            if at_risk_storage:
                self._logSkippedStorage(at_risk_storage)

    # Backwards-compatible alias (execute.py may still call the old name).
    _checkDATContentSafety = _checkTDNContentSafety

    def _logSkippedDATs(self, dats: list) -> None:
        """Log a SUCCESS-level summary of DATs whose content was dropped."""
        names = ', '.join(d.path for d in dats[:5])
        if len(dats) > 5:
            names += f', \u2026 (+{len(dats) - 5} more)'
        self.Log(
            f'Skipped externalization of {len(dats)} at-risk DAT(s): '
            f'{names}', 'SUCCESS')

    def _logSkippedStorage(self, at_risk_storage: list) -> None:
        """Log a SUCCESS-level summary of storage keys that will be dropped."""
        entries = []
        total = 0
        for _, op_entries in at_risk_storage:
            for op_path, keys in op_entries:
                total += len(keys)
                entries.append(f'{op_path}[{",".join(keys)}]')
        shown = ', '.join(entries[:5])
        if len(entries) > 5:
            shown += f', \u2026 (+{len(entries) - 5} more)'
        self.Log(
            f'Dropping {total} TDN storage entr{"y" if total == 1 else "ies"} '
            f'on save (Embed Storage OFF): {shown}', 'SUCCESS')

    def StripCompChildren(self, comp: OP) -> int:
        """Remove children from a TDN-strategy COMP (for smaller .toe).

        Destroys both regular children and utility operators (annotations).
        Before destruction, captures external sibling wires on comp's own
        connectors and stores them on comp via comp.store() so they can
        be restored after the COMP is rebuilt (on post-save, cold open,
        or user reload). Storage survives .toe save since the COMP shell
        itself is not stripped.

        Returns the number of operators destroyed.
        """
        # Capture external connections before destroying children.
        # The in*/out* ops inside comp define its own connectors --
        # destroying them severs any external wires attached to them.
        try:
            externals = self.my.ext.TDN._captureExternalConnections(comp)
            if externals:
                comp.store('_tdn_external_wires', externals)
                self.Log(
                    f'Captured {len(externals)} external connection(s) on '
                    f'{comp.path} before strip', 'DEBUG')
        except Exception as e:
            self.Log(
                f'External capture failed on {comp.path}: {e}', 'WARNING')

        # findChildren with includeUtility=True gets everything:
        # regular children + hidden utility ops (annotations with utility=True)
        all_ops = list(comp.findChildren(depth=1, includeUtility=True))
        count = len(all_ops)
        n_utility = sum(1 for c in all_ops if getattr(c, 'utility', False))
        # Clear dock relationships before destroying -- TD's engine
        # raises an uncatchable tdError if a dock target is destroyed
        # before its docked operator.
        for child in all_ops:
            try:
                if child.dock is not None:
                    child.dock = None
            except Exception:
                pass
        for child in all_ops:
            try:
                child.destroy()
            except Exception as e:
                self.Log(f'Failed to destroy {child.path}: {e}', 'WARNING')
        if count:
            self.Log(f'Stripped {count} operators from {comp.path} '
                     f'({count - n_utility} children, {n_utility} annotations)', 'INFO')
        return count

    def _verifyReconstructedComp(self, comp) -> list[str]:
        """Check a reconstructed COMP for TD errors (broken connections, scripts, etc.).

        Returns list of error strings found.
        """
        errors = []
        try:
            for child in comp.findChildren():
                err_str = child.errors()
                if err_str:
                    for err in err_str.split('\n'):
                        err = err.strip()
                        if err:
                            errors.append(f'{child.path}: {err}')
                warn_str = child.warnings()
                if warn_str:
                    for warn in warn_str.split('\n'):
                        warn = warn.strip()
                        if warn:
                            self.Log(f'Warning in {child.path}: {warn}', 'WARNING')
        except Exception as e:
            self.Log(f'Error checking {comp.path}: {e}', 'WARNING')

        for err in errors:
            self.Log(f'Reconstruction error: {err}', 'ERROR')

        return errors

    def _logReconstructionReport(self, tdn_comps, errors_total) -> None:
        """Log a summary report after TDN reconstruction."""
        count = len(tdn_comps)
        if errors_total:
            self.Log(
                f'TDN reconstruction complete: {count} COMP(s), '
                f'{errors_total} error(s) detected',
                'WARNING')
        else:
            self.Log(
                f'TDN reconstruction complete: {count} COMP(s) rebuilt successfully',
                'SUCCESS')

    def _createMissingCompShell(self, comp_path: str, strategy: str,
                               comp_type_override: str = None) -> 'OP | None':
        """Create a missing COMP that was tagged but not saved in the .toe.

        Used by both ReconstructTDNComps and RestoreTOXComps when a tracked
        COMP doesn't exist on project open.

        Args:
            comp_path: Full TD path (e.g., '/embody/base_tdn')
            strategy: 'tdn' or 'tox' -- determines which tag/color to apply
            comp_type_override: Full TD type string (e.g. 'containerCOMP')
                from TDN file. Takes priority over externalizations table.

        Returns:
            The created COMP, or None on failure.
        """
        parent_path = comp_path.rsplit('/', 1)[0] or '/'
        parent_op = op(parent_path)
        if not parent_op or not hasattr(parent_op, 'create'):
            self.Log(f'Cannot create {comp_path}: parent {parent_path} '
                     f'not found or not a COMP', 'WARNING')
            return None

        # Priority: TDN type override > externalizations table > 'baseCOMP'
        if comp_type_override:
            td_type = comp_type_override
        else:
            comp_type = self._getCompTypeFromTable(comp_path) or 'base'
            td_type = f'{comp_type}COMP'
        comp_name = comp_path.rsplit('/', 1)[-1]

        try:
            new_comp = parent_op.create(td_type, comp_name)
        except Exception as e:
            self.Log(f'Failed to create {comp_path} ({td_type}): {e}', 'ERROR')
            return None

        self.Log(f'Created missing COMP shell: {comp_path}', 'INFO')

        # Restore position/color from table metadata
        self._restorePositionFromTable(new_comp, comp_path)

        return new_comp

    def _getCompTypeFromTable(self, comp_path: str) -> str:
        """Read the 'type' column for a COMP from the externalizations table."""
        table = self.Externalizations
        if not table:
            return ''
        for i in range(1, table.numRows):
            if table[i, 'path'].val == comp_path:
                return table[i, 'type'].val
        return ''

    def _restorePositionFromTable(self, comp: 'OP', comp_path: str) -> None:
        """Restore an operator's position and color from the externalizations table."""
        table = self.Externalizations
        if not table:
            return
        # Check if position columns exist
        if table[0, 'node_x'] is None:
            return
        for i in range(1, table.numRows):
            if table[i, 'path'].val == comp_path:
                x_val = table[i, 'node_x'].val
                y_val = table[i, 'node_y'].val
                if x_val and y_val:
                    try:
                        comp.nodeX = int(float(x_val))
                        comp.nodeY = int(float(y_val))
                    except (ValueError, TypeError):
                        pass
                color_val = table[i, 'node_color'].val
                if color_val:
                    try:
                        r, g, b = [float(c) for c in color_val.split(',')]
                        comp.color = (r, g, b)
                    except (ValueError, TypeError):
                        pass
                return

    # ==========================================================================
    # METADATA RECONCILIATION ON START
    # ==========================================================================

    def ReconcileMetadata(self) -> None:
        """Re-apply file parameters and positions from the externalizations table.

        With par-driven discovery the table is *derived from* par state via
        _scanAndPopulate(), so reconciling pars back from the table is mostly
        circular. This is retained as a no-op for now -- positions can still
        be restored from the table by RestoreTOXComps and ReconstructTDNComps
        when they create missing operators.
        """
        return

    # ==========================================================================
    # TOX RESTORATION ON START
    # ==========================================================================

    def RestoreTOXComps(self) -> None:
        """Restore missing TOX-strategy COMPs from .tox files on project open.

        For each TOX-strategy entry in the externalizations table where the
        operator is missing but the .tox file exists on disk, creates the COMP
        and sets externaltox to trigger TD's auto-load.
        """
        if not self.my.par.Toxrestoreonstart.eval():
            return

        tox_comps = self._getTOXStrategyComps()
        if not tox_comps:
            return

        # Filter to only missing COMPs with existing .tox files
        to_restore = []
        for comp_path, rel_tox_path, comp_type in tox_comps:
            if op(comp_path):
                continue  # Already exists in .toe -- nothing to do
            abs_path = self.buildAbsolutePath(rel_tox_path)
            if not abs_path.is_file():
                self.Log(f'TOX file not found for missing COMP '
                         f'{comp_path}: {rel_tox_path}', 'WARNING')
                continue
            to_restore.append((comp_path, rel_tox_path, comp_type))

        if not to_restore:
            return

        self.Log(f'Restoring {len(to_restore)} TOX COMP(s) from disk...', 'INFO')
        restored = 0
        errors = 0

        for comp_path, rel_tox_path, comp_type in to_restore:
            # Check if it appeared (e.g. loaded as child of a parent .tox)
            if op(comp_path):
                restored += 1
                self.Log(f'COMP {comp_path} already present '
                         f'(loaded from parent .tox)', 'INFO')
                continue

            # Verify parent exists
            parent_path = comp_path.rsplit('/', 1)[0] or '/'
            parent_op = op(parent_path)
            if not parent_op:
                self.Log(f'Parent {parent_path} not found, cannot restore '
                         f'{comp_path}', 'WARNING')
                errors += 1
                continue

            if not hasattr(parent_op, 'create'):
                self.Log(f'Parent {parent_path} is not a COMP, cannot restore '
                         f'{comp_path}', 'WARNING')
                errors += 1
                continue

            comp_name = comp_path.rsplit('/', 1)[-1]
            td_type = f'{comp_type}COMP'

            try:
                new_comp = parent_op.create(td_type, comp_name)
            except Exception as e:
                self.Log(f'Failed to create {comp_path} '
                         f'(type {td_type}): {e}', 'ERROR')
                errors += 1
                continue

            # Set externaltox to trigger TD auto-load from .tox
            try:
                new_comp.par.externaltox = self.normalizePath(rel_tox_path)
                new_comp.par.externaltox.readOnly = True
                new_comp.par.enableexternaltox = True

                # Handle timing issue (same workaround as
                # _setupCompForExternalization)
                if ("Cannot load external tox from path"
                        in new_comp.scriptErrors()):
                    new_comp.allowCooking = False
                    run(lambda p=new_comp.path: self._safeAllowCooking(p, True),
                        delayFrames=1)

                # Restore position from table metadata
                self._restorePositionFromTable(new_comp, comp_path)

                restored += 1
                self.Log(f'Restored {comp_path} from {rel_tox_path}', 'SUCCESS')

            except Exception as e:
                self.Log(f'Failed to configure externaltox for '
                         f'{comp_path}: {e}', 'ERROR')
                errors += 1

        self._logTOXRestorationReport(len(to_restore), restored, errors)

    def _getTOXStrategyComps(self) -> list[tuple[str, str, str]]:
        """Get all TOX-strategy COMPs from the externalizations table.

        Returns list of (comp_path, rel_tox_path, comp_type) tuples,
        sorted by path depth (shallowest first) so parents are created
        before children.

        Never includes Embody itself, its ancestors, or its descendants.
        """
        table = self.Externalizations
        if not table:
            return []
        if table[0, 'strategy'] is None:
            return []  # Legacy table without strategy column
        embody_path = self.my.path
        result = []
        for i in range(1, table.numRows):
            if table[i, 'strategy'].val == 'tox':
                comp_path = table[i, 'path'].val
                # Never include Embody, its ancestors, or its descendants
                if (comp_path == '/'
                        or comp_path == embody_path
                        or embody_path.startswith(comp_path + '/')
                        or comp_path.startswith(embody_path + '/')):
                    continue
                result.append((
                    comp_path,
                    table[i, 'rel_file_path'].val,
                    table[i, 'type'].val,
                ))
        # Sort by path depth -- parents first
        result.sort(key=lambda x: x[0].count('/'))
        return result

    def _logTOXRestorationReport(self, total, restored, errors) -> None:
        """Log a summary report after TOX restoration."""
        if errors:
            self.Log(
                f'TOX restoration complete: {restored}/{total} COMP(s) '
                f'restored, {errors} error(s)',
                'WARNING')
        else:
            self.Log(
                f'TOX restoration complete: {restored} COMP(s) restored '
                f'successfully',
                'SUCCESS')

    # ==========================================================================
    # DAT RESTORATION ON START
    # ==========================================================================

    def RestoreDATs(self) -> None:
        """Restore missing DATs from externalized files on project open.

        For each DAT-strategy entry in the externalizations table where the
        operator is missing but the source file exists on disk, creates the
        correct DAT type and configures file/syncfile for auto-sync.
        """
        if not self.my.par.Datrestoreonstart.eval():
            return

        dat_entries = self._getDATEntries()
        if not dat_entries:
            return

        # Supported DAT types (matches self.supported_dat_types)
        valid_dat_types = set(self.supported_dat_types)

        # Filter to only missing DATs with existing files on disk
        to_restore = []
        for dat_path, rel_file_path, dat_type, strategy in dat_entries:
            if op(dat_path):
                continue  # Already exists in network
            abs_path = self.buildAbsolutePath(rel_file_path)
            if not abs_path.is_file():
                self.Log(f'File not found for missing DAT '
                         f'{dat_path}: {rel_file_path}', 'WARNING')
                continue
            to_restore.append((dat_path, rel_file_path, dat_type, strategy))

        if not to_restore:
            return

        self.Log(f'Restoring {len(to_restore)} DAT(s) from disk...', 'INFO')
        restored = 0
        errors = 0

        for dat_path, rel_file_path, dat_type, strategy in to_restore:
            # Check if it appeared (e.g. loaded as child of a parent .tox)
            if op(dat_path):
                restored += 1
                self.Log(f'DAT {dat_path} already present '
                         f'(loaded from parent)', 'INFO')
                continue

            # Verify parent exists and is a COMP
            parent_path = dat_path.rsplit('/', 1)[0] or '/'
            parent_op = op(parent_path)
            if not parent_op:
                self.Log(f'Parent {parent_path} not found, cannot restore '
                         f'{dat_path}', 'WARNING')
                errors += 1
                continue

            if not hasattr(parent_op, 'create'):
                self.Log(f'Parent {parent_path} is not a COMP, cannot restore '
                         f'{dat_path}', 'WARNING')
                errors += 1
                continue

            if dat_type not in valid_dat_types:
                self.Log(f'Unknown DAT type "{dat_type}" for '
                         f'{dat_path}', 'WARNING')
                errors += 1
                continue

            dat_name = dat_path.rsplit('/', 1)[-1]
            td_type = f'{dat_type}DAT'
            try:
                new_dat = parent_op.create(td_type, dat_name)
            except Exception as e:
                self.Log(f'Failed to create {dat_path} '
                         f'(type {td_type}): {e}', 'ERROR')
                errors += 1
                continue

            try:
                # Configure file sync
                normalized = self.normalizePath(rel_file_path)
                new_dat.par.file = normalized
                new_dat.par.syncfile = True
                new_dat.par.file.readOnly = True

                # Kick syncfile to force TD to read from disk
                op_path = str(new_dat)
                run(lambda p=op_path: self._safeSyncFile(p, False),
                    delayFrames=1)
                run(lambda p=op_path: self._safeSyncFile(p, True),
                    delayFrames=2)

                # Restore position from table metadata
                self._restorePositionFromTable(new_dat, dat_path)

                restored += 1
                self.Log(f'Restored {dat_path} from {rel_file_path}',
                         'SUCCESS')

            except Exception as e:
                self.Log(f'Failed to configure DAT {dat_path}: {e}', 'ERROR')
                errors += 1

        self._logDATRestorationReport(len(to_restore), restored, errors)

    def _getDATEntries(self) -> list[tuple[str, str, str, str]]:
        """Get all DAT-strategy entries from the externalizations table.

        Returns list of (dat_path, rel_file_path, dat_type, strategy) tuples,
        sorted by path depth (shallowest first).

        Never includes Embody itself or its descendants.
        Excludes DATs inside TOX-strategy or TDN-strategy COMPs
        (those are handled by RestoreTOXComps / ReconstructTDNComps).
        """
        table = self.Externalizations
        if not table:
            return []
        if table[0, 'strategy'] is None:
            return []  # Legacy table without strategy column

        embody_path = self.my.path

        # Collect TOX/TDN COMP paths so we can skip DATs inside them
        comp_paths = set()
        for i in range(1, table.numRows):
            strategy = table[i, 'strategy'].val
            if strategy in ('tox', 'tdn'):
                comp_paths.add(table[i, 'path'].val)

        result = []
        for i in range(1, table.numRows):
            strategy = table[i, 'strategy'].val
            if strategy in ('tox', 'tdn', ''):
                continue  # COMP strategies or empty

            dat_path = table[i, 'path'].val
            if not dat_path:
                continue

            # Never include Embody or its descendants
            if (dat_path == embody_path
                    or dat_path.startswith(embody_path + '/')):
                continue

            # Skip DATs inside TOX/TDN COMPs
            inside_comp = any(
                dat_path.startswith(cp + '/')
                for cp in comp_paths)
            if inside_comp:
                continue

            result.append((
                dat_path,
                table[i, 'rel_file_path'].val,
                table[i, 'type'].val,
                strategy,
            ))

        # Sort by path depth -- shallowest first
        result.sort(key=lambda x: x[0].count('/'))
        return result

    def _logDATRestorationReport(self, total, restored, errors) -> None:
        """Log a summary report after DAT restoration."""
        if errors:
            self.Log(
                f'DAT restoration complete: {restored}/{total} DAT(s) '
                f'restored, {errors} error(s)',
                'WARNING')
        else:
            self.Log(
                f'DAT restoration complete: {restored} DAT(s) restored '
                f'successfully',
                'SUCCESS')

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

        Phase 8: custom listCOMP popup at /embody/Embody/window_action_menu.
        Replaces the previous PopDialog cascade -- vertical action list,
        unlimited items, full label width, keyboard nav (up/down/Enter/Esc).
        Text-entry sub-flows (Release name/version) still use PopDialog.

        If op_path is None, picks the target from the user's current pane:
        rollover op -> first selected -> pane owner.
        """
        if self._performMode:
            return
        target = op(op_path) if op_path else self._resolveActionTarget()
        if target is None:
            self.Log('Action menu: no operator under cursor', 'WARNING')
            return
        if target.family not in ('COMP', 'DAT'):
            self.Log(
                f'Action menu: {target.family} not supported', 'WARNING')
            return

        items = self._buildActionItems(target)
        if not items:
            self.Log(
                f'Action menu: no actions available for {target.path}',
                'INFO')
            return

        # Stash the target so the listCOMP / keyboardin callbacks can find it.
        self._action_menu_target_path = target.path

        # Populate the action_items table.
        am = self.my.op('action_menu')
        if am is None:
            self.Log(
                'Action menu COMP missing at /embody/Embody/action_menu '
                '-- run Embody.Reset() or restore the .toe',
                'ERROR')
            return
        table = am.op('action_items')
        table.clear()
        table.appendRow(['label', 'action_id', 'enabled'])
        for label, action_id, enabled in items:
            table.appendRow([label, action_id, '1' if enabled else '0'])

        # Reset selection to first item.
        am.store('actionmenu_selected_row', 1)
        lst = am.op('list1')
        if lst:
            # Size list to match number of rows + a little padding.
            row_count = max(1, len(items))
            row_height = 26
            lst.par.h = row_count * row_height + 2

        # Resize and open the window.
        win = self.my.op('window_action_menu')
        if win:
            try:
                win.par.winh = row_count * 26 + 4
            except Exception:
                pass
            try:
                win.par.winopen.pulse()
            except Exception as e:
                self.Log(f'window_action_menu open failed: {e}', 'WARNING')

    def CloseActionMenu(self) -> None:
        """Close the action menu window."""
        win = self.my.op('window_action_menu')
        if win:
            try:
                win.par.winclose.pulse()
            except Exception:
                pass

    def _buildActionItems(self, target: OP) -> list:
        """Compute the action list for an operator, as (label, action_id,
        enabled) tuples in display order.
        """
        is_comp = (target.family == 'COMP')
        is_externalized = self._isOpExternalized(target)

        items: list[tuple[str, str, bool]] = []

        if is_externalized:
            items.append(('Save', 'save', True))
            if is_comp:
                items.append(('Reload from .tdn', 'reload_tdn', True))
                items.append(('Reload from .tox', 'reload_tox', True))
                items.append(('Release...', 'release', True))
            items.append(('Reveal in file browser', 'reveal', True))
            items.append(('Remove externalization', 'remove', True))
        else:
            if not is_comp and target.type not in self.supported_dat_types:
                items.append(
                    (f'(DAT type {target.type!r} not supported)', 'noop', False))
                return items
            default_par = ('Defaulttoxfolder' if is_comp
                           else 'Defaultscriptfolder')
            has_default = bool(
                getattr(self.my.par, default_par, None)
                and getattr(self.my.par, default_par).eval())
            if has_default:
                items.append(
                    ('Externalize (default folder)', 'extern_default', True))
            items.append(
                ('Externalize (choose folder)...', 'extern_choose', True))
        return items

    def _dispatchAction(self, action_id: str) -> None:
        """Run the action identified by action_id on the stashed target."""
        path = getattr(self, '_action_menu_target_path', None)
        target = op(path) if path else None
        # Close the window before running -- the action may itself open
        # another dialog (Release cascade) and we don't want the menu
        # lingering on top of it.
        self.CloseActionMenu()
        if target is None:
            self.Log(
                f'Action menu: target {path!r} no longer exists', 'WARNING')
            return
        try:
            if action_id == 'save':
                self._saveOpFromMenu(target)
            elif action_id == 'reload_tdn':
                self.ReloadFromTdn(target.path)
            elif action_id == 'reload_tox':
                self._reloadFromTox(target)
            elif action_id == 'release':
                self._actionMenuReleaseName(target)
            elif action_id == 'reveal':
                is_comp = (target.family == 'COMP')
                rel = (target.par.externaltox.eval() if is_comp
                       else target.par.file.eval())
                if rel:
                    self.OpenSaveFile(rel)
            elif action_id == 'remove':
                is_comp = (target.family == 'COMP')
                rel = (target.par.externaltox.eval() if is_comp
                       else target.par.file.eval())
                self.RemoveListerRow(target.path, rel, delete_file=True)
            elif action_id == 'extern_default':
                self._externalizeViaMenu(target, use_default=True)
            elif action_id == 'extern_choose':
                self._externalizeViaMenu(target, use_default=False)
            elif action_id == 'noop':
                pass
            else:
                self.Log(
                    f'Action menu: unknown action_id {action_id!r}',
                    'WARNING')
        except Exception as e:
            import traceback
            self.Log(
                f'Action {action_id!r} failed: {e}', 'ERROR',
                traceback.format_exc()[-400:])

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

    def _actionMenuReload(self, target: OP) -> None:
        """Sub-menu: choose how to reload an externalized COMP from disk."""
        if target.family != 'COMP':
            self.Log('Reload only applies to COMPs', 'WARNING')
            return
        # PopDialog clips button labels past ~6 chars at the typical
        # dialog width. The body text below carries the meaning -- the
        # buttons just need to indicate which format.
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
        """Reload a COMP from its .tox file via TD's native reloadtoxpulse."""
        try:
            if not target.par.externaltox.eval():
                self.Log(
                    f'No .tox path set for {target.path}', 'WARNING')
                return
            target.par.reloadtoxpulse.pulse()
            self.Log(f'Reloaded {target.path} from .tox', 'SUCCESS')
        except Exception as e:
            self.Log(
                f'Reload from .tox failed for {target.path}: {e}', 'ERROR')

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
        self.Release(target, name=new_name, version=new_version,
                     save_path=save_path)

    def _externalizeViaMenu(self, target: OP, use_default: bool) -> None:
        """Set the external file par and run Update."""
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
        self.UpdateHandler()

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
        except Exception as e:
            self.Log(
                f'Reload from .tdn failed for {comp_path}: {e}', 'ERROR')

    def getProjectFolder(self) -> str:
        """Get project folder path."""
        if self.my.par.Folder.mode == ParMode.EXPRESSION:
            return self.my.par.Folder.eval()
        return str(Path(project.folder) / self.my.par.Folder)

    def getSaveFolder(self) -> str:
        """Get save folder path."""
        if self.my.par.Folder.expr:
            return self.my.par.Folder.eval()
        return project.folder + '/' + self.my.par.Folder

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
        """Open externalizations table viewer."""
        self.Externalizations.openViewer()

    def MissingExternalizationsPar(self) -> None:
        """Log error for missing externalizations table."""
        self.Log("Missing Externalization tableDAT - required for operation", "ERROR")

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
        """Capture all parameters of a COMP."""
        params = {}
        for page in comp.pages + comp.customPages:
            for par in page.pars:
                if par.name in ['externaltox', 'file']:
                    continue
                params[par.name] = {
                    'value': par.eval(),
                    'expr': par.expr if par.expr else None,
                    'bindExpr': par.bindExpr if par.bindExpr else None,
                    'mode': par.mode
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