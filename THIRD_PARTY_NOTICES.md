# Third-party notices

The MIT license at the repository root applies to the original Codex Monitor code only. Third-party software keeps its original license. Components are unmodified.

| Component | Version in the Windows v1.2.0 package | License |
| --- | --- | --- |
| CPython | 3.12.10 | PSF License and included third-party notices |
| Paramiko | 5.1.0 | LGPL-2.1-or-later |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| bcrypt | 5.0.0 | Apache-2.0 |
| PyNaCl | 1.6.2 | Apache-2.0; libsodium ISC |
| cffi | 2.1.1 | MIT-0 |
| pycparser | 3.12 | BSD-3-Clause |
| Invoke | 3.0.3 | BSD-2-Clause |
| Tcl / Tk | 8.6, as distributed with CPython | Tcl/Tk license terms |
| OpenSSL | as distributed with CPython / cryptography | Apache-2.0; see included notices |
| PyInstaller bootloader | 6.22.3 | GPL-2.0-or-later with bootloader exception |

Full texts are in `third_party/licenses/`. CPython's additional notices cover bundled runtime libraries. The build tools themselves are installed separately; their versions are pinned in `requirements-build.txt`. Windows system runtime DLLs retain Microsoft's applicable terms; no Microsoft ownership is claimed.

Paramiko's exact upstream source distribution accompanies this release at `third_party/sources/paramiko-5.1.0.tar.gz`. Its upstream URL and verified SHA-256 are recorded in `PROVENANCE.json` in the same directory. To inspect or replace the component, extract that archive, modify the source if desired, install it in a Python 3.12 environment, and run or rebuild this application using the documented build script. The original application's MIT terms do not restrict modification of the LGPL component or debugging those modifications.

Upstream sources: [Python](https://www.python.org/), [Paramiko](https://www.paramiko.org/), [cryptography](https://cryptography.io/), [bcrypt](https://pypi.org/project/bcrypt/), [PyNaCl](https://pynacl.readthedocs.io/), [cffi](https://cffi.readthedocs.io/), [pycparser](https://github.com/eliben/pycparser), [Invoke](https://www.pyinvoke.org/), [PyInstaller](https://pyinstaller.org/), [Tcl/Tk](https://www.tcl.tk/), [OpenSSL](https://www.openssl.org/).

The Windows archive also includes the matching Codex Monitor source ZIP. OpenAI, Codex, Tailscale and Windows names identify the compatible products; this project is independently made and does not claim endorsement.
