#!/usr/bin/env python3
"""
Computes real, per-day contribution counts from GitLab activity.

Replaces the old approach of trusting a push event's `commit_count` at face
value. That number is GitLab's count of commits *in that push*, not commits
*you personally authored* -- an import, a mirror sync, or a history rewrite
(force-push after rebase/filter-repo) can attach thousands of someone else's
commits, or the same commits twice, to a single event under your account.

For every "pushed to"/"pushed new" event we instead fetch the actual commits
introduced by that push and keep only the ones whose author email is one of
this GitLab account's own verified emails. Everything else (issues, MRs,
comments, etc.) still counts as 1 per event, deduped by event id so paginating
mid-update can't double-count.

Per-event and per-day caps are a safety net: no single anomaly should ever
again be able to make the caller try to create tens of thousands of commits.

Prints "YYYY-MM-DD COUNT" lines to stdout, one per day with activity.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict

GITLAB_URL = os.environ["GITLAB_URL"].rstrip("/")
GITLAB_TOKEN = os.environ["GITLAB_TOKEN"]
GITLAB_USER = os.environ["GITLAB_USER"]

PER_EVENT_CAP = 50
PER_DAY_CAP = 100
HTTP_TIMEOUT = 15
MAX_RETRIES = 3


def api_get(path, params=None):
    url = f"{GITLAB_URL}/api/v4{path}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        url = f"{url}?{query}"
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": GITLAB_TOKEN})

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                print(f"Retrying ({attempt}/{MAX_RETRIES}) after error on {path}: {e}",
                      file=sys.stderr)
                time.sleep(2 * attempt)
    raise last_err


def get_own_emails():
    emails = set()
    try:
        me = api_get("/user")
        if me.get("email"):
            emails.add(me["email"].lower())
    except Exception as e:
        print(f"Warning: could not fetch /user: {e}", file=sys.stderr)
    try:
        for entry in api_get("/user/emails"):
            if entry.get("email"):
                emails.add(entry["email"].lower())
    except Exception as e:
        print(f"Warning: could not fetch /user/emails: {e}", file=sys.stderr)

    # GitLab's verified-emails API doesn't necessarily list every address you
    # actually commit with (e.g. a personal email never added/verified on the
    # account). GITLAB_AUTHOR_EMAILS is an explicit, known-good allowlist that
    # covers that gap.
    extra = os.environ.get("GITLAB_AUTHOR_EMAILS", "")
    for e in extra.split(","):
        e = e.strip().lower()
        if e:
            emails.add(e)

    if not emails:
        print("Warning: no known emails to filter by; push-event commits "
              "cannot be author-filtered.", file=sys.stderr)
    return emails


def fetch_all_events():
    events, seen_ids, page = [], set(), 1
    while True:
        batch = api_get(f"/users/{GITLAB_USER}/events", {"per_page": 100, "page": page})
        if not isinstance(batch, list):
            print(f"Warning: unexpected events response on page {page}: {batch}", file=sys.stderr)
            break
        print(f"Page {page}: {len(batch)} events", file=sys.stderr)
        new = 0
        for ev in batch:
            if ev.get("id") not in seen_ids:
                seen_ids.add(ev.get("id"))
                events.append(ev)
                new += 1
        if len(batch) < 100:
            break
        page += 1
    print(f"Total unique events fetched: {len(events)}", file=sys.stderr)
    return events


def commits_for_push(event, own_emails):
    """Returns (matched_shas, all_authors_seen, raw_commit_count, resolved_count)."""
    project_id = event.get("project_id")
    push_data = event.get("push_data") or {}
    commit_to = push_data.get("commit_to")
    commit_from = push_data.get("commit_from")
    raw_commit_count = push_data.get("commit_count", 0)
    if not project_id or not commit_to:
        return [], set(), raw_commit_count, 0

    try:
        if commit_from:
            commits = api_get(f"/projects/{project_id}/repository/compare",
                               {"from": commit_from, "to": commit_to})
            commits = commits.get("commits", []) if isinstance(commits, dict) else []
        else:
            # Brand new branch/ref: no base to diff against. Bound the lookup
            # to the day of the event so we don't walk into unrelated history.
            date = event["created_at"][:10]
            commits = api_get(f"/projects/{project_id}/repository/commits",
                               {"ref_name": commit_to, "since": f"{date}T00:00:00Z",
                                "until": event["created_at"], "per_page": 100})
    except Exception as e:
        print(f"Warning: could not resolve commits for event {event.get('id')} "
              f"(project {project_id}): {e}", file=sys.stderr)
        return [], set(), raw_commit_count, 0

    if not isinstance(commits, list):
        return [], set(), raw_commit_count, 0

    shas = []
    seen_authors = set()
    for c in commits:
        author_email = (c.get("author_email") or "").lower()
        seen_authors.add(author_email)
        if author_email in own_emails:
            shas.append(c["id"])

    if len(shas) > PER_EVENT_CAP:
        print(f"Warning: event {event.get('id')} on {event['created_at'][:10]} "
              f"resolved to {len(shas)} authored commits, capping at {PER_EVENT_CAP} "
              f"(likely an import/mirror/history-rewrite, not real daily activity)",
              file=sys.stderr)
        shas = shas[:PER_EVENT_CAP]

    return shas, seen_authors, raw_commit_count, len(commits)


def main():
    own_emails = get_own_emails()
    print(f"Own verified emails used for author-filtering: {sorted(own_emails)}", file=sys.stderr)
    events = fetch_all_events()

    action_counts = defaultdict(int)
    for ev in events:
        action_counts[ev.get("action_name", "unknown")] += 1
    print("Action types found:", file=sys.stderr)
    for name, count in sorted(action_counts.items(), key=lambda x: -x[1]):
        print(f"    {count} {name}", file=sys.stderr)

    per_day = defaultdict(int)
    credited_shas = set()
    debug_raw = defaultdict(int)
    debug_resolved = defaultdict(int)
    debug_matched = defaultdict(int)
    debug_authors = defaultdict(set)
    debug_other = defaultdict(int)

    for ev in events:
        date = ev["created_at"][:10]
        action = ev.get("action_name")

        if action in ("pushed to", "pushed new"):
            shas, authors_seen, raw_count, resolved_count = commits_for_push(ev, own_emails)
            debug_raw[date] += raw_count
            debug_resolved[date] += resolved_count
            debug_authors[date] |= authors_seen
            for sha in shas:
                if sha not in credited_shas:
                    credited_shas.add(sha)
                    per_day[date] += 1
                    debug_matched[date] += 1
        else:
            per_day[date] += 1
            debug_other[date] += 1

    print("Per-day debug (raw commit_count from events / commits resolved via API / "
          "matched to own email(s) / other non-push events / distinct authors seen):",
          file=sys.stderr)
    for date in sorted(debug_raw.keys() | debug_other.keys()):
        print(f"  {date}: raw={debug_raw[date]} resolved={debug_resolved[date]} "
              f"matched={debug_matched[date]} other_events={debug_other[date]} "
              f"distinct_authors={len(debug_authors[date])}", file=sys.stderr)

    print("Contributions per day (after author-filtering and caps):", file=sys.stderr)
    for date in sorted(per_day):
        count = per_day[date]
        if count > PER_DAY_CAP:
            print(f"Warning: {date} totals {count}, capping at {PER_DAY_CAP}", file=sys.stderr)
            count = PER_DAY_CAP
        print(f"{date} {count}", file=sys.stderr)
        print(f"{date} {count}")


if __name__ == "__main__":
    main()
