#!/usr/bin/env python3
"""Safe dummy worker binary for runner tests — no network, no grok."""
import sys
import time

def main(argv: list[str]) -> int:
    if "--sleep" in argv:
        i = argv.index("--sleep")
        time.sleep(float(argv[i + 1]))
    if "--fail" in argv:
        print("dummy-fail", file=sys.stderr)
        return 3
    if "--huge" in argv:
        sys.stdout.write("X" * 200000)
        return 0
    # echo stdin length only (privacy: don't print prompt)
    data = sys.stdin.read()
    print(f"ok stdin_bytes={len(data.encode('utf-8'))}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
