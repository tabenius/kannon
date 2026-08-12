# Kannon

Kannon is a Python terminal CLI for scanning documents, generating first-page
thumbnails, caching the generated data in `kannon.yaml`, and browsing the results
one document at a time in a keyboard-driven terminal UI.

It currently supports:

- PDF first-page thumbnails rendered with PyMuPDF.
- Markdown thumbnails rendered with a lightweight Pillow-based renderer.
- `kannon.yaml` cache output with metadata plus base64-encoded PNG thumbnails.
- Sixel image output when the terminal appears to support it.
- ANSI true-color block output as a fallback or explicit mode.
- Arrow-key navigation by default.

## Repository Layout

```text
.
├── kannon                  # executable repo-root wrapper
├── pyproject.toml          # Python project metadata and dependencies
├── README.md
└── src/python/kannon/      # application package
```

The wrapper keeps the source tree local-friendly:

```sh
./kannon --help
```

It sets `PYTHONPATH=src/python` and runs `python3 -m kannon`.

## Dependencies

Runtime dependencies are listed in `pyproject.toml`:

- `PyMuPDF>=1.24` for PDF metadata and first-page rendering.
- `Pillow>=10.0` for image resizing, Markdown rendering, PNG encoding, sixel
  palette preparation, and ANSI block rendering.
- `PyYAML>=6.0` for reading and writing `kannon.yaml`.

The same Python dependencies are repeated in `requirements.txt` for users who
prefer direct `pip install -r requirements.txt` workflows.

System expectations:

- Python 3.10 or newer.
- Optional `poppler-utils` for `pdftoppm` and `pdfinfo`. Kannon uses this as a
  PDF fallback when PyMuPDF is not installed in the active Python environment.
  This is recorded in `system-dependencies.txt`.
- A terminal with sixel support for image-mode output, or any ANSI-compatible
  terminal for the fallback renderer.
- Fonts are optional. Kannon tries common DejaVu font paths for Markdown previews
  and falls back to Pillow's built-in font.

## Install

From the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

You can then run either:

```sh
kannon
```

or:

```sh
./kannon
```

## Basic Usage

Scan the current directory recursively and open the TUI:

```sh
./kannon
```

Scan explicit files or directories:

```sh
./kannon reports/ contract.pdf notes.md
```

Regenerate all cached thumbnails and metadata:

```sh
./kannon --refresh
```

Write or update `kannon.yaml` without opening a preview:

```sh
./kannon --scan-only
```

Render only the first record once:

```sh
./kannon --no-tui
```

Force ANSI fallback output:

```sh
./kannon --ansi
```

Force sixel output:

```sh
./kannon --sixel
```

Use a different thumbnail max edge:

```sh
./kannon --size 768
```

Use a different cache file:

```sh
./kannon --cache .kannon-documents.yaml
```

## TUI Controls

Kannon opens the TUI by default when both standard input and standard output are
TTYs.

- `Right`, `Down`, `j`, `n`, or `Space`: next document.
- `Left`, `Up`, `k`, or `p`: previous document.
- `Home`: first document.
- `End`: last document.
- `q`: quit.

The preview is drawn at roughly 75% of terminal width, with metadata shown to the
right. ANSI mode uses terminal character cells directly. Sixel mode scales the
image in pixels using an 8-pixel cell-width estimate, then places metadata in the
right-side terminal columns.

## Cache Format

Kannon writes generated data to `kannon.yaml` by default. This file is intended
to be inspectable and portable, but it may be large because thumbnails are stored
inline.

The high-level structure is:

```yaml
version: 1
generated_at: "2026-08-12T10:00:00+00:00"
thumbnail_size: 512
documents:
  - path: report.pdf
    path_abs: /absolute/path/report.pdf
    kind: pdf
    title: Quarterly Report
    source:
      size_bytes: 123456
      mtime_ns: 1797051735123456789
      modified: "2026-08-12T09:30:00+00:00"
      owner_user: xyzzy
    metadata:
      author: Example Person
      page_count: 12
    thumbnail:
      media_type: image/png
      encoding: base64
      max_edge: 512
      width: 362
      height: 512
      data: iVBORw0KGgo...
```

Cached records are reused when the source file size, source `mtime_ns`, and
configured thumbnail size still match. Use `--refresh` to force regeneration.

## Metadata

For PDFs, Kannon records metadata exposed by PyMuPDF, including fields such as:

- `title`
- `author`
- `subject`
- `keywords`
- `creator`
- `producer`
- `creationDate`
- `modDate`
- `page_count`
- first-page size in points

For Markdown, Kannon records:

- title from the first `# Heading` or first non-empty line.
- line count.
- word count.
- renderer identifier.

For every supported document, Kannon also records source file size, modified
time, and owner. The `user_name` field is added when the file owner is not the
current user.

## Renderer Notes

Sixel support is detected with environment heuristics such as `TERM`,
`TERM_PROGRAM`, `WEZTERM_EXECUTABLE`, `KONSOLE_VERSION`, and related variables.
Terminal sixel support is inconsistent, so use `--ansi` if the preview appears
garbled or your terminal claims partial support.

The ANSI renderer uses true-color escape sequences and upper-half block
characters. It should work in modern terminals even when sixel is unavailable.

Markdown rendering is intentionally lightweight. It handles headings, bullets,
blank lines, and fenced code blocks well enough for a first-page thumbnail, but
it is not a full CommonMark/browser renderer.

## Troubleshooting

If PDF scanning fails with `ModuleNotFoundError: No module named 'fitz'`, install
the project dependencies or install `poppler-utils`:

```sh
python -m pip install -e .
```

On Debian or Ubuntu systems the Poppler fallback package is typically:

```sh
sudo apt install poppler-utils
```

If sixel output corrupts the screen:

```sh
./kannon --ansi
```

If the cache appears stale:

```sh
./kannon --refresh
```

If no files are found, check that the input paths contain files ending in
`.pdf`, `.md`, or `.markdown`.

## Development

Run a syntax check:

```sh
python3 -m py_compile src/python/kannon/*.py
```

Build or install from the project metadata:

```sh
python -m pip install -e .
```

The implementation avoids a terminal UI framework so the image renderers can
control sixel and ANSI output directly. Keep display changes tested in both
`--ansi` and `--sixel` modes because terminal cursor behavior differs between
emulators.
