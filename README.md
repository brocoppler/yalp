# yalp — Robot Spec

The spec for **yalp**, a from-scratch hobby robot: a Raspberry Pi 5 brain with a USB camera and
two differential-drive wheels that you talk to in plain language — it interprets your intent, moves
and looks around, and tells you what it sees. The honest mental model is *a clever talking pet that
sees, chats, and moves toward things — not a butler that fetches.*

## Start here

Open **`index.html`** in a browser — a clean, navigable, badge-coded consolidation of the whole
spec. **It is fully self-contained:** every doc is embedded, so any sidebar link or any
`` `filename.md` `` reference in the text opens **inline** — you never leave the page. Share the
single `index.html` file and the recipient gets everything, offline, with no server.

## How to view

No dependencies, no server, no build step required just to read it:

```
open index.html        # macOS — opens in your default browser
```

Click any sidebar doc link or any `` `*.md` `` reference to read that doc inline. Deep links work too:
`index.html#doc/architecture.md` jumps straight to that doc. Press **Esc** or **← Back** to return to
the overview. The four opinionated markers — **DECISION**, **THESIS**, **RISK**, **OPEN** — render as
colored callouts inside the docs and as a legend on the overview.

## Source-of-truth workflow

The **markdown files are the source of truth.** The page is generated from them.

1. **Edit content** — edit the `.md` files in `product/`, `technical/`, and `archive/`. Edit
   `index.html` directly *only* for the consolidated overview sections or layout/CSS.
2. **Rebuild** — re-embed the docs into the page (needs `python3`, preinstalled on macOS):
   ```
   python3 build.py     # embeds all docs into index.html, prints a summary
   ```
3. **Reload** the browser (⌘R / Ctrl-R).

`build.py` reads each doc from its subdirectory but embeds it under its **basename** (e.g.
`technical/architecture.md` → `data-name="architecture.md"`) so the backticked cross-links inside
the docs resolve. Only `build.py` touches the embedded copies between the `<!--DOCS:START-->` /
`<!--DOCS:END-->` markers — your `.md` files stay the editable source.

## Add a new doc

1. Create the `.md` file (e.g. `technical/sensing-notes.md`).
2. Add its **path** to the `DOCS` list in `build.py` (the path, not just the basename).
3. Add a sidebar link in `index.html` under the right group:
   `<a class="doc" data-doc="sensing-notes.md">sensing-notes.md</a>` (use the **basename**).
4. Run `python3 build.py` and reload.

## Document map

| Track | Doc | What it is |
|---|---|---|
| Product | `product/product-vision.md` | Why yalp exists, who it's for, the two horizons, the scope envelope. |
| Product | `product/product-spec.md` | What yalp does from the user's seat — capability catalog, acceptance scenarios, latency. |
| Technical | `technical/architecture.md` | The two-loop concept, the agent/tool model, the contract between thinking and reflexes. |
| Technical | `technical/hardware.md` | Bill of materials, power architecture, drivetrain, sensing, pin planning. |
| Technical | `technical/software-spec.md` | The two-layer runtime, the loop-to-loop wire contract, person-tracking, config. |
| Technical | `technical/roadmap.md` | Build sequencing, the go/no-go gates, the risk register, the consolidated open questions. |
| Archive | `archive/initial-plan.md` | The original brain-dump the six specs were factored out of — kept for provenance. |

## How it's built

`index.html` is a single, hand-written, zero-dependency file: a sticky sidebar, a consolidated
overview, and a small vanilla-JS markdown renderer that displays any doc inline in a modal overlay
with hash routing (`#doc/<name>`). No CDNs, no frameworks, no external assets — offline-first by
design. `build.py` is the only moving part, and it only re-embeds the markdown between the DOCS
markers.
