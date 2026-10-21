"""Apply pending SQL files in migrations/ once each.

If a migration is applied manually in Supabase, register its basename first:
INSERT INTO schema_migrations (filename) VALUES ('003_address_cleaning.sql');
"""
from pathlib import Path
import sys

from db import connect


# Apply each unapplied SQL migration and record its filename.
def main():
    conn = None
    try:
        conn = connect()
        with conn.cursor() as cur:
            cur.execute(
                "create table if not exists schema_migrations ("
                "filename text primary key, applied_at timestamptz not null default now())"
            )
            conn.commit()
            cur.execute("select filename from schema_migrations")
            done = {row[0] for row in cur.fetchall()}

            for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                if path.name in done:
                    print(f"already applied: {path.name}")
                    continue
                try:
                    sql = path.read_text(encoding="utf-8")
                    cur.execute(sql)
                    cur.execute("insert into schema_migrations (filename) values (%s)", (path.name,))
                    conn.commit()
                    print(f"applied: {path.name}")
                except Exception as exc:
                    conn.rollback()
                    print(f"migration failed: {path.name} ({type(exc).__name__})", file=sys.stderr)
                    return 1
        return 0
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        print(f"migration setup failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
