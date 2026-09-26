# AltServer-Linux

**Keep sideloaded iOS apps working, from a Linux box, with no Mac or PC involved.**

## What this is

Apps installed outside the App Store - through [AltStore](https://altstore.io) - are signed with
a free Apple developer certificate that **expires after 7 days**. Something has to re-sign them
before then, or they stop opening. Normally that something is AltServer on a Mac or Windows PC,
which has to be awake and on the same Wi-Fi when the deadline comes round.

This runs that job on a Linux machine instead - a home server, a NAS, a Raspberry Pi, a VM. Set it
up once and your sideloaded apps keep refreshing by themselves, over Wi-Fi, with nothing else
switched on.

### What you need

| | |
|---|---|
| A Linux host with Docker | Written against Ubuntu 24.04; nothing is Ubuntu-specific beyond the `apt` lines |
| An iPhone or iPad | On the same network as the host |
| A USB cable | **Once**, for the first pairing. Never needed again after that |
| An Apple ID | Ideally a secondary one - see the warning in [Setup](#setup) |
| ~30 minutes | Most of it waiting on downloads |

A free Apple ID caps you at **3 sideloaded apps**, 10 new app IDs per week, and the 7-day
certificate. A paid developer account raises those limits but is not required.

### What it will not do

- **AltJIT on iOS 17+.** Needs a personalised DDI, TSS signing and a RemoteXPC tunnel. Use
  [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) instead.
- **Pair without a cable.** Wireless pairing is an Apple-TV-only feature and is not available here.
- **Refresh while your phone is off the network.** It needs to reach the device.

### Where to go next

| You want to | Go to |
|---|---|
| Install it | **[Setup](#setup)** - seven steps, start to finish |
| Understand the design | [How the build works](#how-the-build-works) and [docs/REVIVAL.md](docs/REVIVAL.md) |
| Deploy on a Pi, or any non-amd64 host | [Deploying on your platform](#deploying-on-your-platform) |
| Run the binary without Docker | [Reference](#reference) |
| Fork it and publish your own builds | [If you fork it](#if-you-fork-it) |

---

## Fork status

> **This is a fork** of [NyaMisty/AltServer-Linux](https://github.com/NyaMisty/AltServer-Linux),
> whose last real code commit predates 2025 and whose CI had been failing on every run. The table
> below is what this fork changed - useful if you arrived from upstream or an issue thread, and
> safe to skip if you just want it running.

| | |
|---|---|
| CI | **Fixed.** Was dying at "Set up job" on every run; no binaries since 2025-03 |
| Apple sign-in | **Working**, including 2FA, team lookup, device registration and certificate issuance |
| Apple's 2026 GSA client-info block | **Fixed** and confirmed in both directions against live Apple infrastructure |
| GrandSlam `429` on connection reuse | **Fixed**; proven with a zero-credential probe |
| corecrypto build ([#111](https://github.com/NyaMisty/AltServer-Linux/issues/111)) | **Fixed.** The buildenv image is rebuildable from source again |
| iOS 26 launch crash ([#131](https://github.com/NyaMisty/AltServer-Linux/issues/131)) | Fixed in code; AltStore **installs and launches** |
| Wireless refresh | **Working.** The sockaddr-layout bug that broke every Wi-Fi connection is fixed and confirmed on a real device: profiles refresh over Wi-Fi with no cable |

---

## Setup

> **Use a secondary Apple ID if you have one.** Issue [#88](https://github.com/NyaMisty/AltServer-Linux/issues/88) documents Apple IDs being *locked* after
> trouble with the machine-identity server this relies on (anisette - explained in step 3). An app-specific password will **not** work - sideloading needs the real
> password plus a 2FA code. Free accounts cap at 3 sideloaded apps, 10 app IDs/week, 7-day certs.

> **Never run a second signing agent against the same Apple ID** - a Mac or Windows AltServer,
> Sideloadly, or Xcode. Each one revokes the other's certificate, and your apps stop opening.

### 1. Host prerequisites

The container image bundles everything AltServer itself needs. Three things must exist on the
**host**, because the stack reaches out to them:

```bash
sudo apt install -y avahi-daemon avahi-utils usbmuxd libimobiledevice-utils
```

| Host package | Why the stack needs it |
|---|---|
| `avahi-daemon` **running** | The containers bind-mount its socket and the system D-Bus socket; it does the actual mDNS publishing |
| `usbmuxd` | Owns the USB cable for step 2's one-time pairing. The stack bind-mounts `/var/run/usbmuxd` |
| `libimobiledevice-utils` | `idevice_id` / `idevicepair`, used to confirm the pairing worked |

Do **not** `systemctl enable usbmuxd` on Ubuntu - it is udev-activated and has no `[Install]`
section, so `enable` prints a confusing "unit files have no installation config" message. It starts
on its own when an iOS device is plugged in, and exits when the last one is unplugged. That is
normal and is exactly why the stack ships netmuxd for the wireless path.

```bash
systemctl is-active avahi-daemon          # expect: active
```

### 2. Pair the iPhone (USB cable, once)

Do this before deploying anything. Wireless pairing is not possible in this build -
`HAVE_WIRELESS_PAIRING` is undefined, and Apple restricts it to Apple TV.

Plug the iPhone into the host, unlock it, and tap **Trust**. If the host is a VM, pass the USB
device through to it first.

```bash
idevice_id -l                    # prints the UDID -- save it
idevicepair validate             # expect: SUCCESS
```

The pairing record lands in `/var/lib/lockdown/` on the host, which the stack mounts. Back up
**both** files together - they are not independent, and half a pairing is indistinguishable from
none:

```bash
sudo tar czf ~/lockdown-backup.tgz /var/lib/lockdown/
```

> That tarball contains a record named after your phone's UDID. `.gitignore` already refuses
> `lockdown-backup*.tgz`, but keep it out of anything you publish.

### 3. Deploy the stack

Everything else runs as one stack: AltServer, an anisette server, netmuxd for the wireless
transport, and a web UI that drives the install.

> **Anisette**, since it comes up constantly below: Apple will not accept a sign-in unless the
> request carries proof that it came from a real, consistent machine - a set of `X-Apple-*`
> headers derived from a provisioning blob Apple issues on first contact. An anisette server
> generates those. It is why sign-in can fail with a clock error, and why that identity is worth
> backing up: lose it and Apple sees a brand-new machine, which means another 2FA prompt and,
> if it happens repeatedly, the account lockouts issue [#88](https://github.com/NyaMisty/AltServer-Linux/issues/88) describes.

**Portainer -> Stacks -> Add stack -> Repository**, pointing at this repo with compose path
`deploy/altserver-stack.yml`. That path needs nothing on the host - Portainer fetches the repo
itself.

Everything else below runs files FROM this repo, so for those, get it onto the host first:

```bash
git clone --recursive https://github.com/Ben-Diehlci/altserver-linux.git && cd altserver-linux
```

Or with plain compose:

```bash
docker compose -f deploy/altserver-stack.yml up -d
```

**No host preparation beyond step 1.** It uses named volumes, the image is public, and the
AltStore IPA is fetched automatically on start, resolved from AltStore's own catalogue so it is
always current.

> **On a Raspberry Pi or anything not x86_64, stop here and read
> [Deploying on your platform](#deploying-on-your-platform) first.** The published image is
> `linux/amd64`, so the pull above fails on other architectures. It is one extra command, not a
> different procedure - then you come straight back to this step.

Then open **`http://<your-host>:8099`**.

#### Two settings that are load-bearing

- **`security_opt: apparmor=unconfined`** on the `altserver` and `altserver-web` services. Docker's
  default AppArmor profile contains no `dbus` rules at all, and AppArmor denies a mediated class it
  does not mention - so the very first call a Bonjour client makes is refused, mDNS silently fails,
  and the server is invisible to your phone. This is why the default is `unconfined` rather than
  carelessness.

  **Optional, and better:** [`deploy/apparmor/altserver-mdns`](deploy/apparmor/) is
  `docker-default` plus only the two D-Bus rules Bonjour needs - the bus handshake and
  `org.freedesktop.Avahi`. The container still cannot reach systemd, NetworkManager, or anything
  else on the bus. To switch, **in this order**:

  ```bash
  sudo bash deploy/apparmor/install.sh
  ```

  then set `ALTSERVER_APPARMOR=altserver-mdns` in the stack environment and redeploy. Order
  matters: a container requesting a profile the host kernel has not loaded **fails to start**. The
  script verifies the profile actually loaded before telling you to continue.

  Afterwards, check it took - and check from **another machine**, because a too-tight profile
  breaks mDNS silently, which is the exact failure the profile exists to fix:

  ```bash
  docker exec altserver cat /proc/self/attr/current   # expect: altserver-mdns (enforce)
  avahi-browse -rt _altserver._tcp                    # on macOS: dns-sd -B _altserver._tcp
  ```

  Expect `ptrace` denials in `dmesg` afterwards. Those are the profile working: the web UI runs
  with `pid: host`, so its process check walks every process on the box and is correctly refused
  on the ones it has no business reading.
- **`network_mode: host`** on all three of `altserver`, `netmuxd` and the web UI. mDNS is
  link-local multicast and does not cross a Docker bridge: netmuxd would never see the phone, and
  the phone would never discover `_altserver._tcp`. A `ports:` mapping cannot substitute for
  `altserver` either - it binds an **ephemeral port that differs on every start**, which is
  exactly why Bonjour discovery is load-bearing rather than a convenience. Put `altserver` on a
  bridge and it will start, log nothing wrong, pass every process check, and be invisible to your
  phone.

#### The anisette trap, if you deploy anisette separately

Every copy of the upstream docs says to mount `/home/Alcoholic/.config/anisette-v3/lib/`. That is
**wrong**. `lib/` holds only the two Apple `.so` files downloaded at first run; the machine identity -
`device.json` and the ADI provisioning blob - lives one level **up**. Mount the parent, or you
re-provision on every redeploy and collect 2FA prompts forever.

[`deploy/altserver-stack.yml`](deploy/altserver-stack.yml) already gets this right with a named
volume. The standalone [`deploy/anisette-stack.yml`](deploy/anisette-stack.yml) exists for running
anisette on its own.

#### Verify before going further

```bash
curl -fsS http://127.0.0.1:6969 | jq 'keys'     # ten X-Apple-* keys, all strings
```

**Run that once, and do not put it in a loop.** The bare root is the v1 DATA route: on a server
whose machine identity is missing it performs real provisioning against Apple, which is fine as
the one-off it is here (that is what you are verifying) and is how an Apple ID gets rate-limited
if something polls it. Anything on a timer should ask `/v3/client_info` instead, which is static.
The status page and the healthcheck already do.

The status page at `/` checks this and more. **Survive a redeploy, not just a restart** - `docker
restart` keeps the container's filesystem, so it proves nothing about persistence. Recreate the
container and confirm the identity is still there.

### 4. Install AltStore

Open **`http://<your-host>:8099/install`**, enter your Apple ID and password, and submit. The 2FA
code is prompted for **in the browser**.

That page exists because AltServer reads the 2FA code from `std::cin`, and a detached container has
no terminal. The web UI supervises the process and delivers the code to a read with no tty.

**If it says `No IPA at /data/AltStore.ipa`**, the automatic fetch in step 3 failed - usually a
network blip at first start, since it resolves the download from AltStore's live catalogue. It is
not fatal and it does not block refreshes of apps you have already installed; only a first-time
install needs the file. Confirm and retry without recreating anything:

```bash
docker logs altserver 2>&1 | grep -i "could not"
docker exec altserver fetch-altstore --dest /data/AltStore.ipa
```

A `docker restart altserver` does the same on the way up. Add `--force` to re-download a file
that is present but suspect.

> **The install page takes an Apple ID password over plain HTTP.** On a trusted LAN that is a
> considered trade-off. Anywhere else, change the `altserver-web` command to
> `["--host", "127.0.0.1", "--port", "8099"]` and reach it over an SSH tunnel:
> `ssh -L 8099:127.0.0.1:8099 you@your-host`. Never put it behind a reverse proxy or a tunnel -
> this is LAN-only by design.

Credentials go to AltServer through the **environment**, never a command line, and the install log
shown in the browser is filtered before it is rendered - enforced by
[`tests/check_redaction.py`](tests/check_redaction.py). **`docker logs` is filtered too** - the
entrypoint routes AltServer's stdout and stderr through the same `_redact` function before
anything reaches Docker's log driver, so no unredacted copy of the output exists anywhere in the
deployment.

There is one exception and it announces itself. If the filter cannot be executed at start, the
entrypoint logs `entrypoint: WARNING -- log redaction unavailable; credentials will appear in
docker logs` and then logs unfiltered *deliberately*, because a server you cannot debug is worse
than a credential in a log you already had. Grep for that line before assuming a log is clean.

#### Reading the output

| What you see | What it means |
|---|---|
| `No anisette server is configured` | `ALTSERVER_ANISETTE_SERVER` unset |
| `ALTSERVER_ANISETTE_SERVER is not a usable URL` | Missing `http://` scheme |
| `Could not reach the anisette server at ...` | Container not running, or wrong port |
| `... returned HTTP 502/404. Response body: ...` | Anisette up but unhealthy - the body is quoted for you |
| `... did not return a JSON object` | Wrong endpoint; you are getting HTML |
| `... no "X-Apple-I-MD-M" field` | Protocol mismatch - re-check step 3 |
| `-36607` / "Unable to sign you in" | Anisette identity or clock. Check NTP **on the anisette host** - its timestamp is forwarded to Apple verbatim |
| `AltServer could not find the device` | Pairing or usbmuxd, **not** mDNS at this stage |
| `Finished!` | **Not proof of success** - it prints even on failure. Read the lines above it |

### 5. Verify it actually worked

1. AltStore appears on the home screen.
2. **Settings -> General -> VPN & Device Management** -> trust the developer certificate.
3. **Open AltStore.** This is the real test - an app that installs but will not launch is the
   failure mode issue [#131](https://github.com/NyaMisty/AltServer-Linux/issues/131) described, and that is fixed in this fork.

### 6. Wireless refresh

**Already running.** netmuxd is part of the stack; there is nothing to install and nothing to
switch over. Confirmed working on iOS 26: profiles refresh over Wi-Fi with no cable attached.

This deliberately does **not** follow the advice in issue [#77](https://github.com/NyaMisty/AltServer-Linux/issues/77), and you should not apply that advice
here. netmuxd runs on its **own socket path** in a shared volume:

```yaml
command: ["--socket-path", "/run/muxd/usbmuxd", "--disable-usb", ...]
```

so the host's usbmuxd is left completely alone to handle the cable, and nothing contends for
anything. **Do not stop usbmuxd** - older guidance says to, because netmuxd binds `/var/run/usbmuxd`
by default, but this stack overrides that. AltServer is pointed at netmuxd with
`USBMUXD_SOCKET_ADDRESS` (note the spelling - the widely-copied `USBMUXD_SOCKET_ADRESS`, one D, is
silently ignored).

Requirements on the phone side, both normally already true after step 2:

| Needs to be true | How to check from the server |
|---|---|
| Phone advertises itself | `avahi-browse -rt _apple-mobdev2._tcp` shows it |
| `lockdownd` accepts network connections | port **62078** open on the phone's IP |

Port 62078 is the one that matters. The port in the mDNS TXT record is a different service and
refuses connections - which looks alarming and is not.

AltStore refreshes itself: it sets an hourly background-fetch interval and runs a
`BackgroundRefreshAppsOperation`. iOS grants that at its own discretion, so if an app ever expires
unexpectedly, that is why - not the server.

### 7. Don't lose it

```bash
sudo tar czf ~/lockdown-backup.tgz /var/lib/lockdown/
docker run --rm -v STACK_anisette-config:/data -v ~:/backup \
  alpine tar czf /backup/anisette-state.tgz /data
```

Replace `STACK` with your stack's name - `docker volume ls | grep anisette` shows the real one.
Angle-bracket placeholders are avoided in these blocks on purpose: bash reads `<` and `>` as
redirections, so a pasted `<stack>` silently creates files instead of failing.

`adi.pb` is rewritten on every request, so stop the container first if you want a clean copy.
Restoring that tarball is the **only** disaster-recovery path for the anisette identity; without it
a loss means re-provisioning and a fresh 2FA prompt.

**There is a third thing worth knowing about, which is not in either tarball.** The
`altserver-data` volume (mounted at `/data`) holds `AltServerData`, where AltServer keeps the
signing certificate and the `serverID` your phone remembers this server by. Losing it does not
cost you a 2FA prompt or a cable, but the next start mints a new identity, so AltStore treats
this as a different server and installed apps have to be re-signed. Back it up the same way:

```bash
docker run --rm -v STACK_altserver-data:/data -v ~:/backup \
  alpine tar czf /backup/altserver-data.tgz /data
```

Both tarballs contain identifying material and are already in `.gitignore`.

**Before sharing logs or a backup**, [`deploy/credential-hygiene.sh`](deploy/credential-hygiene.sh)
audits the places this deployment stores Apple credentials and device identifiers - logs, stray
files, temp directories, shell history - and can clear them. It is for your server, not for the
repo; it changes nothing about how the stack runs.

```bash
sudo bash deploy/credential-hygiene.sh
```

---

## The web UI

Reachable at `http://<your-host>:8099` once the stack is up. Everything in Setup can be driven
from here; the command lines above are for when you want to see what it is doing.

| Page | What it does |
|---|---|
| `/` | Health: anisette contract, clock, pairing, mDNS publication, process |
| `/pairing` | Guided pairing, distinguishing the "nothing shows up" cases |
| `/install` | Apple ID sign-in with 2FA entry in the browser |

Four JSON endpoints back those pages, and they are worth knowing if you script anything:

| Endpoint | Returns |
|---|---|
| `/api/status` | every check, plus `overall` and `server_overall`. `?full=1` for the complete anisette check |
| `/api/history` | recent runs for the timeline, with `age_seconds` and `stale` |
| `/api/logs` | the tail of the redacted log, with `mtime`, `age_seconds` and `stale` |
| `/api/pairing` | the pairing wizard's steps as data |

The status page also carries a **live log view**. Start it, trigger a refresh from AltStore,
and watch AltServer's own output - which is the only thing that can actually confirm a
refresh worked, since `idevice_id` and the wireless status row both use Debian's
libimobiledevice rather than the vendored copy AltServer links.

It reads a file on the volume the two containers share, written by the same redaction
filter that protects `docker logs` - so the web UI needs neither the Docker socket (which
would be root-on-host for a service that takes an Apple ID password over plain HTTP) nor a
host bind mount, and what it shows is already redacted. Polling runs only while watching is
on, and stops itself after five minutes so a forgotten tab does not poll forever.

The status page exists because **this software cannot report its own health.** avahi can report a
successful registration while publishing nothing; AltStore suppresses the one error it would raise
during a background refresh; and nearly everything is logged to stdout at info level, so
`journalctl -p err` stays empty no matter what breaks. Without an external check, the first symptom
of a dead deployment is an app that will not open, a week later.

---

## Before trusting it unattended

- **Watch it from outside.** Nothing inside AltServer reports its own health: no liveness signal,
  and `journalctl -p err` stays empty no matter what breaks. A background refresh that finds no
  server notifies nobody on either end. Point a monitor at it:

  | Channel | Healthy | Broken |
  |---|---|---|
  | `GET /api/status` | `200` | **`503`** when the SERVER is failing |
  | `GET /api/status?full=1` | `200` | `503` when ANY check fails, phone included |
  | `docker exec altserver-web python3 /opt/altserver-web/status_checks.py -q --server-only` | exits `0` | `2` = critical, `1` = degraded |

  **The status code judges the server, not whether your phone is home.** That distinction is the
  difference between a monitor you keep and one you mute: `iPhone reachability` fails whenever
  the phone is asleep, out of the house, or off Wi-Fi, and an alert that fires every time you go
  out gets silenced within a week -- which lands back at no monitoring at all, the state that let
  the 09-22 outage run for days. The response body always carries both verdicts: `overall`
  (everything, which is what the page shows) and `server_overall` (the four checks the server is
  responsible for, which is what the status code follows). Use `?full=1` if you genuinely do want
  to be paged when the phone is away.

  **Poll the plain endpoint, not `?full=1`.** Without it, the anisette check probes a static
  route that makes no contact with Apple. `?full=1` fetches the anisette server's bare root to
  verify all ten fields -- and on a server whose machine identity is missing, that route performs
  REAL PROVISIONING against Apple. Polling it would retry provisioning on every interval at
  exactly the moment your identity volume has gone missing, which is how an Apple ID gets
  rate-limited or locked. The status page asks for the full check when you open it and uses the
  safe probe for its 30-second refresh, for the same reason.

  **You do not have to set any of this up.** Every service in the stack now declares a
  healthcheck, so Portainer shows a health badge with no external tooling at all, and the
  `altserver-web` check is what runs `status_checks.py` on a schedule (every 5 minutes) -- until
  it existed, nothing ran the checks except a browser loading the page.

  **What each badge actually tests**, because "unhealthy" with no explanation is not much better
  than no badge:

  | Service | Its healthcheck asks | Red after |
  |---|---|---|
  | `altserver` | does `_altserver._tcp` resolve, browsed from outside? | ~6 min (2m x 3) |
  | `altserver-web` | `status_checks.py --server-only`: anisette, clock, advertisement, process | ~15 min (5m x 3) |
  | `netmuxd` | does its socket exist? | ~6 min (2m x 3) |
  | `anisette` | does `/v3/client_info` answer? | ~15 min (5m x 3) |

  The `altserver` window is deliberately longer than the watchdog's recovery (two misses at
  `ALTSERVER_MDNS_RECHECK_SECONDS`, so ~2 min), or it would flag an outage that is already being
  repaired. Note that `altserver-web` goes red on a WARN as well as a failure - "I cannot tell"
  must not look like "fine" - so a full `/data` volume or a missing `avahi-utils` turns it red
  without anything being wrong with the server itself. The card on the page names which check it
  was.

  The page also keeps a **history**: each healthcheck run is recorded, and the status page draws
  a per-check timeline with "last not-ok 2h ago" beside each row. The timeline shows the most
  recent 120 runs, about 10 hours; the file keeps ~17 days (`ALTSERVER_HISTORY_MAX`) and
  `/api/history` serves all of it. That is there because the
  09-22 outage could be seen but not dated -- the honest answer to "how long has this been down"
  was "between three and nine days". Every check is recorded, including the device ones.

  The health badge uses `--server-only`, which judges the server rather than whether your phone
  happens to be at home: anisette, clock agreement, the mDNS advertisement and the daemon
  process. Otherwise the container would go unhealthy every time you left the house. The page
  itself always shows everything, device checks included. Note that plain Compose
  never RESTARTS an unhealthy container; that is Swarm. The health state is for the dashboard and
  for any poller you add.

  Exit codes follow the Nagios plugin convention (`0` OK, `1` WARNING, `2` CRITICAL), so a
  monitoring agent understands them as-is. Two caveats worth knowing. **A degraded result stays
  HTTP 200**, including the case where the mDNS check cannot run at all -- over the status code
  alone that is indistinguishable from health, so anything needing that distinction must read
  `overall` from the body or use the exit code. And `status_checks.py` is not on your PATH; it
  lives inside the image, hence the `docker exec` form in the table above.

  **Confirming the watchdog is actually running**, which matters because it can be loaded and
  blind. One of these appears in `docker logs altserver` at start:

  ```
  mDNS watchdog: ON, re-checking _altserver._tcp every 60s
  mDNS watchdog: WARNING -- avahi-browse did not run, so the advertisement CANNOT be verified
  ```

  The second means `avahi-utils` is missing or avahi cannot be reached: the watchdog is there and
  can see nothing, so a dropped registration would go unnoticed exactly as before. When it acts
  you also get `mDNS watchdog: ... is NOT published; re-registering`. It needs two consecutive
  misses before doing so and never acts on an unreadable browse, so expect recovery to take about
  two intervals rather than one.

  This is not theoretical. On 2026-09-22 the mDNS advertisement was dropped and the server was
  undiscoverable for at least three days -- possibly nine, since nothing recorded it working.
  `status_checks.py` detected it correctly the whole time, but `/api/status` answered `200` and
  the script exited `0`, so every machine-readable signal said healthy. Both are fixed; the
  advertisement also re-registers itself now (see `ALTSERVER_MDNS_RECHECK_SECONDS`).
- **Two checks that cannot tell you anything.** `docker exec altserver idevice_id -l` and the
  wireless row on the status page both use **Debian's** libimobiledevice from the image's apt
  packages, not the vendored copy AltServer links. They were green throughout a bug that broke
  every wireless refresh. The only evidence that refresh works is AltServer's own log.
- **A misleading error.** Real device faults are *displayed* as "AltServer could not be found",
  because AltStore remaps them for any server that is not `isPreferred`, and this port hardcodes
  serverID `"1234567"` where Mac and Windows use a UUID. It will send you to debug mDNS when mDNS
  is fine.
- **`-d` makes things worse.** `libusbmuxd_set_debug_level(debugLogLevel - 2)` underflows, and a
  single `-d` silences the two messages that actually diagnose a netmuxd mismatch.
- **Changing the Apple ID password kills unattended refresh.** A background refresh has no way to
  present a login view, so it fails permanently until someone opens AltStore **on the phone** and
  re-enters the credentials. The phone's keychain and the stack's `ALTSERVER_APPLE_PASSWORD` are
  separate copies - updating Portainer alone is not enough.
- **Do not casually re-run the one-shot install.** The revoke-confirmation prompt is compiled out
  on Linux, so a revoke proceeds unattended and invalidates the certificate your installed apps
  depend on.

---

## Reference

### Running it directly

```
Usage:  AltServer-Linux options [ ipa-file ]
  -h  --help             Display this usage information.
  -u  --udid UDID        Device's UDID, only needed when installing IPA.
  -a  --appleID AppleID  Apple ID to sign the ipa, only needed when installing IPA.
  -p  --password passwd  Password of Apple ID, only needed when installing IPA.
  -d  --debug            Print debug output, can be used several times to increase debug level.

The following environment var can be set for some special situation:
  - ALTSERVER_ANISETTE_SERVER: (REQUIRED) URL of an anisette server, including
          the scheme, e.g. http://127.0.0.1:6969
          ... (four more entries; see the Environment table below)
```

That elision is marked on purpose. `--help` really does print an environment section after the
flags, and reproducing only the flags implied the binary had nothing to say about them - which is
how `ALTSERVER_NO_SUBSCRIBE` stayed undocumented here while the program itself described it. Run
`docker exec altserver AltServer --help` for the authoritative text.

No IPA argument starts the daemon. With one, it performs a one-time install - which needs a real
terminal, because the 2FA code is read from stdin.

### Environment

| Variable | Purpose |
|---|---|
| `ALTSERVER_ANISETTE_SERVER` | **Required.** Full URL including scheme, e.g. `http://127.0.0.1:6969`. There is no default |
| `ALTSERVER_UDID` / `ALTSERVER_APPLE_ID` / `ALTSERVER_APPLE_PASSWORD` | Alternatives to `-u` / `-a` / `-p`. Prefer these: a password passed as `-p` is visible in `ps` to every user on the host |
| `ALTSERVER_NO_CLIENTINFO_SANITIZE` | Set to `1` to stop rewriting `com.apple.dt.Xcode` in `X-MMe-Client-Info`. Diagnostic only - leave unset |
| `ALTSTORE_SKIP_FETCH` | Set to `1` to stop the container refreshing `AltStore.ipa` on start. The fetcher also takes `--force` (re-download even if current) and `--beta` (the beta channel), and exits `75` rather than `0` when it keeps an existing IPA after a failed check, so the entrypoint's warning is reachable |
| `ALTSERVER_LOG` | Where the web UI READS the redacted log from. Default `/data/altserver.log`. It does not control where the log is WRITTEN - that is the `--tee` path in the entrypoint, and the two must match |
| `ALTSERVER_NETMUXD_SOCKET` | Which socket the status page probes for netmuxd. Default `/run/muxd/usbmuxd`. Changing netmuxd's socket means changing this, `USBMUXD_SOCKET_ADDRESS`, and netmuxd's own `--socket-path` - all three, or the checks and the daemon look at different places |
| `ALTSTORE_IPA_PATH` | Where the AltStore IPA is fetched to. Default `/data/AltStore.ipa` |
| `ALTSERVER_HISTORY` | Where the run-by-run check history is written. Default `/data/status-history.jsonl`. The `altserver-web` healthcheck records one line every 5 minutes; the status page draws a timeline from it |
| `ALTSERVER_HISTORY_MAX` | How many runs to RETAIN. Default `5000`, about 17 days at the healthcheck's cadence. The page draws only the most recent 120 of them, roughly 10 hours; `/api/history` returns the rest |
| `ALTSERVER_MDNS_RECHECK_SECONDS` | How often the server re-checks that its own mDNS advertisement is still published, re-registering if not. Default `60`. Set to `0` to disable. Leave it on: without it, an avahi restart makes the server permanently undiscoverable with no other symptom |
| `ALTSERVER_APPARMOR` | Which AppArmor profile the containers request. Default `unconfined`. Set to `altserver-mdns` only AFTER running `sudo bash deploy/apparmor/install.sh` on the host - a container asking for an unloaded profile fails to start |

There is deliberately **no default anisette server**. The one that used to be hardcoded has
returned HTTP 502 since 2026-09, and pointing every user at a single shared anisette identity can
get Apple IDs locked.

### Runtime requirements

Bundled in the container image. Needed on the host if you run the binary directly:

| Requirement | Why | If missing |
|---|---|---|
| `python3` | The binary is `-static` and cannot dlopen Bonjour, so it shells out to python3 | Advertisement fails |
| `libavahi-compat-libdnssd1` | Provides `libdns_sd.so.1`, the Bonjour compatibility library the code dlopens | Advertisement fails |
| `avahi-daemon` running | Performs the actual mDNS publishing | Advertisement fails |
| `avahi-utils` | Provides `avahi-browse`, the only way to check an advertisement from outside | **The self-heal watchdog silently turns itself OFF**, and the status checks report UNKNOWN |
| `usbmuxd` for cabled pairing; **`netmuxd` for Wi-Fi** | Device access | No device found |
| An anisette server | Apple machine identity | Sign-in fails |
| Accurate clock **on the anisette host** | Its timestamp is forwarded to Apple verbatim | Opaque `-36607` |

The code dlopens `libdns_sd.so.1` and falls back to the unversioned `libdns_sd.so`, so either the
runtime package or `libavahi-compat-libdnssd-dev` works. Without the library, the server runs
without advertising itself and prints an ERROR at startup; it is then invisible to your phone.

---

## Deploying on your platform

Two questions decide everything: **what architecture is your host**, and **are you forking**.

### Which platforms work

| Host | Container stack | Static binary | Refresh |
|---|---|---|---|
| **amd64** (x86_64) - most servers, NAS, VMs | pull the published image | yes | over Wi-Fi |
| **arm64** (aarch64) - Raspberry Pi 4/5, Apple silicon VMs | build locally, one command | yes | over Wi-Fi |
| **armv7** - 32-bit ARM | not supported | yes | over a cable |
| **i386** - legacy 32-bit x86 | not supported | yes | over a cable |

The bottom two are not broken, they just need the cable left plugged in. Refreshing over USB works
the same way it always has - the host's `usbmuxd` gives AltServer the device, which is exactly what
netmuxd replaces for the cable-free case. A small always-on machine with the phone permanently
attached is a perfectly good deployment, and arguably a good use for hardware too slow for much
else.

What they cannot do is refresh *wirelessly*, because **netmuxd publishes releases only for x86_64
and aarch64**. That is also why the container stack is not offered there: the image treats netmuxd
as required and refuses to build rather than produce something that looks fine and cannot reach
your phone.

```
netmuxd publishes no build for TARGETARCH=arm
```

### Find your device

Not sure which you are? On the host:

```bash
uname -m                 # x86_64 | aarch64 | armv7l | i686
lscpu | grep -i 64       # on a 32-bit OS, tells you if the CPU could do better
```

| Device | Reports as | Path |
|---|---|---|
| x86 server, NAS, mini PC, VM | `x86_64` | **A** |
| Raspberry Pi 5 | `aarch64` | **B** |
| Raspberry Pi 4 - 64-bit OS | `aarch64` | **B** |
| Raspberry Pi 4 - 32-bit OS | `armv7l` | **B** after reinstalling 64-bit, see below |
| Raspberry Pi 3 / 3B+ / Zero 2 W - 64-bit OS | `aarch64` | **B** |
| Raspberry Pi 3 / 3B+ / Zero 2 W - 32-bit OS | `armv7l` | **B** after reinstalling 64-bit, see below |
| Raspberry Pi 2 | `armv7l` | **C** - 32-bit only, no 64-bit option |
| **Raspberry Pi 1, Zero, Zero W** | `armv6l` | **Not supported.** ARMv6; the armv7 binary will not run |
| Apple silicon Mac VM (UTM, Parallels, Lima) | `aarch64` | **B** |
| Older ARM NAS - some Synology, QNAP, Odroid, Orange Pi | usually `armv7l` | **C** |
| Old 32-bit x86 thin client or netbook | `i686` | **C** |

The Pi Zero and Zero W are the trap in that list: they look like the obvious tiny always-on machine
for this, but their ARM1176 core is **ARMv6**, and an armv7 binary will fail with an illegal
instruction. The **Zero 2 W** is a different chip entirely (Cortex-A53) and works fine - prefer it.

> **If you are on armv7, check whether you actually need to be.** Raspberry Pi OS shipped 32-bit by
> default until 2022, so a Pi 3, 4 or Zero 2 W installed a few years ago reports as `armv7` even
> though the CPU is 64-bit capable. `uname -m` says `armv7l`; `lscpu | grep -i 64` will tell you
> whether the hardware could do better. Reinstalling with 64-bit Raspberry Pi OS moves you to
> **Path B** and gets you the container stack and wireless refresh. That is almost always less work
> than building netmuxd from source.

Genuinely 32-bit-only hardware takes the cabled route, Path C.

---

### Path A - amd64, not forking

Nothing to change. Follow [Setup](#setup). The stack pulls
`ghcr.io/ben-diehlci/altserver-linux:latest`, which is public.

### Path B - arm64 (Raspberry Pi), not forking

The one thing that does not work out of the box is the published image, which is `linux/amd64`.
Build it locally instead - everything else in Setup is unchanged.

**How the build works**, because the fix only makes sense once you know: the image is built in two
stages. Stage one runs inside a prebuilt *toolchain* image that already contains corecrypto,
cpprestsdk, boost and libzip - the slow, awkward dependencies - and compiles AltServer there.
Stage two copies the finished binary into a small Debian runtime. Building on a Pi means pointing
stage one at the **arm64 toolchain**, which is already published, not compiling those dependencies
yourself.

```bash
git clone --recursive https://github.com/Ben-Diehlci/altserver-linux.git
cd altserver-linux
docker build -f docker/Dockerfile \
  --build-arg BUILDER=ghcr.io/ben-diehlci/altserver_builder_alpine_aarch64 \
  --build-arg TARGETARCH=arm64 \
  -t ghcr.io/ben-diehlci/altserver-linux:latest .
```

Both `--build-arg`s matter, and they are **not** the same value:

| Arg | Value on arm64 | Selects |
|---|---|---|
| `BUILDER` | `..._aarch64` | the toolchain image, named for the gcc triple |
| `TARGETARCH` | `arm64` | which netmuxd release to fetch, named for Docker's platform |

Set only the first and you get a correctly compiled arm64 AltServer that downloads an **x86_64
netmuxd** and dies at startup with an exec-format error. Modern Docker routes `docker build`
through buildx, which sets `TARGETARCH` for you, but passing it explicitly costs nothing.

Tagging it with the name the stack already expects means no file edit - Docker finds the local
image and does not pull.

> **Through Portainer, use the `build:` block instead.** Ticking "re-pull image" on a stack update
> fetches the amd64 image from the registry and discards your local arm64 build, and the containers
> then fail with exec-format errors. Uncomment `build:` in
> [`deploy/altserver-stack.yml`](deploy/altserver-stack.yml) and set these in the stack
> environment:
>
> ```
> ALTSERVER_BUILD_ARCH=aarch64
> ALTSERVER_TARGETARCH=arm64
> ```
>
> Then it is rebuilt from source on every update rather than pulled, which is what you want on a
> platform the registry has no image for.

Expect it to take a while. It is native rather than emulated - it is slow because a Pi is a Pi.

### Path C - armv7 or i386

No container stack. Build the static binary and run it directly - see
[Reference](#reference) for flags and environment, and
[Runtime requirements](#runtime-requirements) for what the host needs, since nothing is bundled
for you.

```bash
docker run --rm -v "$PWD":/workdir -w /workdir \
  ghcr.io/ben-diehlci/altserver_builder_alpine_armv7 \
  bash -c 'mkdir -p build; cd build; make -f ../Makefile -j"$(nproc)"'
```

Swap `_i386` for the other.

Leave the phone connected and refreshing works normally through the host's `usbmuxd` - you are
trading the convenience of a cable-free setup, not the function. If you later want wireless, it
needs a netmuxd built from source (it is Rust) for that target, or a move to 64-bit per the note
above.

---

### If you fork it

Most of it follows you with no edits - the published image name derives from
`github.repository_owner`, so your images go to your namespace automatically.

**Pushing:** an ordinary push builds **amd64 only**, because the other three run under QEMU on the
runner and take several times longer. Paying that per-commit is latency for no benefit; dropping
them entirely would make this useless on a Pi.

Two knobs, both **repository variables rather than file edits**, because a git-backed stack
overwrites hand edits on its next pull:

| Variable | Where | Set it when |
|---|---|---|
| `BUILDER_NAMESPACE` = your GitHub account, lowercased | Settings -> Secrets and variables -> Actions | you have run **Build buildenv Docker** to publish your own toolchain. Until then the default set is public and works. **Only `build_image.yml` reads it** - the binary build in `build.yml` has its builder image pinned in the matrix, so setting this does not redirect that one |
| `ALTSERVER_APPARMOR` = `altserver-mdns` | Portainer stack environment | you want the confined profile - see [Two settings that are load-bearing](#two-settings-that-are-load-bearing) in Setup, which applies whether you fork or not |

`platforms: linux/amd64` in `build_image.yml` is hardcoded deliberately. Stage one pulls an
**architecture-specific** toolchain, so a plain multi-arch `platforms:` list would run the amd64
toolchain under emulation and still emit an amd64 binary - an image that is mislabelled rather than
merely slow. Multi-arch needs a per-architecture `BUILDER`, which is what `build.yml`'s four-way
matrix does for the static binaries.

---

## Downloads and releases

- **Container image:** `ghcr.io/<owner>/altserver-linux:latest`, built by
  [`build_image.yml`](.github/workflows/build_image.yml). `linux/amd64` only - see Path B.
- **Static binaries:** GitHub Actions artifacts. **`chmod +x` after downloading** - artifact upload
  does not preserve the executable bit.

The four binaries and their names:

| Matrix name | Binary inside | Typical host |
|---|---|---|
| `amd64` | `AltServer-x86_64` | servers, NAS, VMs |
| `aarch64` | `AltServer-aarch64` | Raspberry Pi 4/5 64-bit |
| `armv7` | `AltServer-armv7` | older 32-bit Pi |
| `i386` | `AltServer-i386` | legacy 32-bit x86 |

Note the mismatch: the **artifact** is named for the matrix label (`AltServer-amd64`) while the
**binary inside it** is named for the gcc triple (`AltServer-x86_64`). Only amd64 differs, which is
exactly why it surprises.

### Getting all four architectures

**Actions -> Build AltServer -> Run workflow**, tick **"Build every architecture, not just amd64"** (the `all_arches` input).
Nothing is published; the binaries appear as artifacts on that run. Best for a one-off.

**Or push a tag**, which builds all four *and* publishes them as a GitHub Release. Any tag name
triggers it - the workflow matches `refs/tags/*` - and the release takes the tag's name, so follow
the existing `vMAJOR.MINOR.PATCH` convention:

```bash
git tag v1.0.0
git push origin v1.0.0
```

> **Push tags one at a time. Never `git push --tags`.** This repo inherited eight tags from upstream
> (`v0.0.1` through `v0.0.5` and some `-rc` variants) that are *not* published here. `--tags` would
> push all of them, and every one would start its own four-architecture build and publish its own
> release.

```bash
git tag -l                        # local, including the inherited ones
git ls-remote --tags origin       # what is actually published
```

To move or remove a tag - delete it in both places, then re-tag:

```bash
git tag -d v1.0.0                 # local
git push origin :refs/tags/v1.0.0 # remote
git tag v1.0.0 COMMIT_SHA         # substitute a real sha -- an angle-bracket
git push origin v1.0.0            # placeholder would be read by bash as a redirect
```

Deleting the tag does **not** delete the GitHub Release it created; remove that from the Releases
page separately, or the next push of the same tag attaches to the old one. Anyone who already
fetched a moved tag keeps their copy pointing at the old commit - which is why moving a published
tag is worth avoiding rather than merely fixing.

---

## Advanced: building from source

- Preparation: `git clone --recursive <this repo>`

- Easiest, using the same prebuilt toolchain CI uses (it already has corecrypto, cpprestsdk, boost
  and libzip):
  ```
  docker run --rm -v "$PWD":/workdir -w /workdir \
    ghcr.io/ben-diehlci/altserver_builder_alpine_amd64 \
    bash -c 'mkdir -p build; cd build; make -f ../Makefile -j"$(nproc)"'
  ```
  Or build the container image directly - note the `-f`, because the Dockerfile is not at the
  repo root and the build context must still be the root:

  ```bash
  docker build -f docker/Dockerfile -t altserver .
  ```

- By hand (note the `cd build` - the Makefile builds into the *current* directory):
  ```
  cd AltServer-Linux
  mkdir build
  cd build
  make -f ../Makefile -j3
  ls AltServer-*
  ```

### How the build works

This project never forked AltServer-Windows. `upstream_repo/` is a submodule of it, and the build
**rewrites those sources at compile time** - `makefiles/rewrite_altserver_source.py` and friends
convert `L"..."` to `U("...")`, `std::wstring` to `std::string`, `boost::filesystem` to
`std::filesystem`, strip the Win32 GUI and splice in a console implementation. Win32 gaps are
filled by `-include shims/windows_shim.h`.

Patches to vendored code live in those rewriters rather than in the submodule, because the
libraries are submodules: an edit in place cannot be committed here - only the submodule pointer
would move, to a commit that does not exist upstream, breaking every fresh clone.

**Every rewriter fails the build if its patterns stop matching**, rather than emitting a binary
that is quietly missing a transformation:

| Rewriter | How it fails loudly |
|---|---|
| `makefiles/rewrite_altserver_source.py` | Match counts on the AltServerApp.cpp substitutions, plus post-conditions on the output |
| `makefiles/AltSign-build/rewrite_altsign_source.py` | Match assertions |
| `makefiles/AltSign-build/rewrite_ldid_source.py` | Match assertions |
| `makefiles/libimobiledevice-build/rewrite_idevice_source.py` | Match assertions |

The first one needs both kinds because its substitutions fail differently. The AltServerApp.cpp
block runs for one file and every substitution in it is mandatory, so each asserts a count. The
global ones run over all 35 files in the directory - including binaries like `MenuBarIcon.ico` -
and legitimately match zero times in most, so a count would be meaningless; they are checked as
post-conditions on the output instead (no `L"..."` literal, no `boost::filesystem`, no bare
`std::wstring` may survive). That is stronger than counting, because it also catches an occurrence
arriving in a form the pattern was never written to handle.

### Building the buildenv image

`buildenv/Dockerfile` builds the toolchain. Apple's current corecrypto distribution needs three
fixes, all applied there:

1. The archive extracts to `corecrypto-2024/`, not `corecrypto/`. Docker's `WORKDIR` silently
   *creates* the missing directory, so the error surfaces one step later as a confusing
   "does not appear to contain CMakeLists.txt"
2. `CMakeLists.txt` includes `scripts/code-coverage.cmake`, which Apple does not ship
3. `CoreCryptoSources.cmake` still points at `corecrypto_static/ccrng_static.c`, which moved to the
   tree root. The visible error is "No SOURCES given to target"; the real one is the
   "Cannot find source file" line above it

The old note about removing `-mno-default` for ARM is **stale** - the Makefile already guards that
flag to i386/i686, so ARM builds work unmodified.

---

## The guard suite

Eleven checks under [`tests/`](tests/) run on every push, wired into
`.github/workflows/build.yml`. Each one exists because the bug it guards **shipped silently** -
the build stayed green, the stack deployed, and the failure showed up days later as an app that
would not open. They are the reason this repo can be changed with any confidence.

```bash
python3 -m pip install pyyaml          # only check_compose and check_observability need it
for t in tests/check_*.py; do echo "== $t"; python3 "$t" || break; done
```

| Guard | The bug it exists because of |
|---|---|
| `check_workflow_paths` | A `paths:` filter omitted `web/**`, so five commits of fixes never reached a published image |
| `check_page_js` | One literal newline in a JS string killed every page's entire `<script>` block |
| `check_compose` | A duplicate YAML key silently kept the last value; the stack only broke at deploy time |
| `check_conn_data_layout` | A usbmux address parsed in one sockaddr layout, so every Wi-Fi refresh failed as a device fault |
| `check_redaction` | The install log printed the anisette identity in full, over plain HTTP |
| `check_ascii_punctuation` | Typographic characters reached the served page as raw bytes, `\u` escapes and HTML entities |
| `check_mdns_watchdog` | The advertisement was registered once and trusted forever; avahi restarted and it never came back |
| `check_status_signals` | `/api/status` answered `200` and the script exited `0` through a three-day total outage |
| `check_rewriter_deps` | Editing a source rewriter regenerated nothing, and the build reported success with the old patch |
| `check_observability` | Nothing ran the checks, nothing consumed them, and a dead log read as a quiet server |
| `check_readme_claims` | This file drifted into 38 defects, including a security claim contradicted 90 lines later, because nothing executed the documentation |

Two conventions worth keeping if you add one. **Verify it by mutation** - reintroduce the bug and
confirm the guard fails; several here were written, passed, and only caught anything after a
mutant proved they could. And **exercise the thing rather than grep its source**: three separate
times in this repo a guard that searched for a variable name passed while the behaviour it named
was broken.

---

## Credits

Original Linux port by [NyaMisty](https://github.com/NyaMisty/AltServer-Linux). AltStore, AltServer
and AltSign by [Riley Testut](https://github.com/rileytestut). This fork only revives and extends
that work. Licensed AGPL-3.0, as upstream.
