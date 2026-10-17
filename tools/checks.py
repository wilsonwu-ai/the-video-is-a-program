#!/usr/bin/env python3
"""checks.py -- editorial checks for an explainer repo's README.md (stdlib only).

usage:
  checks.py <repo_dir> --brief <brief.md> --sources <dir> [--research <dir>] [--http]
  checks.py --skill <SKILL.md>

Always exits 0 and prints exactly one JSON object on stdout.
"""
import argparse
import bisect
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

EM_DASH = "—"
BANNED_RE = re.compile(
    r"ELI5|explain(?:ed)?\s+like\s+I['’]?m\s+(?:five|5)|like\s+you['’]?re\s+five",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>\"'`\]\[]+")
TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*")
NGRAM_N = 8

UNITS = (
    "fps|ms|us|ns|kb|mb|gb|tb|kib|mib|gib|hz|khz|mhz|ghz|px|pt|s|sec|secs|second|seconds|"
    "min|mins|minute|minutes|h|hr|hrs|hour|hours|day|days|week|weeks|month|months|year|years|"
    "byte|bytes|bit|bits|token|tokens|word|words|line|lines|file|files|user|users|frame|frames|"
    "percent|x|time|times|dollar|dollars|usd|calls|requests|k|m|b|"
    "million|billion|thousand|trillion|people|customers|rows|events|samples|steps|layers|"
    "parameters|params"
)
UNIT_AFTER_RE = re.compile(r"\s*(?:%|(?:" + UNITS + r")\b)", re.IGNORECASE)

# a numeric token: 900, 2.1M, $1,200, 143.7k, 292,426, 1.58.2, 2026, 40%
NUM_RE = re.compile(r"(?<![\w.,$])\$?\d+(?:[,.]\d+)*[kKmMbB]?(?![\w])")
MULT_WORDS = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}
MULT_AFTER_RE = re.compile(r"\s+(thousand|million|billion|trillion)\b", re.IGNORECASE)


# --------------------------------------------------------------------------- helpers
def read_text(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def walk_files(root, exts=None):
    out = []
    if not root or not os.path.isdir(root):
        return out
    for dp, _dn, fns in os.walk(root):
        for fn in sorted(fns):
            if exts is None or os.path.splitext(fn)[1].lower() in exts:
                out.append(os.path.join(dp, fn))
    return sorted(out)


def line_starts(text):
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return starts


def line_of(starts, offset):
    return bisect.bisect_right(starts, offset)  # 1-based


def blank_spans(text, regex):
    """Replace regex matches with spaces, preserving offsets and newlines."""
    return regex.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def tokenize(text):
    """-> list of (normalized_token, start_offset)."""
    out = []
    for m in TOKEN_RE.finditer(text):
        out.append((m.group(0).lower().replace("'", "").replace("’", ""), m.start()))
    return out


def clean_url(u):
    u = u.rstrip()
    while u:
        c = u[-1]
        if c in ".,;:!?*_>\"'":
            u = u[:-1]
        elif c in ")]}":
            opener = {")": "(", "]": "[", "}": "{"}[c]
            if u.count(c) > u.count(opener):
                u = u[:-1]
            else:
                break
        else:
            break
    return u


def url_variants(u):
    u = u.strip()
    base = u.rstrip("/")
    return {u.lower(), base.lower()}


def norm_title(t):
    t = re.sub(r"[`*_]", "", t)
    t = re.sub(r"\s+#+\s*$", "", t)
    return re.sub(r"\s+", " ", t).strip().casefold()


def count_words(text):
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\]\([^)]*\)", "] ", text)
    text = URL_RE.sub(" ", text)
    return sum(1 for t in text.split() if re.search(r"\w", t))


# --------------------------------------------------------------------------- brief
def split_h2(text):
    """-> list of (title, body_text, heading_line_index0) skipping fenced code."""
    lines = text.split("\n")
    heads = []
    fence = False
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("```") or ln.lstrip().startswith("~~~"):
            fence = not fence
            continue
        if not fence:
            m = re.match(r"^##\s+(.+?)\s*$", ln)
            if m and not ln.startswith("###"):
                heads.append((m.group(1), i))
    out = []
    for k, (title, i) in enumerate(heads):
        end = heads[k + 1][1] if k + 1 < len(heads) else len(lines)
        out.append((title, "\n".join(lines[i + 1:end]), i))
    return out


def parse_brief(path, warnings):
    brief = {"allowed_urls": set(), "allowed_wild": [], "numbers_text": [], "required": [], "caps": {}, "total_cap": None}
    if not path or not os.path.isfile(path):
        warnings.append("brief not found: %s" % path)
        return brief
    text = read_text(path) or ""
    secs = {norm_title(t): body for t, body, _ in split_h2(text)}

    body = secs.get("allowed urls", None)
    if body is None:
        warnings.append("brief has no '## Allowed URLs' section")
    else:
        for m in URL_RE.finditer(body):
            u = clean_url(m.group(0))
            if u.endswith("*"):
                brief["allowed_wild"].append(u.rstrip("*").lower())
            else:
                brief["allowed_urls"] |= url_variants(u)
        # wildcard URLs end with '*' which URL_RE keeps; handle those that clean_url left alone
        for m in re.finditer(r"https?://\S*\*", body):
            brief["allowed_wild"].append(m.group(0).rstrip("*").lower())

    body = secs.get("numbers", None)
    if body is None:
        warnings.append("brief has no '## Numbers' section")
    else:
        for ln in body.split("\n"):
            s = ln.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not cells or re.fullmatch(r"[:\- ]*", cells[0] or "-"):
                continue
            brief["numbers_text"].append(cells[0])

    body = secs.get("required sections", None)
    if body is None:
        warnings.append("brief has no '## Required sections' section")
    else:
        for ln in body.split("\n"):
            m = re.match(r"^\s*[-*]\s+(.+?)\s*$", ln)
            if m:
                brief["required"].append(re.sub(r"^##\s+", "", m.group(1)).strip())

    body = secs.get("caps", None)
    if body is None:
        warnings.append("brief has no '## Caps' section")
    else:
        for ln in body.split("\n"):
            m = re.match(r"^\s*[-*]\s+(.+):\s*([\d,]+)\s*(?:words?)?\s*$", ln)
            if not m:
                continue
            name, n = m.group(1).strip(), int(m.group(2).replace(",", ""))
            if name.upper() == "TOTAL":
                brief["total_cap"] = n
            else:
                brief["caps"][name] = n
    return brief


# --------------------------------------------------------------------------- corpus
class Corpus:
    def __init__(self):
        self.texts = []          # (path, text)
        self.ngrams = set()
        self.raw_lower = ""
        self.num_lits = set()
        self.num_vals = set()

    def add_file(self, path):
        t = read_text(path)
        if t is None:
            return
        self.texts.append((path, t))

    def finalize(self):
        self.raw_lower = "\n".join(t.lower() for _p, t in self.texts)
        for _p, t in self.texts:
            toks = [tok for tok, _o in tokenize(blank_spans(t, URL_RE))]
            for i in range(len(toks) - NGRAM_N + 1):
                self.ngrams.add(" ".join(toks[i:i + NGRAM_N]))
            add_numbers(self.num_lits, self.num_vals, t)


def norm_num(raw):
    s = raw.replace("$", "").replace(",", "").replace("%", "")
    return s.lower()


def num_value(norm, mult=1.0):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmb]?)", norm)
    if not m:
        return None
    v = float(m.group(1))
    v *= {"": 1.0, "k": 1e3, "m": 1e6, "b": 1e9}[m.group(2)]
    return round(v * mult, 6)


def add_numbers(lits, vals, text):
    text = blank_spans(text, URL_RE)
    for m in NUM_RE.finditer(text):
        n = norm_num(m.group(0))
        lits.add(n)
        mm = MULT_AFTER_RE.match(text, m.end())
        mult = MULT_WORDS[mm.group(1).lower()] if mm else 1.0
        v = num_value(n, mult)
        if v is not None:
            vals.add(v)
            if mult != 1.0:
                vals.add(num_value(n, 1.0))


# --------------------------------------------------------------------------- article checks
def quote_mask(text):
    mask = bytearray(len(text))
    # paragraph-wise pairing of straight quotes, so one stray quote cannot swallow the file
    pos = 0
    for para in re.split(r"(\n\s*\n)", text):
        qs = [pos + m.start() for m in re.finditer('"', para)]
        for a, b in zip(qs[0::2], qs[1::2]):
            for k in range(a, b + 1):
                mask[k] = 1
        for m in re.finditer("“[^”]*”", para):
            for k in range(pos + m.start(), pos + m.end()):
                mask[k] = 1
        pos += len(para)
    # blockquote lines
    off = 0
    for ln in text.split("\n"):
        if ln.lstrip().startswith(">"):
            for k in range(off, off + len(ln)):
                mask[k] = 1
        off += len(ln) + 1
    return mask


def check_banned(text, starts):
    banned = []
    lines = text.split("\n")
    for m in BANNED_RE.finditer(text):
        ln = line_of(starts, m.start())
        banned.append({"line": ln, "text": m.group(0), "context": lines[ln - 1].strip()[:200]})
    dashes = []
    for m in re.finditer(EM_DASH, text):
        ln = line_of(starts, m.start())
        dashes.append({"line": ln, "col": m.start() - starts[ln - 1] + 1})
    return banned, dashes


def check_overlaps(text, starts, corpus):
    scan = blank_spans(text, URL_RE)
    scan = blank_spans(scan, re.compile(r"<[^>]+>"))
    toks = tokenize(scan)
    mask = quote_mask(text)
    n = len(toks)
    matched = []
    for i in range(n - NGRAM_N + 1):
        gram = " ".join(t for t, _o in toks[i:i + NGRAM_N])
        matched.append(gram in corpus.ngrams)
    out, seen = [], set()
    i = 0
    while i < len(matched):
        if not matched[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(matched) and matched[j + 1]:
            j += 1
        run = toks[i:j + NGRAM_N]
        ngram = " ".join(t for t, _o in run)
        if ngram not in seen:
            seen.add(ngram)
            out.append({
                "ngram": ngram,
                "words": len(run),
                "in_quote": all(mask[o] for _t, o in run),
                "line": line_of(starts, run[0][1]),
            })
        i = j + 1
    return out


def check_urls(text, starts, corpus, brief):
    unknown, seen = [], set()
    for m in URL_RE.finditer(text):
        u = clean_url(m.group(0))
        if not u or u in seen:
            continue
        seen.add(u)
        variants = url_variants(u)
        ok = (
            bool(variants & brief["allowed_urls"])
            or any(u.lower().startswith(w) for w in brief["allowed_wild"])
            or any(v in corpus.raw_lower for v in variants)
        )
        if not ok:
            unknown.append({"url": u, "line": line_of(starts, m.start())})
    return unknown, sorted(seen)


def check_files(text, starts, repo):
    refs = []  # (path, line, kind)

    def add(raw, off, kind):
        raw = raw.strip().strip("<>")
        if not raw or re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", raw, re.IGNORECASE):
            return
        refs.append((raw, line_of(starts, off), kind))

    # markdown images and links
    for m in re.finditer(r'!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)', text):
        add(m.group(1), m.start(1), "image")
    for m in re.finditer(r'(?<!!)\[(?:[^\[\]]|!\[[^\]]*\]\([^)]*\))*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)', text):
        add(m.group(1), m.start(1), "link")
    for m in re.finditer(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", text, re.MULTILINE):
        add(m.group(1), m.start(1), "link")
    # html
    for m in re.finditer(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", text, re.IGNORECASE | re.DOTALL):
        add(m.group(1), m.start(1), "image")
    for m in re.finditer(r"<source\b[^>]*?\bsrcset\s*=\s*[\"']([^\"']+)[\"']", text, re.IGNORECASE | re.DOTALL):
        for part in m.group(1).split(","):
            bits = part.strip().split()
            if bits:
                add(bits[0], m.start(1), "image")
    for m in re.finditer(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"']", text, re.IGNORECASE | re.DOTALL):
        add(m.group(1), m.start(1), "link")

    missing, seen = [], set()
    root = os.path.realpath(repo)
    for raw, ln, kind in refs:
        rel = urllib.parse.unquote(raw.split("#", 1)[0].split("?", 1)[0])
        if not rel:
            continue
        key = (rel, kind)
        if key in seen:
            continue
        seen.add(key)
        target = os.path.realpath(os.path.join(root, rel.lstrip("/")))
        if not target.startswith(root + os.sep) and target != root:
            missing.append({"path": rel, "line": ln, "kind": kind, "reason": "outside repo"})
        elif not os.path.exists(target):
            missing.append({"path": rel, "line": ln, "kind": kind})
    return missing


def check_numbers(text, starts, corpus, brief):
    lits, vals = set(corpus.num_lits), set(corpus.num_vals)
    for cell in brief["numbers_text"]:
        add_numbers(lits, vals, cell)

    scan = blank_spans(text, URL_RE)
    scan = blank_spans(scan, re.compile(r"<[^>]+>"))
    scan = blank_spans(scan, re.compile(r"\]\([^)]*\)"))
    scan = blank_spans(scan, re.compile(r"\b\d{4}-\d{2}-\d{2}\b"))
    scan = blank_spans(scan, re.compile(r"^\s*\d+[.)](?=\s)", re.MULTILINE))

    out, seen = [], set()
    for m in NUM_RE.finditer(scan):
        raw = m.group(0)
        n = norm_num(raw)
        if re.fullmatch(r"\d+", n):
            iv = int(n)
            if iv <= 12:
                continue
            if "," not in raw and not raw.startswith("$") and len(n) == 4 and 1990 <= iv <= 2030:
                if not UNIT_AFTER_RE.match(scan, m.end()):
                    continue
        ln = line_of(starts, m.start())
        key = (raw, ln)
        if key in seen:
            continue
        seen.add(key)
        mm = MULT_AFTER_RE.match(scan, m.end())
        mult = MULT_WORDS[mm.group(1).lower()] if mm else 1.0
        v = num_value(n, mult)
        v1 = num_value(n, 1.0)
        if n in lits or (v is not None and v in vals) or (v1 is not None and v1 in vals):
            continue
        out.append({"number": raw, "normalized": n, "line": ln})
    return out


def check_sections(text, brief):
    secs = split_h2(text)
    titles = [norm_title(t) for t, _b, _i in secs]
    first = {}
    for k, t in enumerate(titles):
        first.setdefault(t, k)
    req = brief["required"]
    missing = [r for r in req if norm_title(r) not in first]
    present = [(r, first[norm_title(r)]) for r in req if norm_title(r) in first]
    # longest increasing subsequence of article positions; the rest are out of order
    pos = [p for _r, p in present]
    best = [1] * len(pos)
    prev = [-1] * len(pos)
    for i in range(len(pos)):
        for j in range(i):
            if pos[j] < pos[i] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    keep = set()
    if pos:
        k = max(range(len(pos)), key=lambda x: best[x])
        while k != -1:
            keep.add(k)
            k = prev[k]
    out_of_order = [present[i][0] for i in range(len(present)) if i not in keep]
    return missing, out_of_order, secs


def check_caps(text, secs, brief):
    over = []
    by_title = {}
    for title, body, _i in secs:
        by_title.setdefault(norm_title(title), count_words(body))
    for name, mx in brief["caps"].items():
        w = by_title.get(norm_title(name))
        if w is not None and w > mx:
            over.append({"section": name, "words": w, "max": mx})
    total = count_words(text)
    if brief["total_cap"] is not None and total > brief["total_cap"]:
        over.append({"section": "TOTAL", "words": total, "max": brief["total_cap"]})
    return over


def http_check(url, opener=None, timeout=10):
    opener = opener or urllib.request.urlopen
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host == "x.com" or host.endswith(".x.com") or host == "twitter.com" or host.endswith(".twitter.com"):
        return {"url": url, "status": "skipped (x.com)"}
    status = None
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": "explainer-checks/1.0"})
        try:
            with opener(req, timeout=timeout) as resp:
                status = resp.status if hasattr(resp, "status") else resp.getcode()
        except urllib.error.HTTPError as e:
            status = e.code
        except Exception as e:  # network errors, timeouts, bad URLs
            status = "error: %s" % (str(e)[:80] or type(e).__name__)
        if isinstance(status, int) and status < 400:
            break
    return {"url": url, "status": status}


# --------------------------------------------------------------------------- skill mode
def parse_frontmatter(text):
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, 0
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, 0
    fm, key = {}, None
    for ln in lines[1:end]:
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", ln)
        if m and not ln.startswith((" ", "\t")):
            key = m.group(1)
            val = m.group(2).strip()
            if val in (">", "|", ">-", "|-", ">+", "|+"):
                val = ""
            fm[key] = val
        elif key and ln.strip():
            fm[key] = (fm[key] + " " + ln.strip()).strip()
    for k, v in list(fm.items()):
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            fm[k] = v[1:-1]
    return fm, end + 1


def run_skill(path):
    res = {"pass": False, "mode": "skill", "file": path, "lines": 0, "words": 0, "banned": [], "em_dashes": [],
           "frontmatter_errors": [], "name": None, "folder": None}
    text = read_text(path) if os.path.isfile(path) else None
    if text is None:
        res["frontmatter_errors"].append("cannot read %s" % path)
        return res
    res["lines"] = len(text.splitlines())
    fm, body_start = parse_frontmatter(text)
    if fm is None:
        res["frontmatter_errors"].append("missing or unterminated YAML frontmatter")
        body_start = 0
        fm = {}
    for key in ("name", "description"):
        if not fm.get(key):
            res["frontmatter_errors"].append("frontmatter missing '%s'" % key)
    folder = os.path.basename(os.path.dirname(os.path.realpath(path)))
    res["name"], res["folder"] = fm.get("name"), folder
    if fm.get("name") and fm["name"] != folder:
        res["frontmatter_errors"].append("name '%s' != parent folder '%s'" % (fm["name"], folder))
    lines = text.split("\n")
    body = "\n".join([""] * body_start + lines[body_start:])  # keep original line numbers
    starts = line_starts(body)
    res["banned"], res["em_dashes"] = check_banned(body, starts)
    res["words"] = count_words("\n".join(lines[body_start:]))
    res["pass"] = not res["frontmatter_errors"] and not res["banned"]
    return res


# --------------------------------------------------------------------------- article mode
def run_article(args):
    warnings = []
    repo = args.repo_dir
    res = {"pass": False, "words": 0, "banned": [], "em_dashes": [], "overlaps": [], "unknown_urls": [],
           "missing_files": [], "unverified_numbers": [], "missing_sections": [], "out_of_order": [],
           "over_cap": [], "http": []}
    readme = os.path.join(repo or "", "README.md")
    text = read_text(readme) if repo and os.path.isfile(readme) else None
    if text is None:
        res["error"] = "cannot read %s" % readme
        return res
    text = text.replace("\r\n", "\n")
    starts = line_starts(text)

    brief = parse_brief(args.brief, warnings)

    corpus = Corpus()
    n_src = n_res = 0
    if args.sources:
        files = walk_files(args.sources, {".txt", ".md"})
        n_src = len(files)
        for f in files:
            corpus.add_file(f)
        if not os.path.isdir(args.sources):
            warnings.append("sources dir not found: %s" % args.sources)
    if args.research:
        files = walk_files(args.research)
        n_res = len(files)
        for f in files:
            corpus.add_file(f)
        if not os.path.isdir(args.research):
            warnings.append("research dir not found: %s" % args.research)
    corpus.finalize()

    res["words"] = count_words(text)
    res["banned"], res["em_dashes"] = check_banned(text, starts)
    res["overlaps"] = check_overlaps(text, starts, corpus)
    res["unknown_urls"], all_urls = check_urls(text, starts, corpus, brief)
    res["missing_files"] = check_files(text, starts, repo)
    res["unverified_numbers"] = check_numbers(text, starts, corpus, brief)
    res["missing_sections"], res["out_of_order"], secs = check_sections(text, brief)
    res["over_cap"] = check_caps(text, secs, brief)
    if args.http:
        res["http"] = [http_check(u) for u in all_urls]

    res["pass"] = not (
        res["banned"]
        or any(not o["in_quote"] for o in res["overlaps"])
        or res["unknown_urls"]
        or res["missing_files"]
        or res["missing_sections"]
    )
    res["em_dash_count"] = len(res["em_dashes"])
    res["corpus_files"] = {"sources": n_src, "research": n_res}
    if warnings:
        res["warnings"] = warnings
    return res


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv):
    p = QuietParser(description=__doc__, add_help=True)
    p.add_argument("repo_dir", nargs="?")
    p.add_argument("--brief")
    p.add_argument("--sources")
    p.add_argument("--research")
    p.add_argument("--http", action="store_true")
    p.add_argument("--skill")
    try:
        args = p.parse_args(argv)
        if args.skill:
            res = run_skill(args.skill)
        elif args.repo_dir:
            res = run_article(args)
        else:
            raise ValueError("repo_dir (or --skill FILE) is required")
    except SystemExit:
        res = {"pass": False, "error": "usage: checks.py <repo_dir> --brief <brief.md> --sources <dir> [--research <dir>] [--http] | --skill <SKILL.md>"}
    except Exception as e:  # never crash: always exit 0 with JSON
        res = {"pass": False, "error": "%s: %s" % (type(e).__name__, e)}
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
