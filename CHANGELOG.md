# Changelog

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
