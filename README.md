# gh-app-agent

A reproducible starter for letting **a local coding agent** (or any local tool, or just plain you) act on your behalf on GitHub via a **scoped GitHub App** — without putting your personal Personal Access Token, OAuth login, or `gh` credentials on the machine where the agent runs. Agent-agnostic: nothing here is specific to any particular agent or vendor.

The agent ends up with the ability to clone your repos, push feature branches, and open draft PRs. It does **not** have admin powers (cannot delete/rename repos, change visibility, or modify settings), it cannot reach repos in work organizations you're a member of, and every token it uses expires after one hour.

## What this is, in three sentences

1. You register a GitHub App against your personal account; the App has narrow permissions (`Contents: write`, `Pull requests: write`, `Metadata: read`) and is installed only on the repos you pick.
2. The App's private key lives on your local disk in `apps/<name>/` (gitignored, `chmod 600`); a small Python helper mints short-lived (1-hour) installation tokens on demand by signing a JWT with that key.
3. Git's credential helper and a `gh` wrapper plug into the helper, so `git clone`, `git push`, and `gh pr create` "just work" — they're authenticated as the App, not as you.

## What you'll end up with

- **Scope**: App can write to feature branches and open PRs. Cannot change repo settings, delete repos, or reach repos in any org/account other than where the App is installed.
- **Secrets stored locally only**: `private-key.pem`, `config.env`, and the token cache live in `apps/<name>/`, gitignored. Nothing sensitive is ever committed.
- **Branch protection caveat**: GitHub's permissions are coarse; `Contents: write` does technically permit pushing to `main`. The actual constraint preventing that is **branch protection rules** you set on each repo's default branch. The setup steps include this — don't skip it.
- **Reproducible**: One command (`bin/register-app.py --name <name>`) does the App registration, key download, install, and ID capture. Re-runnable on other machines or for friends, each producing a separate App tied to that person's account.

## Prerequisites

- macOS or Linux
- Python 3.9+
- `git`
- `gh` CLI (optional but recommended for `gh-agent`)
- A GitHub account you're logged into in your default browser

## Setup

```bash
# 1. Clone this repo as your local agent directory
git clone https://github.com/<author>/gh-app-agent.git ~/.github-agent
cd ~/.github-agent

# 2. Create a Python venv with the helper's dependencies
python3 -m venv venv
venv/bin/pip install --upgrade pip pyjwt cryptography requests

# 3. Make the helpers executable
chmod +x bin/* .githooks/pre-commit

# 4. (Optional, recommended) Opt in to the secret-blocking pre-commit hook
git config core.hooksPath .githooks

# 5. Register the App. Opens your browser twice for consent.
bin/register-app.py --name <your-app-name>
# Suggested name pattern: <your-handle>-agent
# (Must be globally unique on GitHub. Letters, digits, dashes, underscores.)
```

The `register-app.py` flow:

1. Opens `http://localhost:8765/start` → auto-submits the App manifest to GitHub.
2. You click **"Create GitHub App from manifest"** on github.com (this is browser confirmation #1; GitHub requires it).
3. GitHub redirects back; the script saves `apps/<name>/private-key.pem` and `apps/<name>/config.env`.
4. Browser navigates to the install page; you pick "All repositories" or "Select repositories" and click **Install** (browser confirmation #2).
5. GitHub redirects back; the script appends `GITHUB_INSTALLATION_ID` to `config.env` and creates `apps/default` as a symlink to your new app.

> ⚠ The App registers against whichever GitHub account is logged in to your default browser. Check before clicking through — there's no way to recover from this except deleting the App and starting over.

Then wire up git's credential helper, scoped to your personal account so it doesn't intercept work-org repos:

```bash
# 6. Route HTTPS git operations on YOUR repos through the credential helper.
# Replace <your-handle> with your GitHub username.
git config --global --unset-all "credential.https://github.com/<your-handle>.helper" 2>/dev/null
git config --global --add "credential.https://github.com/<your-handle>.helper" ""
git config --global --add "credential.https://github.com/<your-handle>.helper" "$HOME/.github-agent/bin/git-credential-github-app"
git config --global "credential.https://github.com/<your-handle>.useHttpPath" true
```

Finally — and this is the actual safety net, not just a nice-to-have — set branch protection on `main` of each repo you'll work in. Go to **Settings → Branches → Add branch protection rule** (or **Settings → Rules → Rulesets**) and enable:

- ✅ Require a pull request before merging
- ✅ Do not allow bypassing the above settings

Without this, the App's `Contents: write` permission can technically push directly to `main`. With this, GitHub refuses the push regardless of who's pushing.

## Verification

Run these in order; each should succeed.

```bash
# Print a fresh installation token. Run twice — second run is instant (cache hit).
bin/mint-token.py

# List every repo the App can see.
TOKEN=$(bin/mint-token.py)
curl -sH "Authorization: Bearer $TOKEN" \
     -H "Accept: application/vnd.github+json" \
     https://api.github.com/installation/repositories | jq '.repositories[].full_name'

# Clone, branch, push, draft PR.
git clone https://github.com/<your-handle>/<some-private-repo>.git /tmp/agent-test
cd /tmp/agent-test
git checkout -b agent-test-branch
echo "test" > .agent-test
git add .agent-test
git commit -m "agent test"
git push -u origin agent-test-branch
~/.github-agent/bin/gh-agent pr create --draft \
    --title "Agent test" --body "Verifying agent setup."

# Confirm main is protected (this should FAIL):
git push origin agent-test-branch:main

# Cleanup
~/.github-agent/bin/gh-agent pr close <pr-number>
git push origin --delete agent-test-branch
rm -rf /tmp/agent-test
```

## Day-to-day usage

- **`git clone/fetch/push`** to your repos: just works, via the credential helper.
- **`gh` commands** as the App: prefix with `bin/gh-agent`. The wrapper sets `GH_TOKEN` to a freshly-minted installation token, then execs `gh`.
- **Switching active App** (if you have multiple): `export GH_AGENT_APP=<other-app-name>` in the shell, or update the `apps/default` symlink: `ln -sfn <name> apps/default`.
- **Rotating the private key**: GitHub UI → Settings → Developer settings → GitHub Apps → your app → Private keys → Generate a new key, then delete the old one. Replace `apps/<name>/private-key.pem` locally with the new one (same `chmod 600`).
- **Uninstalling**: GitHub UI → Settings → Applications → Installed GitHub Apps → Configure → Uninstall. Locally, remove `apps/<name>/`.
- **Updating the scripts**: `git -C ~/.github-agent pull`. Your `apps/` directory is gitignored, so `pull` never touches your local credentials.

## Adding more apps

You can register additional Apps under the same install (e.g., one per project, one for a friend's account, etc.):

```bash
bin/register-app.py --name <new-app-name>
# Switch active App per shell:
export GH_AGENT_APP=<new-app-name>
# Or change the default symlink:
ln -sfn <new-app-name> apps/default
```

Each App's credentials live in its own `apps/<name>/` directory and are independent.

## Using one App from multiple machines

`register-app.py` always creates a *new* App, so don't re-run it on the second machine. Instead, share the existing App's identity (`config.env` + a private key). Two approaches:

- **Separate keys per machine (recommended).** On github.com → Settings → Developer settings → GitHub Apps → your app → Private keys → **Generate a new key**. On machine 2, do the normal Setup steps 1–4, then manually create `apps/<name>/` containing the new `.pem` (`chmod 600`) and a copy of `config.env` from machine 1. Each machine has an independently revocable key.
- **Shared key.** Copy `apps/<name>/` from machine 1 to machine 2 verbatim. Simpler, but revoking the key kills both.

In both cases, finish with the credential-helper config (Setup step 6) and optionally `ln -sfn <name> apps/default`. Each machine maintains its own `.token-cache.json`; no runtime coordination needed.

## Contributing

PRs welcome. To work on the scripts:

1. Fork and clone normally (not as `~/.github-agent`).
2. Make changes in `bin/`, `.githooks/`, etc.
3. Test locally by symlinking your fork into `~/.github-agent` and running `register-app.py` against a throwaway App.

**No-secrets policy**: nothing in `apps/<name>/` should ever appear in `git status` as a staged file. If `git ls-files | xargs grep -lE 'BEGIN.*PRIVATE KEY|gh[sopur]_[A-Za-z0-9]{30,}'` finds anything, that's a bug. The `.gitignore` and the opt-in pre-commit hook together should make this impossible to do by accident.

## Trust model / security notes

Worth being explicit:

- **The private key on disk is the highest-value secret here.** Anyone who can read `apps/<name>/private-key.pem` can mint installation tokens until the App is deleted on GitHub. Mitigations: `chmod 600` (the scripts enforce this), `.gitignore` keeps it out of any repo, and you can revoke instantly via GitHub UI → Settings → Developer settings → GitHub Apps → your app → Delete app. Deleting the App invalidates every key and installation it ever had.
- **Installation tokens are short-lived (1 hour).** Even if a token leaks, the blast radius is ≤ 1 hour. The cache file `apps/<name>/.token-cache.json` holds the most recent token; treat it as sensitive (`chmod 600`, gitignored).
- **GitHub App permissions are coarse.** `Contents: write` does not distinguish between "feature branches only" and "any branch including main". The only thing that prevents the App from pushing to `main` is branch protection on the repo. Treat branch protection as a required step, not optional.
- **The App is scoped to the account it's installed on.** It cannot reach repos in work organizations you're a member of, even if you can. Installations don't inherit the installer's broader access.
- **Anyone who can run code as your local user can read the private key.** This is true of any local credential. If you don't trust the machine you're installing this on, don't install this on it.

## Layout

```
~/.github-agent/                    # this repo, cloned here
├── README.md                       # ← you are here
├── LICENSE                         # MIT
├── .gitignore                      # ignores apps/*/, *.pem, *.env, etc.
├── .githooks/pre-commit            # opt-in secret-blocking hook
├── manifest-template.json          # GitHub App manifest
├── bin/
│   ├── register-app.py             # App Manifest flow driver
│   ├── mint-token.py               # mints + caches installation tokens
│   ├── git-credential-github-app   # git credential helper
│   └── gh-agent                    # gh wrapper that injects GH_TOKEN
├── venv/                           # gitignored; created in setup step 2
└── apps/                           # gitignored except .gitkeep
    ├── default -> <app-name>       # symlink; updated by register-app.py
    └── <app-name>/
        ├── config.env              # APP_ID, INSTALLATION_ID, SLUG, OWNER
        ├── private-key.pem         # chmod 600
        └── .token-cache.json       # auto-managed; {token, expires_at_epoch}
```

## License

MIT — see `LICENSE`.
