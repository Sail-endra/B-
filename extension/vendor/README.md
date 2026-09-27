# Vendored parser libraries (included)

These are bundled in the extension because Manifest V3 forbids loading code
from a CDN in extension pages. They are loaded by `offscreen.html`.

| File | Package / version | Used for |
|---|---|---|
| `pdf.min.js`, `pdf.worker.min.js` | `pdfjs-dist@3.11.174` (`build/`) | PDF text + figure extraction |
| `mammoth.browser.min.js` | `mammoth@1.13.0` | DOCX -> HTML (incl. images) |
| `jszip.min.js` | `jszip@3.10.2` (`dist/`) | PPTX (a zip of XML files) |

## Do not "upgrade" pdf.js by just copying files

pdf.js 4.x and later ship **only** ES-module builds (`.mjs`), including in
`legacy/build/`. `offscreen.html` loads classic scripts, so an `.mjs` file
renamed to `pdf.min.js` fails with a syntax error on `export`, pdf.js never
loads, and every PDF fails to parse. 3.11.174 is the last classic-script
release. Upgrading requires changing `offscreen.html` to
`<script type="module">` and importing pdf.js explicitly.

## Verified

All three libraries were run through `offscreen.js` against real generated
PDF/DOCX/PPTX files under the MV3 extension-page CSP
(`script-src 'self' 'wasm-unsafe-eval'`): text, headings and embedded images
extracted, zero CSP violations. `offscreen.js` passes
`isEvalSupported: false` to pdf.js so it never compiles fonts with
`new Function()`.
