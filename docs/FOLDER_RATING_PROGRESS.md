# Folder ratings and reading progress

Ratings rename physical folders using `{zpi$r=N}`. Descendant media and comic
entry IDs include the parent path, so renaming only the folder previously made
saved progress appear to disappear. The rating endpoint did not delete progress.

Folder rating changes now capture comic entry identities before the rename and
transfer saved progress, bookmarks, known descendant media paths, video progress,
favorites and recent paths in one SQLite transaction. Nested archive container
paths are included. Existing target progress is replaced only by a newer record.
Source progress rows remain available for recovery. A database failure rolls
back the database transaction and attempts to restore the original folder name.
The sequence cache is invalidated after a successful Web rating operation.

For records hidden by older versions, restore the folder's previous rating and
reopen the folder before changing it again with the fix installed. If it was
previously unrated, remove its stars. Do not mark every volume as read or clear
the database as a recovery step. This patch does not automatically infer unknown
historical folder names, repair external renames, or retarget already-open reader
sessions; reopen the reader after changing a parent folder's rating.

Regression tests cover rating changes/removal, completed and partial progress,
image folders, direct/nested archives, video resume, recent paths, and database
failure rollback. QNAP device recovery has not been verified.
