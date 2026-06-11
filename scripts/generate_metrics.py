import os
import base64
import time
from datetime import datetime, timezone
from collections import defaultdict
import requests

TOKEN = os.environ["METRICS_TOKEN"]
USERNAME = os.environ.get("GITHUB_USER", "phillychi3")
OUT_FILE = os.environ.get("METRICS_FILE", "github-metrics.svg")
TZ_LABEL = "Asia/Taipei"
IGNORED = {"HTML", "CSS", "Jupyter Notebook"}

if TOKEN == "none":
    print("No token provided, skipping metrics generation")
    exit(0)

_s = requests.Session()
_s.headers["Authorization"] = f"Bearer {TOKEN}"
_s.headers["User-Agent"] = "metrics-gen/1.0"


def gql(query, **v):
    delay = 1
    for _ in range(5):
        r = _s.post(
            "https://api.github.com/graphql", json={"query": query, "variables": v}
        )

        if r.status_code == 429 or r.status_code == 503:
            wait = int(r.headers.get("retry-after", delay))
            print(f"  Rate limited, waiting {wait}s...")
            time.sleep(wait)
            delay = min(delay * 2, 64)
            continue

        remaining = r.headers.get("x-ratelimit-remaining")
        if remaining == "0":
            reset = int(r.headers.get("x-ratelimit-reset", time.time() + 60))
            wait = max(reset - int(time.time()), 1)
            print(f"  Rate limit exhausted, waiting {wait}s...")
            time.sleep(wait)
            continue

        if r.status_code >= 500:
            print(f"  Server error {r.status_code}, retrying in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 64)
            continue

        r.raise_for_status()
        d = r.json()
        if errs := d.get("errors"):
            raise RuntimeError(errs)
        time.sleep(0.5)  # avoid secondary rate limit between serial requests
        return d["data"]

    raise RuntimeError("Exceeded maximum retries")


def fetch_profile():
    return gql(
        """
      query($l: String!) {
        user(login: $l) {
          name login createdAt
          avatarUrl(size: 80)
          followers { totalCount }
          sponsors  { totalCount }
          repositories(first: 1, ownerAffiliations: OWNER) { totalCount }
          repositoriesContributedTo(
            first: 1,
            contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY, PULL_REQUEST_REVIEW]
          ) { totalCount }
          contributionsCollection {
            totalCommitContributions
            totalPullRequestContributions
            totalPullRequestReviewContributions
            totalIssueContributions
            contributionCalendar {
              weeks { contributionDays { contributionCount color } }
            }
          }
        }
      }
    """,
        l=USERNAME,
    )["user"]


def fetch_repos():
    q_stats = """
      query($l: String!, $c: String) {
        user(login: $l) {
          repositories(
            first: 50, after: $c,
            ownerAffiliations: OWNER, privacy: PUBLIC
          ) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id stargazerCount forkCount diskUsage
              watchers { totalCount }
              releases { totalCount }
              packages { totalCount }
              licenseInfo { spdxId }
            }
          }
        }
      }
    """
    repos, cursor = [], None
    while True:
        p = gql(q_stats, l=USERNAME, c=cursor)["user"]["repositories"]
        repos += p["nodes"]
        if not p["pageInfo"]["hasNextPage"]:
            break
        cursor = p["pageInfo"]["endCursor"]
        print(f"  Stats: {len(repos)} repos fetched...")

    q_langs = """
      query($ids: [ID!]!) {
        nodes(ids: $ids) {
          ... on Repository {
            id
            languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
              edges { size node { name color } }
            }
          }
        }
      }
    """
    lang_map = {}
    batch = 25
    for i in range(0, len(repos), batch):
        ids = [r["id"] for r in repos[i : i + batch]]
        nodes = gql(q_langs, ids=ids)["nodes"]
        for node in nodes:
            if node:
                lang_map[node["id"]] = node["languages"]["edges"]
        print(f"  Languages: {min(i + batch, len(repos))}/{len(repos)} repos...")

    for r in repos:
        r["languages"] = {"edges": lang_map.get(r["id"], [])}

    return repos


def build_metrics(user, repos, avatar_data):
    cc = user["contributionsCollection"]

    all_days = [
        d for w in cc["contributionCalendar"]["weeks"] for d in w["contributionDays"]
    ]
    calendar = all_days[-14:]

    stars = forks = watchers = disk_kb = rels = pkgs = 0
    lang_bytes, lang_color = defaultdict(int), {}
    licenses = defaultdict(int)

    for r in repos:
        stars += r["stargazerCount"]
        forks += r["forkCount"]
        watchers += r["watchers"]["totalCount"]
        disk_kb += r.get("diskUsage") or 0
        rels += r["releases"]["totalCount"]
        pkgs += r["packages"]["totalCount"]
        if r.get("licenseInfo"):
            licenses[r["licenseInfo"]["spdxId"]] += 1
        for edge in r["languages"]["edges"]:
            n = edge["node"]["name"]
            if n not in IGNORED:
                lang_bytes[n] += edge["size"]
                lang_color[n] = edge["node"]["color"] or "#aaa"

    top_lic = max(licenses, key=licenses.get, default=None) or "No license preference"
    total_b = sum(lang_bytes.values()) or 1
    languages = sorted(
        [
            {"name": n, "color": c, "pct": round(lang_bytes[n] / total_b * 100, 1)}
            for n, c in lang_color.items()
        ],
        key=lambda x: -x["pct"],
    )[:10]

    gb = disk_kb / (1024 * 1024)
    disk = f"{gb:.2f} GB" if gb >= 1 else f"{disk_kb / 1024:.1f} MB"

    created = datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))
    years = (datetime.now(timezone.utc) - created).days // 365

    return {
        "name": user["name"] or user["login"],
        "avatar": avatar_data,
        "years": years,
        "followers": user["followers"]["totalCount"],
        "calendar": calendar,
        "contributed": user["repositoriesContributedTo"]["totalCount"],
        "commits": cc["totalCommitContributions"],
        "prs": cc["totalPullRequestContributions"],
        "reviews": cc["totalPullRequestReviewContributions"],
        "issues": cc["totalIssueContributions"],
        "total_repos": user["repositories"]["totalCount"],
        "license": top_lic,
        "releases": rels,
        "packages": pkgs,
        "disk": disk,
        "sponsors": user["sponsors"]["totalCount"],
        "stars": stars,
        "forks": forks,
        "watchers": watchers,
        "languages": languages,
        "lang_count": len(lang_bytes),
    }


ICON_MAP = {
    "clock": "octicon:clock-16",
    "person": "octicon:person-16",
    "repo": "octicon:repo-16",
    "star": "octicon:star-16",
    "fork": "octicon:repo-forked-16",
    "eye": "octicon:eye-16",
    "law": "octicon:law-16",
    "tag": "octicon:tag-16",
    "package": "octicon:package-16",
    "database": "octicon:database-16",
    "heart": "octicon:heart-16",
    "code": "octicon:code-16",
    "activity": "octicon:pulse-16",
    "git-commit": "octicon:git-commit-16",
    "git-review": "octicon:code-review-16",
    "git-pr": "octicon:git-pull-request-16",
    "issue": "octicon:issue-opened-16",
}

_icon_cache: dict[str, tuple[str, int, int]] = {}


def prefetch_icons() -> None:
    by_prefix: dict[str, list[str]] = defaultdict(list)
    for icon_id in ICON_MAP.values():
        prefix, name = icon_id.split(":")
        by_prefix[prefix].append(name)

    for prefix, names in by_prefix.items():
        url = f"https://api.iconify.design/{prefix}.json?icons={','.join(names)}"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        default_w = data.get("width", 16)
        default_h = data.get("height", 16)
        for name, icon_data in data["icons"].items():
            w = icon_data.get("width", default_w)
            h = icon_data.get("height", default_h)
            _icon_cache[f"{prefix}:{name}"] = (icon_data["body"], w, h)


def ico(name, size=16):
    body, w, h = _icon_cache[ICON_MAP[name]]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{size}" height="{size}">{body}</svg>'
    )


CSS = """\
svg{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,\
sans-serif,Apple Color Emoji,Segoe UI Emoji;font-size:14px;color:#777}
h1,h2,h3{margin:8px 0 2px;padding:0;color:#0366d6}
h1{font-size:20px;font-weight:700}h2,h3{font-weight:400}
h1 svg,h2 svg,h3 svg{fill:currentColor}
h2{font-size:16px}h3{font-size:14px}
section>.field{margin-left:5px;margin-right:5px}
.field{display:flex;align-items:center;margin-bottom:2px;white-space:nowrap}
.field svg{margin:0 8px;fill:#959da5;flex-shrink:0}
.row{display:flex;flex-wrap:wrap}.row section{flex:1 1 0}
.column,footer{display:flex;flex-direction:column}
.column{align-items:center}
#metrics-end,.fill-width{width:100%}
.avatar{border-radius:50%;margin:0 6px}
.calendar.field{margin:4px 0 4px 7px}
.calendar .day{outline:1px solid rgba(27,31,35,.04);outline-offset:-1px}
footer{margin-top:8px;font-size:10px;font-style:italic;color:#666;\
text-align:right;justify-content:flex-end;padding:0 4px}
.lang-row{display:flex;align-items:center;margin:1px 0;font-size:12px;white-space:nowrap}
.lang-dot{width:10px;height:10px;border-radius:50%;margin-right:5px;flex-shrink:0}
.lang-pct{color:#888;margin-left:4px}
.lang-name{color:#777}
"""


def calendar_svg(days):
    cells = "".join(
        f'<rect class="day" x="{i * 15}" y="0" width="11" height="11" '
        f'fill="{d["color"]}" rx="2" ry="2"/>'
        for i, d in enumerate(days)
    )
    w = len(days) * 15 - 4
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="calendar" '
        f'width="{w}" height="11">{cells}</svg>'
    )


def lang_col(langs):
    rows = []
    for l in langs:
        rows.append(
            f'<div class="lang-row">'
            f'<span class="lang-dot" style="background:{l["color"]}"></span>'
            f'<span class="lang-name">{l["name"]}</span>'
            f'<span class="lang-pct">{l["pct"]}%</span>'
            f"</div>"
        )
    return "".join(rows)


def generate_svg(m):
    now = datetime.now()
    now = f"{now.day} {now.strftime('%b %Y, %H:%M:%S')}"

    half = (len(m["languages"]) + 1) // 2
    left_l = lang_col(m["languages"][:half])
    right_l = lang_col(m["languages"][half:])

    lang_rows = (len(m["languages"]) + 1) // 2
    height = 310 + max(lang_rows, 1) * 22

    html = f"""\
<section>
  <h1 class="field">
    <img class="avatar" src="{m["avatar"]}" width="40" height="40"/>
    <span>{m["name"]}</span>
  </h1>
  <div class="row">
    <section>
      <div class="field">{ico("clock")} Joined GitHub {m["years"]} year{"s" if m["years"] != 1 else ""} ago</div>
      <div class="field">{ico("person")} Followed by {m["followers"]} users</div>
    </section>
    <section>
      <div class="field calendar">{calendar_svg(m["calendar"])}</div>
      <div class="field">{ico("repo")} Contributed to {m["contributed"]} repositories</div>
    </section>
  </div>
</section>
<section>
  <div class="row">
    <section>
      <h2 class="field">{ico("git-commit")} Activity</h2>
      <div class="field">{ico("git-commit")} {m["commits"]} Commits</div>
      <div class="field">{ico("git-review")} {m["reviews"]} Pull requests reviewed</div>
      <div class="field">{ico("git-pr")} {m["prs"]} Pull requests opened</div>
      <div class="field">{ico("issue")} {m["issues"]} Issues opened</div>
    </section>
    <section>
      <h2 class="field">{ico("repo")} {m["total_repos"]} Repositories</h2>
      <div class="row">
        <section>
          <div class="field">{ico("law")} {m["license"]}</div>
          <div class="field">{ico("tag")} {m["releases"]} Releases</div>
          <div class="field">{ico("package")} {m["packages"]} Packages</div>
          <div class="field">{ico("database")} {m["disk"]} used</div>
        </section>
        <section>
          <div class="field">{ico("heart")} {m["sponsors"]} Sponsors</div>
          <div class="field">{ico("star")} {m["stars"]} Stargazers</div>
          <div class="field">{ico("fork")} {m["forks"]} Forkers</div>
          <div class="field">{ico("eye")} {m["watchers"]} Watchers</div>
        </section>
      </div>
    </section>
  </div>
</section>
<section>
  <h2 class="field">{ico("code")} {m["lang_count"]} Languages</h2>
</section>
<section class="column">
  <h3 class="field">Most used languages</h3>
  <div class="row fill-width">
    <section>{left_l}</section>
    <section>{right_l}</section>
  </div>
</section>
<footer>
  <span>These metrics include private contributions</span>
  <span>Last updated {now} (timezone {TZ_LABEL})</span>
</footer>"""

    return f"""\
<svg xmlns="http://www.w3.org/2000/svg" width="480" height="{height}" class="">
  <defs><style/></defs>
  <style>{CSS}</style>
  <style/>
  <foreignObject x="0" y="0" width="100%" height="100%">
    <div xmlns="http://www.w3.org/1999/xhtml"
         xmlns:xlink="http://www.w3.org/1999/xlink"
         class="items-wrapper">
      {html}
    </div>
    <div xmlns="http://www.w3.org/1999/xhtml" id="metrics-end"></div>
  </foreignObject>
</svg>"""


def main():
    print("Fetching icons from Iconify...")
    prefetch_icons()

    print(f"Fetching profile for {USERNAME}...")
    user = fetch_profile()

    print("Fetching repositories...")
    repos = fetch_repos()
    print(f"  Found {len(repos)} public repos")

    print("Downloading avatar...")
    r = _s.get(user["avatarUrl"])
    r.raise_for_status()
    mime = r.headers.get("content-type", "image/png").split(";")[0]
    avatar = f"data:{mime};base64,{base64.b64encode(r.content).decode()}"

    print("Building metrics...")
    metrics = build_metrics(user, repos, avatar)

    print(
        f"  Stars: {metrics['stars']}  Forks: {metrics['forks']}  "
        f"Watchers: {metrics['watchers']}  Languages: {metrics['lang_count']}"
    )

    print(f"Generating {OUT_FILE}...")
    svg = generate_svg(metrics)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(svg)
    print("Done.")


if __name__ == "__main__":
    main()
