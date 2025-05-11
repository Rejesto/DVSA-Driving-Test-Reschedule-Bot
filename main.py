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
    are_we_in,
    log_test_centre_availability
)

###############################################################################
#                           CONFIG & GLOBALS                                  #
###############################################################################

DRIVER_EXECUTABLE_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "chromedriver",
                                      "chromedriver_win.exe")

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

MANUALLY_SOLVING_HANG = True

DVSA_QUEUE_URL = (
    "https://queue.driverpracticaltest.dvsa.gov.uk/"
    "?c=dvsatars&e=ibsredirectprod0915"
    "&t=https%3A%2F%2Fdriverpracticaltest.dvsa.gov.uk%2Flogin&cid=en-GB"
)
DVSA_APPLICATION_URL = "https://driverpracticaltest.dvsa.gov.uk/application"

DVSA_DELAY = 60
MAX_ATTEMPTS = 4

BLOCK_IMAGES = False
SOLVE_MANUALLY = False
RUN_ON_VM = False

DVSA_OPEN_TIME = dtime(6, 0, 30)
DVSA_CLOSE_TIME = dtime(21, 50)

# Coordinates used for hardware click puzzle
COORD_TOP_RIGHT = (820, 420)
COORD_BOTTOM_LEFT = None  # e.g. (1020, 485) if desired

ALTERNATIVE_TEST = None


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

        self.current_test_date_obj = None
        self.current_test_date_str = None

        print("Initializing DVSABot with preferences:", preferences)

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
        # 👇 Use your manually downloaded chromedriver_win.exe
        self.driver = uc.Chrome(
            driver_executable_path=DRIVER_EXECUTABLE_PATH,  # your local chromedriver
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
        """Updated queue & firewall handling using modern CAPTCHA logic with logging."""
        continue_flag = False
        logger.info("Starting queue and firewall handling...")

        for attempt in range(5):
            logger.info("Queue/firewall check attempt %d", attempt + 1)
            status = check_firewall_and_queue(self.driver)
            logger.info("Firewall/queue check returned status: %s", status)

            if status in ("queue", "firewall"):
                logger.info("Encountered '%s'. Initiating CAPTCHA handling.", status)

                solved = solve_captcha(
                    self.driver,
                    skip=SOLVE_MANUALLY,
                    coord_top_right=COORD_TOP_RIGHT,
                    coord_bottom_left=COORD_BOTTOM_LEFT
                )
                logger.info("CAPTCHA solve attempt result: %s", solved)

                if not solved:
                    if MANUALLY_SOLVING_HANG:
                        logger.info("Manual solving enabled. Waiting for user to solve CAPTCHA manually.")
                        while True:
                            if are_we_in(self.driver):
                                logger.info("User has manually solved CAPTCHA.")
                                continue_flag = True
                                break
                            logger.info("Still waiting for manual CAPTCHA solution...")
                            random_sleep(5, 5)
                    else:
                        logger.warning("CAPTCHA not solved. Refreshing the page to retry.")
                        self.driver.refresh()
                        time.sleep(3)

                    if continue_flag:
                        logger.info("Breaking out after manual solve.")
                        break

                    logger.info("Retrying firewall/queue logic after unsuccessful solve.")
                    continue
                else:
                    logger.info("CAPTCHA solved successfully.")
                    break

            elif status in ("ok", "login_required"):
                logger.info("Firewall clear. Proceeding with login flow. Status: %s", status)
                break

            elif status == "error":
                logger.warning("Error page encountered. Refreshing page.")
                time.sleep(3)
                self.driver.refresh()

            time.sleep(2)

        logger.info("Finished queue/firewall handling routine.")

    def login(self):
        """
        Logs into DVSA by:
          1. Getting through the firewall/queue.
          2. Entering user credentials.
        Does NOT continue to booking logic.
        """
        if not self.driver:
            self.setup_driver()

        logger.info("Navigating to DVSA queue URL: %s", DVSA_QUEUE_URL)
        self.driver.get(DVSA_QUEUE_URL)

        # Step 1: pass firewall/queue
        self.handle_queue_and_firewall()

        # Step 2: enter login credentials
        self.enter_reschedule_credentials(manual=False)

        # Step 3: firewall may appear again after form submission
        self.handle_queue_and_firewall()

        # Step 4: check for login error
        if "loginError=true" in self.driver.current_url:
            logger.error("Incorrect licence/booking reference. Marking inactive.")
            self.active = False
            return

        self.active = True

    def search_and_book(self):
        if not self.active:
            logger.info("Bot not active; skipping search.")
            return False

        logger.info("Waiting for reschedule page to become active...")
        for _ in range(30):
            status = check_firewall_and_queue(self.driver)
            if status == "reschedule_page":
                logger.info("Reschedule page detected.")
                break
            logger.info("Still waiting for reschedule page... (%s)", status)
            time.sleep(2)
        else:
            logger.warning("Reschedule page did not load in time. Marking inactive.")
            self.active = False
            return False

        try:
            logger.info("Extracting current test date from reschedule summary panel...")
            date_panel = self.driver.find_element(By.CLASS_NAME, "contents")
            raw_date_text = date_panel.find_element(By.XPATH, ".//dd").text.strip()
            logger.info("Found test date string: '%s'", raw_date_text)
            self.current_test_date_str = raw_date_text

            # Try parsing into a datetime object
            for fmt in ("%A %d %B %Y %I:%M%p", "%d/%m/%Y %H:%M", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    self.current_test_date_obj = datetime.strptime(raw_date_text, fmt)
                    logger.info("Parsed current test date: %s", self.current_test_date_obj)
                    break
                except ValueError:
                    continue

            if not self.current_test_date_obj:
                logger.warning("Unable to parse current test date. Storing raw string only.")
        except Exception as e:
            logger.warning("Failed to extract current test date: %s", e)

        # Submit 'earliest test' option
        try:
            logger.info("Submitting earliest test option.")
            logger.info("Clicking 'earliest test' button.")
            self.driver.find_element(By.ID, "date-time-change").click()
            random_sleep(1, 2)
            logger.info("Clicking 'earliest test' radio button.")
            self.driver.find_element(By.ID, "test-choice-earliest").click()

            random_sleep(1, 2)
            logger.info("Clicking 'Continue' button.")
            self.driver.find_element(By.ID, "driving-licence-submit").click()
        except Exception as e:
            logger.error("Failed to submit earliest test choice: %s", e)
            self.active = False
            return False

        centres = self.preferences.get("center", [])
        if not centres:
            logger.warning("No test centres specified in preferences.")
            self.reset_reschedule()
            return False

        self.current_centre_index = (self.current_centre_index + 1) % len(centres)
        centre_to_search = centres[self.current_centre_index - 1]

        if not self.check_centre_for_tests(centre_to_search):
            logger.info("No tests available at centre '%s'.", centre_to_search)
            random_sleep(80, 220)
            self.reset_reschedule()
            return False

        found, found_date_str, date_el = scan_for_preferred_tests(
            driver=self.driver,
            before_date_str=self.preferences.get("before-date"),
            after_date_str=self.preferences.get("after-date"),
            unavailable_dates=self.preferences.get("disabled-dates", []),
            current_test_date=self.preferences["current-test"]["date"],
            formatted_test_date=FORMATTED_CURRENT_TEST_DATE,
            minimum_days_notice=self.preferences.get("minimum_days_notice", 0)
        )
        logger.info("Scan result: %s", [found, found_date_str, date_el])

        if not found:
            logger.info("No preferred test dates found.")
            self.reset_reschedule()
            return False

        try:
            min_notice_days = int(self.preferences.get("minimum_days_notice", 0))
            test_date = datetime.strptime(found_date_str, "%Y-%m-%d")
            days_until_test = (test_date - datetime.now()).days

            if days_until_test < min_notice_days:
                logger.info("Test is only %d days away, which is less than minimum of %d. Skipping booking.",
                            days_until_test, min_notice_days)
                self.reset_reschedule()
                return False
        except Exception as exc:
            logger.warning("Failed to apply days notice check: %s", exc)

        self.attempt_booking(found_date_str, date_el, centre_to_search)
        return True

    def reset_reschedule(self):
        """
        Navigates back to the original booking screen.
        """
        logger.info("Resetting by returning to original booking screen...")
        try:
            return_link = self.driver.find_element(By.ID, "return-original-booking-link")
            return_link.click()
            time.sleep(2)
            logger.info("Successfully clicked return to booking link.")
        except NoSuchElementException:
            logger.error("Return to booking link not found.")
        except Exception as e:
            logger.error("Failed during reset: %s", e)

        logger.info("Reset complete. Waiting for 5 seconds before next attempt.")
        time.sleep(5)
        self.search_and_book()

    def change_test_centre(self, centre_to_search):
        try:
            logger.info("Switching test centre to '%s'", centre_to_search)
            self.driver.find_element(By.ID, "change-test-centre").click()
            random_sleep(2, 2)

            search_box = self.driver.find_element(By.ID, "test-centres-input")
            search_box.clear()
            input_text_box(self.driver, "test-centres-input", centre_to_search)
            self.driver.find_element(By.ID, "test-centres-submit").click()
            random_sleep(5, 2)

            results_container = self.driver.find_element(By.CLASS_NAME, "test-centre-results")
            link = results_container.find_element(By.XPATH, ".//a")
            link.click()
            random_sleep(3, 2)
            return True

        except NoSuchElementException:
            logger.error("Could not change test center or find results.")
            status = check_firewall_and_queue(self.driver)
            if status in ("error", "queue", "firewall", "login_required"):
                logger.warning("Encountered '%s'. Marking inactive.", status)
                self.active = False
            return False
        except Exception as exc:
            logger.error("Error while changing centre: %s", exc)
            self.active = False
            return False

    def check_centre_for_tests(self, centre_to_search):
        """
        Scans the booking calendar for available test dates.
        If no valid dates are available in the current or previous month, initiates reset.
        """
        logger.info("Checking calendar for available test dates at '%s'...", centre_to_search)

        try:
            calendar_body = self.driver.find_element(By.CLASS_NAME, "BookingCalendar-datesBody")

            all_bookable = calendar_body.find_elements(By.CLASS_NAME, "BookingCalendar-date--bookable")
            # Filter out dates that are already selected (e.g., 'is-chosen')
            valid_bookable = []
            for el in all_bookable:
                cls = el.get_attribute("class")
                if "is-chosen" in cls:
                    continue  # Skip selected
                try:
                    date_link = el.find_element(By.TAG_NAME, "a")
                    date_str = date_link.get_attribute("data-date")
                    if self.current_test_date_obj and date_str == self.current_test_date_obj.strftime("%Y-%m-%d"):
                        logger.debug("Skipping already booked test date: %s", date_str)
                        continue
                    valid_bookable.append(el)
                except Exception as e:
                    logger.warning("Failed to process bookable element: %s", e)
                    continue

            logger.info("Found %d total bookable days, %d valid (non-chosen) bookable days.",
                        len(all_bookable), len(valid_bookable))

            if valid_bookable:
                logger.info("Valid test dates available.")
                logger.info("Dates: %s",
                            [el.find_element(By.TAG_NAME, "a").get_attribute("data-date") for el in valid_bookable])
                return True

            # No valid bookable dates — check previous month
            try:
                prev_month_button = self.driver.find_element(By.CLASS_NAME, "BookingCalendar-nav--prev")
                if "is-active" in prev_month_button.get_attribute("class"):
                    logger.info("No valid dates this month. Going to previous month...")
                    prev_month_button.click()
                    time.sleep(2)
                else:
                    logger.warning("Previous month button not active. Cannot check earlier dates.")
                    self.reset_reschedule()
                    return False
            except NoSuchElementException:
                logger.warning("Previous month navigation button not found.")
                self.reset_reschedule()
                return False

            # Check for "no earlier tests" message
            page_source = self.driver.page_source.lower()
            if "there are no earlier tests available" in page_source:
                logger.info("Message indicates no earlier tests available.")
                random_sleep(80, 120)
                self.reset_reschedule()
                return False

            # Re-check calendar in previous month
            calendar_body = self.driver.find_element(By.CLASS_NAME, "BookingCalendar-datesBody")
            all_bookable = calendar_body.find_elements(By.CLASS_NAME, "BookingCalendar-date--bookable")
            valid_bookable = [
                el for el in all_bookable
                if "is-chosen" not in el.get_attribute("class")
            ]

            logger.info("In previous month: %d total bookable, %d valid.", len(all_bookable), len(valid_bookable))

            if valid_bookable:
                return True
            else:
                logger.info("No valid test dates found even in previous month.")
                self.reset_reschedule()
                return False

        except Exception as exc:
            logger.error("Error while checking calendar: %s", exc)
            capture_screenshot(self.driver, label="check_centre_for_tests_error")
            self.reset_reschedule()
            return False

    def navigate_to_test_month(self, target_dt):
        attempts = 0
        while attempts < 12:
            current_month = self.driver.find_element(By.CLASS_NAME, "BookingCalendar-currentMonth").text
            if target_dt.strftime("%B") == current_month:
                break
            try:
                self.driver.find_element(By.CLASS_NAME, "BookingCalendar-nav--prev").click()
            except NoSuchElementException:
                logger.warning("Could not navigate calendar to previous month.")
                break
            random_sleep(0.1, 0.2)
            attempts += 1

    def extract_test_slot_info(self, found_date_str):
        container = self.driver.find_element(By.ID, f"date-{found_date_str}")
        label = container.find_element(By.XPATH, ".//label")
        label_for = label.get_attribute("for")

        epoch_ms = int(label_for.replace("slot-", "")) / 1000
        test_time_str = datetime.fromtimestamp(epoch_ms).strftime("%H:%M")

        short_notice_raw = self.driver.find_element(By.ID, label_for).get_attribute("data-short-notice")
        short_notice = (short_notice_raw == "true")

        return label, test_time_str, short_notice

    def attempt_booking(self, found_date_str, date_el, centre_to_search):
        time.sleep(500)
        try:
            target_dt = datetime.strptime(found_date_str, "%Y-%m-%d")
            self.navigate_to_test_month(target_dt)

            date_el.click()
            label, test_time_str, short_notice = self.extract_test_slot_info(found_date_str)

            logger.info("Found test: %s %s. Short notice=%s", found_date_str, test_time_str, short_notice)
            send_text_available(PHONE_NUMBER, found_date_str, test_time_str)
            send_text_test_found(PHONE_NUMBER, centre_to_search, found_date_str, test_time_str, short_notice)

            label.click()
            time.sleep(0.2)
            self.driver.find_element(By.ID, "slot-chosen-submit").click()
            time.sleep(0.4)

            if short_notice:
                self.driver.find_element(By.XPATH, "(//button[@id='slot-warning-continue'])[2]").click()
            else:
                self.driver.find_element(By.ID, "slot-warning-continue").click()
            random_sleep(1, 1)

            success = book_test_flow(
                self.driver,
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
            capture_screenshot(self.driver, label="booking_flow")


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
      - Choose an available slot
      - Click continue and confirm
    """
    logger.info("Starting Initial Booking Flow...")
    licence_num = config_data.get("licence", "")

    # Use alternative test centre if defined; otherwise, default postcode.
    if ALTERNATIVE_TEST is not None:
        postcode = ALTERNATIVE_TEST[0]
    else:
        postcode = "NE21PL"

    attempt = 0
    # Loop until we complete a booking attempt or hit MAX_ATTEMPTS.
    while attempt < MAX_ATTEMPTS:
        # Check if DVSA is open; if not, wait without counting as an attempt.
        if not is_time_between(DVSA_OPEN_TIME, DVSA_CLOSE_TIME):
            logger.info("Currently outside DVSA operational hours (%s - %s). Waiting...",
                        DVSA_OPEN_TIME, DVSA_CLOSE_TIME)
            random_sleep(10, 5)
            continue

        logger.info("-" * 60)
        logger.info("Initial booking attempt %d / %d", attempt + 1, MAX_ATTEMPTS)
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

            # Set the Chrome binary location (adjust if you use Chromium)
            chrome_options.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
            print("Using Chrome binary:", chrome_options.binary_location)

            # Launch the driver using your local chromedriver and disable patching.
            driver = uc.Chrome(
                driver_executable_path=DRIVER_EXECUTABLE_PATH,  # Adjust path as needed
                options=chrome_options,
                use_subprocess=True,
                patcher=False
            )

            logger.info("Driver created for initial booking flow.")
            driver.get(DVSA_APPLICATION_URL)
            time.sleep(2)

            # --- CAPTCHA/FIREWALL HANDLING ---
            continue_flag = False
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

                    logger.info(f"Solved Status was {status}.")

                    if solved is not True:
                        if MANUALLY_SOLVING_HANG:
                            logger.info("Solving manually.")
                            while True:
                                if are_we_in(driver):
                                    logger.info("Captcha was solved manually.")
                                    continue_flag = True
                                    break
                                logger.info("Captcha has not yet been solved manually.")
                                random_sleep(5, 5)

                        else:
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
            # --- End of CAPTCHA/FIREWALL HANDLING ---

            # --- NEW: Queue Handling (after captcha is solved) ---
            while True:
                try:
                    queue_elem = driver.find_element(By.ID, "MainPart_lbUsersInLineAheadOfYouText")
                    if queue_elem.is_displayed():
                        logger.info("Queue detected: %s", queue_elem.text)
                        logger.info("Waiting in queue... checking again in 10 seconds.")
                        time.sleep(10)
                        # Do NOT refresh the page—just wait and re-check.
                        continue
                    else:
                        break
                except NoSuchElementException:
                    break
                except Exception as e:
                    logger.error("Error during queue handling: %s", e)
                    break
            # --- End of Queue Handling ---

            # --- NEW: Check for Oops Page ---
            try:
                oops_elem = driver.find_element(By.XPATH,
                                                "//*[contains(text(), 'Oops - you went away and came back again')]")
                if oops_elem.is_displayed():
                    logger.info("Detected Oops page. Clicking 'Continue' button to proceed.")
                    continue_button = driver.find_element(By.XPATH, "//button[contains(text(), 'Continue')]")
                    continue_button.click()
                    time.sleep(2)
            except NoSuchElementException:
                logger.info("No Oops page detected. Proceeding with booking flow.")
            except Exception as e:
                logger.error("Error handling Oops page: %s", e)
            # --- End of Oops Page Handling ---

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

            # Step 5: Fill in test date (for testing, set to a fixed date; otherwise, use one week from now)
            future_date = (datetime.now() + timedelta(days=14)).strftime("%d/%m/%y")

            logger.info("Entering preferred test date: %s", future_date)
            input_text_box(driver, "test-choice-calendar", future_date)
            time.sleep(1)

            # Step 6: Click 'Continue' again (same ID as before)
            driver.find_element(By.ID, "driving-licence-submit").click()
            time.sleep(2)

            # --- NEW: Save the current URL before entering the postcode ---
            saved_url = driver.current_url
            logger.info("Saved URL for test centre page: %s", saved_url)

            # Step 7: Enter test centre postcode
            logger.info("Entering postcode: %s", postcode)
            input_text_box(driver, "test-centres-input", postcode)
            time.sleep(1)

            # Step 8: Click 'Find test centres'
            driver.find_element(By.ID, "test-centres-submit").click()
            time.sleep(3)

            # Step 9: Click the test centre link (e.g., Gateshead or alternative)
            logger.info("Clicking test centre link.")
            if ALTERNATIVE_TEST is not None:
                driver.find_element(By.ID, f"centre-name-{ALTERNATIVE_TEST[1]}").click()
            else:
                driver.find_element(By.ID, "centre-name-957").click()
            time.sleep(3)

            # --- Step 10: Click the first bookable date on the calendar with retries ---
            found_bookable_date = False
            max_date_retries = 30
            for retry in range(max_date_retries):
                try:
                    logger.info("Looking for first bookable calendar date (attempt %d)...", retry + 1)
                    calendar_container = driver.find_element(By.CLASS_NAME, "BookingCalendar-datesBody")
                    bookable_days = calendar_container.find_elements(By.CLASS_NAME, "BookingCalendar-date--bookable")

                    if not bookable_days:
                        logger.warning("No bookable dates available on attempt %d.", retry + 1)
                        capture_screenshot(driver, label="no_bookable_dates")
                        wait_time = random.uniform(10, 20)
                        logger.info("Waiting for %.2f seconds before revisiting the test centre page...", wait_time)
                        time.sleep(wait_time)
                        # Instead of refreshing, revisit the saved URL and redo steps 7-9.
                        driver.get(saved_url)
                        time.sleep(3)
                        logger.info("Re-entering postcode: %s", postcode)
                        input_text_box(driver, "test-centres-input", postcode)
                        time.sleep(1)
                        driver.find_element(By.ID, "test-centres-submit").click()
                        time.sleep(3)
                        log_test_centre_availability(driver)

                        if ALTERNATIVE_TEST is not None:
                            driver.find_element(By.ID, f"centre-name-{ALTERNATIVE_TEST[1]}").click()
                        else:
                            driver.find_element(By.ID, "centre-name-957").click()
                        time.sleep(3)
                        continue
                    # Found at least one bookable day.
                    found_bookable_date = True
                    first_day = bookable_days[0]
                    link = first_day.find_element(By.TAG_NAME, "a")
                    date_str = link.get_attribute("data-date")
                    logger.info("Clicking bookable date: %s", date_str)
                    link.click()
                    time.sleep(2)
                    break
                except Exception as exc:
                    logger.error("Error looking for bookable date on attempt %d: %s", retry + 1, exc)
                    capture_screenshot(driver, label="click_bookable_date_error")
                    wait_time = random.uniform(10, 20)
                    logger.info("Waiting for %.2f seconds before revisiting the test centre page...", wait_time)
                    time.sleep(wait_time)
                    driver.get(saved_url)
                    time.sleep(3)
                    logger.info("Re-entering postcode: %s", postcode)
                    element = driver.find_element(By.ID, "test-centres-input")
                    element.clear()
                    input_text_box(driver, "test-centres-input", postcode)
                    time.sleep(1)
                    log_test_centre_availability(driver)

                    driver.find_element(By.ID, "test-centres-submit").click()
                    time.sleep(3)
                    if ALTERNATIVE_TEST is not None:
                        driver.find_element(By.ID, f"centre-name-{ALTERNATIVE_TEST[1]}").click()
                    else:
                        driver.find_element(By.ID, "centre-name-957").click()
                    time.sleep(3)
            if not found_bookable_date:
                logger.error("Failed to find any bookable date after %d attempts.", max_date_retries)
                return

            # Step 11: Choose an available slot
            try:
                logger.info("Looking for available slot...")
                slot_labels = driver.find_elements(By.CSS_SELECTOR, "label.SlotPicker-slot-label.unchecked")
                if not slot_labels:
                    logger.warning("No available slots found.")
                    capture_screenshot(driver, label="no_slot_found")
                    return
                # Click the first available slot
                slot_labels[0].click()
                time.sleep(1)
                logger.info("Slot selected.")
            except Exception as exc:
                logger.error("Error choosing available slot: %s", exc)
                capture_screenshot(driver, label="slot_choice_error")
                return

            # Step 12: Click the continue button for the chosen slot
            try:
                logger.info("Clicking slot chosen continue button.")
                driver.find_element(By.ID, "slot-chosen-submit").click()
                time.sleep(1)
            except Exception as exc:
                logger.error("Error clicking slot chosen continue button: %s", exc)
                capture_screenshot(driver, label="slot_continue_error")
                return

            # Step 13: Click the confirm button
            try:
                logger.info("Clicking confirm button.")
                driver.find_element(By.ID, "slot-warning-continue").click()
                time.sleep(2)
            except Exception as exc:
                logger.error("Error clicking confirm button: %s", exc)
                capture_screenshot(driver, label="confirm_button_error")
                return

            logger.info("Initial booking form submitted through to centre page and slot confirmed.")

            # Sleep to avoid spamming and then break out of the loop
            random_sleep(DVSA_DELAY, 10)
            if MANUALLY_SOLVING_HANG:
                random_sleep(500, 10)
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

        attempt += 1

    if attempt >= MAX_ATTEMPTS:
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
                logger.info("Setting up driver for reschedule flow...")
                bot.login()
                logger.info("Driver setup complete.")

                bot.search_and_book()

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
