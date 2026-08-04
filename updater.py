#!/usr/bin/env python3
"""Small updater utility.

Scrapes a (relatively static) web page for the current version string and the
download link (an <a href> button), compares the site version to the installed
version, and — only if they differ — downloads the zip, extracts it,
auto-flattens a single top-level folder, and installs the files in place over
an existing install directory while preserving a user-specified folder
(e.g. "decks"). Keeps exactly one safety backup ("<install>.bak") per update.

Download links are pulled fresh from the page each run because they rotate and
sometimes go dead; a dead/invalid link is detected and aborts with no changes.

Config lives next to this script as config.json, so the script can sit in an
arbitrary location and be called via an alias/shell wrapper.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

DEFAULT_CONFIG = {
    "version_url": "",                 # page showing version + download button
    "version_regex": r"\d+\.\d+[a-z]?",  # e.g. matches "1.41b"
    "link_match": r"dropbox\.com|dropboxusercontent\.com|\.zip(\?|$)",  # pick which href
    "source_url": "",                  # manual fallback link if scrape finds none
    "install_dir": "",                 # app dir (files overwritten in place)
    "excluded_folder": "decks",        # top-level folder in zip to skip (user data)
    "installed_version": None,         # last version we installed
    "launch_cmd": "",                  # command to start the app, run with cwd=install_dir
    "platform": "auto",                # auto/windows/mac/linux/android/ios
}

UA = {"User-Agent": "updater/1.0"}
PLATFORM_ALIASES = {
    "windows": ("windows", "win"),
    "mac": ("mac", "macos", "osx"),
    "linux": ("linux",),
    "android": ("android",),
    "ios": ("ios", "iphone", "ipad"),
}
PLATFORM_LABELS = {
    "auto": "auto",
    "windows": "Windows",
    "mac": "Mac",
    "linux": "Linux",
    "android": "Android",
    "ios": "iOS",
}


class UpdaterError(Exception):
    """Recoverable failure. Top-level commands turn it into a clean exit;
    `launch` catches it so a down site never blocks starting the app."""


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError) as e:
            sys.exit(f"error: could not read {CONFIG_PATH}: {e}")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def resolve_install_dir(cfg: dict) -> Path:
    raw = cfg.get("install_dir") or ""
    if not raw:
        sys.exit("error: install_dir not set. Run: updater.py config --install-dir <path>")
    return Path(raw).expanduser().resolve()


def normalize_platform(value: str | None) -> str:
    if not value:
        return "auto"
    value = value.strip().lower()
    if value in PLATFORM_ALIASES:
        return value
    for name, aliases in PLATFORM_ALIASES.items():
        if value in aliases:
            return name
    return "auto"


def resolve_platform(cfg: dict, requested: str | None = None) -> str:
    if requested:
        return normalize_platform(requested)
    raw = (cfg.get("platform") or "auto").strip()
    normalized = normalize_platform(raw)
    if normalized != "auto":
        return normalized
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("win"):
        return "windows"
    return "auto"


def _platform_matches(href: str, text: str, platform: str) -> bool:
    if platform == "auto":
        return False
    keywords = PLATFORM_ALIASES.get(platform, ())
    lowered_href = href.lower()
    lowered_text = text.lower()
    return any(keyword in lowered_href or keyword in lowered_text for keyword in keywords)


# --------------------------------------------------------------------------
# url handling  (Dropbox + direct only; Google Drive intentionally skipped)
# --------------------------------------------------------------------------
def transform_url(url: str) -> str:
    """Turn a Dropbox share link into a direct download. Pass others through."""
    parts = urllib.parse.urlparse(url)
    if "dropbox.com" in parts.netloc.lower():
        query = dict(urllib.parse.parse_qsl(parts.query))
        query["dl"] = "1"  # force download instead of preview
        parts = parts._replace(query=urllib.parse.urlencode(query))
        return urllib.parse.urlunparse(parts)
    return url


# --------------------------------------------------------------------------
# scraping
# --------------------------------------------------------------------------
class LinkParser(HTMLParser):
    """Collect every <a href> on the page, in document order."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, object]] = []
        self._stack: list[dict[str, object]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs_map = dict(attrs)
            href = attrs_map.get("href")
            if href:
                self._stack.append({"href": href, "attrs": attrs_map, "text": []})

    def handle_endtag(self, tag):
        if tag == "a" and self._stack:
            self.links.append(self._stack.pop())

    def handle_data(self, data):
        if self._stack:
            self._stack[-1]["text"].append(data)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class TextParser(HTMLParser):
    """Extract visible text from HTML while skipping script/style content."""

    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self._skip_content = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._skip_content = True

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._skip_content = False

    def handle_data(self, data):
        if not self._skip_content:
            self.text_parts.append(data)

    def get_text(self) -> str:
        return _normalize_text(" ".join(self.text_parts))


def _extract_version_from_text(text: str, version_regex: str) -> str | None:
    if not text:
        return None
    m = re.search(version_regex, text, re.IGNORECASE)
    return m.group(0) if m else None


def _extract_version_from_url(url: str, version_regex: str) -> str | None:
    return _extract_version_from_text(url, version_regex)


def _is_placeholder_version(version: str | None, version_regex: str) -> bool:
    if not version:
        return True
    if version.lower() in {"latest", "unknown", "n/a", "none", "placeholder", ""}:
        return True
    return False


def _select_download_link(links: list[dict[str, object]], base_url: str, link_match: str, platform: str = "auto"):
    lm = re.compile(link_match, re.IGNORECASE)

    for link in links:
        href = str(link["href"])
        if not lm.search(href):
            continue
        text = _normalize_text("".join(link["text"]))
        lowered = text.lower()
        if platform != "auto" and _platform_matches(href, text, platform):
            if "main download" in lowered and "mirror" not in lowered:
                return urllib.parse.urljoin(base_url, href)
            if "mirror" not in lowered:
                return urllib.parse.urljoin(base_url, href)

    for link in links:
        href = str(link["href"])
        if not lm.search(href):
            continue
        text = _normalize_text("".join(link["text"]))
        lowered = text.lower()
        if "main download" in lowered and "mirror" not in lowered:
            return urllib.parse.urljoin(base_url, href)

    for link in links:
        href = str(link["href"])
        if lm.search(href):
            return urllib.parse.urljoin(base_url, href)
    return None


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, "replace")


def scrape_page(html: str, base_url: str, version_regex: str, link_match: str, platform: str = "auto"):
    """Return (version, download_url). Prefer the site page version and only fall back to link-derived versions when needed."""
    parser = LinkParser()
    parser.feed(html)
    download_url = _select_download_link(parser.links, base_url, link_match, platform)

    visible_text = TextParser()
    visible_text.feed(html)
    version = _extract_version_from_text(visible_text.get_text(), version_regex)
    url_version = _extract_version_from_url(download_url or "", version_regex) if download_url else None

    if _is_placeholder_version(version, version_regex) and url_version:
        version = url_version

    return version, download_url


def probe_link(url: str) -> bool:
    """Cheap reachability check. True = looks alive."""
    direct = transform_url(url)
    req = urllib.request.Request(direct, headers=UA, method="HEAD")
    try:
        with urllib.request.urlopen(req) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as e:
        # some hosts reject HEAD (405); treat as unknown-but-present
        return e.code in (403, 405)
    except urllib.error.URLError:
        return False


# --------------------------------------------------------------------------
# download / extract
# --------------------------------------------------------------------------
def extract_version_from_zip(zip_path: Path, version_regex: str | None = None) -> str | None:
    """Infer a version from archive names or paths when the page version is placeholder."""
    if not zip_path.exists():
        return None
    regex = version_regex or r"\d+\.\d+[a-z]?"
    try:
        with zipfile.ZipFile(zip_path) as zf:
            candidates: list[str] = []
            for name in zf.namelist():
                for match in re.finditer(regex, name, re.IGNORECASE):
                    candidates.append(match.group(0))
            if not candidates:
                return None

            def score(value: str) -> tuple[int, int, int]:
                return (
                    len(value),
                    value.count("."),
                    sum(1 for ch in value if ch.isalpha()),
                )

            return max(candidates, key=score)
    except zipfile.BadZipFile:
        return None


def download(url: str, dest: Path) -> None:
    direct = transform_url(url)
    print(f"downloading: {direct}")
    req = urllib.request.Request(direct, headers=UA)
    try:
        with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:
            total = resp.headers.get("Content-Length")
            total = int(total) if total else None
            read = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                read += len(chunk)
                if total:
                    pct = read * 100 // total
                    print(f"\r  {read // 1024} KiB / {total // 1024} KiB ({pct}%)",
                          end="", flush=True)
                else:
                    print(f"\r  {read // 1024} KiB", end="", flush=True)
        print()
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise UpdaterError(f"download link appears down ({e}). No changes made.")
    if not zipfile.is_zipfile(dest):
        raise UpdaterError("downloaded file is not a zip (dead link or error page?). No changes made.")


def extract_and_flatten(zip_path: Path, staging: Path) -> Path:
    """Extract zip into staging; if it holds a single top folder, return that.

    Restores unix permission bits (extractall drops them), so executable
    scripts/binaries in the release stay runnable.
    """
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            out = zf.extract(info, staging)
            mode = info.external_attr >> 16  # unix perms stored by most zippers
            if mode:
                Path(out).chmod(mode & 0o777)
    entries = [p for p in staging.iterdir() if not p.name.startswith("__MACOSX")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return staging


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------
def make_backup(install_dir: Path) -> None:
    """Replace the single .bak with a fresh copy of the current install."""
    if not install_dir.exists():
        return
    bak = install_dir.with_name(install_dir.name + ".bak")
    if bak.exists():
        shutil.rmtree(bak)
    print(f"backup: {install_dir} -> {bak}")
    shutil.copytree(install_dir, bak)


def install_files(source_root: Path, install_dir: Path, excluded: str) -> None:
    """Overwrite files in install_dir from source_root, skipping excluded folder."""
    install_dir.mkdir(parents=True, exist_ok=True)
    for item in source_root.iterdir():
        if excluded and item.name == excluded:
            print(f"skip excluded: {item.name}/")
            continue
        target = install_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
    print(f"installed into: {install_dir}")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_config(args) -> None:
    cfg = load_config()
    changed = False
    for key in ("version_url", "version_regex", "link_match",
                "source_url", "install_dir", "excluded_folder", "launch_cmd"):
        val = getattr(args, key)
        if val is not None:
            cfg[key] = val
            changed = True
    if changed:
        save_config(cfg)
        print(f"saved: {CONFIG_PATH}")
    print(json.dumps(cfg, indent=2))


def _scrape_site(cfg: dict, platform: str | None = None):
    """Fetch version_url and return (version, download_url). Exits on no version_url."""
    vurl = cfg.get("version_url")
    if not vurl:
        raise UpdaterError("version_url not set. Run: updater.py config --version-url <page>")
    try:
        html = fetch_text(vurl)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise UpdaterError(f"version page unreachable ({e}). No changes made.")
    selected_platform = resolve_platform(cfg, platform)
    return scrape_page(html, vurl, cfg["version_regex"], cfg["link_match"], selected_platform)


def cmd_check(args) -> None:
    cfg = load_config()
    version, dl = _scrape_site(cfg, getattr(args, "platform", None))
    installed = cfg.get("installed_version")
    print(f"installed version: {installed}")
    print(f"site version:      {version or '(not found — check version_regex)'}")
    print(f"platform:          {PLATFORM_LABELS.get(resolve_platform(cfg, getattr(args, 'platform', None)), resolve_platform(cfg, getattr(args, 'platform', None)))}")
    print(f"download link:     {dl or '(not found — check link_match)'}")
    if dl:
        print(f"link status:       {'alive' if probe_link(dl) else 'DOWN'}")
    if version and installed == version:
        print("=> up to date.")
    elif version:
        print("=> update available.")


def install_from_url(cfg: dict, url: str, install_dir: Path,
                     excluded: str, site_version) -> None:
    """Download+extract+backup+install. Raises UpdaterError if the link is down
    (before any file is touched). Records the best available version on success."""
    version_regex = cfg.get("version_regex") or DEFAULT_CONFIG["version_regex"]
    with tempfile.TemporaryDirectory(prefix="updater_") as tmp:
        tmp = Path(tmp)
        zip_path = tmp / "download.zip"
        staging = tmp / "staging"
        staging.mkdir()

        download(url, zip_path)        # raises before any file op if link is down
        source_root = extract_and_flatten(zip_path, staging)
        make_backup(install_dir)
        install_files(source_root, install_dir, excluded)

        resolved_version = site_version
        if _is_placeholder_version(resolved_version, version_regex):
            resolved_version = extract_version_from_zip(zip_path, version_regex) or resolved_version



    if resolved_version:
        cfg["installed_version"] = resolved_version
        save_config(cfg)
        print(f"recorded installed_version: {resolved_version}")


def cmd_update(args) -> None:
    cfg = load_config()
    install_dir = resolve_install_dir(cfg)
    excluded = cfg.get("excluded_folder") or ""

    site_version = None
    if args.url:                       # explicit one-off link, no scrape
        url = args.url
    else:
        site_version, dl = _scrape_site(cfg, getattr(args, "platform", None))
        if site_version and site_version == cfg.get("installed_version") and not args.force:
            print(f"up to date: {site_version} (use --force to reinstall)")
            return
        url = dl or cfg.get("source_url")
        if not url:
            raise UpdaterError("no download link found on page and no source_url fallback set.")

    install_from_url(cfg, url, install_dir, excluded, site_version)
    print("update complete.")


def cmd_launch(args) -> None:
    cfg = load_config()
    install_dir = resolve_install_dir(cfg)
    excluded = cfg.get("excluded_folder") or ""
    launch_cmd = cfg.get("launch_cmd") or ""
    if not launch_cmd:
        sys.exit("error: launch_cmd not set. Run: updater.py config --launch-cmd '<cmd>'")

    # --- version check (soft: a down site warns but never blocks launch) ---
    if not args.no_update:
        try:
            site_version, dl = _scrape_site(cfg, getattr(args, "platform", None))
            installed = cfg.get("installed_version")
            if site_version and site_version != installed:
                if _confirm_update(installed, site_version, args.yes):
                    url = dl or cfg.get("source_url")
                    if not url:
                        print("warning: no download link found; launching current version.")
                    else:
                        install_from_url(cfg, url, install_dir, excluded, site_version)
                        print("update complete.")
                else:
                    print("skipping update.")
        except UpdaterError as e:
            print(f"warning: {e} Launching current version.")

    # --- launch app detached, then this utility exits ---
    print(f"launching: {launch_cmd}")
    try:
        subprocess.Popen(shlex.split(launch_cmd), cwd=str(install_dir),
                         start_new_session=True)
    except (OSError, ValueError) as e:
        sys.exit(f"error: could not launch app: {e}")
    # util returns/exits here; app keeps running in its own session


def _confirm_update(installed, latest, assume_yes: bool) -> bool:
    """Prompt to update. Auto-yes with --yes; auto-skip when non-interactive."""
    banner = f"update available: {installed} -> {latest}."
    if assume_yes:
        print(f"{banner} (--yes) updating.")
        return True
    if not sys.stdin.isatty():
        print(f"{banner} non-interactive shell, skipping (use --yes to auto-update).")
        return False
    print(banner)
    return input("update now? [y/N] ").strip().lower() in ("y", "yes")


def cmd_rollback(args) -> None:
    cfg = load_config()
    install_dir = resolve_install_dir(cfg)
    bak = install_dir.with_name(install_dir.name + ".bak")
    if not bak.exists():
        sys.exit(f"error: no backup at {bak}")
    print(f"rollback: {bak} -> {install_dir}")
    if install_dir.exists():
        shutil.rmtree(install_dir)
    shutil.move(str(bak), str(install_dir))
    print("rollback complete. (backup consumed)")


def cmd_status(args) -> None:
    cfg = load_config()
    install_dir = Path(cfg["install_dir"]).expanduser() if cfg.get("install_dir") else None
    bak = install_dir.with_name(install_dir.name + ".bak") if install_dir else None
    print(f"config file:       {CONFIG_PATH}")
    print(f"version_url:       {cfg.get('version_url') or '(unset)'}")
    print(f"version_regex:     {cfg.get('version_regex')}")
    print(f"link_match:        {cfg.get('link_match')}")
    print(f"source_url:        {cfg.get('source_url') or '(unset)'}")
    print(f"install_dir:       {install_dir or '(unset)'}")
    print(f"  exists:          {install_dir.exists() if install_dir else False}")
    print(f"backup:            {bak if bak else '(n/a)'}")
    print(f"  exists:          {bak.exists() if bak else False}")
    print(f"excluded_folder:   {cfg.get('excluded_folder')}")
    print(f"platform:          {PLATFORM_LABELS.get(resolve_platform(cfg), resolve_platform(cfg))}")
    print(f"installed_version: {cfg.get('installed_version')}")
    print(f"launch_cmd:        {cfg.get('launch_cmd') or '(unset)'}")


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Scrape/download/unzip/install app updates.")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("config", help="set/show stored settings")
    c.add_argument("--version-url", dest="version_url")
    c.add_argument("--version-regex", dest="version_regex")
    c.add_argument("--link-match", dest="link_match")
    c.add_argument("--source-url", dest="source_url")
    c.add_argument("--install-dir", dest="install_dir")
    c.add_argument("--excluded-folder", dest="excluded_folder")
    c.add_argument("--launch-cmd", dest="launch_cmd")
    c.add_argument("--platform", dest="platform")
    c.set_defaults(func=cmd_config)

    ck = sub.add_parser("check", help="scrape site: show installed vs latest + link status")
    ck.add_argument("--platform", dest="platform")
    ck.set_defaults(func=cmd_check)

    u = sub.add_parser("update", help="scrape + install if version differs")
    u.add_argument("--url", help="one-off link, skips scrape entirely")
    u.add_argument("--force", action="store_true", help="reinstall even if version matches")
    u.add_argument("--platform", dest="platform")
    u.set_defaults(func=cmd_update)

    r = sub.add_parser("rollback", help="restore the .bak backup")
    r.set_defaults(func=cmd_rollback)

    s = sub.add_parser("status", help="show current settings and paths")
    s.set_defaults(func=cmd_status)

    ln = sub.add_parser("launch", help="check version (prompt to update), then start the app")
    ln.add_argument("--yes", action="store_true", help="auto-update without prompting")
    ln.add_argument("--no-update", action="store_true", help="skip version check, just launch")
    ln.add_argument("--platform", dest="platform")
    ln.set_defaults(func=cmd_launch)

    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except UpdaterError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
