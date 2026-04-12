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
    const first = DOM.queueList.querySelector('.video-item, .skeleton-item');
    if (first && first.offsetHeight > 0) {
        const gap = parseFloat(getComputedStyle(DOM.queueList).gap) || 12;
        itemH = first.offsetHeight + gap;
    }
    const calc = Math.max(1, Math.floor((h - 8) / itemH));

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
    DOM.queueList.innerHTML = `
        <div class="skeleton-item"></div>
        <div class="skeleton-item"></div>
        <div class="skeleton-item"></div>
    `;
    state.queue = [];
    state.queueIndex.clear();

    const url = force ? '/api/sync/stream?force=true' : '/api/sync/stream';
    const es = new EventSource(url);

    es.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);

            if (data.type === 'init') {
                data.api_key_configured ? hideNotification() : showNotification('YouTube API Key is not configured. Please edit config.yaml.');
                state.watchHistory = {
                    watched_video_ids: new Set(data.history?.watched_video_ids || []),
                    last_watched_video_id: data.history?.last_watched_video_id || null,
                };
            }
            else if (data.type === 'videos') {
                for (const v of data.videos) {
                    if (!state.queueIndex.has(v.id)) state.queue.push(v);
                }
                state.queue.sort((a, b) => new Date(b.publishedAt) - new Date(a.publishedAt));
                rebuildQueueIndex();
                renderQueue();
            }
            else if (data.type === 'error') {
                showNotification(data.message);
                es.close();
                DOM.syncBtn.textContent = 'Sync';
            }
            else if (data.type === 'done') {
                es.close();
                if (force) {
                    DOM.syncBtn.textContent = '✓';
                    setTimeout(() => { DOM.syncBtn.textContent = 'Sync'; }, 2000);
                } else {
                    DOM.syncBtn.textContent = 'Sync';
                }
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
            es.close();
            DOM.syncBtn.textContent = 'Sync';
        }
    };

    es.onerror = () => {
        es.close();
        showNotification('Error loading video queue. Check console.');
        DOM.syncBtn.textContent = 'Sync';
        if (state.queue.length === 0) DOM.queueList.innerHTML = '';
    };
}

// ── Private ────────────────────────────────────────────────────────────────

function _scrollToCurrent() {
    if (!state.currentPlayingId) return;
    const el = document.querySelector(`.video-item[data-id="${state.currentPlayingId}"]`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
}
