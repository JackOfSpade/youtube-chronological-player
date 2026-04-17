/**
 * app.js — Application entry point.
 *
 * Wires up event listeners and kicks off the initial data load.
 * This is the only file that needs to know about all the other modules.
 */

import { DOM, state } from './state.js';
import { resizeQueue, renderQueue, loadQueueData } from './queue.js';
import { resumePlayback } from './player.js';
import { loadComments } from './comments.js';
import {
    openSettingsModal, closeSettingsModal,
    handleAddChannel, handleSearchInput, saveChannels,
} from './settings.js';

function init() {
    // ── Responsive queue sizing ────────────────────────────────────────
    let resizeTimer;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(resizeQueue, 150);
    });
    setTimeout(resizeQueue, 100);

    // ── Infinite-scroll for comments ───────────────────────────────────
    DOM.commentsSection.addEventListener('scroll', () => {
        if (!state.currentPlayingId || !state.commentsToken || state.isFetchingComments) return;
        const dist = DOM.commentsSection.scrollHeight - DOM.commentsSection.scrollTop - DOM.commentsSection.clientHeight;
        if (dist < 300) loadComments(state.currentPlayingId, true);
    });

    // ── Initial data load ──────────────────────────────────────────────
    loadQueueData();

    // ── Queue controls ─────────────────────────────────────────────────
    DOM.syncBtn.addEventListener('click', () => {
        if (DOM.syncBtn.textContent !== 'Sync') return;
        DOM.syncBtn.textContent = '...';
        loadQueueData(true);
    });

    DOM.resumeBtn.addEventListener('click', resumePlayback);

    DOM.prevPageBtn.addEventListener('click', () => {
        if (state.currentPage > 1) { state.currentPage--; renderQueue(); }
    });

    DOM.nextPageBtn.addEventListener('click', () => {
        const total = Math.ceil(state.queue.length / state.pageSize);
        if (state.currentPage < total) { state.currentPage++; renderQueue(); }
    });

    // ── Settings modal ─────────────────────────────────────────────────
    DOM.settingsBtn.addEventListener('click', openSettingsModal);
    DOM.closeModalBtn.addEventListener('click', closeSettingsModal);
    DOM.addChannelBtn.addEventListener('click', handleAddChannel);
    DOM.saveChannelsBtn.addEventListener('click', saveChannels);

    // ── Search autocomplete ────────────────────────────────────────────
    DOM.channelInput.addEventListener('input', handleSearchInput);
    DOM.channelInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            handleAddChannel();
        }
    });
    document.addEventListener('click', (e) => {
        if (!e.target.closest('.add-channel-form')) {
            DOM.searchDropdown.classList.add('hidden');
            if (state.searchTimeout) {
                clearTimeout(state.searchTimeout);
                state.searchTimeout = null;
            }
        }
    });
}

document.addEventListener('DOMContentLoaded', init);
