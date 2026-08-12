# Kannon

Kannon is a Python terminal CLI for scanning documents, generating first-page
thumbnails, caching the generated data in `kannon.yaml`, and browsing the results
one document at a time in a keyboard-driven terminal UI.

It currently supports:

- PDF first-page thumbnails rendered with PyMuPDF.
- Markdown thumbnails rendered with a lightweight Pillow-based renderer,
  including colored headings, bullets, and fenced source-code snippets.
- Mermaid flowchart previews inside Markdown fenced code blocks.
- RTF text previews.
- DOCX text and metadata previews using Python's ZIP/XML standard library.
- ODT previews for LibreOffice/OpenDocument text files using Python's ZIP/XML
  standard library.
- `kannon.yaml` cache output with metadata plus base64-encoded PNG thumbnails.
- Chafa terminal thumbnail output when the `chafa` command is available.
- Built-in sixel image output when explicitly requested or when Chafa is absent
  and the terminal appears to support sixel.
- Built-in ANSI true-color block output as a fallback or explicit mode.
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

PDF renderer dependencies are only required when the scan input includes PDFs.
Markdown, RTF, DOCX, and ODT scans do not require PyMuPDF, `pdftoppm`, or
`pdfinfo`.

System expectations:

- Python 3.10 or newer.
- Optional `poppler-utils` for `pdftoppm` and `pdfinfo`. Kannon uses this as a
  PDF fallback when PyMuPDF is not installed in the active Python environment.
  This is recorded in `system-dependencies.txt`.
- Optional `chafa` for preferred terminal thumbnail rendering. Kannon uses Chafa
  automatically when it is installed because it supports more terminal graphics
  environments and generally produces better output than the built-in ANSI block
  renderer.
- A terminal with sixel support for image-mode output, or any ANSI-compatible
  terminal for the fallback renderer.
- Fonts are optional. Kannon tries common DejaVu font paths for Markdown previews
  and falls back to Pillow's built-in font.
- Nerd Fonts are used by default for small terminal UI glyphs. Use `--no-nerd`
  for plain ASCII labels when your terminal font does not support those glyphs.

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
./kannon reports/ contract.pdf notes.md draft.docx outline.odt
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

Show two fitted previews per terminal page:

```sh
./kannon -n 2
```

Force ANSI fallback output:

```sh
./kannon --ansi
```

Force Chafa output:

```sh
./kannon --chafa
```

Disable automatic Chafa output:

```sh
./kannon --no-chafa
```

Force sixel output:

```sh
./kannon --sixel
```

Disable Nerd Font UI glyphs:

```sh
./kannon --no-nerd
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
right. By default Kannon fits one thumbnail per terminal page. Use `-n 2`,
`-n 3`, and so on to fit more previews on one page. Kannon reserves a bottom
status bar and gives each preview a colored header line:

```text
--[ FILENAME ][ CREATION DATE ][ LAST MODIFIED ]
```

Chafa is the preferred terminal renderer when available. Kannon invokes it in
colored symbols mode for reliable side-by-side layout inside the TUI. Built-in
ANSI mode uses terminal character cells directly. Built-in sixel mode scales the
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

For Markdown, RTF, DOCX, and ODT, Kannon records:

- title from the first `# Heading` or first non-empty line.
- line count.
- word count.
- renderer identifier.

DOCX and ODT previews also read document metadata such as title, creator, created
date, modified date, keywords, and description when those fields are present in
the file.

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
blank lines, and fenced code blocks with small syntax-highlighting rules well
enough for a first-page thumbnail, but it is not a full CommonMark/browser
renderer.

Heading scale is designed for terminal previews:

- `# H1` gets a three-terminal-line visual block.
- `## H2` gets a two-terminal-line visual block.
- `### H3` and smaller headings get a one-terminal-line visual block.

Mermaid support is aimed at terminal suitability rather than browser-perfect
layout. Fenced ` ```mermaid ` blocks using common `graph` or `flowchart` edge
syntax are drawn as colored boxes and connectors in the generated thumbnail. If
the Mermaid syntax is not recognized, Kannon falls back to highlighted Mermaid
source code instead of failing the document scan.

## Troubleshooting

Kannon tries to avoid raw dependency tracebacks. During normal scans and TUI
launches that include PDF files, and only when PDF files are present, it reports
missing PDF-related dependencies on stderr with:

- what dependency is missing,
- what Kannon will do instead,
- what functionality is degraded or unavailable,
- install commands to fix the environment,
- a reminder to rerun with `--refresh` after installing dependencies.

If PyMuPDF is missing but Poppler is available, Kannon will say it is using the
Poppler fallback. PDF thumbnails should still be generated, but metadata can vary.

If both PyMuPDF and Poppler's `pdftoppm` are missing, PDF records are still saved
in `kannon.yaml`, but affected PDFs have no generated thumbnail and the TUI shows
`(no thumbnail)` for them.

To install the primary Python PDF renderer and the rest of the Python
dependencies:

```sh
python -m pip install -e .
```

To install only PyMuPDF:

```sh
python -m pip install PyMuPDF
```

On Debian or Ubuntu systems the Poppler fallback package is typically:

```sh
sudo apt install poppler-utils
```

On Fedora:

```sh
sudo dnf install poppler-utils
```

On macOS with Homebrew:

```sh
brew install poppler
```

If sixel output corrupts the screen:

```sh
./kannon --ansi
```

If Chafa output is not appropriate for your terminal:

```sh
./kannon --no-chafa
```

If `--chafa` fails because Chafa is missing, install it with your package manager:

```sh
sudo apt install chafa
```

If the cache appears stale:

```sh
./kannon --refresh
```

If no files are found, check that the input paths contain files ending in
`.pdf`, `.md`, `.markdown`, `.rtf`, `.docx`, or `.odt`.

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

## Related Terminal Viewers

Kannon is intentionally closer to a cached document browser than a general image
or Markdown renderer. Useful nearby projects:

- Chafa: mature terminal graphics renderer with ANSI, sixel, Kitty, and iTerm2
  output support.
- libsixel / `img2sixel`: focused sixel encoder/decoder and conversion tools.
- timg: terminal image and video viewer with sixel, Kitty, iTerm2, and Unicode
  block fallbacks.
- viu: Rust terminal image viewer with terminal graphics protocols and block
  fallback output.
- Glow: polished Markdown CLI/TUI reader with themes and syntax highlighting.
- mdcat: Markdown `cat`/pager with syntax highlighting and inline image support
  in capable terminals; the original repository is archived, with maintained
  forks available.
