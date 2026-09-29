"""Deterministic Russian labels for Upwork's fixed vocabulary (no AI involved).

Only the interface vocabulary is translated here: publication time, experience level, project
length, proposal tiers and common country names. Job titles and descriptions are translated by
:mod:`upwork_scout.translate`. Unknown text is returned unchanged — never guessed.
"""

from __future__ import annotations

import re

from .models import Job

_UNITS = {
    "second": "с",
    "minute": "мин",
    "hour": "ч",
    "day": "дн",
    "week": "нед",
    "month": "мес",
    "year": "г",
}
_RELATIVE_RE = re.compile(r"^(\d+|an?|one)\s+(second|minute|hour|day|week|month|year)s?\s+ago$", re.I)
_FIXED_POSTED = {
    "just now": "только что",
    "yesterday": "вчера",
    "last week": "на прошлой неделе",
    "last month": "в прошлом месяце",
}
_MONTHS = {
    "jan": "янв", "feb": "фев", "mar": "мар", "apr": "апр", "may": "мая", "jun": "июн",
    "jul": "июл", "aug": "авг", "sep": "сен", "oct": "окт", "nov": "ноя", "dec": "дек",
}
_ABS_DATE_RE = re.compile(r"^([A-Z][a-z]{2})[a-z]*\.? (\d{1,2}),? (\d{4})$")

_EXPERIENCE = {"entry level": "начальный", "intermediate": "средний", "expert": "эксперт"}

# Ordered: longer phrases first.
_DURATION = [
    (r"Less than (?:1|one|a) week", "менее недели"),
    (r"Less than (?:1|one|a) month", "менее 1 месяца"),
    (r"More than 6 months", "более 6 месяцев"),
    (r"1 to 3 months", "1–3 месяца"),
    (r"3 to 6 months", "3–6 месяцев"),
    (r"(\d+) to (\d+) months", r"\1–\2 мес."),
    (r"Less than 30 hrs/week", "до 30 ч/нед"),
    (r"More than 30 hrs/week", "более 30 ч/нед"),
    (r"30\+ hrs/week", "30+ ч/нед"),
    (r"Hours to be determined", "часы не определены"),
]

_COUNTRIES = {
    "united states": "США", "usa": "США", "united kingdom": "Великобритания", "uk": "Великобритания",
    "canada": "Канада", "australia": "Австралия", "germany": "Германия", "france": "Франция",
    "netherlands": "Нидерланды", "switzerland": "Швейцария", "sweden": "Швеция", "norway": "Норвегия",
    "denmark": "Дания", "finland": "Финляндия", "ireland": "Ирландия", "spain": "Испания",
    "italy": "Италия", "portugal": "Португалия", "belgium": "Бельгия", "austria": "Австрия",
    "poland": "Польша", "czech republic": "Чехия", "czechia": "Чехия", "israel": "Израиль",
    "united arab emirates": "ОАЭ", "saudi arabia": "Саудовская Аравия", "qatar": "Катар",
    "india": "Индия", "pakistan": "Пакистан", "bangladesh": "Бангладеш", "singapore": "Сингапур",
    "japan": "Япония", "south korea": "Южная Корея", "china": "Китай", "hong kong": "Гонконг",
    "new zealand": "Новая Зеландия", "brazil": "Бразилия", "mexico": "Мексика",
    "argentina": "Аргентина", "south africa": "ЮАР", "nigeria": "Нигерия", "egypt": "Египет",
    "turkey": "Турция", "türkiye": "Турция", "ukraine": "Украина", "georgia": "Грузия",
    "cyprus": "Кипр", "estonia": "Эстония", "latvia": "Латвия", "lithuania": "Литва",
    "romania": "Румыния", "bulgaria": "Болгария", "greece": "Греция", "serbia": "Сербия",
    "kazakhstan": "Казахстан", "armenia": "Армения", "vietnam": "Вьетнам", "philippines": "Филиппины",
    "indonesia": "Индонезия", "malaysia": "Малайзия", "thailand": "Таиланд",
}


def posted(text: str | None) -> str | None:
    """"3 hours ago" → "3 ч назад"; "yesterday" → "вчера"; "Sep 20, 2026" → "20 сен 2026"."""
    if not text:
        return text
    clean = " ".join(text.split())
    low = clean.lower()
    if low in _FIXED_POSTED:
        return _FIXED_POSTED[low]
    m = _RELATIVE_RE.match(clean)
    if m:
        n = "1" if m.group(1).lower() in ("a", "an", "one") else m.group(1)
        return f"{n} {_UNITS[m.group(2).lower()]} назад"
    m = _ABS_DATE_RE.match(clean)
    if m and m.group(1).lower() in _MONTHS:
        return f"{int(m.group(2))} {_MONTHS[m.group(1).lower()]} {m.group(3)}"
    return clean


def experience(text: str | None) -> str | None:
    if not text:
        return text
    return _EXPERIENCE.get(text.strip().lower(), text)


def duration(text: str | None) -> str | None:
    if not text:
        return text
    out = text
    for pattern, repl in _DURATION:
        out = re.sub(pattern, repl, out, flags=re.I)
    return out


def proposals(text: str | None) -> str | None:
    """"Less than 5" → "меньше 5"; "5 to 10" → "5–10"; "50+" stays."""
    if not text:
        return text
    m = re.match(r"^\s*Less than (\d+)\s*$", text, re.I)
    if m:
        return f"меньше {m.group(1)}"
    m = re.match(r"^\s*(\d+)\s*to\s*(\d+)\s*$", text, re.I)
    if m:
        return f"{m.group(1)}–{m.group(2)}"
    return text


def country(text: str | None) -> str | None:
    if not text:
        return text
    return _COUNTRIES.get(text.strip().lower(), text)


def budget(job: Job) -> str:
    """Budget line for the Russian report (the English variant in fmt.budget goes to Jev)."""
    from .fmt import money  # local import keeps fmt free of localisation concerns

    if job.job_type == "fixed":
        return f"фикс. цена {money(job.budget)}" if job.budget is not None else "фикс. цена, бюджет не указан"
    if job.job_type == "hourly":
        lo, hi = job.hourly_min, job.hourly_max
        if lo is None and hi is None:
            return "почасовая, ставка не указана"
        if lo is not None and hi is not None and lo != hi:
            return f"почасовая {money(lo)}–{money(hi)}/ч"
        return f"почасовая {money(hi if hi is not None else lo)}/ч"
    return "не указано"
