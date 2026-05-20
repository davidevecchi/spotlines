"""
Fetch OSM Map Features wiki page and build osm-features-checklist.html
Columns: LOS allow | Buffer allow | Key | Value | Element | Description | Map rendering | Image

Handles both:
  - <table class="wikitable"> sections (Amenity, Building, Highway, Landuse, etc.)
  - <div class="taglist" data-taginfo-taglist-tags="..."> sections (Barrier, Natural, Man made, etc.)
"""

import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup, NavigableString

BASE    = "https://wiki.openstreetmap.org"
URL     = f"{BASE}/wiki/Map_features"
TAGINFO = "https://taginfo.openstreetmap.org"

# OSM element icon URLs (small PNGs from the wiki)
_ELEM_ICON = {
    "node":     f"{BASE}/w/images/thumb/7/76/Osm_element_node.svg/20px-Osm_element_node.svg.png",
    "way":      f"{BASE}/w/images/thumb/9/93/Osm_element_way.svg/20px-Osm_element_way.svg.png",
    "area":     f"{BASE}/w/images/thumb/e/e6/Osm_element_area.svg/20px-Osm_element_area.svg.png",
    "relation": f"{BASE}/w/images/thumb/f/f3/Osm_element_relation.svg/20px-Osm_element_relation.svg.png",
}

WIKI_API   = f"{BASE}/w/api.php"
WIKI_BATCH = 50   # max titles per MediaWiki API call

_ti_session = requests.Session()
_ti_session.headers["User-Agent"] = "spotlines-osm-checklist/1.0"

# ── taginfo ────────────────────────────────────────────────────────────────────

def _fetch_taginfo(key, value):
    """Return English wiki_pages entry from taginfo, or {}."""
    try:
        r = _ti_session.get(
            f"{TAGINFO}/api/4/tag/wiki_pages",
            params={"key": key, "value": value},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        for entry in data:
            if entry.get("lang") == "en":
                return entry
        return data[0] if data else {}
    except Exception:
        return {}

# ── wiki rendering image (image2 from infobox) ────────────────────────────────

_IMAGE2_RE = re.compile(
    r"[|\n]\s*(?:osmcarto-rendering|image2|carto-rendering|rendering)\s*=\s*([^\n|}{]+)",
    re.IGNORECASE,
)

def _fetch_wikitext_batch(titles):
    """Return {normalised_title: wikitext} for a batch of page titles."""
    try:
        r = _ti_session.get(WIKI_API, params={
            "action": "query", "prop": "revisions",
            "rvprop": "content", "rvslots": "main",
            "format": "json", "redirects": "1",
            "titles": "|".join(titles),
        }, timeout=30)
        r.raise_for_status()
        out = {}
        for page in r.json().get("query", {}).get("pages", {}).values():
            title = page.get("title", "")
            revs  = page.get("revisions", [])
            if revs:
                slot = revs[0].get("slots", {}).get("main", {})
                wt   = slot.get("*", "") or revs[0].get("*", "")
                out[title] = wt
        return out
    except Exception:
        return {}

def _fetch_file_thumb_urls(filenames, width=20):
    """Return {filename: thumb_url} for a list of File: names."""
    if not filenames:
        return {}
    out = {}
    for i in range(0, len(filenames), WIKI_BATCH):
        batch = filenames[i : i + WIKI_BATCH]
        try:
            r = _ti_session.get(WIKI_API, params={
                "action": "query", "prop": "imageinfo",
                "iiprop": "url", f"iiurlwidth": width,
                "format": "json",
                "titles": "|".join(f"File:{fn}" for fn in batch),
            }, timeout=30)
            r.raise_for_status()
            for page in r.json().get("query", {}).get("pages", {}).values():
                fn  = page.get("title", "").removeprefix("File:")
                ii  = page.get("imageinfo", [])
                url = ii[0].get("thumburl", "") if ii else ""
                if url:
                    out[fn] = url
        except Exception:
            pass
    return out

def fetch_rendering_cache(all_pairs):
    """Return {(key, value): thumb_url} for all tag pairs that have image2 on their wiki page."""
    pairs_with_value = [(k, v) for k, v in all_pairs if v]
    titles = [f"Tag:{k}={v}" for k, v in pairs_with_value]

    print(f"  Fetching wiki rendering images ({len(titles)} pages, "
          f"{(len(titles)-1)//WIKI_BATCH+1} batches) …", flush=True)

    # Step 1: fetch wikitext in batches
    title_to_image2: dict[str, str] = {}
    for i in range(0, len(titles), WIKI_BATCH):
        batch = titles[i : i + WIKI_BATCH]
        wt_map = _fetch_wikitext_batch(batch)
        for title, wt in wt_map.items():
            m = _IMAGE2_RE.search(wt)
            if m:
                fn = m.group(1).strip().lstrip("[").rstrip("]").strip()
                fn = re.sub(r"^(?:File|Image):", "", fn, flags=re.IGNORECASE)
                if fn:
                    title_to_image2[title] = fn

    # Step 2: resolve unique filenames → thumb URLs
    unique_files = list({fn for fn in title_to_image2.values()})
    file_urls = _fetch_file_thumb_urls(unique_files, width=60)

    # Step 3: build cache keyed by (key, value)
    # The wiki API normalises underscores → spaces in returned titles, so match both.
    cache: dict[tuple, str] = {}
    for k, v in pairs_with_value:
        for title in (f"Tag:{k}={v}", f"Tag:{k}={v.replace('_', ' ')}"):
            fn = title_to_image2.get(title, "")
            if fn:
                break
        url = file_urls.get(fn, "") if fn else ""
        if url:
            cache[(k, v)] = url
    print(f"    {len(cache)} rendering images resolved", flush=True)
    return cache

# ── cell builder ──────────────────────────────────────────────────────────────

def _taginfo_cells(key, value, info, rendering_url=""):
    """Build a cells dict for a taglist row using taginfo + rendering data."""
    wiki_key = f"{BASE}/wiki/Key:{key}"
    wiki_tag = f"{BASE}/wiki/Tag:{key}%3D{value}"
    cells = {
        "KEY":           f'<td><a href="{wiki_key}">{key}</a></td>',
        "VALUE":         f'<td><a href="{wiki_tag}">{value}</a></td>' if value else "<td></td>",
        "IMAGE":         "<td></td>",
        "MAP_RENDERING": "<td></td>",
        "ELEMENT":       "<td></td>",
        "DESCRIPTION":   "<td></td>",
    }
    if not info and not rendering_url:
        return cells

    desc = (info or {}).get("description", "")
    if desc:
        cells["DESCRIPTION"] = f"<td>{desc}</td>"

    img    = (info or {}).get("image") or {}
    prefix = img.get("thumb_url_prefix", "")
    suffix = img.get("thumb_url_suffix", "")
    if prefix:
        src = f"{prefix}120{suffix}"
        cells["IMAGE"] = (
            f'<td><figure class="mw-halign-center">'
            f'<img decoding="async" src="{src}">'
            f'</figure></td>'
        )

    if rendering_url:
        cells["MAP_RENDERING"] = (
            f'<td><figure class="mw-halign-center">'
            f'<img decoding="async" src="{rendering_url}">'
            f'</figure></td>'
        )

    elem_parts = []
    for attr, label in [("on_node","node"),("on_way","way"),("on_area","area"),("on_relation","relation")]:
        if info.get(attr):
            icon = _ELEM_ICON[label]
            elem_parts.append(
                f'<span class="mw-valign-text-bottom">'
                f'<a href="{BASE}/wiki/{label.capitalize()}" title="{label}">'
                f'<img alt="{label}" decoding="async" height="20" src="{icon}" width="20">'
                f'</a></span>'
            )
    if elem_parts:
        cells["ELEMENT"] = f'<td>{" ".join(elem_parts)}</td>'

    return cells

# ── Column slot names ──────────────────────────────────────────────────────────
SLOTS = ["KEY", "VALUE", "IMAGE", "MAP_RENDERING", "ELEMENT", "DESCRIPTION"]
SLOT_LABELS = {
    "KEY":           "Key",
    "VALUE":         "Value",
    "ELEMENT":       "Element",
    "DESCRIPTION":   "Description",
    "MAP_RENDERING": "Map rendering",
    "IMAGE":         "Image",
}

def classify_th(text):
    t = text.lower().strip()
    if t == "key":                                    return "KEY"
    if t == "value":                                  return "VALUE"
    if "element" in t:                                return "ELEMENT"
    if "comment" in t or "description" in t:         return "DESCRIPTION"
    if "carto" in t or "rendering" in t or "render" in t: return "MAP_RENDERING"
    if "photo" in t or "image" in t:                 return "IMAGE"
    return None

def abs_url(url):
    if not url:
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    return url

def fix_urls(tag):
    """Make all src/href absolute in-place."""
    for t in tag.find_all(True):
        for attr in ("src", "href", "srcset"):
            val = t.get(attr)
            if not val:
                continue
            if attr == "srcset":
                parts = []
                for chunk in val.split(","):
                    chunk = chunk.strip()
                    if chunk:
                        pieces = chunk.split()
                        pieces[0] = abs_url(pieces[0])
                        parts.append(" ".join(pieces))
                t[attr] = ", ".join(parts)
            else:
                t[attr] = abs_url(val)

# ── Fetch ──────────────────────────────────────────────────────────────────────
print(f"Fetching {URL} …", flush=True)
resp = requests.get(URL, headers={"User-Agent": "spotlines-osm-checklist/1.0"}, timeout=60)
resp.raise_for_status()
print(f"  {len(resp.content)//1024} KB received", flush=True)

soup = BeautifulSoup(resp.text, "html.parser")

# ── DOM-order walk ─────────────────────────────────────────────────────────────
# We walk the content div collecting sections in document order.
# Each section is a list of rows: {type: "category"|"data"|"taglist_data", ...}
# Wikitable sections may have their own internal category rows.
# Taglist sections emit a category row per heading then simple key=value rows.

content = (
    soup.find("div", class_="mw-content-ltr")
    or soup.find("div", class_="mw-parser-output")
    or soup.body
)

# Headings we care about for section tracking
SKIP_HEADINGS = {
    # top-level non-feature sections
    "contents", "primary features", "additional properties",
    "addresses", "annotations", "name", "properties", "references", "restrictions",
}

# Category rows whose text matches any of these substrings are dropped,
# along with all their data rows (they contain tag modifiers, not features).
SKIP_CATEGORY_SUBSTRINGS = (
    "attribute",          # "Attributes", "Additional attributes", "Additional_attributes"
    "lifecycle",          # "Lifecycle (see also lifecycle prefixes)"
    "sidewalk",           # highway sidewalk sub-sections
    "cycleway tagged on", # highway cycleway-on-road sub-section
    "busway",             # highway busway sub-section
    "street parking",     # highway street parking sub-section
    "tagged on the main", # generic "tagged on main roadway" headers
    "when sidewalk",
    "when cycleway",
    "this group lists",   # highway roads intro row
)

# Skip wikitable data rows whose first key column starts with these prefixes
# (these are tag attributes/qualifiers, not mappable features)
SKIP_KEY_PREFIXES = (
    "building:", "addr:", "name:", "alt_name", "source", "fixme",
    "is_in", "population", "int_ref", "nat_ref", "loc_ref",
)

def _skip_category(text: str) -> bool:
    t = text.lower()
    return any(sub in t for sub in SKIP_CATEGORY_SUBSTRINGS)

def _skip_key(key_text: str) -> bool:
    t = key_text.strip()
    return any(t.startswith(p) for p in SKIP_KEY_PREFIXES)

# Collect all top-level children of the content div in order
all_rows = []   # flat list of row dicts, in document order

current_h3 = None
current_h4 = None
n_data = 0
n_taglist = 0
n_cat = 0

# We'll keep one open "section" (table) per h3.  Within a wikitable the section
# headers are already embedded.  For taglist/non-table sections we emit explicit
# category rows derived from the headings.

def _heading_text(tag):
    headline = tag.find(class_="mw-headline")
    return (headline or tag).get_text(strip=True)

def _section_id(tag):
    headline = tag.find(class_="mw-headline")
    return headline["id"] if headline and headline.get("id") else ""

def process_wikitable(table):
    """Return list of row dicts extracted from a wikitable."""
    global n_data, n_cat
    tbody = table.find("tbody") or table

    header_row = tbody.find("tr", style=lambda s: s and "F8F4C2" in s)
    if not header_row:
        # Fall back to any row that has th elements with KEY/VALUE columns
        header_row = next(
            (tr for tr in tbody.find_all("tr", recursive=False) if tr.find("th")),
            None,
        )
    if not header_row:
        return []

    col_map = {}
    for i, th in enumerate(header_row.find_all("th", recursive=False)):
        slot = classify_th(th.get_text())
        if slot and slot not in col_map:
            col_map[slot] = i

    if "KEY" not in col_map and "VALUE" not in col_map:
        return []

    rows = []
    skip_until_next_cat = False  # True when current category is in skip list

    for tr in tbody.find_all("tr", recursive=False):
        style = tr.get("style", "")
        if "F8F4C2" in style:
            continue

        cat_th = tr.find("th", attrs={"colspan": True})
        if cat_th:
            fix_urls(cat_th)
            headline = cat_th.find(class_="mw-headline")
            cat_id   = headline["id"] if headline and headline.get("id") else ""
            cat_text = cat_th.get_text(strip=True)
            if _skip_category(cat_text):
                skip_until_next_cat = True
                continue
            skip_until_next_cat = False
            rows.append({"type": "category", "text": cat_text, "id": cat_id})
            n_cat += 1
            continue

        if skip_until_next_cat:
            continue

        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue

        # Skip pure-attribute rows (e.g. building:colour=*, addr:housenumber=*)
        key_text = tds[col_map["KEY"]].get_text(strip=True) if "KEY" in col_map and col_map["KEY"] < len(tds) else ""
        if _skip_key(key_text):
            continue

        fix_urls(tr)
        cells = {}
        for slot in SLOTS:
            idx = col_map.get(slot)
            if idx is not None and idx < len(tds):
                cells[slot] = str(tds[idx])
            else:
                cells[slot] = "<td></td>"
        rows.append({"type": "data", "cells": cells})
        n_data += 1

    return rows


def parse_taglist_pairs(div, heading_text, heading_id):
    """Return (heading_text, heading_id, [(key, value), ...]) or None if skipped.

    Taglist format: "key=val1,val2,val3,key2=val4,..."
    Items without "=" carry the most recently seen key forward.
    """
    tags_str = div.get("data-taginfo-taglist-tags", "")
    if not tags_str.strip() or _skip_category(heading_text):
        return None
    pairs = []
    current_key = None
    for token in tags_str.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" in token:
            k, v = token.split("=", 1)
            current_key = k.strip()
            pairs.append((current_key, v.strip()))
        elif current_key is not None:
            pairs.append((current_key, token))
    return (heading_text, heading_id, pairs) if pairs else None


def build_taglist_rows(heading_text, heading_id, pairs, taginfo_cache, rendering_cache):
    """Build row dicts for a taglist section using pre-fetched taginfo + rendering data."""
    global n_taglist, n_cat
    rows = [{"type": "category", "text": heading_text, "id": heading_id}]
    n_cat += 1
    for k, v in pairs:
        info          = taginfo_cache.get((k, v), {})
        rendering_url = rendering_cache.get((k, v), "")
        rows.append({"type": "data", "cells": _taginfo_cells(k, v, info, rendering_url)})
        n_taglist += 1
    return rows


# ── DOM walk — pass 1: collect wikitable rows + taglist (heading, pairs) ───────
# Each section_item is either:
#   {"kind": "wikitable_rows", "rows": [...]}
#   {"kind": "taglist",        "heading": str, "id": str, "pairs": [(k,v),...]}

section_items = []   # list of section_item dicts (in doc order, one per h3)
_sec_buf = []        # current h3 accumulator

def _flush():
    if _sec_buf:
        section_items.append(list(_sec_buf))
        _sec_buf.clear()

current_h3 = None
pending_h4 = None

for el in content.children:
    if not hasattr(el, "name") or el.name is None:
        continue

    if el.name == "h2":
        _flush()
        pending_h4 = None
        current_h3 = None
        continue

    if el.name == "h3":
        _flush()
        current_h3 = _heading_text(el)
        pending_h4 = None
        if current_h3.lower() in SKIP_HEADINGS:
            current_h3 = None
        continue

    if current_h3 is None:
        continue

    if el.name == "h4":
        ht = _heading_text(el)
        pending_h4 = None if _skip_category(ht) else (ht, _section_id(el))
        continue

    if el.name == "table" and "wikitable" in (el.get("class") or []):
        rows = process_wikitable(el)
        if rows:
            has_cat = any(r["type"] == "category" for r in rows)
            if not has_cat:
                rows.insert(0, {"type": "category", "text": current_h3,
                                 "id": current_h3.replace(" ", "_")})
                n_cat += 1
            _sec_buf.append({"kind": "wikitable_rows", "rows": rows})
            pending_h4 = None
        continue

    if el.name == "div":
        taglist = el.find("div", class_="taglist")
        if taglist:
            ht, hid = pending_h4 or (current_h3, current_h3.replace(" ", "_"))
            parsed = parse_taglist_pairs(taglist, ht, hid)
            if parsed:
                heading_text, heading_id, pairs = parsed
                _sec_buf.append({"kind": "taglist", "heading": heading_text,
                                  "id": heading_id, "pairs": pairs})
            pending_h4 = None
        continue

_flush()

# ── Collect all unique (key, value) pairs for bulk taginfo fetch ───────────────
all_pairs = {
    (k, v)
    for sec in section_items
    for item in sec
    if item["kind"] == "taglist"
    for k, v in item["pairs"]
}

print(f"  Fetching taginfo for {len(all_pairs)} tag pairs …", flush=True)
taginfo_cache: dict[tuple, dict] = {}
with ThreadPoolExecutor(max_workers=20) as ex:
    futures = {ex.submit(_fetch_taginfo, k, v): (k, v) for k, v in all_pairs}
    done = 0
    for fut in as_completed(futures):
        k, v = futures[fut]
        taginfo_cache[(k, v)] = fut.result()
        done += 1
        if done % 100 == 0:
            print(f"    {done}/{len(all_pairs)}", flush=True)
print(f"    done ({len(all_pairs)} fetched)", flush=True)

rendering_cache = fetch_rendering_cache(all_pairs)

# ── Pass 2: build final row lists ──────────────────────────────────────────────
sections = []

for sec in section_items:
    buf = []
    for item in sec:
        if item["kind"] == "wikitable_rows":
            buf.extend(item["rows"])
        else:
            buf.extend(build_taglist_rows(
                item["heading"], item["id"], item["pairs"], taginfo_cache, rendering_cache
            ))
    if buf:
        sections.append(buf)

print(
    f"  {n_cat} category rows, {n_data} wikitable data rows, "
    f"{n_taglist} taglist data rows across {len(sections)} sections",
    flush=True,
)

# ── CSS ────────────────────────────────────────────────────────────────────────
CSS = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
  font-size: 13px;
  margin: 0; padding: 12px 16px;
  background: #fafafa; color: #222;
}
h1 { font-size: 1.4em; margin-bottom: 8px; }
table.wikitable {
  border-collapse: collapse;
  width: 100%;
  margin-bottom: 24px;
  background: #fff;
}
table.wikitable th, table.wikitable td {
  border: 1px solid #ccc;
  padding: 2px;
  vertical-align: middle;
}
table.wikitable th {
  background: #eee;
  font-weight: 600;
}
tr.header-row th { background: #F8F4C2; }
tr.category-row th.cat-name {
  background: #dde8f0;
  font-weight: bold;
  text-align: left;
  padding: 5px 8px;
}
td img, th img { display: block; margin: auto; }
td.col-image img { width: auto !important; height: 80px !important; object-fit: contain; }
td:has(> figure), td:has(> img) { padding: 4px !important; }
figure { margin: 1px; }
code { font-size: 11px; }

/* Checkbox columns */
.cb-col {
  width: 46px; min-width: 46px;
  text-align: center; vertical-align: middle;
  padding: 3px 2px !important;
  background: #f4f4f4;
}
th.cb-col { background: #e8e8e8; font-size: 11px; line-height: 1.3; }
.tristate {
  cursor: pointer;
  width: 22px; height: 22px;
  border: 2px solid #bbb;
  border-radius: 4px;
  display: inline-flex;
  align-items: center; justify-content: center;
  font-size: 14px; line-height: 1;
  background: #fff;
  user-select: none;
  transition: background .12s, border-color .12s;
}
.tristate:hover { border-color: #555; }
.tristate[data-state="1"] { background: #43A047; border-color: #2E7D32; color: #fff; }
.tristate[data-state="2"] { background: #FB8C00; border-color: #E65100; color: #fff; }
.tristate[data-state="0"] { color: transparent; }
"""

# ── JS ─────────────────────────────────────────────────────────────────────────
JS = r"""
(function(){
  function show(btn){
    var s=btn.dataset.state;
    btn.textContent=s==='1'?'✓':s==='2'?'−':' ';
  }
  function dataRowsOfCat(catTr){
    var rows=[],tr=catTr.nextElementSibling;
    while(tr){
      if(tr.classList.contains('category-row')) break;
      if(tr.querySelectorAll('td').length>4) rows.push(tr);
      tr=tr.nextElementSibling;
    }
    return rows;
  }
  function syncCat(catTr,colIdx){
    var cb=catTr.querySelectorAll('.tristate')[colIdx];
    if(!cb) return;
    var on=0,off=0;
    dataRowsOfCat(catTr).forEach(function(r){
      var b=r.querySelectorAll('.tristate')[colIdx];
      if(b){if(b.dataset.state==='1')on++;else off++;}
    });
    cb.dataset.state=on===0?'0':off===0?'1':'2';
    show(cb);
  }
  document.addEventListener('DOMContentLoaded',function(){
  // If LOS (col 0) is turned on, also turn on Buffer (col 1); never force Buffer off.
  function propagateLosToBuf(row,isCatRow){
    var btns=row.querySelectorAll('.tristate');
    if(btns.length<2) return;
    var los=btns[0], buf=btns[1];
    if(los.dataset.state==='1' && buf.dataset.state==='0'){
      buf.dataset.state='1'; show(buf);
      if(!isCatRow){
        var prev=row.previousElementSibling;
        while(prev&&!prev.classList.contains('category-row'))
          prev=prev.previousElementSibling;
        if(prev) syncCat(prev,1);
      }
    }
  }

    document.querySelectorAll('td .tristate').forEach(function(btn){
      show(btn);
      btn.addEventListener('click',function(){
        btn.dataset.state=btn.dataset.state==='1'?'0':'1';
        show(btn);
        var row=btn.closest('tr');
        var idx=Array.from(row.querySelectorAll('.tristate')).indexOf(btn);
        var prev=row.previousElementSibling;
        while(prev&&!prev.classList.contains('category-row'))
          prev=prev.previousElementSibling;
        if(prev) syncCat(prev,idx);
        propagateLosToBuf(row,false);
      });
    });
    document.querySelectorAll('th .tristate').forEach(function(btn){
      show(btn);
      btn.addEventListener('click',function(){
        var catTr=btn.closest('tr');
        var idx=Array.from(catTr.querySelectorAll('.tristate')).indexOf(btn);
        var ns=btn.dataset.state==='1'?'0':'1';
        dataRowsOfCat(catTr).forEach(function(r){
          var b=r.querySelectorAll('.tristate')[idx];
          if(b){b.dataset.state=ns;show(b);}
        });
        btn.dataset.state=ns; show(btn);
        // propagate LOS→Buffer at category level
        if(idx===0){
          dataRowsOfCat(catTr).forEach(function(r){ propagateLosToBuf(r,false); });
          propagateLosToBuf(catTr,true);
        }
      });
    });
  });
})();
"""

# ── Render HTML ────────────────────────────────────────────────────────────────
def cb_th(title):
    return f'<th class="cb-col"><button class="tristate" data-state="0" title="{title}"></button></th>'

def cb_td():
    return '<td class="cb-col"><button class="tristate" data-state="0"></button></td>'

content_headers = "".join(
    f'<th>{SLOT_LABELS[s]}</th>' for s in SLOTS
)
header_row_html = (
    f'<tr class="header-row">'
    f'<th class="cb-col">LOS<br>allow</th>'
    f'<th class="cb-col">Buffer<br>allow</th>'
    f'<th class="cb-col">Anchor</th>'
    f'<th class="cb-col">Water</th>'
    f'{content_headers}'
    f'</tr>\n'
)

lines = [
    '<!DOCTYPE html>',
    '<html lang="en">',
    '<head>',
    '<meta charset="UTF-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    '<title>OSM Map Features Checklist</title>',
    f'<style>{CSS}</style>',
    f'<script>{JS}</script>',
    '</head>',
    '<body>',
    '<h1>OSM Map Features — LOS / Buffer Checklist</h1>',
]

def inner(td_str):
    m = re.match(r'<td[^>]*>(.*)</td>$', td_str, re.DOTALL)
    return m.group(1) if m else td_str

for rows in sections:
    lines.append('<table class="wikitable">')
    lines.append('<tbody>')
    lines.append(header_row_html)

    for row in rows:
        if row["type"] == "category":
            cid = f' id="{row["id"]}"' if row.get("id") else ""
            lines.append(
                f'<tr class="category-row"{cid}>'
                f'{cb_th("LOS allow")}'
                f'{cb_th("Buffer allow")}'
                f'{cb_th("Anchor")}'
                f'{cb_th("Water")}'
                f'<th class="cat-name" colspan="6">{row["text"]}</th>'
                f'</tr>'
            )
        else:
            cells = row["cells"]
            content_tds = "".join(
                f'<td class="col-{s.lower()}">{inner(cells[s])}</td>' for s in SLOTS
            )
            lines.append(
                f'<tr>'
                f'{cb_td()}'
                f'{cb_td()}'
                f'{cb_td()}'
                f'{cb_td()}'
                f'{content_tds}'
                f'</tr>'
            )

    lines.append('</tbody>')
    lines.append('</table>')

lines += ['</body>', '</html>']

out = "\n".join(lines)
with open("osm-features-checklist.html", "w", encoding="utf-8") as f:
    f.write(out)

print(f"Written osm-features-checklist.html ({len(out)//1024} KB)")
