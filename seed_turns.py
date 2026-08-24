#!/usr/bin/env python3
"""Seed a pipeline design's Chat history: one import_step turn + transcript.

The studio's Chat history is GET /designs/{slug}/turns - generation_jobs rows
(target_design_id + owner_id) with the transcript in claude_session_store
keyed by cc_session_id (panda-social-backend handlers_design_turns.go,
buildJobReplay). bin/importdesign creates NO turn, so pipeline designs showed
an empty chat page (found on Trotter, 2026-08-24). This inserts ONE resolved
turn per design:

  generation_jobs       type=import_step, status=done (a resolved status -
                        workers never claim it; no usage/billing fields are
                        written so settlement never looks at it), prompt =
                        the user turn of <out_dir>/conversation.jsonl,
                        cc_session_id = "pipeline-<design_id>"
  claude_session_store  one doc per conversation.jsonl line (entry = the line
                        verbatim - the SDK entry shape the FE renderer already
                        knows; mtime = unix MILLISECONDS, the merged feed's
                        unit; inserted in order so the _id sort holds)

Idempotent by cc_session_id: an existing turn is left alone; --force replaces
the transcript and refreshes the job. Timestamps come from the conversation
lines themselves, so a stranger's publish-bookmark cut (notAfter) keeps the
turn visible as long as it predates published_at.

    ./seed_turns.py <out_dir> [--force]     # needs conversation.jsonl + published.json

Needs pymongo (the pi-agent venv has it); exits 0 with a note when it or the
Mongo config is missing, so publish flows can call it best-effort anywhere.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ENV = "/root/panda-secrets/.env"


def load_mongo():
    try:
        from pymongo import MongoClient
    except ImportError:
        print("seed_turns: pymongo not available - skipped")
        return None
    uri = db = None
    p = Path(ENV)
    if not p.is_file():
        print(f"seed_turns: {ENV} missing - skipped")
        return None
    for line in p.read_text().splitlines():
        line = line.strip()
        if line.startswith("MONGODB_URI="):
            uri = line.split("=", 1)[1].strip().strip('"')
        if line.startswith("MONGODB_DBNAME="):
            db = line.split("=", 1)[1].strip().strip('"')
    if not (uri and db):
        print("seed_turns: no MONGODB_URI/DBNAME - skipped")
        return None
    return MongoClient(uri)[db]


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    out_dir = Path(sys.argv[1]).resolve()
    force = "--force" in sys.argv
    conv, pub = out_dir / "conversation.jsonl", out_dir / "published.json"
    if not conv.is_file() or not pub.is_file():
        print(f"seed_turns: need {conv.name} + {pub.name} in {out_dir} - skipped")
        return 0
    db = load_mongo()
    if db is None:
        return 0
    from bson import ObjectId

    design_id = ObjectId(json.loads(pub.read_text())["id"])
    d = db.designs.find_one({"_id": design_id}, {"owner_id": 1, "title": 1})
    if not d:
        print(f"seed_turns: design {design_id} not found - skipped")
        return 0

    lines = [json.loads(l) for l in conv.read_text(encoding="utf-8").splitlines() if l.strip()]
    prompt = next((l["message"]["content"] for l in lines
                   if l.get("type") == "user" and isinstance(l.get("message", {}).get("content"), str)), "")
    if not (lines and prompt):
        print("seed_turns: conversation has no user prompt - skipped")
        return 0
    first, last = parse_ts(lines[0]["timestamp"]), parse_ts(lines[-1]["timestamp"])

    sid = f"pipeline-{design_id}"
    existing = db.generation_jobs.find_one({"cc_session_id": sid}, {"_id": 1})
    if existing and not force:
        print(f"seed_turns: turn already seeded for {d.get('title')} ({existing['_id']}) - use --force")
        return 0

    job = {
        "schema_version": 2, "type": "import_step", "status": "done",
        "user_id": d["owner_id"], "target_design_id": design_id,
        "prompt": prompt, "progress": 100, "cc_session_id": sid,
        "created_at": first, "updated_at": last,
        "finished_at": int(last.timestamp()),
    }
    if existing:
        db.generation_jobs.update_one({"_id": existing["_id"]}, {"$set": job})
        job_id = existing["_id"]
    else:
        job_id = db.generation_jobs.insert_one(job).inserted_id
    db.claude_session_store.delete_many({"session_id": sid})
    for l in lines:  # one insert per entry, in order - the reader sorts by _id
        db.claude_session_store.insert_one({
            "session_id": sid, "subpath": "",
            "entry": l, "mtime": int(parse_ts(l["timestamp"]).timestamp() * 1000),
        })
    print(f"seed_turns: {d.get('title')} -> job {job_id}, {len(lines)} transcript entries (session {sid})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
