#define _GNU_SOURCE
#include "frl-watchdog.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdlib.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#ifndef FRL_WATCHDOG_HOST_TEST
#include <linux/reboot.h>
#include <sys/syscall.h>
#endif

static int64_t monotonic_ms(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) return -1;
    return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

static int close_other_fds(int keep) {
    DIR *d = opendir("/proc/self/fd");
    if (!d) return -1; /* proc is a documented bootstrap prerequisite */
    int scan_fd = dirfd(d);
    struct dirent *e;
    while ((e = readdir(d))) {
        char *end;
        long n = strtol(e->d_name, &end, 10);
        if (!*e->d_name || *end || n < 0 || n > INT_MAX) continue;
        if (n != keep && n != scan_fd) close((int)n);
    }
    return closedir(d);
}

/* Writers use O_CREAT|O_EXCL, close the empty marker, and retry on EEXIST.
 * No symlink following or FIFO opens that could block recovery. Only one trusted
 * producer is permitted; it must not replace/remove markers being consumed.
 */
static int consume(int dir, const char *name) {
    int fd = openat(dir, name, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return errno == ENOENT || errno == ELOOP ? 0 : -1;
    struct stat s;
    int rc = fstat(fd, &s);
    close(fd);
    if (rc) return -1;
    if (!S_ISREG(s.st_mode) || s.st_uid != geteuid() || s.st_nlink != 1) return 0;
    if (unlinkat(dir, name, 0)) return errno == ENOENT ? 0 : -1;
    return 1;
}

static enum frl_wd_result watch(int dir, uint32_t timeout) {
    int64_t now = monotonic_ms();
    if (now < 0) return FRL_WD_ERROR;
    int64_t deadline = now + timeout;
    for (;;) {
        now = monotonic_ms();
        if (now < 0) return FRL_WD_ERROR;
        if (now >= deadline) return FRL_WD_TIMEOUT;
        int result = consume(dir, "request-exit");
        if (result < 0) return FRL_WD_ERROR;
        if (result) return FRL_WD_REQUEST;
        result = consume(dir, "keepalive");
        if (result < 0) return FRL_WD_ERROR;
        if (result) deadline = now + timeout;
        int64_t wake = now + 100;
        if (wake > deadline) wake = deadline;
        struct timespec t = { .tv_sec = wake / 1000,
                             .tv_nsec = (wake % 1000) * 1000000 };
        int rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &t, NULL);
        if (rc && rc != EINTR) return FRL_WD_ERROR;
    }
}

static void finish(enum frl_wd_result result) __attribute__((noreturn));
static void finish(enum frl_wd_result result) {
#ifdef FRL_WATCHDOG_HOST_TEST
    _exit((int)result);
#else
    (void)result;
    /* Recovery must not hang in sync() on a failed filesystem. Normal clean
     * shutdown belongs to PID1; this is its bounded emergency fallback. */
    for (;;) {
        syscall(SYS_reboot, LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2,
                LINUX_REBOOT_CMD_RESTART2, "bootloader");
        struct timespec retry = { .tv_sec = 1, .tv_nsec = 0 };
        nanosleep(&retry, NULL);
    }
#endif
}

int frl_watchdog_start(int dir, const struct frl_wd_config *config, pid_t *pid) {
    uint32_t timeout = config && config->timeout_ms ? config->timeout_ms :
                       FRL_WD_DEFAULT_TIMEOUT_MS;
    if (!pid) { errno = EINVAL; return -1; }
    if (timeout < FRL_WD_DEFAULT_TIMEOUT_MS) {
#if defined(FRL_WATCHDOG_HOST_TEST) || defined(FRL_WATCHDOG_ALLOW_DEBUG_TIMEOUT)
        if (!config || !config->debug_short_timeout || timeout < 100) {
            errno = EINVAL; return -1;
        }
#else
        errno = EINVAL; return -1;
#endif
    }
#ifndef FRL_WATCHDOG_HOST_TEST
    if (getpid() != 1 || geteuid() != 0) { errno = EPERM; return -1; }
#endif
    struct stat s;
    if (fstat(dir, &s)) return -1;
    if (!S_ISDIR(s.st_mode) || s.st_uid != geteuid() || (s.st_mode & 0077)) {
        errno = EACCES; return -1;
    }
    int owned = fcntl(dir, F_DUPFD_CLOEXEC, 3);
    if (owned < 0) return -1;
    pid_t child = fork();
    if (child < 0) { int saved = errno; close(owned); errno = saved; return -1; }
    if (!child) {
        /* Ignore inherited handlers/masks: TERM is deliberately blocked so
         * supervisor shutdown cannot accidentally disable the rescue timer.
         * PID1 may explicitly SIGKILL it only after taking over recovery. */
        sigset_t blocked;
        sigfillset(&blocked);
        if (sigprocmask(SIG_SETMASK, &blocked, NULL) || close_other_fds(owned))
            finish(FRL_WD_ERROR);
        finish(watch(owned, timeout));
    }
    close(owned);
    *pid = child;
    return 0;
}
