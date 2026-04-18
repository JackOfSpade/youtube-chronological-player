/**
 * player.js — YouTube IFrame player, video info panel, and autoplay logic.
 */

import { DOM, state, findOldestUnwatchedIndex, formatDate } from './state.js';
import { renderQueue, updateResumeButton } from './queue.js';
import { loadComments } from './comments.js';

// ── YouTube IFrame API readiness ───────────────────────────────────────────
// ES modules execute deferred, so the API may have already initialized.

if (window.YT && window.YT.Player) {
    state.isPlayerReady = true;
} else {
    const _prev = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = function () {
        state.isPlayerReady = true;
        if (typeof _prev === 'function') _prev();
        if (state.currentPlayingId && !state.ytPlayer) {
            playVideo(state.currentPlayingId);
        }
    };

    // Global singleton timer to prevent multiple watchdogs if this module is reloaded/imported.
    if (!window._yt_watchdog_active) {
        window._yt_watchdog_active = true;
        setTimeout(() => {
            if (!state.isPlayerReady && !state.ytPlayer) {
                console.warn('YouTube API loading timeout.');
                if (!window.YT || !window.YT.Player || window._yt_script_failed) {
                    import('./state.js').then(({ showNotification }) => {
                        showNotification('YouTube API is taking long to load. Refresh if player doesn\'t appear.');
                    });
                    _showPlayerError('YouTube API Timeout', 'The player script took too long to load or was blocked by an extension. Please refresh.');
                }
            }
        }, 8000);
    }
}

function _showPlayerError(title, message) {
    const placeholder = document.getElementById('player-placeholder');
    if (placeholder) {
        placeholder.innerHTML = `
            <div class="player-empty-state" style="color: #ff6b6b;">
                <span class="icon" style="background: rgba(255,107,107,0.1); color: #ff6b6b;">✕</span>
                <h3>${title}</h3>
                <p style="margin-top: 10px; opacity: 0.8;">${message}</p>
            </div>
        `;
    }
}

// ── Public API ─────────────────────────────────────────────────────────────

export function playVideo(videoId, globalIndex) {
    // Mark outgoing video as watched
    if (state.currentPlayingId && state.currentPlayingId !== videoId) {
        if (!state.watchHistory.watched_video_ids.has(state.currentPlayingId)) {
            navigator.sendBeacon(`/api/watched/${encodeURIComponent(state.currentPlayingId)}`);
            state.watchHistory.watched_video_ids.add(state.currentPlayingId);
            state.watchHistory.last_watched_video_id = state.currentPlayingId;
        }
    }

    state.currentPlayingId = videoId;
    state.commentsToken = null;

    // Navigate to the correct page if needed
    if (globalIndex !== undefined && globalIndex !== -1) {
        const page = Math.floor(globalIndex / state.pageSize) + 1;
        if (page !== state.currentPage) state.currentPage = page;
    }

    renderQueue();
    _updateInfoPanel(videoId);

    if (!state.ytPlayer) {
        if (state.isPlayerReady) {
            state.ytPlayer = new YT.Player('yt-player', {
                height: '100%',
                width: '100%',
                videoId,
                playerVars: { autoplay: 1, controls: 1, rel: 0 },
                events: { 
                    onReady: (event) => {
                        if (state.currentPlayingId && state.currentPlayingId !== videoId) {
                            event.target.loadVideoById(state.currentPlayingId);
                        }
                    },
                    onStateChange: _onStateChange,
                    onError: _onError
                },
            });
            DOM.ytPlayer.style.zIndex = '10';
        } else {
            console.warn('YouTube API not ready yet.');
        }
    } else {
        if (typeof state.ytPlayer.loadVideoById === 'function') {
            state.ytPlayer.loadVideoById(videoId);
        }
    }
}

export function resumePlayback() {
    const idx = findOldestUnwatchedIndex();
    if (idx !== -1) {
        playVideo(state.queue[idx].id, idx);
    } else if (state.queue.length > 0) {
        const last = state.queue.length - 1;
        playVideo(state.queue[last].id, last);
    }
}

// ── Private helpers ────────────────────────────────────────────────────────

function _updateInfoPanel(videoId) {
    const idx = state.queueIndex.get(videoId);
    if (idx === undefined) return;
    const video = state.queue[idx];

    DOM.videoInfo.classList.remove('hidden');
    DOM.videoTitle.textContent = video.title;
    DOM.videoChannel.textContent = video.channelTitle;
    DOM.videoDate.textContent = formatDate(video.publishedAt);

    if (DOM.commentBtn) {
        DOM.commentBtn.href = `https://www.youtube.com/watch?v=${encodeURIComponent(videoId)}#comments`;
    }

    loadComments(videoId);
}

function _onStateChange(event) {
    // YT.PlayerState.ENDED === 0
    if (event.data === 0) _handleEnded(state.currentPlayingId);
}

function _onError(event) {
    const errorCode = event.data;
    // 100: Not found/removed, 101/150: Embed blocked
    const fatalErrors = [100, 101, 150];
    
    if (fatalErrors.includes(errorCode)) {
        console.warn('YouTube Player Fatal Error:', errorCode, 'Skipping video...');
        _handleEnded(state.currentPlayingId);
    } else {
        console.warn('YouTube Player Transient Error:', errorCode);
        _showPlayerError('Playback Error', 'Try refreshing or selecting another video.');
    }
}

function _handleEnded(videoId) {
    navigator.sendBeacon(`/api/watched/${encodeURIComponent(videoId)}`);

    state.watchHistory.watched_video_ids.add(videoId);
    state.watchHistory.last_watched_video_id = videoId;

    const next = findOldestUnwatchedIndex();
    if (next !== -1) {
        playVideo(state.queue[next].id, next);
    } else {
        renderQueue();
        updateResumeButton();
    }
}

// ── Persist on tab close ───────────────────────────────────────────────────

window.addEventListener('beforeunload', () => {
    if (state.currentPlayingId && !state.watchHistory.watched_video_ids.has(state.currentPlayingId)) {
        // Only mark as watched on exit if we've watched for at least 15 seconds
        let playedTime = 0;
        try {
            if (state.ytPlayer && typeof state.ytPlayer.getCurrentTime === 'function') {
                playedTime = state.ytPlayer.getCurrentTime();
            }
        } catch (e) {}

        if (playedTime > 15) {
            navigator.sendBeacon(`/api/watched/${encodeURIComponent(state.currentPlayingId)}`);
        }
    }
});
