"""Normalize a PowerShell script for Windows PowerShell 5.1:
   - exactly ONE UTF-8 BOM (0 or 2+ BOMs both break the parser / garble Chinese)
   - CRLF line endings
Usage: python -I _ps1_bom.py <file.ps1> [<file2.ps1> ...]
"""
import sys

for p in sys.argv[1:]:
    b = open(p, 'rb').read()
    txt = b.decode('utf-8-sig')            # strips one BOM (if any)
    while txt.startswith('﻿'):        # strip any remaining BOMs
        txt = txt[1:]
    out = txt.replace('\r\n', '\n').replace('\n', '\r\n')
    open(p, 'wb').write(b'\xef\xbb\xbf' + out.encode('utf-8'))
    print(f'normalized: {p}  ({len(out)} chars)')
