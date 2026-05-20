import os
import re
import shutil
import sys
import tempfile
import importlib.util
import time
from importlib.machinery import SourcelessFileLoader
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from PyQt6.QtCore import QDate, QThread, QTime, Qt, QObject, pyqtSignal
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

# Core definitions (inlined from inkxmynovel.py)
IS_FROZEN = getattr(sys, "frozen", False)


def _user_data_dir() -> Path:
    """Per-user folder for config / progress / automation profile data.

    The packaged build runs from a read-only (and, for a single-file build,
    temporary) bundle, so user data cannot live next to the program.
    Running from source keeps data next to this .py file, so the existing
    developer workflow is unchanged.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "InkMyNovel"


if IS_FROZEN:
    APP_DIR = _user_data_dir()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    # Windowed builds may have no real stdout/stderr — keep print() safe.
    for _std in ("stdout", "stderr"):
        if getattr(sys, _std, None) is None:
            setattr(sys, _std, open(os.devnull, "w", encoding="utf-8"))
else:
    APP_DIR = Path(__file__).resolve().parent

DEFAULT_PRICE = 20


@dataclass
class BrowserProfile:
    label: str
    user_data_dir: str
    profile_dir_name: str


@dataclass
class NovelTask:
    title_keyword: str = ""
    working_url_override: str = ""
    chapter_files: List[str] = field(default_factory=list)
    run_mode: str = "fast"
    headless: bool = False
    sequence_policy: str = "auto_fix"
    price_mode: str = "free"
    price_value: int = DEFAULT_PRICE
    publish_mode: str = "published"
    schedule: Dict[str, Any] = field(default_factory=dict)
    auto_free_details: Optional[Dict[str, Any]] = None
    status: str = "pending"
    last_error: str = ""
    selected: bool = False

    def display_name(self):
        text = (self.title_keyword or "").strip() or (self.working_url_override or "").strip()
        return text or "นิยายใหม่"


@dataclass
class ScheduleConfig:
    start_date: str
    start_time: str
    chapters_per_day: int
    interval_minutes: int
    limit_per_day_enabled: bool
    skip_enabled: bool
    skip_start: str
    skip_end: str
    interval_unit: str = "minutes"


@dataclass
class UploadJob:
    preset_name: str
    novel_keyword: str
    chapter_files: List[str]
    browser_mode: str
    selected_profile_label: str
    custom_profile_path: str
    chrome_path: str
    headless: bool
    price_mode: str
    price_value: int
    publish_mode: str
    schedule: Dict[str, Any]
    reset_progress: bool
    run_mode: str
    sequence_policy: str = "auto_fix"
    auto_free_details: Optional[Dict] = None


def build_guest_profile():
    return BrowserProfile(label="Guest", user_data_dir="", profile_dir_name="")


def build_automation_profile():
    automation_root = APP_DIR / "automation_profile"
    automation_root.mkdir(parents=True, exist_ok=True)
    return BrowserProfile(label="Automation Profile", user_data_dir=str(automation_root), profile_dir_name="Default")


def build_profile_from_custom_path(custom_path: str):
    return BrowserProfile(label=f"Custom: {Path(custom_path).name}", user_data_dir=custom_path, profile_dir_name="Default")


def build_progress_file_path(job: UploadJob) -> Path:
    safe_keyword = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in (job.novel_keyword or "unknown")).strip() or "unknown"
    return APP_DIR / f"inkxmynovel_progress_{safe_keyword}.txt"


def compute_schedule_datetimes(file_count: int, cfg: ScheduleConfig):
    result = []
    start_parts = cfg.start_time.split(":")
    base_dt = datetime.strptime(cfg.start_date, "%Y-%m-%d").replace(hour=int(start_parts[0]), minute=int(start_parts[1]))
    skip_start = cfg.skip_start if cfg.skip_enabled else None
    skip_end = cfg.skip_end if cfg.skip_enabled else None
    daily_limit = max(1, int(cfg.chapters_per_day or 1)) if cfg.limit_per_day_enabled else None
    current_dt = base_dt
    current_day = base_dt.date()
    scheduled_today = 0
    for _ in range(file_count):
        while True:
            if skip_start and skip_end:
                t_str = current_dt.strftime("%H:%M")
                if is_time_in_skip_window(t_str, skip_start, skip_end):
                    current_dt = advance_out_of_skip_window(current_dt, skip_start, skip_end)
                    continue
            break
        if daily_limit is not None and current_dt.date() != current_day:
            current_day = current_dt.date()
            scheduled_today = 0
        result.append(current_dt)
        scheduled_today += 1
        if daily_limit is not None and scheduled_today >= daily_limit:
            scheduled_today = 0
            next_day = current_dt + timedelta(days=1)
            current_dt = next_day.replace(hour=int(start_parts[0]), minute=int(start_parts[1]))
            current_day = current_dt.date()
        else:
            current_dt += timedelta(minutes=cfg.interval_minutes)
    return result


def detect_chrome_path() -> str:
    if sys.platform == "win32":
        common_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    elif sys.platform == "darwin":
        common_paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:
        common_paths = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]
    for p in common_paths:
        if os.path.exists(p):
            return p
    # Fall back to anything resolvable on PATH.
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def detect_chrome_user_data_dir() -> Optional[Path]:
    """Locate Chrome's real 'User Data' directory for the current OS."""
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if not local_app_data:
            return None
        return Path(local_app_data) / "Google" / "Chrome" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return Path.home() / ".config" / "google-chrome"


def load_chrome_profiles():
    user_data_dir = detect_chrome_user_data_dir()
    if user_data_dir is None or not user_data_dir.exists():
        return []

    local_state = read_json(user_data_dir / "Local State", {})
    info_cache = ((local_state or {}).get("profile") or {}).get("info_cache") or {}
    profiles = []

    for profile_dir in sorted(user_data_dir.iterdir()):
        if not profile_dir.is_dir():
            continue
        if profile_dir.name not in info_cache and not profile_dir.name.startswith("Profile") and profile_dir.name != "Default":
            continue

        cache_entry = info_cache.get(profile_dir.name, {}) if isinstance(info_cache, dict) else {}
        display_name = (cache_entry.get("name") or profile_dir.name or "").strip()
        profile_email = (cache_entry.get("user_name") or cache_entry.get("email") or "").strip()

        if not profile_email:
            preferences = read_json(profile_dir / "Preferences", {})
            account_info = preferences.get("account_info") if isinstance(preferences, dict) else None
            if isinstance(account_info, list):
                for account in account_info:
                    candidate_email = (account or {}).get("email") or (account or {}).get("full_name")
                    if candidate_email:
                        profile_email = str(candidate_email).strip()
                        break

        if profile_email:
            label = f"{display_name} ({profile_email})"
        else:
            label = display_name

        profiles.append(
            BrowserProfile(
                label=label,
                user_data_dir=str(user_data_dir),
                profile_dir_name=profile_dir.name,
            )
        )

    return profiles


def normalize_path(file_path: str) -> str:
    return os.path.normpath(os.path.abspath(file_path)).lower()


def read_chapter_file(file_path: str):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = [line.rstrip("\n\r") for line in f]
    except UnicodeDecodeError:
        with open(file_path, "r", encoding="cp874") as f:
            lines = [line.rstrip("\n\r") for line in f]
    title = Path(file_path).stem
    if lines:
        title = lines[0].strip() or title
        body = lines[1:]
    else:
        body = []
    return title, body


def read_json(path: Path, default=None):
    if not path.exists():
        return default if default is not None else {}
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def write_json(path: Path, data):
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def sort_file_paths(paths: List[str]):
    def sort_key(path: str):
        stem = Path(path).stem
        episode_no = extract_episode_number_from_text(stem)
        return (episode_no is None, episode_no if episode_no is not None else stem.lower(), stem.lower(), path.lower())

    return sorted(paths, key=sort_key)


def normalize_episode_title_text(text: str) -> str:
    normalized = (text or "").strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def extract_episode_number_from_text(text: str) -> Optional[int]:
    source = (text or "").strip()
    if not source:
        return None
    patterns = [
        r"ตอนที่\s*(\d+)",
        r"ตอน\s*(\d+)",
        r"chapter\s*(\d+)",
        r"ep(?:isode)?\s*(\d+)",
        r"(?:^|\D)(\d{1,6})(?:\D|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, source, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                continue
    return None


def is_episode_free_by_rule(episode_number: Optional[int], rule_text: str, custom_text: str = "") -> bool:
    if episode_number is None:
        return False
    rule_text = (rule_text or "").strip()
    custom_text = (custom_text or "").strip()
    if "กำหนดเอง" in rule_text:
        try:
            custom_numbers = {int(x.strip()) for x in custom_text.split(",") if x.strip().isdigit()}
        except Exception:
            custom_numbers = set()
        return episode_number in custom_numbers
    if "ลงท้ายด้วย 5, 0" in rule_text:
        return episode_number % 5 == 0
    if "ลงท้ายด้วย 0" in rule_text:
        return episode_number % 10 == 0
    if "ลงท้ายด้วย 5" in rule_text:
        return episode_number % 10 == 5
    if "ทุก 3 ตอน" in rule_text:
        return episode_number % 3 == 0
    if "ทุก 5 ตอน" in rule_text:
        return episode_number % 5 == 0
    if "ทุก 10 ตอน" in rule_text:
        return episode_number % 10 == 0
    return False


def extract_episode_numbers_from_paths(paths: List[str]) -> List[int]:
    episode_numbers = []
    for path in paths or []:
        number = extract_episode_number_from_text(Path(path).stem)
        if number is not None:
            episode_numbers.append(number)
    return episode_numbers


def is_time_in_skip_window(time_text: str, skip_start: str, skip_end: str) -> bool:
    if not skip_start or not skip_end or skip_start == skip_end:
        return False
    if skip_start < skip_end:
        return skip_start <= time_text < skip_end
    return time_text >= skip_start or time_text < skip_end


def advance_out_of_skip_window(current_dt: datetime, skip_start: str, skip_end: str) -> datetime:
    end_hour, end_minute = [int(part) for part in skip_end.split(":")]
    adjusted = current_dt.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    current_time_text = current_dt.strftime("%H:%M")
    if skip_start > skip_end and current_time_text >= skip_start:
        adjusted += timedelta(days=1)
    return adjusted


def replace_episode_number_in_text(text: str, new_number: int) -> str:
    source = (text or "").strip()
    if not source:
        return f"ตอนที่ {new_number}"
    replacements = [
        (r"(ตอนที่\s*)(\d+)", r"\g<1>{}".format(new_number)),
        (r"(ตอน\s*)(\d+)", r"\g<1>{}".format(new_number)),
        (r"(chapter\s*)(\d+)", r"\g<1>{}".format(new_number)),
        (r"(ep(?:isode)?\s*)(\d+)", r"\g<1>{}".format(new_number)),
    ]
    for pattern, repl in replacements:
        updated, count = re.subn(pattern, repl, source, count=1, flags=re.IGNORECASE)
        if count:
            return updated
    updated, count = re.subn(r"(\d{1,6})", str(new_number), source, count=1)
    if count:
        return updated
    return f"ตอนที่ {new_number} {source}"


class MyNovelBot:
    def __init__(self, log_func=print):
        self.log_func = log_func
        self._driver_temp_dirs = {}

    def log(self, msg: str):
        self.log_func(msg)

    def prepare_profile_runtime_copy(self, profile: BrowserProfile):
        if not profile.user_data_dir:
            return profile, None

        source_user_data_dir = Path(profile.user_data_dir)
        if not source_user_data_dir.exists():
            raise RuntimeError(f"ไม่พบโฟลเดอร์ Chrome profile: {source_user_data_dir}")

        profile_dir_name = profile.profile_dir_name or "Default"
        source_profile_dir = source_user_data_dir / profile_dir_name
        if not source_profile_dir.exists() and (source_user_data_dir / "Preferences").exists():
            source_profile_dir = source_user_data_dir
            profile_dir_name = "Default"

        if not source_profile_dir.exists():
            raise RuntimeError(f"ไม่พบโฟลเดอร์โปรไฟล์ย่อย: {source_profile_dir}")

        temp_root = Path(tempfile.mkdtemp(prefix="inkxmynovel_profile_"))
        temp_user_data_dir = temp_root / "User Data"
        temp_user_data_dir.mkdir(parents=True, exist_ok=True)

        local_state_path = source_user_data_dir / "Local State"
        if local_state_path.exists():
            shutil.copy2(local_state_path, temp_user_data_dir / "Local State")

        target_profile_dir = temp_user_data_dir / profile_dir_name
        shutil.copytree(
            source_profile_dir,
            target_profile_dir,
            ignore=shutil.ignore_patterns(
                "Singleton*",
                "LOCK",
                "lockfile",
                "DevToolsActivePort",
                "Current Session",
                "Current Tabs",
                "Last Session",
                "Last Tabs",
                "Sessions",
                "Crashpad*",
                "BrowserMetrics*",
            ),
        )

        return BrowserProfile(profile.label, str(temp_user_data_dir), profile_dir_name), temp_root

    def cleanup_driver(self, driver):
        temp_root = self._driver_temp_dirs.pop(id(driver), None)
        try:
            driver.quit()
        except Exception:
            pass
        if temp_root:
            shutil.rmtree(temp_root, ignore_errors=True)

    def create_driver(self, chrome_path: str, profile: BrowserProfile, headless: bool = False, use_runtime_copy: bool = True):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        from selenium.webdriver.chrome.service import Service as ChromeService

        if use_runtime_copy:
            runtime_profile, temp_root = self.prepare_profile_runtime_copy(profile)
        else:
            runtime_profile, temp_root = profile, None
        options = ChromeOptions()
        if chrome_path:
            options.binary_location = chrome_path
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--window-size=1600,1200")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--remote-debugging-port=0")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-features=CalculateNativeWinOcclusion")
        if runtime_profile.user_data_dir:
            options.add_argument(f"--user-data-dir={runtime_profile.user_data_dir}")
        if runtime_profile.profile_dir_name:
            options.add_argument(f"--profile-directory={runtime_profile.profile_dir_name}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        service = ChromeService()
        try:
            driver = webdriver.Chrome(service=service, options=options)
        except Exception as error:
            if temp_root:
                shutil.rmtree(temp_root, ignore_errors=True)
            raise RuntimeError(
                "เปิด Chrome profile ไม่สำเร็จ: โปรไฟล์จริงอาจกำลังถูกใช้งาน หรือ Chrome เปิดไม่ขึ้นจาก profile นี้\n"
                f"รายละเอียด: {error}"
            )
        if temp_root:
            self._driver_temp_dirs[id(driver)] = temp_root
        driver.set_page_load_timeout(60)
        try:
            driver.set_window_rect(0, 0, 1600, 1200)
        except Exception:
            pass
        return driver

    def ensure_logged_in(self, driver, wait):
        current_url = driver.current_url
        if "/auth" in current_url or "/login" in current_url:
            raise RuntimeError("ยังไม่ได้ล็อกอิน")

    def find_working_url(self, driver, wait, keyword: str):
        if not keyword:
            return driver.current_url
        driver.get("https://mynovel.co/dashboard/workings")
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "div.rounded-xl.cursor-pointer")) > 0)
        cards = driver.find_elements(By.CSS_SELECTOR, "div.rounded-xl.cursor-pointer")
        for card in cards:
            try:
                title_el = card.find_element(By.CSS_SELECTOR, "h3")
                title_text = (title_el.text or "").strip()
                if keyword.lower() in title_text.lower() or title_text.lower() in keyword.lower():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
                    previous_url = driver.current_url
                    driver.execute_script("arguments[0].click();", card)
                    wait.until(lambda d: d.current_url != previous_url)
                    return driver.current_url.split("?")[0] + "?tab=episode"
            except Exception:
                continue
        raise RuntimeError(f"ไม่พบเรื่อง '{keyword}'")

    def open_episode_creator(self, driver, wait, working_url: str):
        target_url = working_url.split("?")[0] + "?tab=episode"
        driver.get(target_url)
        wait.until(lambda d: d.find_element(By.TAG_NAME, "body"))
        self.click_add_episode_button(driver, wait)
        self.wait_for_episode_form(wait)

    def click_element(self, driver, element):
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        try:
            element.click()
        except Exception:
            driver.execute_script("arguments[0].click();", element)

    def get_add_episode_button_candidates(self, driver):
        add_button_selectors = [
            (By.CSS_SELECTOR, "button.bg-primary.text-white[type='button']"),
            (By.CSS_SELECTOR, "button[class*='bg-primary'][class*='text-white'][type='button']"),
            (By.XPATH, "//button[@type='button' and normalize-space(.)='เพิ่มตอน']"),
            (By.XPATH, "//button[@type='button' and .//*[contains(@class, 'lucide-plus')] and contains(normalize-space(.), 'เพิ่มตอน')]"),
            (By.XPATH, "//button[contains(normalize-space(.), 'เพิ่มตอน')]"),
            (By.XPATH, "//button[contains(@class, 'bg-primary') and .//*[contains(@class, 'lucide-plus')]]"),
        ]
        add_buttons = []
        for by, selector in add_button_selectors:
            try:
                add_buttons.extend(driver.find_elements(by, selector))
            except Exception:
                continue
        if not add_buttons:
            add_buttons = driver.find_elements(By.CSS_SELECTOR, "button.bg-primary, button[class*='bg-primary']")
        unique_buttons = []
        seen_signatures = set()
        for btn in add_buttons:
            try:
                signature = (
                    (btn.text or "").strip(),
                    (btn.get_attribute("class") or "").strip(),
                    btn.location.get("x", 0),
                    btn.location.get("y", 0),
                    btn.size.get("width", 0),
                    btn.size.get("height", 0),
                )
            except Exception:
                signature = id(btn)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            unique_buttons.append(btn)
        return unique_buttons, add_button_selectors

    def find_submit_episode_button(self, driver):
        dialog = self.find_visible_episode_dialog(driver)
        root = dialog or driver
        candidates = [
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.CSS_SELECTOR, "button[data-slot='button']"),
            (By.XPATH, ".//button[@type='submit' and contains(., 'สร้างตอน')]"),
            (By.XPATH, ".//button[contains(., 'สร้างตอน')]"),
            (By.XPATH, ".//button[contains(., 'บันทึก')]"),
            (By.XPATH, ".//button[contains(., 'เผยแพร่')]"),
            (By.XPATH, ".//button[contains(@aria-label, 'สร้างตอน') or contains(@aria-label, 'เผยแพร่') or contains(@aria-label, 'บันทึก')]"),
        ]
        for by, value in candidates:
            try:
                buttons = root.find_elements(by, value)
                for btn in buttons:
                    if btn.is_displayed() and btn.is_enabled():
                        return btn
            except Exception:
                continue
        if dialog is not None:
            try:
                footer_buttons = dialog.find_elements(By.XPATH, ".//button")
                for btn in reversed(footer_buttons):
                    try:
                        button_text = ((btn.text or "") + " " + (btn.get_attribute("aria-label") or "")).strip()
                        button_type = (btn.get_attribute("type") or "").strip().lower()
                        if not btn.is_displayed() or not btn.is_enabled():
                            continue
                        if button_type == "submit" or any(keyword in button_text for keyword in ["สร้างตอน", "เผยแพร่", "บันทึก", "submit"]):
                            return btn
                    except Exception:
                        continue
            except Exception:
                pass
        return None

    def click_add_episode_button(self, driver, wait):
        _, add_button_selectors = self.get_add_episode_button_candidates(driver)

        wait.until(
            lambda d: any(
                len(d.find_elements(by, selector)) > 0
                for by, selector in add_button_selectors
            )
        )
        add_buttons, _ = self.get_add_episode_button_candidates(driver)
        current_url = driver.current_url
        last_error = RuntimeError("ไม่พบฟอร์มเพิ่มตอนหลังจากกดปุ่ม")
        self.log_debug(driver, f"พบปุ่มเพิ่มตอน {len(add_buttons)} ปุ่ม")
        for button_index in range(len(add_buttons)):
            try:
                if not add_buttons[button_index].is_displayed() or not add_buttons[button_index].is_enabled():
                    continue
                button_text = (add_buttons[button_index].text or "").strip()
                button_class = (add_buttons[button_index].get_attribute("class") or "").strip()
                self.log_debug(driver, f"ลองกดปุ่มเพิ่มตอน: text='{button_text}' class='{button_class[:120]}'")
                for attempt in range(2):
                    current_buttons, _ = self.get_add_episode_button_candidates(driver)
                    if button_index >= len(current_buttons):
                        last_error = RuntimeError(f"ปุ่มเพิ่มตอนลำดับ {button_index + 1} หายไปหลัง DOM refresh")
                        break
                    btn = current_buttons[button_index]
                    if not btn.is_displayed() or not btn.is_enabled():
                        continue
                    button_text = (btn.text or "").strip()
                    button_class = (btn.get_attribute("class") or "").strip()
                    self.log_debug(driver, f"ลองกดปุ่มเพิ่มตอน: text='{button_text}' class='{button_class[:120]}'")
                    self.click_element(driver, btn)
                    try:
                        WebDriverWait(driver, 8).until(
                            lambda d: self.find_visible_episode_dialog(d) is not None
                            or self.find_episode_form(d) is not None
                            or (
                                self.find_title_input(d) is not None
                                and self.find_body_input(d) is not None
                            )
                            or d.current_url != current_url
                        )
                        self.log_debug(driver, "กดปุ่มเพิ่มตอนสำเร็จ")
                        return
                    except (StaleElementReferenceException, TimeoutException) as error:
                        last_error = error
                        self.log_debug(driver, f"คลิกเพิ่มตอนแล้วแต่ยังไม่เห็นฟอร์ม (retry {attempt + 1}/2)")
                        time.sleep(0.3)
                        continue
            except Exception as error:
                last_error = error
                self.log_debug(driver, f"กดปุ่มเพิ่มตอนไม่สำเร็จ: {type(error).__name__}: {error!r}")
                continue
        raise RuntimeError(f"กดปุ่มเพิ่มตอนไม่สำเร็จ: {last_error}")

    def log_debug(self, driver, message):
        try:
            current_url = driver.current_url
        except Exception:
            current_url = ""
        print(f"[DEBUG] {message} | url={current_url}")

    def log_process_time(self, driver, step_name: str, started_at: float):
        elapsed = time.perf_counter() - started_at
        self.log_debug(driver, f"{step_name} ใช้เวลา {elapsed:.2f}s")
        return elapsed

    def format_duration(self, seconds):
        total_seconds = max(0, int(round(seconds)))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:d}ชม. {minutes:02d}น. {secs:02d}วิ"
        if minutes:
            return f"{minutes:d}น. {secs:02d}วิ"
        return f"{secs:d}วิ"

    def find_title_input(self, driver):
        selectors = [
            (By.NAME, "chapterTitle"),
            (By.CSS_SELECTOR, "input[name='chapterTitle']"),
            (By.CSS_SELECTOR, "input[placeholder*='ชื่อตอน']"),
            (By.CSS_SELECTOR, "input[placeholder*='ชื่' ]"),
            (By.CSS_SELECTOR, "textarea[name='chapterTitle']"),
        ]
        for by, value in selectors:
            try:
                elements = driver.find_elements(by, value)
                for element in elements:
                    if element.is_displayed():
                        return element
            except Exception:
                continue
        return None

    def click_choice_by_keywords(self, driver, root, keywords):
        lowered_keywords = [keyword.lower() for keyword in keywords if keyword]
        candidates = root.find_elements(By.XPATH, ".//button | .//label | .//*[@role='radio'] | .//div[@role='radio']")
        for candidate in candidates:
            try:
                text = ((candidate.text or "") + " " + (candidate.get_attribute("aria-label") or "")).strip().lower()
                if not text:
                    continue
                if not any(keyword in text for keyword in lowered_keywords):
                    continue
                if not candidate.is_displayed() or not candidate.is_enabled():
                    continue
                try:
                    self.scroll_modal_element_into_view(driver, candidate)
                except Exception:
                    pass
                self.click_element(driver, candidate)
                return True
            except Exception:
                continue
        return False

    def find_body_input(self, driver):
        selectors = [
            (By.NAME, "chapterContent"),
            (By.CSS_SELECTOR, "textarea[name='chapterContent']"),
            (By.CSS_SELECTOR, "textarea"),
            (By.CSS_SELECTOR, "div[contenteditable='true']"),
            (By.CSS_SELECTOR, "[role='textbox'][contenteditable='true']"),
        ]
        for by, value in selectors:
            try:
                elements = driver.find_elements(by, value)
                for element in elements:
                    if element.is_displayed():
                        return element
            except Exception:
                continue
        return None

    def clear_body_input(self, driver, body_input):
        tag_name = (body_input.tag_name or "").lower()
        is_contenteditable = (body_input.get_attribute("contenteditable") or "").lower() == "true"
        if is_contenteditable:
            driver.execute_script(
                """
                const element = arguments[0];
                element.focus();
                element.innerHTML = '';
                element.textContent = '';
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                body_input,
            )
            return
        if tag_name in {"textarea", "input"}:
            driver.execute_script(
                """
                const element = arguments[0];
                element.focus();
                element.value = '';
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                body_input,
            )
            return
        try:
            body_input.clear()
        except Exception:
            driver.execute_script("arguments[0].innerHTML = ''; arguments[0].textContent = '';", body_input)

    def get_body_input_text(self, driver, body_input):
        return driver.execute_script(
            """
            const element = arguments[0];
            if (!element) {
                return '';
            }
            const tagName = (element.tagName || '').toLowerCase();
            if (tagName === 'textarea' || tagName === 'input') {
                return element.value || '';
            }
            return element.innerText || element.textContent || '';
            """,
            body_input,
        ) or ""

    def normalize_editor_text(self, text: str):
        normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalized.replace("\u00a0", " ")
        lines = [line.strip() for line in normalized.split("\n")]
        compact_lines = []
        previous_blank = False
        for line in lines:
            is_blank = line == ""
            if is_blank and previous_blank:
                continue
            compact_lines.append(line)
            previous_blank = is_blank
        return "\n".join(compact_lines).strip()

    def set_body_input_text(self, driver, body_input, body_text: str):
        tag_name = (body_input.tag_name or "").lower()
        is_contenteditable = (body_input.get_attribute("contenteditable") or "").lower() == "true"
        if tag_name in {"textarea", "input"}:
            driver.execute_script(
                """
                const element = arguments[0];
                const value = arguments[1];
                element.focus();
                element.value = value;
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                body_input,
                body_text,
            )
            return
        if is_contenteditable:
            html = "<p>" + body_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "</p><p>") + "</p>"
            driver.execute_script(
                """
                const element = arguments[0];
                const html = arguments[1];
                element.focus();
                element.innerHTML = html;
                element.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertFromPaste' }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
                """,
                body_input,
                html,
            )
            return
        body_input.send_keys(body_text)

    def ensure_body_text_inserted(self, driver, wait, body_input, body_text: str):
        expected = self.normalize_editor_text(body_text)
        expected_compact = "".join(expected.split())
        deadline = time.time() + 4.0
        last_actual = ""
        while time.time() < deadline:
            last_actual = self.normalize_editor_text(self.get_body_input_text(driver, body_input))
            actual_compact = "".join(last_actual.split())
            if last_actual == expected or actual_compact == expected_compact:
                return
            if expected_compact and actual_compact:
                check_len = min(200, len(expected_compact))
                prefix = expected_compact[:check_len]
                suffix = expected_compact[-check_len:]
                if prefix in actual_compact and suffix in actual_compact and len(actual_compact) >= int(len(expected_compact) * 0.85):
                    return
            time.sleep(0.2)
        raise TimeoutException(f"body verification failed expected_len={len(expected)} actual_len={len(last_actual)}")

    def wait_for_episode_form(self, wait):
        wait.until(
            lambda d: self.find_visible_episode_dialog(d) is not None
            or self.find_episode_form(d) is not None
            or (self.find_title_input(d) is not None and self.find_body_input(d) is not None)
        )

    def find_visible_episode_dialog(self, driver):
        dialogs = driver.find_elements(By.CSS_SELECTOR, "div[role='dialog']")
        for dialog in reversed(dialogs):
            try:
                if dialog.is_displayed() and len(dialog.find_elements(By.NAME, "chapterTitle")) > 0:
                    return dialog
            except Exception:
                continue
        return None

    def find_episode_form(self, driver):
        dialog = self.find_visible_episode_dialog(driver)
        if dialog is None:
            return None
        try:
            forms = dialog.find_elements(By.TAG_NAME, "form")
            for form in forms:
                if form.is_displayed():
                    return form
        except Exception:
            pass
        return None

    def scroll_modal_element_into_view(self, driver, element):
        driver.execute_script(
            """
            const element = arguments[0];
            if (!element) {
                return;
            }
            let current = element.parentElement;
            while (current) {
                const style = window.getComputedStyle(current);
                const overflowY = style.overflowY || '';
                if ((overflowY.includes('auto') || overflowY.includes('scroll')) && current.scrollHeight > current.clientHeight) {
                    const currentRect = current.getBoundingClientRect();
                    const elementRect = element.getBoundingClientRect();
                    const delta = elementRect.top - currentRect.top - (current.clientHeight / 2) + (elementRect.height / 2);
                    current.scrollTop += delta;
                }
                current = current.parentElement;
            }
            const form = element.closest('form');
            if (form) {
                const formRect = form.getBoundingClientRect();
                const elementRect = element.getBoundingClientRect();
                const delta = elementRect.top - formRect.top - (form.clientHeight / 2) + (elementRect.height / 2);
                form.scrollTop += delta;
            }
            element.scrollIntoView({ block: 'center', inline: 'nearest' });
            """,
            element,
        )

    def find_section_by_text(self, root, label_text: str):
        xpath = (
            f".//label[contains(normalize-space(.), '{label_text}')]/ancestor::div[contains(@class, 'space-y-2')][1]"
            f" | .//label[contains(normalize-space(.), '{label_text}')]/ancestor::div[contains(@class, 'space-y-2')][2]"
        )
        try:
            sections = root.find_elements(By.XPATH, xpath)
            for section in sections:
                try:
                    if section.is_displayed():
                        return section
                except Exception:
                    continue
        except Exception:
            pass
        return root

    def find_radio_control(self, root, value: str):
        selectors = [
            (By.CSS_SELECTOR, f"button[role='radio'][value='{value}']"),
            (By.CSS_SELECTOR, f"input[type='radio'][value='{value}']"),
            (By.XPATH, f".//*[@role='radio' and @value='{value}']"),
            (By.XPATH, f".//input[@type='radio' and @value='{value}']"),
        ]
        for by, selector in selectors:
            try:
                elements = root.find_elements(by, selector)
                for element in elements:
                    try:
                        if element.is_displayed():
                            return element
                    except Exception:
                        return element
            except Exception:
                continue
        return None

    def select_radio_value(self, driver, root, value: str, label_text: str):
        control = self.find_radio_control(root, value)
        if control is None:
            raise RuntimeError(f"ไม่พบตัวเลือก {label_text}: {value}")
        try:
            if (control.get_attribute("role") or "") == "radio" and (control.get_attribute("aria-checked") or "").lower() == "true":
                return
            if (control.get_attribute("type") or "").lower() == "radio" and control.is_selected():
                return
        except Exception:
            pass
        click_target = control
        try:
            label = control.find_element(By.XPATH, "ancestor::label[1]")
            if label.is_displayed():
                click_target = label
        except Exception:
            pass
        try:
            self.scroll_modal_element_into_view(driver, click_target)
        except Exception:
            pass
        self.click_element(driver, click_target)
        try:
            WebDriverWait(driver, 3).until(
                lambda d: ((control.get_attribute("role") or "") == "radio" and (control.get_attribute("aria-checked") or "").lower() == "true")
                or ((control.get_attribute("type") or "").lower() == "radio" and control.is_selected())
            )
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", control)
            except Exception:
                pass

    def set_react_input_value(self, driver, element, value: str):
        driver.execute_script(
            """
            const el = arguments[0];
            const val = arguments[1];
            const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            nativeSetter.call(el, val);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            element,
            value,
        )

    def click_in_popover(self, driver, element):
        try:
            element.click()
        except Exception:
            driver.execute_script("arguments[0].click();", element)

    def find_schedule_day_button(self, popover, schedule_dt: datetime):
        iso_day = schedule_dt.strftime("%Y-%m-%d")
        short_day = f"{schedule_dt.month}/{schedule_dt.day}/{schedule_dt.year}"
        selectors = [
            f"button[data-day='{short_day}']",
            f"td[data-day='{iso_day}'] button[data-day='{short_day}']",
            f"td[data-day='{iso_day}'] button",
            f"[data-day='{iso_day}'] button",
        ]
        for selector in selectors:
            try:
                candidates = popover.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                candidates = []
            for candidate in candidates:
                try:
                    if candidate.is_displayed() and candidate.is_enabled() and not candidate.get_attribute("disabled"):
                        return candidate
                except Exception:
                    continue
        return None

    def blur_active_element(self, driver):
        try:
            driver.execute_script(
                """
                const active = document.activeElement;
                if (active && typeof active.blur === 'function') {
                    active.blur();
                }
                """
            )
        except Exception:
            pass

    def close_schedule_popover(self, driver):
        try:
            self.blur_active_element(driver)
            driver.execute_script(
                """
                document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
                document.dispatchEvent(new KeyboardEvent('keyup', { key: 'Escape', bubbles: true }));
                """
            )
        except Exception:
            pass
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                self.get_visible_schedule_popover(driver)
                time.sleep(0.1)
            except Exception:
                return

    def get_visible_schedule_popover(self, driver):
        popovers = driver.find_elements(By.CSS_SELECTOR, "div[id^='radix-'][role='dialog'], div[id^='radix-']")
        for popover in reversed(popovers):
            try:
                if not popover.is_displayed():
                    continue
                has_calendar = len(popover.find_elements(By.CSS_SELECTOR, ".rdp-caption_label")) > 0
                if has_calendar:
                    return popover
            except Exception:
                continue
        raise RuntimeError("ไม่พบป็อปอัปเลือกวันเวลา")

    def set_price(self, driver, wait, price_mode: str, price_value: int):
        form = self.find_episode_form(driver)
        root = form or self.find_visible_episode_dialog(driver) or driver
        section = self.find_section_by_text(root, "กำหนดราคา")
        target_value = "paid" if price_mode == "paid" else "free"
        self.log_debug(driver, f"เริ่มตั้งราคา mode={target_value} value={price_value}")
        try:
            self.scroll_modal_element_into_view(driver, section)
        except Exception:
            pass
        try:
            self.select_radio_value(driver, section, target_value, "กำหนดราคา")
        except Exception:
            fallback_keywords = ["ติดเหรียญ", "ขาย", "coin"] if target_value == "paid" else ["ฟรี", "ไม่ขาย"]
            if not self.click_choice_by_keywords(driver, section, fallback_keywords):
                raise
        if target_value != "paid":
            self.log_debug(driver, "ตั้งราคาแบบฟรีสำเร็จ")
            return
        price_input = None
        selectors = [
            (By.NAME, "chapterPrice"),
            (By.XPATH, ".//label[contains(., 'ราคาตอน')]/following-sibling::input"),
            (By.XPATH, ".//input[@type='number']"),
        ]
        deadline = time.time() + 5.0
        while time.time() < deadline and price_input is None:
            for by, selector in selectors:
                try:
                    elements = section.find_elements(by, selector)
                    for element in elements:
                        if element.is_displayed():
                            price_input = element
                            break
                    if price_input is not None:
                        break
                except Exception:
                    continue
        if price_input is None:
            raise RuntimeError("ไม่พบช่องกรอกราคาตอน")
        try:
            self.scroll_modal_element_into_view(driver, price_input)
        except Exception:
            pass
        self.click_in_popover(driver, price_input)
        self.set_react_input_value(driver, price_input, str(price_value))
        self.blur_active_element(driver)
        deadline = time.time() + 5.0
        confirmed = False
        while time.time() < deadline:
            actual = driver.execute_script("return arguments[0].value;", price_input) or ""
            if actual.strip() == str(price_value):
                confirmed = True
                break
            self.set_react_input_value(driver, price_input, str(price_value))
            time.sleep(0.15)
        if not confirmed:
            raise RuntimeError(f"ตั้งราคาไม่สำเร็จ expected={price_value} actual={driver.execute_script('return arguments[0].value;', price_input)}")
        self.log_debug(driver, f"ตั้งราคาสำเร็จ value={driver.execute_script('return arguments[0].value;', price_input)}")

    def set_publish_mode(self, driver, wait, publish_mode: str, schedule_dt):
        form = self.find_episode_form(driver)
        root = form or self.find_visible_episode_dialog(driver) or driver
        section = self.find_section_by_text(root, "ตั้งค่าสถานะเผยแพร่")
        mapping = {
            "draft": "draft",
            "published": "published",
            "scheduled": "scheduled",
        }
        target_value = mapping.get(publish_mode, "published")
        self.log_debug(driver, f"เริ่มตั้งสถานะเผยแพร่ mode={target_value}")
        try:
            self.scroll_modal_element_into_view(driver, section)
        except Exception:
            pass
        try:
            self.select_radio_value(driver, section, target_value, "ตั้งค่าสถานะเผยแพร่")
        except Exception:
            fallback_keywords = ["ไม่เผยแพร่", "เผยแพร่ทันที", "ตั้งเวลา"] if target_value == "draft" else ["เผยแพร่ทันที", "ตั้งเวลา"] if target_value == "published" else ["ตั้งเวลา"]
            if not self.click_choice_by_keywords(driver, section, fallback_keywords):
                raise
        if target_value == "scheduled":
            self.set_schedule_datetime(driver, wait, schedule_dt)
            return
        self.log_debug(driver, f"ตั้งสถานะเผยแพร่สำเร็จ mode={target_value}")

    def set_schedule_datetime(self, driver, wait, schedule_dt: datetime):
        if schedule_dt is None:
            raise RuntimeError("ไม่มีวันเวลาสำหรับการตั้งเวลาเผยแพร่")
        form = self.find_episode_form(driver)
        root = form or self.find_visible_episode_dialog(driver) or driver
        publish_section = self.find_section_by_text(root, "ตั้งค่าสถานะเผยแพร่")
        trigger_selectors = [
            (By.XPATH, ".//label[contains(., 'กำหนดเวลาเผยแพร่')]/following-sibling::button"),
            (By.XPATH, ".//button[contains(., 'เลือกวันและเวลาที่ต้องการเผยแพร่')]"),
            (By.XPATH, ".//button[@aria-haspopup='dialog']"),
            (By.CSS_SELECTOR, "button[aria-haspopup='dialog'][type='button']"),
        ]
        trigger = None
        last_error = None
        for search_root in (publish_section, root):
            for by, selector in trigger_selectors:
                try:
                    elements = search_root.find_elements(by, selector)
                    for element in elements:
                        if element.is_displayed():
                            trigger = element
                            break
                    if trigger is not None:
                        break
                except Exception as error:
                    last_error = error
            if trigger is not None:
                break
        if trigger is None:
            raise RuntimeError(f"ไม่พบปุ่มเปิดตัวเลือกวันเวลา: {last_error}")

        self.log_debug(driver, f"เริ่มตั้งเวลาเผยแพร่ {schedule_dt.strftime('%Y-%m-%d %H:%M')}")
        try:
            self.scroll_modal_element_into_view(driver, trigger)
        except Exception:
            pass
        self.click_element(driver, trigger)
        popover = None
        deadline = time.time() + 6.0
        while time.time() < deadline:
            try:
                popover = self.get_visible_schedule_popover(driver)
                break
            except Exception:
                time.sleep(0.2)
        if popover is None:
            raise TimeoutException("เปิดปฏิทินตั้งเวลาเผยแพร่ไม่สำเร็จ")

        target_month = (schedule_dt.year, schedule_dt.month)
        for _ in range(24):
            caption = popover.find_element(By.CSS_SELECTOR, ".rdp-caption_label").text.strip()
            current_dt = datetime.strptime(caption, "%B %Y")
            current_month = (current_dt.year, current_dt.month)
            if current_month == target_month:
                break
            selector = ".rdp-button_next" if current_month < target_month else ".rdp-button_previous"
            nav_buttons = popover.find_elements(By.CSS_SELECTOR, selector)
            if not nav_buttons:
                raise RuntimeError(f"ไม่พบปุ่มเปลี่ยนเดือน {selector}")
            self.click_in_popover(driver, nav_buttons[0])
            time.sleep(0.25)
            popover = self.get_visible_schedule_popover(driver)
        else:
            raise RuntimeError("เลื่อนไปเดือนไม่สำเร็จ")

        target_day = f"{schedule_dt.month}/{schedule_dt.day}/{schedule_dt.year}"
        clickable_day = self.find_schedule_day_button(popover, schedule_dt)
        if clickable_day is None:
            raise RuntimeError(f"ไม่พบวันที่ {target_day} ในปฏิทิน")
        self.click_in_popover(driver, clickable_day)
        time.sleep(0.3)

        popover = self.get_visible_schedule_popover(driver)
        inputs = [element for element in popover.find_elements(By.CSS_SELECTOR, "input[type='number']") if element.is_displayed()]
        if len(inputs) < 2:
            raise RuntimeError("ไม่พบช่องกรอกเวลาในปฏิทิน")
        for input_box, expected_val in ((inputs[0], f"{schedule_dt.hour:02d}"), (inputs[1], f"{schedule_dt.minute:02d}")):
            self.click_in_popover(driver, input_box)
            self.set_react_input_value(driver, input_box, expected_val)
            deadline = time.time() + 3.0
            while time.time() < deadline:
                actual = driver.execute_script("return arguments[0].value;", input_box) or ""
                if actual.zfill(2) == expected_val:
                    break
                self.set_react_input_value(driver, input_box, expected_val)
                time.sleep(0.1)
        self.blur_active_element(driver)
        self.close_schedule_popover(driver)
        self.log_debug(driver, "ตั้งเวลาเผยแพร่สำเร็จ")

    def fill_episode_form(self, driver, wait, title: str, body_lines, price_mode: str, price_value: int, publish_mode: str, schedule_dt):
        form_started = time.perf_counter()
        title_input = self.find_title_input(driver)
        if title_input is None:
            raise RuntimeError("ไม่พบช่องชื่อตอน")
        title_started = time.perf_counter()
        title_input.clear()
        title_input.send_keys(title)
        self.log_process_time(driver, "กรอกชื่อตอน", title_started)

        body_input = self.find_body_input(driver)
        if body_input is None:
            raise RuntimeError("ไม่พบช่องเนื้อหาตอน")
        body_text = "\n".join(body_lines)
        body_started = time.perf_counter()
        self.click_element(driver, body_input)
        self.clear_body_input(driver, body_input)
        self.set_body_input_text(driver, body_input, body_text)
        is_contenteditable = (body_input.get_attribute("contenteditable") or "").lower() == "true"
        try:
            self.ensure_body_text_inserted(driver, wait, body_input, body_text)
        except Exception:
            actual_text = self.normalize_editor_text(self.get_body_input_text(driver, body_input))
            expected_text = self.normalize_editor_text(body_text)
            self.log_debug(driver, f"ตรวจสอบเนื้อหาไม่ผ่าน expected_len={len(expected_text)} actual_len={len(actual_text)}")
            if not is_contenteditable:
                self.log_debug(driver, "จะลอง send_keys ซ้ำสำหรับ textarea/input")
                self.click_element(driver, body_input)
                self.clear_body_input(driver, body_input)
                body_input.send_keys(body_text)
                self.ensure_body_text_inserted(driver, wait, body_input, body_text)
        self.log_process_time(driver, "กรอกเนื้อหา", body_started)

        price_started = time.perf_counter()
        self.set_price(driver, wait, price_mode, price_value)
        self.log_process_time(driver, "ตั้งราคา", price_started)

        publish_started = time.perf_counter()
        self.set_publish_mode(driver, wait, publish_mode, schedule_dt)
        self.log_process_time(driver, "ตั้งค่าสถานะเผยแพร่", publish_started)
        self.log_process_time(driver, "กรอกฟอร์มรวม", form_started)

    def submit_episode(self, driver, wait):
        submit_button = self.find_submit_episode_button(driver)
        if submit_button is None:
            raise RuntimeError("กดปุ่มสร้างตอนไม่สำเร็จ: ไม่พบปุ่ม submit")

        self.log_debug(driver, f"เตรียมกดสร้างตอน text='{(submit_button.text or '').strip()}'")
        self.blur_active_element(driver)
        try:
            self.scroll_modal_element_into_view(driver, submit_button)
        except Exception:
            pass

        last_error = None
        for attempt in range(3):
            try:
                submit_button = self.find_submit_episode_button(driver)
                if submit_button is None:
                    raise RuntimeError("ไม่พบปุ่ม submit หลัง DOM refresh")
                self.log_debug(driver, f"ลองกดสร้างตอน ครั้งที่ {attempt + 1}")
                try:
                    self.scroll_modal_element_into_view(driver, submit_button)
                except Exception:
                    pass
                self.click_element(driver, submit_button)
                WebDriverWait(driver, 6).until(lambda d: self.find_visible_episode_dialog(d) is None)
                self.log_debug(driver, "กดสร้างตอนสำเร็จและฟอร์มปิดแล้ว")
                break
            except Exception as submit_error:
                last_error = submit_error
                self.log_debug(driver, f"กดสร้างตอนไม่สำเร็จ ครั้งที่ {attempt + 1}: {type(submit_error).__name__}: {submit_error}")
                time.sleep(0.4)
        else:
            raise RuntimeError(f"กดปุ่มสร้างตอนไม่สำเร็จ: {last_error}")

        time.sleep(0.8)

    def load_progress(self, progress_file: Path):
        if not progress_file.exists():
            return set()
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                return set(line.strip() for line in f if line.strip())
        except Exception:
            return set()

    def append_progress(self, progress_file: Path, filename: str):
        with open(progress_file, "a", encoding="utf-8") as f:
            f.write(f"{filename}\n")

    def open_login_browser(self, chrome_path: str, profile: BrowserProfile, persistent_profile: bool = False):
        driver = self.create_driver(chrome_path, profile, headless=False, use_runtime_copy=not persistent_profile)
        driver.get("https://mynovel.co/auth")
        return driver

    def run_job(self, job: UploadJob, profile: BrowserProfile):
        driver = self.create_driver(job.chrome_path, profile, headless=job.headless)
        wait = WebDriverWait(driver, 20)
        try:
            self.ensure_logged_in(driver, wait)
            working_url = self.find_working_url(driver, wait, job.novel_keyword)
            files = list(job.chapter_files)
            schedule_plan = compute_schedule_datetimes(len(files), ScheduleConfig(**job.schedule)) if job.publish_mode == "scheduled" else []
            uploaded_count = 0
            total_elapsed_seconds = 0.0

            for i, filename in enumerate(files):
                title, body = read_chapter_file(filename)
                schedule_dt = schedule_plan[i] if schedule_plan else None
                previous_latest_row = None
                chapter_started = time.perf_counter()
                self.log(f"เริ่มอัปโหลด ({i + 1}/{len(files)}): {Path(filename).name}")
                try:
                    step_started = time.perf_counter()
                    self.open_episode_creator(driver, wait, working_url)
                    self.log(f"เวลาเปิดฟอร์มเพิ่มตอน: {time.perf_counter() - step_started:.2f}s")
                    step_started = time.perf_counter()
                    self.fill_episode_form(driver, wait, title, body, job.price_mode, job.price_value, job.publish_mode, schedule_dt)
                    self.log(f"เวลาตั้งค่าฟอร์มตอน: {time.perf_counter() - step_started:.2f}s")
                    step_started = time.perf_counter()
                    previous_latest_row = self.submit_episode_with_recovery(
                        self,
                        driver,
                        wait,
                        working_url,
                        previous_latest_row,
                        title,
                        body,
                        job.price_mode,
                        job.price_value,
                        job.publish_mode,
                        schedule_dt,
                    )
                    self.log(f"เวลากดสร้างตอน: {time.perf_counter() - step_started:.2f}s")
                    elapsed_seconds = time.perf_counter() - chapter_started
                    uploaded_count += 1
                    total_elapsed_seconds += elapsed_seconds
                    avg_seconds = total_elapsed_seconds / uploaded_count
                    remaining_count = len(files) - (i + 1)
                    eta_seconds = avg_seconds * remaining_count
                    est_time = datetime.now() + timedelta(seconds=eta_seconds)
                    self.log(
                        f"สำเร็จ: {Path(filename).name} | ใช้เวลา {self.format_duration(elapsed_seconds)} | "
                        f"เฉลี่ย {self.format_duration(avg_seconds)} | "
                        f"เหลือประมาณ {self.format_duration(eta_seconds)} | EST {est_time.strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                except Exception:
                    elapsed_seconds = time.perf_counter() - chapter_started
                    remaining_count = len(files) - i
                    avg_seconds = (total_elapsed_seconds / uploaded_count) if uploaded_count else elapsed_seconds
                    eta_seconds = avg_seconds * remaining_count
                    est_time = datetime.now() + timedelta(seconds=eta_seconds)
                    self.log(
                        f"ตอนปัจจุบันใช้เวลาไปแล้ว {self.format_duration(elapsed_seconds)} | "
                        f"คาดว่าเหลือประมาณ {self.format_duration(eta_seconds)} | EST {est_time.strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    raise
        finally:
            self.cleanup_driver(driver)


# End core definitions

OLD_QT_SETTINGS_FILE = APP_DIR / "inkxmynovel_pyqt_settings.json"
QT_SETTINGS_FILE = APP_DIR / "config.json"
NOVEL_LIST_FILE = APP_DIR / "inkxmynovel_novel_list.txt"


def load_novel_list():
    if not NOVEL_LIST_FILE.exists():
        return []
    try:
        with open(NOVEL_LIST_FILE, "r", encoding="utf-8") as file:
            return [line.strip() for line in file if line.strip() and not line.strip().startswith("#")]
    except Exception:
        return []


def save_novel_list(novels):
    with open(NOVEL_LIST_FILE, "w", encoding="utf-8") as file:
        for novel in novels:
            file.write(f"{novel}\n")


class UploadWorker(QObject):
    status = pyqtSignal(str)
    job_finished = pyqtSignal(int, bool, str)
    finished = pyqtSignal(bool, str)

    def __init__(self, job, profile, existing_driver=None):
        super().__init__()
        self.jobs = job if isinstance(job, list) else [job]
        self.job = self.jobs[0] if self.jobs else None
        self.profile = profile
        self.existing_driver = existing_driver

    def click_element(self, driver, element):
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        try:
            element.click()
        except Exception:
            driver.execute_script("arguments[0].click();", element)

    def get_visible_schedule_popover(self, driver):
        popovers = driver.find_elements(By.CSS_SELECTOR, "div[id^='radix-'][role='dialog'], div[id^='radix-']")
        for popover in reversed(popovers):
            try:
                if not popover.is_displayed():
                    continue
                has_calendar = len(popover.find_elements(By.CSS_SELECTOR, ".rdp-caption_label")) > 0
                has_time_inputs = len(popover.find_elements(By.CSS_SELECTOR, "input[type='number']")) >= 2
                if has_calendar and has_time_inputs:
                    return popover
            except Exception:
                continue
        raise RuntimeError("ไม่พบป็อปอัปเลือกวันเวลา")

    def set_schedule_datetime_patch(self, bot, driver, wait, schedule_dt):
        trigger_xpaths = [
            "//label[contains(., 'กำหนดเวลาเผยแพร่')]/following-sibling::button",
            "//button[contains(., 'เลือกวันและเวลาที่ต้องการเผยแพร่')]",
        ]
        last_error = None
        trigger = None
        for xpath in trigger_xpaths:
            try:
                candidates = driver.find_elements(By.XPATH, xpath)
                for candidate in candidates:
                    if candidate.is_displayed():
                        trigger = candidate
                        break
                if trigger is not None:
                    break
            except Exception as error:
                last_error = error
        if trigger is None:
            raise RuntimeError(f"ไม่พบปุ่มเปิดตัวเลือกวันเวลา: {last_error}")

        self.click_element(driver, trigger)
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, ".rdp-caption_label")) > 0)
        popover = self.get_visible_schedule_popover(driver)

        target_caption = schedule_dt.strftime("%B %Y")
        for _ in range(24):
            current_caption = ""
            try:
                current_caption = popover.find_element(By.CSS_SELECTOR, ".rdp-caption_label").text.strip()
            except Exception:
                current_caption = ""
            if current_caption == target_caption:
                break
            next_month = (schedule_dt.year, schedule_dt.month) > (datetime.strptime(current_caption or target_caption, "%B %Y").year, datetime.strptime(current_caption or target_caption, "%B %Y").month) if current_caption else True
            selector = ".rdp-button_next" if next_month else ".rdp-button_previous"
            nav_buttons = popover.find_elements(By.CSS_SELECTOR, selector)
            if not nav_buttons:
                raise RuntimeError(f"ไม่พบปุ่มเปลี่ยนเดือน {selector}")
            self.click_element(driver, nav_buttons[0])
            time.sleep(0.25)
            popover = self.get_visible_schedule_popover(driver)

        target_day = f"{schedule_dt.month}/{schedule_dt.day}/{schedule_dt.year}"
        clickable_day = self.find_schedule_day_button(popover, schedule_dt)
        if clickable_day is None:
            raise RuntimeError(f"ไม่พบวันที่ {target_day} ในปฏิทิน")
        self.click_element(driver, clickable_day)
        time.sleep(0.2)

        popover = self.get_visible_schedule_popover(driver)
        inputs = [element for element in popover.find_elements(By.CSS_SELECTOR, "input[type='number']") if element.is_displayed()]
        if len(inputs) < 2:
            raise RuntimeError("ไม่พบช่องกรอกเวลา")
        hour_input = inputs[0]
        minute_input = inputs[1]
        for input_box, value in ((hour_input, f"{schedule_dt.hour:02d}"), (minute_input, f"{schedule_dt.minute:02d}")):
            input_box.clear()
            input_box.send_keys(value)
            driver.execute_script(
                "arguments[0].dispatchEvent(new Event('input', {bubbles:true})); arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                input_box,
            )
        time.sleep(0.5)

    def patch_bot(self, bot):
        bot.set_schedule_datetime = lambda driver, wait, schedule_dt: MyNovelBot.set_schedule_datetime(bot, driver, wait, schedule_dt)

    def format_duration(self, seconds):
        total_seconds = max(0, int(round(seconds)))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:d}ชม. {minutes:02d}น. {secs:02d}วิ"
        if minutes:
            return f"{minutes:d}น. {secs:02d}วิ"
        return f"{secs:d}วิ"

    def ensure_episode_creator_open(self, bot, driver, wait, working_url):
        current_url = driver.current_url or ""
        target_base = (working_url or "").split("?")[0]

        try:
            title_inputs = driver.find_elements(By.NAME, "chapterTitle")
            if any(element.is_displayed() for element in title_inputs):
                return
        except Exception:
            pass

        if target_base and current_url.startswith(target_base) and "tab=episode" in current_url:
            try:
                self.click_add_episode_button(driver, wait)
                self.wait_for_episode_form(wait)
                return
            except Exception:
                pass

        bot.open_episode_creator(driver, wait, working_url)

    def normalize_episode_url(self, url):
        raw_url = (url or "").strip()
        if not raw_url:
            return ""
        base_url = raw_url.split("?")[0]
        return f"{base_url}?tab=episode"

    def get_latest_episode_row_info(self, driver, wait):
        try:
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "table tbody tr")) > 0 or len(d.find_elements(By.NAME, "chapterTitle")) > 0)
        except Exception:
            pass
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        for row in rows:
            try:
                cells = [cell for cell in row.find_elements(By.CSS_SELECTOR, "td") if cell.is_displayed()]
                if len(cells) < 3:
                    continue
                order_text = (cells[1].text or "").strip()
                title_text = (cells[2].text or "").strip()
                if not title_text:
                    try:
                        title_text = (cells[2].find_element(By.CSS_SELECTOR, ".truncate").text or "").strip()
                    except Exception:
                        title_text = (cells[2].text or "").strip()
                row_order_no = extract_episode_number_from_text(order_text)
                title_number = extract_episode_number_from_text(title_text)
                effective_number = title_number if title_number is not None else row_order_no
                if effective_number is None and not title_text:
                    continue
                return {
                    "order": row_order_no,
                    "title_number": title_number,
                    "sequence_number": effective_number,
                    "title": title_text,
                    "normalized_title": normalize_episode_title_text(title_text),
                }
            except Exception:
                continue
        return None

    def verify_episode_sequence_before_run(self, bot, driver, wait, files):
        latest_row = self.get_latest_episode_row_info(driver, wait)
        policy = getattr(self.job, "sequence_policy", "auto_fix") or "auto_fix"
        plan = {
            "latest_row": latest_row,
            "renumber_start": None,
            "renumber_offset": None,
        }
        if not files:
            return plan
        first_title, _ = read_chapter_file(files[0])
        first_number = extract_episode_number_from_text(first_title) or extract_episode_number_from_text(Path(files[0]).stem)
        if latest_row is None:
            bot.log("ไม่พบตารางตอนเดิมบนเว็บ จะข้ามการตรวจสอบลำดับก่อนอัปโหลด")
            return plan
        latest_number = latest_row.get("sequence_number")
        latest_order = latest_row.get("order")
        latest_title_number = latest_row.get("title_number")
        latest_title = latest_row.get("title") or ""
        normalized_latest = latest_row.get("normalized_title") or ""
        normalized_first = normalize_episode_title_text(first_title)
        if latest_number is not None:
            expected_number = latest_number + 1
            plan["renumber_start"] = expected_number
            if first_number is not None and first_number != expected_number:
                if policy == "stop":
                    raise RuntimeError(
                        f"ลำดับตอนไม่ต่อเนื่อง: ตอนล่าสุดบนเว็บคือ {latest_number} '{latest_title}' แต่ไฟล์แรกที่จะลงคือ {first_number} '{first_title}'"
                    )
                plan["renumber_offset"] = expected_number - first_number
                bot.log(
                    f"ตรวจพบลำดับไม่ต่อเนื่อง จะ fallback renumber อัตโนมัติ | ล่าสุดบนเว็บ: {latest_number} '{latest_title}'"
                    f" (title={latest_title_number if latest_title_number is not None else '-'} row={latest_order if latest_order is not None else '-'}) | "
                    f"ไฟล์แรก: {first_number} '{first_title}' | จะเริ่มลงเป็นตอนที่ {expected_number}"
                )
            elif normalized_latest and normalized_first and normalized_latest == normalized_first:
                if policy == "stop":
                    raise RuntimeError(f"ตรวจพบว่าตอนล่าสุดบนเว็บซ้ำกับไฟล์แรกที่จะลง: '{first_title}'")
                base_number = first_number if first_number is not None else latest_number
                plan["renumber_offset"] = expected_number - base_number
                bot.log(
                    f"ตรวจพบชื่อตอนแรกซ้ำกับตอนล่าสุดบนเว็บ จะ fallback renumber อัตโนมัติเป็นตอนที่ {expected_number}"
                )
            else:
                bot.log(
                    f"ตรวจสอบลำดับก่อนอัปโหลดผ่าน | ล่าสุดบนเว็บ: {latest_number} '{latest_title}'"
                    f" (title={latest_title_number if latest_title_number is not None else '-'} row={latest_order if latest_order is not None else '-'}) | "
                    f"ไฟล์แรก: {first_number if first_number is not None else '-'} '{first_title}'"
                )
        else:
            bot.log(
                f"ไม่พบเลขตอนล่าสุดบนเว็บ จะใช้ชื่อตอนเดิมในการอัปโหลด | ล่าสุดบนเว็บ: '{latest_title}'"
            )
        return plan

    def resolve_upload_title(self, original_title: str, filename: str, upload_index: int, sequence_plan):
        latest_row = (sequence_plan or {}).get("latest_row")
        renumber_start = (sequence_plan or {}).get("renumber_start")
        renumber_offset = (sequence_plan or {}).get("renumber_offset")
        if latest_row is None or renumber_start is None:
            return original_title
        original_number = extract_episode_number_from_text(original_title) or extract_episode_number_from_text(Path(filename).stem)
        if renumber_offset is not None and original_number is not None:
            target_number = original_number + renumber_offset
        else:
            target_number = renumber_start + upload_index
        return replace_episode_number_in_text(original_title, target_number)

    def verify_episode_created(self, bot, driver, wait, previous_latest_row, expected_title):
        normalized_expected = normalize_episode_title_text(expected_title)
        previous_sequence = None
        previous_order = None
        if previous_latest_row:
            previous_sequence = previous_latest_row.get("sequence_number")
            previous_order = previous_latest_row.get("order")
        latest_row = None
        deadline = time.time() + 15.0
        while time.time() < deadline:
            latest_row = self.get_latest_episode_row_info(driver, wait)
            if latest_row is None:
                time.sleep(0.5)
                continue
            normalized_latest = latest_row.get("normalized_title") or ""
            latest_sequence = latest_row.get("sequence_number")
            latest_order = latest_row.get("order")
            if normalized_expected and normalized_latest == normalized_expected:
                if previous_sequence is None or latest_sequence is None or latest_sequence >= previous_sequence:
                    return latest_row
            if previous_sequence is not None and latest_sequence is not None and latest_sequence > previous_sequence:
                return latest_row
            if previous_order is not None and latest_order is not None and latest_order > previous_order:
                return latest_row
            time.sleep(0.5)

        raise RuntimeError(
            f"ยืนยันหลังอัปโหลดไม่ผ่าน: ตอนล่าสุดบนเว็บยังไม่เปลี่ยนเป็น '{expected_title}'"
            + (f" (ล่าสุดตอนนี้: '{(latest_row or {}).get('title', '')}')" if latest_row else "")
        )

    def is_retryable_submit_error(self, error):
        if isinstance(error, (StaleElementReferenceException, TimeoutException)):
            return True
        message = f"{type(error).__name__}: {error}".lower()
        retryable_keywords = [
            "staleelementreference",
            "stale element",
            "timeout",
            "ไม่พบปุ่ม submit",
            "กดปุ่มสร้างตอนไม่สำเร็จ",
        ]
        return any(keyword in message for keyword in retryable_keywords)

    def submit_episode_with_recovery(self, bot, driver, wait, working_url, previous_latest_row, title, body_lines, price_mode, price_value, publish_mode, schedule_dt):
        try:
            bot.submit_episode(driver, wait)
            return self.verify_episode_created(bot, driver, wait, previous_latest_row, title)
        except Exception as submit_error:
            if not self.is_retryable_submit_error(submit_error):
                raise
            bot.log(f"submit ล้มเหลวแบบกู้คืนได้ จะลอง refresh/recheck | {type(submit_error).__name__}: {submit_error}")

        episode_url = self.normalize_episode_url(working_url or driver.current_url)
        if episode_url:
            bot.log(f"กำลังกลับไปหน้า episode เพื่อตรวจตอนล่าสุด: {episode_url}")
        else:
            bot.log("กำลัง refresh หน้าปัจจุบันเพื่อตรวจตอนล่าสุด")
        try:
            if episode_url:
                driver.get(episode_url)
            else:
                driver.refresh()
            self.ensure_episode_tab(driver, wait)
        except Exception as refresh_error:
            bot.log(f"กลับหน้า episode ไม่สำเร็จ แต่จะลองตรวจต่อ: {type(refresh_error).__name__}: {refresh_error}")

        try:
            latest_row = self.verify_episode_created(bot, driver, wait, previous_latest_row, title)
            bot.log("ตรวจพบว่าตอนถูกสร้างแล้วหลัง refresh จะข้ามการ submit ซ้ำ")
            return latest_row
        except Exception as verify_error:
            bot.log(f"หลัง refresh ยังไม่พบตอนล่าสุด จะ reopen ฟอร์มแล้วลองใหม่อีก 1 ครั้ง | {type(verify_error).__name__}: {verify_error}")

        self.ensure_episode_creator_open(bot, driver, wait, working_url)
        bot.fill_episode_form(driver, wait, title, body_lines, price_mode, price_value, publish_mode, schedule_dt)
        bot.submit_episode(driver, wait)
        latest_row = self.verify_episode_created(bot, driver, wait, previous_latest_row, title)
        bot.log("reopen modal และ submit ซ้ำสำเร็จ")
        return latest_row

    def ensure_episode_tab(self, driver, wait):
        episode_url = self.normalize_episode_url(driver.current_url)
        if episode_url and driver.current_url != episode_url:
            driver.get(episode_url)
        try:
            wait.until(lambda d: "tab=episode" in (d.current_url or ""))
            return driver.current_url
        except Exception:
            pass
        episode_tab_selectors = [
            "button[role='tab'][aria-controls*='episode']",
            "button[role='tab'][id*='episode']",
        ]
        for selector in episode_tab_selectors:
            try:
                tab_button = driver.find_element(By.CSS_SELECTOR, selector)
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", tab_button)
                try:
                    tab_button.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", tab_button)
                wait.until(lambda d: "tab=episode" in (d.current_url or "") or d.find_element(By.CSS_SELECTOR, "button[role='tab'][aria-controls*='episode']").get_attribute("aria-selected") == "true")
                return driver.current_url
            except Exception:
                continue
        raise RuntimeError("ไม่สามารถเปิดแท็บรายละเอียดตอนได้")

    def resolve_working_url(self, bot, driver, wait):
        working_url_override = getattr(self.job, "working_url_override", "")
        if working_url_override:
            try:
                target_url = self.normalize_episode_url(working_url_override)
                bot.log(f"ใช้ URL เรื่องที่กำหนด: {target_url}")
                driver.get(target_url)
                resolved_url = self.ensure_episode_tab(driver, wait)
                bot.log(f"เปิดหน้าลงตอนจาก URL สำเร็จ: {resolved_url}")
                return resolved_url
            except Exception as error:
                bot.log(f"ใช้ URL เรื่องไม่ได้ จะ fallback ไปค้นหาจากชื่อเรื่อง: {error}")
                driver.get("https://mynovel.co/dashboard/workings")
        try:
            driver.get("https://mynovel.co/dashboard/workings")
            target_url = self.find_working_url_from_cards(bot, driver, wait, self.job.novel_keyword)
        except Exception:
            target_url = bot.find_working_url(driver, wait, self.job.novel_keyword)
        target_url = self.normalize_episode_url(target_url)
        driver.get(target_url)
        resolved_url = self.ensure_episode_tab(driver, wait)
        bot.log(f"เปิดหน้าลงตอนจากชื่อเรื่องสำเร็จ: {resolved_url}")
        return resolved_url

    def find_working_url_from_cards(self, bot, driver, wait, keyword):
        keyword_text = (keyword or "").strip().lower()
        if not keyword_text:
            return bot.find_working_url(driver, wait, keyword)

        def open_page(page_no):
            if page_no == 1:
                return True
            pagination_links = driver.find_elements(By.CSS_SELECTOR, 'nav[aria-label="pagination"] a.cursor-pointer')
            for link in pagination_links:
                link_text = (link.text or "").strip()
                if link_text == str(page_no):
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
                    current_marker = driver.find_element(By.CSS_SELECTOR, "div.grid.grid-cols-1.xl\\:grid-cols-2")
                    try:
                        link.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", link)
                    wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "nav[aria-label='pagination'] a[aria-current='page']").text.strip() == str(page_no))
                    wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "div.grid.grid-cols-1.xl\\:grid-cols-2") != current_marker)
                    return True
            next_buttons = driver.find_elements(By.CSS_SELECTOR, 'nav[aria-label="pagination"] button')
            for button in next_buttons:
                try:
                    icon = button.find_element(By.CSS_SELECTOR, "svg.lucide-chevron-right")
                except Exception:
                    continue
                if icon is None:
                    continue
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
                try:
                    button.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", button)
                wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "nav[aria-label='pagination'] a[aria-current='page']").text.strip() == str(page_no))
                return True
            return False

        for page_no in range(1, 11):
            if not open_page(page_no):
                break
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "h3")) > 0)
            cards = driver.find_elements(By.CSS_SELECTOR, "div.rounded-xl.cursor-pointer")
            for card in cards:
                try:
                    title_el = card.find_element(By.CSS_SELECTOR, "h3")
                except Exception:
                    continue
                title_text = (title_el.text or "").strip()
                if not title_text:
                    continue
                normalized_title = title_text.lower()
                if keyword_text in normalized_title or normalized_title in keyword_text:
                    bot.log(f"พบนิยายจากการ์ดหน้า {page_no}: {title_text}")
                    current_url = driver.current_url
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", card)
                    try:
                        card.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", card)
                    wait.until(lambda d: d.current_url != current_url)
                    return self.normalize_episode_url(driver.current_url)

            if page_no >= 10:
                break
            bot.log(f"ยังไม่พบเรื่องในหน้าที่ {page_no}, กำลังไปหน้าถัดไป...")

        raise RuntimeError(f"ไม่พบเรื่องที่มีคำว่า '{keyword}' ใน dashboard/workings")

    def run_existing_driver_job(self, bot, driver):
        wait = WebDriverWait(driver, 20)
        bot.ensure_logged_in(driver, wait)
        working_url = self.resolve_working_url(bot, driver, wait)
        bot.log(f"พบนิยายแล้ว: {working_url}")

        files = list(self.job.chapter_files)
        schedule_plan = []
        if self.job.publish_mode == "scheduled":
            schedule_plan = compute_schedule_datetimes(len(files), ScheduleConfig(**self.job.schedule))
        sequence_plan = self.verify_episode_sequence_before_run(bot, driver, wait, files)
        previous_latest_row = sequence_plan.get("latest_row")
        uploaded_count = 0
        total_elapsed_seconds = 0.0
        run_started_at = datetime.now()

        for index, (original_index, filename) in enumerate(enumerate(files)):
            original_title, body = read_chapter_file(filename)
            schedule_dt = schedule_plan[index] if self.job.publish_mode == "scheduled" else None
            title = self.resolve_upload_title(original_title, filename, index, sequence_plan)
            body_lines = body
            current_price_mode = self.job.price_mode
            current_price_value = self.job.price_value
            if self.job.price_mode == "auto_free":
                auto_free_details = self.job.auto_free_details or {}
                episode_number_in_title = extract_episode_number_from_text(original_title) or extract_episode_number_from_text(Path(filename).stem)
                check_index = episode_number_in_title if episode_number_in_title is not None else (original_index + 1)
                is_free = is_episode_free_by_rule(
                    check_index,
                    auto_free_details.get("rule", ""),
                    auto_free_details.get("custom_numbers", ""),
                )
                if not is_free:
                    free_chapters = set(auto_free_details.get("free_chapters_list", []))
                    is_free = check_index in free_chapters
                current_price_mode = "free" if is_free else "paid"

            bot.log(f"เริ่มอัปโหลด ({index + 1}/{len(files)}): {Path(filename).name}")
            if title != original_title:
                bot.log(f"fallback renumber title: '{original_title}' -> '{title}'")
            if current_price_mode == "paid":
                bot.log(f"pricing: paid {current_price_value} coins | episode_number={check_index if self.job.price_mode == 'auto_free' else '-'} | chapter_index={original_index + 1}")
            else:
                bot.log(f"pricing: free | episode_number={check_index if self.job.price_mode == 'auto_free' else '-'} | chapter_index={original_index + 1}")
            episode_started = time.perf_counter()
            try:
                step_started = time.perf_counter()
                self.ensure_episode_creator_open(bot, driver, wait, working_url)
                bot.log(f"เวลาเปิดฟอร์มเพิ่มตอน: {time.perf_counter() - step_started:.2f}s")
                step_started = time.perf_counter()
                bot.fill_episode_form(driver, wait, title, body_lines, current_price_mode, current_price_value, self.job.publish_mode, schedule_dt)
                bot.log(f"เวลาตั้งค่าฟอร์มตอน: {time.perf_counter() - step_started:.2f}s")
                step_started = time.perf_counter()
                previous_latest_row = self.submit_episode_with_recovery(
                    bot,
                    driver,
                    wait,
                    working_url,
                    previous_latest_row,
                    title,
                    body_lines,
                    current_price_mode,
                    current_price_value,
                    self.job.publish_mode,
                    schedule_dt,
                )
                bot.log(f"เวลากดสร้างตอน: {time.perf_counter() - step_started:.2f}s")
                bot.log(f"เวลายืนยันตอนล่าสุดหลังอัปโหลด: {time.perf_counter() - step_started:.2f}s")
                elapsed_seconds = time.perf_counter() - episode_started
                uploaded_count += 1
                total_elapsed_seconds += elapsed_seconds
                avg_seconds = total_elapsed_seconds / uploaded_count
                remaining_count = len(files) - (index + 1)
                eta_seconds = avg_seconds * remaining_count
                est_time = datetime.now() + timedelta(seconds=eta_seconds)
                bot.log(
                    f"สำเร็จ: {Path(filename).name} | ใช้เวลา {self.format_duration(elapsed_seconds)} | "
                    f"เฉลี่ย {self.format_duration(avg_seconds)} | "
                    f"เหลือประมาณ {self.format_duration(eta_seconds)} | EST {est_time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
            except Exception:
                elapsed_seconds = time.perf_counter() - episode_started
                remaining_count = len(files) - index
                avg_seconds = (total_elapsed_seconds / uploaded_count) if uploaded_count else elapsed_seconds
                eta_seconds = avg_seconds * remaining_count
                est_time = datetime.now() + timedelta(seconds=eta_seconds)
                bot.log(
                    f"ตอนปัจจุบันใช้เวลาไปแล้ว {self.format_duration(elapsed_seconds)} | "
                    f"คาดว่าเหลือประมาณ {self.format_duration(eta_seconds)} | EST {est_time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                raise

        total_run_seconds = (datetime.now() - run_started_at).total_seconds()
        if uploaded_count:
            bot.log(
                f"สรุปเวลา: อัปโหลด {uploaded_count} ตอน | รวม {self.format_duration(total_run_seconds)} | "
                f"เฉลี่ย {self.format_duration(total_elapsed_seconds / uploaded_count)}"
            )

    def run(self):
        try:
            bot = MyNovelBot(lambda message: self.status.emit(str(message)))
            self.patch_bot(bot)
            if self.existing_driver is not None:
                for index, job in enumerate(self.jobs):
                    self.job = job
                    self.status.emit(f"เริ่มงานนิยาย {index + 1}/{len(self.jobs)}: {(job.novel_keyword or getattr(job, 'working_url_override', '') or 'นิยายใหม่')}")
                    try:
                        self.run_existing_driver_job(bot, self.existing_driver)
                        self.job_finished.emit(index, True, "สำเร็จ")
                    except Exception as error:
                        error_message = str(error).strip() or repr(error)
                        self.job_finished.emit(index, False, f"{type(error).__name__}: {error_message}")
                        raise
            else:
                for index, job in enumerate(self.jobs):
                    self.job = job
                    self.status.emit(f"เริ่มงานนิยาย {index + 1}/{len(self.jobs)}: {(job.novel_keyword or getattr(job, 'working_url_override', '') or 'นิยายใหม่')}")
                    try:
                        driver = bot.create_driver(job.chrome_path, self.profile, headless=job.headless)
                        try:
                            self.run_existing_driver_job(bot, driver)
                        finally:
                            bot.cleanup_driver(driver)
                        self.job_finished.emit(index, True, "สำเร็จ")
                    except Exception as error:
                        error_message = str(error).strip() or repr(error)
                        self.job_finished.emit(index, False, f"{type(error).__name__}: {error_message}")
                        raise
            self.finished.emit(True, "อัปโหลดเสร็จสิ้น")
        except Exception as error:
            error_message = str(error).strip() or repr(error)
            self.finished.emit(False, f"{type(error).__name__}: {error_message}")


class UploaderGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = read_json(QT_SETTINGS_FILE, read_json(OLD_QT_SETTINGS_FILE, {}))
        self.novel_list = load_novel_list()
        self.file_paths = []
        self.tasks = []
        self.current_task_index = -1
        self.last_novel_folder_path = self.settings.get("last_novel_folder_path", "")
        self.last_novel_list_path = self.settings.get("last_novel_list_path", "")
        self._initializing = True
        self.login_bot = None
        self.login_driver = None
        self.thread = None
        self.worker = None
        self.running_task_indices = []
        self.parallel_threads = []
        self.parallel_workers = []
        self.parallel_pending_count = 0
        self.parallel_results = {}
        self.profile_items = []
        self.profile_map = {}
        self.init_ui()
        self.load_ui_settings()
        self.refresh_profile_combo()
        self.ensure_task_exists()
        self.refresh_task_list_widget()
        if self.tasks:
            self.load_task_into_form(self.tasks[self.current_task_index if self.current_task_index >= 0 else 0])
        self._initializing = False

    def init_ui(self):
        self.setWindowTitle("โปรแกรมอัปโหลดนิยายอัตโนมัติ - Enhanced Version")
        self.setWindowIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        screen = self.screen().availableGeometry()
        screen_width = screen.width()
        screen_height = screen.height()
        min_width = min(900, int(screen_width * 0.75))
        min_height = min(720, int(screen_height * 0.75))
        self.setMinimumSize(min_width, min_height)
        default_width = min(int(screen_width * 0.88), 1400)
        default_height = min(int(screen_height * 0.9), 950)
        self.resize(default_width, default_height)
        self.center()

        outer_layout = QVBoxLayout(self)
        margin = max(8, min(12, int(screen_width * 0.01)))
        outer_layout.setContentsMargins(margin, margin, margin, margin)
        outer_layout.setSpacing(max(5, int(margin * 0.8)))

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_margin = max(15, min(20, int(screen_width * 0.02)))
        content_spacing = max(10, int(content_margin * 0.75))
        content_layout.setContentsMargins(content_margin, content_margin, content_margin, content_margin)
        content_layout.setSpacing(content_spacing)

        instruction_group = QGroupBox("📋 คำแนะนำ")
        instruction_layout = QVBoxLayout()
        instruction_label = QLabel(
            "1. เลือกหรือพิมพ์ชื่อนิยายสำหรับใช้ค้นหาใน MyNovel\n"
            "2. เลือกไฟล์ตอน .txt ที่ต้องการอัปโหลด\n"
            "3. เลือก Chrome profile หรือ Guest mode แล้วกดเปิดหน้า Login\n"
            "4. ตั้งราคา / การเผยแพร่ / เวลา จากนั้นกดเริ่มอัปโหลด"
        )
        instruction_label.setObjectName("InstructionLabel")
        instruction_layout.addWidget(instruction_label)
        instruction_group.setLayout(instruction_layout)

        task_group = QGroupBox("🗂️ งานนิยายที่รออัปโหลด")
        task_layout = QHBoxLayout()
        self.task_list_widget = QListWidget()
        self.task_list_widget.setMinimumHeight(180)
        self.task_list_widget.setMinimumWidth(260)
        self.task_list_widget.currentRowChanged.connect(self.on_task_selected)
        self.task_list_widget.itemChanged.connect(self.on_task_item_changed)
        task_layout.addWidget(self.task_list_widget, 1)
        task_button_layout = QVBoxLayout()
        self.add_task_button = QPushButton("➕ เพิ่มนิยาย")
        self.add_task_button.clicked.connect(self.add_task)
        task_button_layout.addWidget(self.add_task_button)
        self.remove_task_button = QPushButton("🗑️ ลบนิยาย")
        self.remove_task_button.clicked.connect(self.remove_selected_task)
        task_button_layout.addWidget(self.remove_task_button)
        self.task_count_label = QLabel("ทั้งหมด 0 งาน")
        task_button_layout.addWidget(self.task_count_label)
        task_button_layout.addStretch(1)
        task_layout.addLayout(task_button_layout)
        task_group.setLayout(task_layout)

        novel_select_group = QGroupBox("📚 1. ชื่อเรื่อง (ใส่ให้ตรงกับเรื่องบนเว็บ)")
        novel_select_layout = QVBoxLayout()
        novel_row = QHBoxLayout()
        self.novel_combo = QComboBox()
        self.novel_combo.setEditable(True)
        self.novel_combo.setPlaceholderText("พิมพ์หรือเลือกชื่อเรื่อง...")
        self.novel_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        novel_row.addWidget(self.novel_combo, 3)

        load_list_btn = QPushButton("📂 โหลดรายการ")
        load_list_btn.clicked.connect(self.load_novel_list_from_file)
        novel_row.addWidget(load_list_btn)

        add_novel_btn = QPushButton("➕ เพิ่ม")
        add_novel_btn.clicked.connect(self.add_current_novel)
        novel_row.addWidget(add_novel_btn)

        remove_novel_btn = QPushButton("🗑️ ลบ")
        remove_novel_btn.clicked.connect(self.remove_current_novel)
        novel_row.addWidget(remove_novel_btn)

        novel_select_layout.addLayout(novel_row)
        work_url_row = QHBoxLayout()
        work_url_row.addWidget(QLabel("URL เรื่อง:"))
        self.work_url_edit = QLineEdit()
        self.work_url_edit.setPlaceholderText("https://mynovel.co/dashboard/workings/... หรือ URL หน้า detail/episode")
        work_url_row.addWidget(self.work_url_edit, 1)
        novel_select_layout.addLayout(work_url_row)
        hint_label = QLabel("💡 ใช้การค้นหาแบบ contains ดังนั้นใส่ชื่อสั้นกว่าได้ถ้าเป็นส่วนหนึ่งของชื่อเต็ม")
        hint_label.setStyleSheet("color: #EBCB8B; font-size: 10pt; font-style: italic;")
        novel_select_layout.addWidget(hint_label)
        self.novel_count_label = QLabel("รายการทั้งหมด: 0 รายการ")
        self.novel_count_label.setStyleSheet("color: #88C0D0; font-size: 10pt;")
        novel_select_layout.addWidget(self.novel_count_label)
        novel_select_group.setLayout(novel_select_layout)

        file_group = QGroupBox("📁 2. เลือกไฟล์นิยาย")
        file_layout = QHBoxLayout()
        self.file_label = QLabel("ยังไม่ได้เลือกไฟล์...")
        self.file_label.setObjectName("FileLabel")
        self.file_label.setWordWrap(True)
        self.file_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        select_files_button = QPushButton("  เลือกไฟล์ (.txt)")
        select_files_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        select_files_button.clicked.connect(self.select_files)
        file_layout.addWidget(self.file_label, 1)
        file_layout.addWidget(select_files_button)
        file_group.setLayout(file_layout)

        browser_group = QGroupBox("🌐 MyNovel / Chrome Profile")
        browser_layout = QGridLayout()
        browser_layout.addWidget(QLabel("Chrome path:"), 0, 0)
        self.chrome_path_edit = QLineEdit()
        browser_layout.addWidget(self.chrome_path_edit, 0, 1, 1, 2)
        auto_detect_button = QPushButton("Auto Detect")
        auto_detect_button.clicked.connect(self.auto_detect_chrome)
        browser_layout.addWidget(auto_detect_button, 0, 3)

        browser_layout.addWidget(QLabel("Profile mode:"), 1, 0)
        profile_mode_layout = QHBoxLayout()
        self.automation_profile_radio = QRadioButton("Automation Profile")
        self.automation_profile_radio.setChecked(True)
        self.installed_profile_radio = QRadioButton("Installed Profile")
        self.guest_profile_radio = QRadioButton("Guest Mode")
        self.custom_profile_radio = QRadioButton("Custom Folder")
        self.automation_profile_radio.toggled.connect(self.toggle_profile_mode)
        self.installed_profile_radio.toggled.connect(self.toggle_profile_mode)
        self.guest_profile_radio.toggled.connect(self.toggle_profile_mode)
        self.custom_profile_radio.toggled.connect(self.toggle_profile_mode)
        profile_mode_layout.addWidget(self.automation_profile_radio)
        profile_mode_layout.addWidget(self.installed_profile_radio)
        profile_mode_layout.addWidget(self.guest_profile_radio)
        profile_mode_layout.addWidget(self.custom_profile_radio)
        browser_layout.addLayout(profile_mode_layout, 1, 1, 1, 3)

        browser_layout.addWidget(QLabel("Detected profiles:"), 2, 0)
        self.profile_combo = QComboBox()
        self.profile_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        browser_layout.addWidget(self.profile_combo, 2, 1, 1, 2)
        refresh_profile_button = QPushButton("Refresh")
        refresh_profile_button.clicked.connect(self.refresh_profile_combo)
        browser_layout.addWidget(refresh_profile_button, 2, 3)

        browser_layout.addWidget(QLabel("Custom folder:"), 3, 0)
        self.custom_profile_edit = QLineEdit()
        browser_layout.addWidget(self.custom_profile_edit, 3, 1, 1, 2)
        browse_profile_button = QPushButton("เลือกโฟลเดอร์")
        browse_profile_button.clicked.connect(self.browse_custom_profile)
        browser_layout.addWidget(browse_profile_button, 3, 3)

        self.headless_checkbox = QCheckBox("ซ่อนหน้าต่างเบราว์เซอร์ระหว่างอัปโหลด")
        browser_layout.addWidget(self.headless_checkbox, 4, 0, 1, 2)
        self.login_button = QPushButton("เปิดหน้า Login MyNovel")
        self.login_button.clicked.connect(self.open_login_page)
        browser_layout.addWidget(self.login_button, 4, 2)
        self.close_login_button = QPushButton("ปิดหน้า Login")
        self.close_login_button.clicked.connect(self.close_login_browser)
        browser_layout.addWidget(self.close_login_button, 4, 3)
        browser_group.setLayout(browser_layout)

        mode_select_group = QGroupBox("� 4. โหมดการทำงาน")
        mode_select_layout = QVBoxLayout()
        self.cloudflare_mode_radio = QRadioButton("โหมด Cloudflare (ทำงานช้าแต่ปลอดภัย, แนะนำ)")
        self.fast_mode_radio = QRadioButton("โหมด Fast (ทำงานเร็ว, อาจถูกตรวจจับได้)")
        self.fast_mode_radio.setChecked(True)
        mode_select_layout.addWidget(self.cloudflare_mode_radio)
        mode_select_layout.addWidget(self.fast_mode_radio)
        mode_select_group.setLayout(mode_select_layout)

        settings_layout = QHBoxLayout()
        settings_layout.setSpacing(20)

        mode_group = QGroupBox("🎯 5. โหมดตอน")
        mode_layout = QVBoxLayout()
        self.price_mode_group = QButtonGroup(self)
        self.free_radio = QRadioButton("ตอนฟรี (ไม่ขาย)")
        self.free_radio.setChecked(True)
        self.sell_radio = QRadioButton("ตอนติดเหรียญ (ขายตลอดไป)")
        self.auto_free_radio = QRadioButton("เลือกตอนฟรีอัตโนมัติ (ตอนอื่นขายตามราคา)")
        self.free_radio.toggled.connect(lambda _: self.toggle_auto_free_options(self.auto_free_radio.isChecked()))
        self.sell_radio.toggled.connect(lambda _: self.toggle_auto_free_options(self.auto_free_radio.isChecked()))
        self.auto_free_radio.toggled.connect(self.toggle_auto_free_options)
        self.price_mode_group.addButton(self.free_radio)
        self.price_mode_group.addButton(self.sell_radio)
        self.price_mode_group.addButton(self.auto_free_radio)
        mode_layout.addWidget(self.free_radio)
        mode_layout.addWidget(self.sell_radio)
        mode_layout.addWidget(self.auto_free_radio)
        mode_group.setLayout(mode_layout)

        publish_group = QGroupBox("⏰ 6. รูปแบบการเผยแพร่")
        publish_layout = QVBoxLayout()
        self.publish_mode_group = QButtonGroup(self)
        self.draft_radio = QRadioButton("ไม่เผยแพร่")
        self.publish_radio = QRadioButton("เผยแพร่ทันที")
        self.publish_radio.setChecked(True)
        self.schedule_radio = QRadioButton("ตั้งเวลาล่วงหน้า")
        self.schedule_radio.toggled.connect(self.toggle_schedule_options)
        self.schedule_radio.toggled.connect(self.toggle_time_filter_group)
        self.publish_mode_group.addButton(self.draft_radio)
        self.publish_mode_group.addButton(self.publish_radio)
        self.publish_mode_group.addButton(self.schedule_radio)
        publish_layout.addWidget(self.draft_radio)
        publish_layout.addWidget(self.publish_radio)
        publish_layout.addWidget(self.schedule_radio)
        publish_group.setLayout(publish_layout)

        settings_layout.addWidget(mode_group)
        settings_layout.addWidget(publish_group)

        self.schedule_group = QGroupBox("📅 7. ตั้งค่าเวลาเผยแพร่")
        schedule_layout = QGridLayout()
        grid_spacing = max(10, min(15, int(screen_width * 0.015)))
        schedule_layout.setSpacing(grid_spacing)
        schedule_layout.addWidget(QLabel("วันที่เริ่ม:"), 0, 0)
        self.start_date_edit = QDateEdit(calendarPopup=True)
        self.start_date_edit.setDate(QDate.currentDate())
        self.start_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.start_date_edit.dateChanged.connect(self.check_year_warning)
        schedule_layout.addWidget(self.start_date_edit, 0, 1)

        schedule_layout.addWidget(QLabel("เวลาเริ่ม (24 ชม.):"), 0, 2)
        self.start_time_edit = QTimeEdit()
        self.start_time_edit.setDisplayFormat("HH:mm")
        self.start_time_edit.setTime(QTime(17, 0))
        schedule_layout.addWidget(self.start_time_edit, 0, 3)

        schedule_layout.addWidget(QLabel("จำนวนบทต่อวัน:"), 1, 0)
        self.chapters_per_day_spin = QSpinBox()
        self.chapters_per_day_spin.setRange(1, 100)
        self.chapters_per_day_spin.setValue(5)
        schedule_layout.addWidget(self.chapters_per_day_spin, 1, 1)

        schedule_layout.addWidget(QLabel("ระยะห่างระหว่างตอน:"), 1, 2)
        interval_layout = QHBoxLayout()
        self.interval_value_spin = QSpinBox()
        self.interval_value_spin.setRange(1, 1440)
        self.interval_value_spin.setValue(10)
        interval_layout.addWidget(self.interval_value_spin)
        self.interval_unit_minutes = QRadioButton("นาที")
        self.interval_unit_hours = QRadioButton("ชั่วโมง")
        self.interval_unit_minutes.setChecked(True)
        interval_layout.addWidget(self.interval_unit_minutes)
        interval_layout.addWidget(self.interval_unit_hours)
        interval_layout.addStretch(1)
        schedule_layout.addLayout(interval_layout, 1, 3)

        schedule_layout.addWidget(QLabel("ตัวเลือก:"), 2, 0)
        self.check_duplicate_same_day = QCheckBox("จำกัดจำนวนตอนต่อวัน")
        self.check_duplicate_same_day.setChecked(True)
        schedule_layout.addWidget(self.check_duplicate_same_day, 2, 1, 1, 3)
        self.schedule_group.setLayout(schedule_layout)
        self.toggle_schedule_options(False)

        self.sequence_policy_group = QGroupBox("🧠 7. นโยบายลำดับตอน")
        sequence_policy_layout = QVBoxLayout()
        self.sequence_policy_button_group = QButtonGroup(self)
        self.sequence_stop_radio = QRadioButton("ถ้าเลขตอน/ชื่อตอนไม่ต่อ ให้หยุดและแจ้ง")
        self.sequence_auto_fix_radio = QRadioButton("ถ้าเลขตอน/ชื่อตอนไม่ต่อ ให้แก้เลขตอนอัตโนมัติ")
        self.sequence_auto_fix_radio.setChecked(True)
        self.sequence_policy_button_group.addButton(self.sequence_stop_radio)
        self.sequence_policy_button_group.addButton(self.sequence_auto_fix_radio)
        sequence_policy_layout.addWidget(self.sequence_stop_radio)
        sequence_policy_layout.addWidget(self.sequence_auto_fix_radio)
        self.sequence_policy_group.setLayout(sequence_policy_layout)

        self.time_filter_group = QGroupBox("🚫 ข้ามเวลาลง (Time Filter)")
        time_filter_layout = QVBoxLayout()
        time_filter_layout.setSpacing(15)
        self.time_filter_checkbox = QCheckBox("เปิดใช้งานการข้ามเวลาลง")
        self.time_filter_checkbox.setChecked(False)
        self.time_filter_checkbox.stateChanged.connect(self.toggle_time_filter_options)
        time_filter_layout.addWidget(self.time_filter_checkbox)
        time_input_layout = QGridLayout()
        time_input_layout.setSpacing(grid_spacing)
        time_input_layout.addWidget(QLabel("ข้ามเวลาตั้งแต่:"), 0, 0)
        self.skip_start_time_edit = QTimeEdit()
        self.skip_start_time_edit.setDisplayFormat("HH:mm")
        self.skip_start_time_edit.setTime(QTime(0, 0))
        time_input_layout.addWidget(self.skip_start_time_edit, 0, 1)
        time_input_layout.addWidget(QLabel("ถึงเวลา:"), 0, 2)
        self.skip_end_time_edit = QTimeEdit()
        self.skip_end_time_edit.setDisplayFormat("HH:mm")
        self.skip_end_time_edit.setTime(QTime(6, 0))
        time_input_layout.addWidget(self.skip_end_time_edit, 0, 3)
        time_filter_layout.addLayout(time_input_layout)
        self.time_filter_group.setLayout(time_filter_layout)
        self.toggle_time_filter_group(False)
        self.toggle_time_filter_options(Qt.CheckState.Unchecked.value)

        self.auto_free_group = QGroupBox("🎲 8. ตั้งค่าตอนฟรีอัตโนมัติ / ราคาตอนขาย")
        auto_free_layout = QGridLayout()
        auto_free_layout.setSpacing(grid_spacing)
        self.free_rule_label = QLabel("กฎการเลือกตอนฟรี (ตอนที่ไม่เข้า rule จะขายตามราคา):")
        auto_free_layout.addWidget(self.free_rule_label, 0, 0)
        self.free_rule_combo = QComboBox()
        self.free_rule_combo.addItems([
            "ตอนที่ลงท้ายด้วย 5, 0 (เช่น 5, 10, 15, 20...)",
            "ตอนที่ลงท้ายด้วย 0 (เช่น 10, 20, 30...)",
            "ตอนที่ลงท้ายด้วย 5 (เช่น 5, 15, 25...)",
            "ทุก 3 ตอน (เช่น 3, 6, 9, 12...)",
            "ทุก 5 ตอน (เช่น 5, 10, 15, 20...)",
            "ทุก 10 ตอน (เช่น 10, 20, 30...)",
            "กำหนดเอง",
        ])
        self.free_rule_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        auto_free_layout.addWidget(self.free_rule_combo, 0, 1, 1, 3)
        self.custom_free_label = QLabel("กำหนดเอง (เช่น 1,5,10,15):")
        auto_free_layout.addWidget(self.custom_free_label, 1, 0)
        self.custom_free_input = QLineEdit()
        self.custom_free_input.setPlaceholderText("ใส่หมายเลขตอนที่ต้องการให้ฟรี คั่นด้วยเครื่องหมายจุลภาค")
        self.custom_free_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        auto_free_layout.addWidget(self.custom_free_input, 1, 1, 1, 3)
        self.price_spin_label = QLabel("ราคาเหรียญตอนขาย:")
        auto_free_layout.addWidget(self.price_spin_label, 2, 0)
        self.price_spin = QSpinBox()
        self.price_spin.setRange(0, 999)
        self.price_spin.setValue(DEFAULT_PRICE)
        auto_free_layout.addWidget(self.price_spin, 2, 1)
        self.auto_free_group.setLayout(auto_free_layout)
        self.toggle_auto_free_options(False)

        button_layout = QHBoxLayout()
        cancel_button = QPushButton("  ยกเลิก")
        cancel_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton))
        cancel_button.clicked.connect(self.close)
        self.calc_button = QPushButton("  คำนวณวันเสร็จ")
        self.calc_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        self.calc_button.clicked.connect(self.show_estimate_dialog)
        self.calc_button.setEnabled(self.schedule_radio.isChecked())
        self.run_current_button = QPushButton("  รันเรื่องที่เลือก")
        self.run_current_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.run_current_button.clicked.connect(self.apply_current_task)
        self.start_button = QPushButton("  รันทั้งหมด (คิว)")
        self.start_button.setObjectName("StartButton")
        self.start_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOkButton))
        self.start_button.clicked.connect(self.apply_settings)
        self.enqueue_selected_button = QPushButton("  เพิ่มเข้าคิวที่เลือก")
        self.enqueue_selected_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp))
        self.enqueue_selected_button.clicked.connect(self.enqueue_selected_tasks)
        self.parallel_button = QPushButton("  รันพร้อมกัน")
        self.parallel_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowRight))
        self.parallel_button.clicked.connect(self.apply_parallel_settings)
        self.max_parallel_spin = QSpinBox()
        self.max_parallel_spin.setRange(1, 20)
        self.max_parallel_spin.setValue(int(self.settings.get("max_parallel_jobs", 5) or 5))
        self.start_button.setDefault(True)
        button_layout.addStretch(1)
        button_layout.addWidget(cancel_button)
        button_layout.addWidget(self.calc_button)
        button_layout.addWidget(self.run_current_button)
        button_layout.addWidget(self.enqueue_selected_button)
        button_layout.addWidget(QLabel("พร้อมกันสูงสุด:"))
        button_layout.addWidget(self.max_parallel_spin)
        button_layout.addWidget(self.parallel_button)
        button_layout.addWidget(self.start_button)

        self.status_label = QLabel("พร้อมใช้งาน")
        self.status_label.setObjectName("StatusLabel")

        content_layout.addWidget(instruction_group)
        content_layout.addWidget(task_group)
        content_layout.addWidget(novel_select_group)
        content_layout.addWidget(file_group)
        content_layout.addWidget(browser_group)
        content_layout.addWidget(mode_select_group)
        content_layout.addLayout(settings_layout)
        content_layout.addWidget(self.schedule_group)
        content_layout.addWidget(self.sequence_policy_group)
        content_layout.addWidget(self.auto_free_group)
        content_layout.addWidget(self.time_filter_group)
        content_layout.addStretch(1)

        scroll_area.setWidget(content_widget)
        outer_layout.addWidget(scroll_area)
        outer_layout.addWidget(self.status_label)
        outer_layout.addLayout(button_layout)
        self.center()
        self.update_novel_combo()
        self.ensure_task_exists()
        self.refresh_task_list_widget()
        if self.tasks:
            self.load_task_into_form(self.tasks[0])

    def load_ui_settings(self):
        chrome_path = self.settings.get("chrome_path", detect_chrome_path())
        self.chrome_path_edit.setText(chrome_path)
        schedule_date_text = self.settings.get("schedule_date", "") or QDate.currentDate().toString("yyyy-MM-dd")
        run_mode = self.settings.get("run_mode", "fast")
        self.cloudflare_mode_radio.setChecked(run_mode == "cloudflare")
        self.fast_mode_radio.setChecked(run_mode != "cloudflare")
        browser_mode = self.settings.get("browser_mode", "automation")
        if browser_mode == "guest":
            self.guest_profile_radio.setChecked(True)
        elif browser_mode == "custom":
            self.custom_profile_radio.setChecked(True)
        elif browser_mode == "installed":
            self.installed_profile_radio.setChecked(True)
        else:
            self.automation_profile_radio.setChecked(True)
        self.custom_profile_edit.setText(self.settings.get("custom_profile_path", ""))
        self.headless_checkbox.setChecked(bool(self.settings.get("headless", False)))
        self.price_spin.setValue(int(self.settings.get("price_value", DEFAULT_PRICE)))
        price_mode = self.settings.get("price_mode", "free")
        self.set_price_mode(price_mode)
        publish_mode = self.settings.get("publish_mode", "published")
        self.set_publish_mode_ui(publish_mode)
        self.start_date_edit.setDate(QDate.fromString(schedule_date_text, "yyyy-MM-dd"))
        self.start_time_edit.setTime(QTime.fromString(self.settings.get("schedule_time", "17:00"), "HH:mm"))
        self.chapters_per_day_spin.setValue(int(self.settings.get("chapters_per_day", 5)))
        self.interval_value_spin.setValue(int(self.settings.get("interval_value", 10)))
        self.interval_unit_hours.setChecked(self.settings.get("interval_unit", "minutes") == "hours")
        self.interval_unit_minutes.setChecked(self.settings.get("interval_unit", "minutes") != "hours")
        self.check_duplicate_same_day.setChecked(bool(self.settings.get("limit_per_day", True)))
        self.time_filter_checkbox.setChecked(bool(self.settings.get("time_filter_enabled", False)))
        self.skip_start_time_edit.setTime(QTime.fromString(self.settings.get("skip_start", "00:00"), "HH:mm"))
        self.skip_end_time_edit.setTime(QTime.fromString(self.settings.get("skip_end", "06:00"), "HH:mm"))
        self.free_rule_combo.setCurrentText(self.settings.get("free_rule", self.free_rule_combo.currentText()))
        self.custom_free_input.setText(self.settings.get("custom_free_input", ""))
        self.work_url_edit.setText(self.settings.get("work_url", ""))
        task_data = self.settings.get("tasks", [])
        parsed_tasks = []
        if isinstance(task_data, list):
            for item in task_data:
                if not isinstance(item, dict):
                    continue
                parsed_tasks.append(
                    NovelTask(
                        title_keyword=item.get("title_keyword", ""),
                        working_url_override=item.get("working_url_override", ""),
                        chapter_files=sort_file_paths(list(item.get("chapter_files", []))),
                        run_mode=item.get("run_mode", self.settings.get("run_mode", "fast")),
                        headless=bool(item.get("headless", self.settings.get("headless", False))),
                        sequence_policy=item.get("sequence_policy", "auto_fix"),
                        price_mode=item.get("price_mode", "free"),
                        price_value=int(item.get("price_value", DEFAULT_PRICE)),
                        publish_mode=item.get("publish_mode", "published"),
                        schedule=item.get("schedule", {}),
                        auto_free_details=item.get("auto_free_details") or {},
                        selected=bool(item.get("selected", False)),
                        status=item.get("status", "pending"),
                        last_error=item.get("last_error", ""),
                    )
                )
        if parsed_tasks:
            self.tasks = parsed_tasks
        else:
            fallback_task = self.create_empty_task()
            fallback_task.title_keyword = self.novel_combo.currentText().strip() if self.novel_combo.currentText().strip() != "-- เลือกหรือพิมพ์ชื่อเรื่อง --" else ""
            fallback_task.working_url_override = self.work_url_edit.text().strip()
            fallback_task.chapter_files = sort_file_paths(list(self.file_paths))
            fallback_task.run_mode = "cloudflare" if self.cloudflare_mode_radio.isChecked() else "fast"
            fallback_task.headless = self.headless_checkbox.isChecked()
            fallback_task.sequence_policy = "stop" if self.sequence_stop_radio.isChecked() else "auto_fix"
            fallback_task.price_mode = self.get_price_mode()
            fallback_task.price_value = self.price_spin.value()
            fallback_task.publish_mode = self.get_publish_mode()
            fallback_task.schedule = self.collect_schedule()
            fallback_task.auto_free_details = {
                "rule": self.free_rule_combo.currentText(),
                "custom_numbers": self.custom_free_input.text().strip(),
                "free_chapters_list": self.get_free_chapters_list(len(self.file_paths)),
            }
            self.tasks = [fallback_task]
        self.current_task_index = 0
        self.toggle_profile_mode()
        self.toggle_auto_free_options(self.auto_free_radio.isChecked())

    def save_settings(self):
        self.save_current_task_from_form()
        data = {
            "last_novel_folder_path": self.last_novel_folder_path,
            "last_novel_list_path": self.last_novel_list_path,
            "chrome_path": self.chrome_path_edit.text().strip(),
            "run_mode": "cloudflare" if self.cloudflare_mode_radio.isChecked() else "fast",
            "browser_mode": self.get_browser_mode(),
            "selected_profile_label": self.profile_combo.currentText().strip(),
            "custom_profile_path": self.custom_profile_edit.text().strip(),
            "headless": self.headless_checkbox.isChecked(),
            "price_mode": self.get_price_mode(),
            "price_value": self.price_spin.value(),
            "publish_mode": self.get_publish_mode(),
            "schedule_date": self.start_date_edit.date().toString("yyyy-MM-dd"),
            "schedule_time": self.start_time_edit.time().toString("HH:mm"),
            "chapters_per_day": self.chapters_per_day_spin.value(),
            "interval_value": self.interval_value_spin.value(),
            "interval_unit": "hours" if self.interval_unit_hours.isChecked() else "minutes",
            "limit_per_day": self.check_duplicate_same_day.isChecked(),
            "time_filter_enabled": self.time_filter_checkbox.isChecked(),
            "skip_start": self.skip_start_time_edit.time().toString("HH:mm"),
            "skip_end": self.skip_end_time_edit.time().toString("HH:mm"),
            "free_rule": self.free_rule_combo.currentText(),
            "custom_free_input": self.custom_free_input.text().strip(),
            "work_url": self.work_url_edit.text().strip(),
            "tasks": [
                {
                    "title_keyword": task.title_keyword,
                    "working_url_override": task.working_url_override,
                    "chapter_files": list(task.chapter_files),
                    "run_mode": task.run_mode,
                    "headless": task.headless,
                    "sequence_policy": task.sequence_policy,
                    "price_mode": task.price_mode,
                    "price_value": task.price_value,
                    "publish_mode": task.publish_mode,
                    "schedule": task.schedule,
                    "auto_free_details": task.auto_free_details or {},
                    "selected": bool(task.selected),
                    "status": task.status,
                    "last_error": task.last_error,
                }
                for task in self.tasks
            ],
            "max_parallel_jobs": self.max_parallel_spin.value(),
        }
        write_json(QT_SETTINGS_FILE, data)

    def closeEvent(self, event):
        self.save_current_task_from_form()
        self.save_settings()
        self.close_login_browser()
        event.accept()

    def center(self):
        screen_geometry = self.screen().availableGeometry()
        window_geometry = self.frameGeometry()
        window_geometry.moveCenter(screen_geometry.center())
        self.move(window_geometry.topLeft())

    def update_status(self, message):
        self.status_label.setText(message)
        print(message)

    def create_empty_task(self):
        return NovelTask(
            schedule={
                "start_date": QDate.currentDate().toString("yyyy-MM-dd"),
                "start_time": "17:00",
                "chapters_per_day": 5,
                "limit_per_day_enabled": True,
                "interval_minutes": 10,
                "skip_enabled": False,
                "skip_start": "00:00",
                "skip_end": "06:00",
            },
            auto_free_details={
                "rule": "ตอนที่ลงท้ายด้วย 5, 0 (เช่น 5, 10, 15, 20...)",
                "custom_numbers": "",
                "free_chapters_list": [],
            },
        )

    def ensure_task_exists(self):
        if not self.tasks:
            self.tasks = [self.create_empty_task()]
            self.current_task_index = 0

    def refresh_task_list_widget(self, selected_row=None):
        current_row = self.current_task_index if selected_row is None else selected_row
        self.task_list_widget.blockSignals(True)
        self.task_list_widget.clear()
        for index, task in enumerate(self.tasks):
            label = f"{index + 1}. {task.display_name()}"
            item = QListWidgetItem(label)
            status = (task.status or "pending").strip()
            if status == "done":
                item.setText(f"✅ {label}")
            elif status == "failed":
                item.setText(f"❌ {label}")
            elif status == "running":
                item.setText(f"⏳ {label}")
            elif status == "queued":
                item.setText(f"📌 {label}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if task.selected else Qt.CheckState.Unchecked)
            self.task_list_widget.addItem(item)
        if self.tasks:
            if current_row < 0 or current_row >= len(self.tasks):
                current_row = 0
            self.task_list_widget.setCurrentRow(current_row)
            self.current_task_index = current_row
        self.task_list_widget.blockSignals(False)
        self.task_count_label.setText(f"ทั้งหมด {len(self.tasks)} งาน")

    def on_task_item_changed(self, item):
        if self._initializing:
            return
        row = self.task_list_widget.row(item)
        if row < 0 or row >= len(self.tasks):
            return
        self.tasks[row].selected = item.checkState() == Qt.CheckState.Checked
        self.save_settings()

    def get_selected_task_indices(self):
        return [index for index, task in enumerate(self.tasks) if getattr(task, "selected", False)]

    def get_populated_task_indices(self, selected_only=False):
        indices = []
        for index, task in enumerate(self.tasks):
            if selected_only and not getattr(task, "selected", False):
                continue
            if task.chapter_files or task.title_keyword or task.working_url_override:
                indices.append(index)
        return indices

    def can_run_task_in_parallel(self, task: NovelTask):
        return bool(task.chapter_files) and bool((task.title_keyword or "").strip() or (task.working_url_override or "").strip())

    def mark_tasks_queued(self, task_indices):
        for index in task_indices:
            if index < 0 or index >= len(self.tasks):
                continue
            task = self.tasks[index]
            if task.status == "running":
                continue
            task.status = "queued"
            task.last_error = ""
        self.refresh_task_list_widget(selected_row=self.current_task_index)
        self.save_settings()

    def build_jobs_for_indices(self, task_indices, allow_empty_novel=False):
        jobs = []
        indices = []
        for index in task_indices:
            if index < 0 or index >= len(self.tasks):
                continue
            task = self.tasks[index]
            if not (task.chapter_files or task.title_keyword or task.working_url_override):
                continue
            jobs.append(self.build_job_from_task(task, allow_empty_novel=allow_empty_novel))
            indices.append(index)
        return jobs, indices

    def launch_parallel_job(self, job, task_index):
        thread = QThread(self)
        worker = UploadWorker(job, self.parallel_profile)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.status.connect(lambda message, idx=task_index: self.update_status(f"[งาน {idx + 1}] {message}"))
        worker.finished.connect(lambda success, message, idx=task_index: self.on_parallel_worker_finished(idx, success, message))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.parallel_active[task_index] = (thread, worker)
        self.parallel_threads.append(thread)
        self.parallel_workers.append(worker)
        if 0 <= task_index < len(self.tasks):
            task = self.tasks[task_index]
            task.status = "running"
            task.last_error = ""
        self.refresh_task_list_widget(selected_row=self.current_task_index)
        self.save_settings()
        thread.start()

    def process_parallel_queue(self):
        if not self.parallel_scheduler_running:
            return
        max_parallel = max(1, self.max_parallel_spin.value())
        while self.parallel_queue and len(self.parallel_active) < max_parallel:
            task_index, job = self.parallel_queue.pop(0)
            if task_index in self.parallel_active:
                continue
            if task_index in self.parallel_results:
                continue
            self.launch_parallel_job(job, task_index)
        pending_total = len(self.parallel_queue) + len(self.parallel_active)
        self.parallel_pending_count = pending_total
        if pending_total <= 0:
            self.finish_parallel_scheduler()

    def finish_parallel_scheduler(self):
        self.parallel_scheduler_running = False
        self.set_busy(False)
        self.running_task_indices = []
        failures = [(index, result[1]) for index, result in self.parallel_results.items() if not result[0]]
        self.parallel_threads = []
        self.parallel_workers = []
        self.parallel_active = {}
        self.parallel_queue = []
        self.parallel_pending_count = 0
        self.parallel_profile = None
        if failures:
            summary = "\n".join(f"งาน {index + 1}: {message}" for index, message in failures)
            self.update_status("มีบางงานอัปโหลดไม่สำเร็จ")
            QMessageBox.critical(self, "อัปโหลดพร้อมกันไม่สมบูรณ์", summary)
        else:
            self.update_status("อัปโหลดพร้อมกันเสร็จสิ้น")
            QMessageBox.information(self, "สำเร็จ", "อัปโหลดพร้อมกันเสร็จสิ้น")

    def enqueue_selected_tasks(self):
        self.save_current_task_from_form(refresh=False)
        selected_indices = self.get_populated_task_indices(selected_only=True)
        if not selected_indices:
            QMessageBox.information(self, "ยังไม่ได้เลือก", "กรุณาติ๊กเลือกงานนิยายที่ต้องการเข้าคิวก่อน")
            return
        if self.get_browser_mode() == "guest":
            QMessageBox.critical(self, "ข้อผิดพลาด", "Guest Mode ไม่เหมาะกับการรันพร้อมกัน เพราะไม่สามารถแชร์ session login ให้หลาย browser ได้")
            return
        allow_empty_novel = self.confirm_allow_empty_novel(selected_indices)
        if not allow_empty_novel:
            for index in selected_indices:
                task = self.tasks[index]
                if not ((task.title_keyword or "").strip() or (task.working_url_override or "").strip()):
                    return
        try:
            if not self.parallel_scheduler_running:
                self.parallel_profile = self.resolve_profile()
            jobs, indices = self.build_jobs_for_indices(selected_indices, allow_empty_novel=allow_empty_novel)
        except Exception as error:
            QMessageBox.critical(self, "ข้อผิดพลาด", str(error))
            return
        added_count = 0
        for task_index, job in zip(indices, jobs):
            if task_index in self.parallel_active:
                continue
            if any(existing_index == task_index for existing_index, _ in self.parallel_queue):
                continue
            self.parallel_queue.append((task_index, job))
            added_count += 1
        if added_count == 0:
            QMessageBox.information(self, "คิวงาน", "งานที่เลือกมีอยู่ในคิวหรือกำลังรันอยู่แล้ว")
            return
        self.parallel_scheduler_running = True
        self.running_task_indices = sorted(set(self.running_task_indices + indices))
        self.mark_tasks_queued(indices)
        self.set_busy(True)
        self.update_status(f"เพิ่มงานเข้าคิวแล้ว {added_count} เรื่อง | กำลังรัน {len(self.parallel_active)} | รอคิว {len(self.parallel_queue)}")
        self.process_parallel_queue()

    def load_task_into_form(self, task: NovelTask):
        self._initializing = True
        try:
            self.file_paths = list(task.chapter_files or [])
            self.novel_combo.setCurrentText(task.title_keyword or "")
            self.work_url_edit.setText(task.working_url_override or "")
            self.cloudflare_mode_radio.setChecked((task.run_mode or "fast") == "cloudflare")
            self.fast_mode_radio.setChecked((task.run_mode or "fast") != "cloudflare")
            self.headless_checkbox.setChecked(bool(task.headless))
            self.sequence_stop_radio.setChecked((task.sequence_policy or "auto_fix") == "stop")
            self.sequence_auto_fix_radio.setChecked((task.sequence_policy or "auto_fix") != "stop")
            price_mode = task.price_mode or "free"
            self.set_price_mode(price_mode)
            self.price_spin.setValue(int(task.price_value or DEFAULT_PRICE))
            publish_mode = task.publish_mode or "published"
            self.set_publish_mode_ui(publish_mode)
            schedule = task.schedule or {}
            start_date = schedule.get("start_date") or QDate.currentDate().toString("yyyy-MM-dd")
            self.start_date_edit.setDate(QDate.fromString(start_date, "yyyy-MM-dd"))
            self.start_time_edit.setTime(QTime.fromString(schedule.get("start_time", "17:00"), "HH:mm"))
            self.chapters_per_day_spin.setValue(int(schedule.get("chapters_per_day", 5)))
            self.check_duplicate_same_day.setChecked(bool(schedule.get("limit_per_day_enabled", True)))
            interval_minutes = int(schedule.get("interval_minutes", 10))
            interval_unit = (schedule.get("interval_unit") or "").strip().lower()
            if interval_unit == "hours":
                self.interval_unit_hours.setChecked(True)
                self.interval_value_spin.setValue(max(1, interval_minutes // 60))
            elif interval_unit == "minutes":
                self.interval_unit_minutes.setChecked(True)
                self.interval_value_spin.setValue(max(1, interval_minutes))
            elif interval_minutes % 60 == 0 and interval_minutes >= 60:
                self.interval_unit_hours.setChecked(True)
                self.interval_value_spin.setValue(max(1, interval_minutes // 60))
            else:
                self.interval_unit_minutes.setChecked(True)
                self.interval_value_spin.setValue(max(1, interval_minutes))
            self.time_filter_checkbox.setChecked(bool(schedule.get("skip_enabled", False)))
            self.skip_start_time_edit.setTime(QTime.fromString(schedule.get("skip_start", "00:00"), "HH:mm"))
            self.skip_end_time_edit.setTime(QTime.fromString(schedule.get("skip_end", "06:00"), "HH:mm"))
            auto_free = task.auto_free_details or {}
            if auto_free.get("rule"):
                self.free_rule_combo.setCurrentText(auto_free.get("rule"))
            self.custom_free_input.setText(auto_free.get("custom_numbers", ""))
            self.toggle_time_filter_options(self.time_filter_checkbox.checkState().value)
            self.update_file_label()
        finally:
            self._initializing = False

    def save_current_task_from_form(self, refresh=True):
        if self._initializing or self.current_task_index < 0 or self.current_task_index >= len(self.tasks):
            return
        task = self.tasks[self.current_task_index]
        task.title_keyword = self.novel_combo.currentText().strip()
        if task.title_keyword == "-- เลือกหรือพิมพ์ชื่อเรื่อง --":
            task.title_keyword = ""
        task.working_url_override = self.work_url_edit.text().strip()
        task.chapter_files = sort_file_paths(list(self.file_paths))
        task.run_mode = "cloudflare" if self.cloudflare_mode_radio.isChecked() else "fast"
        task.headless = self.headless_checkbox.isChecked()
        task.sequence_policy = "stop" if self.sequence_stop_radio.isChecked() else "auto_fix"
        task.price_mode = self.get_price_mode()
        task.price_value = self.price_spin.value()
        task.publish_mode = self.get_publish_mode()
        task.schedule = self.collect_schedule()
        task.auto_free_details = {
            "rule": self.free_rule_combo.currentText(),
            "custom_numbers": self.custom_free_input.text().strip(),
            "free_chapters_list": self.get_free_chapters_list(len(task.chapter_files)),
        }
        if refresh:
            self.refresh_task_list_widget(selected_row=self.current_task_index)

    def on_task_selected(self, index):
        if self._initializing:
            return
        previous_index = self.current_task_index
        if previous_index >= 0 and previous_index < len(self.tasks):
            self.save_current_task_from_form(refresh=False)
        if index < 0 or index >= len(self.tasks):
            return
        self.current_task_index = index
        self.load_task_into_form(self.tasks[index])
        self.refresh_task_list_widget(selected_row=index)

    def add_task(self):
        if self.current_task_index >= 0:
            self.save_current_task_from_form(refresh=False)
        self.tasks.append(self.create_empty_task())
        self.current_task_index = len(self.tasks) - 1
        self.refresh_task_list_widget(selected_row=self.current_task_index)
        self.load_task_into_form(self.tasks[self.current_task_index])

    def remove_selected_task(self):
        if not self.tasks:
            return
        row = self.task_list_widget.currentRow()
        if row < 0:
            row = self.current_task_index if self.current_task_index >= 0 else 0
        if len(self.tasks) == 1:
            self.tasks = [self.create_empty_task()]
            self.current_task_index = 0
        else:
            self.tasks.pop(row)
            self.current_task_index = max(0, min(row, len(self.tasks) - 1))
        self.refresh_task_list_widget(selected_row=self.current_task_index)
        self.load_task_into_form(self.tasks[self.current_task_index])

    def load_novel_list_from_file(self):
        default_dir = self.last_novel_list_path if self.last_novel_list_path else ""
        file_path, _ = QFileDialog.getOpenFileName(self, "เลือกไฟล์รายชื่อเรื่อง", default_dir, "Text files (*.txt);;All files (*)")
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as file:
                    novels = [line.strip() for line in file if line.strip() and not line.strip().startswith("#")]
                if novels:
                    self.novel_list = novels
                    save_novel_list(novels)
                    self.update_novel_combo()
                    self.last_novel_list_path = os.path.dirname(file_path)
                    self.save_settings()
                    QMessageBox.information(self, "สำเร็จ", f"โหลดรายชื่อเรื่อง {len(novels)} รายการ")
                else:
                    QMessageBox.warning(self, "คำเตือน", "ไฟล์ไม่มีรายชื่อเรื่อง")
            except Exception as error:
                QMessageBox.critical(self, "ข้อผิดพลาด", f"ไม่สามารถโหลดไฟล์ได้: {error}")

    def update_novel_combo(self):
        current_text = self.novel_combo.currentText()
        self.novel_combo.clear()
        self.novel_combo.addItem("-- เลือกหรือพิมพ์ชื่อเรื่อง --")
        for novel in self.novel_list:
            self.novel_combo.addItem(novel)
        index = self.novel_combo.findText(current_text)
        if index >= 0:
            self.novel_combo.setCurrentIndex(index)
        else:
            self.novel_combo.setCurrentText(current_text)
        self.novel_count_label.setText(f"รายการทั้งหมด: {len(self.novel_list)} รายการ")

    def add_current_novel(self):
        current_text = self.novel_combo.currentText().strip()
        if not current_text or current_text == "-- เลือกหรือพิมพ์ชื่อเรื่อง --":
            QMessageBox.warning(self, "คำเตือน", "กรุณาพิมพ์ชื่อเรื่องก่อน")
            return
        if current_text in self.novel_list:
            QMessageBox.information(self, "แจ้งเตือน", "ชื่อนี้มีในรายการอยู่แล้ว")
            return
        self.novel_list.append(current_text)
        save_novel_list(self.novel_list)
        self.update_novel_combo()
        QMessageBox.information(self, "สำเร็จ", f"เพิ่ม '{current_text}' เข้ารายการแล้ว")

    def remove_current_novel(self):
        current_text = self.novel_combo.currentText().strip()
        if not current_text or current_text == "-- เลือกหรือพิมพ์ชื่อเรื่อง --":
            QMessageBox.warning(self, "คำเตือน", "กรุณาเลือกชื่อเรื่องที่ต้องการลบ")
            return
        if current_text not in self.novel_list:
            QMessageBox.warning(self, "คำเตือน", "ชื่อนี้ไม่มีในรายการ")
            return
        reply = QMessageBox.question(self, "ยืนยันการลบ", f"ต้องการลบ '{current_text}' ออกจากรายการหรือไม่?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.novel_list = [novel for novel in self.novel_list if novel != current_text]
            save_novel_list(self.novel_list)
            self.update_novel_combo()
            QMessageBox.information(self, "สำเร็จ", f"ลบ '{current_text}' ออกจากรายการแล้ว")

    def select_files(self):
        default_dir = self.last_novel_folder_path if self.last_novel_folder_path else ""
        paths, _ = QFileDialog.getOpenFileNames(self, "เลือกไฟล์นิยาย", default_dir, "Text files (*.txt)")
        if paths:
            self.file_paths = sort_file_paths(paths)
            self.last_novel_folder_path = os.path.dirname(paths[0])
            self.update_file_label()
            self.save_current_task_from_form()
            self.save_settings()

    def update_file_label(self):
        if not self.file_paths:
            self.file_label.setText("ยังไม่ได้เลือกไฟล์...")
        else:
            self.file_label.setText(f"เลือกแล้ว {len(self.file_paths)} ไฟล์")

    def refresh_profile_combo(self):
        self.profile_items = load_chrome_profiles()
        self.profile_map = {profile.label: profile for profile in self.profile_items}
        current = self.settings.get("selected_profile_label", self.profile_combo.currentText())
        self.profile_combo.clear()
        for profile in self.profile_items:
            self.profile_combo.addItem(profile.label)
        if current:
            index = self.profile_combo.findText(current)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)

    def toggle_profile_mode(self):
        installed = self.installed_profile_radio.isChecked()
        custom = self.custom_profile_radio.isChecked()
        self.profile_combo.setEnabled(installed)
        self.custom_profile_edit.setEnabled(custom)

    def get_price_mode(self):
        if self.auto_free_radio.isChecked():
            return "auto_free"
        if self.sell_radio.isChecked():
            return "paid"
        return "free"

    def set_price_mode(self, price_mode: str):
        mode = price_mode if price_mode in {"free", "paid", "auto_free"} else "free"
        self.free_radio.setChecked(mode == "free")
        self.sell_radio.setChecked(mode == "paid")
        self.auto_free_radio.setChecked(mode == "auto_free")
        self.toggle_auto_free_options(mode == "auto_free")

    def set_publish_mode_ui(self, publish_mode: str):
        mode = publish_mode if publish_mode in {"draft", "published", "scheduled"} else "published"
        self.draft_radio.setChecked(mode == "draft")
        self.publish_radio.setChecked(mode == "published")
        self.schedule_radio.setChecked(mode == "scheduled")
        self.toggle_schedule_options(mode == "scheduled")
        self.toggle_time_filter_group(mode == "scheduled")

    def get_browser_mode(self):
        if self.automation_profile_radio.isChecked():
            return "automation"
        if self.guest_profile_radio.isChecked():
            return "guest"
        if self.custom_profile_radio.isChecked():
            return "custom"
        return "installed"

    def auto_detect_chrome(self):
        detected = detect_chrome_path()
        if detected:
            self.chrome_path_edit.setText(detected)
            self.update_status(f"พบ Chrome: {detected}")
        else:
            QMessageBox.warning(self, "ไม่พบ Chrome", "ไม่พบ Chrome ในตำแหน่งมาตรฐาน กรุณาเลือกไฟล์เอง")

    def browse_custom_profile(self):
        folder_path = QFileDialog.getExistingDirectory(self, "เลือกโฟลเดอร์โปรไฟล์ Chrome")
        if folder_path:
            self.custom_profile_edit.setText(folder_path)

    def toggle_schedule_options(self, checked):
        self.schedule_group.setEnabled(checked)
        if hasattr(self, "calc_button"):
            self.calc_button.setEnabled(checked)

    def toggle_auto_free_options(self, checked):
        self.free_rule_label.setEnabled(checked)
        self.free_rule_combo.setEnabled(checked)
        self.custom_free_label.setEnabled(checked)
        self.custom_free_input.setEnabled(checked)
        paid_enabled = checked or self.sell_radio.isChecked()
        self.price_spin_label.setEnabled(paid_enabled)
        self.price_spin.setEnabled(paid_enabled)

    def toggle_time_filter_group(self, checked):
        self.time_filter_group.setEnabled(checked)
        if not checked:
            self.time_filter_checkbox.setChecked(False)

    def toggle_time_filter_options(self, state):
        enabled = state == Qt.CheckState.Checked.value
        self.skip_start_time_edit.setEnabled(enabled)
        self.skip_end_time_edit.setEnabled(enabled)

    def check_year_warning(self, date):
        if self._initializing:
            return
        selected_year = date.year()
        current_year = QDate.currentDate().year()
        if selected_year > current_year:
            QMessageBox.information(
                self,
                "คำเตือน: ปีเกินปีปัจจุบัน",
                f"คุณตั้งปี {selected_year} ซึ่งเกินปีปัจจุบัน ({current_year})\n\nกรุณาตรวจสอบให้แน่ใจว่าตั้งค่าถูกต้อง",
                QMessageBox.StandardButton.Ok,
            )

    def get_publish_mode(self):
        if self.schedule_radio.isChecked():
            return "scheduled"
        if self.draft_radio.isChecked():
            return "draft"
        return "published"

    def get_free_chapters_list(self, total_chapters):
        return self.compute_free_chapters_list(
            total_chapters,
            self.free_rule_combo.currentText(),
            self.custom_free_input.text().strip(),
            extract_episode_numbers_from_paths(self.file_paths),
        )

    def compute_free_chapters_list(self, total_chapters, rule_text, custom_text="", chapter_numbers=None):
        free_chapters = set()
        rule_text = (rule_text or "").strip()
        custom_text = (custom_text or "").strip()
        candidate_numbers = [num for num in (chapter_numbers or []) if isinstance(num, int)]
        if not candidate_numbers:
            candidate_numbers = list(range(1, total_chapters + 1))
        if "กำหนดเอง" in rule_text:
            if custom_text:
                try:
                    for num in [int(x.strip()) for x in custom_text.split(",") if x.strip().isdigit()]:
                        if num in candidate_numbers or (1 <= num <= total_chapters and not chapter_numbers):
                            free_chapters.add(num)
                except ValueError:
                    pass
        elif "ลงท้ายด้วย 5, 0" in rule_text:
            for i in candidate_numbers:
                if i % 5 == 0:
                    free_chapters.add(i)
        elif "ลงท้ายด้วย 0" in rule_text:
            for i in candidate_numbers:
                if i % 10 == 0:
                    free_chapters.add(i)
        elif "ลงท้ายด้วย 5" in rule_text:
            for i in candidate_numbers:
                if i % 10 == 5:
                    free_chapters.add(i)
        elif "ทุก 3 ตอน" in rule_text:
            for i in candidate_numbers:
                if i % 3 == 0:
                    free_chapters.add(i)
        elif "ทุก 5 ตอน" in rule_text:
            for i in candidate_numbers:
                if i % 5 == 0:
                    free_chapters.add(i)
        elif "ทุก 10 ตอน" in rule_text:
            for i in candidate_numbers:
                if i % 10 == 0:
                    free_chapters.add(i)
        return sorted(free_chapters)

    def collect_schedule(self):
        interval_value = self.interval_value_spin.value()
        if self.interval_unit_hours.isChecked():
            interval_minutes = interval_value * 60
        else:
            interval_minutes = interval_value
        return {
            "start_date": self.start_date_edit.date().toString("yyyy-MM-dd"),
            "start_time": self.start_time_edit.time().toString("HH:mm"),
            "chapters_per_day": self.chapters_per_day_spin.value(),
            "limit_per_day_enabled": self.check_duplicate_same_day.isChecked(),
            "interval_minutes": interval_minutes,
            "interval_unit": "hours" if self.interval_unit_hours.isChecked() else "minutes",
            "skip_enabled": self.time_filter_checkbox.isChecked(),
            "skip_start": self.skip_start_time_edit.time().toString("HH:mm"),
            "skip_end": self.skip_end_time_edit.time().toString("HH:mm"),
        }

    def resolve_profile(self):
        mode = self.get_browser_mode()
        if mode == "automation":
            return build_automation_profile()
        if mode == "guest":
            return build_guest_profile()
        if mode == "custom":
            custom_path = self.custom_profile_edit.text().strip()
            if not custom_path:
                raise RuntimeError("กรุณาเลือก Custom profile folder")
            return build_profile_from_custom_path(custom_path)
        label = self.profile_combo.currentText().strip()
        if not label or label not in self.profile_map:
            raise RuntimeError("กรุณาเลือก Chrome profile")
        return self.profile_map[label]

    def build_job_from_task(self, task: NovelTask, allow_empty_novel=False):
        chapter_files = sort_file_paths(list(task.chapter_files or []))
        if not chapter_files:
            raise RuntimeError(f"งาน '{task.display_name()}' ยังไม่ได้เลือกไฟล์นิยาย")
        novel_keyword = (task.title_keyword or "").strip()
        work_url = (task.working_url_override or "").strip()
        if (not novel_keyword or novel_keyword == "-- เลือกหรือพิมพ์ชื่อเรื่อง --") and not allow_empty_novel and not work_url:
            raise RuntimeError("กรุณาระบุชื่อนิยายสำหรับค้นหา")
        if novel_keyword == "-- เลือกหรือพิมพ์ชื่อเรื่อง --":
            novel_keyword = ""
        profile = self.resolve_profile()
        publish_mode = task.publish_mode or "published"
        schedule = dict(task.schedule or {})
        if publish_mode == "scheduled":
            compute_schedule_datetimes(len(chapter_files), ScheduleConfig(**schedule))
        price_mode = task.price_mode or "free"
        auto_free_details = None
        if price_mode == "auto_free":
            auto_free_details = dict(task.auto_free_details or {})
            auto_free_details.setdefault("rule", "ตอนที่ลงท้ายด้วย 5, 0 (เช่น 5, 10, 15, 20...)")
            auto_free_details.setdefault("custom_numbers", "")
            auto_free_details["free_chapters_list"] = self.compute_free_chapters_list(
                len(chapter_files),
                auto_free_details.get("rule", ""),
                auto_free_details.get("custom_numbers", ""),
                extract_episode_numbers_from_paths(chapter_files),
            )
        job = UploadJob(
            preset_name="",
            novel_keyword=novel_keyword,
            chapter_files=chapter_files,
            browser_mode=self.get_browser_mode(),
            selected_profile_label=profile.label if self.get_browser_mode() in {"installed", "automation"} else self.profile_combo.currentText().strip(),
            custom_profile_path=self.custom_profile_edit.text().strip(),
            chrome_path=self.chrome_path_edit.text().strip(),
            headless=bool(task.headless),
            price_mode=price_mode,
            price_value=int(task.price_value or DEFAULT_PRICE),
            publish_mode=publish_mode,
            schedule=schedule,
            reset_progress=False,
            run_mode=task.run_mode or "fast",
            sequence_policy=task.sequence_policy or "auto_fix",
            auto_free_details=auto_free_details,
        )
        setattr(job, "working_url_override", work_url)
        return job

    def build_jobs(self, allow_empty_novel=False):
        self.save_current_task_from_form()
        jobs = []
        valid_tasks = []
        for task in self.tasks:
            if not (task.title_keyword or task.working_url_override or task.chapter_files):
                continue
            valid_tasks.append(task)
        if not valid_tasks:
            raise RuntimeError("กรุณาเพิ่มนิยายอย่างน้อย 1 งาน")
        for task in valid_tasks:
            jobs.append(self.build_job_from_task(task, allow_empty_novel=allow_empty_novel))
        return jobs

    def build_current_task_job(self, allow_empty_novel=False):
        self.save_current_task_from_form()
        if self.current_task_index < 0 or self.current_task_index >= len(self.tasks):
            raise RuntimeError("กรุณาเลือกงานนิยายก่อน")
        task = self.tasks[self.current_task_index]
        return self.build_job_from_task(task, allow_empty_novel=allow_empty_novel)

    def show_estimate_dialog(self):
        if not self.schedule_radio.isChecked():
            QMessageBox.information(self, "ข้อมูลไม่พอ", "โหมดเผยแพร่ทันที: ไม่ต้องคำนวณเวลา")
            return
        if not self.file_paths:
            QMessageBox.information(self, "ข้อมูลไม่พอ", "กรุณาเลือกไฟล์นิยายก่อน")
            return
        try:
            schedule_cfg = ScheduleConfig(**self.collect_schedule())
            schedule_plan = compute_schedule_datetimes(len(self.file_paths), schedule_cfg)
            base_dt = schedule_plan[0]
            end_dt = schedule_plan[-1]
            msg = (
                f"จำนวนไฟล์ทั้งหมด: {len(self.file_paths)} ตอน\n"
                f"เริ่ม: {base_dt.strftime('%Y-%m-%d %H:%M')}\n"
                f"สิ้นสุดโดยประมาณ: {end_dt.strftime('%Y-%m-%d %H:%M')}\n"
                f"ใช้ประมาณ {len({dt.date() for dt in schedule_plan})} วัน"
            )
            if schedule_cfg.skip_enabled:
                msg += f"\n🚫 ข้ามเวลาลง: {schedule_cfg.skip_start} ถึง {schedule_cfg.skip_end}"
            QMessageBox.information(self, "ผลคำนวณ", msg)
        except Exception as error:
            QMessageBox.warning(self, "คำนวณไม่ได้", str(error))

    def open_login_page(self):
        try:
            profile = self.resolve_profile()
            chrome_path = self.chrome_path_edit.text().strip()
            self.close_login_browser()
            self.login_bot = MyNovelBot(self.update_status)
            self.login_driver = self.login_bot.open_login_browser(chrome_path, profile, persistent_profile=self.get_browser_mode() == "automation")
            QMessageBox.information(
                self,
                "Login",
                "เปิดหน้า Login แล้ว\n\n"
                "ล็อกอินให้เสร็จเพียงครั้งเดียว แล้วค่อยกลับมากดเริ่มอัปโหลด\n"
                "ถ้ายังไม่ปิดหน้าต่างนี้ โปรแกรมจะ reuse session นี้ต่อทันที"
            )
        except Exception as error:
            QMessageBox.critical(self, "เปิดหน้า Login ไม่สำเร็จ", str(error))

    def ensure_login_browser(self, profile):
        if self.login_driver is not None:
            try:
                _ = self.login_driver.current_url
                return self.login_driver
            except Exception:
                self.close_login_browser()

        chrome_path = self.chrome_path_edit.text().strip()
        self.login_bot = MyNovelBot(self.update_status)
        self.login_driver = self.login_bot.open_login_browser(
            chrome_path,
            profile,
            persistent_profile=self.get_browser_mode() == "automation",
        )
        return self.login_driver

    def close_login_browser(self):
        if self.login_driver:
            if self.login_bot is not None:
                self.login_bot.cleanup_driver(self.login_driver)
            else:
                try:
                    self.login_driver.quit()
                except Exception:
                    pass
            self.login_driver = None
            self.login_bot = None
            self.update_status("ปิดหน้า Login แล้ว")

    def set_busy(self, busy):
        self.start_button.setEnabled(not busy)
        self.run_current_button.setEnabled(not busy)
        self.parallel_button.setEnabled(not busy)
        self.enqueue_selected_button.setEnabled(True)
        self.max_parallel_spin.setEnabled(True)
        self.calc_button.setEnabled((not busy) and self.schedule_radio.isChecked())
        self.login_button.setEnabled(not busy)
        self.close_login_button.setEnabled(not busy)
        self.add_task_button.setEnabled(True)
        self.remove_task_button.setEnabled(not busy)
        self.task_list_widget.setEnabled(True)

    def confirm_allow_empty_novel(self, task_indices):
        for index in task_indices:
            if index < 0 or index >= len(self.tasks):
                continue
            task = self.tasks[index]
            novel_name = (task.title_keyword or "").strip()
            work_url = (task.working_url_override or "").strip()
            if novel_name and novel_name != "-- เลือกหรือพิมพ์ชื่อเรื่อง --":
                continue
            if work_url:
                continue
            reply = QMessageBox.question(
                self,
                "ยืนยัน",
                f"งาน '{task.display_name()}' ไม่ได้เลือกชื่อเรื่อง ต้องการดำเนินการต่อหรือไม่?\n(โปรแกรมอาจหานิยายไม่เจอ)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            return reply == QMessageBox.StandardButton.Yes
        return False

    def reset_task_statuses(self, task_indices, status="pending", clear_error=True):
        for index in task_indices or []:
            if index < 0 or index >= len(self.tasks):
                continue
            task = self.tasks[index]
            if not (task.chapter_files or task.title_keyword or task.working_url_override):
                continue
            task.status = status
            if clear_error:
                task.last_error = ""
        self.refresh_task_list_widget(selected_row=self.current_task_index)
        self.save_settings()

    def start_jobs(self, jobs, task_indices):
        try:
            profile = self.resolve_profile()
            self.running_task_indices = list(task_indices)
            for index, task in enumerate(self.tasks):
                if not (task.chapter_files or task.title_keyword or task.working_url_override):
                    continue
                if index in self.running_task_indices:
                    task.status = "running"
                    task.last_error = ""
            self.save_settings()
            self.refresh_task_list_widget(selected_row=self.current_task_index)
        except Exception as error:
            QMessageBox.critical(self, "ข้อผิดพลาด", str(error))
            return

        run_headless_only = all(bool(job.headless) for job in jobs)
        driver = None
        if not run_headless_only:
            try:
                reused_existing = self.login_driver is not None
                driver = self.ensure_login_browser(profile)
                if not reused_existing:
                    QMessageBox.information(
                        self,
                        "Login",
                        "ยังไม่มีหน้าต่าง Login ที่เปิดอยู่\n\n"
                        "กรุณาล็อกอิน MyNovel ในหน้าต่าง Chrome ที่เปิดขึ้นมา\n"
                        "หลังจากล็อกอินเสร็จแล้ว กด OK เพื่อเริ่มอัปโหลด"
                    )
                else:
                    self.update_status("ใช้ session Login ที่เปิดค้างไว้อยู่แล้ว")
            except Exception as error:
                self.reset_task_statuses(self.running_task_indices)
                self.running_task_indices = []
                QMessageBox.critical(self, "เปิดหน้า Login ไม่สำเร็จ", str(error))
                return
            try:
                driver.get("https://mynovel.co/dashboard/workings")
            except Exception as error:
                self.reset_task_statuses(self.running_task_indices)
                self.running_task_indices = []
                QMessageBox.critical(self, "เปลี่ยนหน้าไม่สำเร็จ", str(error))
                return
        else:
            self.update_status("เริ่มโหมดซ่อนจอ: สร้าง browser ใหม่ตามงาน (ไม่เปิดหน้า Login)")

        self.thread = QThread(self)
        self.worker = UploadWorker(jobs, profile, existing_driver=driver)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.status.connect(self.update_status)
        self.worker.job_finished.connect(lambda order, success, message, indices=list(task_indices): self.on_sequential_job_finished(indices[order] if 0 <= order < len(indices) else -1, success, message))
        self.worker.finished.connect(self.on_upload_finished)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.set_busy(True)
        if len(jobs) == 1:
            self.update_status("เริ่มอัปโหลดเรื่องที่เลือก...")
        else:
            self.update_status("เริ่มอัปโหลดหลายเรื่อง...")
        self.thread.start()

    def start_parallel_jobs(self, jobs, task_indices):
        try:
            profile = self.resolve_profile()
            self.running_task_indices = list(task_indices)
            self.parallel_threads = []
            self.parallel_workers = []
            self.parallel_results = {}
            self.parallel_pending_count = len(jobs)
            self.parallel_queue = list(zip(task_indices, jobs))
            self.parallel_active = {}
            self.parallel_profile = profile
            self.parallel_scheduler_running = True
            for index, task in enumerate(self.tasks):
                if not (task.chapter_files or task.title_keyword or task.working_url_override):
                    continue
                if index in self.running_task_indices:
                    task.status = "queued"
                    task.last_error = ""
            self.save_settings()
            self.refresh_task_list_widget(selected_row=self.current_task_index)
        except Exception as error:
            QMessageBox.critical(self, "ข้อผิดพลาด", str(error))
            return

        self.set_busy(True)
        self.update_status(f"เริ่มอัปโหลดพร้อมกัน {len(jobs)} เรื่อง... (สูงสุด {self.max_parallel_spin.value()} เรื่อง)")
        self.process_parallel_queue()

    def on_parallel_worker_finished(self, task_index, success, message):
        self.parallel_results[task_index] = (success, message)
        if 0 <= task_index < len(self.tasks):
            task = self.tasks[task_index]
            task.status = "done" if success else "failed"
            task.last_error = "" if success else message
        self.parallel_active.pop(task_index, None)
        self.parallel_pending_count = max(0, len(self.parallel_queue) + len(self.parallel_active))
        self.refresh_task_list_widget(selected_row=self.current_task_index)
        self.save_settings()
        self.process_parallel_queue()

    def on_sequential_job_finished(self, task_index, success, message):
        if 0 <= task_index < len(self.tasks):
            task = self.tasks[task_index]
            task.status = "done" if success else "failed"
            task.last_error = "" if success else message
        self.refresh_task_list_widget(selected_row=self.current_task_index)
        self.save_settings()

    def apply_current_task(self):
        self.save_current_task_from_form(refresh=False)
        if self.current_task_index < 0 or self.current_task_index >= len(self.tasks):
            QMessageBox.critical(self, "ข้อผิดพลาด", "กรุณาเลือกงานนิยายก่อน")
            return
        task = self.tasks[self.current_task_index]
        if not task.chapter_files:
            QMessageBox.critical(self, "ข้อผิดพลาด", "กรุณาเลือกไฟล์นิยายของเรื่องนี้ก่อน")
            return
        allow_empty_novel = self.confirm_allow_empty_novel([self.current_task_index])
        if not allow_empty_novel and not ((task.title_keyword or "").strip() or (task.working_url_override or "").strip()):
            return
        reply = QMessageBox.question(
            self,
            "ยืนยันการอัปโหลด",
            f"ต้องการรันเฉพาะเรื่อง '{task.display_name()}' ใช่หรือไม่?\n\n"
            "หากมีหน้าต่าง Login ค้างอยู่ โปรแกรมจะใช้ session นั้นต่อทันที",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return
        try:
            job = self.build_current_task_job(allow_empty_novel=allow_empty_novel)
        except Exception as error:
            QMessageBox.critical(self, "ข้อผิดพลาด", str(error))
            return
        self.start_jobs([job], [self.current_task_index])

    def apply_settings(self):
        self.save_current_task_from_form()
        task_indices = self.get_populated_task_indices(selected_only=True)
        if not task_indices:
            task_indices = self.get_populated_task_indices(selected_only=False)
        if not task_indices:
            QMessageBox.critical(self, "ข้อผิดพลาด", "กรุณาเพิ่มนิยายและเลือกไฟล์อย่างน้อย 1 งาน")
            return
        allow_empty_novel = self.confirm_allow_empty_novel(task_indices)
        if not allow_empty_novel:
            for index in task_indices:
                task = self.tasks[index]
                if not ((task.title_keyword or "").strip() or (task.working_url_override or "").strip()):
                    return
        
        reply = QMessageBox.question(
            self,
            "ยืนยันการอัปโหลด",
            "ต้องการรันทุกเรื่องในรายการใช่หรือไม่?\n\n"
            "หากมีหน้าต่าง Login ค้างอยู่ โปรแกรมจะใช้ session นั้นต่อทันที\n"
            "⚠️ อย่าปิดหน้าต่าง Chrome จนกว่าจะอัปโหลดเสร็จ",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return
        
        try:
            jobs, task_indices = self.build_jobs_for_indices(task_indices, allow_empty_novel=allow_empty_novel)
        except Exception as error:
            QMessageBox.critical(self, "ข้อผิดพลาด", str(error))
            return
        self.start_jobs(jobs, task_indices)

    def apply_parallel_settings(self):
        self.save_current_task_from_form()
        task_indices = self.get_populated_task_indices(selected_only=True)
        if not task_indices:
            task_indices = self.get_populated_task_indices(selected_only=False)
        if len(task_indices) < 2:
            QMessageBox.critical(self, "ข้อผิดพลาด", "โหมดรันพร้อมกันต้องมีอย่างน้อย 2 งาน")
            return
        if self.get_browser_mode() == "guest":
            QMessageBox.critical(self, "ข้อผิดพลาด", "Guest Mode ไม่เหมาะกับการรันพร้อมกัน เพราะไม่สามารถแชร์ session login ให้หลาย browser ได้")
            return
        allow_empty_novel = self.confirm_allow_empty_novel(task_indices)
        if not allow_empty_novel:
            for index in task_indices:
                task = self.tasks[index]
                if not ((task.title_keyword or "").strip() or (task.working_url_override or "").strip()):
                    return
        reply = QMessageBox.question(
            self,
            "ยืนยันการรันพร้อมกัน",
            "ต้องการเปิด browser แยกและรันทุกเรื่องพร้อมกันใช่หรือไม่?\n\n"
            "แนะนำให้ใช้ Automation Profile หรือ profile ที่ล็อกอินไว้แล้ว\n"
            "แต่ละงานจะเปิด browser ของตัวเอง",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return
        try:
            jobs, task_indices = self.build_jobs_for_indices(task_indices, allow_empty_novel=allow_empty_novel)
        except Exception as error:
            QMessageBox.critical(self, "ข้อผิดพลาด", str(error))
            return
        self.start_parallel_jobs(jobs, task_indices)

    def on_upload_finished(self, success, message):
        self.set_busy(False)
        target_indices = self.running_task_indices or [index for index, task in enumerate(self.tasks) if task.chapter_files or task.title_keyword or task.working_url_override]
        for index in target_indices:
            if index < 0 or index >= len(self.tasks):
                continue
            task = self.tasks[index]
            if not (task.chapter_files or task.title_keyword or task.working_url_override):
                continue
            if success:
                if task.status in {"running", "queued"}:
                    task.status = "done"
                    task.last_error = ""
            else:
                if task.status in {"running", "queued"}:
                    task.status = "pending"
                    task.last_error = ""
        self.running_task_indices = []
        self.refresh_task_list_widget()
        self.save_settings()
        if success:
            self.update_status(message)
            QMessageBox.information(self, "สำเร็จ", message)
        else:
            self.update_status(f"เกิดข้อผิดพลาด: {message}")
            QMessageBox.critical(self, "เกิดข้อผิดพลาด", message)


STYLESHEET = """
QWidget{background-color:#2E3440;color:#ECEFF4;font-family:'Tahoma';font-size:13pt}
QGroupBox{border:2px solid #4C566A;border-radius:10px;margin-top:15px;padding:20px;background-color:#3B4252}
QGroupBox::title{subcontrol-origin:margin;subcontrol-position:top left;padding:0 15px;left:15px;color:#88C0D0;font-size:15pt;font-weight:bold}
#InstructionLabel{color:#EBCB8B;font-size:12pt;padding:15px;background-color:#434C5E;border-radius:8px;border:1px solid #5E81AC}
#StatusLabel{color:#A3BE8C;font-size:11pt;padding:8px 12px;background-color:#3B4252;border-radius:8px;border:1px solid #4C566A}
QPushButton{background-color:#434C5E;border:2px solid #4C566A;padding:10px 20px;border-radius:8px;font-weight:bold;font-size:12pt}
QPushButton:hover{background-color:#4C566A;border:2px solid #5E81AC}
QPushButton:pressed{background-color:#3B4252}
#StartButton{background-color:#A3BE8C;color:#2E3440;border:2px solid #8FBCBB;font-weight:bold}
#StartButton:hover{background-color:#8FBCBB;border:2px solid #A3BE8C}
#FileLabel{border:2px dashed #4C566A;padding:15px;border-radius:8px;color:#D8DEE9;background-color:#434C5E;font-weight:bold}
QRadioButton{spacing:15px;font-size:12pt;padding:5px}
QRadioButton::indicator{width:20px;height:20px}
QRadioButton::indicator:unchecked{border:2px solid #4C566A;background-color:#3B4252;border-radius:10px}
QRadioButton::indicator:checked{border:2px solid #81A1C1;background-color:#5E81AC;border-radius:10px}
QDateEdit,QTimeEdit,QSpinBox,QComboBox,QLineEdit{background-color:#3B4252;border:2px solid #4C566A;padding:8px 12px;border-radius:6px;min-width:120px;font-size:11pt}
QDateEdit:focus,QTimeEdit:focus,QSpinBox:focus,QComboBox:focus,QLineEdit:focus{border:2px solid #5E81AC;background-color:#434C5E}
QDateEdit::drop-down{subcontrol-origin:padding;subcontrol-position:top right;width:30px;border-left:2px solid #4C566A;background-color:#434C5E;border-radius:0 6px 6px 0}
QDateEdit::drop-down:hover{background-color:#4C566A}
QComboBox::drop-down{subcontrol-origin:padding;subcontrol-position:top right;width:30px;border-left:2px solid #4C566A;background-color:#434C5E;border-radius:0 6px 6px 0}
QComboBox::drop-down:hover{background-color:#4C566A}
QCalendarWidget{background-color:#3B4252;border:2px solid #4C566A;border-radius:8px}
QMessageBox{background-color:#3B4252;border:2px solid #4C566A;border-radius:8px}
QCheckBox{spacing:15px;font-size:12pt;padding:5px}
QCheckBox::indicator{width:20px;height:20px}
QCheckBox::indicator:unchecked{border:2px solid #4C566A;background-color:#3B4252;border-radius:5px}
QCheckBox::indicator:checked{border:2px solid #81A1C1;background-color:#5E81AC;border-radius:5px}
QLabel{font-size:12pt;padding:2px}
"""


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    window = UploaderGUI()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
