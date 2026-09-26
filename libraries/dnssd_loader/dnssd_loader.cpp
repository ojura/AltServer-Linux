#include "dns_sd.h"
#include <iostream>
#include <string>
#include <unistd.h>
#include <sys/prctl.h> // prctl(), PR_SET_PDEATHSIG
#include <signal.h> // signals
#include <sys/wait.h> // waitpid()
#include <arpa/inet.h> // ntohs()


// The advertisement helper, run as `python3 -c <this> <args>`.
//
// WHY A HELPER AT ALL. AltServer is linked -static and therefore cannot dlopen Bonjour itself,
// so registration is delegated to python3, which can.
//
// WHY IT HAS A WATCHDOG. The registration used to be made once and then trusted forever, and on
// 2026-09-22 that failed in production: avahi-daemon restarted, every registration on the box was
// dropped, and this helper carried on holding a handle to a service that no longer existed. The
// server was undiscoverable for three days while every other health signal stayed green.
//
// It could not have noticed. avahi-compat's background thread is not a poll loop: it runs exactly
// one poll(), writes COMMAND_POLL_DONE, and then blocks reading for a command that only
// DNSServiceProcessResult() ever sends -- and nothing in this project calls that. So the compat
// event loop is parked permanently after the first poll. The client never reaches
// AVAHI_CLIENT_FAILURE, the callback never fires (it is NULL here anyway), and avahi's
// NameOwnerChanged signal sits unread in the D-Bus socket forever. DNSServiceRefSockFD, which is
// how a caller would drive that loop, is the stub below returning 0xDEADBEEF.
//
// The consequence is that NO in-process signal can work. Liveness is not a signal either: the
// helper stays alive and healthy-looking with a dead registration. The only honest test is the
// one an outside observer would make, so that is what this does -- it browses for its own record
// and re-registers when it is actually gone.
//
// Three rules keep it from making things worse:
//   * It matches on PORT plus the serverID TXT value, never the service name. flags is 0 here, so
//     on a name collision avahi RENAMES to "AltServer #2" rather than withdrawing; matching by
//     name would re-register forever against a perfectly healthy record.
//   * A browse that errors or times out is UNKNOWN, not absent. It never re-registers on
//     ignorance.
//   * It wants two consecutive misses before acting, and deallocates before re-registering, so a
//     single false negative costs a sub-second gap rather than a duplicate record.
//
// Arguments arrive as argv rather than interpolated source, so a service name containing a quote
// cannot break the program, and so a test can execute this exact text.
static const char *kAdvertiseHelper = R"PY(
import os, sys, time, shutil, socket, subprocess
from ctypes import (CDLL, POINTER, byref, create_string_buffer,
                    c_void_p, c_uint32, c_uint16, c_int, c_char_p)
from threading import Event

flags, iface, name, regtype, domain, host, port_nbo, txt_hex = sys.argv[1:9]
flags = int(flags)
iface = int(iface)
port_nbo = int(port_nbo)
txt = bytes.fromhex(txt_hex)
name = name.encode() or None
regtype = regtype.encode() or None
domain = domain.encode() or None
host = host.encode() or None

# The versioned soname first: it is what the runtime package ships (libavahi-compat-libdnssd1 on
# Debian/Ubuntu). The unversioned libdns_sd.so symlink exists only where the -dev package is
# installed, so loading that alone made the -dev package a runtime requirement.
try:
    dll = CDLL('libdns_sd.so.1')
except OSError:
    dll = CDLL('libdns_sd.so')
# Declared explicitly. sdRef is a DNSServiceRef, which is a POINTER: the previous version passed a
# 4-byte c_int, so on any 64-bit build the daemon wrote 8 bytes into 4 and the stored handle was
# truncated. Harmless only while nobody used the handle; this file now deallocates it.
dll.DNSServiceRegister.restype = c_int
dll.DNSServiceRegister.argtypes = [POINTER(c_void_p), c_uint32, c_uint32,
                                   c_char_p, c_char_p, c_char_p, c_char_p,
                                   c_uint16, c_uint16, c_void_p, c_void_p, c_void_p]
dll.DNSServiceRefDeallocate.restype = None
dll.DNSServiceRefDeallocate.argtypes = [c_void_p]

SERVICE = (regtype or b'_altserver._tcp').decode()
PORT = socket.ntohs(port_nbo)   # avahi-browse prints host byte order; the API takes network order


def say(msg):
    # Explicit flush: stdout is a pipe into the redaction filter, so it is block buffered and a
    # long-running process would otherwise never show a line.
    print(msg)
    sys.stdout.flush()


def server_id():
    """The serverID= entry, parsed out of the length-prefixed DNS-SD TXT record."""
    i = 0
    while i < len(txt):
        n = txt[i]
        i += 1
        item = txt[i:i + n]
        i += n
        if item.startswith(b'serverID='):
            return item.decode('ascii', 'replace')
    return ''


SERVER_ID = server_id()


def register():
    ref = c_void_p()
    buf = create_string_buffer(txt, len(txt)) if txt else None
    rc = dll.DNSServiceRegister(byref(ref), flags, iface, name, regtype, domain, host,
                                port_nbo, len(txt), buf, None, None)
    return rc, ref, buf   # buf is returned only to keep it alive alongside the registration


def published():
    """True, False, or None for 'cannot tell'. None must never trigger a re-registration."""
    try:
        p = subprocess.run(['avahi-browse', '-rpt', SERVICE],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    for line in p.stdout.decode('utf-8', 'replace').splitlines():
        if not line.startswith('='):
            continue
        f = line.split(';')
        if len(f) < 10:        # =;iface;proto;name;type;domain;host;address;port;txt
            continue
        if f[8] != str(PORT):
            continue
        if SERVER_ID and SERVER_ID not in f[9]:
            continue
        return True
    return False


rc, ref, buf = register()
say('DNSServiceRegister result: %d' % rc)
if rc != 0:
    # The parent polls waitpid for about a second and treats a prompt exit as failure. Keep it.
    sys.exit(1)

try:
    interval = int(os.environ.get('ALTSERVER_MDNS_RECHECK_SECONDS', '60'))
except ValueError:
    interval = 60

if interval <= 0 or not shutil.which('avahi-browse'):
    say('mDNS watchdog: OFF (interval %d, avahi-browse %s). The advertisement will not be '
        're-checked; if avahi-daemon restarts this server goes silently undiscoverable.'
        % (interval, 'present' if shutil.which('avahi-browse') else 'missing'))
    Event().wait()

# Prove the instrument works before trusting it. If avahi-browse cannot run, every check below
# returns UNKNOWN and the watchdog quietly does nothing forever -- which is the same silent
# failure it exists to prevent, just moved one level up. Say so loudly instead.
if published() is None:
    say('mDNS watchdog: WARNING -- avahi-browse did not run, so the advertisement CANNOT be '
        'verified. The watchdog is loaded but blind; a dropped registration will go unnoticed. '
        'Check that avahi-utils is installed and that the D-Bus system bus socket is mounted.')
else:
    say('mDNS watchdog: ON, re-checking %s every %ds' % (SERVICE, interval))

misses = 0
unknowns = 0
while True:
    time.sleep(interval)
    state = published()
    if state is None:
        # Never act on ignorance -- but do not stay quiet about it either. A permanently failing
        # browse is indistinguishable from a healthy server unless it is reported.
        misses = 0
        unknowns += 1
        if unknowns in (5, 50) or (unknowns and unknowns % 500 == 0):
            say('mDNS watchdog: %d consecutive checks could not tell whether %s is published. '
                'Not re-registering, because an unreadable browse is not proof of absence.'
                % (unknowns, SERVICE))
        continue
    unknowns = 0
    if state:
        misses = 0
        continue
    misses += 1
    if misses < 2:
        continue
    misses = 0
    say('mDNS watchdog: %s is NOT published; re-registering' % SERVICE)
    try:
        dll.DNSServiceRefDeallocate(ref)
    except Exception as exc:
        # Prefer leaking the old sdref (a few KB and one parked thread, once per avahi restart)
        # over blocking here. A hang would leave the advertisement dead permanently.
        say('mDNS watchdog: deallocate failed, continuing anyway: %s' % exc)
    rc, ref, buf = register()
    say('DNSServiceRegister result: %d (re-registration)' % rc)
)PY";


DNSServiceErrorType DNSSD_API DNSServiceRegister
    (
    DNSServiceRef                       *sdRef,
    DNSServiceFlags                     flags,
    uint32_t                            interfaceIndex,
    const char                          *name,         /* may be NULL */
    const char                          *regtype,
    const char                          *domain,       /* may be NULL */
    const char                          *host,         /* may be NULL */
    uint16_t                            port,
    uint16_t                            txtLen,
    const void                          *txtRecord,    /* may be NULL */
    DNSServiceRegisterReply             callBack,      /* may be NULL */
    void                                *context       /* may be NULL */
    ) {
        std::string flagsStr = std::to_string(flags);
        std::string ifaceStr = std::to_string(interfaceIndex);
        std::string portStr = std::to_string(port);

        // Plain hex, decoded by bytes.fromhex on the other side. Read through an UNSIGNED char:
        // a signed one sign-extends any byte >= 0x80 into "\xFFFFFF80" and corrupts the record.
        std::string txtHex;
        txtHex.reserve(txtLen * 2);
        for (uint16_t i = 0; i < txtLen; i++) {
            char buf[4] = { 0 };
            snprintf(buf, sizeof(buf), "%02X", ((const unsigned char *)txtRecord)[i]);
            txtHex += buf;
        }

        printf("Advertising over mDNS via python3: %s port %d\n",
               regtype ? regtype : "(null)", ntohs(port));

        pid_t ppid_before_fork = getpid();
        int child;
        if ((child = fork()) < 0) {
            perror("fork");
            return EXIT_FAILURE;
        }
        if(child == 0){
            int r = prctl(PR_SET_PDEATHSIG, SIGTERM);
            if (r == -1) { perror(0); exit(1); }
            // test in case the original parent exited just
            // before the prctl() call
            if (getppid() != ppid_before_fork)
                exit(1);
            // With -c, the first argument after the program text lands in sys.argv[1]. An empty
            // string stands for NULL, which is why these are not quoted or escaped anywhere.
            execlp("python3", "python3", "-c", kAdvertiseHelper,
                   flagsStr.c_str(),
                   ifaceStr.c_str(),
                   name ? name : "",
                   regtype ? regtype : "",
                   domain ? domain : "",
                   host ? host : "",
                   portStr.c_str(),
                   txtHex.c_str(),
                   (char *)NULL);
            exit(1);
        } else {
            // The child is meant to run forever -- it ends in either Event().wait() or the
            // watchdog loop. So if it exits promptly, advertisement failed. Poll rather than
            // block: a healthy child never exits, and a plain waitpid() would hang here for the
            // life of the server.
            //
            // This check matters more than its size suggests. Without it this function returned
            // success unconditionally, so a missing python3 or an unloadable libdns_sd.so left
            // AltServer running, logging nothing wrong, and completely undiscoverable by the
            // device. On an unattended headless server that is the worst possible failure mode:
            // nobody finds out until a sideloaded app expires a week later.
            bool advertised = true;

            for (int attempt = 0; attempt < 20; attempt++) // ~1 second total
            {
                usleep(50 * 1000);

                int status = 0;
                pid_t result = waitpid(child, &status, WNOHANG);

                if (result == child)
                {
                    advertised = false;
                    break;
                }

                if (result < 0)
                {
                    // Cannot tell either way; assume it is running rather than cry wolf.
                    break;
                }
            }

            if (!advertised)
            {
                fprintf(stderr,
                    "ERROR: could not advertise this server over mDNS -- the python3 helper exited\n"
                    "       immediately. AltStore on your device will NOT be able to discover this\n"
                    "       server, and refreshing will never happen.\n"
                    "       Verify with the same call this program makes:\n"
                    "           python3 -c \"from ctypes import CDLL; CDLL('libdns_sd.so.1')\"\n"
                    "       On Debian/Ubuntu install libavahi-compat-libdnssd1, which provides\n"
                    "       libdns_sd.so.1, and make sure avahi-daemon is running.\n");

                return kDNSServiceErr_Unknown;
            }

            printf("Advertising this server over mDNS as _altserver._tcp on port %d\n", ntohs(port));
        }
        return 0;
    }

int DNSSD_API DNSServiceRefSockFD(DNSServiceRef sdRef) {
    return 0xDEADBEEF;
}
