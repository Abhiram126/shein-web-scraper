import os
import undetected_chromedriver as uc


class BrowserManager:
    def __init__(self):
        self.browser = None

    def get_browser(self):
        """
        Returns an existing browser if available.
        Otherwise creates a new one.
        """

        browser_alive = False

        # Check if the existing browser is still usable
        if self.browser is not None:
            try:
                _ = self.browser.current_url
                browser_alive = True
            except Exception:
                browser_alive = False

        # Create a new browser if needed
        if not browser_alive:

            options = uc.ChromeOptions()

            profile_path = os.path.abspath(
                os.path.join(os.getcwd(), "ChromeProfile")
            )

            options.add_argument(f"--user-data-dir={profile_path}")
            options.add_argument("--start-minimized")

            self.browser = uc.Chrome(
                options=options,
                
            )

        return self.browser

    def close_browser(self):

        if self.browser:
            try:
                self.browser.quit()
            except Exception:
                pass

            self.browser = None