#!/usr/bin/env bash
# Rebuild the static frontend against a given tunnel URL and publish it to
# the gh-pages branch (GitHub Pages source: branch gh-pages, path /).
#
#   deploy/deploy_pages.sh https://<something>.trycloudflare.com
#
# Added 2026-09-20 for the public-demo deployment (see PROJECT_STATUS.md,
# "Public demo"). Why a script rather than a GitHub Actions workflow: pushing
# a workflow file needs the `workflow` OAuth scope on the gh token, which
# would mean another interactive login; publishing a plain branch needs only
# the `repo` scope already granted. Trade-off: the build happens on this
# machine, not in CI -- fine, because this machine has to be on for the demo
# to work at all.
#
# WHY THE API URL IS AN ARGUMENT: a Cloudflare *quick* tunnel gets a NEW
# random URL every time cloudflared restarts, and the static frontend bakes
# the API URL in at build time. So a restarted tunnel means: new URL ->
# re-run this script with it. (A named tunnel on a domain you own would give
# a stable URL and remove this step -- see PROJECT_STATUS.md.)
set -euo pipefail

API_URL="${1:?usage: deploy/deploy_pages.sh https://<tunnel-host>}"
REPO_NAME="api-migration-agent"
REMOTE="https://github.com/GarvBardia/${REPO_NAME}.git"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/frontend"

# If this fails with "Device or resource busy": some process has its working
# directory inside frontend/out (it happened when cloudflared was started
# from a shell that had cd'd into it). Start long-running processes from the
# project root, not from inside a build output directory.
rm -rf .next out

# MSYS_NO_PATHCONV=1: Git Bash otherwise rewrites the leading-slash value
# "/api-migration-agent" into "C:/Program Files/Git/api-migration-agent" and
# the build fails ("basePath has to start with a /").
MSYS_NO_PATHCONV=1 \
NEXT_PUBLIC_API_BASE_URL="$API_URL" \
NEXT_PUBLIC_BASE_PATH="/${REPO_NAME}" \
  npm run build

# Without .nojekyll GitHub Pages runs Jekyll, which silently drops every
# directory starting with an underscore -- i.e. all of /_next/, leaving a page
# that loads its HTML but none of its JS/CSS.
touch out/.nojekyll

cd out
rm -rf .git
git init -q -b gh-pages
git add -A
git commit -q -m "Deploy static frontend (API: ${API_URL})"
git push -f "$REMOTE" gh-pages
cd ..
rm -rf out/.git

echo "Pushed. Pages: https://garvbardia.github.io/${REPO_NAME}/"
