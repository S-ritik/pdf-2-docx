#!/usr/bin/env python3
"""
Verified-readings glossary for the PDF->DOCX reconstruction pipeline.

Two buckets, both keyed on the EXACT source string (case-sensitive):
  preserve     - unusual tokens confirmed CORRECT as printed; never "fix" these
  corrections  - confirmed OCR errors mapped  wrong -> right ; always apply

Typical loop (wraps your Stage C correction step):
    g = load("work/glossary.json")
    block = to_prompt(g, filter_text=raw_ocr)          # inject into the prompt
    ... run the correction model on this page ...
    ingest(g, preserve=confirmed_ok, corrections=confirmed_fixes, page=pageno)
    save(g, "work/glossary.json")                      # accumulate for next page

CLI:
    python glossary.py preserve "Archieves" --note "archaic; body uses 'Archives'" --page 91
    python glossary.py fix "centrally,leaving" "centrally, leaving" --note "missing space" --page 91
    python glossary.py prompt        # print the injectable block
    python glossary.py list          # counts
"""

import json, os, tempfile, datetime, argparse

SCHEMA = 1


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def load(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            g = json.load(f)
        g.setdefault("preserve", [])
        g.setdefault("corrections", [])
        return g
    return {"schema": SCHEMA, "updated": None, "preserve": [], "corrections": []}


def _find(items, key, val):
    for it in items:
        if it.get(key) == val:
            return it
    return None


def add_preserve(g, text, note="", page=None):
    """Record a token that is correct as printed. Returns True if newly added."""
    text = text.rstrip("\n")
    if not text:
        return False
    hit = _find(g["preserve"], "text", text)
    if hit:
        if page is not None and page not in hit["pages"]:
            hit["pages"].append(page); hit["pages"].sort()
        if note and not hit.get("note"):
            hit["note"] = note
        return False
    g["preserve"].append({"text": text, "note": note,
                          "pages": [page] if page is not None else []})
    return True


def add_correction(g, wrong, right, note="", page=None):
    """Record a confirmed OCR fix  wrong -> right . Returns True if newly added.
    Raises if `wrong` is already mapped to a different `right` (contradiction)."""
    wrong = wrong.rstrip("\n"); right = right.rstrip("\n")
    if not wrong or wrong == right:
        return False
    hit = _find(g["corrections"], "wrong", wrong)
    if hit:
        if hit["right"] != right:
            raise ValueError(f"conflicting fix for {wrong!r}: "
                             f"{hit['right']!r} vs {right!r}")
        if page is not None and page not in hit["pages"]:
            hit["pages"].append(page); hit["pages"].sort()
        return False
    g["corrections"].append({"wrong": wrong, "right": right, "note": note,
                             "pages": [page] if page is not None else []})
    return True


def ingest(g, preserve=None, corrections=None, page=None):
    """Bulk add. preserve: list of str or (str, note).
    corrections: list of (wrong, right[, note]) or {'wrong','right','note'}."""
    n = 0
    for p in (preserve or []):
        n += add_preserve(g, p, page=page) if isinstance(p, str) \
            else add_preserve(g, p[0], p[1] if len(p) > 1 else "", page=page)
    for c in (corrections or []):
        if isinstance(c, dict):
            n += add_correction(g, c["wrong"], c["right"], c.get("note", ""), page=page)
        else:
            n += add_correction(g, c[0], c[1], c[2] if len(c) > 2 else "", page=page)
    return n


def conflicts(g):
    """Strings marked preserve that are ALSO the 'wrong' side of a correction
    (the model can't both keep and change them). Returns a sorted list."""
    pres = {it["text"] for it in g["preserve"]}
    return sorted(pres & {it["wrong"] for it in g["corrections"]})


def save(g, path):
    g["preserve"].sort(key=lambda it: it["text"].lower())
    g["corrections"].sort(key=lambda it: it["wrong"].lower())
    bad = conflicts(g)
    if bad:
        raise ValueError(f"preserve/correction conflict for: {bad}")
    g["updated"] = _now()
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")          # atomic write:
    with os.fdopen(fd, "w", encoding="utf-8") as f:           # a crash mid-batch
        json.dump(g, f, ensure_ascii=False, indent=2)        # can't corrupt the
    os.replace(tmp, path)                                     # accumulated file


def _recency(it):
    """Higher = more recently confirmed (latest page the entry was seen on)."""
    pages = it.get("pages") or []
    return max(pages) if pages else -1


def to_prompt(g, filter_text=None, max_items=None):
    """Render the glossary as a block to inject into the correction prompt.

    filter_text: if given, keep only corrections whose `wrong` form occurs in it
      (lean, per-page prompts). Preserve entries are deliberately NOT filtered,
      because OCR may mangle the token so it won't match the page verbatim --
      which is exactly when the reminder is most needed.
    max_items: cap each list, keeping the MOST RECENTLY seen entries (by latest
      page) -- those are the most likely to be relevant to the current page.
    """
    pres, corr = g["preserve"], g["corrections"]
    if filter_text is not None:
        corr = [c for c in corr if c["wrong"] in filter_text]
    if max_items:
        pres = sorted(pres, key=_recency, reverse=True)[:max_items]
        corr = sorted(corr, key=_recency, reverse=True)[:max_items]
    out = []
    if pres:
        out += ["CONFIRMED SOURCE-CORRECT - reproduce these EXACTLY. Do not correct,",
                "normalise, or 'improve' them even if they look like spelling/spacing errors:"]
        out += [f'  - "{it["text"]}"' + (f'   ({it["note"]})' if it.get("note") else "")
                for it in pres]
        out += [""]
    if corr:
        out += ["CONFIRMED OCR ERRORS - when the left form appears, output the right form:"]
        out += [f'  - "{it["wrong"]}"  ->  "{it["right"]}"'
                + (f'   ({it["note"]})' if it.get("note") else "")
                for it in corr]
    return "\n".join(out).strip()


def _cli():
    ap = argparse.ArgumentParser(description="verified-readings glossary")
    ap.add_argument("--path", default="work/glossary.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preserve"); p.add_argument("text")
    p.add_argument("--note", default=""); p.add_argument("--page", type=int)
    c = sub.add_parser("fix"); c.add_argument("wrong"); c.add_argument("right")
    c.add_argument("--note", default=""); c.add_argument("--page", type=int)
    sub.add_parser("prompt"); sub.add_parser("list")
    a = ap.parse_args()
    g = load(a.path)
    if a.cmd == "preserve":
        ok = add_preserve(g, a.text, a.note, a.page); save(g, a.path)
        print(("added" if ok else "already present") + f": preserve {a.text!r}")
    elif a.cmd == "fix":
        ok = add_correction(g, a.wrong, a.right, a.note, a.page); save(g, a.path)
        print(("added" if ok else "already present") + f": {a.wrong!r} -> {a.right!r}")
    elif a.cmd == "prompt":
        print(to_prompt(g))
    elif a.cmd == "list":
        print(f"{len(g['preserve'])} preserve, {len(g['corrections'])} corrections; "
              f"updated {g.get('updated')}")


if __name__ == "__main__":
    _cli()
