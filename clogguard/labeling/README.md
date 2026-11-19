# clogguard.labeling

Blind defect labelling (definition of visible defect onset, `docs/plan.md`).

```
clogguard label build runs/<run_id> --out rate_<n>.html    # send the page to a rater
clogguard label apply runs/<run_id> ratings_Alice.json ratings_Bob.json
```

The page is one self-contained HTML file: the run's photos in time order, embedded as base64,
numbered 1..N. It contains no sensor data, timestamps, alert time, induction details or run id
(only an opaque token). The rater enters a name, marks "First defect visible here" on one photo
or "No defect in any photo", and downloads a small JSON file. `apply` checks the token and photo
count, maps the index back to the photo's `t_s` from `meta.json`, and merges into
`meta.defect_ratings` (a re-rating by the same rater replaces the old one).
`RunMetadata.defect_onset_s` is then the earliest marked time.
