"""Entry point for clicked magnet: links, registered by --register-magnet-handler.

Browsers launch the handler from an arbitrary working directory, where
`python -m torrentfiler` can't find the package. Running a script puts the
script's own folder on sys.path, so this works from anywhere. The .pyw
extension and pythonw.exe keep a console window from flashing up.
"""

from torrentfiler.__main__ import main

raise SystemExit(main())
