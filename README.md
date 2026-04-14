# YouTube Chronological Player

Welcome to the **YouTube Chronological Player**! This application is a custom, highly-optimized YouTube client designed to put you back in control of your subscriptions. 

Instead of relying on algorithmic recommendations that push engagement-bait, this player offers a strict, time-based queue of uploads from the specific channels you care about. To make catching up on the news painless, it includes a powerful built-in **AI Deduplication Filter** that groups redundant news stories and automatically selects the most objective, highest-quality reporting for you to watch.

---

## 🌟 Key Features

### 1. True Chronological Feed
No algorithms, no "suggested videos," and no endless scrolling traps. Your feed strictly shows the latest uploads from your curated list of channels, ordered from newest to oldest.

### 2. AI-Powered Deduplication
Tired of seeing 5 different news channels upload videos covering the exact same story? 
Powered by the **Gemini 2.5 Pro** model, our Sync engine actually reads the video transcripts and leverages live Google Web Searches to ensure your feed is clean:
- **Narration Priority:** Automatically deprioritizes raw, unspoken background noise in favor of videos with structured commentary or narration.
- **Bias Filtering:** When multiple narrated videos cover the exact same story, the AI researches the channel backgrounds and retains the video from the most objectively neutral/unbiased source.

### 3. Smart Watch History
The app remembers what you've watched! Videos are automatically marked as watched when you move on to the next one. If you close your browser midway through catching up, just launch the app and click the **Resume** button to pick up right on the oldest unwatched video in your queue.

### 4. Interactive Viewing Experience
- **In-App Player:** Watch securely right from the dashboard.
- **Deep Comments Integration:** Read top-tier comment threads and deeply-nested replies without ever opening the bloated YouTube interface. 

---

## 🚀 Typical Workflow

### Initial Setup
1. **API Keys:** Add your YouTube Data API v3 key to the `config.yaml` and your `GEMINI_API_KEY` to the `.env` file at the root of the project.
2. **Start the App:** Launch the app using Python or run `start.command` (if on macOS). 

### Curating your Feed
1. Open the UI and click the **⚙️ Channels** button.
2. Search for your favorite creators or news channels using the fully functional autocomplete search bar.
3. Add the channels to your tracking list and click **Save & Sync**.

### Catching Up
1. Click the **Sync** button at any time. The engine will deploy a multi-threaded parallel fetch to efficiently scrape all new uploads from your channels since your exact last watch window.
2. Wait a brief few seconds as the AI scrubs the transcripts and eliminates repetitive news stories.
3. Kick back and click the video thumbnail to drop into the player. Catch up sequentially in a distraction-free environment!

---

## 🧠 Under the Hood
This application is beautifully built using an ultra-lightweight architecture:
- **Backend:** Pure Python 3 with `Flask`. Components are decoupled (`youtube_api.py`, `sync_service.py`, `ai_filter.py`, `config_manager.py`, `storage_manager.py`) ensuring threading safety, atomic JSON cache saves, and highly robust error handling.
- **Frontend:** Vanilla JS (`ES6 Modules`) communicating with the backend over live Server-Sent Events (SSE) so you can watch sync progressions in real-time. Everything is snappy and responsive.
