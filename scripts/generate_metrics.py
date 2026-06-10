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


ICONS = {
    "clock": "M1.5 8a6.5 6.5 0 1113 0 6.5 6.5 0 01-13 0zM8 0a8 8 0 100 16A8 8 0 008 0zm.5 4.75a.75.75 0 00-1.5 0v3.5a.75.75 0 00.471.696l2.5 1a.75.75 0 00.557-1.392L8.5 7.742V4.75z",
    "person": "M10.5 5a2.5 2.5 0 11-5 0 2.5 2.5 0 015 0zm.061 3.073a1 1 0 00-.275 1.398c.185.302.558.63 1.214.63A2.21 2.21 0 0113.5 11.5V13a.5.5 0 01-.5.5H3a.5.5 0 01-.5-.5v-1.5A2.21 2.21 0 014.5 9.1c.657 0 1.029-.328 1.214-.63a1 1 0 00-.275-1.398C4.856 6.758 4 5.728 4 4.5a4 4 0 118 0c0 1.228-.856 2.258-1.439 2.573z",
    "repo": "M2 2.5A2.5 2.5 0 014.5 0h8.75a.75.75 0 01.75.75v12.5a.75.75 0 01-.75.75h-2.5a.75.75 0 110-1.5h1.75v-2h-8a1 1 0 00-.714 1.7.75.75 0 01-1.072 1.05A2.495 2.495 0 012 11.5v-9zm10.5-1V9h-8c-.356 0-.694.074-1 .208V2.5a1 1 0 011-1h8zM5 12.25v3.25a.25.25 0 00.4.2l1.45-1.087a.25.25 0 01.3 0L8.6 15.7a.25.25 0 00.4-.2v-3.25a.25.25 0 00-.25-.25h-3.5a.25.25 0 00-.25.25z",
    "star": "M8 .25a.75.75 0 01.673.418l1.882 3.815 4.21.612a.75.75 0 01.416 1.279l-3.046 2.97.719 4.192a.75.75 0 01-1.088.791L8 12.347l-3.766 1.98a.75.75 0 01-1.088-.79l.72-4.194L.818 6.374a.75.75 0 01.416-1.28l4.21-.611L7.327.668A.75.75 0 018 .25zm0 2.445L6.615 5.5a.75.75 0 01-.564.41l-3.097.45 2.24 2.184a.75.75 0 01.216.664l-.528 3.084 2.769-1.456a.75.75 0 01.698 0l2.77 1.456-.53-3.084a.75.75 0 01.216-.664l2.24-2.183-3.096-.45a.75.75 0 01-.564-.41L8 2.694v.001z",
    "fork": "M5 3.25a.75.75 0 11-1.5 0 .75.75 0 011.5 0zm0 2.122a2.25 2.25 0 10-1.5 0v.878A2.25 2.25 0 005.75 8.5h1.5v2.128a2.251 2.251 0 101.5 0V8.5h1.5a2.25 2.25 0 002.25-2.25v-.878a2.25 2.25 0 10-1.5 0v.878a.75.75 0 01-.75.75h-4.5A.75.75 0 015 6.25v-.878zm3.75 7.378a.75.75 0 11-1.5 0 .75.75 0 011.5 0zm3-8.75a.75.75 0 100-1.5.75.75 0 000 1.5z",
    "eye": "M1.679 7.932c.412-.621 1.242-1.75 2.366-2.717C5.175 4.242 6.527 3.5 8 3.5c1.473 0 2.824.742 3.955 1.715 1.124.967 1.954 2.096 2.366 2.717a.119.119 0 010 .136c-.412.621-1.242 1.75-2.366 2.717C10.825 11.758 9.473 12.5 8 12.5c-1.473 0-2.824-.742-3.955-1.715C2.92 9.818 2.09 8.69 1.679 8.068a.119.119 0 010-.136zM8 2c-1.981 0-3.67.992-4.933 2.078C1.797 5.169.88 6.423.43 7.1a1.619 1.619 0 000 1.798c.45.678 1.367 1.932 2.637 3.024C4.329 13.008 6.019 14 8 14c1.981 0 3.67-.992 4.933-2.078 1.27-1.091 2.187-2.345 2.637-3.023a1.619 1.619 0 000-1.798c-.45-.678-1.367-1.932-2.637-3.023C11.671 2.992 9.981 2 8 2zm0 8a2 2 0 100-4 2 2 0 000 4z",
    "law": "M8.75.75a.75.75 0 00-1.5 0V2h-.984c-.305 0-.604.08-.869.23l-1.288.737A.25.25 0 013.984 3H1.75a.75.75 0 000 1.5h.428L.066 9.192a.75.75 0 00.154.838l.53-.53-.53.53v.001l.002.002.002.002.006.006.016.015.045.04a3.514 3.514 0 00.686.45A4.492 4.492 0 003 11c.88 0 1.556-.22 2.023-.454a3.515 3.515 0 00.686-.45l.045-.04.016-.015.006-.006.002-.002.001-.002L5.25 9.5l.53.53a.75.75 0 00.154-.838L3.822 4.5h.162c.305 0 .604-.08.869-.23l1.289-.737a.25.25 0 01.124-.033h.984V13h-2.5a.75.75 0 000 1.5h6.5a.75.75 0 000-1.5h-2.5V3.5h.984a.25.25 0 01.124.033l1.29.736c.264.152.563.231.868.231h.162l-2.112 4.692a.75.75 0 00.154.838l.53-.53-.53.53v.001l.002.002.002.002.006.006.016.015.045.04a3.517 3.517 0 00.686.45A4.492 4.492 0 0013 11c.88 0 1.556-.22 2.023-.454a3.512 3.512 0 00.686-.45l.045-.04.01-.01.006-.005.006-.006.002-.002.001-.002-.529-.531.53.53a.75.75 0 00.154-.838L13.823 4.5h.427a.75.75 0 000-1.5h-2.234a.25.25 0 01-.124-.033l-1.29-.736A1.75 1.75 0 009.735 2H8.75V.75zM1.695 9.227c.285.135.718.273 1.305.273s1.02-.138 1.305-.273L3 6.327l-1.305 2.9zm10 0c.285.135.718.273 1.305.273s1.02-.138 1.305-.273L13 6.327l-1.305 2.9z",
    "tag": "M2.5 7.775V2.75a.25.25 0 01.25-.25h5.025a.25.25 0 01.177.073l6.25 6.25a.25.25 0 010 .354l-5.025 5.025a.25.25 0 01-.354 0l-6.25-6.25a.25.25 0 01-.073-.177zm-1.5 0V2.75C1 1.784 1.784 1 2.75 1h5.025c.464 0 .91.184 1.238.513l6.25 6.25a1.75 1.75 0 010 2.474l-5.026 5.026a1.75 1.75 0 01-2.474 0l-6.25-6.25A1.75 1.75 0 011 7.775zM6 5a1 1 0 100 2 1 1 0 000-2z",
    "package": "M8.878.392a1.75 1.75 0 00-1.756 0l-5.25 3.045A1.75 1.75 0 001 4.951v6.098c0 .624.332 1.2.872 1.514l5.25 3.045a1.75 1.75 0 001.756 0l5.25-3.045c.54-.313.872-.89.872-1.514V4.951c0-.624-.332-1.2-.872-1.514L8.878.392zM7.875 1.69a.25.25 0 01.25 0l4.63 2.685L8 7.133 3.245 4.375l4.63-2.685zM2.5 5.677v5.372c0 .09.047.171.125.216l4.625 2.683V8.432L2.5 5.677zm6.25 8.271l4.625-2.683a.25.25 0 00.125-.216V5.677L8.75 8.432v5.516z",
    "database": "M12 21q-3.775 0-6.387-1.162T3 17V7q0-1.65 2.638-2.825T12 3t6.363 1.175T21 7v10q0 1.675-2.613 2.838T12 21m0-11.975q2.225 0 4.475-.638T19 7.025q-.275-.725-2.512-1.375T12 5q-2.275 0-4.462.638T5 7.025q.35.75 2.538 1.375T12 9.025M12 14q1.05 0 2.025-.1t1.863-.288t1.675-.462T19 12.525v-3q-.65.35-1.437.625t-1.675.463t-1.863.287T12 11t-2.05-.1t-1.888-.288T6.4 10.15T5 9.525v3q.625.35 1.4.625t1.663.463t1.887.287T12 14m0 5q1.15 0 2.338-.175t2.187-.462t1.675-.65t.8-.738v-2.45q-.65.35-1.437.625t-1.675.463t-1.863.287T12 16t-2.05-.1t-1.888-.288T6.4 15.15T5 14.525V17q.125.375.788.725t1.662.638t2.2.462T12 19",
    "heart": "m12.1 18.55l-.1.1l-.11-.1C7.14 14.24 4 11.39 4 8.5C4 6.5 5.5 5 7.5 5c1.54 0 3.04 1 3.57 2.36h1.86C13.46 6 14.96 5 16.5 5c2 0 3.5 1.5 3.5 3.5c0 2.89-3.14 5.74-7.9 10.05M16.5 3c-1.74 0-3.41.81-4.5 2.08C10.91 3.81 9.24 3 7.5 3C4.42 3 2 5.41 2 8.5c0 3.77 3.4 6.86 8.55 11.53L12 21.35l1.45-1.32C18.6 15.36 22 12.27 22 8.5C22 5.41 19.58 3 16.5 3",
    "code": "m8 18l-6-6l6-6l1.425 1.425l-4.6 4.6L9.4 16.6zm8 0l-1.425-1.425l4.6-4.6L14.6 7.4L16 6l6 6z",
    "activity": "M3 12h4l3 8l4-16l3 8h4",
    "git-commit": "M8.813 15.863Q7.45 14.725 7.1 13H2v-2h5.1q.35-1.725 1.713-2.863T12 7t3.188 1.138T16.9 11H22v2h-5.1q-.35 1.725-1.712 2.863T12 17t-3.187-1.137M12 15q1.25 0 2.125-.875T15 12t-.875-2.125T12 9t-2.125.875T9 12t.875 2.125T12 15",
    "git-review": "M8 3.5a1 1 0 1 0 0-2a1 1 0 0 0 0 2M8 0a2.5 2.5 0 0 1 2.45 2H13a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1h2.55A2.5 2.5 0 0 1 8 0M7 5h3.5V3.5h2v11h-9v-11h2V5zm3.53 3.28a.75.75 0 1 0-1.06-1.06L7.5 9.19l-.47-.47a.75.75 0 0 0-1.06 1.06l1 1a.75.75 0 0 0 1.06 0z",
    "git-pr": "m8.5.5l-2 2m0 0l2 2m-2-2h3a3 3 0 0 1 3 3v2m-10 3a2 2 0 1 0 0 4a2 2 0 0 0 0-4Zm0 0v-6m0 0a2 2 0 1 0 0-4a2 2 0 0 0 0 4Zm10 3a2 2 0 1 0 0 4a2 2 0 0 0 0-4Z",
    "issue": "M12 20a8 8 0 1 0 0-16a8 8 0 0 0 0 16m0 2C6.477 22 2 17.523 2 12S6.477 2 12 2s10 4.477 10 10s-4.477 10-10 10m0-8a2 2 0 1 1 0-4a2 2 0 0 1 0 4",
}


def ico(name, size=16):
    d = ICONS[name]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" '
        f'width="{size}" height="{size}"><path fill-rule="evenodd" d="{d}"/></svg>'
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
