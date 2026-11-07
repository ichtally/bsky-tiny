#!/usr/bin/env python3
"""bsky-tiny: a one-file, zero-dependency Bluesky CLI. Python 3.8+, standard library only.

Credentials come from the environment (use an app password, not your main password):
  BSKY_HANDLE        e.g. alice.bsky.social
  BSKY_APP_PASSWORD  from Settings -> Privacy and security -> App passwords
  BSKY_PDS           optional, default https://bsky.social
  BSKY_SESSION_FILE  optional, default ~/.cache/bsky-tiny/session.json (tokens only, chmod 600)

Run `bsky.py -h` for commands. Posts and replies get clickable links, @mentions and #tags.
"""
import argparse, datetime, json, mimetypes, os, re, sys, urllib.error, urllib.parse, urllib.request

__version__ = "0.1.0"
PDS = os.environ.get("BSKY_PDS", "https://bsky.social").rstrip("/") + "/xrpc/"
SESSION_FILE = os.environ.get("BSKY_SESSION_FILE") or os.path.join(
    os.path.expanduser("~"), ".cache", "bsky-tiny", "session.json")
UA = "bsky-tiny/" + __version__


# ---------- HTTP ----------

class BskyError(Exception):
    pass


def xrpc(method, nsid, params=None, body=None, token=None, raw=None, ctype="application/json"):
    url = PDS + nsid
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", ctype)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        raise BskyError("HTTP %s on %s: %s" % (e.code, nsid, e.read().decode(errors="replace")[:300]))


# ---------- session ----------

def _save(s):
    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
    fd = os.open(SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({k: s.get(k) for k in ("did", "handle", "accessJwt", "refreshJwt")}, f)


def login():
    handle = os.environ.get("BSKY_HANDLE", "").lstrip("@")
    if os.path.exists(SESSION_FILE):
        try:
            old = json.load(open(SESSION_FILE))
            if not handle or old.get("handle") == handle:
                s = xrpc("POST", "com.atproto.server.refreshSession", token=old["refreshJwt"], raw=b"")
                _save(s)
                return s
        except (BskyError, ValueError, KeyError):
            pass
    pw = os.environ.get("BSKY_APP_PASSWORD")
    if not handle or not pw:
        sys.exit("set BSKY_HANDLE and BSKY_APP_PASSWORD (an app password) in the environment")
    s = xrpc("POST", "com.atproto.server.createSession", body={"identifier": handle, "password": pw})
    _save(s)
    return s


# ---------- rich text ----------
# Facet offsets are UTF-8 *byte* offsets, which is the usual bug in small clients.
# A leading space is added so a link or mention at position 0 still matches the [$|\W] guard.

URL_RE = re.compile(rb"[$|\W](https?://(www\.)?[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b"
                    rb"([-a-zA-Z0-9()@:%_+.~#?&/=]*[-a-zA-Z0-9@%_+~#/=])?)")
MENTION_RE = re.compile(rb"[$|\W](@(([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
                        rb"[a-zA-Z]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?))")
TAG_RE = re.compile(rb"(?:^|\s)(#[^\s#]+)")
TRAILING = b".,;:!?'\")"


def parse_facets(text, resolve=None):
    """Return facets for links, #tags and (if `resolve` maps handle->did) @mentions."""
    b = b" " + text.encode("utf-8")
    facets = []

    def add(start, end, feature):
        facets.append({"index": {"byteStart": start - 1, "byteEnd": end - 1}, "features": [feature]})

    for m in URL_RE.finditer(b):
        add(m.start(1), m.end(1), {"$type": "app.bsky.richtext.facet#link", "uri": m.group(1).decode()})
    if resolve:
        for m in MENTION_RE.finditer(b):
            did = resolve(m.group(2).decode())
            if did:
                add(m.start(1), m.end(1), {"$type": "app.bsky.richtext.facet#mention", "did": did})
    for m in TAG_RE.finditer(b):
        tag = m.group(1).rstrip(TRAILING)
        name = tag[1:].decode("utf-8", "ignore")
        if not name or name.isdigit() or len(name) > 64:
            continue
        if any(f["index"]["byteStart"] <= m.start(1) - 1 < f["index"]["byteEnd"] for f in facets):
            continue  # '#' inside a URL fragment
        add(m.start(1), m.start(1) + len(tag), {"$type": "app.bsky.richtext.facet#tag", "tag": name})
    return sorted(facets, key=lambda f: f["index"]["byteStart"])


# ---------- helpers ----------

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_handle(handle, token=None):
    try:
        return xrpc("GET", "com.atproto.identity.resolveHandle", {"handle": handle.lstrip("@")}, token=token)["did"]
    except BskyError:
        return None


def to_at_uri(ref, token):
    """Accept at:// URIs or https://bsky.app/profile/<handle-or-did>/post/<rkey> links."""
    if ref.startswith("at://"):
        return ref
    m = re.match(r"https://bsky\.app/profile/([^/]+)/post/([^/?#]+)", ref)
    if not m:
        sys.exit("not a post URI or bsky.app link: " + ref)
    who = m.group(1) if m.group(1).startswith("did:") else resolve_handle(m.group(1), token)
    if not who:
        sys.exit("could not resolve " + m.group(1))
    return "at://%s/app.bsky.feed.post/%s" % (who, m.group(2))


def get_post(uri, token):
    posts = xrpc("GET", "app.bsky.feed.getPosts", {"uris": [uri]}, token=token)["posts"]
    if not posts:
        sys.exit("post not found: " + uri)
    return posts[0]


def read_text(arg):
    if arg == "-":
        return sys.stdin.read().strip()
    return arg


def web_url(uri, handle):
    return "https://bsky.app/profile/%s/post/%s" % (handle, uri.rsplit("/", 1)[-1])


def short(p):
    t = p["record"].get("text", "").replace("\n", " ")
    return "%s  @%s  ♥%s ↩%s\n  %s\n  %s" % (p["indexedAt"][:16].replace("T", " "), p["author"]["handle"],
                                            p.get("likeCount", 0), p.get("replyCount", 0), t,
                                            web_url(p["uri"], p["author"]["handle"]))


def upload_images(paths, alts, token):
    if len(paths) > 4:
        sys.exit("at most 4 images")
    imgs = []
    for i, path in enumerate(paths):
        data = open(path, "rb").read()
        if len(data) > 1_000_000:
            sys.exit("%s is over 1 MB; Bluesky will reject it" % path)
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        blob = xrpc("POST", "com.atproto.repo.uploadBlob", raw=data, token=token, ctype=mime)["blob"]
        imgs.append({"image": blob, "alt": alts[i] if i < len(alts) else ""})
    return {"$type": "app.bsky.embed.images", "images": imgs}


def create_post(s, text, reply=None, images=(), alts=()):
    if len(text) > 300:
        sys.exit("post is %d characters; the limit is 300" % len(text))
    t = s["accessJwt"]
    rec = {"$type": "app.bsky.feed.post", "text": text, "createdAt": now()}
    facets = parse_facets(text, lambda h: resolve_handle(h, t))
    if facets:
        rec["facets"] = facets
    if reply:
        rec["reply"] = reply
    if images:
        rec["embed"] = upload_images(images, alts, t)
    r = xrpc("POST", "com.atproto.repo.createRecord",
             body={"repo": s["did"], "collection": "app.bsky.feed.post", "record": rec}, token=t)
    print(web_url(r["uri"], s["handle"]))
    return r


# ---------- commands ----------

def cmd_whoami(s, a):
    p = xrpc("GET", "app.bsky.actor.getProfile", {"actor": s["did"]}, token=s["accessJwt"])
    if a.json:
        return print(json.dumps(p, indent=1))
    print("@%s  %s\n%s\nfollowers %s · following %s · posts %s" % (
        p["handle"], p.get("displayName", ""), p.get("description", ""),
        p.get("followersCount"), p.get("followsCount"), p.get("postsCount")))


def cmd_post(s, a):
    create_post(s, read_text(a.text), images=a.image or (), alts=a.alt or ())


def cmd_reply(s, a):
    parent = get_post(to_at_uri(a.post, s["accessJwt"]), s["accessJwt"])
    ref = {"uri": parent["uri"], "cid": parent["cid"]}
    root = parent["record"].get("reply", {}).get("root", ref)
    create_post(s, read_text(a.text), reply={"root": root, "parent": ref}, images=a.image or (), alts=a.alt or ())


def cmd_like(s, a):
    p = get_post(to_at_uri(a.post, s["accessJwt"]), s["accessJwt"])
    xrpc("POST", "com.atproto.repo.createRecord", token=s["accessJwt"], body={
        "repo": s["did"], "collection": "app.bsky.feed.like",
        "record": {"$type": "app.bsky.feed.like", "subject": {"uri": p["uri"], "cid": p["cid"]}, "createdAt": now()}})
    print("liked")


def cmd_delete(s, a):
    uri = to_at_uri(a.post, s["accessJwt"])
    repo, coll, rkey = uri[5:].split("/")
    if repo != s["did"]:
        sys.exit("that post isn't yours")
    xrpc("POST", "com.atproto.repo.deleteRecord", token=s["accessJwt"],
         body={"repo": s["did"], "collection": coll, "rkey": rkey})
    print("deleted")


def cmd_follow(s, a):
    did = a.actor if a.actor.startswith("did:") else resolve_handle(a.actor, s["accessJwt"])
    if not did:
        sys.exit("unknown handle " + a.actor)
    xrpc("POST", "com.atproto.repo.createRecord", token=s["accessJwt"], body={
        "repo": s["did"], "collection": "app.bsky.graph.follow",
        "record": {"$type": "app.bsky.graph.follow", "subject": did, "createdAt": now()}})
    print("followed", a.actor)


def _list(items, a, fmt):
    if a.json:
        return print(json.dumps(items, indent=1))
    for it in items:
        print(fmt(it))


def cmd_timeline(s, a):
    r = xrpc("GET", "app.bsky.feed.getTimeline", {"limit": a.n}, token=s["accessJwt"])
    _list([f["post"] for f in r["feed"]], a, short)


def cmd_notifs(s, a):
    r = xrpc("GET", "app.bsky.notification.listNotifications", {"limit": a.n}, token=s["accessJwt"])
    _list(r["notifications"], a, lambda n: "%s  %-8s @%s  %s" % (
        n["indexedAt"][:16].replace("T", " "), n["reason"], n["author"]["handle"],
        (n.get("record", {}).get("text") or "").replace("\n", " ")[:120]))


def cmd_thread(s, a):
    r = xrpc("GET", "app.bsky.feed.getPostThread", {"uri": to_at_uri(a.post, s["accessJwt"]), "depth": a.depth},
             token=s["accessJwt"])["thread"]
    if a.json:
        return print(json.dumps(r, indent=1))

    def show(node, d):
        if "post" in node:
            p = node["post"]
            print("  " * d + "@%s: %s" % (p["author"]["handle"], p["record"].get("text", "").replace("\n", " ")))
        for c in node.get("replies") or []:
            show(c, d + 1)
    show(r, 0)


def cmd_search(s, a):
    r = xrpc("GET", "app.bsky.feed.searchPosts", {"q": a.query, "limit": a.n, "sort": a.sort}, token=s["accessJwt"])
    _list(r["posts"], a, short)


def cmd_people(s, a):
    r = xrpc("GET", "app.bsky.actor.searchActors", {"q": a.query, "limit": a.n}, token=s["accessJwt"])
    _list(r["actors"], a, lambda p: "@%s  %s\n  %s" % (
        p["handle"], p.get("displayName", ""), (p.get("description") or "").replace("\n", " ")[:120]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bsky.py", description="One-file Bluesky CLI.")
    ap.add_argument("--json", action="store_true", help="print raw JSON where it makes sense")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def c(name, fn, help):
        p = sub.add_parser(name, help=help)
        p.set_defaults(fn=fn)
        return p

    c("whoami", cmd_whoami, "your profile and counts")
    for name, fn, h in (("post", cmd_post, "write a post ('-' reads stdin)"),
                        ("reply", cmd_reply, "reply to a post")):
        p = c(name, fn, h)
        if name == "reply":
            p.add_argument("post", help="at:// URI or bsky.app link")
        p.add_argument("text")
        p.add_argument("--image", action="append", help="attach an image (repeat up to 4x)")
        p.add_argument("--alt", action="append", help="alt text, one per --image, same order")
    c("like", cmd_like, "like a post").add_argument("post")
    c("delete", cmd_delete, "delete one of your posts").add_argument("post")
    c("follow", cmd_follow, "follow a handle or DID").add_argument("actor")
    for name, fn, h in (("timeline", cmd_timeline, "home timeline"), ("notifs", cmd_notifs, "notifications")):
        c(name, fn, h).add_argument("-n", type=int, default=20)
    p = c("thread", cmd_thread, "show a post and its replies")
    p.add_argument("post"); p.add_argument("--depth", type=int, default=6)
    p = c("search", cmd_search, "search posts")
    p.add_argument("query"); p.add_argument("-n", type=int, default=15)
    p.add_argument("--sort", choices=("top", "latest"), default="latest")
    p = c("people", cmd_people, "search accounts")
    p.add_argument("query"); p.add_argument("-n", type=int, default=15)

    a = ap.parse_args(argv)
    try:
        a.fn(login(), a)
    except BskyError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
