#!/usr/bin/env python3
import sys
import zipfile
from pathlib import Path

BAD_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.wav', '.mp3', '.ogg', '.m4a', '.sb3'}
BAD_NAMES = {'__pycache__'}


def main() -> int:
    if len(sys.argv) != 2:
        print('usage: python3 tools/check_clean_package.py <zip>')
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(f'not found: {path}')
        return 2
    offenders = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            p = Path(name)
            parts = set(p.parts)
            if parts & BAD_NAMES:
                offenders.append(name)
                continue
            if p.suffix.lower() in BAD_EXTS:
                offenders.append(name)
    if offenders:
        print('Found non-clean package files:')
        for item in offenders:
            print(' -', item)
        return 1
    print('OK: no unnecessary image/music/generated Scratch assets inside package zip')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
