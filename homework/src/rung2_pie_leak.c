/* rung2 — ret2win under PIE.
 *
 * Same overflow primitive as rung1, but the binary is position-independent:
 * absolute addresses are randomized per run. The banner leaks a runtime code
 * address (main); the offset from main to win is constant and recoverable
 * from the binary (nm / objdump), so: leak -> compute base -> compute win ->
 * deliver the same ret2win payload.
 *
 * Build: gcc -fno-stack-protector -pie -fPIE -g -O0
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
    printf("rung2: ret2win under PIE. main: %p\n", (void *)main);
    vuln();
    printf("done.\n");
    return 0;
}
