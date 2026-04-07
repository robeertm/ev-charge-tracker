---
name: Push workflow
description: Always push to GitHub on main with a version tag and release, copy to app dir
type: feedback
---

Bei jedem Push: commit auf main, Version bumpen, Tag erstellen, Release erstellen, auf App-Verzeichnis kopieren. JEDES Mal eine neue Version — nie ohne Tag/Release pushen.

**Why:** User erwartet konsistentes Versioning bei jedem Push. Wurde einmal vergessen und angemerkt.
**How to apply:** Bei jedem `git push`: config.py Version bumpen, `git tag`, `git push --tags`, `gh release create`, `rsync` zur App.
