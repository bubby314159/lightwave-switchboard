#!/usr/bin/env bash
#
# create_and_push_private_repo_ssh.sh
#
# Creates (or reuses) a private GitHub repository, writes a README if needed,
# commits the current directory, and pushes the code to an auto-generated
# setup branch over SSH.
#
# GitHub API authentication:
#   $GITHUB_TOKEN       Required for GitHub API calls only.
#
# Git authentication:
#   SSH key configured by create_ssh_key.sh
#
# Optional:
#   $GITHUB_COLLABORATOR
#   $GIT_NAME
#   $GIT_EMAIL
#
# Usage:
#   export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
#   export GITHUB_COLLABORATOR=some-username
#   ./create_and_push_private_repo_ssh.sh
#

set -euo pipefail

# ---------- configuration ----------

CODE_DIR="."
TOKEN="${GITHUB_TOKEN:-}"
COLLABORATOR="${GITHUB_COLLABORATOR:-}"

GIT_NAME="${GIT_NAME:-}"
GIT_EMAIL="${GIT_EMAIL:-}"

BRANCH="setup/$(date -u +%Y%m%d-%H%M%S)"

# ---------- prerequisites ----------

command -v git >/dev/null 2>&1 || {
  echo "Error: git is not installed."
  exit 1
}

command -v curl >/dev/null 2>&1 || {
  echo "Error: curl is not installed."
  exit 1
}

command -v ssh >/dev/null 2>&1 || {
  echo "Error: ssh is not installed."
  exit 1
}

if [ -z "$TOKEN" ]; then
  echo "Error: GITHUB_TOKEN is not set."
  echo
  echo "Set it with:"
  echo "  export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx"
  echo
  echo "The token is used only for GitHub API calls."
  echo "Git itself uses SSH."
  exit 1
fi

cd "$CODE_DIR"

# ---------- derive repository information ----------

REPO_NAME="$(basename "$(pwd)" | sed -E 's/[^A-Za-z0-9._-]+/-/g')"

echo "==> Repo name: $REPO_NAME"
echo "==> Branch:    $BRANCH"

# ---------- check SSH access ----------

echo "==> Checking SSH access to GitHub..."

SSH_TEST_OUTPUT="$(
  ssh \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new \
    -T git@github.com 2>&1 || true
)"

if ! echo "$SSH_TEST_OUTPUT" | grep -q "successfully authenticated"; then
  echo
  echo "Error: SSH authentication to GitHub failed."
  echo
  echo "$SSH_TEST_OUTPUT"
  echo
  echo "Run create_ssh_key.sh first."
  exit 1
fi

echo "==> SSH authentication OK."

# ---------- get authenticated GitHub user ----------

echo "==> Fetching authenticated GitHub user..."

USER_JSON="$(
  curl -fsS \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    https://api.github.com/user
)"

GH_USER="$(
  echo "$USER_JSON" |
    grep -m1 '"login"' |
    sed -E 's/.*"login": *"([^"]+)".*/\1/'
)"

if [ -z "$GH_USER" ]; then
  echo "Error: could not determine authenticated GitHub user."
  echo "$USER_JSON"
  exit 1
fi

echo "==> Authenticated as: $GH_USER"

# ---------- determine Git email ----------

if [ -z "$GIT_EMAIL" ]; then
  # Try to obtain the GitHub account's public email.
  GITHUB_EMAIL="$(
    echo "$USER_JSON" |
      grep -m1 '"email"' |
      sed -E 's/.*"email": *"([^"]*)".*/\1/' || true
  )"

  if [ -n "$GITHUB_EMAIL" ] && [ "$GITHUB_EMAIL" != "null" ]; then
    GIT_EMAIL="$GITHUB_EMAIL"
  else
    GIT_EMAIL="${GH_USER}@users.noreply.github.com"
  fi
fi

if [ -z "$GIT_NAME" ]; then
  GIT_NAME="$GH_USER"
fi

echo "==> Git identity: $GIT_NAME <$GIT_EMAIL>"

# ---------- create or reuse GitHub repository ----------

echo "==> Checking whether repository already exists..."

REPO_RESPONSE="$(
  curl -sS \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${GH_USER}/${REPO_NAME}"
)"

SSH_URL="$(
  echo "$REPO_RESPONSE" |
    grep -m1 '"ssh_url"' |
    sed -E 's/.*"ssh_url": *"([^"]+)".*/\1/' || true
)"

if [ -n "$SSH_URL" ]; then

  echo "==> Repository already exists."
  echo "==> Reusing: $SSH_URL"

else

  echo "==> Repository does not exist."
  echo "==> Creating private repository '$REPO_NAME'..."

  CREATE_RESPONSE="$(
    curl -sS -X POST \
      -H "Authorization: Bearer $TOKEN" \
      -H "Accept: application/vnd.github+json" \
      https://api.github.com/user/repos \
      -d "{\"name\":\"$REPO_NAME\",\"private\":true}"
  )"

  SSH_URL="$(
    echo "$CREATE_RESPONSE" |
      grep -m1 '"ssh_url"' |
      sed -E 's/.*"ssh_url": *"([^"]+)".*/\1/' || true
  )"

  if [ -z "$SSH_URL" ]; then
    echo
    echo "Error: repository creation failed."
    echo
    echo "$CREATE_RESPONSE"
    exit 1
  fi

  echo "==> Repository created: $SSH_URL"

fi

# ---------- write README ----------

if [ -f "README.md" ]; then

  echo "==> README.md already exists — leaving it unchanged."

else

  echo "==> Writing README.md..."

  cat > README.md <<EOF
# ${REPO_NAME}

## Repository

Private GitHub repository for ${REPO_NAME}.

## SSH

Verify GitHub SSH access with:

\`\`\`bash
ssh -T git@github.com
\`\`\`

## Clone

\`\`\`bash
git clone ${SSH_URL}
cd ${REPO_NAME}
\`\`\`

## Setup branch

Code from this upload is pushed to:

\`${BRANCH}\`

## Security

Never commit:

- Private SSH keys
- GitHub tokens
- Passwords
- API keys
- Certificates containing private keys
- Other secrets

Check \`.gitignore\` before committing.
EOF

fi

# ---------- initialize Git repository ----------

if [ -d ".git" ]; then

  echo "==> Git repository already initialized."

else

  echo "==> Initializing Git repository..."

  git init

fi

# ---------- configure Git identity ----------

echo "==> Configuring Git identity..."

git config user.name "$GIT_NAME"
git config user.email "$GIT_EMAIL"

# ---------- configure SSH remote ----------

if git remote get-url origin >/dev/null 2>&1; then

  CURRENT_REMOTE="$(git remote get-url origin)"

  echo "==> Existing origin: $CURRENT_REMOTE"
  echo "==> Setting origin to: $SSH_URL"

  git remote set-url origin "$SSH_URL"

else

  echo "==> Adding origin: $SSH_URL"

  git remote add origin "$SSH_URL"

fi

# ---------- stage files ----------

echo "==> Staging files..."

git add .

# ---------- commit files ----------

if git diff --cached --quiet; then

  echo "==> Nothing new to commit."

else

  echo "==> Creating commit..."

  git commit -m "Initial commit"

fi

# ---------- create/check branch ----------

echo "==> Switching to branch '$BRANCH'..."

git checkout -B "$BRANCH"

# ---------- push ----------

echo "==> Pushing branch '$BRANCH' to GitHub over SSH..."

git push -u origin "$BRANCH"

echo
echo "============================================================"
echo "SUCCESS"
echo "============================================================"
echo
echo "Repository:"
echo "  https://github.com/${GH_USER}/${REPO_NAME}"
echo
echo "Branch:"
echo "  ${BRANCH}"
echo
echo "SSH:"
echo "  ${SSH_URL}"
echo

# ---------- collaborator ----------

if [ -n "$COLLABORATOR" ]; then

  echo "==> Inviting collaborator '$COLLABORATOR'..."

  INVITE_RESPONSE="$(
    curl -sS -X PUT \
      -H "Authorization: Bearer $TOKEN" \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/${GH_USER}/${REPO_NAME}/collaborators/${COLLABORATOR}"
  )"

  # A successful collaborator invitation normally returns a JSON object
  # containing an invitation ID. Handle both invitation and already-member
  # responses without treating the latter as a fatal error.

  if echo "$INVITE_RESPONSE" | grep -q '"id"'; then

    echo "==> Collaborator invitation sent."

  elif echo "$INVITE_RESPONSE" | grep -q '"message".*"already a collaborator"'; then

    echo "==> $COLLABORATOR is already a collaborator."

  else

    echo "Warning: collaborator invitation may have failed."
    echo "$INVITE_RESPONSE"

  fi

else

  echo "==> No GITHUB_COLLABORATOR set — skipping collaborator invite."

fi

echo
echo "==> Done."
