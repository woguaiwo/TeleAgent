from __future__ import annotations

import sys


def ask(prompt: str) -> str:
    print(prompt, end="", flush=True)
    return sys.stdin.readline().strip()


def main() -> int:
    first = ask("Continue? [y/N] ")
    print(f"first={first}")
    second = ask("Enter choice: ")
    print(f"second={second}")
    return 0 if first == "y" and second == "1" else 1


if __name__ == "__main__":
    raise SystemExit(main())
