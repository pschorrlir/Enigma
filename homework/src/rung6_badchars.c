/* rung6 — constrained ret2win: bad-byte discipline on a string parser.
 *
 * Modeled on the arvo_63746 ground truth (2026-07-30): the overflow rides an
 * sscanf %s conversion, so the payload is a C STRING — 0x00 truncates it
 * (the line is fgets/strlen-parsed), whitespace ends the %s conversion, and
 * the "protocol" reserves ',' and '@' (rejected by the gate before the
 * vulnerable call). Anything after the first NUL simply does not exist for
 * the parser — but a SINGLE address still survives: win is linked below
 * 0x1000000 (text base 0x20000), so its 3 significant bytes are written,
 * the input's own NUL stops the conversion right after them, and %s's
 * terminating NUL supplies the 4th byte. The return slot's high bytes were
 * already 0x00 (vuln's original return is into main, also in low text).
 *
 * Trains: offset discovery through a format gate with a safe alphabet,
 * bad-byte awareness (0x00/whitespace/,/@), and understanding WHY a chain
 * longer than one address cannot cross a string parser.
 *
 * Build: gcc -static -fno-stack-protector -no-pie -g -O0 \
 *          -Wl,-Ttext-segment=0x20000
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void win(void) {
    char buf[256];
    FILE *wf = fopen("/flag.txt", "rb");
    if (!wf) {
        puts("win: cannot open /flag.txt");
        return;
    }
    size_t n = fread(buf, 1, sizeof(buf), wf);
    fclose(wf);
    if (n)
        fwrite(buf, 1, n, stdout);
}

static char line[512];   /* file-scope: keep vuln's frame minimal (rung4 lesson) */

void vuln(const char *path) {
    char tail[32];
    FILE *f = fopen(path, "rb");
    if (!f) {
        puts("cannot open input");
        return;
    }
    if (!fgets(line, sizeof line, f)) {
        puts("empty");
        fclose(f);
        return;
    }
    fclose(f);
    if (strncmp(line, "PWN6", 4) != 0) {
        puts("bad magic");   /* parser rejection: NO crash, NO signal */
        return;
    }
    if (strchr(line, ',') || strchr(line, '@')) {
        puts("bad char");    /* protocol separators are reserved */
        return;
    }
    /* UNSAFE: unbounded %s into a 32-byte stack buffer. The conversion
     * stops at whitespace and, crucially, at the input's first NUL —
     * then writes its OWN terminating NUL right after what it copied. */
    sscanf(line, "PWN6%s", tail);
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    if (argc < 2) {
        puts("usage: rung6 <input-file>");
        return 1;
    }
    vuln(argv[1]);
    puts("done.");
    return 0;
}
