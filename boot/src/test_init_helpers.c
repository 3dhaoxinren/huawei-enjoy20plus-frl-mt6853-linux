/* Offline helper test only: includes no invocation of mounts/BCB/handoff. */
#define main frl_v15_uninvoked_main
#include "init-v15.c"
#undef main

int main(void) {
    unsigned long long start = process_start(getpid());
    if (!start || process_start(getpid()) != start || process_start(INT32_MAX)) return 1;
    puts("PASS process identity read/recheck; nonexistent PID refused");
    return 0;
}
