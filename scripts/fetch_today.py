#!/usr/bin/env python3
"""Fetch today's NAR (local horse racing) schedule from keiba.go.jp.

The site has no JSON feed: TodayRaceInfoTop is an HTML page that links to
one RaceList page per track holding races today, and each RaceList page
contains an HTML table with the day's races for that track. This script
parses those pages with only the Python standard library and writes
data/today.json in the shape the dashboard (index.html) consumes.

On any failure (no tracks found, no races parsed, network error) it exits
non-zero and leaves the existing data/today.json untouched, so a bad run
never replaces good data with junk.

Politeness policy (the data is provided for free by keiba.go.jp, so
minimising load on their system comes first):
- Total requests per run: 1 top page + 1 RaceList page per track holding
  races today (typically 5-7 requests in total, never per-race pages).
- Requests are strictly sequential, with a pause between them.
- No retries: a failed run simply tries again at the next scheduled run.
"""

import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

JST = timezone(timedelta(hours=9))
BASE = "https://www.keiba.go.jp"
TOP_URL = BASE + "/KeibaWeb/TodayRaceInfo/TodayRaceInfoTop"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 30
# Pause between requests so a run never hammers keiba.go.jp.
REQUEST_INTERVAL_SEC = 2

# Track name (as printed on keiba.go.jp) -> dashboard id / kana / location.
TRACK_INFO = {
    "帯広": {"id": "obihiro", "kana": "OBIHIRO", "location": "北海道"},
    "帯広ば": {"id": "obihiro", "kana": "OBIHIRO", "location": "北海道"},
    "門別": {"id": "monbetsu", "kana": "MONBETSU", "location": "北海道"},
    "盛岡": {"id": "morioka", "kana": "MORIOKA", "location": "岩手県"},
    "水沢": {"id": "mizusawa", "kana": "MIZUSAWA", "location": "岩手県"},
    "浦和": {"id": "urawa", "kana": "URAWA", "location": "埼玉県"},
    "船橋": {"id": "funabashi", "kana": "FUNABASHI", "location": "千葉県"},
    "大井": {"id": "oi", "kana": "OI", "location": "東京シティ"},
    "川崎": {"id": "kawasaki", "kana": "KAWASAKI", "location": "神奈川県"},
    "金沢": {"id": "kanazawa", "kana": "KANAZAWA", "location": "石川県"},
    "笠松": {"id": "kasamatsu", "kana": "KASAMATSU", "location": "岐阜県"},
    "名古屋": {"id": "nagoya", "kana": "NAGOYA", "location": "愛知県"},
    "園田": {"id": "sonoda", "kana": "SONODA", "location": "兵庫県"},
    "姫路": {"id": "himeji", "kana": "HIMEJI", "location": "兵庫県"},
    "高知": {"id": "kochi", "kana": "KOCHI", "location": "高知県"},
    "佐賀": {"id": "saga", "kana": "SAGA", "location": "佐賀県"},
}


def normalize(text):
    """NFKC-normalize (full-width digits/letters/spaces -> ASCII forms)
    and collapse whitespace."""
    if text is None:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    return " ".join(text.split()).strip()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read().decode("utf-8", errors="replace")


class AnchorParser(HTMLParser):
    """Collect (href, text) for every <a> on a page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._href = None
        self._text = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href", "")
            self._text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._text is not None:
            self.links.append((self._href or "", normalize("".join(self._text))))
            self._href = None
            self._text = None

    def handle_data(self, data):
        if self._text is not None:
            self._text.append(data)


class RowParser(HTMLParser):
    """Collect table rows as lists of normalized cell texts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(normalize("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
RACE_NO_RE = re.compile(r"^(\d{1,2})R$")


def parse_race_list(html_text):
    """Extract race dicts from one track's RaceList page.

    Table columns: 競走 (1R), 発走時刻 (HH:MM), 変更, 競走種類
    (blank / 特別 / 重賞), 競走名・出馬表, コース (右1200m), 天候, 馬場, 頭数.
    Other tables on the page (scratches, changes) also start with a race
    number, but their second cell is not a HH:MM time, so the time check
    filters them out.
    """
    # The race-name cells link to the official entry table (DebaTable).
      # Capture those real hrefs per race number from the same page we
    # already downloaded — no extra requests — and prefer them over
    # locally constructed URLs.
    links = {}
    for href in re.findall(r'href="?([^\s">]*DebaTable\?[^\s">]*)', html_text):
              href = href.replace("&amp;", "&")
              qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
              no = qs.get("k_raceNo", [None])[0]
              if no and no.isdigit():
                            links[int(no)] = urllib.parse.urljoin(BASE, href)
                
    parser = RowParser()
    parser.feed(html_text)
    races = []
    for row in parser.rows:
        if len(row) < 9:
            continue
        m_no = RACE_NO_RE.match(row[0])
        m_time = TIME_RE.match(row[1])
        if not m_no or not m_time:
            continue
        kind = row[3] if row[3] in ("重賞", "特別") else ""
        horses = None
        m_horses = re.search(r"\d+", row[8])
        if m_horses:
            horses = int(m_horses.group(0))
        races.append(
            {
                "num": int(m_no.group(1)),
                "time": "%02d:%02d" % (int(m_time.group(1)), int(m_time.group(2))),
                "kind": kind,
                "name": row[4],
                "course": row[5],
                "weather": row[6],
                "going": row[7],
                "horses": horses,
              "url": links.get(int(m_no.group(1))),
            }
        )
    races.sort(key=lambda r: r["num"])
    return races


def find_today_tracks(top_html, today_str):
    """Return [(baba_code, track_name, url)] for today's RaceList links,
    deduplicated, in first-seen order. today_str is 'YYYY/MM/DD'."""
    parser = AnchorParser()
    parser.feed(top_html)
    found = []
    seen = set()
    for href, text in parser.links:
        if "RaceList" not in href:
            continue
        url = urllib.parse.urljoin(BASE, href.replace("&amp;", "&"))
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        race_date = (qs.get("k_raceDate") or [""])[0]
        baba_code = (qs.get("k_babaCode") or [""])[0]
        if race_date != today_str or not baba_code or baba_code in seen:
            continue
        if not text:
            continue
        seen.add(baba_code)
        found.append((baba_code, text, url))
    return found


def build_track(baba_code, name, races, race_date):
    info = TRACK_INFO.get(name)
    if info is None:
        info = {"id": "baba-%s" % baba_code, "kana": name, "location": "NAR"}

    # Per-race official entry-table (出走表) links: prefer the real href
    # captured from the RaceList page; fall back to a locally built URL
    # string. Either way the DebaTable pages themselves are never
  # fetched, so linking adds zero requests to keiba.go.jp.
    race_list = []
    for race in races:
        enriched = dict(race)
      if not enriched.get("url"):
            query = urllib.parse.urlencode(
                {
                    "k_raceDate": race_date,
                    "k_babaCode": baba_code,
                    "k_raceNo": race["num"],
                }
            )
            enriched["url"] = BASE + "/KeibaWeb/TodayRaceInfo/DebaTable?" + query
        race_list.append(enriched)

    # subInfo: distinct weather / going values seen during the day.
    weathers = []
    goings = []
    for race in races:
        if race["weather"] and race["weather"] not in weathers:
            weathers.append(race["weather"])
        if race["going"] and race["going"] not in goings:
            goings.append(race["going"])
    sub_parts = []
    if weathers:
        sub_parts.append("/".join(weathers))
    if goings:
        sub_parts.append("/".join(goings))

    track = {
        "id": info["id"],
        "name": name,
        "kana": info["kana"],
        "location": info["location"],
        "babaCode": baba_code,
        "races": len(races),
        "subInfo": "・".join(sub_parts),
        "raceList": race_list,
    }

    # mainRace: prefer the highest-numbered 重賞 race, else the
    # highest-numbered 特別 race, else omit the key entirely.
    for kind in ("重賞", "特別"):
        candidates = [r for r in races if r["kind"] == kind]
        if candidates:
            main = max(candidates, key=lambda r: r["num"])
            track["mainRace"] = {
                "num": main["num"],
                "name": main["name"],
                "source": "keiba.go.jp",
            }
            break
    return track


def main():
    now_jst = datetime.now(JST)
    today = now_jst.date()
    today_slash = today.strftime("%Y/%m/%d")

    top_html = fetch(TOP_URL)
    track_links = find_today_tracks(top_html, today_slash)
    if not track_links:
        print("error: no RaceList links found for %s" % today_slash, file=sys.stderr)
        return 1

    tracks = []
    total_races = 0
    for baba_code, name, url in track_links:
        time.sleep(REQUEST_INTERVAL_SEC)
        races = parse_race_list(fetch(url))
        if not races:
            print("error: no races parsed for %s (%s)" % (name, url), file=sys.stderr)
            return 1
        tracks.append(build_track(baba_code, name, races, today_slash))
        total_races += len(races)

    if not tracks or total_races == 0:
        print("error: nothing to write", file=sys.stderr)
        return 1

    payload = {
        "date": today.isoformat(),
        "generatedAt": now_jst.isoformat(timespec="seconds"),
        "source": "keiba.go.jp",
        "tracks": tracks,
    }

    out_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "today.json",
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp_path, out_path)
    print(
        "wrote %s: %d tracks, %d races for %s"
        % (out_path, len(tracks), total_races, payload["date"])
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
