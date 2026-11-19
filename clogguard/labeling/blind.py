"""Blind defect labelling.

``build_page`` makes one self-contained HTML page with a run's photos in time order, numbered
1..N. The page shows nothing else about the run: no sensor data, no timestamps, no alert time,
no induction details, no run id (only an opaque token). The rater marks the first photo with
visible under-extrusion (or "no defect") and downloads a small JSON file. ``apply_ratings``
maps the photo index back to ``t_s`` from ``meta.json`` and merges into
``meta.defect_ratings`` (one rating per rater; a re-rating replaces the old one).
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
from pathlib import Path
from typing import Any

from clogguard.core.schema import DefectRating, PhotoRecord, RunMetadata
from clogguard.store.io import read_meta, write_meta

FORMAT = "blind-defect-rating"
FORMAT_VERSION = 1


def run_token(meta: RunMetadata) -> str:
    """Opaque id tying a ratings file to a run without revealing the run id."""
    return hashlib.sha256(f"clogguard-blind|{meta.run_id}".encode()).hexdigest()[:16]


def ordered_photos(meta: RunMetadata) -> list[PhotoRecord]:
    return sorted(meta.photos, key=lambda p: (p.t_s, p.path))


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Blind defect rating</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0 auto; max-width: 960px; padding: 1rem;
         color: #1d1d1f; background: #fafafa; }}
  header {{ position: sticky; top: 0; background: #fafafa; padding: .5rem 0;
           border-bottom: 1px solid #ddd; z-index: 1; }}
  figure {{ margin: 1rem 0; padding: .75rem; background: #fff; border: 2px solid #ddd;
           border-radius: 8px; }}
  figure.chosen {{ border-color: #c0392b; }}
  figure img {{ max-width: 100%; display: block; margin-bottom: .5rem; }}
  button {{ font-size: 1rem; padding: .4rem .8rem; cursor: pointer; }}
  #status {{ margin-left: .5rem; font-weight: 600; }}
</style>
</head>
<body>
<header>
  <h1>Blind defect rating</h1>
  <p>Photos are in print order. Mark the <b>first</b> photo where under-extrusion is visible,
     or choose "No defect in any photo". Then download your ratings file and send it back.</p>
  <label>Rater name <input id="rater" type="text" autocomplete="name"></label>
  <button id="none" type="button">No defect in any photo</button>
  <button id="download" type="button">Download ratings</button>
  <span id="status">Nothing chosen yet</span>
</header>
<main>
{figures}
</main>
<script>
const RUN_TOKEN = {token};
const N_PHOTOS = {n};
let choice; // undefined = not chosen, null = no defect, number = photo index
function show() {{
  document.querySelectorAll("figure").forEach(f =>
    f.classList.toggle("chosen", Number(f.dataset.index) === choice));
  document.getElementById("status").textContent =
    choice === undefined ? "Nothing chosen yet" :
    choice === null ? "Chosen: no defect" : "Chosen: first defect in photo " + choice;
}}
document.querySelectorAll("button.mark").forEach(b => b.addEventListener("click", () => {{
  choice = Number(b.dataset.index); show();
}}));
document.getElementById("none").addEventListener("click", () => {{ choice = null; show(); }});
document.getElementById("download").addEventListener("click", () => {{
  const rater = document.getElementById("rater").value.trim();
  if (!rater) {{ alert("Enter your name first."); return; }}
  if (choice === undefined) {{ alert("Choose a photo or 'No defect' first."); return; }}
  const out = {{format: "{fmt}", version: {ver}, run_token: RUN_TOKEN, n_photos: N_PHOTOS,
               rater: rater, first_defect_photo: choice}};
  const blob = new Blob([JSON.stringify(out, null, 2)], {{type: "application/json"}});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "ratings_" + rater.replace(/[^A-Za-z0-9_-]+/g, "_") + ".json";
  document.body.appendChild(a); a.click(); a.remove();
}});
</script>
</body>
</html>
"""

_FIGURE = """<figure data-index="{i}">
  <img src="{src}" alt="Photo {i}">
  <figcaption>Photo {i}
    <button class="mark" type="button" data-index="{i}">First defect visible here</button>
  </figcaption>
</figure>"""


def build_page(run_dir: str | Path) -> str:
    """Return the blind-rating HTML for one run. Reads only meta.json photo paths and the
    photo files; never the time series."""
    run_dir = Path(run_dir)
    meta = read_meta(run_dir)
    photos = ordered_photos(meta)
    if not photos:
        raise ValueError(f"run {run_dir} has no photos to rate")
    figures = "\n".join(
        _FIGURE.format(i=i, src=html.escape(_data_uri(run_dir / p.path), quote=True))
        for i, p in enumerate(photos, start=1)
    )
    return _PAGE.format(
        figures=figures,
        token=json.dumps(run_token(meta)),
        n=len(photos),
        fmt=FORMAT,
        ver=FORMAT_VERSION,
    )


def write_page(run_dir: str | Path, out: str | Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_page(run_dir), encoding="utf-8")
    return out


def _as_list(data: Any) -> list[dict[str, Any]]:
    items = data if isinstance(data, list) else [data]
    if not all(isinstance(x, dict) for x in items):
        raise ValueError("ratings file must hold a JSON object or a list of objects")
    return items


def apply_ratings(
    run_dir: str | Path, ratings: str | Path | dict[str, Any] | list[Any]
) -> RunMetadata:
    """Merge exported rating(s) into ``meta.defect_ratings``; returns the updated metadata."""
    run_dir = Path(run_dir)
    data = (
        json.loads(Path(ratings).read_text(encoding="utf-8"))
        if isinstance(ratings, str | Path)
        else ratings
    )
    meta = read_meta(run_dir)
    photos = ordered_photos(meta)
    token = run_token(meta)
    merged = list(meta.defect_ratings)
    for item in _as_list(data):
        if item.get("format") != FORMAT:
            raise ValueError(f"not a {FORMAT} file")
        if item.get("run_token") != token:
            raise ValueError("ratings file belongs to a different run")
        if item.get("n_photos") != len(photos):
            raise ValueError("photo count changed since the page was built; rebuild and re-rate")
        rater = str(item.get("rater", "")).strip()
        if not rater:
            raise ValueError("rating has no rater name")
        idx = item.get("first_defect_photo")
        if idx is None:
            t_s = None
        elif isinstance(idx, int) and not isinstance(idx, bool) and 1 <= idx <= len(photos):
            t_s = photos[idx - 1].t_s
        else:
            raise ValueError(f"photo index {idx!r} out of range 1..{len(photos)}")
        merged = [r for r in merged if r.rater.strip().casefold() != rater.casefold()]
        merged.append(DefectRating(rater=rater, t_s=t_s))
    meta.defect_ratings = merged
    meta = RunMetadata.model_validate(meta.model_dump())
    write_meta(run_dir, meta)
    return meta
