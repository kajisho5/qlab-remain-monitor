# Changelog

## 1.3.4
- Sharpen 詳細診断/`--try`: also shows the raw `/connect` reply, and now tests both
  `cueLists` and `cueLists/shallow` separately (previously only the non-shallow one),
  since a real-world case showed `runningOrPausedCues` succeeding while `cueLists`
  alone came back `status=denied` even with a passcode that has full View/Edit/Control
  permission -- narrowing down exactly which query is denied, and whether shallow vs.
  full makes a difference, without another guess-and-check round.

## 1.3.3
- Fix: automatic reconnect (retrying on each network adapter after a "host
  unreachable"/"timed out" error) never triggered on non-English Windows, because it
  matched the English words "timed out"/"unreachable" against `str(exception)` --
  which is OS-locale-translated (e.g. a Japanese Windows reports
  "到達できないホスト..." for WSAEHOSTUNREACH, with no English substring at all).
  Now compares the actual Windows error code (`.winerror`, since CPython leaves
  `.errno` at 0 for these) against a fixed set of retriable codes instead, so it
  works regardless of the OS display language. Verified against a simulated
  OSError shaped exactly like the real Windows exception (`errno=0,
  winerror=10065`).

## 1.3.2
- New "詳細診断" button in the settings window: opens a temporary connection to the
  configured QLab and shows QLab's actual `cueLists` reply (list/cue counts, raw JSON
  on anything unexpected) right in the log box. Same logic `--try` used, factored out
  into `diagnose_workspace()` so both share one implementation. Useful when a cue list
  isn't showing and running a separate CLI diagnostic isn't convenient.

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
