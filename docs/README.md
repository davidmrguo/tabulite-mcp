# docs/ — the project site

`index.html` is the Tabulite source walkthrough, published as a GitHub Pages
project site at **https://davidmrguo.github.io/tabulite-mcp/**.

This directory is the **source of truth** for that document. Edit `index.html`
here; the published site follows on the next push to `main`.

## How it is served

GitHub Pages is configured to deploy from the `main` branch, `/docs` folder —
Settings → Pages → *Deploy from a branch*. No build step and no workflow: the
file is served exactly as committed.

`.nojekyll` disables Jekyll processing. Nothing here needs it, and without the
file Pages would skip any path beginning with an underscore.

## Keeping it current

The walkthrough describes `src/tabulite_mcp` module by module, so it goes stale
when the code changes shape. Update it on a **major** edit — a new module, a new
tool, a changed architectural decision — not on every commit. The things that
drift first:

- the masthead line and footer (module count, total lines, the commit it was
  written against)
- the module numbering, which is a topological sort of the import graph, and the
  dependency diagram that mirrors it
- the nav rail, whose entries have to match the `<section id="mN">` anchors
- the tool table in the `server.py` section

`index.html` is fully self-contained — one file, no assets, no build. The only
external requests it makes are to Google Fonts.
