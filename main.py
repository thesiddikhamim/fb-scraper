import asyncio
import sys

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from feedgen.feed import FeedGenerator
from datetime import datetime, timezone, timedelta
import json
import os
import random
import re

class FacebookScraper:
    def __init__(self, page_url, page_name):
        self.page_url = page_url
        self.page_name = page_name
        self.posts = []
    
    async def handle_blocking_elements(self, page):
        """Aggressively remove blocking elements (login popups, sticky headers/footers) via DOM"""
        try:
            # 1. Click "Close" buttons if present (standard approach)
            selectors = [
                'div[aria-label="Close"]', 
                'div[role="button"]:has-text("Not Now")',
                'div[role="button"]:has-text("Decline")',
                'div[aria-label="Decline optional cookies"]',
                'div[role="button"]:has-text("Allow all cookies")'
            ]
            for selector in selectors:
                try:
                    if await page.query_selector(selector):
                        await page.click(selector, timeout=1000)
                except: pass

            # 2. Aggressive DOM Removal: Remove Full-Screen Login Overlays
            # Facebook often uses role="dialog" or "banner" for these
            await page.evaluate("""() => {
                const blockers = document.querySelectorAll('div[role="dialog"], div[role="banner"], div[id^="mount_0_0_"] > div > div > div > div > div[style*="position: fixed"]');
                blockers.forEach(el => {
                    if (el.innerText.includes("Log In") || el.innerText.includes("Join Facebook") || el.innerText.includes("See more on Facebook")) {
                        el.remove();
                        console.log("Removed blocking login overlay");
                    }
                });
                
                // Remove the "Login to continue" bottom sticky bar
                const stickyBottom = document.querySelectorAll('div[data-testid="bottom_sheet"]');
                stickyBottom.forEach(el => el.remove());
                
                // Force enable scrolling on body/html in case it was disabled by a modal
                document.body.style.overflow = 'auto';
                document.documentElement.style.overflow = 'auto';
            }""")
            
            # Press Escape as a safe fallback
            await page.keyboard.press('Escape')
        except Exception as e:
            print(f"  Error handling blocking elements: {e}")

    async def scrape(self, max_posts=10):
        """Main scraping method with improved error handling and stealth"""
        print(f"Starting scrape for {self.page_name}...")
        try:
            async with async_playwright() as p:
                # Launch with better arguments for stability and stealth
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-accelerated-2d-canvas',
                        '--no-first-run',
                        '--no-zygote',
                        '--disable-gpu',
                        '--disable-blink-features=AutomationControlled',
                        '--disable-notifications',
                        '--disable-popup-blocking',
                        '--start-maximized',
                        '--disable-background-networking',
                        '--disable-client-side-phishing-detection',
                        '--disable-default-apps',
                        '--disable-hang-monitor',
                        '--disable-prompt-on-repost',
                        '--disable-sync',
                        '--disable-web-resources',
                        '--enable-automation',
                        '--no-default-browser-check',
                        '--no-pings'
                    ]
                )
                
                # Use a realistic context
                context = await browser.new_context(
                    viewport={'width': 1366, 'height': 768},  # Standard desktop resolution
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    locale='en-US',
                    timezone_id='UTC',
                    permissions=['geolocation'],
                    geolocation={'latitude': 37.7749, 'longitude': -122.4194},
                    device_scale_factor=1,
                    is_mobile=False,
                    has_touch=False
                )
                
                # Apply stealth scripts to mask WebDriver
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                """)
                
                page = await context.new_page()
                
                # Navigate with better timeout handling
                try:
                    # Use 'networkidle' for CI/Ubuntu to ensure all resources load
                    await page.goto(self.page_url, timeout=90000, wait_until='networkidle')
                    await page.wait_for_timeout(random.randint(8000, 12000))  # Much longer initial wait for Ubuntu
                    
                    # Close login popup (wait a bit first)
                    await self.handle_blocking_elements(page)
                        
                except PlaywrightTimeout:
                    print(f"Timeout loading {self.page_url}")
                    await browser.close()
                    return []
                
                # Scroll loop to load dynamic content
                # Use incremental scrolling which is more human-like and triggers lazy loading better
                last_height = await page.evaluate("document.body.scrollHeight")
                no_change_count = 0
                for scroll_iteration in range(20):  # INCREASED: More scrolls for Ubuntu CI environment
                    # Scroll down in steps - MORE AGGRESSIVE
                    for _ in range(5):  # INCREASED from 3
                        await page.mouse.wheel(0, 1200)  # INCREASED from 800
                        await page.wait_for_timeout(random.randint(2000, 3500))  # INCREASED wait times
                    
                    # Force scroll via JS as fallback (in case mouse events are blocked)
                    await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight)")
                    
                    # Wait for network to stabilize - INCREASED significantly for Ubuntu
                    await page.wait_for_timeout(random.randint(5000, 8000))  # INCREASED wait time
                    
                    print(f"  Scroll iteration {scroll_iteration + 1}/20 completed")
                    
                    # Close login popup if it appears again
                    await self.handle_blocking_elements(page)
                    
                    # Check if we've reached the bottom
                    new_height = await page.evaluate("document.body.scrollHeight")
                    if new_height == last_height:
                        no_change_count += 1
                        print(f"  Page height unchanged ({no_change_count}/3)")
                        # Break after 3 consecutive scrolls with no new height
                        if no_change_count >= 3:
                            print("  Reached page bottom, stopping scroll.")
                            break
                    else:
                        no_change_count = 0
                    last_height = new_height
                
                # Wait for articles to appear after all scrolling
                try:
                    # Try with multiple timeouts in case network is slow
                    for attempt in range(3):
                        try:
                            await page.wait_for_selector('div[role="article"]', timeout=15000)
                            break
                        except PlaywrightTimeout:
                            if attempt < 2:
                                print(f"  Attempt {attempt + 1} to find articles failed, retrying...")
                                await page.wait_for_timeout(5000)
                            else:
                                raise
                    
                    article_count = await page.evaluate('document.querySelectorAll("div[role=\\"article\\"]").length')
                    print(f"  ✓ {article_count} articles loaded successfully")
                except:
                    print("  ⚠ Warning: No articles selector found after scrolling")
                
                # Extract posts
                self.posts = await self._extract_posts(page, max_posts)
                
                # Debugging: If few posts are found, take a screenshot
                if len(self.posts) < 3:
                    print(f"⚠ Low post count ({len(self.posts)}). Saving debug artifacts...")
                    await page.screenshot(path="debug_screenshot.png", full_page=True)
                    content = await page.content()
                    with open("debug_html.html", "w", encoding="utf-8") as f:
                        f.write(content)
                
                await browser.close()
                
        except Exception as e:
            print(f"Critical scraping error: {e}")
            # Try to capture state if possible
            try:
                if 'page' in locals():
                    await page.screenshot(path="debug_error.png")
            except:
                pass
        
        return self.posts
    
    async def _extract_posts(self, page, max_posts):
        """Extract post data, carefully excluding comments"""
        posts = []
        
        # 'div[role="article"]' is the standard container for a post card
        articles = await page.query_selector_all('div[role="article"]')
        
        print(f"Found {len(articles)} potential posts. Processing top {max_posts}...")
        
        for i, article in enumerate(articles[:max_posts]):
            try:
                # 0. Filter out Comments (which also have role="article")
                # Comments usually have a "Reply" button or specific aria-labels
                if await article.query_selector('div[aria-label="Reply"], span[innerText="Reply"], div[role="button"]:has-text("Reply")'):
                    print(f"⚠ Skipping likely comment item {i}")
                    continue
                
                # Double check: Posts usually have a "Share" button, comments usually don't (or it's hidden/different)
                # But safer to check for explicit comment indicators
                aria_label = await article.get_attribute("aria-label")
                if aria_label and "comment" in aria_label.lower():
                    continue

                # 1. Extract Post Text
                post_text = ""

                # Expand "See more" if present to get full caption
                try:
                    # More aggressive JS-based expansion
                    await article.evaluate("""(article) => {
                        const expansionPhrases = ["See more", "আরও দেখুন", "See More", "আরও", "আরও দেখুন..."];
                        
                        // Find all elements that might be the expansion trigger
                        const elements = Array.from(article.querySelectorAll('div[role="button"], span[role="button"], a[role="button"], div, span, a'));
                        
                        let clicked = false;
                        elements.forEach(el => {
                            if (el.children.length > 3) return; // Skip containers, look for leaf-ish elements
                            
                            const text = (el.innerText || el.textContent || "").trim();
                            if (expansionPhrases.includes(text) || (text.includes("...") && expansionPhrases.some(p => text.includes(p)))) {
                                // Use dispatchEvent for more reliable click triggering
                                ["mousedown", "mouseup", "click"].forEach(type => {
                                    el.dispatchEvent(new MouseEvent(type, {
                                        view: window,
                                        bubbles: true,
                                        cancelable: true,
                                        buttons: 1
                                    }));
                                });
                                clicked = true;
                            }
                        });
                        return clicked;
                    }""")
                    
                    # Wait for expansion to complete
                    await page.wait_for_timeout(2000) 
                except Exception as e:
                    print(f"  ⚠ Error expanding 'See more': {e}")

                # Improved message container selection
                # Facebook changes these frequently, so we check multiple common patterns
                selectors = [
                    'div[data-ad-preview="message"]',
                    'div[data-ad-comet-preview="message"]',
                    'div[dir="auto"].xdj266r', # Common Comet message classes
                    'div.x11i5rnm.xat24cr.x1mh8g0r.x1vvkbs.xtl81vo', # Specific text container
                    'div[id^="mount_0_0_"] div[dir="auto"]'
                ]
                
                msg_element = None
                for selector in selectors:
                    msg_element = await article.query_selector(selector)
                    if msg_element:
                        # Ensure it's not a tiny container or a footer
                        text = await msg_element.inner_text()
                        if len(text.strip()) > 10:
                            break
                        else:
                            msg_element = None
                
                if msg_element:
                    post_text = await msg_element.inner_text()
                else:
                    # Fallback: Get full text and aggressively clean it
                    full_text = await article.inner_text()
                    
                    # If we find "See more" in the fallback text, it's a sign expansion failed
                    if "See more" in full_text or "আরও দেখুন" in full_text:
                        print("  ⚠ 'See more' still visible in text after expansion attempt.")

                    # 1. Locate the end of the content (start of footer)
                    # Common footer markers in English and Bengali
                    footer_markers = [
                        "All reactions:", "View more comments", "Write a comment", 
                        "Most relevant", "View all", "replies", "shares", "comments"
                    ]
                    
                    # Find the earliest occurrence of any footer marker
                    cutoff_index = len(full_text)
                    for marker in footer_markers:
                        idx = full_text.find(marker)
                        if idx != -1 and idx < cutoff_index:
                            cutoff_index = idx
                    
                    # Truncate text
                    post_text = full_text[:cutoff_index]
                    
                    # 2. Clean up lines
                    lines = post_text.split('\n')
                    clean_lines = []
                    
                    skip_phrases = {
                        "Like", "Comment", "Share", "Send", "Write a comment...", 
                        "Shares", "Comments", "Reply", "Follow", "Join"
                    }
                    
                    for line in lines:
                        line_stripped = line.strip()
                        if not line_stripped: continue
                        
                        # Filter UI noise
                        if line_stripped in skip_phrases: continue
                        
                        # Filter timestamp-like lines (short lines with numbers/time chars)
                        # e.g. "1d", "13h", "2 hrs"
                        if len(line_stripped) < 5 and any(c.isdigit() for c in line_stripped):
                            continue
                            
                        clean_lines.append(line_stripped)
                    
                    post_text = "\n".join(clean_lines)

                # Post-processing
                post_text = ' '.join(post_text.split()) # normalize whitespace
                
                # Filter out the Author Name if it appears at the start (simple heuristic)
                if post_text.lower().startswith(self.page_name.lower()):
                    post_text = post_text[len(self.page_name):].strip()

                if len(post_text) < 5:  # Skip empty/noise posts
                    continue

                # 2. Extract Link
                link = None
                link_elem = await article.query_selector('a[href*="/posts/"], a[href*="/photos/"], a[href*="/videos/"]')
                
                if not link_elem:
                     all_links = await article.query_selector_all('a[href*="facebook.com"]')
                     for a in all_links:
                         href = await a.get_attribute('href')
                         if href and ('/posts/' in href or '/permalink.php' in href):
                             link_elem = a
                             break
                
                if link_elem:
                    href = await link_elem.get_attribute('href')
                    if href:
                        if href.startswith('/'):
                            href = f"https://www.facebook.com{href}"
                        link = href.split('?')[0]

                # 3. Extract Image & Video
                image_url = None
                video_url = None
                try:
                    # Look for videos first
                    video_element = await article.query_selector('video')
                    if video_element:
                        # 1. Try to find a unique Video ID for THIS post
                        video_id = await article.evaluate("""(article) => {
                            // Try multiple ways to find the video ID linked to this article
                            const links = Array.from(article.querySelectorAll('a[href*="/videos/"], a[href*="/watch/"]'));
                            for (const link of links) {
                                const match = link.href.match(/\\/videos\\/(\\d+)/) || link.href.match(/v=(\\d+)/);
                                if (match) return match[1];
                            }
                            
                            // Check for data attributes
                            const videoContainer = article.querySelector('div[data-video-id]');
                            if (videoContainer) return videoContainer.getAttribute('data-video-id');
                            
                            return null;
                        }""")
                        
                        if video_id:
                            # 2. Extract static URLs from the script tags linked to THIS video_id
                            video_url = await page.evaluate(f"""(vId) => {{
                                const scripts = Array.from(document.querySelectorAll('script'));
                                for (const script of scripts) {{
                                    const content = script.textContent;
                                    if (content.includes(vId) && (content.includes('browser_native_sd_url') || content.includes('browser_native_hd_url'))) {{
                                        // Find the block containing both the vId and the URL
                                        // We look for the ID followed by the URL metadata
                                        const idIdx = content.indexOf(vId);
                                        const sub = content.substring(idIdx - 1000, idIdx + 10000);
                                        
                                        const hdMatch = sub.match(/"browser_native_hd_url":"([^"]+)"/);
                                        const sdMatch = sub.match(/"browser_native_sd_url":"([^"]+)"/);
                                        
                                        const url = hdMatch ? hdMatch[1] : (sdMatch ? sdMatch[1] : null);
                                        if (url) return url.replace(/\\\\/g, '');
                                    }}
                                }}
                                return null;
                            }}""", video_id)
                        
                        # 3. Fallback to direct src or source tags (likely blob, but worth a try)
                        if not video_url:
                            video_url = await video_element.get_attribute('src')
                            if not video_url or video_url.startswith('blob:'):
                                video_url = await video_element.evaluate("el => el.currentSrc")
                                
                            if not video_url or video_url.startswith('blob:'):
                                source = await video_element.query_selector('source')
                                if source:
                                    video_url = await source.get_attribute('src')
                    
                    # Look for images
                    images = await article.query_selector_all('img')
                    for img in images:
                        src = await img.get_attribute('src')
                        if not src: continue
                        
                        # Skip UI/noise images
                        if any(x in src for x in ['emoji.php', 'rsrc.php', 'static.xx', 'p50x50', 's100x100']):
                            continue
                        if src.startswith('data:image/svg'):
                            continue
                            
                        image_url = src
                        break
                        
                except Exception as e:
                    print(f"  ⚠ Error extracting media: {e}")

                # 4. Extract Timestamp
                # Facebook shows relative times like "4d", "13h", "2 mins" etc.
                # We'll try to parse these from the article text
                pub_date = None
                try:
                    # Get all text from the article
                    article_text = await article.inner_text()
                    
                    # Look for relative time patterns
                    # Common patterns: "4d", "13h", "2 mins", "1 hr", "Just now"
                    time_patterns = [
                        (r'(\d+)\s*d(?:ays?)?\b', 'days'),
                        (r'(\d+)\s*h(?:rs?|ours?)?\b', 'hours'),
                        (r'(\d+)\s*m(?:ins?|inutes?)?\b', 'minutes'),
                        (r'(\d+)\s*s(?:ecs?|econds?)?\b', 'seconds'),
                        (r'Just now', 'now')
                    ]
                    
                    current_time = datetime.now(timezone.utc)
                    
                    for pattern, unit in time_patterns:
                        match = re.search(pattern, article_text, re.IGNORECASE)
                        if match:
                            if unit == 'now':
                                pub_date = current_time
                            else:
                                value = int(match.group(1))
                                if unit == 'days':
                                    pub_date = current_time - timedelta(days=value)
                                elif unit == 'hours':
                                    pub_date = current_time - timedelta(hours=value)
                                elif unit == 'minutes':
                                    pub_date = current_time - timedelta(minutes=value)
                                elif unit == 'seconds':
                                    pub_date = current_time - timedelta(seconds=value)
                            
                            print(f"  Timestamp: {pub_date.isoformat()} (parsed from '{match.group(0)}')")
                            break
                    
                    # Fallback: Use post order as proxy (newer posts first)
                    # Assign decreasing timestamps based on position
                    if not pub_date:
                        # Assume posts are roughly 1 day apart
                        pub_date = current_time - timedelta(days=i)
                        print(f"  Timestamp: {pub_date.isoformat()} (estimated from position)")
                            
                except Exception as e:
                    # Ultimate fallback
                    pub_date = datetime.now(timezone.utc) - timedelta(days=i)
                    print(f"  ⚠ Could not extract timestamp: {e}, using position-based estimate")

                # 5. Create Post Object
                current_time = datetime.now(timezone.utc)
                # Use a cleaner title
                title = post_text[:80] + '...' if len(post_text) > 80 else post_text
                
                post_obj = {
                    'title': title,
                    'description': post_text,
                    'link': link or self.page_url,
                    'guid': link or f"{self.page_url}#{i}_{int(current_time.timestamp())}",
                    'pubDate': pub_date,
                    'image': image_url,
                    'video': video_url
                }
                
                posts.append(post_obj)
                print(f"✓ Post {len(posts)}: {title}")
                if image_url:
                    print(f"  Image: {image_url[:50]}...")
                
            except Exception as e:
                print(f"⚠ Error extracting post {i}: {e}")
                continue
        
        return posts
    
    def save_cache(self, filename='cache.json'):
        """Save posts to cache"""
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump([{**p, 'pubDate': p['pubDate'].isoformat()} for p in self.posts], f, indent=2)
            print(f"Cache saved to {filename}")
        except Exception as e:
            print(f"Error saving cache: {e}")
    
    def load_cache(self, filename='cache.json'):
        """Load posts from cache"""
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for p in data:
                        p['pubDate'] = datetime.fromisoformat(p['pubDate'])
                    print(f"Loaded {len(data)} posts from cache")
                    return data
            except Exception as e:
                print(f"Error loading cache: {e}")
        return []

def generate_rss(posts, page_name, page_url, output='feed.xml'):
    """Generate RSS feed from posts"""
    try:
        # Sort posts by publication date (newest first)
        sorted_posts = sorted(posts, key=lambda p: p['pubDate'], reverse=True)
        
        fg = FeedGenerator()
        fg.id(page_url)
        fg.title(page_name)
        fg.link(href=page_url, rel='alternate')
        fg.link(href=page_url, rel='self')
        fg.description(f'Unofficial RSS feed for {page_name}')
        fg.language('en')
        fg.generator('Facebook Scraper RSS Generator')
        fg.lastBuildDate(datetime.now(timezone.utc))
        
        for post in sorted_posts:
            fe = fg.add_entry()
            fe.id(post['guid'])
            fe.title(post['title'])
            fe.description(post['description'])
            fe.link(href=post['link'])
            fe.guid(post['guid'], permalink=False)
            fe.published(post['pubDate'])
            
            if post.get('video'):
                # Prioritize video as enclosure if it exists
                v_url = post['video']
                mime_type = 'video/mp4'
                if '.m3u8' in v_url:
                    mime_type = 'application/x-mpegURL'
                
                fe.enclosure(url=v_url, type=mime_type, length='0')
                
                # Add a video player or link to description as well
                current_desc = fe.description()
                fe.description(f'{current_desc}<br/><br/><video controls width="100%" poster="{post.get("image", "")}"><source src="{v_url}" type="{mime_type}">Your browser does not support the video tag.</video>')
            elif post.get('image'):
                fe.enclosure(url=post['image'], type='image/jpeg', length='0')
                current_desc = fe.description()
                fe.description(f'{current_desc}<br/><br/><img src="{post["image"]}" style="max-width:100%"/>')
            
        fg.rss_file(output, pretty=True)
        print(f"\n✓ RSS feed generated: {output}")
        print(f"  Entries: {len(sorted_posts)}")
    except Exception as e:
        print(f"Error generating RSS: {e}")

async def main():
    # Load accounts from JSON file
    # Default to accounts.json, but allow overriding via command line
    accounts_file = 'accounts.json'
    if len(sys.argv) > 1:
        accounts_file = sys.argv[1]
    
    try:
        with open(accounts_file, 'r', encoding='utf-8') as f:
            ACCOUNTS = json.load(f)
    except FileNotFoundError:
        print(f"Error: {accounts_file} not found.")
        return
    except json.JSONDecodeError as e:
        print(f"Error parsing {accounts_file}: {e}")
        return
    
    # Create directories if they don't exist
    cache_dir = "cache"
    feeds_dir = "feeds"
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(feeds_dir, exist_ok=True)
    
    for account in ACCOUNTS:
        try:
            print(f"\n--- Processing {account['name']} ---")
            scraper = FacebookScraper(account['url'], account['name'])
            
            cache_file = os.path.join(cache_dir, f"{account['filename']}_cache.json")
            rss_file = os.path.join(feeds_dir, f"{account['filename']}.xml")
            
            # Try fetching fresh data - INCREASED to 25 to get more from longer scrolling
            posts = await scraper.scrape(max_posts=25)
            
            if posts:
                print(f"✓ Successfully scraped {len(posts)} posts for {account['name']}")
                scraper.save_cache(filename=cache_file)
                generate_rss(posts, account['name'], account['url'], output=rss_file)
            else:
                print(f"✗ Scraping failed or yielded no results for {account['name']}. Checking cache...")
                posts = scraper.load_cache(filename=cache_file)
                if posts:
                    generate_rss(posts, account['name'], account['url'], output=rss_file)
                    print(f"Generated RSS from cached data for {account['name']}.")
                else:
                    print(f"No data available for {account['name']} (scrape failed and no cache).")
                    
        except Exception as e:
            print(f"Error processing {account['name']}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
