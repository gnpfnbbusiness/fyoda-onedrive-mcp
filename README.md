# fyoda-onedrive-mcp — MCP server (read + guarded write) for a PERSONAL OneDrive

Two modes: **local** (stdio, sections 1–3) and **hosted web connector for claude.ai** (section 4).

Why: the built-in Microsoft 365 connector only supports work/school accounts. This server signs in to a
**personal Microsoft account** (e.g. an @gmail.com account used for OneDrive) through Microsoft Graph.

Safety: file content is flagged as untrusted data; downloads never overwrite; local token cache is 0600.
Writes are OFF unless `ONEDRIVE_WRITE_PREFIXES` names the folder(s) Claude may write to (e.g. `/FYODA`). Creating a file
needs no extra step; replacing one requires `overwrite=true` AND `if_match=<current eTag>`, so a stale copy can never
overwrite a newer file. There is no delete. `ONEDRIVE_READONLY=1` removes write tools and requests only `Files.Read`.

## 1. Register the app (once, about 5 minutes — done by you in a browser)
1. Sign in at https://entra.microsoft.com with the personal Microsoft account → **App registrations → New registration**.
   (If Microsoft asks you to create a free directory/Azure account first, accept the free one; no paid resource is needed.)
2. Name: `fyoda-onedrive-mcp`. Supported account types: **Personal Microsoft accounts only**. Register.
3. **Authentication → Add a platform → Mobile and desktop applications**, tick the `nativeclient` redirect URI, then
   set **Allow public client flows = Yes**. Save.
4. **API permissions → Add → Microsoft Graph → Delegated → Files.Read and Files.ReadWrite** (read-only users: Files.Read only) (offline_access is added automatically).
5. Copy the **Application (client) ID**. It is not a secret.

## 2. Install and sign in
    pip install .            # Python 3.10+
    export ONEDRIVE_CLIENT_ID=<client id>
    python -m fyoda_onedrive_mcp login    # shows a code; open microsoft.com/devicelogin and approve

## 3. Use with Claude
Claude Code:   claude mcp add onedrive-personal -e ONEDRIVE_CLIENT_ID=<client id> -- python -m fyoda_onedrive_mcp
Claude Desktop (claude_desktop_config.json):
    {"mcpServers":{"onedrive-personal":{"command":"python","args":["-m","fyoda_onedrive_mcp"],"env":{"ONEDRIVE_CLIENT_ID":"<client id>"}}}}
This is a local (stdio) server, so it works on the computer where it is installed. A claude.ai web connector would need
a hosted HTTPS server with OAuth; that is not included.

CLI for quick checks: `whoami`, `ls /`, `search "FYODA Program Control"`, `meta <ref>`, `read <ref>`, `download <ref> [dir]`.
`<ref>` = `/folder/file.xlsx`, an item id, or an OneDrive sharing URL.

## 4. Hosted web connector (claude.ai → Settings → Connectors → Add custom connector)
Needs an always-on HTTPS URL. `render.yaml` is a DRAFT blueprint for your Render workspace (free plan sleeps when idle).
1. In the same Entra app add a **Web** platform redirect URI `https://<your-host>/oauth/microsoft/callback`, and create a
   **client secret** (Certificates & secrets). Put it only in the host's secret settings.
2. Set the environment variables listed in `render.yaml` (SERVER_SECRET = any long random string, Render can generate it; ALLOWED_ACCOUNT = your personal account).
3. Deploy, then add `https://<your-host>/mcp` as a custom connector in Claude and approve the Microsoft sign-in.
How it works: Claude uses standard MCP OAuth (dynamic client registration + PKCE) against this server; the server signs you
in to Microsoft and keeps no database — client ids, codes and tokens are sealed with SERVER_SECRET and carry the Microsoft
refresh token. Rotating SERVER_SECRET disconnects everything. Only ALLOWED_ACCOUNT can sign in. Claude's own tool-approval
prompts still apply to write tools (marked destructive).

## Tests
`pip install pytest && python -m pytest -q` — 25 tests with Microsoft/Graph stubbed, including the full OAuth flow, the
single-account check and every write guard. **Not yet run against a real Microsoft account, a real deployment or claude.ai.**
Notes: `.xlsx` reads show cached values (formulas not recalculated); writes are plain file uploads (<= 4 MB), not cell edits;
needs `mcp<2`. Free-tier hosts sleep and lose in-memory caches, which is harmless here (tokens are re-derived).
