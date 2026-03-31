#!/usr/bin/env python3
"""Build and query a searchable IMHentai tag database.

The crawler walks listing pages, visits gallery pages, stores gallery metadata,
all tags, and optional thumbnails in an SQLite database.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) HDoujinTagIndexer/1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl IMHentai and build/search a tag index database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    crawl_parser = subparsers.add_parser("crawl", help="Crawl listing pages and build/update tag index")
    crawl_parser.add_argument(
        "start_url",
        help="Listing URL to start from (e.g. https://imhentai.xxx/new/ or https://imhentai.xxx/tag/agata/)",
    )
    crawl_parser.add_argument("--db", default="downloads/imhentai_index.sqlite3", help="SQLite DB path")
    crawl_parser.add_argument("--thumbnail-dir", default="downloads/index_thumbnails", help="Thumbnail output directory")
    crawl_parser.add_argument("--max-listing-pages", type=int, default=200, help="Maximum listing pages to crawl")
    crawl_parser.add_argument("--delay", type=float, default=0.0, help="Delay between requests in seconds")
    crawl_parser.add_argument("--skip-thumbnails", action="store_true", help="Do not download thumbnail files")

    search_parser = subparsers.add_parser("search", help="Search indexed galleries by tags")
    search_parser.add_argument("terms", nargs="+", help="Tag terms to AND together (case-insensitive)")
    search_parser.add_argument("--db", default="downloads/imhentai_index.sqlite3", help="SQLite DB path")
    search_parser.add_argument("--limit", type=int, default=100, help="Maximum rows to print")

    export_parser = subparsers.add_parser("export-json", help="Export whole index to JSON")
    export_parser.add_argument("--db", default="downloads/imhentai_index.sqlite3", help="SQLite DB path")
    export_parser.add_argument("--out", default="downloads/imhentai_index.json", help="Output JSON path")

    links_parser = subparsers.add_parser("export-links", help="Export matching gallery URLs for external tools")
    links_parser.add_argument("terms", nargs="+", help="Search terms to AND together")
    links_parser.add_argument("--db", default="downloads/imhentai_index.sqlite3", help="SQLite DB path")
    links_parser.add_argument("--out", default="downloads/matching_links.txt", help="Output text file path")
    links_parser.add_argument("--limit", type=int, default=5000, help="Max number of links to export")

    serve_parser = subparsers.add_parser("serve", help="Run local searchable web UI with click-through links")
    serve_parser.add_argument("--db", default="downloads/imhentai_index.sqlite3", help="SQLite DB path")
    serve_parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    serve_parser.add_argument("--port", type=int, default=8765, help="HTTP bind port")

    return parser.parse_args()


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def fetch_bytes(url: str, referer: str = "") -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def absolute(base: str, maybe_relative: str) -> str:
    return urllib.parse.urljoin(base, maybe_relative)


def sanitize_filename(name: str) -> str:
    sanitized = re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .")
    return sanitized or "thumbnail"


def text_between(html_src: str, pattern: str) -> str:
    match = re.search(pattern, html_src, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    value = re.sub(r"<[^>]+>", "", match.group(1))
    return html.unescape(value).strip()


def extract_title(html_src: str) -> str:
    return text_between(html_src, r"<h1[^>]*>(.*?)</h1>")


def extract_group_links(html_src: str, group_name: str) -> list[str]:
    block = re.search(
        rf"<span[^>]*>\s*{re.escape(group_name)}\s*</span>(.*?)(?:</li>|<span[^>]*>\s*[A-Za-z ]+\s*</span>)",
        html_src,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not block:
        return []
    values: list[str] = []
    for match in re.findall(r"<a[^>]*>(.*?)</a>", block.group(1), flags=re.IGNORECASE | re.DOTALL):
        cleaned = html.unescape(re.sub(r"<[^>]+>", "", match)).strip()
        if cleaned:
            values.append(cleaned)
    return values


def extract_gallery_urls(page_url: str, html_src: str) -> list[str]:
    gallery_links = re.findall(r'href=["\']([^"\']*?/gallery/\d+/?)["\']', html_src, flags=re.IGNORECASE)
    deduped: list[str] = []
    seen: set[str] = set()
    for link in gallery_links:
        full = absolute(page_url, link)
        if full not in seen:
            seen.add(full)
            deduped.append(full)
    return deduped


def extract_next_page_url(page_url: str, html_src: str) -> str:
    match = re.search(
        r'<a[^>]*class=["\'][^"\']*page-link[^"\']*["\'][^>]*href=["\']([^"\']+)["\'][^>]*>\s*Next\s*</a>',
        html_src,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return absolute(page_url, match.group(1)) if match else ""


def parse_gallery(url: str, html_src: str) -> dict:
    return {
        "url": url,
        "title": extract_title(html_src),
        "tags": extract_group_links(html_src, "Tags"),
        "languages": extract_group_links(html_src, "Languages"),
        "artists": extract_group_links(html_src, "Artists"),
        "characters": extract_group_links(html_src, "Characters"),
        "parodies": extract_group_links(html_src, "Parodies"),
        "groups": extract_group_links(html_src, "Groups"),
        "category": extract_group_links(html_src, "Category"),
        "summary": text_between(html_src, r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']'),
        "thumbnail_url": text_between(html_src, r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](.*?)["\']'),
    }


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS galleries (
            url TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            summary TEXT,
            thumbnail_url TEXT,
            thumbnail_path TEXT,
            searchable_text TEXT,
            raw_metadata_json TEXT NOT NULL,
            crawled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            normalized_name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS gallery_tags (
            gallery_url TEXT NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (gallery_url, tag_id),
            FOREIGN KEY (gallery_url) REFERENCES galleries(url) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tags_normalized_name ON tags(normalized_name);
        CREATE INDEX IF NOT EXISTS idx_gallery_tags_tag_id ON gallery_tags(tag_id);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(galleries)").fetchall()}
    if "searchable_text" not in columns:
        conn.execute("ALTER TABLE galleries ADD COLUMN searchable_text TEXT")
    conn.commit()


def upsert_gallery(conn: sqlite3.Connection, metadata: dict, thumbnail_path: str) -> None:
    searchable_chunks: list[str] = [metadata.get("title", ""), metadata.get("summary", "")]
    for key in ("tags", "languages", "artists", "characters", "parodies", "groups", "category"):
        searchable_chunks.extend(metadata.get(key, []))
    searchable_text = " | ".join(chunk.strip() for chunk in searchable_chunks if chunk and chunk.strip()).lower()

    conn.execute(
        """
        INSERT INTO galleries(url, title, summary, thumbnail_url, thumbnail_path, searchable_text, raw_metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title,
            summary = excluded.summary,
            thumbnail_url = excluded.thumbnail_url,
            thumbnail_path = excluded.thumbnail_path,
            searchable_text = excluded.searchable_text,
            raw_metadata_json = excluded.raw_metadata_json,
            crawled_at = CURRENT_TIMESTAMP
        """,
        (
            metadata["url"],
            metadata["title"] or metadata["url"],
            metadata.get("summary", ""),
            metadata.get("thumbnail_url", ""),
            thumbnail_path,
            searchable_text,
            json.dumps(metadata, ensure_ascii=False),
        ),
    )

    tag_names: set[str] = set()
    for key in ("tags", "languages", "artists", "characters", "parodies", "groups", "category"):
        for value in metadata.get(key, []):
            value = value.strip()
            if value:
                tag_names.add(value)

    conn.execute("DELETE FROM gallery_tags WHERE gallery_url = ?", (metadata["url"],))
    for tag_name in sorted(tag_names):
        normalized = tag_name.lower()
        conn.execute("INSERT OR IGNORE INTO tags(name, normalized_name) VALUES (?, ?)", (tag_name, normalized))
        tag_id = conn.execute("SELECT id FROM tags WHERE normalized_name = ?", (normalized,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO gallery_tags(gallery_url, tag_id) VALUES (?, ?)", (metadata["url"], tag_id))


def maybe_download_thumbnail(url: str, gallery_url: str, title: str, out_dir: Path, skip: bool) -> str:
    if skip or not url:
        return ""

    parsed = urllib.parse.urlparse(url)
    ext = Path(parsed.path).suffix or ".jpg"
    safe_name = sanitize_filename(f"{title}_{hash(gallery_url) & 0xFFFFFFFF:08x}")
    out_path = out_dir / f"{safe_name}{ext}"
    if out_path.exists() and out_path.stat().st_size > 0:
        return str(out_path)

    try:
        data = fetch_bytes(url, referer=gallery_url)
        out_path.write_bytes(data)
        return str(out_path)
    except urllib.error.URLError:
        return ""


def crawl(command: argparse.Namespace) -> int:
    db_path = Path(command.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    thumbnail_dir = Path(command.thumbnail_dir)
    thumbnail_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    ensure_schema(conn)

    start_url = command.start_url.strip()
    listing_url = start_url
    visited_listing_pages: set[str] = set()
    visited_gallery_urls: set[str] = set()

    crawled_count = 0
    page_count = 0

    while listing_url and page_count < max(1, command.max_listing_pages) and listing_url not in visited_listing_pages:
        visited_listing_pages.add(listing_url)
        try:
            listing_html = fetch(listing_url)
        except urllib.error.URLError as exc:
            print(f"Failed listing page {listing_url}: {exc}", file=sys.stderr)
            break

        gallery_urls = extract_gallery_urls(listing_url, listing_html)
        print(f"[listing {page_count + 1}] {listing_url} -> {len(gallery_urls)} galleries")

        for gallery_url in gallery_urls:
            if gallery_url in visited_gallery_urls:
                continue
            visited_gallery_urls.add(gallery_url)

            try:
                gallery_html = fetch(gallery_url)
                metadata = parse_gallery(gallery_url, gallery_html)
                thumb_path = maybe_download_thumbnail(
                    metadata.get("thumbnail_url", ""),
                    gallery_url,
                    metadata.get("title", "gallery"),
                    thumbnail_dir,
                    command.skip_thumbnails,
                )
                upsert_gallery(conn, metadata, thumb_path)
                conn.commit()
                crawled_count += 1
                print(f"  + indexed: {gallery_url}")
            except urllib.error.URLError as exc:
                print(f"  - failed gallery {gallery_url}: {exc}", file=sys.stderr)

            if command.delay > 0:
                time.sleep(command.delay)

        listing_url = extract_next_page_url(listing_url, listing_html)
        page_count += 1
        if command.delay > 0:
            time.sleep(command.delay)

    tag_count = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    gallery_count = conn.execute("SELECT COUNT(*) FROM galleries").fetchone()[0]
    print(f"Completed crawl. Listing pages: {page_count}, galleries indexed this run: {crawled_count}.")
    print(f"Database totals -> galleries: {gallery_count}, tags: {tag_count}")
    conn.close()
    return 0


def search(command: argparse.Namespace) -> int:
    conn = sqlite3.connect(command.db)
    ensure_schema(conn)
    terms = [term.strip().lower() for term in command.terms if term.strip()]
    if not terms:
        print("No search terms provided.", file=sys.stderr)
        return 1

    like_clause = " AND ".join(["g.searchable_text LIKE ?"] * len(terms))
    rows = conn.execute(
        f"""
        SELECT g.url, g.title, g.thumbnail_path
        FROM galleries g
        WHERE {like_clause}
        ORDER BY g.crawled_at DESC
        LIMIT ?
        """,
        tuple(f"%{term}%" for term in terms) + (max(1, command.limit),),
    ).fetchall()

    if not rows:
        print("No matches found.")
        return 0

    for idx, (url, title, thumbnail_path) in enumerate(rows, start=1):
        print(f"{idx}. {title}\n   URL: {url}\n   Thumbnail: {thumbnail_path or '(not downloaded)'}")
    return 0


def export_json(command: argparse.Namespace) -> int:
    conn = sqlite3.connect(command.db)
    ensure_schema(conn)
    galleries = conn.execute(
        "SELECT url, title, summary, thumbnail_url, thumbnail_path, raw_metadata_json FROM galleries ORDER BY crawled_at DESC"
    ).fetchall()

    payload: list[dict] = []
    for url, title, summary, thumbnail_url, thumbnail_path, raw_metadata_json in galleries:
        data = json.loads(raw_metadata_json)
        data.update(
            {
                "url": url,
                "title": title,
                "summary": summary,
                "thumbnail_url": thumbnail_url,
                "thumbnail_path": thumbnail_path,
            }
        )
        payload.append(data)

    out_path = Path(command.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Exported {len(payload)} galleries to {out_path}")
    return 0


def query_matching_urls(conn: sqlite3.Connection, terms: list[str], limit: int) -> list[str]:
    normalized = [term.strip().lower() for term in terms if term.strip()]
    if not normalized:
        return []
    like_clause = " AND ".join(["searchable_text LIKE ?"] * len(normalized))
    rows = conn.execute(
        f"SELECT url FROM galleries WHERE {like_clause} ORDER BY crawled_at DESC LIMIT ?",
        tuple(f"%{term}%" for term in normalized) + (max(1, limit),),
    ).fetchall()
    return [row[0] for row in rows]


def export_links(command: argparse.Namespace) -> int:
    conn = sqlite3.connect(command.db)
    ensure_schema(conn)
    urls = query_matching_urls(conn, command.terms, command.limit)
    out_path = Path(command.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    print(f"Exported {len(urls)} links to {out_path}")
    return 0


def run_web_ui(command: argparse.Namespace) -> int:
    conn = sqlite3.connect(command.db, check_same_thread=False)
    ensure_schema(conn)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"""<!doctype html><html><head><meta charset='utf-8'><title>IMH Index</title>
<style>body{font-family:sans-serif;max-width:1000px;margin:20px auto;padding:0 12px}.row{margin:12px 0;padding:10px;border:1px solid #ddd;border-radius:8px}img{max-width:140px;max-height:180px;display:block}code{background:#f2f2f2;padding:2px 4px}button{margin-left:8px}</style>
</head><body><h1>IMHentai Local Search</h1><p>Use space-separated terms (AND search over all scraped metadata).</p>
<input id='q' style='width:70%' placeholder='agata dog english' /><button onclick='search()'>Search</button><button onclick='copyLinks()'>Copy links</button>
<div id='meta'></div><div id='results'></div>
<script>
let lastUrls=[];
async function search(){const q=document.getElementById('q').value.trim();const r=await fetch('/api/search?q='+encodeURIComponent(q));const data=await r.json();lastUrls=data.results.map(x=>x.url);document.getElementById('meta').innerHTML='<p><b>'+data.results.length+'</b> result(s)</p>';const box=document.getElementById('results');box.innerHTML='';for(const item of data.results){const div=document.createElement('div');div.className='row';div.innerHTML='<div><b>'+item.title+'</b></div><div><a href=\"'+item.url+'\" target=\"_blank\" rel=\"noreferrer\">Open source page</a></div><div><code>'+item.url+'</code></div><div>Thumbnail: '+(item.thumbnail_path||'(not downloaded)')+'</div>';if(item.thumbnail_path){div.innerHTML+='<div><img src=\"/thumb?path='+encodeURIComponent(item.thumbnail_path)+'\"/></div>';}box.appendChild(div);} }
async function copyLinks(){if(!lastUrls.length){alert('Run a search first.');return;}await navigator.clipboard.writeText(lastUrls.join('\\n'));alert('Copied '+lastUrls.length+' link(s).');}
</script></body></html>"""
                )
                return

            if parsed.path == "/api/search":
                params = urllib.parse.parse_qs(parsed.query)
                query = params.get("q", [""])[0].strip().lower()
                terms = [part for part in query.split() if part]
                results = []
                if terms:
                    like_clause = " AND ".join(["searchable_text LIKE ?"] * len(terms))
                    rows = conn.execute(
                        f"SELECT url, title, thumbnail_path FROM galleries WHERE {like_clause} ORDER BY crawled_at DESC LIMIT 200",
                        tuple(f"%{term}%" for term in terms),
                    ).fetchall()
                    for url, title, thumbnail_path in rows:
                        results.append({"url": url, "title": title, "thumbnail_path": thumbnail_path or ""})
                body = json.dumps({"results": results}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/thumb":
                params = urllib.parse.parse_qs(parsed.query)
                thumb_path = params.get("path", [""])[0]
                try:
                    data = Path(thumb_path).read_bytes()
                except OSError:
                    self.send_error(404, "thumbnail not found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_error(404, "not found")

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((command.host, command.port), Handler)
    print(f"Web UI running on http://{command.host}:{command.port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        conn.close()
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "crawl":
        return crawl(args)
    if args.command == "search":
        return search(args)
    if args.command == "export-json":
        return export_json(args)
    if args.command == "export-links":
        return export_links(args)
    if args.command == "serve":
        return run_web_ui(args)
    print(f"Unknown command: {args.command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
