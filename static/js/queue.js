/**
 * queue.js — Queue rendering, pagination, and SSE data loading.
 */

import {
    DOM, state,
    rebuildQueueIndex, findOldestUnwatchedIndex,
    escapeHTML, formatDate,
    showNotification, hideNotification,
} from './state.js';
import { playVideo } from './player.js';

// ── Public API ─────────────────────────────────────────────────────────────

/** Dynamically compute pageSize based on queue panel height. */
export function resizeQueue() {
    if (!DOM.queueList) return;
    const h = DOM.queueList.clientHeight;
    if (h === 0) return;

    let itemH = 104;
    let gap = 12;
    const first = DOM.queueList.querySelector('.video-item, .skeleton-item');
    if (first && first.offsetHeight > 0) {
        gap = parseFloat(getComputedStyle(DOM.queueList).gap) || 12;
        itemH = Math.max(1, first.offsetHeight + gap);
    }
    
    // The container has 16px top and 16px bottom padding (32px total).
    // The available height for items is h - 32.
    // N items take: N * offsetHeight + (N - 1) * gap
    // N * offsetHeight + N * gap - gap <= h - 32
    // N * itemH <= h - 32 + gap
    const calc = Math.min(5, Math.max(1, Math.floor((h - 32 + gap) / itemH)));

    if (state.pageSize !== calc) {
        state.pageSize = calc;
        if (state.queue.length > 0) {
            const total = Math.ceil(state.queue.length / state.pageSize);
            if (state.currentPage > total) state.currentPage = Math.max(1, total);
            renderQueue();
        }
    }
}

/** Render the current page of videos into the queue panel. */
export function renderQueue() {
    if (state.isSyncing) {
        DOM.queueList.innerHTML = `
            <div class="skeleton-item"></div>
            <div class="skeleton-item"></div>
            <div class="skeleton-item"></div>
            <div class="skeleton-item"></div>
            <div class="skeleton-item"></div>
        `;
        return;
    }

    DOM.queueList.innerHTML = '';

    if (state.queue.length === 0) {
        DOM.queueList.innerHTML = '<div class="queue-empty">No videos found. Check config and sync.</div>';
        DOM.prevPageBtn.classList.add('hidden');
        DOM.nextPageBtn.classList.add('hidden');
        DOM.pageIndicator.textContent = 'Page 0';
        return;
    }

    const total = Math.ceil(state.queue.length / state.pageSize);
    if (state.currentPage > total) state.currentPage = total;
    if (state.currentPage < 1) state.currentPage = 1;

    const start = (state.currentPage - 1) * state.pageSize;
    const page = state.queue.slice(start, start + state.pageSize);
    const frag = document.createDocumentFragment();

    page.forEach((video, i) => {
        const gi = start + i;
        const watched = state.watchHistory.watched_video_ids.has(video.id);
        const playing = video.id === state.currentPlayingId;

        const el = document.createElement('div');
        el.className = 'video-item' + (watched ? ' watched' : '') + (playing ? ' playing' : '');
        el.dataset.id = video.id;
        el.dataset.index = gi;

        const t = escapeHTML(video.title);
        const c = escapeHTML(video.channelTitle);

        el.innerHTML = `
            <div class="video-thumbnail">
                <img src="${escapeHTML(video.thumbnail)}" alt="Thumbnail" loading="lazy">
                <div class="watched-overlay"><span class="icon">✓</span></div>
                <div class="playing-overlay"><span class="icon">▶</span></div>
            </div>
            <div class="video-details">
                <div class="video-title" title="${t}">${t}</div>
                <div class="video-channel">${c}</div>
                <div class="video-date">${formatDate(video.publishedAt)}</div>
                <div style="margin-top: 4px;">
                    <a href="https://www.youtube.com/watch?v=${encodeURIComponent(video.id)}" target="_blank" class="glass-btn" style="padding: 2px 6px; font-size: 0.70rem; text-decoration: none;" title="Watch on YouTube" onclick="event.stopPropagation()">↗ YouTube</a>
                </div>
            </div>
        `;

        el.addEventListener('click', () => playVideo(video.id, gi));
        frag.appendChild(el);
    });

    DOM.queueList.appendChild(frag);

    DOM.pageIndicator.textContent = `Page ${state.currentPage} of ${total}`;
    DOM.prevPageBtn.classList.toggle('hidden', state.currentPage <= 1);
    DOM.nextPageBtn.classList.toggle('hidden', state.currentPage >= total);

    _scrollToCurrent();
}

export function updateResumeButton() {
    DOM.resumeBtn.classList.toggle('hidden', findOldestUnwatchedIndex() === -1);
}

/** Open an SSE connection and incrementally fill the queue. */
export function loadQueueData(force = false) {
    if (state.syncRetryTimeout) {
        clearTimeout(state.syncRetryTimeout);
        state.syncRetryTimeout = null;
    }

    if (state.syncEventSource) {
        state.syncEventSource.close();
        state.syncEventSource = null;
    }

    const previousQueue = [...state.queue];
    const previousIndex = new Map(state.queueIndex);

    state.isSyncing = true;
    DOM.syncOverlay.classList.remove('hidden');
    DOM.syncProgressMsg.textContent = 'Initializing connection...';
    renderQueue();

    let retryCount = 0;
    const maxRetries = 3;
    let watchdogTimer = null;

    const connect = () => {
        if (state.syncEventSource) {
            state.syncEventSource.close();
        }

        const url = force ? `/api/sync/stream?force=true&t=${Date.now()}` : `/api/sync/stream?t=${Date.now()}`;
        const es = new EventSource(url);
        state.syncEventSource = es;
        
        DOM.syncBtn.classList.add('syncing');

        const resetWatchdog = () => {
            if (watchdogTimer) clearTimeout(watchdogTimer);
            watchdogTimer = setTimeout(() => {
                console.warn('SSE Watchdog: No activity for 90s. Reconnecting...');
                if (state.syncEventSource) {
                    state.syncEventSource.close();
                    es.onerror();
                }
            }, 90000);
        };

        const cleanup = () => {
            if (watchdogTimer) clearTimeout(watchdogTimer);
            DOM.syncOverlay.classList.add('hidden');
            DOM.syncBtn.classList.remove('syncing');
            state.syncEventSource = null;
        };

        resetWatchdog();

        es.onmessage = (event) => {
            resetWatchdog();
            
            try {
                if (event.data === ':heartbeat') return;
                
                if (event.data && event.data.length > 2000000) {
                     throw new Error("Payload exceeds MAX_CHUNK_SIZE.");
                }
                const data = JSON.parse(event.data);

                if (data.type === 'init') {
                    data.api_key_configured ? hideNotification() : showNotification('YouTube API Key is not configured. Please edit config.yaml.');
                    state.watchHistory = {
                        watched_video_ids: new Set(Array.isArray(data.history?.watched_video_ids) ? data.history.watched_video_ids : []),
                        last_watched_video_id: data.history?.last_watched_video_id || null,
                    };
                }
                else if (data.type === 'progress') {
                    DOM.syncProgressMsg.textContent = data.message;
                }
                else if (data.type === 'videos') {
                    state.queue = [];
                    state.queueIndex.clear();
                    const vidList = Array.isArray(data.videos) ? data.videos : [];
                    for (const v of vidList) {
                        if (v && v.id != null) {
                            v.id = String(v.id);
                            // Avoid duplicates just in case
                            if (!state.queueIndex.has(v.id)) {
                                state.queue.push(v);
                                state.queueIndex.set(v.id, state.queue.length - 1);
                            }
                        }
                    }
                    
                    renderQueue();
                }
                else if (data.type === 'error') {
                    if (data.error_code === 'LOCKED') {
                        DOM.syncProgressMsg.textContent = 'Sync already in progress in another window...';
                        showNotification('Monitoring existing sync progress...', 5000);
                        // Poll /api/sync/status to show the user what's happening
                        _monitorExistingSync();
                    } else {
                        cleanup();
                        if (data.message.includes('Quota Exceeded')) {
                            showNotification('⚠️ YouTube API Quota Exceeded. Try again in 24 hours or use a different API key.');
                        } else {
                            showNotification(data.message);
                        }
                        es.close();
                        state.isSyncing = false;
                        
                        DOM.syncBtn.textContent = 'Sync Error';
                        DOM.syncBtn.classList.add('error');
                        state.lastSyncError = data.status || data.message;
                        state.queue = previousQueue;
                        state.queueIndex = previousIndex;
                    }
                    renderQueue();
                }
                else if (data.type === 'done') {
                    cleanup();
                    es.close();
                    state.isSyncing = false;
                    DOM.syncBtn.textContent = force ? '✓' : 'Sync';
                    if (force) setTimeout(() => { DOM.syncBtn.textContent = 'Sync'; }, 2000);
                    
                    if (state.queue.length > 0) {
                        resizeQueue();
                        const oldest = findOldestUnwatchedIndex();
                        if (oldest !== -1) {
                            state.currentPage = Math.floor(oldest / state.pageSize) + 1;
                            renderQueue();
                        }
                    }
                    updateResumeButton();
                }
            } catch (err) {
                console.error('SSE parse error:', err);
                if (err.message && err.message.includes("MAX_CHUNK_SIZE")) {
                     cleanup();
                     es.close();
                     state.isSyncing = false;
                     DOM.syncBtn.textContent = 'Sync';
                }
            }
        };

        es.onerror = () => {
            es.close();
            state.syncEventSource = null;

            if (retryCount < maxRetries) {
                retryCount++;
                DOM.syncProgressMsg.textContent = `Connection lost. Retrying (${retryCount}/${maxRetries})...`;
                state.syncRetryTimeout = setTimeout(connect, 3000);
            } else {
                cleanup();
                state.isSyncing = false;
                showNotification('Error loading video queue. Check console.');
                DOM.syncBtn.textContent = 'Sync Error';
                DOM.syncBtn.classList.add('error');
                state.queue = previousQueue;
                state.queueIndex = previousIndex;
                renderQueue();
            }
        };
    };

    // Kick off the initial connection attempt
    connect();
}

// ── Private ────────────────────────────────────────────────────────────────


async function _monitorExistingSync() {
    DOM.syncOverlay.classList.remove('hidden');
    let pollCount = 0;
    const maxPolls = 100; // ~5 mins at 3s interval
    
    const poll = async () => {
        try {
            pollCount++;
            if (pollCount > maxPolls) {
                console.warn('Sync monitoring timed out.');
                DOM.syncProgressMsg.textContent = 'Monitoring timed out. Sync may be stalled.';
                state.isSyncing = false;
                DOM.syncBtn.classList.remove('syncing');
                DOM.syncBtn.classList.add('error');
                DOM.syncBtn.textContent = 'Sync Stalled';
                state.lastSyncError = { error: 'TIMEOUT', message: 'Sync monitoring timed out after 5 minutes.' };
                return;
            }

            const resp = await fetch('/api/sync/status');
            const status = await resp.json();
            
            if (!status.is_running) {
                DOM.syncProgressMsg.textContent = 'Sync finished in other window. Refreshing...';
                setTimeout(() => location.reload(), 1500);
                return;
            }
            
            DOM.syncProgressMsg.textContent = `[External] ${status.current_step}`;
            state.lastSyncError = status; // Keep track of last status for the error modal
            setTimeout(poll, 3000);
        } catch (err) {
            console.error('Polling for sync status failed:', err);
            DOM.syncOverlay.classList.add('hidden');
            state.isSyncing = false;
            DOM.syncBtn.classList.remove('syncing');
            DOM.syncBtn.classList.add('error');
            DOM.syncBtn.textContent = 'Sync Error';
            state.lastSyncError = { error: 'POLL_FAILED', message: err.message };
        }
    };
    
    poll();
}

function _scrollToCurrent() {
    if (!state.currentPlayingId) return;
    const items = document.querySelectorAll('.video-item');
    for (const el of items) {
        if (el.dataset.id === state.currentPlayingId) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            break;
        }
    }
}
