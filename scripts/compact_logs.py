"""Compact the Codex app log database (logs_2.sqlite) to fix UI lag.

The Codex desktop app keeps writing diagnostic logs into logs_2.sqlite and
prunes old rows, but SQLite never shrinks the file, so it can balloon to
hundreds of MB of mostly free pages. Every session switch then has to churn
that huge file, which freezes the UI. VACUUM reclaims the free pages without
deleting a single row.

Usage (run only when the Codex app is fully closed):
    python compact_logs.py
    python compact_logs.py --db "C:/path/to/logs_2.sqlite" --backup-dir "D:/backup"
"""
import argparse
import datetime
import os
import shutil
import sqlite3
import subprocess

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".codex", "logs_2.sqlite")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=DEFAULT_DB, help="path to logs_2.sqlite (default: ~/.codex/logs_2.sqlite)")
    p.add_argument("--backup-dir", default=os.path.join(os.path.expanduser("~"), ".codex", "..", "codex-log-backup"),
                   help="directory for the pre-vacuum backup (default: ~/codex-log-backup)")
    p.add_argument("--process-name", default="ChatGPT.exe",
                   help="app process that must not be running (default: ChatGPT.exe)")
    return p.parse_args()


def main():
    args = parse_args()
    db = os.path.abspath(os.path.expanduser(args.db))
    bak_dir = os.path.abspath(os.path.expanduser(args.backup_dir))
    if not os.path.exists(db):
        raise SystemExit("log database not found: %s" % db)
    os.makedirs(bak_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    # 1. sanity check: the app must be closed (SQLite file is held open by the app)
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % args.process_name],
                         capture_output=True, text=True).stdout
    if args.process_name.lower() in out.lower():
        raise SystemExit("%s is still running - close the Codex app first." % args.process_name)

    # 2. backup (db + wal + shm)
    for suffix in ("", "-wal", "-shm"):
        src = db + suffix
        if os.path.exists(src):
            dst = os.path.join(bak_dir, "logs_2.sqlite" + suffix + "." + stamp)
            shutil.copy2(src, dst)
            print("backed up ->", dst)

    # 3. report live data size vs file size
    con = sqlite3.connect(db)
    live = con.execute("SELECT SUM(length(feedback_log_body)) FROM logs").fetchone()[0]
    before = os.path.getsize(db)
    print("live bytes: %.1f MB, file before: %.1f MB" % (live / 1048576.0, before / 1048576.0))

    # 4. VACUUM (reclaim free pages, keeps every row)
    con.execute("VACUUM")
    con.close()

    after = os.path.getsize(db)
    print("file after VACUUM: %.1f MB (was %.1f MB)" % (after / 1048576.0, before / 1048576.0))
    print("DONE. Safe to reopen Codex.")


if __name__ == "__main__":
    main()
