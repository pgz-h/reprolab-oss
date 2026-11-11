"""Wait for job assignment before starting a trusted Windows command."""
import subprocess
import sys


def main():
    if sys.stdin.buffer.read(1) != b'\x01' or len(sys.argv) < 2:
        return 2
    return subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL)


if __name__ == '__main__':
    raise SystemExit(main())
