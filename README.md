# Reddit Job Agent

This script reads new posts from your job subreddits, keeps the hiring posts that match your four CVs, and saves them for Claude to write replies. It only reads Reddit and never posts anything.

## First time setup

1. Install Python from python.org if you do not have it, and tick "Add Python to PATH" during the install.
2. Double-click `test_connection.bat`. It installs the two packages the script needs and then tries to read the first subreddit in your list.
3. If you see post titles in the window, everything works. If you see an error such as HTTP 403 or 429, copy the window text and send it to Claude.

## Daily use

1. Double-click `run_agent.bat`.
2. When it says Done, tell Claude: "check my new Reddit matches".
3. Claude reads `matches.csv`, writes a reply for each good post using the right CV, adds them to your Google Sheet, and waits for your approval before anything is sent.

To look further back the first time, open a terminal in this folder and run `py reddit_job_agent.py --hours 72`.

## Files

| File | What it is |
|---|---|
| `reddit_job_agent.py` | The script |
| `config.json` | Subreddits, minimum score, keywords per stack, words to skip |
| `matches.csv` | Every matching post found so far |
| `latest_matches.txt` | Only the matches from the last run, easy to read |
| `seen_posts.json` | Posts already checked, so nothing is repeated |
| `PROJECT_INSTRUCTIONS.md` | Instructions to paste into a Claude Project |

## If Reddit blocks the free method

The script first reads Reddit's public RSS feeds. If Reddit blocks those, the only other option is official API keys. Request them at reddit.com/prefs/apps, choose a "script" app, and paste the client id and secret into the `reddit_api` section of `config.json`. The script switches to the API automatically once they are filled in.

## Running it automatically

Once the test works, you can make Windows run it twice a day. Open Task Scheduler, choose Create Basic Task, pick Daily, set the time, choose Start a program, and select `run_agent.bat`. Then add a second trigger for the evening.
