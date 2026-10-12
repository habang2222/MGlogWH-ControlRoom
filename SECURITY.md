# Security model

MGlogWH records process names, paths, UIDs and other host activity. The control room reads the collector's root-owned run directory and can stage collector rules. Treat access to the dashboard as privileged access to the host's telemetry and configuration.

- Bind the web server to `127.0.0.1` only. Use an SSH tunnel for a remote Linux server; do not expose port 8765 to the public network.
- Keep `/etc/mglogwh-controlroom/token` readable only by root. Rotate the token if it is disclosed. Do not put a token in a Git repository, shell command argument, URL or screenshot.
- The dashboard is intended for a trusted operator on a single host. It is not a multi-user authorization system or a replacement for a reverse proxy with TLS and access controls.
- Rule edits target `/etc/mglogwh/config.json`. They apply on the next collector start. Restarting the collector interrupts collection briefly, so the UI requires a separate apply action.
- Downloads are restricted to known files inside a selected run. Raw files and the SQLite index can contain sensitive host data. Review them before sharing.
- Event lists show indexed data. An index lag or loss counter means the visible list may not represent every collected or attempted event. A red Critical label means a configured MGlogWH rule matched; it is not itself a threat verdict.

To report a security issue, use a private channel to the repository owner rather than opening a public issue with sensitive logs.
