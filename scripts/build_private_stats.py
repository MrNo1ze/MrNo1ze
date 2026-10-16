#!/usr/bin/env python3
"""Собирает SVG и JSON приватной статистики, обновляет версию картинки в README.

STATS_TOKEN — PAT владельца: все репозитории, Metadata: Read-only (fine-grained)
или scope repo (classic). GITHUB_TOKEN поддержан для локального запуска с PAT.
Публикуются только агрегаты, без названий и адресов репозиториев.

    python3 scripts/build_private_stats.py --self-check
    STATS_TOKEN=... python3 scripts/build_private_stats.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent.parent


class Language(TypedDict):
    bytes: int
    repositories: int


class Stats(TypedDict):
    generated_at: str
    total: int
    archived: int
    forks: int
    pushed_30d: int
    without_languages: int
    coverage_verified: bool
    languages: dict[str, Language]


LANG_COLORS = {
    "Go": "#7dcfff", "TypeScript": "#7aa2f7", "JavaScript": "#e0af68",
    "HTML": "#ff9e64", "Dart": "#bb9af7", "CSS": "#9d7cd8",
    "Python": "#9ece6a", "Shell": "#89ddff", "Rust": "#f7768e",
    "Dockerfile": "#41a6b5", "Makefile": "#c0caf5", "SQL": "#73daca",
    "Vue": "#4fd6be", "Svelte": "#ff757f", "PHP": "#a78bfa",
    "Ruby": "#f43f5e", "Java": "#fbbf24", "Kotlin": "#c084fc",
    "Swift": "#fb923c", "C": "#a9b1d6", "C++": "#60a5fa",
    "HCL": "#a78bfa", "SCSS": "#f472b6", "CMake": "#86efac",
}
FALLBACK = ("#7dcfff", "#9ece6a", "#e0af68", "#bb9af7", "#73daca", "#f7768e")


def api(path: str, token: str) -> tuple[object, dict[str, str]]:
    url = path if path.startswith("https://") else API + path
    if urlsplit(url).scheme != "https" or urlsplit(url).netloc != "api.github.com":
        raise ValueError("Недопустимый адрес GitHub API")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "MrNo1ze-private-stats",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp), {key.lower(): value for key, value in resp.headers.items()}


def next_link(headers: dict[str, str]) -> str | None:
    for key, value in headers.items():
        if key.lower() == "link":
            for part in value.split(","):
                if 'rel="next"' in part:
                    return part.split(";")[0].strip().strip("<>")
    return None


def collect(token: str) -> Stats:
    user, _ = api("/user", token)
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "MrNo1ze")
    if not isinstance(user, dict) or str(user.get("login", "")).casefold() != owner.casefold():
        raise ValueError("STATS_TOKEN должен принадлежать владельцу профиля")

    repos: list[dict[str, object]] = []
    url: str | None = "/user/repos?per_page=100&affiliation=owner&visibility=private"
    seen_pages: set[str] = set()
    while url:
        if url in seen_pages:
            raise ValueError("GitHub API повторил страницу репозиториев")
        seen_pages.add(url)
        page, headers = api(url, token)
        if not isinstance(page, list) or not all(isinstance(repo, dict) for repo in page):
            raise ValueError("Некорректный список репозиториев от GitHub API")
        repos.extend(page)
        url = next_link(headers)

    expected = user.get("owned_private_repos")
    if not repos or (isinstance(expected, int) and expected != len(repos)):
        raise ValueError("Неполная выборка приватных репозиториев: проверь доступ STATS_TOKEN ко всем репозиториям")

    now = datetime.now(timezone.utc)
    languages: dict[str, Language] = {}
    archived = forks = pushed = without_languages = 0
    seen_repos: set[str] = set()
    for repo in repos:
        full_name = repo.get("full_name")
        if (not isinstance(full_name, str) or full_name in seen_repos
                or full_name.split("/")[0].casefold() != owner.casefold()
                or repo.get("private") is not True):
            raise ValueError("Некорректная выборка приватных репозиториев владельца")
        seen_repos.add(full_name)
        is_archived, is_fork = repo.get("archived"), repo.get("fork")
        if not isinstance(is_archived, bool) or not isinstance(is_fork, bool):
            raise ValueError("GitHub API не вернул статус репозитория")
        archived += is_archived
        forks += is_fork
        pushed_at = repo.get("pushed_at")
        if pushed_at is not None:
            if not isinstance(pushed_at, str):
                raise ValueError("GitHub API вернул некорректную дату push")
            pushed += now - timedelta(days=30) <= datetime.fromisoformat(pushed_at.replace("Z", "+00:00")) <= now

        data, _ = api(f"/repos/{full_name}/languages", token)
        if not isinstance(data, dict):
            raise ValueError("Некорректная статистика языков от GitHub API")
        has_languages = False
        for name, size in data.items():
            if not isinstance(name, str) or type(size) is not int or size < 0:
                raise ValueError("Некорректный размер языка от GitHub API")
            if size:
                language = languages.setdefault(name, {"bytes": 0, "repositories": 0})
                language["bytes"] += size
                language["repositories"] += 1
                has_languages = True
        without_languages += not has_languages

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": len(repos), "archived": archived, "forks": forks,
        "pushed_30d": pushed, "without_languages": without_languages,
        "coverage_verified": isinstance(expected, int),
        "languages": dict(sorted(languages.items(), key=lambda item: (-item[1]["bytes"], item[0]))),
    }


def human_bytes(size: int) -> str:
    for unit, step in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if size >= step:
            return f"{size / step:.2f} {unit}"
    return f"{size} B"


def language_color(name: str) -> str:
    return LANG_COLORS.get(name, FALLBACK[int(hashlib.sha256(name.encode()).hexdigest()[:4], 16) % len(FALLBACK)])


def render(stats: Stats) -> str:
    languages = sorted(stats["languages"].items(), key=lambda item: (-item[1]["bytes"], item[0]))
    total_bytes = sum(language["bytes"] for _, language in languages)
    per_column = max(1, (len(languages) + 1) // 2)
    footer_y = 286 + per_column * 52
    height = footer_y + 64
    updated = datetime.fromisoformat(stats["generated_at"]).astimezone(ZoneInfo("Europe/Moscow"))
    metrics = (
        ("РЕПОЗИТОРИИ", str(stats["total"]), f'{stats["total"] - stats["archived"]} вне архива · {stats["archived"]} в архиве'),
        ("ОБЪЁМ КОДА", human_bytes(total_bytes), f'{total_bytes:,} байт'.replace(",", " ")),
        ("ЯЗЫКИ", str(len(languages)), "GitHub Linguist · по байтам"),
    )
    tiles = []
    for i, (label, value, detail) in enumerate(metrics):
        x = 28 + i * 264
        tiles.append(f'''  <g transform="translate({x} 84)">
    <rect width="256" height="100" rx="12" fill="#1c2030" stroke="#30374e"/>
    <text x="16" y="24" class="eyebrow">{label}</text>
    <text x="16" y="61" class="value">{value}</text>
    <text x="16" y="84" class="muted">{detail}</text>
  </g>''')

    segments, entries, cursor = [], [], 0
    for i, (name, language) in enumerate(languages):
        size, count = language["bytes"], language["repositories"]
        ratio = size / total_bytes
        color = language_color(name)
        x, y = 28 + (i // per_column) * 408, 280 + (i % per_column) * 52
        percent = f"{ratio * 100:.2f}%" if ratio >= 0.0001 else "&lt;0.01%"
        text_fit = ' textLength="270" lengthAdjust="spacingAndGlyphs"' if len(name) > 25 else ""
        segments.append(f'    <rect x="{28 + 784 * cursor / total_bytes:.4f}" y="224" width="{784 * ratio:.4f}" height="10" fill="{color}"><title>{escape(name)} · {percent}</title></rect>')
        cursor += size
        entries.append(f'''  <g transform="translate({x} {y})">
    <title>{escape(name)}: {size:,} B; репозиториев: {count}</title>
    <circle cx="5" cy="10" r="4" fill="{color}"/>
    <text x="18" y="15" class="language"{text_fit}>{escape(name)}</text>
    <text x="376" y="15" class="percent" text-anchor="end">{percent}</text>
    <text x="18" y="35" class="muted">{human_bytes(size)} · репо: {count}</text>
    <rect x="18" y="43" width="358" height="2" rx="1" fill="#30374e"/>
    <rect x="18" y="43" width="{358 * ratio:.4f}" height="2" rx="1" fill="{color}"/>
  </g>''')
    if not languages:
        entries.append('  <text x="28" y="304" class="language">GitHub не вернул языковую статистику</text>')

    coverage = "Все приватные репозитории владельца" if stats["coverage_verified"] else "Приватные репозитории, доступные токену"
    scope = "Архивы включены"
    if stats["forks"]:
        scope += f' · форки: {stats["forks"]}'
    if stats["without_languages"]:
        scope += f' · без языков: {stats["without_languages"]}'
    return f'''<svg width="840" height="{height}" viewBox="0 0 840 {height}" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title description">
  <title id="title">Приватные репозитории · {stats["total"]} репо · {len(languages)} языков</title>
  <desc id="description">Срез GitHub Linguist по байтам. {scope}. Репозиториев с push за 30 дней: {stats["pushed_30d"]}. Расчёт {updated:%d.%m.%Y %H:%M:%S} МСК. Названия репозиториев не публикуются.</desc>
  <defs>
    <linearGradient id="panel" x2="1" y2="1"><stop stop-color="#1b1e2d"/><stop offset="1" stop-color="#131720"/></linearGradient>
    <linearGradient id="accent"><stop stop-color="#7dcfff"/><stop offset="1" stop-color="#bb9af7"/></linearGradient>
    <clipPath id="bar"><rect x="28" y="224" width="784" height="10" rx="5"/></clipPath>
  </defs>
  <style>
    text {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .eyebrow {{ fill: #a5aecb; font-size: 11px; font-weight: 600; letter-spacing: 1.2px; }}
    .heading {{ fill: #e2e8ff; font-size: 24px; font-weight: 650; }}
    .value {{ fill: #e2e8ff; font-size: 32px; font-weight: 650; font-variant-numeric: tabular-nums; }}
    .language {{ fill: #d2daf4; font-size: 16px; font-weight: 500; }}
    .percent {{ fill: #d2daf4; font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }}
    .muted {{ fill: #a5aecb; font-size: 12px; }}
    .activity {{ fill: #c4cce7; font-size: 13px; }}
  </style>
  <rect x="1" y="1" width="838" height="{height - 2}" rx="18" fill="url(#panel)" stroke="#343c54"/>
  <rect x="28" y="22" width="40" height="3" rx="1.5" fill="url(#accent)"/>
  <text x="78" y="27" class="eyebrow">GITHUB / MRNO1ZE</text>
  <text x="28" y="60" class="heading">Приватные репозитории</text>
  <text x="812" y="27" text-anchor="end" class="muted">Расчёт · {updated:%d.%m.%Y}</text>
  <text x="812" y="55" text-anchor="end" class="activity">{updated:%H:%M:%S} МСК</text>
{chr(10).join(tiles)}
  <circle cx="33" cy="206" r="4" fill="#9ece6a"/>
  <text x="46" y="210" class="activity">Репо с push за 30 дней: {stats["pushed_30d"]} / {stats["total"]}</text>
  <text x="812" y="210" class="muted" text-anchor="end">{escape(scope)}</text>
  <g clip-path="url(#bar)">
{chr(10).join(segments)}
  </g>
  <text x="28" y="264" class="eyebrow">ЯЗЫК / ОБЪЁМ / РЕПО</text>
  <text x="404" y="264" class="eyebrow" text-anchor="end">ДОЛЯ</text>
  <text x="436" y="264" class="eyebrow">ЯЗЫК / ОБЪЁМ / РЕПО</text>
  <text x="812" y="264" class="eyebrow" text-anchor="end">ДОЛЯ</text>
{chr(10).join(entries)}
  <path d="M28 {footer_y}H812" stroke="#30374e"/>
  <text x="28" y="{footer_y + 25}" class="muted">{coverage} · основные ветки</text>
  <text x="28" y="{footer_y + 47}" class="muted">Объём кода по Linguist, не личный вклад. Доли округлены. KiB / MiB — единицы по 1024.</text>
</svg>
'''


def publish(stats: Stats, root: Path = ROOT) -> None:
    svg = render(stats)
    version = hashlib.sha256(svg.encode()).hexdigest()[:16]
    readme_path = root / "README.md"
    readme, count = re.subn(
        r'(?<=src=")\./assets/private-stats\.svg(?:\?v=[a-f0-9]+)?(?=")',
        f"./assets/private-stats.svg?v={version}", readme_path.read_text(encoding="utf-8"),
    )
    if count != 1:
        raise ValueError("В README должна быть ровно одна карточка private-stats.svg")
    (root / "assets/private-stats.svg").write_text(svg, encoding="utf-8")
    (root / "assets/private-stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readme_path.write_text(readme, encoding="utf-8")


def main() -> int:
    if "--self-check" in sys.argv:
        import unittest
        suite = unittest.defaultTestLoader.discover(str(ROOT / "scripts"), pattern="test_*.py")
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    token = os.environ.get("STATS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Нет STATS_TOKEN: нужен PAT владельца с доступом ко всем приватным репозиториям", file=sys.stderr)
        return 1
    try:
        stats = collect(token)
        publish(stats)
    except urllib.error.HTTPError as exc:
        print(f"GitHub API: HTTP {exc.code}. Проверь STATS_TOKEN и лимиты API.", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"Статистика не обновлена: {exc}", file=sys.stderr)
        return 1
    summary = (f'Репозиториев: {stats["total"]}; языков: {len(stats["languages"])}; '
               f'код: {human_bytes(sum(item["bytes"] for item in stats["languages"].values()))}; '
               f'расчёт: {stats["generated_at"]}')
    print(summary)
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_path, "a", encoding="utf-8") as output:
            output.write(summary + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
