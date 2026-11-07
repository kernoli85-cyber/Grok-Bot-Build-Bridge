# Repository snapshot transfer: architecture and security spec (draft v1)

Status: **design + in-memory protocol codec only** (`snapshot_frames.py`, v0.21.0).
There is no source reader, no receiver writer, no CLI and no wsl.exe streaming yet. No real repo
has been read or copied. Nothing here reaches a model.

## 1. Goal and non-goals
- Goal: give a later, separately approved repo `ask` a **verified, private, disposable copy**
  of owner-selected files inside the WSL distro where Grok runs, so that Grok never sees the
  live repo.
- Non-goals now: editing source, applying diffs, running git, hooks, submodules, package
  managers or build tasks, and sending source content to logs, outbox, MCP or a model.

## 2. Threat model (what can go wrong)
- The repo is **untrusted input**: symlinks/junctions/reparse points to secrets, hard links
  aliasing files outside the root, FIFOs and devices that block or leak, path names built for
  traversal (`..`, drive, UNC, ADS, reserved names, Unicode/case tricks), huge files and bombs,
  instruction files (AGENTS.md, rules) trying to steer the model.
- A **concurrent local process** can swap files or directories between check and use (TOCTOU).
  A metadata preflight (`repo-plan`, v0.20.0) therefore never secures the copy by itself.
- The **receiver** may get a corrupted, truncated or hostile stream.
- Secrets in source code: detection is heuristic only. **No tool can promise a selection
  contains no secrets.** The owner must confirm the file list (with `--reveal-paths`) and
  separately approve disclosure to the model.

## 3. Supported routes (fail closed for everything else)
| Route | v1 status | Reason |
|---|---|---|
| WSL ext4 repo → WSL temp dir (same distro) | **Planned v1** | POSIX `dir_fd` + `O_NOFOLLOW` walk is available in the Python stdlib on Linux |
| Windows NTFS repo → WSL | **Unsupported, fail closed** | see §4.2 |
| Windows repo read from WSL through `/mnt/c` (drvfs) | **Unsupported** | reparse and hard-link semantics on drvfs are not verified; metadata may not match NTFS |
| Any → network / other machines | Out of scope | — |

## 4. Source reader (secure opener)
### 4.1 POSIX (inside WSL), planned
- Open the root with `os.open(root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)` after
  verifying that no ancestor is a symlink (as `repo-plan` does), then `fstat`: it must be a directory.
- Reach every file **one component at a time** with `os.open(name, flags, dir_fd=parent_fd)`.
  Directories use `O_DIRECTORY | O_NOFOLLOW`; files use `O_RDONLY | O_NOFOLLOW | O_NONBLOCK`,
  so a FIFO swapped in cannot block. `O_NOFOLLOW` only guards the final component, which is why
  the walk is per component and never resolves a multi-part path.
- Linux `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_XDEV)` would be stronger,
  but Python has no stdlib binding. It is an optional future ctypes path, not assumed.
- After open, `fstat(fd)` must show `S_ISREG`, `st_nlink == 1`, the same `st_dev` as the root,
  and size ≤ limit. Read at most `size + 1` bytes: a read beyond the fstat size, or fewer bytes
  than it, aborts the file as changed. `fstat` again after reading; size, mtime_ns and inode must match.
- Names come from `os.scandir(dir_fd)` / `os.listdir(fd)`, are NFC-checked, and are revalidated
  with the same lexical rules as `repo-plan` (secret names, excluded dirs, reserved names).
- Remaining honest gap: a file can still be **modified in place** between the two fstats
  without changing the metadata we compare. The copy is consistent per the checks, not a
  filesystem-level atomic snapshot.

### 4.2 Windows NTFS, unsupported for now (no faked security)
A safe Windows walk needs handle-relative opens (`NtCreateFile` with `RootDirectory`, or
`CreateFileW` with `FILE_FLAG_OPEN_REPARSE_POINT` per component plus `GetFileInformationByHandle`
for the link count and reparse attribute, and `GetFinalPathNameByHandleW` to verify the final path).
The Python stdlib exposes none of this. `os.lstat` followed by `open` is a race. Until a
reviewed ctypes or native implementation exists, with tests on real NTFS junctions, symlinks
and hard links, a Windows-source request is refused with `windows_source_unsupported`.
Workaround for owners: keep the repo inside the WSL filesystem.

## 5. Transport
- v1 (WSL → WSL): reader and receiver both run **inside the same distro as the non-root
  owner user**. They are started by the Windows companion through the existing fixed route:
  `wsl.exe -d <distro> -u <user> --cd /tmp --exec <fixed absolute path to the companion
  entrypoint inside WSL> snapshot-receive ...`. There is no shell, no `bash -c`, no `python -c`
  code strings, and no caller-provided interpreter. The stream is a local pipe between two
  processes on one machine. It is **not internet traffic and not encrypted**; it never touches
  Grok Bot account transport or the webhook.
- Framing: `gbbb.snapshot/v1` (implemented, in memory). Magic; length-prefixed JSON manifest
  (schema, exact `{path,size}` entries); per file a u64 length equal to the manifest size, the
  bytes and a SHA-256 trailer; then END with the file count and a SHA-256 over the whole stream;
  nothing after it.
- Explicitly **no archive formats** (tar/zip): no extraction step, so no tar-slip, no link
  entries, no permission bits and no compression bombs. A stream starting with a ZIP or TAR
  signature fails `bad_magic`.
- Bounds enforced before allocation: manifest ≤ 256 KiB, ≤ 200 files, ≤ 256 KiB per file,
  ≤ 2 MiB total, path ≤ 1024 bytes, reads in chunks of at most 64 KiB.
- Credentials: none in the stream, argv or env. The receiver gets no secrets.

## 6. Receiver (filesystem writer), planned, not implemented
- Destination: a fresh `mktemp -d /tmp/gbbb-repo-XXXXXXXXXXXX` (mode 0700, owned by the owner
  user, **never root**), verified like the `ask` cwd (real dir, empty, owner, not group- or
  world-writable).
- It validates the manifest again independently (same codec: canonical NFC paths, no traversal,
  no ADS/drive/UNC, no case duplicates, no file-vs-directory prefix conflicts, secret/excluded
  names refused).
- It creates directories with `mkdir(name, 0o700, dir_fd=parent)` and files with
  `open(name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600, dir_fd=...)`. It never follows
  or creates links, devices or FIFOs.
- All-or-nothing: data is written into a staging subdir and marked complete (a `COMPLETE`
  marker) only after END verifies. On any error the receiver deletes **only its own staging
  dir, by its own dir fd**. It never deletes anything outside the random folder and never
  touches the source repo.
- TTL: snapshot folders carry a creation timestamp. A cleanup command (future) removes only
  `/tmp/gbbb-repo-*` dirs owned by the user, older than the TTL (default 24 h), after
  re-verifying ownership. No recursive delete follows links.

## 7. Model-facing boundary (later, separately approved)
- No source content goes to logs, outbox, MCP or a model until the owner has (1) seen the file
  list and (2) approved this specific disclosure.
- Repo instruction files are untrusted data and are listed as such. `.git`, rule and hook
  dirs and credentials are excluded by default.
- **Tool isolation is not proven.** The 10:00 smoke run's exit 0, or even a future `BRIDGE_OK`,
  does not prove that Grok's deny rules work. Proving enforcement needs a dedicated, approved
  negative test: plant a harmless canary file outside the snapshot, instruct the model to read
  or modify it, and verify that the attempt is denied and the canary is unchanged. That test is
  not requested yet.

## 8. Apply boundary (design only)
The bridge never edits source. A later proposed-diff mode will return a bounded unified diff
computed between the snapshot and its post-run copy, without running git or hooks. Applying it
to the real repo will be a separate owner-run step with path re-validation. No auto-apply.

## 9. Blockers
1. **Companion inside WSL:** the v1 route needs the companion package installed inside the
   WSL distro (today it is only in the Windows venv). That is a host install and needs owner approval.
2. **Real-output validation:** one approved `ask` to confirm the 1.0.70 JSON shape; later the
   canary enforcement test (§7).
3. **Windows-source support** needs a reviewed handle-based implementation (§4.2).
4. Distribution of the per-user app (packaging/signing) is unsolved, but it does not block the
   POSIX reader/receiver work, which can be built and tested on the box with fixture trees.
