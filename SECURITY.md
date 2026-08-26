# Security policy

## Supported versions

Security fixes are applied to the latest released minor version. Older builds
should be upgraded before a report is investigated.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or include private
files, access tokens, database copies or public share links in an issue.

Use the repository's private security-advisory channel. Include the affected
version, deployment mode, reproduction steps, expected impact and any safe
proof of concept. If private reporting is unavailable, open a minimal issue
asking the maintainer to enable a private channel without publishing details.

## Deployment boundary

NAS AI Space is designed for a trusted LAN by default. Do not expose port 8766
directly to the internet. Remote access requires an HTTPS reverse proxy and a
restricted source network or VPN. Keep `.env`, SQLite files, backups, model
data and Docker Socket access readable only by the service administrator.

The optional operations sidecar can restart allow-listed containers and change
their memory limits through Docker Socket. Enable it only when this feature is
needed and do not publish its internal port.
