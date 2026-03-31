#!/usr/bin/env python3
"""Standalone IMHentai downloader.

This utility mirrors the extraction logic used in modules/lua/IMHentai.lua,
allowing gallery downloads without running the HDoujin Downloader GUI.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) HDoujinLuaCompat/1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download galleries from imhentai.xxx")
    parser.add_argument(
        "url",
        help=(
            "Gallery URL or listing/tag URL "
            "(e.g. https://imhentai.xxx/gallery/123456/ or https://imhentai.xxx/tag/agata/)"
        ),
    )
    parser.add_argument("-o", "--output", default="downloads", help="Output root directory")
    parser.add_argument("--delay", type=float, default=0.0, help="Delay in seconds between image downloads")
    parser.add_argument("--metadata-only", action="store_true", help="Only fetch metadata; skip image downloads")
    parser.add_argument(
        "--required-tag",
        action="append",
        default=[],
        help="Only keep galleries containing this tag (repeatable, case-insensitive)",
    )
    parser.add_argument(
        "--required-language",
        action="append",
        default=[],
        help="Only keep galleries containing this language (repeatable, case-insensitive)",
    )
    parser.add_argument("--max-pages", type=int, default=10, help="Max listing pages to crawl for tag/list URLs")
    parser.add_argument("--zip-after-download", action="store_true", help="Zip each downloaded gallery folder")
    return parser.parse_args()


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def absolute(base: str, maybe_relative: str) -> str:
    return urllib.parse.urljoin(base, maybe_relative)


def text_between(html_src: str, pattern: str) -> str:
    m = re.search(pattern, html_src, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    value = re.sub(r"<[^>]+>", "", m.group(1))
    return html.unescape(value).strip()


def extract_title(html_src: str) -> str:
    return text_between(html_src, r"<h1[^>]*>(.*?)</h1>")


def extract_group_links(html_src: str, group_name: str) -> list[str]:
    # Matches both inline links and block links used by IMHentai variants.
    block = re.search(
        rf"<span[^>]*>\s*{re.escape(group_name)}\s*</span>(.*?)(?:</li>|<span[^>]*>\s*[A-Za-z ]+\s*</span>)",
        html_src,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not block:
        return []
    return [
        html.unescape(re.sub(r"<[^>]+>", "", m)).strip()
        for m in re.findall(r"<a[^>]*>(.*?)</a>", block.group(1), flags=re.IGNORECASE | re.DOTALL)
        if re.sub(r"<[^>]+>", "", m).strip()
    ]


def extract_input_value(html_src: str, input_id: str) -> str:
    m = re.search(
        rf'<input[^>]*id=["\']{re.escape(input_id)}["\'][^>]*value=["\']([^"\']+)',
        html_src,
        flags=re.IGNORECASE,
    )
    return m.group(1).strip() if m else ""


def extract_parsejson_blob(html_src: str) -> str:
    m = re.search(r"g_th\s*=\s*\$\.parseJSON\('(.+?)'\)", html_src, flags=re.DOTALL)
    return m.group(1) if m else ""


def decode_images_json(raw_blob: str) -> dict[str, list]:
    if not raw_blob:
        return {}
    # JS string escaping -> JSON string.
    unescaped = raw_blob.encode("utf-8").decode("unicode_escape")
    return json.loads(unescaped)


def extension_from_key(key: str) -> str:
    return {
        "j": ".jpg",
        "p": ".png",
        "b": ".bmp",
        "g": ".gif",
        "w": ".webp",
    }.get(key, ".jpg")


def download_binary(url: str, referer: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": referer})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def candidate_extensions(primary_ext: str) -> list[str]:
    ordered = [primary_ext, ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"]
    deduped: list[str] = []
    for ext in ordered:
        if ext not in deduped:
            deduped.append(ext)
    return deduped


def image_server_from_gallery_id(gallery_id: int) -> str:
    if 0 < gallery_id <= 274825:
        return "m1"
    if gallery_id <= 403818:
        return "m2"
    if gallery_id <= 527143:
        return "m3"
    if gallery_id <= 632481:
        return "m4"
    if gallery_id <= 816010:
        return "m5"
    if gallery_id <= 970098:
        return "m6"
    if gallery_id <= 1121113:
        return "m7"
    if gallery_id <= 1259410:
        return "m8"
    if gallery_id <= 1439024:
        return "m9"
    return "m10"


def sanitize_filename(name: str) -> str:
    sanitized = re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .")
    return sanitized or "gallery"


def parse_metadata(url: str, html_src: str) -> dict:
    meta_description = text_between(html_src, r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']')
    og_description = text_between(html_src, r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']')

    page_count = ""
    m = re.search(r"<span[^>]*>\s*Pages\s*</span>\s*<a[^>]*>(\d+)", html_src, flags=re.IGNORECASE)
    if m:
        page_count = m.group(1)

    return {
        "url": url,
        "title": extract_title(html_src),
        "original_title": text_between(html_src, r'<p[^>]*class=["\'][^"\']*subtitle[^"\']*["\'][^>]*>(.*?)</p>'),
        "parody": extract_group_links(html_src, "Parodies"),
        "characters": extract_group_links(html_src, "Characters"),
        "tags": extract_group_links(html_src, "Tags"),
        "artist": extract_group_links(html_src, "Artists"),
        "circle": extract_group_links(html_src, "Groups"),
        "language": extract_group_links(html_src, "Languages"),
        "type": extract_group_links(html_src, "Category"),
        "page_count": page_count,
        "date_released": text_between(html_src, r"<span[^>]*>\s*Uploaded\s*</span>\s*<span[^>]*>(.*?)</span>"),
        "summary": meta_description or og_description,
    }


def get_gallery_page(url: str, html_src: str) -> tuple[str, str]:
    if "/view/" not in url:
        return url, html_src
    m = re.search(r'<a[^>]*class=["\'][^"\']*return_btn[^"\']*["\'][^>]*href=["\']([^"\']+)', html_src)
    if not m:
        return url, html_src
    gallery_url = absolute(url, m.group(1))
    return gallery_url, fetch(gallery_url)


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
    m = re.search(
        r'<a[^>]*class=["\'][^"\']*page-link[^"\']*["\'][^>]*href=["\']([^"\']+)["\'][^>]*>\s*Next\s*</a>',
        html_src,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return absolute(page_url, m.group(1)) if m else ""


def crawl_listing_urls(start_url: str, max_pages: int) -> list[str]:
    urls: list[str] = []
    page_url = start_url
    pages_seen = 0
    visited_pages: set[str] = set()

    while page_url and pages_seen < max_pages and page_url not in visited_pages:
        visited_pages.add(page_url)
        page_html = fetch(page_url)
        urls.extend(extract_gallery_urls(page_url, page_html))
        page_url = extract_next_page_url(page_url, page_html)
        pages_seen += 1

    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def matches_filters(metadata: dict, required_tags: list[str], required_languages: list[str]) -> bool:
    tag_values = {t.lower() for t in metadata.get("tags", [])}
    language_values = {t.lower() for t in metadata.get("language", [])}
    required_tags_normalized = [t.lower() for t in required_tags]
    required_languages_normalized = [t.lower() for t in required_languages]

    if required_tags_normalized and not all(tag in tag_values for tag in required_tags_normalized):
        return False
    if required_languages_normalized and not all(lang in language_values for lang in required_languages_normalized):
        return False
    return True


def download_gallery(url: str, args: argparse.Namespace) -> tuple[bool, str]:
    page = fetch(url)
    url, page = get_gallery_page(url, page)

    metadata = parse_metadata(url, page)
    if not matches_filters(metadata, args.required_tag, args.required_language):
        return False, f"Skipped (filter mismatch): {url}"

    load_dir = extract_input_value(page, "load_dir")
    load_id = extract_input_value(page, "load_id")
    gallery_id = extract_input_value(page, "gallery_id")

    reader_url_match = re.search(r'<div[^>]*class=["\'][^"\']*gthumb[^"\']*["\'][\s\S]*?<a[^>]*href=["\']([^"\']+)', page)
    if reader_url_match:
        reader_url = absolute(url, reader_url_match.group(1))
        try:
            reader_page = fetch(reader_url)
            load_dir = extract_input_value(reader_page, "image_dir") or load_dir
            load_id = extract_input_value(reader_page, "gallery_id") or load_id
            gallery_id = extract_input_value(reader_page, "u_id") or gallery_id
            page = reader_page
        except urllib.error.URLError:
            pass

    try:
        gallery_id_int = int(gallery_id)
    except ValueError:
        return False, f"Unable to determine gallery ID: {url}"

    images = decode_images_json(extract_parsejson_blob(page))
    if not images:
        return False, f"Unable to parse gallery image manifest: {url}"

    domain = urllib.parse.urlparse(url).netloc
    image_server = image_server_from_gallery_id(gallery_id_int)

    gallery_dir = Path(args.output) / sanitize_filename(metadata["title"])
    image_dir = gallery_dir / "images"
    gallery_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)
    (gallery_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.metadata_only:
        return True, f"Saved metadata: {gallery_dir / 'metadata.json'}"

    total = len(images)
    failure_count = 0
    for idx, key in enumerate(images.keys(), start=1):
        kind = str(images[key][0]) if isinstance(images[key], list) and images[key] else "j"
        selected_ext = extension_from_key(kind)
        downloaded = False

        for ext in candidate_extensions(selected_ext):
            filename = f"{key}{ext}"
            image_url = f"https://{image_server}.{domain}/{load_dir}/{load_id}/{filename}"
            out_path = image_dir / filename

            try:
                out_path.write_bytes(download_binary(image_url, url))
                print(f"[{idx}/{total}] downloaded {filename}")
                downloaded = True
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    print(f"[{idx}/{total}] failed {filename}: {exc}", file=sys.stderr)
                    break
            except urllib.error.URLError as exc:
                print(f"[{idx}/{total}] failed {filename}: {exc}", file=sys.stderr)
                break

        if not downloaded:
            failure_count += 1
            print(f"[{idx}/{total}] failed {key} (all extension fallbacks returned 404)", file=sys.stderr)

        if args.delay > 0:
            time.sleep(args.delay)

    if args.zip_after_download:
        archive_path = shutil.make_archive(str(gallery_dir), "zip", root_dir=gallery_dir)
        return True, f"Downloaded + archived: {archive_path} (failed files: {failure_count})"

    return True, f"Downloaded: {gallery_dir} (failed files: {failure_count})"


def main() -> int:
    args = parse_args()
    url = args.url.strip()

    try:
        page = fetch(url)
    except urllib.error.URLError as exc:
        print(f"Failed to fetch page: {exc}", file=sys.stderr)
        return 1

    target_urls: list[str]
    if "/gallery/" in url or "/view/" in url:
        target_urls = [url]
    else:
        target_urls = crawl_listing_urls(url, max_pages=max(1, args.max_pages))

    if not target_urls:
        print("No gallery URLs found from the provided input URL.", file=sys.stderr)
        return 1

    success_count = 0
    for target_url in target_urls:
        try:
            ok, message = download_gallery(target_url, args)
            print(message)
            if ok:
                success_count += 1
        except urllib.error.URLError as exc:
            print(f"Failed to fetch gallery {target_url}: {exc}", file=sys.stderr)

    print(f"Completed. {success_count}/{len(target_urls)} galleries matched and processed.")
    if success_count == 0:
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
