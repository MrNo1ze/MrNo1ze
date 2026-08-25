#!/usr/bin/env python3
"""Собирает assets/private-stats.svg из GitHub API.

Считает только приватные репозитории, где владелец — сам пользователь.
Названия репозиториев наружу не попадают: в карточку идут только агрегаты.

Нужен токен со scope `repo` (классический PAT) или fine-grained с доступом
на чтение метаданных всех репозиториев — GITHUB_TOKEN из Actions видит
только текущий репозиторий и не годится.

    GITHUB_TOKEN=... python3 scripts/build_private_stats.py
    python3 scripts/build_private_stats.py --self-check   # рендер на фикстуре
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import escape
from pathlib import Path

API = "https://api.github.com"
OUT = Path(__file__).resolve().parent.parent / "assets" / "private-stats.svg"

TOP_N = 5  # столько языков показываем поимённо, остальное схлопывается в «прочее»

# Узнаваемые цвета для частых языков; остальным раздаём из FALLBACK по рангу.
LANG_COLORS = {
    "Go": "#7dcfff",
    "TypeScript": "#7aa2f7",
    "JavaScript": "#e0af68",
    "HTML": "#ff9e64",
    "CSS": "#bb9af7",
    "Python": "#9ece6a",
    "Shell": "#89ddff",
    "Rust": "#f7768e",
    "Dockerfile": "#41a6b5",
    "Makefile": "#c0caf5",
    "SQL": "#73daca",
    "Vue": "#4fd6be",
    "Svelte": "#ff757f",
    "PHP": "#9d7cd8",
    "Ruby": "#f7768e",
    "Java": "#e0af68",
    "Kotlin": "#bb9af7",
    "Swift": "#ff9e64",
    "C": "#a9b1d6",
    "C++": "#7aa2f7",
    "HCL": "#7dcfff",
}
FALLBACK = ["#7dcfff", "#7aa2f7", "#e0af68", "#ff9e64", "#bb9af7", "#73daca", "#f7768e", "#9d7cd8"]
OTHER_COLOR = "#9ece6a"


def api(path: str, token: str) -> tuple[object, dict[str, str]]:
    req = urllib.request.Request(
        path if path.startswith("http") else API + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "MrNo1ze-private-stats",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp), dict(resp.headers)


def next_link(headers: dict[str, str]) -> str | None:
    for part in headers.get("Link", "").split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None


def collect(token: str) -> dict:
    """Тянет приватные репозитории владельца и суммирует байты по языкам."""
    repos: list[dict] = []
    url = "/user/repos?per_page=100&affiliation=owner&visibility=private"
    while url:
        page, headers = api(url, token)
        repos.extend(page)
        url = next_link(headers)

    langs: dict[str, int] = {}
    for repo in repos:
        data, _ = api(f"/repos/{repo['full_name']}/languages", token)
        for name, size in data.items():
            langs[name] = langs.get(name, 0) + size

    return {
        "total": len(repos),
        "archived": sum(1 for r in repos if r.get("archived")),
        "languages": langs,
    }


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 100 // 10 == 1:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def human_bytes(n: int) -> str:
    for unit, step in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= step:
            value = n / step
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
    return f"{n} B"


def breakdown(langs: dict[str, int]) -> list[tuple[str, float, str]]:
    """(имя, процент, цвет), отсортировано по убыванию, сумма процентов ровно 100.0."""
    total = sum(langs.values())
    if total == 0:
        return []

    ranked = sorted(langs.items(), key=lambda kv: -kv[1])
    head = ranked[:TOP_N]
    tail_bytes = sum(size for _, size in ranked[TOP_N:])

    rows = [(name, size / total * 100) for name, size in head]
    if tail_bytes:
        rows.append(("прочее", tail_bytes / total * 100))

    # Округляем до 0.1 и добиваем расхождение в самый крупный сегмент,
    # иначе подписи в легенде не сложатся в 100%.
    pcts = [round(p, 1) for _, p in rows]
    pcts[0] = round(pcts[0] + (100.0 - sum(pcts)), 1)

    used: set[str] = set()
    out = []
    for (name, _), pct in zip(rows, pcts):
        if name == "прочее":
            color = OTHER_COLOR
        else:
            color = LANG_COLORS.get(name)
            if color is None or color in used:
                color = next((c for c in FALLBACK if c not in used), OTHER_COLOR)
        used.add(color)
        out.append((name, pct, color))
    return out


# --- рендер -----------------------------------------------------------------
# Координаты зафиксированы под viewBox 660x364. Легенда — сетка 3x2:
# колонки начинаются на X_COLS, процент прижат к правому краю колонки,
# поэтому длина названия языка не может сломать выравнивание.

X_PAD, INNER_W = 26, 608
BAR_X, BAR_Y, BAR_W, BAR_H = 26, 252, 608, 16
X_COLS = (26, 229, 432)
Y_ROWS = (302, 336)


def legend_font(names: list[str]) -> float:
    """Ужимаем шрифт легенды, если попался длинный язык (Jupyter Notebook и т.п.)."""
    longest = max((len(n) for n in names), default=0)
    if longest <= 11:
        return 15
    if longest <= 13:
        return 13.5
    return 12


def render(stats: dict) -> str:
    rows = breakdown(stats["languages"])
    total_bytes = sum(stats["languages"].values())
    repos = stats["total"]
    archived = stats["archived"]
    active = repos - archived

    repo_word = plural(repos, "приватный репозиторий", "приватных репозитория", "приватных репозиториев")
    active_word = plural(active, "активный", "активных", "активных")
    updated = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    leg_fs = legend_font([name for name, _, _ in rows])

    # Сегменты полосы рисуем «с наложением»: каждый тянется до правого края,
    # следующий ложится сверху. Так между цветами не остаётся волосяных швов.
    segments, cursor = [], 0.0
    for name, pct, color in rows:
        x = BAR_X + BAR_W * cursor / 100
        segments.append(
            f'    <rect x="{x:.1f}" y="{BAR_Y}" width="{BAR_X + BAR_W - x:.1f}" '
            f'height="{BAR_H}" fill="{color}"/>'
        )
        cursor += pct

    legend = []
    for i, (name, pct, color) in enumerate(rows):
        col, row = X_COLS[i % 3], Y_ROWS[i // 3]
        legend.append(
            f'  <g transform="translate({col} {row})">\n'
            f'    <circle cx="5" cy="-5" r="5" fill="{color}"/>\n'
            f'    <text x="20" y="0" class="mono leg">{escape(name)}</text>\n'
            f'    <text x="188" y="0" class="mono pct" text-anchor="end">{pct:.1f}%</text>\n'
            f"  </g>"
        )

    return f"""<svg width="660" height="364" viewBox="0 0 660 364" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Агрегированная статистика приватных репозиториев">
  <defs>
    <linearGradient id="panelGlow" x1="0" y1="0" x2="660" y2="364" gradientUnits="userSpaceOnUse">
      <stop stop-color="#1f2030"/>
      <stop offset="1" stop-color="#171823"/>
    </linearGradient>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">
      <stop stop-color="#22d3ee"/>
      <stop offset="0.5" stop-color="#bb9af7"/>
      <stop offset="1" stop-color="#ff4fd8"/>
    </linearGradient>
    <clipPath id="barClip"><rect x="{BAR_X}" y="{BAR_Y}" width="{BAR_W}" height="{BAR_H}" rx="8"/></clipPath>
  </defs>

  <style>
    .mono  {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }}
    .title {{ fill: #c0caf5; font-size: 26px; font-weight: 700; letter-spacing: 1.4px; }}
    .lead  {{ fill: #c0caf5; font-size: 44px; font-weight: 750; }}
    .label {{ fill: #a9b1d6; font-size: 16px; }}
    .muted {{ fill: #8389a3; font-size: 13.5px; }}
    .stamp {{ fill: #6b7089; font-size: 12.5px; }}
    .pct   {{ fill: #c0caf5; font-size: {leg_fs}px; font-weight: 700; }}
    .leg   {{ fill: #a9b1d6; font-size: {leg_fs}px; }}
  </style>

  <rect x="1" y="1" width="658" height="362" rx="20" fill="url(#panelGlow)" stroke="#32364f" stroke-width="2"/>
  <rect x="{X_PAD}" y="16" width="72" height="4" rx="2" fill="url(#accent)"/>

  <text x="{X_PAD}" y="48" class="mono title">ПРИВАТНАЯ АКТИВНОСТЬ</text>
  <text x="{X_PAD + INNER_W}" y="48" class="mono stamp" text-anchor="end">обновлено {updated}</text>
  <text x="{X_PAD}" y="74" class="mono muted" style="font-size:14px">сводка без раскрытия названий и деталей репозиториев</text>

  <rect x="26" y="92" width="296" height="116" rx="16" fill="#1b1c28" stroke="#2b3048"/>
  <text x="44" y="144" class="mono lead">{repos}</text>
  <text x="44" y="174" class="mono label">{repo_word}</text>
  <text x="44" y="195" class="mono muted">{active} {active_word} · {archived} в архиве</text>

  <rect x="338" y="92" width="296" height="116" rx="16" fill="#1b1c28" stroke="#2b3048"/>
  <text x="356" y="144" class="mono lead">{human_bytes(total_bytes)}</text>
  <text x="356" y="174" class="mono label">кода распознано по языкам</text>
  <text x="356" y="195" class="mono muted">только приватные репозитории</text>

  <text x="{X_PAD}" y="238" class="mono label">распределение по языкам</text>

  <g clip-path="url(#barClip)">
{chr(10).join(segments)}
  </g>

{chr(10).join(legend)}
</svg>
"""


FIXTURE = {
    "total": 12,
    "archived": 3,
    "languages": {
        "Go": 5_200_000,
        "TypeScript": 2_100_000,
        "JavaScript": 1_150_000,
        "HTML": 1_080_000,
        "CSS": 505_000,
        "Shell": 140_000,
        "Makefile": 95_000,
    },
}


def self_check() -> None:
    rows = breakdown(FIXTURE["languages"])
    assert len(rows) == TOP_N + 1, rows
    assert rows[-1][0] == "прочее"
    assert abs(sum(p for _, p, _ in rows) - 100.0) < 1e-9, rows
    assert len({c for _, _, c in rows}) == len(rows), "цвета сегментов не должны повторяться"
    assert rows == sorted(rows, key=lambda r: -r[1]), "сегменты должны идти по убыванию"

    assert plural(1, "a", "b", "c") == "a"
    assert plural(3, "a", "b", "c") == "b"
    assert plural(11, "a", "b", "c") == "c"
    assert plural(12, "a", "b", "c") == "c"
    assert plural(21, "a", "b", "c") == "a"
    assert human_bytes(10_222_000) == "9.7 MB"

    svg = render(FIXTURE)
    import xml.dom.minidom

    xml.dom.minidom.parseString(svg)
    # Последний сегмент полосы обязан упираться в правый край, иначе видна дырка.
    assert f'width="{BAR_W:.1f}"' in svg
    assert "обновлено" in svg

    empty = render({"total": 0, "archived": 0, "languages": {}})
    xml.dom.minidom.parseString(empty)
    print("self-check ok")


def main() -> int:
    if "--self-check" in sys.argv:
        self_check()
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("STATS_TOKEN")
    if not token:
        print("нет токена: задай GITHUB_TOKEN (scope repo)", file=sys.stderr)
        return 1

    try:
        stats = collect(token)
    except urllib.error.HTTPError as exc:
        print(f"GitHub API вернул {exc.code}: {exc.reason}", file=sys.stderr)
        return 1

    # Пустой ответ — это сбой доступа, а не «репозиториев нет».
    # Лучше уронить job, чем закоммитить обнулённую карточку.
    if stats["total"] == 0:
        print("API отдал 0 приватных репозиториев — проверь scope токена", file=sys.stderr)
        return 1

    OUT.write_text(render(stats), encoding="utf-8")
    print(f"{OUT.name}: {stats['total']} репозиториев, {human_bytes(sum(stats['languages'].values()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
