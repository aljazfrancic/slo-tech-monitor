"""Monitor for new job postings on slo-tech.com/delo.

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
import time
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
HTTP_RETRIES = 2
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
    description_snippet: str


# ---------- pure functions ----------


def extract_id(link: str) -> int | None:
    # Anchor to slo-tech.com so unrelated RSS links containing "/delo/<n>"
    # can't masquerade as postings. \b after the digits stops at /, ?, #, .,
    # -, end of string; rejects /delo/7717abc which is a different path.
    # Reject ID 0 — load_state validates positive ints, so a 0 from a junk
    # entry would round-trip into state.json and crash the next run.
    match = re.match(r"https?://slo-tech\.com/delo/(\d+)\b", link or "")
    if not match:
        return None
    pid = int(match.group(1))
    return pid if pid > 0 else None


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


class _TagStripper(HTMLParser):
    # Drop the inner text of these tags — it's code, not content.
    _SKIP_TAGS = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    # Inject whitespace at every tag boundary so adjacent tags
    # (e.g. <li>A</li><li>B</li>) don't get concatenated into "AB".
    # _clean_text collapses the resulting runs back down.
    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        self._chunks.append(" ")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self._chunks.append(" ")

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_snippet(raw_html: str, max_len: int = SNIPPET_LEN) -> str:
    if not raw_html:
        return ""
    stripper = _TagStripper()
    try:
        stripper.feed(raw_html)
        stripper.close()
    except Exception:
        # Malformed HTML — fall back to unescape + naive tag strip.
        text = re.sub(r"<[^>]+>", " ", raw_html)
        text = html.unescape(text)
    else:
        text = stripper.text()
    text = _clean_text(text)
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def fetch_feed(
    url: str = RSS_URL, timeout: int = HTTP_TIMEOUT, retries: int = HTTP_RETRIES
) -> tuple[bytes, str | None]:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "slo-tech-delo-monitor/1.0"})
            resp.raise_for_status()
        except requests.HTTPError as exc:
            # 4xx is usually a client error (bad URL, auth) and retrying won't
            # help. 408 (timeout), 425 (too early), and 429 (rate limit) are
            # transient and worth another attempt.
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500 and status not in (408, 425, 429):
                raise RuntimeError(f"Feed at {url} returned HTTP {status}") from exc
            last_exc = exc
        except requests.RequestException as exc:
            last_exc = exc
        else:
            if resp.content:
                return resp.content, _http_charset(resp.headers.get("Content-Type", ""))
            last_exc = RuntimeError(f"Feed at {url} returned empty body")
        if attempt < retries:
            time.sleep(2**attempt)
    raise RuntimeError(f"Failed to fetch {url} after {retries + 1} attempts: {last_exc}") from last_exc


def _http_charset(content_type: str) -> str | None:
    # Content-Type charset may be quoted per RFC 7231.
    match = re.search(r"""charset\s*=\s*["']?([\w.-]+)""", content_type, re.IGNORECASE)
    return match.group(1) if match else None


def _detect_encoding(
    raw: bytes, http_charset: str | None = None, default: str = FEED_ENCODING
) -> str:
    # XML declarations are always ASCII-compatible, so this prefix decode is safe.
    # Anchor to <?xml ... ?> so an `encoding=` attribute elsewhere in the head
    # (e.g. xmlns:encoding="...") can't be misread as the document encoding.
    head = raw[:512].decode("ascii", errors="replace")
    match = re.search(r"""<\?xml[^?]*encoding\s*=\s*["']([^"']+)["']""", head)
    if match:
        return match.group(1)
    if http_charset:
        return http_charset
    return default


def parse_feed(raw: bytes, http_charset: str | None = None) -> list[Posting]:
    # The feed declares ISO-8859-2 but Content-Type can be inconsistent.
    # Prefer the XML declaration; fall back to the HTTP Content-Type charset
    # if the declaration is absent, then to FEED_ENCODING. Then hand UTF-8
    # to feedparser with the XML declaration rewritten so it doesn't try to
    # redecode wrong.
    encoding = _detect_encoding(raw, http_charset)
    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise RuntimeError(f"Cannot decode feed as {encoding}: {exc}") from exc
    # Only rewrite the encoding inside the XML declaration itself.
    text = re.sub(
        r"""(<\?xml[^?]*encoding\s*=\s*)["'][^"']+["']""",
        r'\1"utf-8"',
        text,
        count=1,
    )

    parsed = feedparser.parse(text.encode("utf-8"))
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"Feed XML is malformed: {parsed.bozo_exception!r}")

    postings: list[Posting] = []
    seen_ids: set[int] = set()
    for entry in parsed.entries:
        link = getattr(entry, "link", "") or ""
        pid = extract_id(link)
        if pid is None:
            print(f"[warn] skipping entry with no extractable ID: {link!r}", file=sys.stderr)
            continue
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        description_html = getattr(entry, "description", "") or getattr(entry, "summary", "") or ""
        postings.append(
            Posting(
                id=pid,
                title=_clean_text(getattr(entry, "title", "") or ""),
                link=link,
                company=_clean_text(getattr(entry, "author", "") or ""),
                pub_date=_clean_text(getattr(entry, "published", "") or ""),
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
    # Sort descending so the cap deterministically keeps the highest (newest)
    # IDs regardless of the order feedparser hands entries to us.
    merged = sorted({*current_ids, *existing_seen}, reverse=True)
    return merged[:max_size]


# ---------- impure boundary ----------


def load_state(path: Path = STATE_PATH) -> list[int] | None:
    # Returns None when no state has been recorded yet (file missing or
    # empty). An explicit `[]` in the file means "initialized but currently
    # tracking nothing" and returns []. Callers use the None vs [] distinction
    # to detect a true first run.
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    data = json.loads(raw)
    if not isinstance(data, list) or not all(
        isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in data
    ):
        raise RuntimeError(f"{path} must be a JSON array of positive integers")
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
    parser = argparse.ArgumentParser(description="slo-tech/delo monitor")
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
        raw, http_charset = fetch_feed()
        postings = parse_feed(raw, http_charset)
    except RuntimeError as exc:
        print(f"Feed error: {exc}", file=sys.stderr)
        return 1

    print(f"Fetched {len(postings)} entries.", file=sys.stderr)

    try:
        seen = load_state(STATE_PATH)
    except (RuntimeError, ValueError) as exc:
        print(f"State error: {exc}", file=sys.stderr)
        return 1
    is_first_run = seen is None

    if is_first_run:
        if not postings:
            print(
                "First run but feed returned no entries — refusing to seed with empty state.",
                file=sys.stderr,
            )
            return 1
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

    # State is written AFTER email send: if save_state fails we'll re-send the
    # digest tomorrow, which is preferable to writing state first and silently
    # dropping postings if email later fails.
    merged = merge_state([p.id for p in postings], seen or [])
    try:
        save_state(STATE_PATH, merged)
    except OSError as exc:
        print(f"State save failed: {exc}", file=sys.stderr)
        return 1
    print(f"State updated ({len(merged)} IDs tracked).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
