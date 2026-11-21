# Security policy

## Reporting a vulnerability

Please report it privately, not in a public issue: use GitHub's **Report a vulnerability** button on the repository's Security tab (private vulnerability reporting). Say what you found, how to reproduce it and which version (Settings → About shows it). Don't include real passwords, API keys or personal data.

You can expect an acknowledgement within a few days. A fix reaches the `stable` branch, which Usher updates itself from, as soon as it is merged, and the next release's notes say what was fixed once people have had the chance to update.

## Supported versions

Only the latest release is supported; a server that builds from source updates itself from the `stable` branch, ahead of the next release.

## What is in scope

How Usher protects accounts, sessions and the services behind it is described in [docs/security.md](docs/security.md), including the threat model.
