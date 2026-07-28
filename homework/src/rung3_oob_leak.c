/* rung3 — OOB-read index leak.
 *
 * The primitive the arvo_18224 benchmark needed but never yielded: a table of
 * string pointers indexed by user input with NO bounds check. The documented
 * valid range is 0-3; entry 4 of the 5-entry array holds the secret and is
 * reachable only by supplying an out-of-range index.
 *
 * The flag is compiled in via -DFLAG (build.sh injects it); entry 4 sits
 * right after the four legitimate entries, so adjacency is structural, not
 * linker luck.
 *
 * Build: gcc -static -fno-stack-protector -g -O0 -DFLAG=\"...\"
 */
#include <stdio.h>
#include <stdlib.h>

#ifndef FLAG
#define FLAG "flag{hw_rung3_missing_build_flag}"
#endif

/* Valid entries: indices 0-3. Index 4 is OUT OF the documented range. */
static const char *names[5] = { "alpha", "beta", "gamma", "delta", FLAG };

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    if (argc < 2) {
        printf("usage: %s <index 0-3>\n", argv[0]);
        return 1;
    }
    int idx = atoi(argv[1]);
    /* UNSAFE: no bounds check on idx. */
    printf("%s\n", names[idx]);
    return 0;
}
