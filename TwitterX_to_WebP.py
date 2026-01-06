"""
Twitter/X Account Archiver
Monitors multiple X accounts and captures screenshots of new posts, reposts, and comments.
"""

import os
import json
import time
import random
import urllib.request
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from PIL import Image
import io

# Ensure random is properly seeded
random.seed()

class TwitterArchiver:
    def __init__(self, base_storage_path, target_usernames, check_interval_base=120):
        """
        Initialize the Twitter archiver.

        Args:
            base_storage_path: Directory where user folders will be created
            target_usernames: List of X usernames to monitor (without @)
            check_interval_base: Base interval in seconds (default 120 = 2 min)
        """
        self.base_path = Path(base_storage_path)
        self.target_usernames = target_usernames
        self.check_interval_base = check_interval_base
        self.check_interval_variance = 60  # +/- 60 seconds

        # Create base directory if it doesn't exist
        self.base_path.mkdir(parents=True, exist_ok=True)

        # Create user directories and load tracking data
        self.user_data = {}
        for username in target_usernames:
            user_path = self.base_path / username
            user_path.mkdir(exist_ok=True)

            # Load or create tracking file
            tracking_file = user_path / "tracked_posts.json"
            if tracking_file.exists():
                with open(tracking_file, 'r') as f:
                    self.user_data[username] = json.load(f)
            else:
                self.user_data[username] = {"captured_ids": []}

    def save_tracking_data(self, username):
        """Save the tracking data for a user."""
        tracking_file = self.base_path / username / "tracked_posts.json"
        with open(tracking_file, 'w') as f:
            json.dump(self.user_data[username], f, indent=2)

    def get_post_id_from_url(self, url):
        """Extract post ID from Twitter URL."""
        if not url:
            return None
        parts = url.split('/')
        for i, part in enumerate(parts):
            if part == 'status' and i + 1 < len(parts):
                return parts[i + 1].split('?')[0]
        return None

    def extract_images_from_tweet(self, article, username, post_id, timestamp):
        """
        Extract and save full-resolution images from a tweet.

        Args:
            article: Playwright element containing the tweet
            username: X username being monitored
            post_id: Unique identifier for the post
            timestamp: Timestamp string for filename

        Returns:
            Number of images saved
        """
        images_saved = 0

        try:
            # Try multiple selectors to find images
            selectors_to_try = [
                '[data-testid="tweetPhoto"] img',
                'img[src*="pbs.twimg.com/media"]',
                'div[data-testid="tweetPhoto"]',
                'img[alt*="Image"]'
            ]

            image_elements = []
            for selector in selectors_to_try:
                elements = article.locator(selector).all()
                if len(elements) > 0:
                    print(f"  🔍 Selector '{selector}' found {len(elements)} element(s)")
                    image_elements = elements
                    break

            if len(image_elements) == 0:
                print(f"  ℹ️  No images found in this tweet")
                return 0

            for idx, img_element in enumerate(image_elements, 1):
                try:
                    # Get the image source URL
                    img_url = img_element.get_attribute('src')

                    # Also try getting from parent div if img doesn't have src
                    if not img_url:
                        # Try getting background-image from parent
                        parent = img_element.locator('xpath=..').first
                        if parent.count() > 0:
                            style = parent.get_attribute('style')
                            if style and 'background-image' in style:
                                # Extract URL from background-image: url("...")
                                import re
                                match = re.search(r'url\(["\']?(.*?)["\']?\)', style)
                                if match:
                                    img_url = match.group(1)
                                    print(f"  🔍 Image {idx}: Found URL in background-image")

                    if not img_url:
                        print(f"  ⚠ Image {idx}: No src or background-image found")
                        # Let's see what attributes it has
                        print(f"  🔎 Debugging element attributes...")
                        continue

                    # Skip profile images (they contain 'profile_images' in URL)
                    if 'profile_images' in img_url:
                        print(f"  ⏭ Image {idx}: Skipping profile picture")
                        continue

                    # Skip if not a media URL
                    if 'pbs.twimg.com/media' not in img_url and 'twimg.com' not in img_url:
                        print(f"  ⏭ Image {idx}: Not a tweet media image")
                        continue

                    print(f"  📥 Image {idx}: Downloading from {img_url[:80]}...")

                    # X serves images with size parameters like &name=small
                    # Replace with &name=large or &name=orig for full resolution
                    if '&name=' in img_url:
                        # Get original/largest version
                        base_url = img_url.split('&name=')[0]
                        img_url = base_url + '&name=orig'
                        print(f"  📐 Image {idx}: Requesting original size")
                    elif '?format=' in img_url and 'name=' not in img_url:
                        # Add name parameter if not present
                        img_url = img_url + '&name=orig'
                        print(f"  📐 Image {idx}: Adding original size parameter")

                    # Download the image
                    response = urllib.request.urlopen(img_url)
                    img_data = response.read()

                    # Determine file extension from URL
                    ext = 'jpg'  # Default
                    if 'format=png' in img_url:
                        ext = 'png'
                    elif 'format=webp' in img_url:
                        ext = 'webp'

                    # Create filename: timestamp_imageN_postid.ext
                    filename = f"{timestamp}_image{idx}_{post_id}.{ext}"
                    filepath = self.base_path / username / filename

                    # Save the image
                    with open(filepath, 'wb') as f:
                        f.write(img_data)

                    file_size_kb = len(img_data) / 1024
                    print(f"  ✅ Image {idx}: Saved as {filename} ({file_size_kb:.1f} KB)")
                    images_saved += 1

                except Exception as e:
                    print(f"  ❌ Image {idx}: Error - {e}")
                    continue

            return images_saved

        except Exception as e:
            print(f"  ❌ Error in extract_images_from_tweet: {e}")
            return 0

    def capture_screenshot(self, element, username, content_type, post_id):
        """
        Capture screenshot of an element and save as WebP.

        Args:
            element: Playwright element to screenshot
            username: X username being monitored
            content_type: 'post', 'repost', or 'comment'
            post_id: Unique identifier for the post

        Returns:
            Timestamp string if successful, None otherwise
        """
        try:
            # Take screenshot as PNG first
            screenshot_bytes = element.screenshot()

            # Convert to WebP using Pillow
            image = Image.open(io.BytesIO(screenshot_bytes))

            # Generate filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{timestamp}_{content_type}_{post_id}.webp"
            filepath = self.base_path / username / filename

            # Save as WebP with good quality
            image.save(filepath, 'WEBP', quality=85)

            print(f"✓ Saved screenshot: {filename}")
            return timestamp  # Return timestamp for image extraction
        except Exception as e:
            print(f"✗ Error capturing screenshot: {e}")
            return None

    def login(self, page, username, password):
        """
        Log into X/Twitter.

        Args:
            page: Playwright page object
            username: X username or email
            password: X password
        """
        print("🔐 Logging into X...")

        try:
            page.goto("https://x.com/i/flow/login", wait_until="networkidle", timeout=30000)
            time.sleep(2)

            # Enter username/email
            username_input = page.locator('input[autocomplete="username"]')
            username_input.fill(username)
            time.sleep(1)

            # Click Next
            page.locator('button:has-text("Next")').click()
            time.sleep(2)

            # Check if phone/username verification is needed (sometimes X asks for this)
            if page.locator('input[data-testid="ocfEnterTextTextInput"]').count() > 0:
                print("⚠️  X is asking for additional verification (phone/username)")
                print("    You may need to handle this manually or use session cookies")
                return False

            # Enter password
            password_input = page.locator('input[name="password"]')
            password_input.fill(password)
            time.sleep(1)

            # Click Log in
            page.locator('button[data-testid="LoginForm_Login_Button"]').click()
            time.sleep(5)

            # Check if login was successful
            if "home" in page.url.lower() or page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').count() > 0:
                print("✓ Login successful!")
                return True
            else:
                print("✗ Login may have failed - check credentials")
                return False

        except Exception as e:
            print(f"✗ Login error: {e}")
            return False

    def hide_overlays(self, page):
        """Remove popups, modals, and overlay elements that interfere with screenshots."""
        try:
            # Hide common X overlays and popups (but NOT login modals)
            page.evaluate("""
                () => {
                    // Remove "Who to follow" suggestions
                    const followSuggestions = document.querySelectorAll('[data-testid="UserCell"]');
                    followSuggestions.forEach(el => {
                        const container = el.closest('aside') || el.closest('[role="complementary"]');
                        if (container) container.style.display = 'none';
                    });
                    
                    // Remove the right sidebar entirely (contains ads and suggestions)
                    const rightSidebar = document.querySelector('[data-testid="sidebarColumn"]');
                    if (rightSidebar) rightSidebar.style.display = 'none';
                    
                    // Only hide overlays that are NOT login related
                    const overlays = document.querySelectorAll('div[style*="position: fixed"]');
                    overlays.forEach(el => {
                        // Don't hide if it contains login-related elements
                        if (!el.querySelector('[data-testid*="login"]') && 
                            !el.querySelector('input[name="password"]') &&
                            !el.textContent.includes('Sign in') &&
                            !el.textContent.includes('Log in')) {
                            const style = window.getComputedStyle(el);
                            if (parseInt(style.zIndex) > 100) {
                                el.style.display = 'none';
                            }
                        }
                    });
                }
            """)
        except Exception as e:
            print(f"   ⚠ Could not hide overlays: {e}")

    def check_user_timeline(self, page, username):
        """
        Check a user's timeline for new content.

        Args:
            page: Playwright page object
            username: X username to check
        """
        print(f"\n🔍 Checking @{username}...")

        try:
            # Navigate to user profile
            page.goto(f"https://x.com/{username}", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)  # Wait for dynamic content to load

            # Hide overlays and popups
            self.hide_overlays(page)
            time.sleep(1)

            # Scroll a bit to trigger lazy loading
            page.evaluate("window.scrollBy(0, 500)")
            time.sleep(2)

            # Find all articles (tweets) on the page
            articles = page.locator('article[data-testid="tweet"]').all()

            if len(articles) == 0:
                print("   ⚠ No tweets found - may need login or page structure changed")
                return

            print(f"   Found {len(articles)} items on timeline")

            new_captures = 0
            for article in articles[:10]:  # Check most recent 10 items
                try:
                    # Get the tweet link to extract ID
                    link_element = article.locator('a[href*="/status/"]').first
                    if link_element.count() == 0:
                        continue

                    tweet_url = link_element.get_attribute('href')
                    post_id = self.get_post_id_from_url(tweet_url)

                    if not post_id:
                        continue

                    # Check if already captured
                    if post_id in self.user_data[username]["captured_ids"]:
                        continue

                    # Determine content type
                    content_type = "post"  # Default

                    # Check if it's a repost (look for "Reposted" text)
                    if article.locator('text="Reposted"').count() > 0:
                        content_type = "repost"
                    # Check if it's a reply/comment
                    elif article.locator('[data-testid="reply"]').count() > 0 or article.locator('text="Replying to"').count() > 0:
                        content_type = "comment"

                    # Capture the screenshot first
                    timestamp = self.capture_screenshot(article, username, content_type, post_id)

                    if timestamp:
                        # Screenshot successful - now extract any images
                        num_images = self.extract_images_from_tweet(article, username, post_id, timestamp)

                        if num_images > 0:
                            print(f"  📷 Successfully extracted {num_images} full-resolution image(s)")

                        # Mark as captured
                        self.user_data[username]["captured_ids"].append(post_id)
                        new_captures += 1

                except Exception as e:
                    print(f"   ⚠ Error processing article: {e}")
                    continue

            # Save tracking data
            if new_captures > 0:
                self.save_tracking_data(username)
                print(f"   📸 Captured {new_captures} new item(s)")
            else:
                print(f"   ℹ No new content")

        except PlaywrightTimeout:
            print(f"   ✗ Timeout loading @{username}'s profile")
        except Exception as e:
            print(f"   ✗ Error checking @{username}: {e}")

    def save_session(self, context, session_file="twitter_session.json"):
        """Save browser session/cookies for reuse."""
        storage = context.storage_state()
        with open(session_file, 'w') as f:
            json.dump(storage, f)
        print(f"💾 Session saved to {session_file}")

    def run(self, twitter_username=None, twitter_password=None, headless=True,
            use_session=True, session_file="twitter_session.json"):
        """
        Main loop to monitor accounts.

        Args:
            twitter_username: X username/email for login (optional)
            twitter_password: X password for login (optional)
            headless: Whether to run browser in headless mode
            use_session: Whether to use saved session/cookies
            session_file: Path to session file
        """
        print("🚀 Starting Twitter/X Archiver")
        print(f"📁 Storage location: {self.base_path}")
        print(f"👥 Monitoring: {', '.join(['@' + u for u in self.target_usernames])}")
        print(f"⏱  Check interval: ~{self.check_interval_base}s ± {self.check_interval_variance}s")
        print(f"🖥️  Headless mode: {headless}")

        with sync_playwright() as p:
            # Launch browser with stealth settings
            browser = p.chromium.launch(
                headless=headless,
                args=[
                    '--disable-blink-features=AutomationControlled',  # Hide automation
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-background-timer-throttling',  # Prevent tab throttling
                    '--disable-backgrounding-occluded-windows',  # Keep running in background
                    '--disable-renderer-backgrounding',  # Don't throttle renderer
                ]
            )

            # Create context with realistic browser fingerprint
            context_options = {
                'viewport': {'width': 1920, 'height': 1080},
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'locale': 'en-US',
                'timezone_id': 'America/New_York',
                'permissions': ['geolocation'],
                'has_touch': False,
                'is_mobile': False,
                'device_scale_factor': 1,
            }

            # Try to load existing session
            session_path = Path(session_file)

            if use_session and session_path.exists():
                print(f"🔄 Loading saved session from {session_file}")
                with open(session_path, 'r') as f:
                    storage_state = json.load(f)
                context_options['storage_state'] = storage_state
            else:
                print("🆕 Starting fresh session")

            context = browser.new_context(**context_options)

            # Add script to hide webdriver property
            page = context.new_page()
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                
                // Add chrome object to make it look more real
                window.chrome = {
                    runtime: {}
                };
                
                // Mock plugins to avoid detection
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
                
                // Mock languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en']
                });
                
                // Prevent page from sleeping/idling
                let wakeLock = null;
                setInterval(() => {
                    // Keep the page active
                    document.dispatchEvent(new Event('mousemove'));
                }, 30000); // Every 30 seconds
            """)

            # Check if session is valid by trying to access X
            logged_in = False
            try:
                page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=15000)
                time.sleep(3)
                # Check if we're logged in
                if page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').count() > 0:
                    print("✓ Using existing session - already logged in!")
                    logged_in = True
                else:
                    print("⚠️  Session invalid or expired")
            except Exception as e:
                print(f"⚠️  Could not verify session: {e}")

            # If not logged in and credentials provided, try to log in
            if not logged_in:
                if twitter_username and twitter_password:
                    if self.login(page, twitter_username, twitter_password):
                        # Save the new session
                        self.save_session(context, session_file)
                        logged_in = True
                    time.sleep(3)
                else:
                    print("\n" + "="*60)
                    print("⚠️  NOT LOGGED IN - Manual Login Required")
                    print("="*60)
                    print("\n1. A browser window will stay open")
                    print("2. Please log in manually (Google login works!)")
                    print("3. Once logged in, press Enter here to continue...")
                    print("4. Your session will be saved for future runs\n")

                    # Open login page
                    page.goto("https://x.com/i/flow/login")

                    # Wait for user to log in manually
                    input("Press Enter after you've logged in...")

                    # Verify login worked - use a more forgiving navigation
                    try:
                        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=10000)
                    except Exception:
                        # Page might already be on home, or redirecting - that's fine
                        pass

                    time.sleep(3)

                    # Check if we're logged in by looking for account switcher
                    if page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').count() > 0:
                        print("✓ Manual login successful!")
                        self.save_session(context, session_file)
                        logged_in = True
                    else:
                        # Try checking current URL as backup
                        if "home" in page.url or "x.com" in page.url:
                            print("✓ Manual login appears successful!")
                            self.save_session(context, session_file)
                            logged_in = True
                        else:
                            print("✗ Login verification failed")

            if not logged_in:
                print("\n⚠️  Continuing without login - limited functionality expected\n")

            print("\nPress Ctrl+C to stop\n")

            try:
                iteration = 0
                while True:
                    # Randomly select a user to check
                    username = random.choice(self.target_usernames)

                    print(f"\n{'='*60}")
                    print(f"Iteration #{iteration + 1}")
                    print(f"Available users: {self.target_usernames}")
                    print(f"Selected user: @{username}")
                    print(f"{'='*60}")

                    # Check the selected user
                    self.check_user_timeline(page, username)

                    # Calculate next check time with randomization
                    wait_time = self.check_interval_base + random.randint(
                        -self.check_interval_variance,
                        self.check_interval_variance
                    )

                    print(f"\n⏳ Next check in {wait_time}s...")
                    iteration += 1
                    time.sleep(wait_time)

            except KeyboardInterrupt:
                print("\n\n🛑 Stopping archiver...")
            finally:
                browser.close()


def load_config(config_file="config.json"):
    """Load configuration from JSON file."""
    config_path = Path(config_file)

    if not config_path.exists():
        print(f"❌ Error: Configuration file '{config_file}' not found!")
        print(f"   Please create a config.json file with your settings.")
        print(f"   See the README or sample config for the required format.")
        return None

    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ Error: Invalid JSON in config file: {e}")
        return None
    except Exception as e:
        print(f"❌ Error loading config: {e}")
        return None


# Main entry point
if __name__ == "__main__":
    # Load configuration
    config = load_config("config.json")

    if config is None:
        exit(1)

    # Create and run archiver
    archiver = TwitterArchiver(
        base_storage_path=config["storage_path"],
        target_usernames=config["target_usernames"],
        check_interval_base=config.get("check_interval_seconds", 120)
    )

    # Run archiver
    archiver.run(
        twitter_username=config.get("twitter_login", {}).get("username"),
        twitter_password=config.get("twitter_login", {}).get("password"),
        headless=config.get("headless", True),
        use_session=config.get("use_saved_session", True),
        session_file=config.get("session_file", "twitter_session.json")
    )
