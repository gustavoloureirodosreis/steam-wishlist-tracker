# Steam Wishlist Tracker

A daily GitHub Actions job that watches one Steam wishlist (`76561198126128927`) and publishes a page to GitHub Pages.

Every day at 08:30 São Paulo time (`30 11 * * *` UTC), `tracker.py`:

- fetches the wishlist and sorts it by priority ascending (the top 20 are the first 20);
- pulls store details, the last 10 community announcements, and, for the top 20, the **Recent Reviews** score scraped from the store page;
- compares everything with `state.json` and writes the day's changes to `today.json`:
  - `controller_alerts`: a game that had no controller support now has it (sent once, then the game stops being watched)
  - `release_date_changes`: a game without a firm date changed its date text (once per change)
  - `new_announcements`: announcements posted since the last run
  - `review_changes`: Recent Reviews % or label differs from the previous run
  - `wishlist_added` / `wishlist_removed`
- renders `docs/index.html`, then commits `state.json`, `today.json` and `docs/`.

The first run only records a baseline and reports nothing.

Run by hand: **Actions → Daily wishlist check → Run workflow**, or locally with `python3 tracker.py` (standard library only, Python 3.9+).
