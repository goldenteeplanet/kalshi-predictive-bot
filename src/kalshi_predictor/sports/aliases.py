from __future__ import annotations

from collections.abc import Mapping

STATIC_TEAM_ALIASES: dict[tuple[str, str], tuple[str, ...]] = {
    ("MLB", "chicago cubs"): ("chicago c", "chi cubs", "chc"),
    ("MLB", "chicago white sox"): ("chicago ws", "chi white sox", "chisox", "chw"),
    ("MLB", "kansas city royals"): ("kansas city", "kc royals", "kc"),
    ("MLB", "texas rangers"): ("texas", "texas r", "texas rng", "tx rangers"),
    ("MLB", "baltimore orioles"): ("baltimore", "bal orioles", "bal"),
    ("MLB", "los angeles dodgers"): ("la dodgers", "lad", "l.a. dodgers"),
    ("MLB", "new york yankees"): ("ny yankees", "nyy", "yanks"),
    ("SOCCER", "manchester city"): ("man city", "mancity", "mcfc"),
    ("SOCCER", "manchester united"): ("man utd", "man united", "mufc"),
    ("SOCCER", "tottenham hotspur"): ("tottenham", "spurs"),
    ("SOCCER", "paris saint-germain"): ("psg", "paris sg"),
    ("SOCCER", "internazionale"): ("inter", "inter milan"),
    ("SOCCER", "inter milan"): ("inter", "internazionale"),
    ("SOCCER", "ac milan"): ("milan", "a.c. milan"),
    ("SOCCER", "real madrid"): ("real madrid", "real"),
    ("SOCCER", "atletico madrid"): ("atletico", "atletico madrid", "atlético madrid"),
    ("SOCCER", "bayern munich"): ("bayern", "fc bayern"),
    ("SOCCER", "borussia dortmund"): ("dortmund", "bvb"),
    ("SOCCER", "inter miami cf"): ("inter miami", "miami"),
    ("SOCCER", "los angeles fc"): ("lafc", "la fc"),
    ("SOCCER", "la galaxy"): ("galaxy", "l.a. galaxy"),
    ("SOCCER", "new york city fc"): ("nycfc", "ny city fc"),
    ("SOCCER", "sporting kansas city"): ("sporting kc", "skc"),
}


def supplemental_team_aliases(
    payload: Mapping[str, object],
    *,
    team_key: str | None = None,
) -> list[str]:
    league = _league_from_payload(payload, team_key=team_key)
    names = {
        _clean_alias(payload.get(key))
        for key in ("team_name", "display_name", "displayName", "name", "team")
    }
    raw_key = str(team_key or payload.get("team_key") or payload.get("key") or "").strip()
    if raw_key:
        names.add(_clean_alias(raw_key.split(":", 1)[-1].replace("-", " ")))
    aliases: list[str] = []
    for name in names:
        if not name:
            continue
        aliases.extend(STATIC_TEAM_ALIASES.get((league, name), ()))
    return _dedupe(aliases)


def canonical_alias_suggestions(
    *,
    league: str,
    team_name: str,
    existing_aliases: list[str],
) -> list[str]:
    expected = STATIC_TEAM_ALIASES.get((league.upper(), _clean_alias(team_name)), ())
    existing = {_clean_alias(alias) for alias in existing_aliases}
    return [alias for alias in expected if _clean_alias(alias) not in existing]


def _league_from_payload(payload: Mapping[str, object], *, team_key: str | None) -> str:
    explicit = str(payload.get("league") or "").strip().upper()
    if explicit:
        return explicit
    raw_key = str(team_key or payload.get("team_key") or payload.get("key") or "").strip()
    if ":" in raw_key:
        return raw_key.split(":", 1)[0].upper()
    return ""


def _clean_alias(value: object) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _dedupe(values: list[str]) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_alias(value)
        if len(text) >= 2 and text not in seen:
            aliases.append(text)
            seen.add(text)
    return aliases
