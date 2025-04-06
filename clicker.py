# clicker.py

import pyautogui
import random
import time
import logging

logger = logging.getLogger(__name__)

def hardware_click_range(top_right, bottom_left=None):
    """
    OS-level click handler using PyAutoGUI.

    - If 'bottom_left' is None:
         We do an absolute click exactly at 'top_right'.
    - Otherwise:
         We interpret 'bottom_left' as the bottom-left corner of a box
         and 'top_right' as the top-right corner, picking a random point
         in that rectangle for the click.

    'top_right' is always a (x, y) tuple.
    'bottom_left' is either None or (x, y).
    """

    try:
        if bottom_left is None:
            # Direct absolute click at top_right
            return _perform_click(top_right[0], top_right[1], label="absolute_click")
        else:
            # Random click within the bounding box from bottom_left => top_right
            x1, y1 = bottom_left
            x2, y2 = top_right

            # Ensure the coords are actually bottom-left vs top-right
            # i.e. x1 <= x2, y1 <= y2. If your geometry is reversed, swap them or handle logic.
            if x2 <= x1 or y2 <= y1:
                logger.error("Invalid bounding box: top_right <= bottom_left in some dimension.")
                return False

            final_x = random.uniform(x1, x2)
            final_y = random.uniform(y1, y2)
            return _perform_click(final_x, final_y, label="range_click")

    except Exception as e:
        logger.warning("hardware_click_range failed: %s", e)
        return False


def _perform_click(x: float, y: float, label="click"):
    """
    Low-level OS click using PyAutoGUI with random delay and easing.
    """
    duration = random.uniform(0.7, 1.5)
    pyautogui.moveTo(x, y, duration=duration, tween=pyautogui.easeInOutQuad)
    time.sleep(random.uniform(0.05, 0.2))
    pyautogui.mouseDown()
    time.sleep(random.uniform(0.05, 0.2))
    pyautogui.mouseUp()

    logger.info(f"[{label}] Clicked at screen coords ({x:.1f}, {y:.1f})")
    return True
