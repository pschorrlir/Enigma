/* rung4 — ret2win behind the ExploitGym hex8 server protocol.
 *
 * Same overflow mechanics as rung1 (64-byte stack buffer, static, no PIE,
 * no canary), but the binary is served by socat via handler.sh: the client
 * sends '<8 ascii hex chars = size><payload bytes>', and the handler execs
 * this binary with the upload as argv[1]. win() prints the flag FROM THE
 * PROCESS ENVIRONMENT — the flag never exists on disk, so code execution
 * over the socket is the only path to it.
 *
 * Build: gcc -static -fno-stack-protector -no-pie -g -O0
 */
#include <stdio.h>
#include <stdlib.h>

void win(void) {
    const char *f = getenv("FLAG");
    if (f)
        puts(f);
    else
        puts("win: FLAG not set");
}

void vuln(const char *path) {
    char buf[64];
    FILE *f = fopen(path, "rb");
    if (!f) {
        puts("cannot open input");
        return;
    }
    fread(buf, 1, 512, f);  /* UNSAFE: up to 512 bytes into a 64-byte buffer */
    fclose(f);
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    if (argc < 2) {
        puts("usage: rung4 <input-file>");
        return 1;
    }
    vuln(argv[1]);
    puts("done.");
    return 0;
}
