# Vendored static assets

Served from `static/vendor/` with relative URLs (HA ingress); no CDN and no build chain at
runtime. Source: the npm registry tarball for each package (`npm pack <pkg>@<ver>`), copied
unmodified. Templates link them as `static/<path>?v=<thermabot version>` (cache-busting; a
versioned request is cached for a year).

| File | Package | Version | License | SHA-256 |
|---|---|---|---|---|
| `htmx.min.js` | htmx.org (`dist/htmx.min.js`) | 2.0.4 | 0BSD | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `pico.min.css` | @picocss/pico (`css/pico.min.css`) | 2.0.6 | MIT | `dd5fd5591afd81ee21dcc117ad85c014dc3f1f19dc2d7b7d101ea0acc29274c2` |
| `uplot/uPlot.iife.min.js` | uplot (`dist/uPlot.iife.min.js`) | 1.6.31 | MIT | `2d27e8ad3d228164525ce213f9dc716f39b4e3aee0cc773fb3491c96cf4921a2` |
| `uplot/uPlot.min.css` | uplot (`dist/uPlot.min.css`) | 1.6.31 | MIT | `df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04` |
| `uplot/LICENSE` | uplot (`LICENSE`) | 1.6.31 | MIT | `8f989229699b4fe2f1a0432d0e9edc338a8a911e250e2d1b01ecd770a5f5b1bd` |

`tests/e2e/test_layout.py::test_vendor_hashes_match_vendor_md` checks these hashes.

uPlot is loaded only on pages with charts (Dashboard, Plan, History, Forecast, Efficiency),
together with `static/charts.js` (thermabot's own chart helpers). axe-core is NOT vendored:
the e2e contrast check computes WCAG contrast ratios itself.

To update uPlot: `npm pack uplot@<ver>`, copy `dist/uPlot.iife.min.js`, `dist/uPlot.min.css`
and `LICENSE` into `uplot/`, update this table, run the e2e suite.
