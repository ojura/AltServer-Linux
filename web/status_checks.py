"""Health checks for an AltServer-Linux deployment.

Stdlib only, deliberately: python3 is already a hard runtime requirement of this project (the
-static binary cannot dlopen Bonjour, so it shells out to python3 to do it), which means adding
these checks costs no new dependency.

Every check here corresponds to a failure this project can produce SILENTLY. That is the whole
point -- AltServer cannot report its own health:

  * DNSServiceRegister returned success unconditionally until we fixed it, and even now avahi can
    report success while publishing nothing, so the only trustworthy test is an external browse.
  * A background refresh that finds no server is suppressed by AltStore itself
    (BackgroundRefreshAppsOperation sets ignoresServerNotFoundError = true), so the phone stays
    quiet too.
  * `journalctl -p err` is empty no matter what breaks, because essentially everything is written
    to stdout at info level.

So the first symptom of a broken deployment is an app that will not open, a week later. These
checks exist to turn that into something visible.
"""

import calendar
import concurrent.futures
import datetime
import email.utils
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# The exact contract src/AnisetteDataManager.cpp enforces. Casing is inconsistent upstream and
# matched case-sensitively: X-MMe-Client-Info has a capital MM, X-Mme-Device-Id a lowercase m.
ANISETTE_REQUIRED_KEYS = [
    "X-Apple-I-MD-M",
    "X-Apple-I-MD",
    "X-Apple-I-MD-LU",
    "X-Apple-I-MD-RINFO",
    "X-Mme-Device-Id",
    "X-Apple-I-SRL-NO",
    "X-MMe-Client-Info",
    "X-Apple-I-Client-Time",
    "X-Apple-Locale",
    "X-Apple-I-TimeZone",
]

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"

# Machine-readable verdicts, so something other than a human looking at the page can tell this is
# broken.
#
# WHY. On 2026-09-22 the mDNS advertisement was dropped and the server went undiscoverable for at
# least three days. This file reported the failure correctly the entire time -- and nothing heard
# it, because `python3 status_checks.py` exited 0 whatever it found and /api/status answered 200
# whatever it found. Following the obvious monitoring advice would have shown green throughout an
# outage in which nothing refreshed.
#
# Exit codes are the Nagios plugin convention (0 OK, 1 WARNING, 2 CRITICAL), which Nagios, Icinga
# and anything that shells out to a check script already understand.
EXIT_CODES = {OK: 0, WARN: 1, UNKNOWN: 1, FAIL: 2}

# HTTP has only two useful answers for a health endpoint, so WARN deliberately stays 200: degraded
# is not down, and UNKNOWN collapses into WARN whenever a tool is merely absent. The consequence
# is worth stating plainly rather than discovering later: A MONITOR WATCHING ONLY THE STATUS CODE
# CANNOT SEE A WARN. That includes the case where the mDNS check cannot run at all, which looks
# identical to health over HTTP alone. Anything that needs to distinguish must read `overall` from
# the body, or use the exit code.
HTTP_CODES = {OK: 200, WARN: 200, UNKNOWN: 200, FAIL: 503}


def exit_code(overall):
    """Process exit status for an `overall` verdict. Unknown verdicts are a WARNING, not an OK."""
    return EXIT_CODES.get(overall, 1)


def http_status(overall):
    """HTTP status for an `overall` verdict. Only FAIL is not-200; see HTTP_CODES above."""
    return HTTP_CODES.get(overall, 200)


def _in_container():
    return os.path.exists("/.dockerenv")


def _result(name, state, summary, detail=None, fix=None):
    return {"name": name, "state": state, "summary": summary, "detail": detail or "", "fix": fix or ""}


def _run(cmd, timeout=10, env=None):
    """Run a command, returning (rc, stdout+stderr). Never raises."""
    if shutil.which(cmd[0]) is None:
        return None, "%s is not installed" % cmd[0]
    try:
        merged = dict(os.environ, **env) if env else None
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=merged)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return None, "%s timed out after %ss" % (cmd[0], timeout)
    except Exception as exc:  # pragma: no cover - defensive
        return None, "%s could not be run: %s" % (cmd[0], exc)


def _http_date_to_iso(value):
    """An HTTP Date header as the UTC timestamp check_clock expects, or None.

    RFC 9110 requires an origin server with a clock to send this on every response, so it is a
    free reading of the anisette host's clock -- the thing that actually breaks sign-in, since
    Linux forwards that server's timestamp to Apple verbatim.
    """
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    try:
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, OverflowError):
        return None


def check_anisette(url=None, polling=False):
    """Fetch anisette data and validate it against the client's actual contract.

    polling=True makes this SAFE TO RUN ON A TIMER, and that is not a performance concern.

    This function fetches the BARE ROOT of the anisette server, which is the v1 data route. On a
    server whose machine identity is missing, that route performs REAL PROVISIONING AGAINST
    APPLE -- deploy/anisette-stack.yml:62-73 refuses to point even a healthcheck at it for
    exactly this reason: "a polling healthcheck on / would hammer Apple's endpoint at exactly
    the moment your identity volume has gone missing, turning a restore-the-backup incident into
    rate-limit / account-lock territory."

    That was harmless while this ran only when a human opened the status page. It stopped being
    harmless the moment the altserver-web healthcheck started running the checks every five
    minutes. So on the timer we ask the STATIC route instead, which makes no Apple contact, and
    say plainly that the field contract was not verified. The page, which is a human asking once,
    still does the real fetch.
    """
    url = url or os.environ.get("ALTSERVER_ANISETTE_SERVER", "")

    if not url:
        return _result("Anisette server", FAIL, "ALTSERVER_ANISETTE_SERVER is not set",
                       "There is no default; the server that used to be hardcoded is dead.",
                       "Set it to a full URL including the scheme, e.g. http://127.0.0.1:6969")

    if not url.startswith(("http://", "https://")):
        return _result("Anisette server", FAIL, "URL has no http:// or https:// scheme",
                       "Configured as %r." % url,
                       "AltServer's HTTP client constructor rejects a scheme-less URL before "
                       "sending anything. Use e.g. http://127.0.0.1:6969")

    if polling:
        # /v3/client_info is static and contacts Apple for nothing.
        probe = url.rstrip("/") + "/v3/client_info"
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(probe, headers={"User-Agent": "Xcode"}),
                    timeout=10) as resp:
                if resp.status == 200:
                    # The Date header IS the anisette host's clock, and every origin server
                    # sends one. Without it check_clock had nothing to compare against, went
                    # permanently UNKNOWN on the polled path, and dragged the verdict to WARN --
                    # which marks the container unhealthy forever. Measured for free, with no
                    # Apple contact, which was the whole point of probing the static route.
                    stamp = _http_date_to_iso(resp.headers.get("Date"))
                    res = _result(
                        "Anisette server", OK, "Reachable (field contract not checked on a timer)",
                        "Probed %s, which makes no Apple contact. The full ten-field check runs "
                        "when the status page is opened." % probe)
                    if stamp:
                        res["anisette_time"] = stamp
                    return res
                return _result("Anisette server", FAIL, "HTTP %s from %s" % (resp.status, probe),
                               "Reachable but unhealthy.", "Check the anisette container's logs.")
        except urllib.error.HTTPError as exc:
            # It ANSWERED. Saying "cannot reach" here and sending the owner to `docker ps` --
            # which will show the container up -- invites the one action this deployment must
            # not take casually: anisette-stack.yml:47-58 documents that a Portainer redeploy
            # destroys device.json and adi.pb, mints a brand-new machine, and demands 2FA.
            # A 404 here is not a failure of the SERVER: /v3/client_info simply does not exist
            # on a v1-only implementation, which can still serve anisette data perfectly.
            state = WARN if exc.code == 404 else FAIL
            return _result("Anisette server", state, "HTTP %s from %s" % (exc.code, probe),
                           "The server answered, so it is running and listening; only this route "
                           "failed. %s" % exc,
                           "Do NOT redeploy the anisette stack to fix this -- that can destroy "
                           "the machine identity. A 404 can mean a v1-only server with no "
                           "/v3/client_info route; a 502/503 means something in front of anisette "
                           "is up and anisette is not. Check `docker logs anisette`.")
        except Exception as exc:
            return _result("Anisette server", FAIL, "Nothing answered at %s" % probe, str(exc),
                           "No listener on that port. Is the anisette container running? "
                           "Check `docker ps`.")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Xcode"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body = resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        hint = {
            401: "The server wants credentials, which AltServer does not send. Wrong server?",
            403: "The server refused. A proxy or its own access rules, not AltServer.",
            404: "No anisette payload at this path. Some servers serve v1 only at the root.",
            429: "Rate limited. If something is polling this URL, stop it before Apple notices.",
            502: "Something IN FRONT of anisette answered; anisette itself is not up behind it.",
            503: "Reachable but not ready. If it has just started, provisioning can take minutes.",
            504: "A proxy timed out waiting for anisette.",
        }.get(exc.code, "Check `docker logs anisette`.")
        return _result("Anisette server", FAIL, "HTTP %s from %s" % (exc.code, url),
                       "The server answered, so it is running and listening. %s" % exc,
                       hint + " Do NOT redeploy the stack to clear this: that can destroy the "
                              "machine identity (see deploy/anisette-stack.yml).")
    except Exception as exc:
        # DNS, refused, timeout and TLS mean different things and need different actions.
        text = str(exc).lower()
        if "timed out" in text or "timeout" in text:
            hint = ("It accepted the connection and did not answer in 10s. A server still "
                    "provisioning against Apple does this; give it a few minutes.")
        elif "name or service not known" in text or "nodename nor servname" in text \
                or "getaddrinfo" in text:
            hint = ("The HOSTNAME did not resolve -- nothing was contacted. Check the spelling in "
                    "ALTSERVER_ANISETTE_SERVER; use 127.0.0.1 for a container on this host.")
        elif "certificate" in text or "ssl" in text:
            hint = "TLS failed. The server is there; its certificate is the problem."
        else:
            hint = ("Nothing is listening on that host and port. Is the anisette container "
                    "running? Check `docker ps`.")
        return _result("Anisette server", FAIL, "Cannot reach %s" % url, str(exc), hint)

    if status != 200:
        return _result("Anisette server", FAIL, "HTTP %s" % status, body[:200],
                       "AltServer requires exactly 200.")

    try:
        data = json.loads(body)
    except ValueError:
        return _result("Anisette server", FAIL, "Response is not JSON", body[:200],
                       "The body is quoted above; read it before changing anything. HTML here is "
                       "usually a proxy or error page answering instead of anisette, not a wrong "
                       "path -- the URL you set is already the root. Only if it looks like a "
                       "different API is the endpoint worth questioning.")

    if not isinstance(data, dict):
        return _result("Anisette server", FAIL, "Response is not a JSON object", body[:200])

    missing = [k for k in ANISETTE_REQUIRED_KEYS if k not in data]
    if missing:
        # Do NOT assert which cause. All that was established is that ten keys are absent from
        # a 200. "Wrong kind of server" is one explanation; the likelier one on this deployment
        # is the opposite -- this route is where anisette provisions against Apple, and a server
        # whose machine identity is gone answers 200 with an error object instead of the fields.
        # Telling the owner their server is the wrong type sends them to replace a working one
        # while the real emergency is a lost identity. The BODY usually names the difference, so
        # show it rather than only the key names we already knew.
        return _result("Anisette server", FAIL, "Missing %d required field(s)" % len(missing),
                       "Absent: %s. Server said: %s" % (", ".join(missing), body[:300]),
                       "Two very different causes look like this. If the body names an error or "
                       "mentions provisioning, the machine identity is missing or Apple refused "
                       "it -- restore the anisette identity volume, and do NOT redeploy the "
                       "stack first. If the body is a well-formed object with different key "
                       "names, the server does not speak the legacy v1 flat-JSON contract.")

    # A numeric value here is the one failure the client swallows: X-Apple-I-MD-RINFO is parsed
    # with std::atoi, which returns 0 for a non-string without erroring, and the consequence
    # surfaces much later as an opaque Apple -36607.
    not_strings = [k for k in ANISETTE_REQUIRED_KEYS if not isinstance(data[k], str)]
    if not_strings:
        return _result("Anisette server", FAIL, "Field(s) not sent as JSON strings",
                       ", ".join(not_strings),
                       "AltServer requires every value to be a string. X-Apple-I-MD-RINFO as a "
                       "number is the common variant, and it fails silently.")

    client_info = data.get("X-MMe-Client-Info", "")
    detail = "Device-Id %s" % data.get("X-Mme-Device-Id", "?")
    if "com.apple.dt.Xcode" in client_info:
        # Verified against live Apple infrastructure: with this substring present the first GSA
        # request returns 503; rewritten to com.apple.akd it returns 200.
        detail += " | client-info contains com.apple.dt.Xcode, so the built-in sanitizer is " \
                  "load-bearing (leave ALTSERVER_NO_CLIENTINFO_SANITIZE unset)"

    ok = _result("Anisette server", OK, "All 10 fields present, all strings, HTTP 200", detail)
    ok["anisette_time"] = data.get("X-Apple-I-Client-Time")
    return ok


def check_clock(anisette_time=None):
    """Drift against the ANISETTE server's clock is what actually matters.

    Linux forwards the anisette server's X-Apple-I-Client-Time to Apple verbatim (macOS stamps
    Date() locally instead), so NTP on the AltServer host proves nothing on its own. Comparing the
    two directly measures the thing that breaks sign-in, and unlike timedatectl it works inside a
    container.
    """
    if anisette_time:
        try:
            parsed = time.strptime(anisette_time[:19], "%Y-%m-%dT%H:%M:%S")
            skew = abs(calendar.timegm(parsed) - time.time())
            if skew <= 30:
                return _result("Clock agreement", OK,
                               "Anisette clock within %ds of ours" % int(skew),
                               "Anisette said %s" % anisette_time)
            return _result("Clock agreement", FAIL,
                           "Anisette clock is %ds away from ours" % int(skew),
                           "Anisette said %s" % anisette_time,
                           "Apple sees the anisette server's timestamp verbatim. Skew surfaces as "
                           "an opaque -36607 with nothing naming time as the cause. Fix NTP on "
                           "whichever host runs anisette -- not just this one.")
        except Exception as exc:
            # Do NOT fall through to the timedatectl fallback below. This used to be a bare
            # `except: pass`, which meant an unparseable anisette timestamp silently skipped the
            # only comparison that matters and then reported whatever LOCAL NTP said -- a green
            # "NTP synchronised" for a check that never ran, measuring the very thing this
            # docstring says proves nothing.
            #
            # The trigger is realistic, not hypothetical. AltServer's own C parses this field with
            # strptime(), which is a PREFIX match, while this is a full match on a fixed 19-char
            # slice. A non-zero-padded month ("2026-9-25T...") is accepted by AltServer and raises
            # here, so refresh would keep working normally while the clock check became a
            # permanent no-op -- and clock skew is the failure that surfaces as an opaque Apple
            # -36607 with nothing naming time as the cause.
            return _result("Clock agreement", WARN,
                           "Could not read the anisette server's clock",
                           "Anisette said %r (%s: %s)"
                           % (anisette_time, type(exc).__name__, exc),
                           "The drift comparison did not run, so clock skew would go unreported. "
                           "Local NTP is not a substitute: Linux forwards the ANISETTE server's "
                           "timestamp to Apple verbatim, so this host's clock proves nothing. "
                           "Expected format is YYYY-MM-DDTHH:MM:SS.")

    rc, out = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if rc is None:
        return _result("Clock agreement", UNKNOWN, "No anisette timestamp to compare against",
                       "timedatectl is unavailable here, which is normal in a container.",
                       "This resolves itself once the anisette check above succeeds.")
    if out.strip() == "yes":
        # WARN, not OK. This measured LOCAL NTP, which the docstring above explains is not the
        # thing that breaks sign-in. Reporting OK here would be a verification that cannot fail,
        # which this project has already been bitten by twice.
        return _result("Clock agreement", WARN, "Local NTP synchronised, anisette drift NOT checked",
                       "There was no anisette timestamp to compare against, so only this host's "
                       "clock was verified.",
                       "Apple sees the anisette server's timestamp, not this one. Fix the "
                       "anisette check above to get a real answer.")
    return _result("Clock agreement", WARN, "Clock is NOT NTP-synchronised")


# netmuxd's socket. Set by the stack; the default matches deploy/altserver-stack.yml.
NETMUXD_SOCKET = os.environ.get("ALTSERVER_NETMUXD_SOCKET", "/run/muxd/usbmuxd")

# libusbmuxd's env var. Note the spelling -- USBMUXD_SOCKET_ADRESS, with one D, is a widely-copied
# typo that is silently ignored. Verified at upstream_repo/libusbmuxd/src/libusbmuxd.c:158.
_WIRELESS_ENV = {"USBMUXD_SOCKET_ADDRESS": "UNIX:" + NETMUXD_SOCKET}
# Empty string makes libusbmuxd fall back to its compiled-in default, /var/run/usbmuxd.
_USB_ENV = {"USBMUXD_SOCKET_ADDRESS": ""}


def _devices_via(env, flag):
    """(udids, note) over one transport. Distinguishes 'no devices' from 'no mux listening'.

    `flag` is NOT optional and must match the transport. idevice_id's -l and -n are not
    verbosity switches, they select which transports are enumerated:

        -l  include_usb = 1        (tools/idevice_id.c: case 'l')
        -n  include_network = 1    (case 'n')
        neither, with no other args: both

    netmuxd only ever presents the phone as ConnectionType: Network, so `idevice_id -l` against
    netmuxd's socket returns an empty list NO MATTER WHAT -- the device is there, it is simply not
    a USB device. This check used -l for the wireless probe and so could never report OK for a
    wireless-only setup, which is the exact configuration it exists to verify. It read "No device
    on either transport" while refresh was demonstrably working.
    """
    rc, out = _run(["idevice_id", flag], env=env)
    if rc is None:
        return None, out
    # This exact string means libusbmuxd could not reach the socket AT ALL -- a dead or absent
    # mux. An empty device list is a silent success with no output, which is a completely
    # different condition and must not be conflated with it.
    if "Unable to retrieve device list" in out:
        return None, "no mux is listening on that socket"
    return [l.strip() for l in out.splitlines() if l.strip()], ""


# lockdownd result codes, from libimobiledevice/include/libimobiledevice/lockdown.h. Only the
# ones this file reasons about; the point is that -8 is a TRANSPORT failure and says nothing
# whatever about the pairing record, which is what the check used to claim it meant.
LOCKDOWN_INVALID_CONF = -2
LOCKDOWN_PAIRING_FAILED = -4
LOCKDOWN_SSL_ERROR = -5
LOCKDOWN_RECEIVE_TIMEOUT = -7
LOCKDOWN_MUX_ERROR = -8
LOCKDOWN_USER_DENIED_PAIRING = -18
LOCKDOWN_PAIRING_DIALOG_PENDING = -19


# How idevicepair actually reports failures, verified against
# libraries/libimobiledevice/tools/idevicepair.c rather than assumed.
#
# THE TRAP: pairing-stage failures are printed as WORDS WITH NO NUMBER (print_error_message,
# :115-144), and only CONNECTION-stage failures carry "error code %d" (:409). So branching on the
# numeric code alone -- which is what this file did first -- catches the transport case and sends
# every genuine pairing failure into the "unrecognised, do not assume a pairing problem" branch,
# telling the owner the opposite of the truth and giving them no action at all.
#
# Text first, code second. Order matters: "no device found" must beat everything, because the
# device being gone is not a statement about pairing.
_PAIR_PATTERNS = (
    ("passcode",    ("because a passcode is set", "enter the passcode")),
    ("gone",        ("no device found",)),
    ("unpaired",    ("is not paired with this host",)),
    ("trust",       ("accept the trust dialog",)),
    ("denied",      ("denied the trust dialog",)),
    ("failed",      ("pairing with device", "pairing failed")),
    ("connection",  ("not possible over this connection",)),
)


def classify_pair_failure(out):
    """What idevicepair's output actually says. One of the tags above, or 'transport'/'unknown'.

    Shared by the status check and the pairing wizard so the two cannot drift: they give the
    owner different instructions for the same condition otherwise.
    """
    low = (out or "").lower()
    for tag, needles in _PAIR_PATTERNS:
        if any(n in low for n in needles):
            return tag
    code = _lockdown_code(out)
    if code in (LOCKDOWN_MUX_ERROR, LOCKDOWN_RECEIVE_TIMEOUT):
        return "transport"
    if code == LOCKDOWN_PAIRING_DIALOG_PENDING:
        return "trust"
    if code == LOCKDOWN_USER_DENIED_PAIRING:
        return "denied"
    if code in (LOCKDOWN_INVALID_CONF, LOCKDOWN_PAIRING_FAILED, LOCKDOWN_SSL_ERROR):
        return "unpaired"
    return "unknown"


def _lockdown_code(out):
    """The numeric lockdownd code in idevicepair's output, or None."""
    m = re.search(r"error code\s+(-?\d+)", out or "")
    return int(m.group(1)) if m else None


def check_device():
    """Is the phone reachable, and -- the part that decides unattended refresh -- over WHICH path?

    Wireless is not a nicety here. Stock usbmuxd enumerates USB only, and on Ubuntu its unit is
    udev-activated: it exits when the last cable is unplugged. So a server with no cable has no mux
    at all unless netmuxd is running, and every refresh fails with what looks like a device fault.
    """
    # -n for netmuxd (network transport), -l for the host's usbmuxd (USB transport).
    wireless, wnote = _devices_via(_WIRELESS_ENV, "-n")
    usb, unote = _devices_via(_USB_ENV, "-l")

    if wireless:
        # -n is required here for the same reason as above: idevicepair.c:372 selects
        # IDEVICE_LOOKUP_USBMUX unless it is passed, so without it this validates a USB device
        # that does not exist on a cable-free server and reports a stale pairing record.
        rc, out = _run(["idevicepair", "-n", "validate"], env=_WIRELESS_ENV)
        if rc == 0:
            return _result("iPhone reachability", OK, "Reachable over Wi-Fi, pairing valid",
                           "UDID %s via netmuxd%s" % (wireless[0], ", also on USB" if usb else ""))
        # Classify on the TEXT first: idevicepair prints pairing-stage failures as words with
        # no number, so a code-only branch sends every real pairing failure to "unrecognised".
        kind = classify_pair_failure(out)

        if kind == "passcode":
            return _result("iPhone reachability", WARN, "Found over Wi-Fi, but the device is locked",
                           out.strip(), "Unlock the phone and re-check.")
        if kind in ("transport", "gone"):
            return _result(
                "iPhone reachability", FAIL, "Listed over Wi-Fi, but the phone did not answer",
                out.strip(),
                "This is a TRANSPORT failure, not a pairing problem -- do NOT re-pair on account "
                "of it. netmuxd keeps listing a known device for a while after it goes away, so "
                "this is what an asleep phone, one with Wi-Fi off, or one off this network looks "
                "like. Check the phone first.")
        if kind == "trust":
            return _result("iPhone reachability", WARN, "Waiting for Trust on the phone",
                           out.strip(),
                           "The phone is showing (or has dismissed) the Trust prompt. Unlock it "
                           "and tap Trust; no re-pairing needed.")
        if kind == "denied":
            return _result("iPhone reachability", FAIL, "The phone refused the pairing",
                           out.strip(),
                           "Someone tapped Do Not Trust. Re-pair over USB and tap Trust.")
        if kind in ("unpaired", "failed"):
            return _result("iPhone reachability", FAIL,
                           "Found over Wi-Fi, but pairing did not validate", out.strip(),
                           "The pairing record in /var/lib/lockdown is stale, half-written or for "
                           "a different host. Re-pair over USB and tap Trust; back up BOTH files "
                           "together.")
        if kind == "connection":
            return _result("iPhone reachability", FAIL,
                           "Pairing is not possible over this connection", out.strip(),
                           "The device will not pair over the transport in use. Pairing needs the "
                           "cable; refreshing afterwards does not.")
        return _result("iPhone reachability", FAIL, "Found over Wi-Fi, but validation failed",
                       out.strip(),
                       "Unrecognised idevicepair output -- it is quoted above verbatim. This does "
                       "not establish a pairing problem either way.")

    if usb:
        return _result(
            "iPhone reachability", WARN, "Reachable over USB ONLY -- wireless refresh will not work",
            "UDID %s. netmuxd: %s" % (usb[0], wnote or "running, but reports no device"),
            "Unattended refresh needs the phone reachable with no cable. Check the netmuxd "
            "container is up, that the phone is on this LAN, and that it advertises itself "
            "(see the next check).")

    # Tools absent entirely is not the same as "no device" -- saying FAIL there would be a
    # confident false negative on a host that simply lacks libimobiledevice.
    if "not installed" in (wnote or "") and "not installed" in (unote or ""):
        return _result("iPhone reachability", UNKNOWN, "idevice_id is not available here", wnote,
                       "Install libimobiledevice-utils. The container image ships it.")

    # _devices_via returns None when the probe COULD NOT RUN (timeout, or no mux answered) and []
    # when it ran and found nothing. Both are falsy, so both used to arrive at the confident
    # "No device on either transport" below -- asserting an absent phone from a question nobody
    # managed to ask. A dead netmuxd reads exactly like a phone that has left the house.
    if wireless is None or usb is None:
        which = []
        if wireless is None:
            which.append("netmuxd: %s" % (wnote or "did not answer"))
        if usb is None:
            which.append("usbmuxd: %s" % (unote or "did not answer"))
        return _result(
            "iPhone reachability", UNKNOWN, "Could not ask one of the transports",
            " | ".join(which),
            "This says NOTHING about whether the phone is present -- the question could not be "
            "put. A netmuxd that is down or whose socket is not mounted looks identical to an "
            "absent phone here, so check `docker ps` for netmuxd before looking for the phone. "
            "The host's usbmuxd exiting with no cable attached is NORMAL and not a fault.")

    return _result(
        "iPhone reachability", FAIL, "No device on either transport",
        "Both muxes answered and neither has a device. netmuxd: %s | usbmuxd: %s"
        % (wnote or "no device", unote or "no device"),
        "The phone is asleep, off this network, or has never been paired. The host's usbmuxd is "
        "udev-activated and exits with the cable removed, which is normal -- that is what the "
        "netmuxd container is for.")


def _browse(service, timeout=15):
    """Browse for a service. Returns (state, rows, note).

    state is "ok" (the browse ran), or an UNKNOWN reason. The distinction this exists to make:
    a browse that COULD NOT RUN is not the same as a browse that found nothing, and reporting
    the first as the second is how a probe failure becomes a confident "not published" -- the
    same disease as blaming a pairing record for an unreachable phone.
    """
    rc, out = _run(["avahi-browse", "-rpt", service], timeout=timeout)
    if rc is None:
        # _run collapses three cases into rc None. They need different advice: "install it" is
        # actively wrong for a tool that IS installed and timed out.
        if "not installed" in (out or ""):
            return "missing", [], out
        return "failed", [], out
    if rc != 0:
        # It ran and refused. Previously this fell through to the "found nothing" branch and was
        # announced as NOT PUBLISHED -- a definite negative from a probe that never answered.
        return "failed", [], "avahi-browse exited %d: %s" % (rc, (out or "").strip()[:200])
    rows = [l.split(";") for l in (out or "").splitlines() if l.startswith("=")]
    return "ok", [r for r in rows if len(r) > 6], out


def _is_ours(row):
    """Is this resolved row advertised by THIS host?"""
    host = (row[6] or "").split(".")[0].lower()
    return host == socket.gethostname().split(".")[0].lower()


def check_phone_advertisement():
    """Is the phone itself discoverable? netmuxd finds it by mDNS, so this is its precondition."""
    state, rows, note = _browse("_apple-mobdev2._tcp")
    if state == "missing":
        return _result("iPhone is advertising", UNKNOWN, "avahi-browse is not installed", note,
                       "Install avahi-utils. The container image ships it.")
    if state == "failed":
        # Not "the phone is not advertising". The probe did not answer, which says nothing about
        # the phone -- and the old message sent the owner to wake a phone that was already awake.
        return _result("iPhone is advertising", UNKNOWN, "Could not ask avahi", note,
                       "This says nothing about the phone. avahi-browse is installed but did not "
                       "complete: check avahi-daemon on the HOST and the D-Bus socket mount.")

    rows = [r for r in rows if len(r) > 8]
    if not rows:
        return _result(
            "iPhone is advertising", FAIL, "The phone is not advertising _apple-mobdev2._tcp",
            "avahi answered and no device is offering itself; netmuxd discovers the device this "
            "way, so it cannot find it.",
            "The phone must be awake, on this Wi-Fi, and have been paired over USB at least once. "
            "This advert is how a device offers itself for wireless access.")

    seen = sorted({"%s:%s" % (r[7], r[8]) for r in rows})
    return _result("iPhone is advertising", OK, "Discoverable over mDNS", ", ".join(seen))


def check_advertisement(service="_altserver._tcp"):
    """The only trustworthy advertisement test: browse for it, do not trust the server."""
    state, rows, note = _browse(service)
    if state == "missing":
        return _result("mDNS advertisement", UNKNOWN, "avahi-browse is not installed", note,
                       "Install avahi-utils. This is the ONLY reliable check: AltServer cannot "
                       "detect its own advertisement failing, and avahi can report success "
                       "while publishing nothing.")
    if state == "failed":
        return _result("mDNS advertisement", UNKNOWN, "Could not ask avahi", note,
                       "The probe did not complete, so this says NOTHING about whether the "
                       "advertisement is up -- do not read it as a failure. avahi-browse is "
                       "installed. Check that avahi-daemon is running on the HOST and that the "
                       "D-Bus socket is mounted; an AppArmor denial looks exactly like this.")

    ours = [r for r in rows if _is_ours(r)]
    if ours:
        return _result("mDNS advertisement", OK, "%s is published" % service,
                       "This host, on: %s" % ", ".join(sorted({r[1] for r in ours})))
    if rows:
        # Someone ELSE's AltServer. This used to read as OK, so a second server on the LAN -- or
        # a stale record from a machine that has gone -- made this check green while this host
        # advertised nothing at all.
        return _result("mDNS advertisement", FAIL,
                       "%s is published, but NOT by this host" % service,
                       "Seen from: %s. This host is %s."
                       % (", ".join(sorted({r[6] for r in rows})), socket.gethostname()),
                       "Another AltServer on this LAN is advertising, or a stale record has not "
                       "expired. Your phone may pair with that one instead. This server is still "
                       "undiscoverable.")
    return _result("mDNS advertisement", FAIL, "%s is NOT published" % service,
                   "avahi answered and nothing is advertising it, so AltStore cannot discover "
                   "this server.",
                   "Check python3 and libavahi-compat-libdnssd1 (it provides libdns_sd.so.1, "
                   "which the code dlopens) and that avahi-daemon is running.")


def _shares_host_pids():
    """Can we see processes outside this container?

    Read PID 1's command line, which answers it directly: under `pid: host` PID 1 is the host's
    init; in our own namespace it is this web server. No ptrace needed.

    The previous test asked whether /proc/1/root/usr/local/bin/AltServer existed, and had the
    answer backwards. Under `pid: host` -- which the shipped stack uses -- PID 1 IS the host
    init, so /proc/1/root is the host filesystem, which has no AltServer binary; it only exists
    inside the image. So the guard fired in exactly the deployment where this check CAN see
    everything, and a genuinely dead daemon was reported as "this check is blind". Combined with
    the match being too loose, the check could not return FAIL at all in the shipped stack --
    and "a verification that cannot fail is worse than none".
    """
    try:
        with open("/proc/1/cmdline", "rb") as fh:
            first = fh.read(4096).split(b"\x00")[0].decode("utf-8", "replace")
    except OSError:
        return False
    # Our own namespace: PID 1 is this process tree's entry point.
    return not (first.endswith("python3") or first.endswith("python")
                or "server.py" in first or first.endswith("/AltServer"))


def check_altserver_running():
    """Is the daemon up? Only meaningful if we can actually see its process."""
    rc, out = _run(["pgrep", "-af", "AltServer"])
    if rc is None:
        return _result("AltServer process", UNKNOWN, "Could not run pgrep", out,
                       "Install procps. The container image ships it.")

    # Match the BINARY, not any command line mentioning the word. `pgrep -af AltServer` also
    # matches this web server's own install subprocess (which passes the AltServer path as an
    # argument) and anything with AltServer.ipa on its line, so "Running" could be claimed from
    # the very process doing the asking.
    lines = [l for l in out.splitlines()
             if re.search(r"(^|\s|/)AltServer(\s|$)", l)
             and "pgrep" not in l and "server.py" not in l and ".ipa" not in l]
    if lines:
        return _result("AltServer process", OK, "Running", lines[0][:160])

    if not _shares_host_pids():
        return _result(
            "AltServer process", UNKNOWN,
            "Cannot see other containers' processes from here",
            "PID 1 here is this web server, so we are in our own PID namespace and pgrep cannot "
            "see the daemon however healthy it is. Set pid: host on this service to make this "
            "check meaningful.",
            "Judge by the mDNS check above: it browses for the advertisement from outside, which "
            "is the one signal that does not depend on seeing the process.")

    return _result("AltServer process", FAIL, "Not running",
                   "We share the host PID namespace and no AltServer process exists.",
                   "AltServer has no liveness signal: Listen() can fail early and the process "
                   "stays alive with no listener, so 'running' is necessary but not sufficient. "
                   "A published advertisement does not prove the daemon is alive either -- the "
                   "record outlives the process. Trust the mDNS check for discoverability and "
                   "this one for liveness; they answer different questions.")


# The checks that depend on the PHONE being present, as opposed to the server working.
#
# This distinction matters for the container healthcheck. check_device returns FAIL when the
# phone is on neither transport, which is correct on the dashboard and wrong as a verdict on the
# server: take the phone out of the house and the container would go unhealthy every time. A
# badge that is red whenever its owner leaves gets ignored, which is how alert fatigue starts --
# and an ignored badge is no better than the no-badge state that let the 09-22 outage run for
# days.
DEVICE_DEPENDENT = ("iPhone reachability", "iPhone is advertising")

# Where the run-by-run history lives. The web container and altserver share this volume.
HISTORY_PATH = os.environ.get("ALTSERVER_HISTORY", "/data/status-history.jsonl")
# ~17 days at the healthcheck's 5-minute cadence, about 750KB. Long enough to answer "when did
# this start" across a 7-day certificate cycle, which is the question the 2026-09-22 outage could
# not answer: the advertisement had been dead for somewhere between three and nine days and
# nothing anywhere recorded which.
HISTORY_MAX = int(os.environ.get("ALTSERVER_HISTORY_MAX", "5000"))


def record(result, path=None):
    """Append one run to the history. Returns None on success, or a reason string.

    Deliberately returns the reason rather than raising: recording must never break the checks
    or the healthcheck that calls it. But it must not vanish either -- a history that silently
    stopped recording would look exactly like a server that has been fine all along, which is
    the failure this whole file now exists to prevent. The caller reports what comes back.
    """
    path = path or HISTORY_PATH
    line = json.dumps({
        "t": int(time.time()),
        "overall": result["overall"],
        "checks": {c["name"]: c["state"] for c in result["checks"]},
    }, sort_keys=True)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:
        return "%s: %s" % (type(exc).__name__, exc)

    # Trim by LINE COUNT, so the timeline is a predictable span of TIME rather than of bytes.
    #
    # This used to skip the count unless the file exceeded HISTORY_MAX * 400 bytes, as a cheap
    # way to avoid reading it every run. That guess does not bind: with the short lines a
    # four-check run produces, the file held roughly double the intended window before the
    # threshold was ever reached. A bound that depends on how long the check names happen to be
    # is not a bound. The read costs nothing at a five-minute cadence on a file this size.
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        if len(lines) > HISTORY_MAX:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(lines[-HISTORY_MAX:])
            os.replace(tmp, path)   # atomic: a reader never sees a half-written file
    except Exception as exc:
        return "trim failed: %s: %s" % (type(exc).__name__, exc)
    return None


def read_history(path=None, limit=None):
    """Recent runs, oldest first. Never raises; an unreadable history is an empty one."""
    path = path or HISTORY_PATH
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    continue      # a torn line must not discard the rest of the timeline
    except OSError:
        return []
    return out[-limit:] if limit else out


def verdict(checks, server_only=False):
    """Roll per-check states into one overall verdict.

    server_only EXCLUDES the device checks from the verdict -- it does not stop them running.
    That distinction was got wrong first time round and is worth stating: the healthcheck needs
    to ignore "the phone is out of the house" when deciding whether the SERVER is healthy, but
    the history wants those checks recorded anyway, or "when did the phone drop off the network"
    becomes unanswerable for exactly the same reason the mDNS outage was.
    """
    considered = [c for c in checks
                  if not (server_only and c["name"] in DEVICE_DEPENDENT)]
    states = [c["state"] for c in considered]
    if FAIL in states:
        return FAIL
    if WARN in states or UNKNOWN in states:
        return WARN
    return OK


def _result_history_broken(reason):
    return _result("Status history", WARN, "Could not record this run", reason,
                   "The checks themselves ran fine; only the timeline is affected. Check that "
                   "%s is writable and that the volume is not full." % HISTORY_PATH)


def run_all(anisette_url=None, server_only=False, polling=False):
    """Run every check, in parallel apart from the one real dependency.

    server_only narrows the OVERALL VERDICT to what the server is responsible for. Every check
    still runs and still appears in the result.

    These were serial, which made the page as slow as the SUM of its checks. Two of them shell out
    to avahi-browse with a 15s timeout, so when an AppArmor rule started denying avahi's D-Bus
    signals the dashboard took over half a minute to render anything -- the checks were reporting
    a problem correctly and the page was unusable while they did it.

    They are subprocess and HTTP calls, so threads are the right tool: the page is now as slow as
    its SLOWEST check, not their total. check_clock is the one genuine dependency, needing the
    timestamp check_anisette collected, so anisette runs first and the rest run together.

    A check that raises must not take the dashboard with it -- that is what the whole page exists
    to avoid -- so each result is collected defensively.
    """
    anisette = check_anisette(anisette_url, polling=polling)

    def _clock():
        return check_clock(anisette.get("anisette_time"))

    rest = [_clock, check_device, check_phone_advertisement,
            check_advertisement, check_altserver_running]
    # Positional names, so a check that raises still reports under its own heading.
    _CHECK_NAMES = ["Clock agreement", "iPhone reachability", "iPhone is advertising",
                    "mDNS advertisement", "AltServer process"]

    results = [None] * len(rest)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(rest)) as pool:
        futures = {pool.submit(fn): i for i, fn in enumerate(rest)}
        for fut in concurrent.futures.as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as exc:  # a broken check must not blank the page
                # Keep the check's NAME. Calling it "Check #3" breaks its row in the history
                # timeline (which is keyed by name) and loses its device-dependent
                # classification, so a crashed device check would start marking the container
                # unhealthy -- the thing --server-only exists to prevent.
                results[i] = _result(_CHECK_NAMES[i], UNKNOWN,
                                     "This check raised an exception", str(exc),
                                     "The other checks are unaffected. This is a bug in the "
                                     "check itself, not necessarily a fault in what it probes.")

    checks = [anisette] + results
    return {"overall": verdict(checks, server_only), "checks": checks,
            "host": socket.gethostname()}


if __name__ == "__main__":
    # Exits non-zero when something is wrong, so this is usable from cron, a Docker healthcheck or
    # a monitoring agent without parsing the JSON. See EXIT_CODES.
    #
    # -q prints ONE line naming what is wrong instead of the full JSON. Docker keeps only the last
    # 5 healthcheck outputs and truncates each to 4KB, so the full document would be clipped
    # mid-object and tell an operator running `docker inspect` nothing useful.
    _quiet = "-q" in sys.argv[1:] or "--quiet" in sys.argv[1:]
    # --server-only: judge the SERVER, not whether the phone happens to be home. See
    # DEVICE_DEPENDENT above for why a healthcheck wants this and the dashboard does not.
    _server_only = "--server-only" in sys.argv[1:]
    # --record appends this run to the history, so the page can answer "when did this start"
    # rather than only "is it broken now". The healthcheck is the writer: it already runs every
    # five minutes, which is a better cadence than anything a page load would produce.
    _record = "--record" in sys.argv[1:]
    # Anything on a timer is polling. See check_anisette: the bare root provisions against Apple
    # when the identity is missing, so it must not be fetched every five minutes.
    _polling = _record or _server_only or "--polling" in sys.argv[1:]
    _run = run_all(server_only=_server_only, polling=_polling)

    _rec_err = record(_run) if _record else None
    if _rec_err:
        # Loud, and it DEGRADES the verdict. A history that quietly stopped recording looks
        # identical to a server that has been fine all along -- which is the exact shape of
        # every bug this file has been fixed for. Never worse than the checks themselves said,
        # so a recording problem cannot mask a real failure.
        sys.stderr.write("status_checks: could not record history (%s)\n" % _rec_err)
        _run["checks"].append(_result_history_broken(_rec_err))
        _run["overall"] = verdict(_run["checks"], _server_only)

    if _quiet:
        _bad = [c["name"] for c in _run["checks"] if c["state"] in (FAIL, WARN, UNKNOWN)]
        print("%s%s" % (_run["overall"], (": " + ", ".join(_bad)) if _bad else ""))
    else:
        print(json.dumps(_run, indent=2))
    sys.exit(exit_code(_run["overall"]))
