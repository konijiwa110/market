# konijiwa-plugin

Personal Claude Code plugin marketplace.

## Add this marketplace

```
/plugin marketplace add konijiwa110/market
```

Or from the terminal:

```
claude plugin marketplace add konijiwa110/market
```

### Troubleshooting: "Host key verification failed"

If `marketplace add` errors with `SSH host key is not in your known_hosts` / `No ED25519 host key is known for github.com` / `Host key verification failed`, the machine is cloning over **SSH** (`git@github.com`) and has never accepted GitHub's host key. The trailing `make sure you have the correct access rights` line is generic git boilerplate — this is **not** an access-rights problem. This is a public repo, so use HTTPS instead:

```
/plugin marketplace add https://github.com/konijiwa110/market.git
```

If the explicit HTTPS URL is *still* rewritten to SSH, a global git config is forcing the rewrite. Check and remove it:

```
git config --global --get-regexp insteadOf
git config --global --unset url.git@github.com:.insteadOf
```

(Alternatively, to keep using SSH, register GitHub's host key once: `ssh-keyscan -t ed25519,rsa github.com >> ~/.ssh/known_hosts`.)

## Plugins

| Plugin | Description |
|--------|-------------|
| [`rolling-context`](./plugins/rolling-context) | Rolling context compression with third-party baseURL support. Old messages get summarized, recent messages stay verbatim. |

## Install a plugin

```
/plugin install rolling-context@konijiwa-plugin
```
