# Bundled components

- Python 3.14.0 Windows x64 embeddable distribution: https://www.python.org/ftp/python/3.14.0/python-3.14.0-embed-amd64.zip
  Archive SHA-256: 8d4d3590c10449d78aa4375f534e6d5f3027d67fdc362dd1a882279db6f90fdf
  Authenticode verification of python.exe on build host: Valid; Python Software Foundation.
  License: licenses/PYTHON-LICENSE.txt (also runtime/LICENSE.txt).
- Microsoft.NETCore.App 8.0.30 win-x64 self-contained runtime, resolved by .NET SDK 8.0.424 publish.
  Licenses: licenses/DOTNET-LICENSE.TXT and licenses/DOTNET-THIRD-PARTY-NOTICES.TXT.
- fuxiang-inject: built from the user's local tools/fuxiang/injector source. No separate project distribution license was present in that source checkout; this private project package does not assert an MIT/Apache grant for that component. Source is supplied in the Source archive.
- Friendswine controller, adapters, portable launcher and installer: project source supplied in the Source archive.

No game executable, engine DLL, game resources, account state, reference mod JAR, Endfield project or reference video/music is included. The verified engine export metadata is included only as a compatibility table. The game must already be installed separately.
