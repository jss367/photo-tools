# "Set up NAS" wizard for remote targets

**Date:** 2026-07-26
**Status:** Approved by Julius (design conversation, this workspace)
**Branch:** `missing-local-processing-option`

## Problem

The chained import → process → move-to-NAS workflow (spec:
`2026-07-19-import-process-move-chain-design.md`) requires a configured
remote target, and today the only way to create one is a nine-field form in
Settings → Remote targets: SSH user, host, port, NAS-side absolute path,
local mount path, local archive root, key path, bandwidth limit. Every path
is hand-typed. The two real cliffs for a non-technical user:

1. **SSH key setup happens entirely outside the app.** Vireo's rsync move
   needs passwordless key auth; a user without `~/.ssh` keys gets
   `Permission denied (publickey,password)` and no guidance.
2. **The NAS-side path is unknowable from the Mac.** The SMB mount shows
   `/Volumes/Photography`; nothing on the client says the share lives at
   `/volume1/Photography` on the NAS.

Yet nearly everything is discoverable: a walkthrough on Julius's machine
pre-filled 8 of 9 fields automatically (mount table → user/host/share/mount
path; reverse DNS → friendly Tailscale hostname; Synology convention → NAS
path candidate). The only step that fundamentally needs the user is entering
the NAS password once to install a key.

## Goal

A guided wizard that takes a user from "my NAS shows up in Finder" to a
tested, saved remote target without opening Terminal or typing an absolute
path. The existing manual form remains as the advanced/edit path.

## Decisions (agreed with Julius)

1. **Scope: full guided wizard** (not just form pre-fill) — it owns key
   generation, key installation, share location, and verification, ending in
   a green Test connection.
2. **Password in the wizard, Terminal as visible fallback.** The wizard asks
   for the NAS password in the browser and uses it once, server-side, to
   install the public key; it is never persisted or logged. An expander
   ("prefer to do this yourself in Terminal?") shows the equivalent
   `ssh-copy-id` command with a Verify button for users who won't type a
   password into an app.
3. **Verified share mapping, not heuristics.** The NAS-side path is proven
   with a nonce probe (below), because the chained move deletes local
   originals after transfer — a wrong same-named share would be
   catastrophic; a nonce match cannot be wrong.
4. **Dedicated Vireo key**, `~/.vireo/ssh/vireo_ed25519` (ed25519, mode 600,
   no passphrase), saved as the target's key path. The wizard never touches
   `~/.ssh` keys. No passphrase is a deliberate trade-off: background rsync
   jobs must run unattended; the key grants only what the NAS account
   grants, and it never leaves the machine.
5. **macOS first.** Two pieces are platform-specific: mount enumeration
   (Windows `net use` parsing slots in later without redesign) and the
   pty-driven key install (Python's `pty` is POSIX-only; a Windows port
   needs its own mechanism). Neither is built now.

## User flow

Five steps in a modal on the Settings page. Every step can be overridden
manually; automation pre-fills, the user confirms.

### Step 1 — Pick your NAS volume

`GET /api/remote-setup/mounts` enumerates network mounts by parsing `mount`
output for `smbfs` / `nfs` / `afpfs` entries. The smbfs source string
(`//julius_admin@100.80.236.59/Photography` on `/Volumes/Photography`)
yields SSH-user candidate, host, share name, and local mount path. IP hosts
are reverse-resolved to a friendly name when possible (Tailscale MagicDNS,
mDNS, DNS) and the name is preferred, with the IP shown as detail.

- One mount → preselected. Multiple → list. Zero → guidance to connect the
  share in Finder (⌘K) first, with a Refresh button.

### Step 2 — Connect over SSH

Pre-filled user/host/port. On entry the wizard probes TCP reachability of
the port (`POST /api/remote-setup/ssh-check`); a closed port shows
brand-aware help (Synology: "Control Panel → Terminal & SNMP → Enable
SSH"), detected best-effort from the mount metadata/hostname, with a
generic fallback. The same endpoint reports whether the Vireo key already
exists and whether key auth already succeeds (idempotent re-runs skip
ahead).

The password form explains exactly what will happen ("used once to
authorize this Mac's key, never stored"). Submitting calls
`POST /api/remote-setup/install-key`, which:

1. Generates `~/.vireo/ssh/vireo_ed25519` if missing.
2. Installs the public key by driving `ssh-copy-id` through a pty (macOS
   ships no `sshpass`; Python's `pty` module answers the password prompt).
   The password lives only in request scope; it is never written to config,
   DB, or logs.
3. Accepts the host key on first contact (`accept-new`) and returns the
   fingerprint for display.
4. Verifies `BatchMode=yes` login now succeeds.

The Terminal fallback expander shows the `ssh-copy-id -i … user@host`
command and a Verify button that re-runs the ssh-check until key auth
passes.

### Step 3 — Locate the share on the NAS (automatic, verified)

`POST /api/remote-setup/locate-share`:

1. Write a nonce file `.vireo-probe-<random hex>` into the *local mount*.
2. Over SSH, test candidate directories for that exact filename:
   `/volume*/<share>`, `/share/<share>`, `/mnt/*/<share>`, plus the share
   name under common export roots.
3. The candidate containing the nonce is the verified `remote_path`.
4. Delete the nonce (via the mount; best-effort cleanup over SSH too).

No match → fall back to a minimal SSH-backed remote directory browser
(list directories via the established connection), then manual entry.
Failure to *write* the nonce (read-only mount) surfaces as its own error —
the move workflow needs a writable share anyway.

### Step 4 — Local archive root

Reuse the import page's folder-browser component. Suggest
`~/Pictures/Vireo Archive` with an inline "create it" action. Plain-English
explanation: "Photos you import here are processed on this Mac, then moved
to the NAS." Show free space for the containing volume. Validation mirrors
`_coerce_remote_target`: absolute, not inside the mount path.

### Step 5 — Review and test

Show the assembled target (name defaulted from the friendly hostname or
share; `bwlimit_kbps` stays 0 — the wizard adds no field for it, the
manual form covers changing it later). Auto-run the existing Test
connection check (ssh login, GNU rsync presence, mount writability).
Green → Save appends to `config.remote_targets` through the existing
settings config-save path and `_coerce_remote_target` validation — no new
save endpoint. Any failure links back to the relevant step.

## Entry points

- Settings → Remote targets: primary button **"Set up from mounted
  volume…"**; the existing manual form stays, presented as the
  advanced path (also used for editing saved targets).
- Import page: the "Move to NAS unavailable: the destination is not inside
  any remote target's local archive root…" hint gains a link that opens
  Settings at the wizard.

## Backend

Four new endpoints in `app.py` under `/api/remote-setup/`:

| Endpoint | Method | Purpose |
|---|---|---|
| `mounts` | GET | Enumerate network mounts, parsed + reverse-resolved |
| `ssh-check` | POST | Port reachability, key existence, key-auth status |
| `install-key` | POST | Generate key if needed, install via pty, verify |
| `locate-share` | POST | Nonce probe, return verified remote path |

Constraints:

- **Loopback guard:** `install-key` refuses (403) when the request does not
  arrive on a loopback address, so the password can never transit a LAN
  even if the app is bound wide.
- **Injectable runner:** all `ssh`/`ssh-keygen`/`ssh-copy-id` invocations go
  through one injectable command-runner seam (module `remote_setup.py`,
  pattern matching `move.py`'s rsync wrapper) so unit tests never need a
  real NAS.
- Timeouts on every network call; endpoints are synchronous (each step is
  seconds, not minutes — no job/SSE machinery).

## Error handling (first-class cases)

| Failure | Treatment |
|---|---|
| SSH port closed | Brand-aware enable-SSH instructions, Retry button |
| Wrong password | Clean retry, distinguished from key-install failure |
| Password auth disabled on NAS | Detected from ssh output; point at the Terminal fallback / NAS console |
| Nonce not found in any candidate | Remote directory browser fallback, then manual entry |
| Read-only mount | Explicit error naming the mount; move workflow requires write access |
| No GNU rsync / OpenSSH locally | Reuse the existing Test-connection warnings and Settings > Paths pointer |

## Testing

- **Mount parsing:** fixture strings for smbfs (user@host/share, IP and
  hostname, spaces and URL-encoding in share names, IPv6 brackets), nfs
  (`host:/export`), afpfs; non-network mounts filtered out.
- **locate-share:** fake runner returning candidate hits/misses; nonce
  cleanup on success, failure, and exception paths.
- **install-key:** pty driver against a stub script that imitates
  ssh-copy-id prompts (password prompt, wrong password, host-key prompt);
  assert the password never appears in logs or config.
- **Endpoints:** validation errors, loopback guard, idempotent re-entry
  (key already installed → ssh-check short-circuits).
- **Wizard e2e:** existing e2e harness with the runner mocked at the Flask
  layer; walks all five steps including the Terminal-fallback branch.

## Out of scope (YAGNI)

- Windows/Linux mount enumeration (design slot exists; not built now).
- Multiple keys per target, passphrase-protected keys, ssh-agent
  integration.
- Editing existing targets through the wizard (manual form covers it).
- NAS-brand API integrations (Synology API etc.) — SSH + nonce is
  brand-agnostic.
