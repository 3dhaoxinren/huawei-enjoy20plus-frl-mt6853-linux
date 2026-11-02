#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <linux/reboot.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/sysmacros.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "frl-watchdog.h"
#include "native-log-display.h"

#define BB "/usr/lib/frl/busybox"
#ifndef FRL_SERIAL_CONTROL
#define FRL_SERIAL_CONTROL "/run/frl/control"
#endif
static int logfile = -1, kernel_log = -1;
static pid_t watchdog_pid = -1;
static unsigned long long watchdog_start_time;

static void emit(const char *text) {
    if (logfile >= 0) {
        ssize_t n = write(logfile, text, strlen(text));
        if (n != (ssize_t)strlen(text) && kernel_log >= 0)
            (void)dprintf(kernel_log, "<3>FRL_V15: RAM log write incomplete\n");
    }
    if (kernel_log >= 0) (void)dprintf(kernel_log, "<3>FRL_V15: %s", text);
    if (access("/tmp/frl-display-owned", F_OK)) display_line(text);
}

static void recovery(void) __attribute__((noreturn));
static void recovery(void) {
    for (;;) {
        syscall(SYS_reboot, LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2,
                LINUX_REBOOT_CMD_RESTART2, "bootloader");
        sleep(1);
    }
}

static int directory(const char *path, mode_t mode) {
    if (mkdir(path, mode) && errno != EEXIST) return -1;
    struct stat s;
    if (lstat(path, &s) || !S_ISDIR(s.st_mode)) return -1;
    return 0;
}

static int mounts_initial(void) {
    const char *dirs[] = {"/dev", "/proc", "/sys", "/run", "/tmp", "/newroot"};
    for (size_t i = 0; i < sizeof dirs / sizeof dirs[0]; ++i)
        if (directory(dirs[i], 0755)) return -1;
    if (mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) ||
        mount("sysfs", "/sys", "sysfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) ||
        mount("tmpfs", "/dev", "tmpfs", MS_NOSUID, "mode=0755") ||
        mount("tmpfs", "/run", "tmpfs", MS_NOSUID | MS_NODEV, "mode=0755") ||
        mount("tmpfs", "/tmp", "tmpfs", MS_NOSUID | MS_NODEV, "mode=1777")) return -1;
    if (mknod("/dev/null", S_IFCHR | 0666, makedev(1, 3)) ||
        mknod("/dev/console", S_IFCHR | 0600, makedev(5, 1)) ||
        mknod("/dev/kmsg", S_IFCHR | 0600, makedev(1, 11))) return -1;
    if (directory("/run/frl", 0755) || directory("/run/frl/control", 0700) ||
        directory("/run/frl/log", 0700) || directory("/run/frl/boot", 0700)) return -1;
    return symlink("/run/frl/control/keepalive", "/tmp/frl-keepalive") ||
           symlink("/run/frl/control/request-exit", "/tmp/frl-request-exit");
}

static void display_start(void) {
    unsigned maj = 0, min = 0;
    FILE *f = fopen("/sys/class/graphics/fb0/dev", "r");
    if (!f) return;
    int ok = fscanf(f, "%u:%u", &maj, &min);
    fclose(f);
    if (ok == 2 && !mknod("/dev/fb0", S_IFCHR | 0600, makedev(maj, min)))
        display_open();
}

/* Optional BCB operation: shell derives /dev/frl-misc from PARTNAME=misc.
 * Absent explicit initramfs flag means no partition write. */
static int guarded_bcb(void) {
    if (access("/etc/frl/allow-bcb-clear", F_OK)) {
        emit("BCB clear disabled; existing bootloader message preserved\n"); return 0;
    }
    unsigned char old[2048], verify[2048], expected[32] = "boot-recovery", zero[32] = {0};
    int fd = open("/dev/frl-misc", O_RDWR | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) { emit("BCB open failed; no write\n"); return -1; }
    if (pread(fd, old, sizeof old, 0) != (ssize_t)sizeof old) {
        close(fd); emit("BCB read failed; preserved\n"); return -1;
    }
    if (!memcmp(old, zero, sizeof zero)) {
        close(fd); emit("BCB command already clear; no write\n"); return 0;
    }
    if (memcmp(old, expected, sizeof expected)) {
        close(fd); emit("BCB command not exact boot-recovery; preserved\n"); return -1;
    }
    int backup = open("/run/frl/boot/bcb-before.bin",
                      O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (backup < 0) { close(fd); emit("BCB backup unavailable; preserved\n"); return -1; }
    ssize_t n = write(backup, old, sizeof old);
    int synced = fsync(backup);
    close(backup);
    if (n != (ssize_t)sizeof old || synced) {
        close(fd); emit("BCB backup failed; preserved\n"); return -1;
    }
    n = pwrite(fd, zero, sizeof zero, 0);
    synced = fsync(fd);
    ssize_t read_n = pread(fd, verify, sizeof verify, 0);
    close(fd);
    if (n == (ssize_t)sizeof zero && !synced && read_n == (ssize_t)sizeof verify &&
        !memcmp(verify, zero, sizeof zero) && !memcmp(verify + 32, old + 32, sizeof old - 32)) {
        emit("BCB command cleared; other2048-byte fields verified; backup retained in /run\n");
        return 0;
    }
    emit("BCB verification FAILED; retain backup for recovery review\n");
    return -1;
}

static pid_t launch_bootstrap(int output_fd) {
    pid_t child = fork();
    if (child) return child;
    int nullfd = open("/dev/null", O_RDONLY);
    if (nullfd < 0 || dup2(nullfd, STDIN_FILENO) < 0 ||
        dup2(output_fd, STDOUT_FILENO) < 0 || dup2(output_fd, STDERR_FILENO) < 0) _exit(126);
    execl(BB, "busybox", "sh", "/usr/lib/frl/bootstrap-v15.sh", (char *)NULL);
    _exit(127);
}

static void drain(int fd) {
    char buf[2048];
    ssize_t n;
    while ((n = read(fd, buf, sizeof buf - 1)) > 0) { buf[n] = 0; emit(buf); }
}

static void reap(void) {
    int status;
    pid_t p;
    while ((p = waitpid(-1, &status, WNOHANG)) > 0)
        if (p == watchdog_pid) { emit("watchdog exited unexpectedly\n"); recovery(); }
}

struct process_identity { pid_t pid; unsigned long long start; };

static unsigned long long process_start(pid_t pid) {
    char path[64], line[1024];
    snprintf(path, sizeof path, "/proc/%ld/stat", (long)pid);
    FILE *f = fopen(path, "r");
    if (!f) {
        snprintf(path, sizeof path, "/newroot/proc/%ld/stat", (long)pid);
        f = fopen(path, "r");
    }
    if (!f) return 0;
    char *read_ok = fgets(line, sizeof line, f);
    fclose(f);
    if (!read_ok) return 0;
    char *end = strrchr(line, ')');
    if (!end || end[1] != ' ') return 0;
    char *saved = NULL;
    char *token = strtok_r(end + 2, " ", &saved);
    for (int field = 3; token; ++field, token = strtok_r(NULL, " ", &saved)) {
        if (field == 22) return strtoull(token, NULL, 10);
    }
    return 0;
}

static int read_serial_identity(const char *path, struct process_identity *identity) {
        FILE *f = fopen(path, "r");
        char moved[240];
        if (!f) {
            if (errno != ENOENT) return -1;
            snprintf(moved, sizeof moved, "/newroot%s", path); f = fopen(moved, "r");
        }
        long p = 0;
        unsigned long long stamp = 0;
        char extra;
        if (!f) return errno == ENOENT ? 0 : -1;
        int valid = fscanf(f, "%ld %llu %c", &p, &stamp, &extra);
        fclose(f);
        if (valid == 2 && p > 1 && p <= INT32_MAX && stamp) {
            identity->pid = (pid_t)p;
            identity->start = stamp;
            return 1;
        }
        return -1;
}
static int read_serial_session(struct process_identity owner, struct process_identity *session) {
    char path[200];
    snprintf(path, sizeof path, "%s/serial-session.%ld.%llu.pid", FRL_SERIAL_CONTROL, (long)owner.pid, owner.start);
    return read_serial_identity(path, session);
}
static void read_serial(struct process_identity pids[2]) {
    char path[200]; snprintf(path, sizeof path, "%s/serial-controller.pid", FRL_SERIAL_CONTROL);
    read_serial_identity(path, &pids[0]);
    if (pids[0].pid > 1 && pids[0].start) read_serial_session(pids[0], &pids[1]);
}

static double serial_clock(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
    return (double)now.tv_sec + (double)now.tv_nsec / 1000000000.0;
}
static int serial_watchdog_alive(void) {
    return watchdog_pid > 1 && watchdog_start_time &&
           process_start(watchdog_pid) == watchdog_start_time && !kill(watchdog_pid, 0);
}
static int stop_serial(const struct process_identity initial[2]) {
    double now = serial_clock(), deadline = now + 25;
    if (now < 0 || initial[0].pid <= 1 || !initial[0].start || !serial_watchdog_alive()) return -1;
    /* Stop the sole respawner first, with exact identity; never signal its group
     * because it shares PID1's group. Wait through exit-files/driver close. */
    unsigned long long controller_observed = process_start(initial[0].pid);
    if (controller_observed && controller_observed != initial[0].start) return -1;
    if (controller_observed == initial[0].start)
        (void)kill(initial[0].pid, SIGKILL);
    while (process_start(initial[0].pid) == initial[0].start) {
        reap(); now = serial_clock();
        if (!serial_watchdog_alive() || now < 0 || now >= deadline) return -1;
        usleep(20000);
    }
    struct process_identity session = {-1, 0};
    if (read_serial_session(initial[0], &session) < 0) return -1; /* publish BEFORE tty open */
    if (session.pid <= 1 || !session.start) return serial_watchdog_alive() ? 0 : -1;
    unsigned long long observed = process_start(session.pid);
    if (observed && observed != session.start) return -1;
    if (observed && (getsid(session.pid) != session.pid || getpgid(session.pid) != session.pid)) return -1;
    /* Include interactive jobs in this verified old session, which may have
     * their own process groups. No new session exists until this returns0. */
    for (;;) {
        reap(); now = serial_clock();
        if (!serial_watchdog_alive() || now < 0 || now >= deadline) return -1;
        observed = process_start(session.pid);
        if (observed && observed != session.start) return -1;
        DIR *d = opendir("/proc");
        if (!d) d = opendir("/newroot/proc");
        if (!d) return -1;
        int remaining = 0; struct dirent *entry;
        while ((entry = readdir(d))) {
            char *end; long n = strtol(entry->d_name, &end, 10);
            if (!*entry->d_name || *end || n <= 1 || n > INT32_MAX) continue;
            pid_t pid = (pid_t)n;
            unsigned long long stamp = process_start(pid);
            if (!stamp || getsid(pid) != session.pid) continue;
            if (pid == watchdog_pid) { closedir(d); return -1; }
            remaining++;
            if (process_start(pid) == stamp && getsid(pid) == session.pid) (void)kill(pid, SIGKILL);
        }
        closedir(d);
        if (!remaining) return 0;
        usleep(20000);
    }
}

static int launch_root_serial(void) {
    int ready[2];
    if (pipe2(ready, O_CLOEXEC)) return -1;
    pid_t p = fork();
    if (p < 0) { close(ready[0]); close(ready[1]); return -1; }
    if (p) {
        close(ready[1]);
        unsigned long long child_start = process_start(p);
        struct pollfd waitfd = { .fd = ready[0], .events = POLLIN, .revents = 0 };
        char byte = 0;
        double now = serial_clock(), deadline = now + 10;
        int ok = 0;
        while (now >= 0 && now < deadline && serial_watchdog_alive()) {
            int event = poll(&waitfd, 1, 100);
            if (event > 0) { ok = read(ready[0], &byte, 1) == 1 && byte == 'R'; break; }
            if (event < 0 && errno != EINTR) break;
            now = serial_clock();
        }
        close(ready[0]);
        if (!ok) {
            struct process_identity failed[2] = {{p, child_start}, {-1, 0}};
            (void)stop_serial(failed); return -1;
        }
        return 0;
    }
    close(ready[0]);
    if (chroot("/newroot") || chdir("/")) _exit(125);
    if (dup2(ready[1], 3) < 0 || fcntl(3, F_SETFD, 0) || setenv("FRL_SERIAL_READY_FD", "3", 1)) _exit(125);
    if (ready[1] != 3) close(ready[1]);
    execl(BB, "busybox", "sh", "/usr/lib/frl/serial-console.sh", (char *)NULL);
    _exit(127);
}

static int handoff(void) {
    const char *names[] = {"dev", "proc", "sys", "tmp", "run"};
    struct process_identity serial[2] = {{-1, 0}, {-1, 0}};
    double now = serial_clock(), deadline = now + 3;
    do {
        read_serial(serial);
        if (serial[0].pid > 1 && serial[0].start) break;
        if (!serial_watchdog_alive() || now < 0 || now >= deadline) return -1;
        usleep(20000); now = serial_clock();
    } while (1);
    struct stat before, after;
    if (stat("/", &before) || stat("/newroot", &after) || before.st_dev == after.st_dev ||
        access("/newroot/sbin/frl-init", X_OK) || access("/newroot" BB, X_OK) ||
        access("/newroot/usr/lib/frl/serial-session", X_OK) ||
        access("/newroot/usr/lib/frl/serial-session.sh", R_OK) ||
        access("/newroot/usr/lib/frl/serial-console.sh", R_OK)) return -1;
    if (stop_serial(serial)) { emit("old serial cleanup failed; new tty session NOT launched\n"); return -1; }
    emit("old serial controller/session exited; opening new tty only after mount move\n");
    size_t moved = 0;
    for (; moved < 5; ++moved) {
        char from[32], to[64];
        snprintf(from, sizeof from, "/%s", names[moved]);
        snprintf(to, sizeof to, "/newroot/%s", names[moved]);
        if (mount(from, to, NULL, MS_MOVE, NULL)) break;
    }
    if (moved != 5 || launch_root_serial()) {
        emit("mount move failed; restoring moved runtime mounts\n");
        while (moved > 0) {
            char from[64], to[32]; --moved;
            snprintf(from, sizeof from, "/newroot/%s", names[moved]);
            snprintf(to, sizeof to, "/%s", names[moved]);
            if (mount(from, to, NULL, MS_MOVE, NULL)) emit("mount rollback failed; watchdog retained\n");
        }
        return -1;
    }
    /* READY comes from the worker after fd0, non-stealing ctty and foreground
     * checks; the old session was fully closed before this child was created. */
    emit("PID1 executing switch_root; no service-ready/watchdog-disarm implied\n");
    execl(BB, "busybox", "switch_root", "/newroot", "/sbin/frl-init", (char *)NULL);
    return -1;
}

static int establish_private_session(void) {
    pid_t self = getpid();
    if (getsid(0) == self && getpgrp() == self) return 0;
    if (setsid() != self) return -1;
    return getsid(0) == self && getpgrp() == self ? 0 : -1;
}

int main(void) {
    if (getpid() != 1) {
        puts("FRL_V15: non-PID1; no mount, BCB, watchdog, serial or reboot actions"); return 0;
    }
    /* Kernel threads retain process group0. Establish PID1's normal session
     * before any child inherits it, including the independent watchdog. */
    if (establish_private_session()) recovery();
    if (mounts_initial()) recovery();
    logfile = open("/run/frl/log/bootstrap.log", O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
    kernel_log = open("/dev/kmsg", O_WRONLY | O_NONBLOCK | O_CLOEXEC);
    int dir = open("/run/frl/control", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct frl_wd_config config = {0};
    if (dir < 0 || frl_watchdog_start(dir, &config, &watchdog_pid)) recovery();
    close(dir);
    unsigned long long watchdog_start = process_start(watchdog_pid);
    watchdog_start_time = watchdog_start;
    int identity_fd = open("/run/frl/control/watchdog.pid", O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (!watchdog_start || identity_fd < 0) recovery();
    if (dprintf(identity_fd, "%ld %llu\n", (long)watchdog_pid, watchdog_start) < 0) recovery();
    close(identity_fd);
    display_start();
    emit("V15 PID1 watchdog armed240s; USB/serial bootstrap starting\n");
    int pipefd[2];
    if (pipe2(pipefd, O_CLOEXEC)) recovery();
    if (fcntl(pipefd[0], F_SETFL, O_NONBLOCK)) recovery();
    pid_t worker = launch_bootstrap(pipefd[1]);
    close(pipefd[1]);
    if (worker < 0) recovery();
    int status = 0;
    for (;;) {
        drain(pipefd[0]);
        pid_t p = waitpid(-1, &status, WNOHANG);
        if (p == watchdog_pid) recovery();
        if (p == worker) break;
        usleep(100000);
    }
    drain(pipefd[0]);
    int bcb_ok = guarded_bcb();
    if (!bcb_ok && WIFEXITED(status) && WEXITSTATUS(status) == 0) {
        emit("bootstrap root gate passed; preparing PID1 handoff\n");
        if (handoff()) emit("handoff failed; watchdog remains armed; inspect runtime mount state\n");
    } else emit("bootstrap NOT ready: recovery mode, serial if USB initialized; no handoff\n");
    for (;;) { drain(pipefd[0]); reap(); usleep(100000); }
}
