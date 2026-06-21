# me-db

Knowledge base service for the [me-server](https://github.com/lidiaev/me-server) frame: Silverbullet
(web editor) plus an MCP server (markdown read/write/search for Claude) and a git-watcher that syncs the
notes to your own private repo.

Notes live in your own private git repo (`GITHUB_REPO`), cloned on start and pushed on change.

## Routes (via the frame's Caddy)
- `me-db.<DOMAIN_BASE>:8443/` — Silverbullet; `/mcp`, `/oauth/*` — MCP
- `me-db.<DOMAIN_BASE>:443/mcp` — MCP (Claude.ai connector backend)
- `auth.<DOMAIN_BASE>:8443` — OAuth browser popup

Don't create top-level Silverbullet pages named `mcp`, `oauth`, `register` or `health` — they are shadowed by MCP routes.

## Images
Prebuilt by CI to `ghcr.io/lidiaev/me-db-{mcp,watcher}` (public; the VPS only pulls). Build your own with `docker compose build`.

## Connect Claude
Add a custom connector in Claude.ai (Settings -> Connectors): URL `https://me-db.<DOMAIN_BASE>/mcp`, OAuth,
then enter the setup secret in the popup. The connector then works across Claude Code, Desktop and mobile.

## License
[MIT](LICENSE)
