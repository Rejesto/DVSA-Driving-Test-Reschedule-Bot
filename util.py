import os
import ast
import json
import time
import random
import logging
import requests

from configparser import ConfigParser
from datetime import datetime, timedelta
from datetime import time as dtime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException, WebDriverException

import text  # Custom 'text' module that handles Twilio SMS logic.
from clicker import hardware_click_range  # Your custom hardware click function.

logger = logging.getLogger(__name__)


def parse_config(config: ConfigParser) -> dict:
    """
    Parses the [preferences] section of config.ini into a dictionary,
    consistently using DD/MM/YYYY strings (or possibly with time),
    and returning only JSON-serializable data.
    """
    def build_dict(**kwargs) -> dict:
        auto_book_str = kwargs.get('auto_book_test', 'False')
        auto_book_bool = (auto_book_str.lower() == 'true')

        # read disabled_dates as a Python list
        raw_disabled = kwargs.get('disabled_dates', '[]')
        try:
            disabled_list = ast.literal_eval(raw_disabled)
        except:
            disabled_list = []

        current_test_date_str = kwargs.get('current_test_date', '').strip()
        formatted_current_test_date_str = kwargs.get('formatted_current_test_date', '').strip()
        before_date_str = kwargs.get('before_date', '').strip()
        after_date_str = kwargs.get('after_date', '').strip()

        return {
            "licence-id": 0,
            "user-id": 0,
            "licence": kwargs.get('licence', ''),
            "booking": kwargs.get('booking', ''),
            "current-test": {
                "date": current_test_date_str,
                "center": kwargs.get('current_test_centre', ''),
                "error": kwargs.get('current_test_error', '')
            },
            "disabled-dates": disabled_list,
            "center": ast.literal_eval(kwargs.get('centre', '[]')),
            "before-date": before_date_str,
            "after-date": after_date_str,
            "auto_book_test": auto_book_bool,
            "formatted_current_test_date": formatted_current_test_date_str,
        }

    key_dict = {}
    for section in config.sections():
        for k, v in config.items(section):
            key_dict[k] = v

    return build_dict(**key_dict)


def random_sleep(base: float, max_extra: float):
    """
    Sleep for 'base' seconds + an extra random fraction of up to 'max_extra' seconds.
    """
    logger.info("Sleeping for %.2f seconds...", base)
    time.sleep(base)
    extra = random.uniform(0, max_extra)
    logger.info("Sleeping an additional %.2f seconds for randomness.", extra)
    time.sleep(extra)


def capture_screenshot(driver: webdriver.Chrome, label: str = "error"):
    """
    Attempts to capture a screenshot with a label + timestamp to './error_screenshots'.
    """
    now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{now_str}_{label}.png"
    path = os.path.join("error_screenshots", filename)
    try:
        driver.get_screenshot_as_file(path)
        logger.info("Screenshot captured: %s", path)
    except WebDriverException as exc:
        logger.warning("Could not capture screenshot: %s", exc)


def send_text_available(phone_number: str, last_date: str, last_time: str):
    """
    Send a text when a new date/time is available (before final booking).
    """
    message = f"Tests are available on {last_date} at {last_time}."
    try:
        text.send_text(phone_number, message)
        logger.info("SMS sent for availability: %s", message)
    except Exception as exc:
        logger.warning("Could not send SMS (available test): %s", exc)


def send_text_test_found(phone_number: str, centre: str, date: str, t_time: str, short_notice: bool):
    """
    Send a text message when a test is actually found (time chosen).
    """
    short_str = "Short Notice" if short_notice else "Standard"
    message = f"Test found at {centre}!\nDate: {date}, Time: {t_time}, {short_str}."
    try:
        text.send_text(phone_number, message)
        logger.info("SMS sent for test found: %s", message)
    except Exception as exc:
        logger.warning("Could not send SMS (test found): %s", exc)


def wait_for_internet_connection():
    """
    Blocks until an internet connection is available.
    """
    while True:
        try:
            resp = requests.get("https://www.google.com/", timeout=5)
            if resp.ok:
                logger.info("Connected to the internet.")
                return
        except Exception:
            pass
        time.sleep(1)


def is_time_between(begin_time: dtime, end_time: dtime, check_time=None) -> bool:
    """
    Checks if the current local time is between begin_time and end_time.
    If crossing midnight, returns True if check_time is outside that range on either side.
    """
    check_time = check_time or datetime.now().time()
    if begin_time < end_time:
        return begin_time <= check_time <= end_time
    return check_time >= begin_time or check_time <= end_time


def input_text_box(driver: webdriver.Chrome, box_id: str, text_to_type: str):
    """
    Types text into a text box (with a random delay between keystrokes).
    """
    try:
        box = driver.find_element(By.ID, box_id)
        for char in text_to_type:
            box.send_keys(char)
            time.sleep(random.uniform(0.01, 0.05))
    except NoSuchElementException as exc:
        logger.error("Cannot find text box %s: %s", box_id, exc)


def solve_captcha(
    driver: webdriver.Chrome,
    skip: bool,
    coord_top_right: tuple,
    coord_bottom_left: tuple
) -> bool:
    """
    Attempts to handle the Imperva/hCaptcha. If skip=True, user solves manually.
    Returns True if we think it's solved or not present, False otherwise.
    """
    if skip:
        logger.info("Manual captcha mode. Please solve hCaptcha manually.")
        time.sleep(60)
        return False

    try:
        # Switch to top-level DOM
        driver.switch_to.default_content()
        page_source = driver.page_source.lower()

        # If we see Imperva text "Request unsuccessful" "incident id", we attempt a click
        if "request unsuccessful" in page_source and "incident id" in page_source:
            logger.info("Possible Imperva/hCaptcha present, attempting hardware click.")

            hardware_click_range(
                top_right=coord_top_right,
                bottom_left=coord_bottom_left
            )
            time.sleep(random.uniform(1.5, 2.5))

        # Return True so we can proceed
        return True

    except Exception as exc:
        logger.warning("Exception while handling Imperva/hCaptcha: %s", exc)
        return False


def check_firewall_and_queue(driver: webdriver.Chrome) -> str:
    """
    Checks the driver’s page source/URL to detect if:
      - We are stuck in a DVSA queue -> return "queue"
      - We are behind the Imperva firewall -> return "firewall"
      - We see a login page -> return "login_required"
      - Or we’re free to proceed -> return "ok"
      - "error" for DVSA "Oops" or unknown
    """
    current_url = driver.current_url.lower()
    page_source = driver.page_source.lower()

    # 1) DVSA Queue
    if "queue.driverpracticaltest.dvsa.gov.uk" in current_url:
        return "queue"

    # 2) Imperva Firewall
    if ("request unsuccessful" in page_source and "incident id" in page_source):
        return "firewall"

    # 3) Standard DVSA "login_required" state
    if "enter details below to access your booking" in page_source:
        return "login_required"

    # 4) DVSA "Oops" error page
    if "oops" in page_source:
        return "error"

    # 5) Otherwise, we assume it's OK
    return "ok"


def scan_for_preferred_tests(
    driver: webdriver.Chrome,
    before_date_str: str,
    after_date_str: str,
    unavailable_dates: list,
    current_test_date: str,
    formatted_test_date: str
):
    """
    Searches the DVSA calendar for a date that meets:
      - date < before_date_str (unless 'None')
      - date > after_date_str (unless 'None')
      - date not in unavailable_dates
      - not the same as your existing test date
      - a weekday (Mon-Fri)
    Returns (found: bool, date_str: str or None, date_element: WebElement or None).
    """
    def parse_or_default(date_string: str, default: str):
        """Attempt to parse a date string or return a default date."""
        if not date_string or date_string == "None":
            return datetime.strptime(default, "%Y-%m-%d")
        return datetime.strptime(date_string, "%Y-%m-%d")

    try:
        # If "Yes" in current_test_date, treat that as no real date
        if current_test_date and "Yes" in current_test_date:
            min_date = datetime.strptime("2050-12-12", "%Y-%m-%d")
        else:
            # E.g. "Wednesday 15 December 2025 2:43PM"
            try:
                dt_current_test = datetime.strptime(current_test_date, "%A %d %B %Y %I:%M%p")
                min_date = dt_current_test - timedelta(days=1)
            except:
                min_date = datetime.strptime("2050-12-12", "%Y-%m-%d")

        # If we have an explicit before_date, override
        if before_date_str and before_date_str != "None":
            parsed_before = datetime.strptime(before_date_str, "%Y-%m-%d")
            if parsed_before < min_date:
                min_date = parsed_before

        max_date = parse_or_default(after_date_str, "2000-01-01")

        cal_body = driver.find_element(By.CLASS_NAME, "BookingCalendar-datesBody")
        days = cal_body.find_elements(By.XPATH, ".//td")

        if not unavailable_dates:
            unavailable_dates = []

        for day in days:
            cls = day.get_attribute("class") or ""
            if "--unavailable" not in cls:
                try:
                    link_el = day.find_element(By.XPATH, ".//a")
                    link_date_str = link_el.get_attribute("data-date")
                    link_date_dt = datetime.strptime(link_date_str, "%Y-%m-%d")

                    if (
                        link_date_str not in unavailable_dates
                        and link_date_dt < min_date
                        and link_date_dt > max_date
                        and link_date_dt.weekday() < 5
                        and link_date_str != formatted_test_date
                    ):
                        return True, link_date_str, link_el
                except NoSuchElementException:
                    pass
        return False, None, None

    except NoSuchElementException as exc:
        logger.error("Calendar not found or incomplete: %s", exc)
        return False, None, None
    except Exception as exc:
        logger.error("Error scanning for tests: %s", exc)
        return False, None, None


def book_test_flow(
    driver: webdriver.Chrome,
    short_notice: bool,
    solve_manually: bool,
    coord_top_right: tuple,
    coord_bottom_left: tuple,
    auto_book_test: bool
) -> bool:
    """
    Once a test date is selected, attempts to finalize booking.
      1) "I am candidate"
      2) Solve captcha if needed
      3) Confirm changes if auto_book_test = True
    """
    booking_attempts = 4
    i_am_candidate_clicked = False
    test_taken = False
    success = False

    for attempt in range(booking_attempts):
        logger.info("Booking attempt %s/%s", attempt + 1, booking_attempts)
        time.sleep(0.3)
        try:
            if not i_am_candidate_clicked:
                driver.find_element(By.ID, "i-am-candidate").click()
                i_am_candidate_clicked = True

            driver.switch_to.default_content()
            try:
                main_frame = driver.find_element(By.ID, "main-iframe")
                driver.switch_to.frame(main_frame)
            except NoSuchElementException:
                # Possibly no main-iframe
                pass

            if "the time chosen is no longer available" in driver.page_source.lower():
                logger.warning("Time chosen is no longer available (taken by someone else).")
                test_taken = True
                break

            # Attempt captcha
            solved = solve_captcha(
                driver,
                skip=solve_manually,
                coord_top_right=coord_top_right,
                coord_bottom_left=coord_bottom_left
            )
            if not solved:
                logger.warning("Captcha not solved successfully.")
            else:
                logger.info("Captcha solved or not present.")

            success = True
            break

        except NoSuchElementException:
            logger.info("No captcha or 'i-am-candidate' found. Possibly reserved already.")
            success = True
            break
        except Exception as exc:
            logger.warning("Booking attempt error: %s", exc)

        # Possibly still on captcha page, try again
        driver.switch_to.default_content()
        time.sleep(1)
        try:
            main_frame = driver.find_element(By.ID, "main-iframe")
            driver.switch_to.frame(main_frame)
            if "Why am I seeing this page" in driver.page_source:
                logger.info("Still behind Imperva puzzle. Will attempt refresh.")
                random_sleep(20, 4)
                driver.refresh()
        except NoSuchElementException:
            pass

    if test_taken:
        logger.warning("Test slot was already taken by someone else.")
        return False
    if not success:
        logger.warning("Failed to finalize booking steps after attempts.")
        return False

    # If we reached here, we presumably have the slot "reserved."
    if auto_book_test:
        logger.info("AUTO_BOOK_TEST is True; clicking 'confirm-changes'.")
        try:
            driver.find_element(By.ID, "confirm-changes").click()
            time.sleep(1)

            page_status = check_firewall_and_queue(driver)
            if page_status == "firewall":
                logger.warning("Imperva triggered at final confirm. Attempting to solve.")
                random_sleep(40, 4)
                driver.refresh()
                if check_firewall_and_queue(driver) == "firewall":
                    solve_captcha(
                        driver,
                        skip=solve_manually,
                        coord_top_right=coord_top_right,
                        coord_bottom_left=coord_bottom_left
                    )
                    if "imperva" in driver.page_source.lower():
                        logger.error("Still behind firewall. Booking failed.")
                        return False
            logger.info("Booking confirmation success!")
            return True

        except NoSuchElementException:
            logger.error("'confirm-changes' button not found.")
            return False
        except Exception as exc:
            logger.error("Error finalizing booking: %s", exc)
            return False
    else:
        logger.info("AUTO_BOOK_TEST=False. Stopping before final confirmation.")
        return True
