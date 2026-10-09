"""Preset interests and deterministic sports topic classification."""
import hashlib
import json
import re
from pathlib import Path


def load_catalog():
    path = Path(__file__).with_name("interest_catalog.json")
    catalog = json.loads(path.read_text(encoding="utf-8"))
    for key in ("companies", "leagues", "teams"):
        entries = catalog.get(key)
        if not isinstance(entries, list):
            raise ValueError(f"兴趣目录 {key} 应为列表")
        ids = set()
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", entry["id"])
                    or entry["id"] in ids or not isinstance(entry.get("name"), str) or not entry["name"].strip()
                    or not isinstance(entry.get("aliases", []), list)
                    or any(not isinstance(alias, str) or not alias.strip() for alias in entry.get("aliases", []))):
                raise ValueError(f"兴趣目录 {key} 存在无效或重复条目")
            ids.add(entry["id"])
    league_ids = {entry["id"] for entry in catalog["leagues"]}
    if any(team.get("league_id") not in league_ids for team in catalog["teams"]):
        raise ValueError("兴趣目录球队必须属于有效联赛")
    return catalog


def term_matches(text, term):
    term = term.strip()
    if not term:
        return False
    escaped = re.escape(term)
    if re.search(r"[A-Za-z0-9]", term) and not re.search(r"[\u3400-\u9fff]", term):
        escaped = r"(?<![A-Za-z0-9_])" + escaped + r"(?![A-Za-z0-9_])"
    return re.search(escaped, text, re.I) is not None


def watch_team(watch, catalog):
    if watch.get("type") != "team" or catalog is None:
        return None
    names = {name.strip().casefold() for name in [watch["name"], *watch["aliases"]]}
    league_id = watch.get("league_id", "")
    teams = [team for team in catalog["teams"]
             if (not league_id or team["league_id"] == league_id)
             and names.intersection(name.strip().casefold() for name in [team["name"], *team.get("aliases", [])])]
    return teams[0] if len(teams) == 1 else None


def article_topics(article, catalog):
    if article["category"] != "sports":
        return set()
    text = article["title"] + " " + article["summary"]
    topics = set()
    for league in catalog["leagues"]:
        if any(term_matches(text, term) for term in [league["name"], *league.get("aliases", [])]):
            topics.add("league:" + league["id"])
    for team in catalog["teams"]:
        if any(term_matches(text, term) for term in [team["name"], *team.get("aliases", [])]):
            topics.add("team:" + team["id"])
            topics.add("league:" + team["league_id"])
    return topics


def reindex_topics(conn, catalog):
    fingerprint = hashlib.sha256(json.dumps(catalog, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    version = "sports-v2:" + fingerprint
    old = conn.execute("SELECT value FROM metadata WHERE key='topic_index_version'").fetchone()
    if old and old[0] == version:
        return False
    conn.execute("DELETE FROM article_topics")
    for article in conn.execute("SELECT id,title,summary,category FROM articles WHERE category='sports'"):
        conn.executemany("INSERT INTO article_topics(article_id,topic_id) VALUES(?,?)",
                         [(article["id"], topic) for topic in article_topics(article, catalog)])
    conn.execute("INSERT INTO metadata(key,value) VALUES('topic_index_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (version,))
    return True
