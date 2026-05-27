"""Daily monitor for new job postings on slo-tech.com/delo.

Pipeline: fetch RSS -> parse -> diff against state.json -> email digest.

Run `python monitor.py --dry-run` locally to see what would be sent without
hitting SMTP or touching state.json.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path

import feedparser
import requests

RSS_URL = "https://slo-tech.com/delo/rss"
STATE_PATH = Path(__file__).parent / "state.json"
MAX_STATE_SIZE = 200
FEED_ENCODING = "iso-8859-2"
HTTP_TIMEOUT = 30
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SNIPPET_LEN = 280


@dataclass(frozen=True)
class Posting:
    id: int
    title: str
    link: str
    company: str
    pub_date: str
    description_html: str
    description_snippet: str


# ---------- pure functions ----------


def extract_id(link: str) -> int | None:
    match = re.search(r"/delo/(\d+)/?$", link or "")
    return int(match.group(1)) if match else None


class _TagStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_snippet(raw_html: str, max_len: int = SNIPPET_LEN) -> str:
    if not raw_html:
        return ""
    stripper = _TagStripper()
    try:
        stripper.feed(raw_html)
    except Exception:
        # Malformed HTML — fall back to unescape + naive tag strip.
        text = re.sub(r"<[^>]+>", " ", raw_html)
        text = html.unescape(text)
    else:
        text = stripper.text()
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def fetch_feed(url: str = RSS_URL, timeout: int = HTTP_TIMEOUT) -> bytes:
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "slo-tech-delo-monitor/1.0"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc
    if not resp.content:
        raise RuntimeError(f"Feed at {url} returned empty body")
    return resp.content


def parse_feed(raw: bytes) -> list[Posting]:
    # The feed declares ISO-8859-2 but Content-Type can be inconsistent.
    # Decode bytes explicitly, then hand UTF-8 to feedparser with the
    # XML declaration rewritten so it doesn't try to redecode wrong.
    try:
        text = raw.decode(FEED_ENCODING)
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Cannot decode feed as {FEED_ENCODING}: {exc}") from exc
    text = re.sub(r'encoding="[^"]+"', 'encoding="utf-8"', text, count=1)

    parsed = feedparser.parse(text.encode("utf-8"))
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"Feed XML is malformed: {parsed.bozo_exception!r}")

    postings: list[Posting] = []
    for entry in parsed.entries:
        link = getattr(entry, "link", "") or ""
        pid = extract_id(link)
        if pid is None:
            print(f"[warn] skipping entry with no extractable ID: {link!r}", file=sys.stderr)
            continue
        description_html = getattr(entry, "description", "") or getattr(entry, "summary", "") or ""
        postings.append(
            Posting(
                id=pid,
                title=(getattr(entry, "title", "") or "").strip(),
                link=link,
                company=(getattr(entry, "author", "") or "").strip(),
                pub_date=(getattr(entry, "published", "") or "").strip(),
                description_html=description_html,
                description_snippet=html_to_snippet(description_html),
            )
        )
    return postings


def diff_new(postings: list[Posting], seen: set[int]) -> list[Posting]:
    return [p for p in postings if p.id not in seen]


def _html_escape(s: str) -> str:
    return html.escape(s or "", quote=True)


def format_digest(new_postings: list[Posting]) -> tuple[str, str, str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = len(new_postings)
    subject = f"slo-tech/delo: {n} new posting{'s' if n != 1 else ''} — {today}"

    text_parts: list[str] = [f"{n} new job posting{'s' if n != 1 else ''} on slo-tech.com/delo\n"]
    html_parts: list[str] = [
        "<html><body style=\"font-family: -apple-system, Segoe UI, sans-serif; max-width: 720px;\">",
        f"<p><strong>{n}</strong> new job posting{'s' if n != 1 else ''} on "
        "<a href=\"https://slo-tech.com/delo\">slo-tech.com/delo</a></p>",
        "<hr>",
    ]

    for p in new_postings:
        company_line = f"Company: {p.company}" if p.company else "Company: (unknown)"
        date_line = f"Date: {p.pub_date}" if p.pub_date else ""

        text_parts.append("")
        text_parts.append(p.title)
        text_parts.append(company_line)
        if date_line:
            text_parts.append(date_line)
        text_parts.append(f"Link: {p.link}")
        if p.description_snippet:
            text_parts.append("")
            text_parts.append(p.description_snippet)
        text_parts.append("")
        text_parts.append("---")

        html_parts.append(f"<h2 style=\"margin-bottom: 4px;\">{_html_escape(p.title)}</h2>")
        html_parts.append(
            f"<p style=\"margin: 2px 0; color: #555;\">{_html_escape(company_line)}</p>"
        )
        if date_line:
            html_parts.append(
                f"<p style=\"margin: 2px 0; color: #555;\">{_html_escape(date_line)}</p>"
            )
        html_parts.append(
            f"<p style=\"margin: 2px 0;\"><a href=\"{_html_escape(p.link)}\">{_html_escape(p.link)}</a></p>"
        )
        if p.description_snippet:
            html_parts.append(f"<p>{_html_escape(p.description_snippet)}</p>")
        html_parts.append("<hr>")

    html_parts.append("</body></html>")
    return subject, "\n".join(text_parts).strip() + "\n", "".join(html_parts)


def format_seed(total: int) -> tuple[str, str, str]:
    subject = "slo-tech/delo monitor initialized"
    text = (
        f"Monitor initialized, tracking {total} posting{'s' if total != 1 else ''}.\n\n"
        "You will receive an email when new postings appear on "
        "https://slo-tech.com/delo .\n"
    )
    html_body = (
        "<html><body style=\"font-family: -apple-system, Segoe UI, sans-serif;\">"
        f"<p>Monitor initialized, tracking <strong>{total}</strong> "
        f"posting{'s' if total != 1 else ''}.</p>"
        "<p>You will receive an email when new postings appear on "
        "<a href=\"https://slo-tech.com/delo\">slo-tech.com/delo</a>.</p>"
        "</body></html>"
    )
    return subject, text, html_body


def merge_state(current_ids: list[int], existing_seen: list[int], max_size: int = MAX_STATE_SIZE) -> list[int]:
    current_set = set(current_ids)
    merged = list(current_ids) + [x for x in existing_seen if x not in current_set]
    return merged[:max_size]


# ---------- impure boundary ----------


def load_state(path: Path = STATE_PATH) -> list[int]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list) or not all(isinstance(x, int) for x in data):
        raise RuntimeError(f"{path} must be a JSON array of integers")
    return data


def save_state(path: Path, ids: list[int]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(ids, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def send_email(
    subject: str,
    body_text: str,
    body_html: str,
    gmail_user: str,
    gmail_app_password: str,
    to: str,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to
    msg.set_content(body_text)
    msg.add_alternative(body_html, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=HTTP_TIMEOUT) as smtp:
        smtp.starttls()
        smtp.login(gmail_user, gmail_app_password)
        smtp.send_message(msg)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="slo-tech/delo daily monitor")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be sent; do not email or persist state.")
    args = parser.parse_args(argv)

    gmail_user = gmail_pass = notify_to = None
    if not args.dry_run:
        gmail_user = os.environ.get("GMAIL_USER")
        gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
        notify_to = os.environ.get("NOTIFY_TO")
        missing = [n for n, v in [("GMAIL_USER", gmail_user), ("GMAIL_APP_PASSWORD", gmail_pass), ("NOTIFY_TO", notify_to)] if not v]
        if missing:
            print(f"Missing required env vars: {', '.join(missing)}", file=sys.stderr)
            return 1

    try:
        raw = fetch_feed()
        postings = parse_feed(raw)
    except RuntimeError as exc:
        print(f"Feed error: {exc}", file=sys.stderr)
        return 1

    print(f"Fetched {len(postings)} entries.", file=sys.stderr)

    seen = load_state(STATE_PATH)
    is_first_run = not seen

    if is_first_run:
        subject, text, html_body = format_seed(len(postings))
    else:
        new_postings = diff_new(postings, set(seen))
        if not new_postings:
            print("No new postings.", file=sys.stderr)
            return 0
        subject, text, html_body = format_digest(new_postings)

    if args.dry_run:
        print(f"--- SUBJECT ---\n{subject}\n\n--- TEXT BODY ---\n{text}\n\n--- HTML BODY ---\n{html_body}")
        return 0

    try:
        send_email(subject, text, html_body, gmail_user, gmail_pass, notify_to)
    except (smtplib.SMTPException, OSError) as exc:
        print(f"Email send failed: {exc}", file=sys.stderr)
        return 1

    merged = merge_state([p.id for p in postings], seen)
    save_state(STATE_PATH, merged)
    print(f"State updated ({len(merged)} IDs tracked).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
