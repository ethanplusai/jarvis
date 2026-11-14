/* Jarvis.app launcher.
 *
 * macOS grants Screen Recording / Accessibility / Microphone to an APP, judged
 * by the app that started a process. A bare launchd job (bash, python) gets
 * nothing, so JARVIS used to need a Terminal window. This tiny app is that
 * "responsible" app instead: it spawns start-jarvis.sh as a child (never
 * exec -- the app must stay the parent for the grants to reach the children),
 * waits for it, and on quit stops the whole process group.
 */
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>
#include <CoreGraphics/CoreGraphics.h>
#include <ApplicationServices/ApplicationServices.h>

#ifndef JARVIS_DIR
#error "build with -DJARVIS_DIR=\"/path/to/jarvis\""
#endif

extern char **environ;
static pid_t child = 0;

static void stop(int sig) {
    if (child > 0) kill(-child, SIGTERM);   /* the whole group: server, vite, clap listener, brain */
    (void)sig;
}

/* Ask once for what JARVIS needs. Each call only prompts while the permission
 * is missing, and it puts "Jarvis" into that Settings list so the user just
 * flips the switch. */
static void ask_permissions(void) {
    if (!CGPreflightScreenCaptureAccess()) CGRequestScreenCaptureAccess();
    const void *keys[] = {kAXTrustedCheckOptionPrompt};
    const void *vals[] = {kCFBooleanTrue};
    CFDictionaryRef opts = CFDictionaryCreate(NULL, keys, vals, 1,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    AXIsProcessTrustedWithOptions(opts);
    CFRelease(opts);
}

int main(void) {
    ask_permissions();
    signal(SIGTERM, stop);
    signal(SIGINT, stop);
    signal(SIGHUP, stop);
    if (chdir(JARVIS_DIR) != 0) { perror("chdir"); return 1; }

    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETPGROUP);
    posix_spawnattr_setpgroup(&attr, 0);    /* child leads its own group, so stop() reaches all of it */

    char *argv[] = {"/bin/bash", JARVIS_DIR "/start-jarvis.sh", NULL};
    if (posix_spawn(&child, "/bin/bash", NULL, &attr, argv, environ) != 0) {
        perror("posix_spawn");
        return 1;
    }
    int status = 0;
    while (waitpid(child, &status, 0) < 0) { /* interrupted by a signal: keep waiting */ }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
