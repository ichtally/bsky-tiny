#!/usr/bin/env python3
"""bsky-tiny: a one-file, zero-dependency Bluesky CLI. Python 3.8+, standard library only.

Credentials come from the environment (use an app password, not your main password):
  BSKY_HANDLE        e.g. alice.bsky.social
  BSKY_APP_PASSWORD  from Settings -> Privacy and security -> App passwords
  BSKY_PDS           optional, default https://bsky.social
  BSKY_SESSION_FILE  optional, default ~/.cache/bsky-tiny/session.json (tokens only, chmod 600)

Run `bsky.py -h` for commands. Posts and replies get clickable links, @mentions and #tags.
"""
import argparse, datetime, json, mimetypes, os, re, sys, unicodedata, urllib.error, urllib.parse, urllib.request

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
# This is a port of the official detector (@atproto/api v0.30.3, rich-text/detection.ts + util.ts),
# quirks included, and is tested against its real output (see facet-check's expected-atproto.json):
# bare domains link only if the last label is a real TLD ("readme.md" links, "notes.txt" doesn't),
# the TLD check is case-sensitive ("Example.COM" doesn't link), and $CASHTAGS become tag facets.
# JS regex semantics are reproduced explicitly: JS \s is spelled out, \b/\d are ASCII, \p{P} uses
# unicodedata. Known gap: tags over 64 UTF-16 units use an approximate grapheme count.

_WS = "\t\n\v\f\r    -     　﻿"  # = JS \s
_ZW = "­⁠ ​‌‍⃢"
MENTION_RE = re.compile(r"(^|[%s]|\()(@)([a-zA-Z0-9.-]+)(\b)" % _WS, re.ASCII)
URL_RE = re.compile(r"(^|[%s]|\()((https?://[^%s]+)|((?P<domain>[a-z][a-z0-9]*(\.[a-z0-9]+)+)[^%s]*))"
                    % (_WS, _WS, _WS), re.I | re.M | re.ASCII)
TAG_RE = re.compile(r"(^|[%s])[#＃]([^%s%s]*)" % (_WS, _WS, _ZW))
CASHTAG_RE = re.compile(r"(^|[%s]|\()\$([A-Za-z][A-Za-z0-9]{0,4})(?=[%s]|\Z|[.,;:!?)\"'’])" % (_WS, _WS))


def _is_punct(c):
    return unicodedata.category(c).startswith("P")


def _valid_domain(d):
    return "." in d and d.rsplit(".", 1)[1] in TLDS


def _grapheme_len(s):  # approximation: combining marks, ZWJ joins, modifiers and RI pairs don't count
    n, prev_zwj, ri = 0, False, 0
    for c in s:
        cat, o = unicodedata.category(c), ord(c)
        if cat.startswith("M") or c == "‍" or 0x1F3FB <= o <= 0x1F3FF or prev_zwj:
            prev_zwj = c == "‍"
            continue
        if 0x1F1E6 <= o <= 0x1F1FF:
            ri += 1
            if ri % 2 == 0:
                continue
        n += 1
    return n


def parse_facets(text, resolve=None):
    """Return facets for links, #tags, $cashtags and (if `resolve` maps handle->did) @mentions."""
    spans = []  # (start, end, feature) in str indices
    if resolve:
        for m in MENTION_RE.finditer(text):
            if not _valid_domain(m.group(3)) and not m.group(3).endswith(".test"):
                continue  # probably not a handle
            did = resolve(m.group(3))
            if did:
                spans.append((m.start(2), m.end(3), {"$type": "app.bsky.richtext.facet#mention", "did": did}))
    for m in URL_RE.finditer(text):
        uri, start, end = m.group(2), m.start(2), m.end(2)
        if not uri.startswith("http"):
            if not m.group("domain") or not _valid_domain(m.group("domain")):
                continue
            uri = "https://" + uri
        if uri[-1] in ".,;:!?":
            uri, end = uri[:-1], end - 1
        if uri.endswith(")") and "(" not in uri:
            uri, end = uri[:-1], end - 1
        spans.append((start, end, {"$type": "app.bsky.richtext.facet#link", "uri": uri}))
    for m in TAG_RE.finditer(text):
        tag = m.group(2)
        # official: (?!️) first, and at least one char that is neither an ASCII digit nor punctuation
        if not tag or tag[0] == "️" or all(c in "0123456789" or _is_punct(c) for c in tag):
            continue
        while tag and _is_punct(tag[-1]):
            tag = tag[:-1]
        u16 = len(tag.encode("utf-16-le")) // 2
        if not tag or (u16 > 64 and _grapheme_len(tag) > 64):
            continue
        spans.append((m.start(1) + len(m.group(1)), m.start(2) + len(tag),
                      {"$type": "app.bsky.richtext.facet#tag", "tag": tag}))
    for m in CASHTAG_RE.finditer(text):
        spans.append((m.end(1), m.end(2), {"$type": "app.bsky.richtext.facet#tag", "tag": "$" + m.group(2).upper()}))

    def byte(i):
        return len(text[:i].encode("utf-8"))

    spans.sort(key=lambda s: s[0])
    return [{"index": {"byteStart": byte(s), "byteEnd": byte(e)}, "features": [f]} for s, e, f in spans]


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


# ASCII entries of the `tlds` npm package v1.261.0, which @atproto/api uses for bare-domain links.
TLDS = frozenset("""
aaa aarp abb abbott abbvie abc able abogado abudhabi ac academy accenture accountant accountants aco
actor ad ads adult ae aeg aero aetna af afl africa ag agakhan agency ai aig airbus airforce airtel
akdn al alibaba alipay allfinanz allstate ally alsace alstom am amazon americanexpress
americanfamily amex amfam amica amsterdam analytics android anquan anz ao aol apartments app apple
aq aquarelle ar arab aramco archi army arpa art arte as asda asia associates at athleta attorney au
auction audi audible audio auspost author auto autos aw aws ax axa az azure ba baby baidu banamex
band bank bar barcelona barclaycard barclays barefoot bargains baseball basketball bauhaus bayern bb
bbc bbt bbva bcg bcn bd be beats beauty beer berlin best bestbuy bet bf bg bh bharti bi bible bid
bike bing bingo bio biz bj black blackfriday blockbuster blog bloomberg blue bm bms bmw bn
bnpparibas bo boats boehringer bofa bom bond boo book booking bosch bostik boston bot boutique box
br bradesco bridgestone broadway broker brother brussels bs bt build builders business buy buzz bv
bw by bz bzh ca cab cafe cal call calvinklein cam camera camp canon capetown capital capitalone car
caravan cards care career careers cars casa case cash casino cat catering catholic cba cbn cbre cc
cd center ceo cern cf cfa cfd cg ch chanel channel charity chase chat cheap chintai christmas chrome
church ci cipriani circle cisco citadel citi citic city ck cl claims cleaning click clinic clinique
clothing cloud club clubmed cm cn co coach codes coffee college cologne com commbank community
company compare computer comsec condos construction consulting contact contractors cooking cool coop
corsica country coupon coupons courses cpa cr credit creditcard creditunion cricket crown crs cruise
cruises cu cuisinella cv cw cx cy cymru cyou cz dad dance data date dating datsun day dclk dds de
deal dealer deals degree delivery dell deloitte delta democrat dental dentist desi design dev dhl
diamonds diet digital direct directory discount discover dish diy dj dk dm dnp do docs doctor dog
domains dot download drive dtv dubai dupont durban dvag dvr dz earth eat ec eco edeka edu education
ee eg email emerck energy engineer engineering enterprises epson equipment er ericsson erni es esq
estate et eu eurovision eus events exchange expert exposed express extraspace fage fail fairwinds
faith family fan fans farm farmers fashion fast fedex feedback ferrari ferrero fi fidelity fido film
final finance financial fire firestone firmdale fish fishing fit fitness fj fk flickr flights flir
florist flowers fly fm fo foo food football ford forex forsale forum foundation fox fr free
fresenius frl frogans frontier ftr fujitsu fun fund furniture futbol fyi ga gal gallery gallo gallup
game games gap garden gay gb gbiz gd gdn ge gea gent genting george gf gg ggee gh gi gift gifts
gives giving gl glass gle global globo gm gmail gmbh gmo gmx gn godaddy gold goldpoint golf goo
goodyear goog google gop got gov gp gq gr grainger graphics gratis green gripe grocery group gs gt
gu gucci guge guide guitars guru gw gy hair hamburg hangout haus hbo hdfc hdfcbank health healthcare
help helsinki here hermes hiphop hisamitsu hitachi hiv hk hkt hm hn hockey holdings holiday
homedepot homegoods homes homesense honda horse hospital host hosting hot hotels hotmail house how
hr hsbc ht hu hughes hyatt hyundai ibm icbc ice icu id ie ieee ifm ikano il im imamat imdb immo
immobilien in inc industries infiniti info ing ink institute insurance insure int international
intuit investments io ipiranga iq ir irish is ismaili ist istanbul it itau itv jaguar java jcb je
jeep jetzt jewelry jio jll jm jmp jnj jo jobs joburg jot joy jp jpmorgan jprs juegos juniper kaufen
kddi ke kerryhotels kerryproperties kfh kg kh ki kia kids kim kindle kitchen kiwi km kn koeln
komatsu kosher kp kpmg kpn kr krd kred kuokgroup kw ky kyoto kz la lacaixa lamborghini lamer land
landrover lanxess lasalle lat latino latrobe law lawyer lb lc lds lease leclerc lefrak legal lego
lexus lgbt li lidl life lifeinsurance lifestyle lighting like lilly limited limo lincoln link live
living lk llc llp loan loans locker locus lol london lotte lotto love lpl lplfinancial lr ls lt ltd
ltda lu lundbeck luxe luxury lv ly ma madrid maif maison makeup man management mango map market
marketing markets marriott marshalls mattel mba mc mckinsey md me med media meet melbourne meme
memorial men menu merckmsd mg mh miami microsoft mil mini mint mit mitsubishi mk ml mlb mls mm mma
mn mo mobi mobile moda moe moi mom monash money monster mormon mortgage moscow moto motorcycles mov
movie mp mq mr ms msd mt mtn mtr mu museum music mv mw mx my mz na nab nagoya name navy nba nc ne
nec net netbank netflix network neustar new news next nextdirect nexus nf nfl ng ngo nhk ni nico
nike nikon ninja nissan nissay nl no nokia norton now nowruz nowtv np nr nra nrw ntt nu nyc nz obi
observer office okinawa olayan olayangroup ollo om omega one ong onl online ooo open oracle orange
org organic origins osaka otsuka ott ovh pa page panasonic paris pars partners parts party pay pccw
pe pet pf pfizer pg ph pharmacy phd philips phone photo photography photos physio pics pictet
pictures pid pin ping pink pioneer pizza pk pl place play playstation plumbing plus pm pn pnc pohl
poker politie porn post pr praxi press prime pro prod productions prof progressive promo properties
property protection pru prudential ps pt pub pw pwc py qa qpon quebec quest racing radio re read
realestate realtor realty recipes red redumbrella rehab reise reisen reit reliance ren rent rentals
repair report republican rest restaurant review reviews rexroth rich richardli ricoh ril rio rip ro
rocks rodeo rogers room rs rsvp ru rugby ruhr run rw rwe ryukyu sa saarland safe safety sakura sale
salon samsclub samsung sandvik sandvikcoromant sanofi sap sarl sas save saxo sb sbi sbs sc scb
schaeffler schmidt scholarships school schule schwarz science scot sd se search seat secure security
seek select sener services seven sew sex sexy sfr sg sh shangrila sharp shell shia shiksha shoes
shop shopping shouji show si silk sina singles site sj sk ski skin sky skype sl sling sm smart smile
sn sncf so soccer social softbank software sohu solar solutions song sony soy spa space sport spot
sr srl ss st stada staples star statebank statefarm stc stcgroup stockholm storage store stream
studio study style su sucks supplies supply support surf surgery suzuki sv swatch swiss sx sy sydney
systems sz tab taipei talk taobao target tatamotors tatar tattoo tax taxi tc tci td tdk team tech
technology tel temasek tennis teva tf tg th thd theater theatre tiaa tickets tienda tips tires tirol
tj tjmaxx tjx tk tkmaxx tl tm tmall tn to today tokyo tools top toray toshiba total tours town
toyota toys tr trade trading training travel travelers travelersinsurance trust trv tt tube tui
tunes tushu tv tvs tw tz ua ubank ubs ug uk unicom university uno uol ups us uy uz va vacations vana
vanguard vc ve vegas ventures verisign versicherung vet vg vi viajes video vig viking villas vin vip
virgin visa vision viva vivo vlaanderen vn vodka volvo vote voting voto voyage vu wales walmart
walter wang wanggou watch watches weather weatherchannel webcam weber website wed wedding weibo weir
wf whoswho wien wiki williamhill win windows wine winners wme wolterskluwer woodside work works
world wow ws wtc wtf xbox xerox xihuan xin xxx xyz yachts yahoo yamaxun yandex ye yodobashi yoga
yokohama you youtube yt yun za zappos zara zero zip zm zone zuerich zw
""".split())


if __name__ == "__main__":
    main()
