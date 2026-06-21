#!/bin/bash
set -e
export ME_DB_PATH="${ME_DB_PATH:-/mnt/me_db}"
BRANCH="${GIT_BRANCH:-master}"
GIT_URL="https://github.com/${GITHUB_REPO}.git"
git config --global user.name "me-db-bot"
git config --global user.email "me-db@local"
git config --global "http.https://github.com/.extraheader" \
  "Authorization: Basic $(printf 'x-access-token:%s' "${GITHUB_TOKEN}" | base64 | tr -d '\n')"

if [ ! -d "${ME_DB_PATH}/.git" ]; then
    TEMP_CLONE=$(mktemp -d)
    if git clone "$GIT_URL" "$TEMP_CLONE" 2>&1; then
        find "${ME_DB_PATH}" -mindepth 1 -maxdepth 1 -not -name '.git' -exec rm -rf {} \; 2>/dev/null || true
        cp -a "$TEMP_CLONE"/. "$ME_DB_PATH"/
        rm -rf "$TEMP_CLONE"
        echo "[entrypoint] Clone OK"
    else
        rm -rf "$TEMP_CLONE"
        echo "[entrypoint] Clone FAILED — starting in read-only mode" >&2
    fi
else
    UNPUSHED=$(git -C "$ME_DB_PATH" log "origin/${BRANCH}..HEAD" --oneline 2>/dev/null | wc -l)
    if [ "$UNPUSHED" -gt 0 ]; then
        echo "[entrypoint] WARNING: $UNPUSHED unpushed commits — pushing before reset"
        git -C "$ME_DB_PATH" push origin "$BRANCH" 2>&1 || echo "[entrypoint] WARN: push failed" >&2
    fi
    git -C "$ME_DB_PATH" fetch origin "$BRANCH" 2>&1 && \
        git -C "$ME_DB_PATH" reset --hard "origin/${BRANCH}" 2>&1 || echo "[entrypoint] Sync FAILED" >&2
fi

exec python /app/mcp-server.py
