/**
 * comments.js — Comment loading and rendering.
 */

import { DOM, state, escapeHTML, formatDate, sanitizeHTML } from './state.js';

// ── Public API ─────────────────────────────────────────────────────────────

export async function loadComments(videoId, append = false) {
    if (!DOM.commentsSection) return;
    if (append && state.isFetchingComments) return;

    state.isFetchingComments = true;
    DOM.commentsSection.classList.remove('hidden');

    if (!append) {
        DOM.commentsContainer.innerHTML = '<div class="comments-status">Loading comments...</div>';
    } else {
        const loader = document.createElement('div');
        loader.id = 'comments-loading-more';
        loader.className = 'comments-status';
        loader.textContent = 'Loading more...';
        DOM.commentsContainer.appendChild(loader);
    }

    try {
        let url = `/api/comments/${encodeURIComponent(videoId)}`;
        if (append && state.commentsToken) {
            url += `?pageToken=${encodeURIComponent(state.commentsToken)}`;
        }

        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP error ${resp.status}`);
        const data = await resp.json();

        if (state.currentPlayingId !== videoId) return; // Prevent rendering if navigated away

        state.commentsToken = data.nextPageToken || null;

        const loader = document.getElementById('comments-loading-more');
        if (loader) loader.remove();

        _renderComments(data.comments, videoId, append);
    } catch (err) {
        if (state.currentPlayingId !== videoId) return;

        const loader = document.getElementById('comments-loading-more');
        if (loader) loader.remove();

        if (!append) {
            DOM.commentsContainer.innerHTML = '<div class="comments-status comments-error">Failed to load comments</div>';
        }
        console.error(err);
    } finally {
        if (state.currentPlayingId === videoId) {
            state.isFetchingComments = false;
        }
    }
}

/** Toggle visibility of a reply thread. Called from inline onclick. */
window.toggleReplies = function (btn) {
    const threadId = btn.dataset.thread;
    const div = document.getElementById(`replies-${threadId}`);
    if (!div) return;
    const isHidden = div.classList.toggle('hidden');
    btn.textContent = isHidden
        ? `▼ View ${btn.dataset.count} replies`
        : '▲ Hide replies';
};

// ── Private ────────────────────────────────────────────────────────────────

function _renderComments(comments, videoId, append) {
    if (!Array.isArray(comments) || comments.length === 0) {
        if (!append) DOM.commentsContainer.innerHTML = '<div class="comments-status">No comments found.</div>';
        return;
    }

    const fragment = document.createDocumentFragment();

    for (const c of comments) {
        const thread = document.createElement('div');
        thread.className = 'comment-thread';

        let repliesHTML = '';
        if (c.replies && c.replies.length > 0) {
            const sorted = [...(c.replies || [])].reverse();
            const items = sorted.map(r => `
                <div class="comment-reply">
                    <img src="${escapeHTML(r.avatar)}" class="comment-avatar comment-avatar--reply" loading="lazy">
                    <div class="comment-body">
                        <div class="comment-header">
                            <strong>${escapeHTML(r.author)}</strong>
                            <span class="comment-meta">${formatDate(r.publishedAt)}</span>
                        </div>
                        <div class="comment-text">${sanitizeHTML(r.text)}</div>
                        <div class="comment-actions">
                            <span class="comment-meta">👍 ${r.likeCount}</span>
                            <a href="https://www.youtube.com/watch?v=${encodeURIComponent(videoId)}&lc=${encodeURIComponent(r.id)}#comments" target="_blank" class="comment-reply-link">Reply</a>
                        </div>
                    </div>
                </div>
            `).join('');

            repliesHTML = `
                <button onclick="toggleReplies(this)" data-thread="${escapeHTML(c.id)}" data-count="${c.replies.length}" class="toggle-replies-btn">▼ View ${c.replies.length} replies</button>
                <div id="replies-${escapeHTML(c.id)}" class="replies hidden">${items}</div>
            `;
        }

        thread.innerHTML = `
            <div class="comment-top">
                <img src="${escapeHTML(c.avatar)}" class="comment-avatar" loading="lazy">
                <div class="comment-body">
                    <div class="comment-header">
                        <strong>${escapeHTML(c.author)}</strong>
                        <span class="comment-meta">${formatDate(c.publishedAt)}</span>
                    </div>
                    <div class="comment-text">${sanitizeHTML(c.text)}</div>
                    <div class="comment-actions">
                        <span class="comment-meta">👍 ${c.likeCount}</span>
                        <a href="https://www.youtube.com/watch?v=${encodeURIComponent(videoId)}&lc=${encodeURIComponent(c.id)}#comments" target="_blank" class="comment-reply-link">Reply</a>
                    </div>
                </div>
            </div>
            ${repliesHTML}
        `;
        fragment.appendChild(thread);
    }

    if (append) {
        DOM.commentsContainer.appendChild(fragment);
    } else {
        DOM.commentsContainer.innerHTML = '';
        DOM.commentsContainer.appendChild(fragment);
    }
}
