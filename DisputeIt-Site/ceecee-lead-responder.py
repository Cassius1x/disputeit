"""
CEECEE LEAD RESPONDER — Automated Reddit Reply Generator
==========================================================
Monitors your email inbox for F5Bot alerts, scrapes the Reddit posts,
generates custom replies using your local Ollama model, and saves
ready-to-paste responses.

SETUP:
1. Install Ollama: https://ollama.com
2. Pull a model: ollama pull llama3.1:8b (or qwen2.5:7b or mistral:7b)
3. Install Python dependencies: pip install requests beautifulsoup4 --break-system-packages
4. Set up ImprovMX email forwarding for ceecee@disputeit.xyz → your Gmail
5. Enable Gmail App Password: Google Account → Security → App Passwords
6. Fill in the CONFIG section below
7. Run: python3 ceecee-lead-responder.py

The script runs continuously, checking for new F5Bot alerts every 5 minutes.
When it finds one, it scrapes the Reddit post and generates a custom reply.
All replies are saved to READY-TO-POST.md — open it, click the link, paste the reply.
"""

import imaplib
import email
import time
import json
import os
import re
import requests
from datetime import datetime
from email.header import decode_header

# ============================================================
# CONFIG — Fill these in
# ============================================================

EMAIL_CONFIG = {
    'imap_server': 'imap.gmail.com',
    'email': 'yanceychance2@gmail.com',
    'password': 'YOUR_GMAIL_APP_PASSWORD',  # NOT your Gmail password. Google Account → Security → App Passwords → generate one
}

OLLAMA_CONFIG = {
    'url': 'http://localhost:11434',
    'model': 'llama3.1:8b',  # Change to whatever you pulled: qwen2.5:7b, mistral:7b, phi3:mini, etc.
}

DISPUTEIT_INFO = {
    'url': 'disputeit.xyz',
    'parking_price': 15,
    'traffic_price': 25,
    'submission_fee': 5,
}

CHECK_INTERVAL = 300  # Check every 5 minutes (in seconds)
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'READY-TO-POST.md')
PROCESSED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.processed_ids.json')

# ============================================================
# SYSTEM PROMPT — This is what makes CeeCee good at this
# ============================================================

SYSTEM_PROMPT = """You are CeeCee, an expert at helping people fight parking and traffic tickets. You work for DisputeIt (disputeit.xyz).

Your job: Write a helpful Reddit comment that genuinely helps the person with their specific ticket situation, and naturally mentions DisputeIt at the end.

RULES:
1. Be genuinely helpful FIRST. Give them real, actionable advice for their specific situation.
2. Reference their specific details — city, violation type, what happened.
3. Identify the strongest defense for their situation (wrong info on ticket, signage issues, meter malfunction, procedural errors, etc.)
4. Tell them exactly what to do — how to dispute, what evidence to gather.
5. At the END (not the beginning), naturally mention DisputeIt as an option if they don't want to write the letter themselves.
6. Keep it conversational — you're a helpful person on Reddit, not a salesperson.
7. Never be pushy. One mention of DisputeIt at the end, max.
8. Keep it under 200 words. Reddit hates walls of text.

PRICING TO MENTION:
- Parking tickets: $15 ($5 submission + $10 dispute fee)
- Traffic tickets: $25 ($5 submission + $20 dispute fee)
- Refund if no valid dispute found (minus $5 submission)

EXAMPLE TONE:
"That sucks but you've got a solid shot at getting this dismissed. Here's what I'd do..."

DO NOT:
- Start with "Hey!" or "Hi there!" (sounds fake)
- Use corporate language
- Make it sound like an ad
- Guarantee results
- Give legal advice (say "I'm not a lawyer but...")
"""

# ============================================================
# EMAIL CHECKER — Reads F5Bot alerts from Gmail
# ============================================================

def get_processed_ids():
    """Load list of already-processed email IDs."""
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, 'r') as f:
            return set(json.load(f))
    return set()

def save_processed_ids(ids):
    """Save processed email IDs."""
    with open(PROCESSED_FILE, 'w') as f:
        json.dump(list(ids), f)

def check_email():
    """Check Gmail for new F5Bot alerts. Returns list of Reddit URLs + post content."""
    alerts = []
    processed = get_processed_ids()

    try:
        mail = imaplib.IMAP4_SSL(EMAIL_CONFIG['imap_server'])
        mail.login(EMAIL_CONFIG['email'], EMAIL_CONFIG['password'])
        mail.select('INBOX')

        # Search for F5Bot emails
        status, messages = mail.search(None, '(FROM "f5bot" UNSEEN)')
        if status != 'OK' or not messages[0]:
            mail.logout()
            return alerts

        for msg_id in messages[0].split():
            msg_id_str = msg_id.decode()
            if msg_id_str in processed:
                continue

            status, msg_data = mail.fetch(msg_id, '(RFC822)')
            if status != 'OK':
                continue

            msg = email.message_from_bytes(msg_data[0][1])

            # Extract body
            body = ''
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == 'text/plain':
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
                    elif part.get_content_type() == 'text/html':
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
            else:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')

            # Extract Reddit URLs from the email
            reddit_urls = re.findall(r'https?://(?:www\.)?reddit\.com/r/\S+', body)
            # Also try old.reddit.com links
            reddit_urls += re.findall(r'https?://old\.reddit\.com/r/\S+', body)

            for url in reddit_urls:
                # Clean URL
                url = url.rstrip('>')
                url = url.split('"')[0]
                url = url.split("'")[0]
                alerts.append({
                    'url': url,
                    'email_body': body[:500],  # First 500 chars for context
                    'msg_id': msg_id_str,
                })

            processed.add(msg_id_str)

        mail.logout()
        save_processed_ids(processed)

    except Exception as e:
        print(f"[Email] Error: {e}")

    return alerts

# ============================================================
# REDDIT SCRAPER — Gets the post content
# ============================================================

def scrape_reddit_post(url):
    """Scrape a Reddit post to get title and body text."""
    try:
        # Use Reddit's JSON API (add .json to any Reddit URL)
        json_url = url.rstrip('/') + '.json'
        headers = {'User-Agent': 'DisputeIt-LeadBot/1.0'}
        resp = requests.get(json_url, headers=headers, timeout=10)

        if resp.status_code != 200:
            print(f"[Scraper] Failed to fetch {url}: HTTP {resp.status_code}")
            return None

        data = resp.json()

        # Reddit JSON structure: array of listings
        if not data or not isinstance(data, list):
            return None

        post_data = data[0]['data']['children'][0]['data']

        result = {
            'title': post_data.get('title', ''),
            'body': post_data.get('selftext', ''),
            'subreddit': post_data.get('subreddit', ''),
            'author': post_data.get('author', ''),
            'url': url,
            'score': post_data.get('score', 0),
            'num_comments': post_data.get('num_comments', 0),
            'created': datetime.fromtimestamp(post_data.get('created_utc', 0)).strftime('%Y-%m-%d %H:%M'),
        }

        print(f"[Scraper] Got post: \"{result['title'][:60]}...\" from r/{result['subreddit']}")
        return result

    except Exception as e:
        print(f"[Scraper] Error scraping {url}: {e}")
        return None

# ============================================================
# OLLAMA — Generate custom reply using local AI
# ============================================================

def generate_reply(post_data):
    """Use local Ollama model to generate a custom reply."""
    try:
        prompt = f"""Here is a Reddit post from r/{post_data['subreddit']} where someone needs help with a ticket:

TITLE: {post_data['title']}

POST: {post_data['body'][:1000]}

Write a helpful Reddit comment reply for this specific person and their specific situation. Follow the rules in your system prompt."""

        resp = requests.post(
            f"{OLLAMA_CONFIG['url']}/api/chat",
            json={
                'model': OLLAMA_CONFIG['model'],
                'messages': [
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user', 'content': prompt}
                ],
                'stream': False
            },
            timeout=120
        )

        if resp.status_code != 200:
            print(f"[Ollama] Error: HTTP {resp.status_code}")
            return None

        data = resp.json()
        reply = data.get('message', {}).get('content', '')

        if reply:
            print(f"[Ollama] Generated reply ({len(reply)} chars)")
            return reply
        return None

    except requests.exceptions.ConnectionError:
        print("[Ollama] Can't connect — is Ollama running? Start it with: ollama serve")
        return None
    except Exception as e:
        print(f"[Ollama] Error: {e}")
        return None

# ============================================================
# OUTPUT — Save ready-to-post replies
# ============================================================

def save_reply(post_data, reply):
    """Append a ready-to-post reply to the output file."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    entry = f"""
---

## [{post_data['title'][:80]}]({post_data['url']})
**Subreddit:** r/{post_data['subreddit']} | **Posted:** {post_data['created']} | **Score:** {post_data['score']} | **Comments:** {post_data['num_comments']}

**Their post:** {post_data['body'][:300]}{'...' if len(post_data['body']) > 300 else ''}

**👆 Click the title link above, then paste this reply:**

```
{reply}
```

*Generated: {timestamp}*

"""

    # Create file with header if it doesn't exist
    if not os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, 'w') as f:
            f.write("# READY TO POST — DisputeIt Lead Replies\n")
            f.write("## Click each link → Paste the reply → Done\n")
            f.write(f"*Last updated: {timestamp}*\n")

    with open(OUTPUT_FILE, 'a') as f:
        f.write(entry)

    print(f"[Output] Saved reply for: {post_data['title'][:50]}...")

# ============================================================
# MAIN LOOP
# ============================================================

def run_cycle():
    """Run one check cycle."""
    print(f"\n[CeeCee] Checking for new leads... {datetime.now().strftime('%H:%M:%S')}")

    # Check email for F5Bot alerts
    alerts = check_email()

    if not alerts:
        print("[CeeCee] No new alerts")
        return 0

    print(f"[CeeCee] Found {len(alerts)} new alerts!")
    replies_generated = 0

    for alert in alerts:
        # Scrape the Reddit post
        post = scrape_reddit_post(alert['url'])
        if not post:
            continue

        # Skip if post is too old or has too many comments (already served)
        if post['num_comments'] > 50:
            print(f"[CeeCee] Skipping (too many comments): {post['title'][:50]}...")
            continue

        # Generate custom reply
        reply = generate_reply(post)
        if not reply:
            continue

        # Save to ready-to-post file
        save_reply(post, reply)
        replies_generated += 1

        # Small delay between requests to be respectful
        time.sleep(2)

    return replies_generated

def main():
    """Main entry point — runs continuously."""
    print("=" * 60)
    print("  CEECEE LEAD RESPONDER")
    print("  Monitoring F5Bot alerts → Generating custom replies")
    print("=" * 60)
    print(f"  Email: {EMAIL_CONFIG['email']}")
    print(f"  Model: {OLLAMA_CONFIG['model']}")
    print(f"  Output: {OUTPUT_FILE}")
    print(f"  Check interval: {CHECK_INTERVAL}s")
    print("=" * 60)

    # Verify Ollama is running
    try:
        resp = requests.get(f"{OLLAMA_CONFIG['url']}/api/tags", timeout=5)
        if resp.status_code == 200:
            models = [m['name'] for m in resp.json().get('models', [])]
            print(f"  Ollama: Connected ({len(models)} models)")
            if OLLAMA_CONFIG['model'] not in [m.split(':')[0] for m in models]:
                print(f"  WARNING: Model '{OLLAMA_CONFIG['model']}' not found. Pull it: ollama pull {OLLAMA_CONFIG['model']}")
        else:
            print("  Ollama: Connected but couldn't list models")
    except:
        print("  Ollama: NOT RUNNING — start with: ollama serve")
        print("  Script will still check emails and scrape posts, but can't generate replies without Ollama.")

    print("\n[CeeCee] Starting monitoring loop. Press Ctrl+C to stop.\n")

    total_replies = 0
    while True:
        try:
            count = run_cycle()
            total_replies += count
            if count > 0:
                print(f"[CeeCee] {count} new replies ready! Total: {total_replies}")
                print(f"[CeeCee] Open {OUTPUT_FILE} to see them.")
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            print(f"\n[CeeCee] Stopped. Total replies generated: {total_replies}")
            break
        except Exception as e:
            print(f"[CeeCee] Error in main loop: {e}")
            time.sleep(60)  # Wait a minute before retrying

if __name__ == '__main__':
    main()
