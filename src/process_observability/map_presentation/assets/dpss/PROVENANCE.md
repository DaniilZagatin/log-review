# SB Sans Display 400/600/700 — unchanged copies

Source files (repository-relative, first agent / producer):

- `producer/backend/pko/render/assets/dpss/SBSansDisplay-400.woff2`
- `producer/backend/pko/render/assets/dpss/SBSansDisplay-600.woff2`
- `producer/backend/pko/render/assets/dpss/SBSansDisplay-700.woff2`

Each file here is an unchanged byte copy. Its SHA256 must equal the SHA256 of the producer
source file (verify with `shasum -a 256` on both); every file starts with the WOFF2 signature
`wOF2`.

The producer's note (`producer/backend/pko/render/assets/dpss/README.md`) states that the fonts
come from the user-supplied archive «Дизайн-система ДПСС (Claude Design).zip», folder
`ДПСС-кит/assets`, are unmodified, and are the user's brand materials rather than a separate
open font package; no separate licence was included in that archive.

`theme.embedded_fonts()` embeds the WOFF2 files into the report as `data:font/woff2` URIs, so the
page makes no external request and needs no installed font.
