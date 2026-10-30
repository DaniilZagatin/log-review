# Provenance of `map_presentation`

This package is the first agent's (producer, "Белка") trusted map renderer, vendored so the
consumer report works offline without importing `pko`, without an installed producer and
without any absolute path. The consumer adds only a logging overlay through the renderer's own
`scenario_map_parts(..., overlay=..., embedded=True)` API; it never builds a second map and never
reads or executes incoming HTML.

| File in this package | Producer source (repository-relative) | Relation |
|---|---|---|
| `scenario_map.py` | `producer/backend/pko/render/scenario_map.py` | Copy; differs **only** in its import lines (see rule below) plus a three-line provenance comment at the top |
| `theme.py` | `producer/backend/pko/render/dpss_theme.py` (`embedded_fonts`), `producer/backend/pko/render/horizon_theme.py` (`horizon_tokens`) | Same function bodies; asset location is `assets/` of this package |
| `compat.py` | `producer/backend/pko/stages/inputs.py` (`map_revision`), `producer/backend/pko/stages/contracts.py` (`validate_hierarchy`) | `map_revision` unchanged; `validate_hierarchy` keeps the structural/reference checks only (the consumer cannot recompute the producer's grouping-input and methodology digests) |
| `assets/horizon/tokens.css` | `producer/backend/pko/render/design/horizon/tokens.css` | Unchanged copy; SHA256 must equal the source |
| `assets/dpss/SBSansDisplay-400.woff2`, `-600.woff2`, `-700.woff2` | `producer/backend/pko/render/assets/dpss/SBSansDisplay-{400,600,700}.woff2` | Unchanged binary copies; SHA256 must equal the source (see `assets/dpss/PROVENANCE.md`) |

## Rule for `scenario_map.py`

Change the producer renderer first, then re-vendor. Never edit only this copy. The copy is the
producer file with this exact import block

```python
from pko.render.dpss_theme import embedded_fonts
from pko.render.horizon_theme import horizon_tokens
from pko.stages.contracts import validate_hierarchy
from pko.stages.inputs import map_revision
```

replaced by

```python
from .compat import map_revision, validate_hierarchy
from .theme import embedded_fonts, horizon_tokens
```

and these three comment lines prepended:

```python
# Vendored from the first agent (producer): backend/pko/render/scenario_map.py.
# Rule: this copy differs from that file only in its import lines (pko.* -> .theme/.compat).
# Change the producer renderer first, then re-vendor; never edit only this copy.
```

Deterministic re-vendoring and verification, run from a checkout that contains both `producer/`
and `consumer/` (`--check` only compares and exits non-zero on any difference):

```bash
python3 -I - [--check] <<'PY'
from pathlib import Path
import hashlib, sys
source = Path('producer/backend/pko/render/scenario_map.py').read_text(encoding='utf-8')
target = Path('consumer/src/process_observability/map_presentation/scenario_map.py')
old = ('from pko.render.dpss_theme import embedded_fonts\n'
       'from pko.render.horizon_theme import horizon_tokens\n'
       'from pko.stages.contracts import validate_hierarchy\n'
       'from pko.stages.inputs import map_revision\n')
new = ('from .compat import map_revision, validate_hierarchy\n'
       'from .theme import embedded_fonts, horizon_tokens\n')
header = ('# Vendored from the first agent (producer): backend/pko/render/scenario_map.py.\n'
          '# Rule: this copy differs from that file only in its import lines (pko.* -> .theme/.compat).\n'
          '# Change the producer renderer first, then re-vendor; never edit only this copy.\n')
if source.count(old) != 1:
    sys.exit('producer import block changed; update this rule first')
expected = header + source.replace(old, new, 1)
if '--check' in sys.argv:
    sys.exit(0 if target.exists() and target.read_text(encoding='utf-8') == expected else 'vendored renderer differs')
target.write_text(expected, encoding='utf-8')
print(hashlib.sha256(expected.encode('utf-8')).hexdigest(), target)
PY
```

Assets are verified by comparing `shasum -a 256` of each producer source with its copy here.

## Packaging

`consumer/pyproject.toml` lists `assets/dpss/*.woff2`, `assets/horizon/tokens.css` and the
provenance notes as package data of `process_observability.map_presentation`, so an installed
wheel or a plain copy of this folder renders the same offline page.
