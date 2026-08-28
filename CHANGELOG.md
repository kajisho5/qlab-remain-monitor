# Changelog

## 1.3.1
- Suppress the harmless `ConnectionAbortedError`/`ConnectionResetError` traceback that
  Python's HTTP server prints when the browser closes a keep-alive connection mid-poll
  (common on Windows, `WinError 10053`). Genuinely unexpected server errors still print
  as before -- only the known disconnect exceptions are silenced.

## 1.3.0
- Fix: cues nested inside a Group cue (a common way to structure a whole show) never
  showed up in the monitor. Structure scan now queries `cueLists` instead of
  `cueLists/shallow` and recursively flattens Group children (nested Groups included)
  into the cue list display, in order. Unit-tested against a synthetic nested-Group
  structure.

## 1.2.1
- `--try` now also queries `cueLists/shallow` and shows QLab's raw reply when the cue
  list comes back empty, instead of silently showing nothing. Distinguishes a real
  QLab-side `status=denied`/`error` from a cue list that only contains cues nested
  inside a Group (which `cueLists/shallow` never includes, per QLab's own docs) --
  useful for diagnosing "connected but no cues visible".

## 1.2.0
- Bonjour discovery (`_qlab._tcp.local.`): find QLab workspaces on the LAN by name, the
  same mechanism the official QLab Remote app uses. New "Bonjourで検索" button in the
  settings window and `--discover` CLI flag. Implemented as a minimal stdlib-only mDNS
  client (no new dependency).

## 1.1.0
- Now Playing cards show the media file name, cue color, and a progress bar

## 1.0.0
- Initial release
