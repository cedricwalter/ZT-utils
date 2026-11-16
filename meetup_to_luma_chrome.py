import html
import json
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

RSS_URL = "https://www.meetup.com/zurichtogether/events/rss/"
CHROME_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_luma_profile")
DEFAULT_TZ = ZoneInfo("Europe/Zurich")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
}

MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def _extract_from_next_data(node, found: dict):
    """Recursively scan Meetup Next.js __NEXT_DATA__ for event dates, venue, and photo."""
    if isinstance(node, dict):
        if "dateTime" in node and isinstance(node.get("dateTime"), str):
            found.setdefault("start_date", node["dateTime"])
            if isinstance(node.get("endTime"), str):
                found.setdefault("end_date", node["endTime"])

        venue = node.get("venue")
        if isinstance(venue, dict) and (venue.get("name") or venue.get("address")):
            parts = [
                venue.get("name", ""),
                venue.get("address", ""),
                venue.get("postalCode", ""),
                venue.get("city", ""),
            ]
            addr_str = ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
            if addr_str:
                found.setdefault("location", addr_str)

        for img_key in ("featuredEventPhoto", "photo", "image"):
            photo = node.get(img_key)
            if isinstance(photo, dict):
                high_res = (
                    photo.get("highResUrl")
                    or photo.get("baseUrl")
                    or photo.get("source")
                    or photo.get("url")
                )
                if isinstance(high_res, str) and high_res.startswith("http"):
                    if "baseUrl" in photo and "id" in photo and high_res == photo.get("baseUrl"):
                        high_res = f"{photo['baseUrl'].rstrip('/')}/{photo['id']}/highres.jpeg"
                    found.setdefault("image_url", high_res)

        for val in node.values():
            _extract_from_next_data(val, found)
    elif isinstance(node, list):
        for item in node:
            _extract_from_next_data(item, found)


def scrape_meetup_details(event_url: str) -> dict:
    """Fetch real start/end dates, venue address, and cover image URL from the Meetup event page."""
    details = {}
    if not event_url:
        return details

    try:
        resp = requests.get(event_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        page_html = resp.text

        # 1. Parse Next.js __NEXT_DATA__ JSON
        next_match = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            page_html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if next_match:
            try:
                next_json = json.loads(next_match.group(1).strip())
                _extract_from_next_data(next_json, details)
            except json.JSONDecodeError:
                pass

        # 2. Parse schema.org JSON-LD blocks for missing fields
        ld_blocks = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            page_html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        for raw in ld_blocks:
            try:
                data = json.loads(raw.strip())
                objs = data if isinstance(data, list) else [data]
                for obj in objs:
                    if isinstance(obj, dict) and obj.get("@type") in ("Event", "SocialEvent"):
                        if obj.get("startDate"):
                            details.setdefault("start_date", obj["startDate"])
                        if obj.get("endDate"):
                            details.setdefault("end_date", obj["endDate"])

                        loc = obj.get("location", {})
                        if isinstance(loc, dict):
                            name = loc.get("name", "")
                            addr = loc.get("address", {})
                            if isinstance(addr, dict):
                                parts = [
                                    name,
                                    addr.get("streetAddress", ""),
                                    addr.get("postalCode", ""),
                                    addr.get("addressLocality", ""),
                                ]
                                loc_str = ", ".join(p for p in parts if p)
                                if loc_str:
                                    details.setdefault("location", loc_str)
                            elif name:
                                details.setdefault("location", name)
                        img = obj.get("image")
                        if isinstance(img, list) and img:
                            details.setdefault("image_url", img[0])
                        elif isinstance(img, str) and img.startswith("http"):
                            details.setdefault("image_url", img)
            except json.JSONDecodeError:
                continue

        # 3. Fallback og:image
        if "image_url" not in details:
            og_match = re.search(
                r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                page_html,
                flags=re.IGNORECASE,
            )
            if not og_match:
                og_match = re.search(
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
                    page_html,
                    flags=re.IGNORECASE,
                )
            if og_match:
                details["image_url"] = html.unescape(og_match.group(1).strip())

    except Exception as e:
        print(f"  [Note] Could not fetch extra details from Meetup page: {e}")

    return details


def fetch_rss_events() -> list[dict]:
    resp = requests.get(RSS_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    events = []
    for item in root.find("channel").findall("item"):
        title = (item.findtext("title") or "Untitled Event").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        events.append({
            "title": title,
            "link": link,
            "description": desc,
        })
    return events


def parse_selection(user_input: str, max_len: int) -> list[int]:
    """Parse user input like '1, 3, 5-7' or 'all' into 0-based indices."""
    user_input = user_input.strip().lower()
    if user_input == "all":
        return list(range(max_len))
    if user_input in ("q", "quit", "exit", ""):
        return []

    indices = set()
    for part in user_input.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start_s, end_s = part.split("-", 1)
                for n in range(int(start_s), int(end_s) + 1):
                    if 1 <= n <= max_len:
                        indices.add(n - 1)
            except ValueError:
                pass
        else:
            try:
                n = int(part)
                if 1 <= n <= max_len:
                    indices.add(n - 1)
            except ValueError:
                pass
    return sorted(indices)


def download_temp_image(image_url: str) -> str | None:
    """Download cover image to a temp file so Chrome can upload it to Lu.ma."""
    if not image_url:
        return None
    try:
        r = requests.get(image_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        if len(r.content) < 500:
            print(f"  [Cover] Downloaded file too small ({len(r.content)} bytes), skipping.")
            return None
        content_type = r.headers.get("Content-Type", "").lower()
        ext = ".jpg"
        if "png" in content_type or ".png" in image_url.lower():
            ext = ".png"
        elif "webp" in content_type or ".webp" in image_url.lower():
            ext = ".webp"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="luma_cover_")
        tmp.write(r.content)
        tmp.close()
        return tmp.name
    except Exception as e:
        print(f"  [Cover] Failed to download image from {image_url}: {e}")
        return None


def upload_luma_cover_image(driver: webdriver.Chrome, image_path: str) -> bool:
    """Reliable cover image upload for Lu.ma (without triggering native macOS file dialogs)."""
    abs_path = os.path.abspath(image_path)
    if not os.path.exists(abs_path):
        print(f"  [Cover] Temp file not found: {abs_path}")
        return False

    try:
        file_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')

        if not file_inputs:
            cover_triggers = driver.find_elements(
                By.XPATH,
                "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'upload') "
                "or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'cover') "
                "or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'image') "
                "or contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'cover')]",
            )
            for el in cover_triggers:
                try:
                    if el.is_displayed():
                        driver.execute_script("arguments[0].click();", el)
                        time.sleep(0.8)
                        file_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
                        if file_inputs:
                            break
                except Exception:
                    continue

        if not file_inputs:
            print("  [Cover] Could not find <input type='file'> on Lu.ma page.")
            return False

        target_input = file_inputs[0]
        for inp in file_inputs:
            accept = (inp.get_attribute("accept") or "").lower()
            if "image" in accept:
                target_input = inp
                break

        driver.execute_script(
            """
            const inp = arguments[0];
            inp.style.display = 'block';
            inp.style.visibility = 'visible';
            inp.style.opacity = '1';
            inp.style.width = '100px';
            inp.style.height = '40px';
            inp.style.position = 'fixed';
            inp.style.top = '10px';
            inp.style.left = '10px';
            inp.style.zIndex = '999999';
            inp.removeAttribute('hidden');
            """,
            target_input,
        )
        time.sleep(0.3)

        target_input.send_keys(abs_path)
        time.sleep(2.0)

        for btn_text in ("Upload", "Save", "Apply", "Done", "Confirm", "Crop"):
            confirm_btns = driver.find_elements(
                By.XPATH,
                f"//div[@role='dialog']//button[contains(normalize-space(.), '{btn_text}')] | "
                f"//button[contains(normalize-space(.), '{btn_text}')]",
            )
            for btn in confirm_btns:
                try:
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1.5)
                        return True
                except Exception:
                    continue

        return True
    except Exception as e:
        print(f"  [Cover] Upload exception: {e}")
        return False


def parse_iso_to_zurich(iso_str: str) -> datetime | None:
    if not iso_str:
        return None
    try:
        cleaned = iso_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=DEFAULT_TZ)
        return dt.astimezone(DEFAULT_TZ)
    except Exception:
        return None


def _parse_calendar_header(header_text: str) -> tuple[int, int] | None:
    """Parse month & year from Lu.ma calendar header (e.g., 'November 2026' or 'Nov 2026')."""
    text = header_text.strip().lower()
    year_match = re.search(r"(20\d{2})", text)
    if not year_match:
        return None
    year = int(year_match.group(1))
    for name, month_num in MONTH_NAMES.items():
        if re.search(rf"\b{name}\b", text):
            return (year, month_num)
    return None


def _select_date_in_luma_calendar(driver, target_dt: datetime) -> bool:
    """Navigate month header and click target day in Lu.ma's open calendar popover."""
    target_ym = (target_dt.year, target_dt.month)
    target_day = str(target_dt.day)
    time.sleep(0.5)

    for _ in range(18):
        candidates = driver.find_elements(
            By.XPATH,
            "//*[contains(text(), '2025') or contains(text(), '2026') or contains(text(), '2027') or contains(text(), '2028')]",
        )
        current_ym = None
        header_el = None
        for el in candidates:
            if not el.is_displayed():
                continue
            txt = el.text.strip().replace("\n", " ")
            if len(txt) <= 25:
                parsed = _parse_calendar_header(txt)
                if parsed:
                    current_ym = parsed
                    header_el = el
                    break

        if not current_ym or current_ym == target_ym:
            break

        container = header_el
        for _ in range(3):
            try:
                parent = container.find_element(By.XPATH, "..")
                if len(parent.find_elements(By.TAG_NAME, "button")) >= 2:
                    container = parent
                    break
                container = parent
            except Exception:
                break

        nav_buttons = [
            b for b in container.find_elements(By.TAG_NAME, "button")
            if b.is_displayed() and len(b.text.strip()) <= 2
        ]
        if len(nav_buttons) >= 2:
            if current_ym < target_ym:
                nav_buttons[-1].click()
            else:
                nav_buttons[0].click()
            time.sleep(0.35)
        else:
            break

    day_cells = driver.find_elements(
        By.XPATH,
        f"//*[(@role='dialog' or @role='grid' or contains(@class, 'popover') or contains(@class, 'calendar') or contains(@class, 'picker'))]"
        f"//*[(self::button or @role='gridcell' or self::div or self::span) and normalize-space()='{target_day}']",
    )
    if not day_cells:
        day_cells = driver.find_elements(
            By.XPATH,
            f"//button[normalize-space()='{target_day}'] | //*[@role='gridcell' and normalize-space()='{target_day}']",
        )

    valid_cells = []
    for cell in day_cells:
        if not cell.is_displayed():
            continue
        cls = (cell.get_attribute("class") or "").lower()
        aria_disabled = cell.get_attribute("aria-disabled")
        outside = any(k in cls for k in ("outside", "muted", "other-month", "disabled", "prev", "next"))
        if aria_disabled != "true" and not outside:
            valid_cells.append(cell)

    if not valid_cells:
        valid_cells = [c for c in day_cells if c.is_displayed()]

    if valid_cells:
        chosen = valid_cells[-1] if target_dt.day > 20 and len(valid_cells) > 1 else valid_cells[0]
        try:
            chosen.click()
        except Exception:
            driver.execute_script("arguments[0].click();", chosen)
        time.sleep(0.45)
        return True

    return False


def _select_time_in_luma_dropdown(driver, target_dt: datetime) -> bool:
    """Type or select the target time (24h or 12h) in Lu.ma's open time dropdown."""
    time_24 = target_dt.strftime("%H:%M")
    hour_12 = target_dt.strftime("%I:%M %p").lstrip("0")
    hour_12_compact = target_dt.strftime("%I:%M%p").lstrip("0").lower()
    time.sleep(0.45)

    active = driver.switch_to.active_element
    if active and active.tag_name.lower() == "input":
        active.send_keys(Keys.COMMAND, "a")
        active.send_keys(Keys.BACKSPACE)
        active.send_keys(time_24)
        time.sleep(0.35)
        active.send_keys(Keys.ENTER)
        time.sleep(0.35)
        return True

    for candidate_str in (time_24, hour_12, hour_12_compact):
        options = driver.find_elements(
            By.XPATH,
            f"//*[@role='option' or @role='menuitem' or self::li or self::button or self::div]"
            f"[normalize-space()='{candidate_str}']",
        )
        for opt in options:
            if opt.is_displayed() and opt.location.get("y", 0) > 80:
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block: 'nearest'});", opt)
                    opt.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", opt)
                time.sleep(0.4)
                return True

    rounded_minute = (target_dt.minute // 15) * 15
    rounded_dt = target_dt.replace(minute=rounded_minute)
    for candidate_str in (
        rounded_dt.strftime("%H:%M"),
        rounded_dt.strftime("%I:%M %p").lstrip("0"),
    ):
        options = driver.find_elements(
            By.XPATH,
            f"//*[@role='option' or @role='menuitem' or self::li or self::button or self::div]"
            f"[normalize-space()='{candidate_str}']",
        )
        for opt in options:
            if opt.is_displayed() and opt.location.get("y", 0) > 80:
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block: 'nearest'});", opt)
                    opt.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", opt)
                time.sleep(0.4)
                return True

    return False


def _find_luma_datetime_triggers(driver) -> tuple[list, list]:
    """Scan visible interactive elements in the top half of Lu.ma's create page for date & time pills."""
    date_pattern = re.compile(
        r"^(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+)?"
        r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?)"
        r"(?:,?\s+20\d{2})?$",
        re.IGNORECASE,
    )
    time_pattern = re.compile(r"^\d{1,2}:\d{2}(?:\s*[AP]M)?$", re.IGNORECASE)

    date_els = []
    time_els = []

    candidates = driver.find_elements(
        By.CSS_SELECTOR,
        "button, [role='button'], [tabindex='0'], input",
    )
    for el in candidates:
        try:
            if not el.is_displayed():
                continue
            loc = el.location
            size = el.size
            if loc.get("y", 9999) > 650 or size.get("width", 0) < 35:
                continue

            txt = (el.text or el.get_attribute("value") or "").strip().replace("\n", " ")
            if not txt or len(txt) > 30:
                continue

            if date_pattern.match(txt):
                date_els.append((loc["y"], loc["x"], el))
            elif time_pattern.match(txt):
                time_els.append((loc["y"], loc["x"], el))
        except Exception:
            continue

    date_els.sort(key=lambda t: (t[0], t[1]))
    time_els.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in date_els], [t[2] for t in time_els]


def fill_luma_datetime(driver, start_iso: str, end_iso: str) -> bool:
    """Automatically set Start and End date & time on Lu.ma using coordinate-sorted trigger pills."""
    start_dt = parse_iso_to_zurich(start_iso)
    if not start_dt:
        print("  [Date/Time] Could not parse start timestamp from Meetup page.")
        return False

    end_dt = parse_iso_to_zurich(end_iso) or (start_dt + timedelta(hours=3))
    print(
        f"  [Date/Time] Target Zurich schedule: "
        f"{start_dt.strftime('%Y-%m-%d %H:%M')} -> {end_dt.strftime('%Y-%m-%d %H:%M')}"
    )

    date_triggers, time_triggers = _find_luma_datetime_triggers(driver)
    if len(date_triggers) < 2 or len(time_triggers) < 2:
        print(
            f"  [Date/Time] Found {len(date_triggers)} date pill(s) and {len(time_triggers)} time pill(s)."
        )
        return False

    success_count = 0

    try:
        date_triggers, _ = _find_luma_datetime_triggers(driver)
        date_triggers[0].click()
        if _select_date_in_luma_calendar(driver, start_dt):
            success_count += 1
            print(f"  ✓ Start date set to {start_dt.strftime('%d %b %Y')}")
    except Exception as e:
        print(f"  [Date/Time] Start date step failed: {e}")

    try:
        _, time_triggers = _find_luma_datetime_triggers(driver)
        time_triggers[0].click()
        if _select_time_in_luma_dropdown(driver, start_dt):
            success_count += 1
            print(f"  ✓ Start time set to {start_dt.strftime('%H:%M')}")
    except Exception as e:
        print(f"  [Date/Time] Start time step failed: {e}")

    try:
        date_triggers, _ = _find_luma_datetime_triggers(driver)
        if len(date_triggers) >= 2:
            date_triggers[1].click()
            if _select_date_in_luma_calendar(driver, end_dt):
                success_count += 1
                print(f"  ✓ End date set to {end_dt.strftime('%d %b %Y')}")
    except Exception as e:
        print(f"  [Date/Time] End date step failed: {e}")

    try:
        _, time_triggers = _find_luma_datetime_triggers(driver)
        if len(time_triggers) >= 2:
            time_triggers[1].click()
            if _select_time_in_luma_dropdown(driver, end_dt):
                success_count += 1
                print(f"  ✓ End time set to {end_dt.strftime('%H:%M')}")
    except Exception as e:
        print(f"  [Date/Time] End time step failed: {e}")

    return success_count >= 2


def _clear_and_type(el, text: str):
    """Reliably clear a React/Google Places input and type text."""
    el.click()
    el.send_keys(Keys.COMMAND, "a")
    el.send_keys(Keys.CONTROL, "a")
    el.send_keys(Keys.BACKSPACE)
    time.sleep(0.2)
    el.send_keys(text)


def _pick_top_location_suggestion(driver) -> bool:
    """Find and click the top autocomplete suggestion from Lu.ma / Google Places."""
    selectors = [
        ".pac-container .pac-item",
        "[role='listbox'] [role='option']",
        "[role='option']",
        "[data-radix-popper-content-wrapper] button",
        "[data-radix-popper-content-wrapper] [role='button']",
        "[data-radix-popper-content-wrapper] div[tabindex]",
    ]
    for _ in range(8):
        for sel in selectors:
            items = driver.find_elements(By.CSS_SELECTOR, sel)
            visible = [
                it for it in items
                if it.is_displayed() and it.text.strip() and "add event location" not in it.text.lower()
            ]
            if visible:
                top_item = visible[0]
                try:
                    top_item.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", top_item)
                time.sleep(0.6)
                return True
        time.sleep(0.35)
    return False


def fill_luma_location(driver, wait: WebDriverWait, location_text: str) -> bool:
    """Click 'Add Event Location', type the address (with street fallback), and select the top match."""
    if not location_text:
        return False

    try:
        # 1. Click 'Add Event Location' trigger if the input is not already open
        loc_triggers = driver.find_elements(
            By.XPATH,
            "//*[contains(text(), 'Add Event Location') or contains(text(), 'Offline location or virtual link')]",
        )
        for el in loc_triggers:
            if el.is_displayed():
                try:
                    el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", el)
                time.sleep(0.7)
                break

        # 2. Find the visible location input box
        loc_input = wait.until(
            EC.visibility_of_element_located((
                By.CSS_SELECTOR,
                'input.pac-target-input, input[placeholder*="location" i], input[placeholder*="address" i], input[placeholder*="Enter" i]',
            ))
        )

        # Build candidate queries: full string first, then street+city if comma-separated
        queries = [location_text.strip()]
        parts = [p.strip() for p in location_text.split(",") if p.strip()]
        if len(parts) >= 2:
            street_and_city = ", ".join(parts[1:])
            if street_and_city not in queries:
                queries.append(street_and_city)

        # 3. Try each query until an autocomplete suggestion is selected
        for q in queries:
            _clear_and_type(loc_input, q)
            if _pick_top_location_suggestion(driver):
                return True

        # 4. Final keyboard fallback on the active input
        loc_input.send_keys(Keys.ARROW_DOWN)
        time.sleep(0.3)
        loc_input.send_keys(Keys.ENTER)
        time.sleep(0.5)
        return True

    except Exception as e:
        print(f"  [Note] Could not auto-select location suggestion: {e}")
        return False


def create_chrome_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
    options.add_argument("--start-maximized")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    return webdriver.Chrome(options=options)


def main():
    print("Fetching events from Zurich Together Meetup RSS...")
    events = fetch_rss_events()
    if not events:
        print("No events found in RSS feed.")
        return

    print("\n=== Available Meetup Events ===")
    for i, ev in enumerate(events, 1):
        print(f"  [{i}] {ev['title']}")

    print("\nEnter event numbers to create on Lu.ma (e.g., 1, 3, 5-7 or 'all'):")
    selection_raw = input("> ")
    selected_indices = parse_selection(selection_raw, len(events))

    if not selected_indices:
        print("No valid events selected. Exiting.")
        return

    print(f"\nLaunching Chrome for {len(selected_indices)} selected event(s)...")
    driver = create_chrome_driver()
    wait = WebDriverWait(driver, 15)

    try:
        driver.get("https://lu.ma/create")
        input(
            "\n1. Log into your Lu.ma account in the Chrome window if prompted.\n"
            "2. Once the 'Create Event' form is visible, press ENTER here in Terminal..."
        )

        for count, idx in enumerate(selected_indices, 1):
            ev = events[idx]
            print(f"\n--- [{count}/{len(selected_indices)}] Preparing: {ev['title']} ---")
            details = scrape_meetup_details(ev["link"])

            driver.get("https://lu.ma/create")
            title_el = wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'input[placeholder*="Event Name"], textarea[placeholder*="Event Name"]')
                )
            )
            time.sleep(1.0)

            # 1. Upload Cover Image FIRST
            temp_img_path = None
            if details.get("image_url"):
                print(f"  • Cover URL found: {details['image_url']}")
                temp_img_path = download_temp_image(details["image_url"])
                if temp_img_path:
                    if upload_luma_cover_image(driver, temp_img_path):
                        print("  ✓ Uploaded cover image")
                    else:
                        print(f"  • Manual cover image URL: {details['image_url']}")
            else:
                print("  • No cover image URL found on Meetup page.")

            # 2. Fill Event Title
            title_el.click()
            title_el.send_keys(ev["title"])
            print("  ✓ Filled event title")

            # 3. Set Start & End Date/Time
            if details.get("start_date"):
                if not fill_luma_datetime(driver, details["start_date"], details.get("end_date", "")):
                    print(f"  • Manual Date/Time reference: {details['start_date']} -> {details.get('end_date', '')}")
            else:
                print("  • Could not extract start date from Meetup page; set date/time manually in Chrome.")

            # 4. Fill Event Location
            if details.get("location"):
                if fill_luma_location(driver, wait, details["location"]):
                    print(f"  ✓ Set location: {details['location']}")
                else:
                    print(f"  • Meetup Venue (paste manually): {details['location']}")

            # 5. Open & Fill Description
            full_desc = f"{ev['description']}\n\nRSVP on Meetup: {ev['link']}"
            try:
                add_desc_btns = driver.find_elements(By.XPATH, "//*[contains(text(), 'Add Description')]")
                if add_desc_btns:
                    add_desc_btns[0].click()
                    time.sleep(0.4)
                desc_boxes = driver.find_elements(
                    By.CSS_SELECTOR,
                    '[contenteditable="true"], textarea[placeholder*="description" i]',
                )
                if desc_boxes:
                    desc_boxes[-1].click()
                    desc_boxes[-1].send_keys(full_desc)
                    print("  ✓ Filled description")
            except Exception:
                print("  • Could not auto-focus description box; paste manually if needed.")

            print(f"  • Meetup Link: {ev['link']}")

            if temp_img_path and os.path.exists(temp_img_path):
                os.remove(temp_img_path)

            if count < len(selected_indices):
                cmd = input(
                    "\nReview the event in Chrome and click 'Create Event'.\n"
                    "Press ENTER to load the next event (or 'q' to quit): "
                ).strip().lower()
                if cmd == "q":
                    break
            else:
                input("\nAll selected events processed! Click 'Create Event' in Chrome, then press ENTER to close...")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
