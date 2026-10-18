# Shared-folder volume labels

Comic volume view displays the main series as `01`, `02` (no main-series prefix)
and related series as `共和国編 01` or `一瀬グレン、16歳の破滅 01`.
The backend supplies the same labels/order to the inline listing metadata and
standalone volume-view endpoint. File view keeps the ordinary names. Hovering a
volume label shows its original display filename.

This is metadata-only: no renaming, file moves, ID changes, progress migration,
new persistent metadata or additional directory/archive reads are introduced.

## Generic inference

- Extract title text preceding recognized volume/range/set/chapter markers.
- Normalize Unicode width and whitespace for comparison; discard leading author
  credits, boundary rating stars, rating tags and explicit color-edition metadata.
- Compare with the folder title and actual observed shorter title forms. Do not
  invent a base from an arbitrary longest common character prefix, and do not use
  a dictionary of manga or arc names.
- Display the remaining suffix as the arc name, unwrapping enclosing brackets.
  Full folder titles and their observed shortened forms are main-title aliases
  only when their observed starting volume numbers do not overlap; numeric suffixes such as `1.0` remain distinguishing labels.
- Unrelated titles in a mixed folder retain their full title before the number.
  Unrecognized files retain their existing filename fallback.
- Main volumes come first, followed by each arc in normalized lexical order,
  with existing numeric volume/chapter ordering inside each arc. This is not an
  inferred publication chronology. Color markers and ranges remain visible.
- Inference always uses the whole listing, so filtering does not change labels.

Filename metadata cannot reliably distinguish every subtitle alias from a sequel
or discover relationships between completely different names. Such names remain
visible rather than being assigned an invented relationship. Identical titles
and identical volume numbers may still have identical labels (e.g. scan variants).

Tests cover arbitrary Japanese/English arc names, author changes, numeric sequels,
color editions, ranges, ratings, unknown/unrelated titles, stable ordering and
both API paths including filtered listings and unchanged file identities/content.
