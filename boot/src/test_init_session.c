/* Host-only. No init main, mount, watchdog start, reboot or signals. */
#define main frl_v15_uninvoked_main
#include "init-v15.c"
#undef main
#include <assert.h>

int main(void) {
    pid_t parent_sid = getsid(0), parent_pg = getpgrp();
    int channel[2]; assert(!pipe(channel));
    pid_t leader = fork(); assert(leader >= 0);
    if (!leader) {
        close(channel[0]);
        if (establish_private_session() || establish_private_session()) _exit(2);
        pid_t owner = getpid();
        pid_t child = fork(); if (child < 0) _exit(3);
        if (!child) {
            pid_t proof[3] = {owner, getsid(0), getpgrp()};
            ssize_t n = write(channel[1], proof, sizeof proof);
            _exit(n == (ssize_t)sizeof proof ? 0 : 4);
        }
        int status;
        if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status)) _exit(5);
        close(channel[1]); _exit(0);
    }
    close(channel[1]);
    pid_t proof[3] = {0}; ssize_t n = read(channel[0], proof, sizeof proof); close(channel[0]);
    int status; assert(waitpid(leader, &status, 0) == leader);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    assert(n == (ssize_t)sizeof proof && proof[0] == leader && proof[1] == leader && proof[2] == leader);
    assert(getsid(0) == parent_sid && getpgrp() == parent_pg);
    /* Existing group leader in somebody else's session must fail, not guess. */
    pid_t reject = fork(); assert(reject >= 0);
    if (!reject) {
        if (setpgid(0, 0)) _exit(6);
        _exit(establish_private_session() == -1 ? 0 : 7);
    }
    assert(waitpid(reject, &status, 0) == reject && WIFEXITED(status) && WEXITSTATUS(status) == 0);
    puts("SESSION_CREATED_IDEMPOTENT_CHILD_INHERITS_PARENT_UNCHANGED_FAILURE_REFUSED_PASS");
    return 0;
}
