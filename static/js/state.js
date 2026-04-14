/**
 * state.js — Shared application state, DOM references, and utility functions.
 *
 * Every other module imports from here.  State is mutable (module-level lets)
 * and accessed via exported getters/setters so that future refactors can add
 * reactivity without touching call-sites.
 */

// ── DOM helper ─────────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

// ── DOM references (cached once on import) ─────────────────────────────────

export const DOM = Object.freeze({
    queueList:          $('queue-list'),
    resumeBtn:          $('resume-btn'),
    syncBtn:            $('sync-btn'),
    settingsBtn:        $('settings-btn'),
    notification:       $('notification-banner'),
    prevPageBtn:        $('prev-page-btn'),
    nextPageBtn:        $('next-page-btn'),
    pageIndicator:      $('page-indicator'),
    settingsModal:      $('settings-modal'),
    closeModalBtn:      $('close-modal-btn'),
    channelList:        $('channel-list-container'),
    channelInput:       $('new-channel-id'),
    addChannelBtn:      $('add-channel-btn'),
    searchDropdown:     $('search-dropdown'),
    saveChannelsBtn:    $('save-channels-btn'),
    ytPlayer:           $('yt-player'),
    videoInfo:          $('current-video-info'),
    videoTitle:         $('current-title'),
    videoChannel:       $('current-channel'),
    videoDate:          $('current-date'),
    commentBtn:         $('comment-on-youtube-btn'),
    commentsSection:    $('comments-section'),
    commentsContainer:  $('comments-container'),
});

// ── Application state ──────────────────────────────────────────────────────

export const state = {
    /** @type {YT.Player|null} */
    ytPlayer: null,
    isPlayerReady: false,

    /** @type {Array<Object>}  Sorted newest→oldest */
    queue: [],

    /** @type {{watched_video_ids: Set<string>, last_watched_video_id: string|null}} */
    watchHistory: { watched_video_ids: new Set(), last_watched_video_id: null },

    /** @type {string|null} */
    currentPlayingId: null,

    currentPage: 1,
    pageSize: 10,

    /** videoId → index in queue (rebuilt on mutation) */
    queueIndex: new Map(),

    /** @type {string|null}  Next page token for infinite-scroll comments */
    commentsToken: null,
    isFetchingComments: false,

    /** @type {Array<{id: string, name: string}>} */
    settingsChannels: [],
    searchTimeout: null,
};

// ── Queue index helpers ────────────────────────────────────────────────────

export function rebuildQueueIndex() {
    state.queueIndex.clear();
    for (let i = 0; i < state.queue.length; i++) {
        state.queueIndex.set(state.queue[i].id, i);
    }
}

/**
 * Find the index of the oldest unwatched video.
 * Queue is newest→oldest, so we scan from the end.
 * @returns {number} Index or -1
 */
export function findOldestUnwatchedIndex() {
    for (let i = state.queue.length - 1; i >= 0; i--) {
        if (!state.watchHistory.watched_video_ids.has(state.queue[i].id)) return i;
    }
    return -1;
}

// ── Utility functions ──────────────────────────────────────────────────────

const _escapeEl = document.createElement('span');

/** Escape a string for safe insertion into innerHTML. */
export function escapeHTML(str) {
    _escapeEl.textContent = str;
    return _escapeEl.innerHTML;
}

/**
 * Sanitize HTML by stripping dangerous tags and event-handler attributes
 * while preserving safe formatting tags used by YouTube (b, i, a, br, etc.).
 */
const _ALLOWED_TAGS = new Set(['b', 'i', 'em', 'strong', 'a', 'br', 'p', 'ul', 'ol', 'li', 'span']);
const _sanitizeDiv = document.createElement('div');

export function sanitizeHTML(html) {
    _sanitizeDiv.innerHTML = html;
    // Remove dangerous elements
    for (const el of _sanitizeDiv.querySelectorAll('script, iframe, object, embed, form, style, link, meta, base')) {
        el.remove();
    }
    // Strip event-handler attributes and srcdoc from all remaining elements
    for (const el of _sanitizeDiv.querySelectorAll('*')) {
        for (const attr of [...el.attributes]) {
            if (attr.name.startsWith('on') || attr.name === 'srcdoc') {
                el.removeAttribute(attr.name);
            }
        }
        // Force links to open in new tab and prevent tabnapping
        if (el.tagName === 'A') {
            el.setAttribute('target', '_blank');
            el.setAttribute('rel', 'noopener noreferrer');
        }
    }
    return _sanitizeDiv.innerHTML;
}

const _dateOpts = { month: 'short', day: 'numeric', year: 'numeric' };

/** Format an ISO date string into a short locale string. */
export function formatDate(isoStr) {
    return new Date(isoStr).toLocaleDateString(undefined, _dateOpts);
}

// ── Notification helpers ───────────────────────────────────────────────────

export function showNotification(msg) {
    DOM.notification.textContent = msg;
    DOM.notification.classList.remove('hidden');
}

export function hideNotification() {
    DOM.notification.classList.add('hidden');
}
