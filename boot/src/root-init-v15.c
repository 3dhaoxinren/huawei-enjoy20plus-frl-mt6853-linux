#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/reboot.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t requested;
static volatile sig_atomic_t signal_number, signal_pid, signal_uid, signal_code;
static pid_t timer_pid = -1;
static unsigned long long timer_start;
static int logfd = -1;
static pid_t supervisor = -1;
static const char *last_request_source = "none";
static int last_request_mode;

#ifndef FRL_ROOT_INIT_KMSG_PATH
#define FRL_ROOT_INIT_KMSG_PATH "/dev/kmsg"
#endif

static void note(const char *message) {
    int saved_errno = errno;
    char line[384];
    int n = snprintf(line, sizeof line, "FRL_ROOT_INIT: %s\n", message);
    size_t length = n < 0 ? 0 : (size_t)n < sizeof line ? (size_t)n : sizeof line - 1;
    if (logfd >= 0) { ssize_t written = write(logfd, line, length); (void)written; }
    int kernel_log = open(FRL_ROOT_INIT_KMSG_PATH, O_WRONLY | O_NONBLOCK | O_CLOEXEC);
    if (kernel_log >= 0) { ssize_t written = write(kernel_log, line, length); (void)written; close(kernel_log); }
    errno = saved_errno;
}
struct signal_origin { int mode, number, pid, uid, code; };
static void request_signal_set(sigset_t *set) {
    sigemptyset(set);
    sigaddset(set, SIGTERM); sigaddset(set, SIGINT);
    sigaddset(set, SIGUSR1); sigaddset(set, SIGUSR2);
}
static struct signal_origin snapshot_signal(bool consume) {
    sigset_t block, previous;
    request_signal_set(&block);
    int masked = !sigprocmask(SIG_BLOCK, &block, &previous);
    struct signal_origin origin = {requested, signal_number, signal_pid, signal_uid, signal_code};
    if (consume) requested = 0;
    if (masked) (void)sigprocmask(SIG_SETMASK, &previous, NULL);
    return origin;
}
static unsigned long long proc_number(pid_t pid, int wanted);
static void emergency(const char *reason) __attribute__((noreturn));
static void emergency(const char *reason) {
    int cause_errno = errno;
    struct signal_origin origin = snapshot_signal(false);
    unsigned long long observed = timer_pid > 1 ? proc_number(timer_pid, 22) : 0;
    char message[352];
    snprintf(message, sizeof message,
             "emergency bootloader reason=%s errno=%d timer_pid=%ld expected_start=%llu observed_start=%llu source=%s mode=%d pending=%d sig=%d si_pid=%d si_uid=%u si_code=%d",
             reason, cause_errno, (long)timer_pid, timer_start, observed,
             last_request_source, last_request_mode, origin.mode, origin.number,
             origin.pid, (unsigned)origin.uid, origin.code);
    note(message);
    for (;;) {
        syscall(SYS_reboot, LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2,
                LINUX_REBOOT_CMD_RESTART2, "bootloader");
        sleep(1);
    }
}
static unsigned long long proc_number(pid_t pid, int wanted) {
    char name[64], line[2048];
    snprintf(name, sizeof name, "/proc/%ld/stat", (long)pid);
    FILE *f = fopen(name, "r");
    if (!f) return 0;
    char *ok = fgets(line, sizeof line, f); fclose(f);
    if (!ok) return 0;
    char *end = strrchr(line, ')');
    if (!end || end[1] != ' ') return 0;
    char *save = NULL, *token = strtok_r(end + 2, " ", &save);
    for (int field = 3; token; ++field, token = strtok_r(NULL, " ", &save))
        if (field == wanted) return strtoull(token, NULL, 10);
    return 0;
}
static bool timer_alive(void) {
    return timer_pid > 1 && timer_start && proc_number(timer_pid, 22) == timer_start && !kill(timer_pid, 0);
}
static bool startup_groups_valid(pid_t sid, pid_t pgid, pid_t watchdog_pgid) {
    return sid == 1 && pgid == 1 && watchdog_pgid > 0;
}
static double monotonic(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) emergency("clock_gettime");
    return (double)t.tv_sec + (double)t.tv_nsec / 1000000000.0;
}
static void receive_signal(int sig, siginfo_t *info, void *context) {
    (void)context;
    signal_number = sig;
    signal_pid = info ? (sig_atomic_t)info->si_pid : 0;
    signal_uid = info ? (sig_atomic_t)info->si_uid : 0;
    signal_code = info ? (sig_atomic_t)info->si_code : 0;
    requested = sig == SIGUSR2 ? 3 : sig == SIGUSR1 ? 1 : 2;
}
static pid_t launch(const char *program, const char *arg1, const char *arg2) {
    pid_t p = fork();
    if (!p) {
        (void)setpgid(0, 0);
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL);
        signal(SIGUSR1, SIG_DFL); signal(SIGUSR2, SIG_DFL);
        execl(program, program, arg1, arg2, (char *)NULL);
        _exit(127);
    }
    return p;
}
static int wait_bounded(pid_t child, double seconds) {
    if (child < 1) return -1;
    double deadline = monotonic() + seconds;
    for (;;) {
        if (!timer_alive()) emergency("wait_bounded_timer_dead");
        int status; pid_t p;
        while ((p = waitpid(-1, &status, WNOHANG)) > 0) {
            if (p == timer_pid) emergency("wait_bounded_timer_reaped");
            if (p == supervisor) supervisor = -1;
            if (p == child) return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
        }
        if (monotonic() >= deadline) {
            (void)kill(-child, SIGKILL); (void)kill(child, SIGKILL);
            note("bounded helper timed out"); return -1;
        }
        usleep(100000);
    }
}
static void kill_users(int sig) {
    DIR *d = opendir("/proc");
    if (!d) emergency("kill_users_proc_open");
    struct dirent *entry;
    while ((entry = readdir(d))) {
        char *end; long n = strtol(entry->d_name, &end, 10);
        if (!*entry->d_name || *end || n <= 1 || n > 0x7fffffff) continue;
        pid_t p = (pid_t)n;
        unsigned long long start = proc_number(p, 22);
        if (!start) continue;
        if (p == timer_pid && start == timer_start) continue;
        if (proc_number(p, 9) & 0x00200000ULL) continue; /* PF_KTHREAD */
        if (proc_number(p, 22) == start) (void)kill(p, sig);
    }
    closedir(d);
}
static int parse_request(const char *text, size_t length) {
    const char *modes[] = {"bootloader\n", "recovery\n", "poweroff\n", "restart\n"};
    for (size_t i = 0; i < sizeof modes / sizeof modes[0]; ++i)
        if (length == strlen(modes[i]) && !memcmp(text, modes[i], length)) return (int)i + 1;
    return 0;
}
static const char *shutdown_target(int mode) {
    switch (mode) {
        case 1: return "bootloader";
        case 2: return "recovery";
        case 3: return "poweroff";
        case 4: return "restart";
        default: return NULL;
    }
}
static int request_file(void) {
    const char *path = "/run/frl/control/shutdown-request";
    int fd = open(path, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return 0;
    struct stat s; char text[32] = {0};
    if (fstat(fd, &s) || !S_ISREG(s.st_mode) || s.st_uid || s.st_nlink != 1 || s.st_size >= (off_t)sizeof text) {
        close(fd); note("invalid shutdown request file"); return 0;
    }
    ssize_t n = read(fd, text, sizeof text - 1); close(fd);
    if (n <= 0 || n != s.st_size) return 0;
    (void)unlink(path);
    int mode = parse_request(text, (size_t)n);
    if (mode) return mode;
    note("unknown shutdown mode refused"); return 0;
}
static bool serial_alive(void) {
    FILE *f = fopen("/run/frl/control/serial-controller.pid", "r");
    long p = 0;
    if (!f) return false;
    int ok = fscanf(f, "%ld", &p); fclose(f);
    if (ok != 1 || p <= 1 || p > 0x7fffffff) return false;
    char path[64], command[512];
    snprintf(path, sizeof path, "/proc/%ld/cmdline", p);
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return false;
    ssize_t n = read(fd, command, sizeof command); close(fd);
    const char expected[] = "/usr/lib/frl/serial-console.sh";
    return n > 0 && memmem(command, (size_t)n, expected, sizeof expected) != NULL;
}
static void shutdown_native(int mode) {
    const char *target = shutdown_target(mode);
    if (!target) { note("invalid shutdown mode refused"); return; }
    note("preparing RAM shutdown entry; original watchdog retained");
    int fd = open("/run/frl/control/shutdown-in-progress", O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd >= 0) { (void)dprintf(fd, "%s\n", target); close(fd); }
    pid_t prepare = launch("/usr/bin/python3", "/usr/lib/frl/shutdown/prepare.py", target);
    if (wait_bounded(prepare, 45) != 0) {
        note("RAM shutdown preparation failed; services retained for repair");
        (void)unlink("/run/frl/control/shutdown-in-progress"); return;
    }
    note("stopping foreground services");
    (void)wait_bounded(launch("/usr/lib/frl/root-shutdown.sh", NULL, NULL), 60);
    kill_users(SIGTERM);
    usleep(500000);
    for (int round = 0; round < 3; ++round) { kill_users(SIGKILL); usleep(100000); }
    if (!timer_alive()) emergency("pre_ram_exec_timer_dead");
    note("executing RAM shutdown PID1; no global kill(-1) or uncovered sync");
    execl("/run/frl/shutdown-root/bin/frl-shutdown-entry", "frl-shutdown-entry", (char *)NULL);
    note("RAM exec failed; returning bootloader under watchdog");
    emergency("ram_exec_failed");
}
int main(void) {
    if (getpid() != 1 || geteuid() != 0) {
        puts("FRL_ROOT_INIT: non-PID1; no services, signals, mounts, or reboot actions"); return 0;
    }
    (void)prctl(PR_SET_NAME, "frl-init", 0, 0, 0);
    (void)setenv("PATH", "/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/frl/bin", 1);
    logfd = open("/run/frl/log/root-init.log", O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
    if (logfd >= 0) { (void)dup2(logfd, STDOUT_FILENO); (void)dup2(logfd, STDERR_FILENO); }
    FILE *f = fopen("/run/frl/control/watchdog.pid", "r");
    long p = 0;
    if (!f || fscanf(f, "%ld %llu", &p, &timer_start) != 2 || p <= 1 || p > 0x7fffffff) emergency("startup_timer_record");
    fclose(f); timer_pid = (pid_t)p;
    if (!timer_alive()) emergency("startup_timer_dead");
    pid_t sid = getsid(0), pgid = getpgrp(), watchdog_pgid = getpgid(timer_pid);
    char groups[192];
    snprintf(groups, sizeof groups, "startup groups sid=%ld pgid=%ld timer_pid=%ld timer_pgid=%ld",
             (long)sid, (long)pgid, (long)timer_pid, (long)watchdog_pgid);
    note(groups);
    if (!startup_groups_valid(sid, pgid, watchdog_pgid)) emergency("startup_process_group");
    struct sigaction sa = {.sa_sigaction = receive_signal, .sa_flags = SA_SIGINFO};
    request_signal_set(&sa.sa_mask);
    (void)sigaction(SIGTERM, &sa, NULL); (void)sigaction(SIGINT, &sa, NULL);
    (void)sigaction(SIGUSR1, &sa, NULL); (void)sigaction(SIGUSR2, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);
    note("real root PID1 reached; reaping and original recovery watchdog active");
    int boot_status = wait_bounded(launch("/usr/lib/frl/root-boot.sh", NULL, NULL), 90);
    note(boot_status == 0 ? "root initialization returned success" : "root initialization failed; partial services and serial remain diagnosable");
    double next_start = 0, next_serial = 0;
    for (;;) {
        if (!timer_alive()) emergency("main_timer_dead");
        int status; pid_t exited;
        while ((exited = waitpid(-1, &status, WNOHANG)) > 0) {
            if (exited == timer_pid) emergency("main_timer_reaped");
            if (exited == supervisor) { supervisor = -1; next_start = monotonic() + 2; note("supervisor exited; scheduled restart"); }
        }
        struct signal_origin origin = snapshot_signal(true);
        int mode = origin.mode ? origin.mode : request_file();
        if (mode) {
            last_request_source = origin.mode ? "signal" : "file";
            last_request_mode = mode;
            char message[256];
            if (origin.mode)
                snprintf(message, sizeof message, "shutdown request source=signal mode=%s sig=%d si_pid=%d si_uid=%u si_code=%d",
                         shutdown_target(mode), origin.number, origin.pid, (unsigned)origin.uid, origin.code);
            else snprintf(message, sizeof message, "shutdown request source=file mode=%s path=/run/frl/control/shutdown-request",
                          shutdown_target(mode));
            note(message);
        }
        if (mode) { shutdown_native(mode); next_start = monotonic() + 2; }
        if (supervisor < 1 && monotonic() >= next_start) supervisor = launch("/usr/lib/frl/supervise.sh", NULL, NULL);
        if (monotonic() >= next_serial && !serial_alive()) {
            (void)launch("/usr/lib/frl/serial-console.sh", NULL, NULL);
            next_serial = monotonic() + 2;
        }
        usleep(200000);
    }
}
