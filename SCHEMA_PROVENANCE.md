# SQLite schema contract and provenance

## Supported contract

The upload workflow accepts a SQLite database containing a `dynamics` table with at least these seven columns:

```sql
CREATE TABLE dynamics (
    id          INTEGER NOT NULL PRIMARY KEY,
    uid         INTEGER NOT NULL,
    topic_name  TEXT,
    time        TEXT,
    status      INTEGER NOT NULL,
    description TEXT,
    data        TEXT
);
```

Additional tables, columns, indexes, and constraints are allowed. The POC checks the required column names rather than requiring byte-for-byte DDL equivalence. Rows containing a null value in a profiled field are excluded from the aggregate shape profile.

The database is opened read-only. SQLite is used only to inspect the schema and calculate aggregate shape statistics; it is not used for the timed row-versus-column comparison.

## Provenance

This contract was adapted, with changes, from an internal/private data-collection implementation. That implementation reused portions of schema and code from the public [dingwen07/Bilibili-dynamic](https://github.com/dingwen07/Bilibili-dynamic) project.

The corresponding public SQLite design can be reviewed in [`topic_dynamic.py::TopicDynamic.init_db`](https://github.com/dingwen07/Bilibili-dynamic/blob/master/topic_dynamic.py#L206-L225).

The private implementation is intentionally not named or linked from this public repository. Neither upstream codebase nor any source database content is bundled with this POC. Compatibility with the seven-column contract does not imply exact implementation compatibility with either upstream codebase.
