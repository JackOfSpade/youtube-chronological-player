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
    // Not loaded yet — register for when it does
    const _prev = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = function () {
        state.isPlayerReady = true;
        if (typeof _prev === 'function') _prev();
        if (state.currentPlayingId && !state.ytPlayer) {
            playVideo(state.currentPlayingId);
        }
    };
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
    console.warn('YouTube Player Error:', event.data, 'Skipping video...');
    _handleEnded(state.currentPlayingId);
}

function _handleEnded(videoId) {
    fetch(`/api/watched/${encodeURIComponent(videoId)}`, { method: 'POST' }).catch(err => {
        console.error('Failed to mark video as watched', err);
    });

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
        navigator.sendBeacon(`/api/watched/${encodeURIComponent(state.currentPlayingId)}`);
    }
});
