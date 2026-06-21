#!/bin/bash
set -e
WATCH_DIR="${ME_DB_PATH:-/mnt/me_db}"
GIT_URL="https://github.com/${GITHUB_REPO}.git"
DEBOUNCE_SEC="${DEBOUNCE_SEC:-5}"
BRANCH="${GIT_BRANCH:-master}"

git config --global --add safe.directory "$WATCH_DIR"
git config --global user.name "me-db-bot"
git config --global user.email "me-db@local"
# Token via HTTP header, not embedded in the remote URL.
git config --global "http.https://github.com/.extraheader" \
  "Authorization: Basic $(printf 'x-access-token:%s' "${GITHUB_TOKEN}" | base64 | tr -d '\n')"

for i in $(seq 1 30); do [ -d "${WATCH_DIR}/.git" ] && break; sleep 1; done
[ ! -d "${WATCH_DIR}/.git" ] && echo "[watcher] No repo after 30s" && exit 1

cd "$WATCH_DIR"
git remote get-url origin &>/dev/null && git remote set-url origin "$GIT_URL" || git remote add origin "$GIT_URL"
echo "[watcher] Watching $WATCH_DIR (branch: $BRANCH, debounce: ${DEBOUNCE_SEC}s)"

while true; do
    inotifywait -r -e close_write,create,delete,moved_to,moved_from --exclude '\.git' --quiet "$WATCH_DIR" 2>/dev/null || continue
    while inotifywait -r -e close_write,create,delete,moved_to,moved_from --exclude '\.git' --quiet --timeout "$DEBOUNCE_SEC" "$WATCH_DIR" 2>/dev/null; do :; done
    cd "$WATCH_DIR"
    git add -A
    git diff --cached --quiet 2>/dev/null && continue
    git commit -m "auto-sync: $(date -Iseconds)"
    echo "[watcher] Committed"
    for attempt in 1 2 3; do git push origin "$BRANCH" 2>&1 && break; echo "[watcher] Push attempt $attempt failed"; sleep 3; done
done
