import os
import time
import random
import logging
import traceback

import undetected_chromedriver as uc

from configparser import ConfigParser
from datetime import datetime
from datetime import timedelta
from datetime import time as dtime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.chrome.service import Service

# Import all our “utility” functions from util.py
from util import (
    parse_config,
    random_sleep,
    capture_screenshot,
    wait_for_internet_connection,
    check_firewall_and_queue,
    solve_captcha,
    is_time_between,
    input_text_box,
    scan_for_preferred_tests,
    book_test_flow,
    send_text_available,
    send_text_test_found,
)

###############################################################################
#                           CONFIG & GLOBALS                                  #
###############################################################################

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

CURRENT_PATH = os.path.dirname(os.path.realpath(__file__))

CONFIG = ConfigParser()
CONFIG.read(os.path.join(CURRENT_PATH, 'config.ini'))

PHONE_NUMBER = CONFIG.get("twilio", "phone_number")
CHROMEDRIVER_PATH = "/bin/chromedriver"

AUTO_BOOK_TEST = CONFIG.get("preferences", "auto_book_test") == "True"
FORMATTED_CURRENT_TEST_DATE = CONFIG.get("preferences", "formatted_current_test_date")

BOOKING_MODE = CONFIG.get("preferences", "booking_mode", fallback="reschedule").strip().lower()

BUSTER_ENABLED = False
BUSTER_PATH = os.path.join(CURRENT_PATH, "buster-chrome.zip")

DVSA_QUEUE_URL = (
    "https://queue.driverpracticaltest.dvsa.gov.uk/"
    "?c=dvsatars&e=ibsredirectprod0915"
    "&t=https%3A%2F%2Fdriverpracticaltest.dvsa.gov.uk%2Flogin&cid=en-GB"
)
DVSA_APPLICATION_URL = "https://driverpracticaltest.dvsa.gov.uk/application"

DVSA_DELAY = 60
MAX_ATTEMPTS = 4

BLOCK_IMAGES = True
SOLVE_MANUALLY = False
RUN_ON_VM = False

DVSA_OPEN_TIME = dtime(6, 5)
DVSA_CLOSE_TIME = dtime(23, 35)

# Coordinates used for hardware click puzzle
COORD_TOP_RIGHT = (820, 420)
COORD_BOTTOM_LEFT = None  # e.g. (1020, 485) if desired

HALIFAX_TEST = True


###############################################################################
#                              DVSABot CLASS                                 #
###############################################################################

class DVSABot:
    """
    Encapsulates logic to:
      1) Initialize a driver
      2) (Reschedule flow) Login or pass the DVSA queue
      3) Search for earlier tests
      4) Optionally book
    """

    def __init__(self, preferences: dict):
        self.preferences = preferences
        self.driver = None
        self.active = False
        self.current_centre_index = 0

    def setup_driver(self):
        chrome_options = uc.ChromeOptions()

        # Optional: block images to speed things up
        if BLOCK_IMAGES:
            prefs = {"profile.managed_default_content_settings.images": 2}
            chrome_options.add_experimental_option("prefs", prefs)

        # Optional: if you're on a VM or need stealthy headers
        if RUN_ON_VM:
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("window-size=1400,900")
            chrome_options.add_argument(
                "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.131 Safari/537.36"
            )

        # Optional: add Buster extension if solving captchas with audio
        if BUSTER_ENABLED:
            chrome_options.add_extension(BUSTER_PATH)

        # 👇 IMPORTANT: Use your real Chrome binary (adjust if you use Chromium)
        chrome_options.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        # Or if you're using Chromium:
        # chrome_options.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

        print("Using Chrome binary:", chrome_options.binary_location)
        print("Shouldn't try and download!")
        # 👇 Use your manually downloaded chromedriver.exe
        self.driver = uc.Chrome(
            driver_executable_path=r"C:\chromedriver\chromedriver.exe",  # your local chromedriver
            options=chrome_options,
            use_subprocess=True,
            patcher=False
        )

        logger.info("Driver initialized using local Chrome + chromedriver.")

    def enter_reschedule_credentials(self, manual: bool = False):
        """
        Enter licence and booking reference for the reschedule flow.
        If manual=True, user has 30s to do it themselves.
        """
        if manual:
            logger.info("Manual credential entry requested. Pausing 30s.")
            time.sleep(30)
            return

        licence_num = self.preferences.get("licence", "")
        booking_ref = self.preferences.get("booking", "")

        input_text_box(self.driver, "driving-licence-number", licence_num)
        input_text_box(self.driver, "application-reference-number", booking_ref)

        random_sleep(1, 1)
        try:
            self.driver.find_element(By.ID, "booking-login").click()
            random_sleep(3, 1)
        except NoSuchElementException:
            logger.error("Could not find 'booking-login' button.")
        logger.info("Credentials entered.")

    def handle_queue_and_firewall(self):
        """(Unchanged) handle queue & Imperva for the reschedule flow."""
        max_queue_checks = 100
        loop_count = 0

        while loop_count < max_queue_checks:
            status = check_firewall_and_queue(self.driver)
            if status == "queue":
                logger.info("DVSA queue active; waiting a bit. (loop_count=%d)", loop_count)
                random_sleep(0.5, 1.5)
                loop_count += 1
                continue

            if status == "firewall":
                logger.warning("Imperva firewall encountered. Attempting fix.")
                random_sleep(0.5, 2.5)
                solved = solve_captcha(
                    self.driver,
                    skip=SOLVE_MANUALLY,
                    coord_top_right=COORD_TOP_RIGHT,
                    coord_bottom_left=COORD_BOTTOM_LEFT
                )
                if solved:
                    time.sleep(3)
                    if check_firewall_and_queue(self.driver) == "firewall":
                        logger.warning("Still behind firewall after captcha. Extra delay 3min.")
                        random_sleep(180, 10)
                else:
                    logger.warning("Captcha not solved; continuing attempts.")
                self.driver.refresh()

            elif status == "login_required":
                logger.info("Reached login page.")
                break
            elif status == "error":
                logger.error("DVSA error page. Possibly 'Oops'. Breaking out.")
                break
            elif status == "ok":
                logger.info("Queue/firewall checks are all clear.")
                break

        if loop_count >= max_queue_checks:
            logger.error("Queue max time exceeded. Attempting fallback refresh.")
            self.driver.refresh()

    def login(self):
        """
        The login step for the *reschedule* flow.
        """
        if not self.driver:
            self.setup_driver()

        logger.info("Navigating to DVSA queue URL: %s", DVSA_QUEUE_URL)
        self.driver.get(DVSA_QUEUE_URL)

        # Step 1: handle queue/firewall
        self.handle_queue_and_firewall()

        # Step 2: enter credentials
        self.enter_reschedule_credentials(manual=False)

        # Step 3: check firewall again
        status = check_firewall_and_queue(self.driver)
        if status == "firewall":
            logger.warning("Firewall triggered after login attempt.")
            random_sleep(2, 5)
            solved = solve_captcha(
                self.driver,
                skip=SOLVE_MANUALLY,
                coord_top_right=COORD_TOP_RIGHT,
                coord_bottom_left=COORD_BOTTOM_LEFT
            )
            if solved:
                time.sleep(3)
                status = check_firewall_and_queue(self.driver)
                if status == "firewall":
                    logger.warning("Still behind firewall. 20s delay.")
                    random_sleep(20, 4)
            else:
                logger.warning("Captcha not solved on second attempt.")
            self.driver.refresh()

        if "loginError=true" in self.driver.current_url:
            logger.error("Incorrect licence/booking reference. Marking inactive.")
            self.active = False
            return

        # Step 4: parse booking summary
        try:
            contents = self.driver.find_elements(By.CLASS_NAME, "contents")
            if len(contents) >= 2:
                test_date_temp = contents[0].find_element(By.XPATH, ".//dd").text
                test_center_temp = contents[1].find_element(By.XPATH, ".//dd").text
                logger.info("Current test date: %s", test_date_temp)
                logger.info("Current test center: %s", test_center_temp)
            else:
                test_date_temp = ""
                test_center_temp = ""
                logger.warning("Could not parse booking summary. Possibly unusual page layout.")

            if "Your booking has been cancelled." in self.driver.page_source:
                logger.warning("Booking was cancelled. Marking inactive.")
                self.active = False
                return

            # If user indicated "Yes" in current date => earliest test scenario
            if "Yes" in self.preferences["current-test"]["date"]:
                logger.info("Earliest test scenario. Changing date/time to earliest.")
                self.driver.find_element(By.ID, "date-time-change").click()
                random_sleep(1, 2)
                self.driver.find_element(By.ID, "test-choice-earliest").click()
                random_sleep(1, 2)
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                random_sleep(1, 2)
                self.driver.find_element(By.ID, "driving-licence-submit").click()

                if test_center_temp:
                    self.preferences["center"] = [test_center_temp]
                random_sleep(1, 2)
            else:
                logger.info("Going to 'test-centre-change' flow.")
                self.driver.find_element(By.ID, "test-centre-change").click()
                random_sleep(3, 2)
                search_box = self.driver.find_element(By.ID, "test-centres-input")
                search_box.clear()
                if self.preferences["center"]:
                    input_text_box(self.driver, "test-centres-input", self.preferences["center"][0])
                else:
                    logger.warning("No center preference found in config.")
                self.driver.find_element(By.ID, "test-centres-submit").click()
                random_sleep(5, 2)

                results_container = self.driver.find_element(By.CLASS_NAME, "test-centre-results")
                first_link = results_container.find_element(By.XPATH, ".//a")
                first_link.click()

            final_status = check_firewall_and_queue(self.driver)
            if final_status in ("firewall", "queue", "error", "login_required"):
                logger.warning("Page ended in state '%s'. Marking inactive.", final_status)
                self.active = False
            else:
                self.active = True

        except Exception as exc:
            logger.error("Error parsing post-login summary: %s", exc)
            self.active = False

    def search_and_book(self):
        """
        The step that checks for earlier tests and tries to re-book.
        (reschedule flow).
        """
        if not self.active:
            logger.info("Bot not active; skipping search.")
            return

        centres = self.preferences.get("center", [])
        if not centres:
            logger.warning("No test centres specified in preferences.")
            return

        driver = self.driver
        search_centre_idx = self.current_centre_index
        self.current_centre_index = (self.current_centre_index + 1) % len(centres)
        centre_to_search = centres[search_centre_idx]

        try:
            logger.info("Switching test centre to '%s'", centre_to_search)
            driver.find_element(By.ID, "change-test-centre").click()
            random_sleep(2, 2)

            search_box = driver.find_element(By.ID, "test-centres-input")
            search_box.clear()
            input_text_box(driver, "test-centres-input", centre_to_search)
            driver.find_element(By.ID, "test-centres-submit").click()
            random_sleep(5, 2)

            results_container = driver.find_element(By.CLASS_NAME, "test-centre-results")
            link = results_container.find_element(By.XPATH, ".//a")
            link.click()
            random_sleep(3, 2)

        except NoSuchElementException:
            logger.error("Could not change test center or find results.")
            status = check_firewall_and_queue(driver)
            if status in ("error", "queue", "firewall", "login_required"):
                logger.warning("Encountered '%s'. Marking inactive.", status)
                self.active = False
            return
        except Exception as exc:
            logger.error("Error while changing centre: %s", exc)
            self.active = False
            return

        if not self.active:
            return

        page_source = driver.page_source.lower()
        if "there are no tests available" in page_source:
            logger.info("No test available at centre '%s'", centre_to_search)
            return
        status = check_firewall_and_queue(driver)
        if status != "ok":
            logger.warning("Detected status '%s' after selecting centre. Marking inactive.", status)
            self.active = False
            return

        logger.info("Tests appear available, scanning for suitable dates.")
        found, found_date_str, date_el = scan_for_preferred_tests(
            driver=driver,
            before_date_str=self.preferences.get("before-date"),
            after_date_str=self.preferences.get("after-date"),
            unavailable_dates=self.preferences.get("disabled-dates", []),
            current_test_date=self.preferences["current-test"]["date"],
            formatted_test_date=FORMATTED_CURRENT_TEST_DATE
        )

        if not found:
            logger.info("No preferred test dates found at this time.")
            return

        try:
            from datetime import datetime

            target_dt = datetime.strptime(found_date_str, "%Y-%m-%d")
            attempts = 0
            while attempts < 12:
                current_month = driver.find_element(By.CLASS_NAME, "BookingCalendar-currentMonth").text
                if target_dt.strftime("%B") == current_month:
                    break
                try:
                    driver.find_element(By.CLASS_NAME, "BookingCalendar-nav--prev").click()
                except NoSuchElementException:
                    logger.warning("Could not navigate calendar to previous month.")
                    break
                random_sleep(0.1, 0.2)
                attempts += 1

            # Select date
            date_el.click()
            container = driver.find_element(By.ID, f"date-{found_date_str}")
            label = container.find_element(By.XPATH, ".//label")
            label_for = label.get_attribute("for")
            epoch_ms = int(label_for.replace("slot-", "")) / 1000
            test_time_str = datetime.fromtimestamp(epoch_ms).strftime("%H:%M")

            short_notice_raw = driver.find_element(By.ID, label_for).get_attribute("data-short-notice")
            short_notice = (short_notice_raw == "true")

            logger.info("Found test: %s %s. Short notice=%s", found_date_str, test_time_str, short_notice)
            send_text_available(PHONE_NUMBER, found_date_str, test_time_str)
            send_text_test_found(PHONE_NUMBER, centre_to_search, found_date_str, test_time_str, short_notice)

            label.click()
            time.sleep(0.2)
            driver.find_element(By.ID, "slot-chosen-submit").click()
            time.sleep(0.4)

            if short_notice:
                driver.find_element(By.XPATH, "(//button[@id='slot-warning-continue'])[2]").click()
            else:
                driver.find_element(By.ID, "slot-warning-continue").click()
            random_sleep(1, 1)

            success = book_test_flow(
                driver,
                short_notice=short_notice,
                solve_manually=SOLVE_MANUALLY,
                coord_top_right=COORD_TOP_RIGHT,
                coord_bottom_left=COORD_BOTTOM_LEFT,
                auto_book_test=AUTO_BOOK_TEST
            )
            if success:
                logger.info("Successfully booked test on %s at %s", found_date_str, test_time_str)
            else:
                logger.warning("Failed to finalize booking. Possibly taken or firewall triggered.")

        except Exception as exc:
            logger.error("Failed booking flow: %s", exc)
            capture_screenshot(driver, label="booking_flow")
            return


###############################################################################
#                          INITIAL BOOKING FLOW                               #
###############################################################################

def run_initial_booking_flow(config_data):
    """
    Flow for the initial test booking:
      - Choose test type
      - Enter licence
      - Enter date and centre
      - Click through to test centre page
    """
    logger.info("Starting Initial Booking Flow...")
    licence_num = config_data.get("licence", "")

    if HALIFAX_TEST is True:
        postcode = "Halifax"
    else:
        postcode = "NE21PL"

    for attempt in range(MAX_ATTEMPTS):
        logger.info("-" * 60)
        logger.info("Initial booking attempt %d / %d", attempt + 1, MAX_ATTEMPTS)

        if is_time_between(DVSA_OPEN_TIME, DVSA_CLOSE_TIME):
            print("DVSA is open. Proceeding with booking flow.")
            driver = None
            try:
                # Setup driver
                print("Setting up driver...")
                chrome_options = uc.ChromeOptions()
                if BLOCK_IMAGES:
                    print("Blocking images to speed up loading...")
                    prefs = {"profile.managed_default_content_settings.images": 2}
                    chrome_options.add_experimental_option("prefs", prefs)
                else:
                    print("Not blocking images. Loading everything...")

                if RUN_ON_VM:
                    print("Running on VM. Adding VM-specific options...")
                    chrome_options.add_argument("--disable-gpu")
                    chrome_options.add_argument("window-size=1400,900")
                    chrome_options.add_argument(
                        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.131 Safari/537.36"
                    )
                else:
                    print("Not running on VM. Using default options.")

                if BUSTER_ENABLED:
                    print("Adding Buster extension for captcha solving...")
                    chrome_options.add_extension(BUSTER_PATH)
                else:
                    print("Not using Buster extension.")

                print("Using Chrome binary:", chrome_options.binary_location)

                driver = uc.Chrome(
                    driver_executable_path=r"C:\chromedriver\chromedriver.exe",  # Adjust path as needed
                    options=chrome_options,
                    use_subprocess=True,
                    patcher=False
                )

                logger.info("Driver created for initial booking flow.")
                driver.get(DVSA_APPLICATION_URL)
                time.sleep(2)

                # Solve captcha/firewall
                for _ in range(5):
                    status = check_firewall_and_queue(driver)
                    if status in ("queue", "firewall"):
                        logger.info("Handling queue/firewall or recaptcha.")
                        solved = solve_captcha(
                            driver,
                            skip=SOLVE_MANUALLY,
                            coord_top_right=COORD_TOP_RIGHT,
                            coord_bottom_left=COORD_BOTTOM_LEFT
                        )
                        if not solved:
                            logger.warning("Captcha failed. Refreshing...")
                            driver.refresh()
                            time.sleep(3)
                            continue
                        else:
                            break
                    elif status in ("ok", "login_required"):
                        break
                    elif status == "error":
                        logger.warning("Error page encountered. Refreshing.")
                        time.sleep(3)
                        driver.refresh()
                    time.sleep(2)

                # Step 1: Click 'Car (manual and automatic)' button
                driver.find_element(By.ID, "test-type-car").click()
                time.sleep(1)

                # Step 2: Fill in licence number
                input_text_box(driver, "driving-licence", licence_num)
                time.sleep(1)

                # Step 3: Select 'No special needs'
                driver.find_element(By.ID, "special-needs-none").click()
                time.sleep(0.5)

                # Step 4: Click first 'Continue'
                driver.find_element(By.ID, "driving-licence-submit").click()
                time.sleep(3)

                # ✅ Step 5: Fill in test date (one week from today)
                future_date = (datetime.now() + timedelta(days=7)).strftime("%d/%m/%y")
                logger.info("Entering preferred test date: %s", future_date)
                input_text_box(driver, "test-choice-calendar", future_date)
                time.sleep(1)

                # ✅ Step 6: Click 'Continue' again (same ID as before)
                driver.find_element(By.ID, "driving-licence-submit").click()
                time.sleep(2)

                # ✅ Step 7: Enter test centre postcode
                logger.info("Entering postcode: %s", postcode)
                input_text_box(driver, "test-centres-input", postcode)
                time.sleep(1)

                # ✅ Step 8: Click 'Find test centres'
                driver.find_element(By.ID, "test-centres-submit").click()
                time.sleep(3)

                # ✅ Step 9: Click the Gateshead centre link
                logger.info("Clicking Gateshead test centre link.")
                if HALIFAX_TEST is True:
                    driver.find_element(By.ID, "centre-name-184").click()
                else:
                    driver.find_element(By.ID, "centre-name-957").click()
                time.sleep(3)

                # ✅ Step 10: Click the first bookable date on the calendar
                try:
                    logger.info("Looking for first bookable calendar date...")
                    calendar_container = driver.find_element(By.CLASS_NAME, "BookingCalendar-datesBody")
                    bookable_days = calendar_container.find_elements(By.CLASS_NAME, "BookingCalendar-date--bookable")

                    if not bookable_days:
                        logger.warning("No bookable dates available.")
                        capture_screenshot(driver, label="no_bookable_dates")
                        return

                    # Click the first one
                    first_day = bookable_days[0]
                    link = first_day.find_element(By.TAG_NAME, "a")
                    date_str = link.get_attribute("data-date")
                    logger.info("Clicking bookable date: %s", date_str)
                    link.click()
                    time.sleep(2)

                except Exception as exc:
                    logger.error("Error clicking bookable calendar date: %s", exc)
                    capture_screenshot(driver, label="click_bookable_date_error")
                    return

                logger.info("Initial booking form submitted through to centre page.")

                # Sleep to avoid spamming
                random_sleep(DVSA_DELAY, 10)
                break

            except Exception as exc:
                logger.error("Top-level exception in initial booking attempt: %s", exc)
                logger.debug(traceback.format_exc())
                if driver:
                    capture_screenshot(driver, label="initial_booking_exception")
                time.sleep(5)

            finally:
                if driver:
                    try:
                        driver.quit()
                    except:
                        pass

        else:
            logger.info("Currently outside DVSA operational hours (%s - %s).", DVSA_OPEN_TIME, DVSA_CLOSE_TIME)
            random_sleep(10, 5)

        if attempt == MAX_ATTEMPTS - 1:
            logger.info("Reached max attempts for initial booking flow. Exiting.")



###############################################################################
#                                 MAIN SCRIPT                                 #
###############################################################################

def main():
    if not os.path.exists("error_screenshots"):
        os.makedirs("error_screenshots")

    logger.info("=" * 80)
    logger.info("DVSA Script Start - %s", datetime.now())
    logger.info("Mode: %s", BOOKING_MODE.upper())
    logger.info("=" * 80)

    wait_for_internet_connection()
    config_data = parse_config(CONFIG)
    logger.info("Preferences loaded:\n%s", config_data)

    if BOOKING_MODE == "reschedule":
        run_reschedule_flow(config_data)
    elif BOOKING_MODE == "booking":
        run_initial_booking_flow(config_data)
    else:
        logger.error("Unknown booking_mode in config: '%s'. Exiting.", BOOKING_MODE)


def run_reschedule_flow(config_data):
    for attempt in range(MAX_ATTEMPTS):
        logger.info("-" * 60)
        logger.info("Reschedule attempt %d / %d", attempt + 1, MAX_ATTEMPTS)

        if is_time_between(DVSA_OPEN_TIME, DVSA_CLOSE_TIME):
            try:
                bot = DVSABot(config_data)
                bot.login()
                if bot.active:
                    bot.search_and_book()
                random_sleep(DVSA_DELAY, 10)

            except Exception as exc:
                logger.error("Top-level exception in reschedule attempt: %s", exc)
                logger.debug(traceback.format_exc())
                if 'bot' in locals() and bot.driver:
                    capture_screenshot(bot.driver, label="top_level_exception")
                try:
                    bot.driver.quit()
                except:
                    pass
                time.sleep(30)
        else:
            logger.info("Currently outside DVSA operational hours (%s - %s).", DVSA_OPEN_TIME, DVSA_CLOSE_TIME)
            random_sleep(10, 5)

        if attempt == MAX_ATTEMPTS - 1:
            logger.info("Reached max reschedule attempts. Exiting.")


if __name__ == "__main__":
    main()
