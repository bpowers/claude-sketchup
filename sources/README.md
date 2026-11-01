# Sources

The evidence the model is built from. `plans/` holds the architect's drawings as PDFs, named as the set names them
(`plans/permit-set.pdf`, `plans/as-built-1962.pdf`), and `plans/registrations.json`, which places each drawing sheet in
the model's frame (`docs/DRAWINGS.md`). `photos/` holds photographs of the house as JPEG or PNG files, each named by a
short stem (`photos/living-01.jpg`) that the photo-matching files in `overlay/` use (`docs/PHOTOS.md`).

Drawings and photos are never altered: a photo's camera is tied to its pixels, and a drawing is the designer's record.
Renaming a photo to its stem before any picks name it is fine, and so is converting a HEIC or WebP photo once, at full
size, to JPEG (`docs/PHOTOS.md`, step 1). `registrations.json` is the one file the tools write here; everything else
derived from these files goes to `out/`.

Until the owner's house replaces it, this holds the example cottage's synthetic sources: `plans/example-cottage.pdf`,
`photos/living-01.jpg` and their registrations, drawn from the cottage by `tests/fixtures/example_sources.py`.
