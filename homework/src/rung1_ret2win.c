/* rung1 — classic ret2win.
 *
 * win() is never called from anywhere. vuln() reads attacker input into a
 * fixed-size stack buffer with no length check (read(0, buf, 512) into a
 * 64-byte buffer — no linker warning, identical overflow to gets()).
 * Objective: overwrite vuln()'s saved return address with &win.
 *
 * Build: gcc -static -fno-stack-protector -no-pie -g -O0
 */
#include <stdio.h>
#include <unistd.h>
#include <fcntl.h>

void win(void) {
    char buf[256];
    int fd = open("/flag.txt", O_RDONLY);
    if (fd < 0) {
        write(1, "win: cannot open /flag.txt\n", 27);
        return;
    }
    ssize_t n = read(fd, buf, sizeof(buf));
    close(fd);
    if (n > 0)
        write(1, buf, (size_t)n);
}

void vuln(void) {
    char buf[64];
    /* UNSAFE: up to 512 bytes into a 64-byte stack buffer. */
    read(0, buf, 512);
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("rung1: ret2win (static, no PIE, no canary). feed me stdin.\n");
    vuln();
    printf("done.\n");
    return 0;
}
