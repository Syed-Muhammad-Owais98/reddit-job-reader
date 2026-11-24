"""
Reddit Job Agent for Syed Muhammad Owais

Reads new posts from the subreddits in config.json, keeps the hiring posts that
match one of the four CVs (Full Stack, Frontend, Backend, DevOps), and saves
them to matches.csv so Claude can write the replies and update the Google Sheet.

It never posts, comments or sends messages. It only reads.

Usage (Windows):
    py reddit_job_agent.py            normal run
    py reddit_job_agent.py --test     checks if Reddit can be reached, saves nothing
    py reddit_job_agent.py --hours 72 look further back than the config says
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("The 'requests' package is missing. Run:  py -m pip install -r requirements.txt")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SEEN_PATH = os.path.join(HERE, "seen_posts.json")
CSV_PATH = os.path.join(HERE, "matches.csv")
LATEST_PATH = os.path.join(HERE, "latest_matches.txt")

CSV_COLUMNS = [
    "Date Found", "Subreddit", "Post Title", "Post Link", "Poster", "Posted At",
    "Stack", "CV To Use", "Match Score", "Matched Skills", "Red Flags",
    "How To Apply", "Post Text", "Draft Reply", "Status", "Date Sent",
    "Response Received", "Follow Up Date", "Notes",
]

ATOM = "{http://www.w3.org/2005/Atom}"


# ----------------------------------------------------------------- helpers

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def strip_html(raw):
    text = re.sub(r"<br\s*/?>|</p>", "\n", raw or "", flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.replace("submitted by", "\nsubmitted by").strip()


def has_term(text, term):
    """Whole word match that also works for terms like next.js, c# or ci/cd."""
    pattern = r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


# ----------------------------------------------------------------- fetching

def fetch_with_api(cfg, sub, limit):
    import praw  # only needed when API keys are filled in
    api = cfg["reddit_api"]
    reddit = praw.Reddit(
        client_id=api["client_id"],
        client_secret=api["client_secret"],
        user_agent=api.get("user_agent") or cfg["user_agent"],
    )
    reddit.read_only = True
    posts = []
    for s in reddit.subreddit(sub).new(limit=limit):
        posts.append({
            "id": s.id,
            "title": s.title,
            "link": "https://www.reddit.com" + s.permalink,
            "author": str(s.author) if s.author else "",
            "created": datetime.fromtimestamp(s.created_utc, tz=timezone.utc),
            "text": s.selftext or "",
            "flair": s.link_flair_text or "",
        })
    return posts


RSS_URLS = [
    "https://www.reddit.com/r/{sub}/new/.rss?limit={limit}",
    "https://www.reddit.com/r/{sub}/new.rss",
    "https://www.reddit.com/r/{sub}/.rss",
    "https://old.reddit.com/r/{sub}/new/.rss",
    "https://old.reddit.com/r/{sub}/.rss",
]


def fetch_with_rss(cfg, sub, limit, verbose=False):
    headers = {"User-Agent": cfg["user_agent"], "Accept": "application/atom+xml, application/xml"}
    errors, resp = [], None
    for pattern in RSS_URLS:
        url = pattern.format(sub=sub, limit=limit)
        try:
            r = requests.get(url, headers=headers, timeout=30)
        except Exception as e:
            errors.append(f"{url} -> {e.__class__.__name__}")
            continue
        looks_like_feed = b"<feed" in r.content[:2000]
        if verbose:
            print(f"    {url} -> HTTP {r.status_code}{'' if looks_like_feed else ' (not a feed)'}")
        if r.status_code == 200 and looks_like_feed:
            resp = r
            break
        errors.append(f"HTTP {r.status_code}")
    if resp is None:
        raise RuntimeError("every feed address failed (" + ", ".join(errors) + ")")
    root = ET.fromstring(resp.content)
    posts = []
    for entry in root.findall(f"{ATOM}entry"):
        link_el = entry.find(f"{ATOM}link")
        author_el = entry.find(f"{ATOM}author/{ATOM}name")
        when = entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated")
        posts.append({
            "id": (entry.findtext(f"{ATOM}id") or "").replace("t3_", ""),
            "title": entry.findtext(f"{ATOM}title") or "",
            "link": link_el.get("href") if link_el is not None else "",
            "author": (author_el.text or "").replace("/u/", "") if author_el is not None else "",
            "created": datetime.fromisoformat(when.replace("Z", "+00:00")) if when else datetime.now(timezone.utc),
            "text": strip_html(entry.findtext(f"{ATOM}content") or ""),
            "flair": "",  # the feed does not include post flair
        })
    return posts


def fetch_posts(cfg, sub, limit, verbose=False):
    api = cfg.get("reddit_api", {})
    if api.get("client_id") and api.get("client_secret"):
        return fetch_with_api(cfg, sub, limit), "API"
    return fetch_with_rss(cfg, sub, limit, verbose), "RSS"


# ----------------------------------------------------------------- matching

def is_hiring_post(cfg, post):
    title = post["title"].lower()
    flair = post["flair"].lower()
    for bad in cfg["skip_if_title_contains"]:
        if bad.lower() in title or bad.lower() in flair:
            return False
    return True


def score_post(cfg, post):
    text = f"{post['title']} {post['flair']} {post['text']}".lower()
    stacks = cfg["stacks"]

    per_stack, matched = {}, {}
    for name, info in stacks.items():
        total, hits = 0, []
        for term, weight in info["keywords"].items():
            if has_term(text, term):
                total += weight
                hits.append(term)
        per_stack[name], matched[name] = total, hits

    flags = [t for t in cfg["red_flag_terms"] if has_term(text, t)]
    deal_breakers = [t for t in cfg["deal_breaker_terms"] if has_term(text, t)]

    front, back, ops = per_stack["Frontend"], per_stack["Backend"], per_stack["DevOps"]
    full = per_stack["Full Stack"]
    if full > 0 or (front >= 4 and back >= 4):
        stack = "Full Stack"
        raw = full + front + back + ops * 0.5
    else:
        stack = max(("Frontend", "Backend", "DevOps"), key=lambda s: per_stack[s])
        raw = per_stack[stack] + 0.5 * sum(v for k, v in per_stack.items() if k != stack)

    raw += sum(cfg["bonus_terms"].get(t, 0) for t in cfg["bonus_terms"] if has_term(text, t))
    raw -= 3 * len(flags)
    score = max(0, min(10, round(raw / 2)))
    if deal_breakers:
        score = 0

    skills = sorted({h for hits in matched.values() for h in hits})
    return {
        "stack": stack,
        "cv": stacks[stack]["cv_file"],
        "score": score,
        "skills": ", ".join(skills),
        "flags": ", ".join(flags + deal_breakers),
    }


def how_to_apply(post):
    text = f"{post['title']} {post['text']}".lower()
    if re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
        return "Email"
    if re.search(r"\b(dm|pm|chat|message) me\b|send (me )?a (dm|pm|message)|\bdm\b", text):
        return "Reddit DM"
    if re.search(r"apply (here|at|via|through)|application form|careers page|apply now", text):
        return "External link"
    if re.search(r"comment below|reply below|leave a comment", text):
        return "Comment"
    return "Check post"


# ----------------------------------------------------------------- main

def append_csv(rows):
    new_file = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="only check that Reddit can be reached")
    parser.add_argument("--hours", type=int, help="how many hours back to look")
    args = parser.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        print("config.json was not found next to this script.")
        sys.exit(1)

    hours = args.hours or cfg["lookback_hours"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    seen = set(load_json(SEEN_PATH, []))
    limit = cfg.get("posts_per_subreddit", 50)
    subs = cfg["subreddits"][:2] if args.test else cfg["subreddits"]

    rows, report = [], []
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M")

    for sub in subs:
        try:
            if args.test:
                print(f"r/{sub}: trying feed addresses")
            posts, method = fetch_posts(cfg, sub, limit, verbose=args.test)
        except Exception as e:
            print(f"r/{sub}: could not read posts. {e}")
            continue
        print(f"r/{sub}: read {len(posts)} posts using {method}")
        if args.test:
            for p in posts[:5]:
                print("   ", p["created"].strftime("%Y-%m-%d %H:%M"), "|", p["title"][:90])
            continue

        for p in posts:
            if p["id"] in seen or p["created"] < cutoff:
                continue
            seen.add(p["id"])
            if not is_hiring_post(cfg, p):
                continue
            m = score_post(cfg, p)
            if m["score"] < cfg["min_score"]:
                continue
            rows.append({
                "Date Found": now_text,
                "Subreddit": f"r/{sub}",
                "Post Title": p["title"],
                "Post Link": p["link"],
                "Poster": p["author"],
                "Posted At": p["created"].astimezone().strftime("%Y-%m-%d %H:%M"),
                "Stack": m["stack"],
                "CV To Use": m["cv"],
                "Match Score": m["score"],
                "Matched Skills": m["skills"],
                "Red Flags": m["flags"],
                "How To Apply": how_to_apply(p),
                "Post Text": p["text"][:3000],
                "Status": "New",
            })
            report.append(
                f"[{m['score']}/10] {m['stack']} | r/{sub}\n{p['title']}\n{p['link']}\n"
                f"Skills: {m['skills']}\nApply by: {how_to_apply(p)}\n"
                f"{('Red flags: ' + m['flags']) if m['flags'] else ''}\n\n{p['text'][:1500]}\n"
                + "-" * 70
            )
        time.sleep(cfg.get("seconds_between_subreddits", 3))

    if args.test:
        print("\nTest finished. If you see post titles above, the script can read Reddit.")
        return

    save_json(SEEN_PATH, sorted(seen))
    if rows:
        append_csv(rows)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        f.write(f"Run at {now_text}, looking back {hours} hours. {len(rows)} matches.\n\n")
        f.write("\n".join(report))

    print(f"\nDone. {len(rows)} new matching posts saved to matches.csv and latest_matches.txt")
    print("Now tell Claude: 'check my new Reddit matches'")


if __name__ == "__main__":
    main()
