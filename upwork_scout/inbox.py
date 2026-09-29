"""Inbox mode: parse Upwork pages that the user saved manually from a normal Chrome window.

No browser automation is involved: the user opens saved searches (or job pages) in Chrome and
saves them with Ctrl+S — preferably "Webpage, Single File" (``.mhtml``), which stores the
rendered page and its address. The address tells which saved search (``/nx/find-work/{id}``) or
job (``/jobs/…~01…``) a file is; files are then parsed with the same parsers as browser mode,
merged into SQLite by job ID and moved to ``inbox/processed/<timestamp>/``.

Accepted formats:
* ``.mhtml`` / ``.mht`` — Chrome "Webpage, Single File" (recommended);
* ``.html`` / ``.htm`` — Chrome "Webpage, Complete" (the ``<name>_files`` folder is moved too).
  "Webpage, HTML Only" stores the server's original HTML, which may lack the job cards.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .config import Config
from .discovery import parse_saved_searches
from .guards import PageState, detect_page_state
from .htmltext import inline_text, soup_of
from .ids import UPWORK_BASE, is_upwork_url, normalize_job_id, parse_search_id, search_url
from .models import SavedSearch
from .parsing import parse_job_details, parse_search_page
from .storage import Storage

log = logging.getLogger(__name__)

EXTENSIONS = (".mhtml", ".mht", ".html", ".htm")
JOB_PAGES_SEARCH = SavedSearch("job-pages", "Сохранённые страницы вакансий", f"{UPWORK_BASE}/nx/find-work/")
_SAVED_FROM_RE = re.compile(r"<!--\s*saved from url=\(\d+\)\s*(\S+?)\s*-->", re.I)
_TITLE_SUFFIX_RE = re.compile(r"\s*[-–|]\s*(?:Find Work|Upwork).*$", re.I)


@dataclass
class SavedPage:
    path: Path
    url: str | None
    html: str


@dataclass
class InboxResult:
    files: int = 0
    search_pages: int = 0
    job_pages: int = 0
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    archive: Path | None = None
    rejected_archive: Path | None = None

    @property
    def processed(self) -> int:
        return self.search_pages + self.job_pages

    def summary(self) -> str:
        text = f"Папка inbox: файлов {self.files}, страниц поиска {self.search_pages}, страниц вакансий {self.job_pages}"
        if self.skipped:
            text += f", пропущено {len(self.skipped)}"
        return text + "."


def _part_text(part) -> str:
    """Decoded text of a MIME part. Chrome writes ``Content-Type: text/html`` without a
    charset (the page's own <meta> says UTF-8), and ``get_content()`` then falls back to
    US-ASCII, turning every non-ASCII byte into U+FFFD — so decode the raw bytes ourselves."""
    raw = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:  # unknown charset name
        return raw.decode("utf-8", errors="replace")


def read_saved_page(path: Path) -> SavedPage:
    """Read an .mhtml or .html file saved by Chrome; the page address is recovered when stored."""
    data = path.read_bytes()
    if path.suffix.lower() in (".mhtml", ".mht"):
        msg = email.message_from_bytes(data, policy=email.policy.default)
        url = msg.get("Snapshot-Content-Location")
        html_part = None
        for part in msg.walk():
            if part.get_content_type() != "text/html":
                continue
            if html_part is None or (url and part.get("Content-Location") == url):
                html_part = part
                if url and part.get("Content-Location") == url:
                    break
        html = _part_text(html_part) if html_part is not None else ""
        return SavedPage(path, str(url) if url else None, html)
    html = data.decode("utf-8", errors="replace")
    m = _SAVED_FROM_RE.search(html[:2000])
    url = m.group(1) if m else None
    if not url:
        soup = soup_of(html)
        link = soup.find("link", rel="canonical")
        meta = soup.find("meta", property="og:url")
        url = (link.get("href") if link else None) or (meta.get("content") if meta else None)
    return SavedPage(path, str(url) if url else None, html)


def _page_title(html: str) -> str:
    soup = soup_of(html)
    title = inline_text(soup.title) if soup.title else ""
    return _TITLE_SUFFIX_RE.sub("", title).strip()


def identify_search(page: SavedPage) -> SavedSearch:
    """Saved search of a page: by its /nx/find-work/{id} address (name from the page's own tab
    link), otherwise a pseudo-search named after the page title or file name."""
    search_id = parse_search_id(page.url) if page.url else None
    if search_id:
        names = {s.search_id: s.name for s in parse_saved_searches(page.html)}
        name = names.get(search_id) or _page_title(page.html) or f"Поиск {search_id}"
        return SavedSearch(search_id, name, search_url(search_id))
    source = page.url or page.path.name
    pseudo_id = "page-" + hashlib.sha1(source.encode("utf-8")).hexdigest()[:10]
    name = _page_title(page.html) or page.path.stem
    url = page.url if page.url and is_upwork_url(page.url) else f"{UPWORK_BASE}/nx/find-work/"
    return SavedSearch(pseudo_id, name[:120], url)


def _job_id_of_page(page: SavedPage) -> str | None:
    """A job page: its address is an Upwork job URL (search pages carry /nx/find-work/{id})."""
    if not page.url or not is_upwork_url(page.url) or parse_search_id(page.url):
        return None
    path = urlparse(page.url).path
    if "/nx/find-work/" in path and "/details/" not in path:
        return None
    return normalize_job_id(page.url)


def list_inbox(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS)


def _free_path(path: Path) -> Path:
    """``path`` or ``path (2)``, ``path (3)``… — never overwrite an archived file."""
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _archive(paths: list[Path], folder: Path, now: datetime, kind: str = "processed",
             errors: list[str] | None = None) -> Path:
    """Move files (and their ``<stem>_files`` folders) one by one. A file that cannot be moved
    (e.g. locked by another program on Windows) stays in the inbox and is reported; it never
    fails the run, whose data are already committed."""
    target = folder / kind / now.astimezone().strftime("%Y-%m-%d-%H%M%S")  # local time
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Не удалось создать папку %s: %s", target, exc)
        if errors is not None:
            errors.append(f"не удалось создать папку {kind}/ ({type(exc).__name__}) — файлы "
                          f"{', '.join(p.name for p in paths)} остались в inbox; перенесите их вручную")
        return target
    for path in paths:
        try:
            shutil.move(str(path), str(_free_path(target / path.name)))
            assets = path.with_name(path.stem + "_files")
            if assets.is_dir():
                shutil.move(str(assets), str(_free_path(target / assets.name)))
        except OSError as exc:
            log.warning("Не удалось перенести %s: %s", path.name, exc)
            if errors is not None:
                errors.append(f"{path.name}: не удалось перенести в {kind}/ ({type(exc).__name__}) — "
                              "файл остался в inbox; закройте программу, которая его держит, и удалите/перенесите вручную")
    return target


def process_inbox(cfg: Config, store: Storage, run_id: int, now: datetime, notify) -> InboxResult:
    """Parse all saved pages in the inbox folder into the database for ``run_id``."""
    folder = cfg.path(cfg.inbox.folder)
    folder.mkdir(parents=True, exist_ok=True)
    result = InboxResult()
    files = list_inbox(folder)
    result.files = len(files)
    pages: list[SavedPage] = []
    accepted: list[Path] = []  # files whose data were merged; everything else is "rejected"
    for path in files:
        try:
            page = read_saved_page(path)
        except Exception as exc:  # one unreadable file must not stop the others
            log.warning("Файл %s: %s", path.name, exc, exc_info=True)
            result.skipped.append(f"{path.name}: не удалось прочитать ({type(exc).__name__})")
            continue
        # Fail closed: only pages whose saved address is on upwork.com are parsed, so text from
        # any other site can never reach the database or the translation API.
        if not page.url or not is_upwork_url(page.url):
            result.skipped.append(f"{path.name}: в файле нет адреса страницы upwork.com — "
                                  "сохраните страницу из Chrome через Ctrl+S («Веб-страница, один файл»)")
            continue
        state = detect_page_state(page.url, page.html)
        if state is PageState.SECURITY_CHECK:
            result.skipped.append(f"{path.name}: сохранена страница проверки безопасности, а не Upwork")
            continue
        if state is PageState.LOGIN:
            result.skipped.append(f"{path.name}: сохранена страница входа — войдите в Upwork и сохраните заново")
            continue
        pages.append(page)

    # Search pages first, so jobs get their real searches; job pages then add full details.
    search_pages = [p for p in pages if _job_id_of_page(p) is None]
    job_pages = [p for p in pages if _job_id_of_page(p) is not None]
    counts: dict[str, int] = {}
    position: dict[str, int] = {}
    for page in search_pages:
        search = identify_search(page)
        jobs = parse_search_page(page.html, now)
        if not jobs:
            result.skipped.append(f"{page.path.name}: карточек вакансий не найдено "
                                  "(сохраняйте как «Веб-страница, один файл» после загрузки ленты)")
            continue
        store.upsert_searches([search], now)
        store.record_jobs(run_id, search, jobs, now)
        counts[search.search_id] = counts.get(search.search_id, 0) + len(jobs)
        position.setdefault(search.search_id, len(position))
        store.record_search_result(run_id, search.search_id, position[search.search_id], "ok",
                                   counts[search.search_id], None)
        result.search_pages += 1
        accepted.append(page.path)
        notify(f"  {page.path.name}: «{search.name}» — карточек {len(jobs)}")

    for page in job_pages:
        job_id = _job_id_of_page(page)
        details = parse_job_details(page.html, job_id, now)
        if not details.title:
            result.skipped.append(f"{page.path.name}: на странице вакансии нет данных")
            continue
        if store.get_record(job_id) is None or not store.searches_for_job(job_id):
            store.upsert_searches([JOB_PAGES_SEARCH], now)
            store.record_jobs(run_id, JOB_PAGES_SEARCH, [details], now)
        else:
            rec = store.get_record(job_id)
            search_id = store.searches_for_job(job_id)[0][0]
            search = SavedSearch(search_id, "", "")
            store.record_jobs(run_id, search, [rec.job], now)  # sighting in this run
        store.update_job_details(details, run_id, now)
        result.job_pages += 1
        accepted.append(page.path)
        notify(f"  {page.path.name}: страница вакансии «{details.title[:60]}»")

    for note in result.skipped:
        notify(f"  пропущено — {note}")
    if cfg.inbox.move_processed:
        rejected = [p for p in files if p not in accepted]
        if accepted:
            result.archive = _archive(accepted, folder, now, "processed", result.warnings)
            notify(f"Обработанные файлы перенесены в {result.archive}")
        if rejected:
            result.rejected_archive = _archive(rejected, folder, now, "rejected", result.warnings)
            notify(f"Отклонённые файлы перенесены в {result.rejected_archive} — их можно сохранить заново")
        for warning in result.warnings:
            notify(f"  внимание — {warning}")
    return result
