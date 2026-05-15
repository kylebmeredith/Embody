'''
Embody v5
===============

Embody provides robust automated externalization for
TouchDesigner projects. Any COMP or DAT operator can be
externalized to a version-control-friendly file (.tox, .py,
.json, .tdn, etc.) by setting its native external-file
parameter.

Simply drag and drop the Embody .tox from the /release folder
into your project to get started!

Getting Started
---------------
1. Add the Embody .tox to your project
2. Set par.externaltox on COMPs you want to externalize, or
   par.file on supported DATs (or use "Externalize Full
   Project" to do it for every compatible op)
3. Press ctrl-shift-u to initialize/update
4. Work as normal -- externalized files are the source of truth

Discovery is par-driven: an op is externalized iff
par.externaltox != '' (COMPs) or par.file != '' (DATs). No
tags. No persistent tracking table on disk. The in-memory
Externalizations table is a derived view, rebuilt on every
Update/Refresh from a live par-driven scan.

Supported Operators
-------------------
COMPs:
- All COMPs except engine, time, and annotate

DATs:
- Text DAT
- Table DAT
- Execute DAT
- Parameter Execute DAT
- Parameter Group Execute DAT
- CHOP Execute DAT
- DAT Execute DAT
- OP Execute DAT
- Panel Execute DAT

Supported File Formats
----------------------
COMPs: .tox + .tdn sidecar (every externalized COMP writes
       both -- .tox is canonical, .tdn is the diff-friendly
       JSON view that TDN tooling can edit and reload)
DATs:  .py, .json, .xml, .html, .glsl, .frag, .vert,
       .txt, .md, .rtf, .csv, .tsv, .dat

Workflow
--------
Embody keeps your external files up to date as you work.
Press ctrl-shift-u to update all dirty externalizations, or
ctrl-alt-u to update just the COMP you're currently inside.
DATs are automatically synchronized by TouchDesigner via
their Sync to File parameter.

Embody also tracks parameter changes on externalized COMPs.
When any parameter is modified, the COMP is marked dirty
with a "Par" indicator, ensuring parameter tweaks are never
lost.

Automatic Restoration
---------------------
You do not need to save your .toe file to preserve your
externalized work. On project open, Embody automatically
restores everything from the files on disk:

- TOX-strategy COMPs: Restored from .tox files if missing
  from the .toe (via the Toxrestoreonstart toggle)
- TDN-strategy COMPs: Children are reconstructed from .tdn
  JSON files (via the Tdncreateonstart toggle)
- DATs: Synced from their external files via TouchDesigner's
  native file parameter

Your externalized files on disk are the source of truth.
The .toe file is just a convenient container -- all
externalized operators are fully recoverable from the
external files.

All file paths are normalized to forward slashes for cross-
platform compatibility between Windows and macOS.

To reset ('unexternalize'), pulse the Disable button. This
deletes only files tracked by Embody. Untracked files in
the externalization folder are preserved.

Export Portable Tox
-------------------
Export any COMP as a self-contained .tox file with all
external file references stripped. The exported .tox works
when loaded into any TouchDesigner project with no missing
file errors.

Use via the Actions menu in the Manager UI, or call
programmatically: op.Embody.ExportPortableTox(target, path)

Non-system absolute paths are warned about but not stripped.

Envoy (MCP Server)
---------------------
Embody includes Envoy, an MCP (Model Context Protocol)
server that enables AI coding assistants to interact with TouchDesigner
programmatically. When enabled, Envoy lets you:

- Create, modify, connect, and query operators
- Read and write DAT content
- Manage Embody externalizations
- Execute Python code in TouchDesigner
- Export/import networks via the TDN format

To enable: toggle the Envoyenable parameter ON. The server
starts on port 9870 by default and auto-creates a .mcp.json
file in your project root for AI coding assistants to discover.

You can regenerate Envoy config files at any time:
  op.Embody.InitEnvoy()  -- MCP + AI client config
  op.Embody.InitGit()    -- git repo + .gitignore/.gitattributes

TDN Network Format
------------------
Embody can export and import TouchDesigner networks as human-
readable .tdn JSON files. This captures operators, parameters,
connections, and layout in a diffable format.

Every externalized COMP automatically writes a .tdn sidecar
alongside its .tox. Edit the .tdn on disk; Embody (or an
AI agent via MCP) can reload it back into TD with
import_network.

Use ctrl-shift-e to export the full project, or ctrl-alt-e
to export just the current network.

Manager UI
----------
Press ctrl-shift-o to open the Manager, a TreeLister of all
externalized operators and their metadata. From here you can:
- View dirty state for each operator
- Navigate to any operator by clicking
- Open file locations in your system file browser
- Refresh, filter, and search externalizations
- Trigger Initialize/Update or Reset

Keyboard Shortcuts
------------------
ctrl-shift-o :   Open the Manager UI
ctrl-shift-u :   Update all externalizations
ctrl-alt-u :     Update only the current COMP you are inside
ctrl-shift-r :   Refresh tracking state
ctrl-shift-e :   Export the full project network to .tdn
ctrl-alt-e :     Export the current network to .tdn

'''
