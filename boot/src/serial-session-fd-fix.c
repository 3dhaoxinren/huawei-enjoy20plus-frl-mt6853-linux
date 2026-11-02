#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <termios.h>
#include <unistd.h>

static const char *control = "/run/frl/control";
static const char *device = "/dev/ttyGS0";
static const char *busybox = "/usr/lib/frl/busybox";
static const char *script = "/usr/lib/frl/serial-session.sh";
static void diagnostic(const char *stage, int error) {
    char line[384];
    int n = snprintf(line, sizeof line, "FRL_SERIAL_SESSION: %s errno=%d pid=%ld sid=%ld pgid=%ld\n",
                     stage, error, (long)getpid(), (long)getsid(0), (long)getpgrp());
    size_t size = n < 0 ? 0 : (size_t)n < sizeof line ? (size_t)n : sizeof line - 1;
#ifndef FRL_SERIAL_HOST_TEST
    int fd = open("/var/log/frl/serial-session.log", O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK, 0600);
    if (fd < 0) fd = open("/run/frl/log/serial.log", O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK, 0600);
    if (fd >= 0) {
        struct stat st;
        if (!fstat(fd, &st) && S_ISREG(st.st_mode) && st.st_uid == 0) { ssize_t r=write(fd,line,size); (void)r; }
        close(fd);
    }
    fd = open("/dev/kmsg", O_WRONLY|O_NONBLOCK|O_CLOEXEC);
    if (fd >= 0) { ssize_t r=write(fd,line,size); (void)r; close(fd); }
#else
    ssize_t r=write(STDERR_FILENO,line,size); (void)r;
#endif
}
static unsigned long long start(pid_t pid) {
    char path[64], line[2048]; snprintf(path, sizeof path, "/proc/%ld/stat", (long)pid);
    FILE *f = fopen(path, "r"); if (!f) return 0;
    char *ok = fgets(line, sizeof line, f); fclose(f); if (!ok) return 0;
    char *end = strrchr(line, ')'), *save = NULL;
    if (!end || end[1] != ' ') return 0;
    char *s = strtok_r(end + 2, " ", &save);
    for (int n = 3; s; ++n, s = strtok_r(NULL, " ", &save))
        if (n == 22) return strtoull(s, NULL, 10);
    return 0;
}
static int owner_alive(pid_t pid, unsigned long long expected) {
    char path[256]; snprintf(path, sizeof path, "%s/serial-controller.pid", control);
    FILE *f = fopen(path, "r"); long recorded = 0; unsigned long long stamp = 0;
    if (!f) return 0;
    int n = fscanf(f, "%ld %llu", &recorded, &stamp); fclose(f);
    return n == 2 && recorded == pid && stamp == expected &&
           start(pid) == expected && !kill(pid, 0);
}
static int publish(pid_t owner, unsigned long long owner_start) {
    char path[256], temp[300];
    snprintf(path, sizeof path, "%s/serial-session.%ld.%llu.pid", control, (long)owner, owner_start);
    snprintf(temp, sizeof temp, "%s.%ld", path, (long)getpid());
    int fd = open(temp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) return -1;
    int ok = dprintf(fd, "%ld %llu\n", (long)getpid(), start(getpid())) > 0;
    close(fd); if (!ok || rename(temp, path)) { unlink(temp); return -1; }
    return 0;
}
static int fail(const char *why) {
    int error = errno; diagnostic(why, error); return 78;
}
int main(int argc, char **argv) {
    if (getpid() == 1 || geteuid() != 0) return fail("root non-PID1 worker required");
#ifdef FRL_SERIAL_HOST_TEST
    if (argc != 7) return fail("fixture arguments");
    device = argv[3]; control = argv[4]; busybox = argv[5]; script = argv[6];
#else
    if (argc != 3) return fail("controller identity arguments");
#endif
    char *end = NULL;
    long owner = strtol(argv[1], &end, 10);
    if (!*argv[1] || *end || owner <= 1 || owner > 0x7fffffff) return fail("controller PID");
    unsigned long long stamp = strtoull(argv[2], &end, 10);
    if (!*argv[2] || *end || !stamp || !owner_alive((pid_t)owner, stamp)) return fail("controller identity");
    int ready = getenv("FRL_SERIAL_READY_FD") && !strcmp(getenv("FRL_SERIAL_READY_FD"), "3");
    if (ready) { struct stat st; if (fstat(3, &st) || !S_ISFIFO(st.st_mode)) return fail("ready pipe"); }
    if (setsid() < 0) return fail("setsid");
    diagnostic("setsid-ok", 0);
    /* Publish before opening the tty, then recheck owner. After the controller
     * is dead, a not-yet-published worker cannot subsequently acquire the tty. */
    if (publish((pid_t)owner, stamp) || !owner_alive((pid_t)owner, stamp)) return fail("publish/owner changed");
    diagnostic("open-begin", 0);
    int fd = open(device, O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) return fail("open tty");
    diagnostic("open-ok", 0);
    if (ioctl(fd, TIOCSCTTY, 0)) return fail("TIOCSCTTY-no-steal");
    if (tcsetpgrp(fd, getpgrp())) return fail("tcsetpgrp");
    if (tcgetsid(fd) != getpid() || tcgetpgrp(fd) != getpgrp()) { errno=EPROTO; return fail("ctty/foreground verification"); }
    diagnostic("ctty-foreground-ok", 0);
    struct termios attr;
    if (tcgetattr(fd, &attr)) return fail("termios read");
    cfmakeraw(&attr); attr.c_lflag &= ~ECHO; /* preserve driver-reported baud */
    if (tcsetattr(fd, TCSANOW, &attr)) return fail("termios raw");
    int flags = fcntl(fd, F_GETFL);
    if (flags < 0 || fcntl(fd, F_SETFL, flags & ~O_NONBLOCK)) return fail("blocking shell IO");
    if (!owner_alive((pid_t)owner, stamp)) return fail("owner gone before ready");
    for (int target = 0; target < 3; ++target) {
        /* open() can reuse a missing standard descriptor. dup2(fd,fd) is a
         * no-op and preserves FD_CLOEXEC, so clear it explicitly on all three. */
        if (dup2(fd, target) < 0 || fcntl(target, F_SETFD, 0) < 0) return fail("stdio inherit");
    }
    if (fd > 2) close(fd);
    if (ready) {
        signal(SIGPIPE, SIG_IGN);
        diagnostic("READY-checks-ok", 0);
        ssize_t n = write(3, "R", 1); close(3);
        if (n != 1) return fail("ready write");
    }
    unsetenv("FRL_SERIAL_READY_FD");
    execl(busybox, "busybox", "sh", script, (char *)NULL);
    return fail("exec shell");
}
