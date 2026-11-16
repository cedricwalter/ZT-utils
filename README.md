# ZT-utils

# Meetup to Lu.ma with robotic webdriver

Get from meetup RSS feed all information, present a menu and let you easily copy events from meetup to Luma

## Prerequisites
- MacOS
- Google Chrome installed
- You must be admin on Lu.Ma
- You can be guest on meetup, RSS feed is free to access
  
## Installation
run ONCE in terminal to create environment
```
python3 -m venv .venv\nsource .venv/bin/activate
pip install playwright selenium requests
playwright install chromium
playwright install selenium
```

## Run script
```
python3 meetup_to_luma_chrome.py
```
A menu is displayed just follow the instructions.

# Meetup to Hugo blog
This script fetches events from a Meetup RSS feed and converts them into Markdown files for Hugo, including image extraction and YAML front matter formatting.

```
python3 rss-to-hugo.py
```
