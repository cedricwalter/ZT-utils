# run 
# python3 -m venv .venv\nsource .venv/bin/activate\npip install requests
# python3 rss-to-hugo.py


from datetime import datetime
from email.utils import parsedate_to_datetime
import html
import os
import re
import xml.etree.ElementTree as ET
import requests

RSS_URL = "https://www.meetup.com/zurichtogether/events/rss/"
OUTPUT_DIR = "content/events"

NAMESPACES = {
    "media": "http://yahoo.com",
    "content": "http://purl.org",
}


def slugify(text: str) -> str:
    """Convert event title into a clean, URL-friendly Hugo filename."""
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text or "event"


def escape_yaml(text: str) -> str:
    """Escape double quotes and backslashes for safe YAML front matter."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def extract_rss_images(item: ET.Element, description: str) -> list[str]:
    """Extract all image URLs from RSS tags (<enclosure>, <media:*>, or HTML <img>)."""
    images = []

    # 1. Check <enclosure type="image/..." url="...">
    for enclosure in item.findall("enclosure"):
        enc_type = enclosure.attrib.get("type", "")
        url = enclosure.attrib.get("url", "").strip()
        if url and (
            enc_type.startswith("image/")
            or url.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
        ):
            images.append(url)

    # 2. Check <media:content> and <media:thumbnail>
    for tag in ("media:content", "media:thumbnail"):
        for media in item.findall(tag, NAMESPACES):
            url = media.attrib.get("url", "").strip()
            if url:
                images.append(url)

    # 3. Check inline <img src="..."> inside description or content:encoded
    encoded = (
        item.findtext("content:encoded", default="", namespaces=NAMESPACES) or ""
    )
    combined_html = f"{description}\n{encoded}"
    for match in re.findall(
        r'<img[^>]+src=["\']([^"\']+)["\']', combined_html, flags=re.IGNORECASE
    ):
        images.append(html.unescape(match.strip()))

    # Deduplicate while preserving order
    return list(dict.fromkeys(images))


def fetch_og_image(event_url: str, headers: dict) -> str | None:
    """Fallback: fetch the event cover image (og:image) from the Meetup event page."""
    if not event_url:
        return None
    try:
        resp = requests.get(event_url, headers=headers, timeout=10)
        resp.raise_for_status()
        # Match <meta property="og:image" content="...">
        match = re.search(
            r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            resp.text,
            flags=re.IGNORECASE,
        )
        if not match:
            # Handle reversed attribute order: content="..." property="og:image"
            match = re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
                resp.text,
                flags=re.IGNORECASE,
            )
        if match:
            return html.unescape(match.group(1).strip())
    except Exception as e:
        print(f"  [Warning] Could not fetch page image for {event_url}: {e}")
    return None


def parse_rss_to_hugo(
    rss_content: str,
    output_dir: str,
    headers: dict,
    fetch_fallback_images: bool = True,
):
    os.makedirs(output_dir, exist_ok=True)
    root = ET.fromstring(rss_content)

    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else []

    for item in items:
        title = (item.findtext("title") or "Untitled Event").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        description = (item.findtext("description") or "").strip()

        # Extract images from RSS XML first
        images = extract_rss_images(item, description)

        # If RSS had no images, fetch the cover image from the Meetup event page
        if not images and fetch_fallback_images and link:
            og_img = fetch_og_image(link, headers)
            if og_img:
                images.append(og_img)

        # Convert RFC 2822 pubDate to ISO 8601 format
        try:
            dt = parsedate_to_datetime(pub_date_raw)
            iso_date = dt.isoformat()
            date_prefix = dt.strftime("%Y-%m-%d")
        except Exception:
            iso_date = datetime.now().astimezone().isoformat()
            date_prefix = datetime.now().strftime("%Y-%m-%d")

        event_id_match = re.search(r"/events/(\d+)", guid or link)
        event_id = event_id_match.group(1) if event_id_match else ""

        slug = slugify(title)
        filename = (
            f"{date_prefix}-{event_id}-{slug}.md"
            if event_id
            else f"{date_prefix}-{slug}.md"
        )
        filepath = os.path.join(output_dir, filename)

        # Format images for YAML front matter
        featured_image = images[0] if images else ""
        if images:
            yaml_images = "\n" + "\n".join(
                f'  - "{escape_yaml(img)}"' for img in images
            )
        else:
            yaml_images = " []"

        # Format Markdown image block for the top of the post body
        md_images_block = ""
        if images:
            md_lines = [f"![{title}]({img})" for img in images]
            md_images_block = "\n\n".join(md_lines) + "\n\n"

        md_content = f"""---
title: "{escape_yaml(title)}"
date: {iso_date}
draft: false
event_id: "{escape_yaml(event_id)}"
meetup_url: "{escape_yaml(link)}"
guid: "{escape_yaml(guid)}"
pub_date_raw: "{escape_yaml(pub_date_raw)}"
featured_image: "{escape_yaml(featured_image)}"
images:{yaml_images}
---

{md_images_block}{description}
"""

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)

        print(f"Created: {filepath} ({len(images)} image(s))")


if __name__ == "__main__":
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    response = requests.get(RSS_URL, headers=headers, timeout=15)
    response.raise_for_status()

    parse_rss_to_hugo(
        response.text, OUTPUT_DIR, headers, fetch_fallback_images=True
    )
