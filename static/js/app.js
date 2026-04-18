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
        if (state.isSyncing) return;
        if (DOM.syncBtn.classList.contains('error')) {
            showErrorModal('Synchronization failed. Click Dismiss to try again, or click here for details.', state.lastSyncError);
            return;
        }
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

    // ── Error modal ───────────────────────────────────────────────────
    const closeError = () => {
        DOM.errorModal.classList.add('hidden');
        DOM.syncBtn.classList.remove('error');
        DOM.syncBtn.textContent = 'Sync';
    };
    DOM.closeErrorModalBtn.addEventListener('click', closeError);
    DOM.errorModalOkBtn.addEventListener('click', closeError);

    DOM.errorModalOverrideBtn.addEventListener('click', async () => {
        try {
            DOM.errorModalOverrideBtn.disabled = true;
            DOM.errorModalOverrideBtn.textContent = 'Resetting...';
            const resp = await fetch('/api/sync/reset', { method: 'POST' });
            const data = await resp.json();
            if (data.status === 'success') {
                closeError();
                loadQueueData(true);
            } else {
                alert(data.message || 'Reset failed.');
            }
        } catch (err) {
            console.error('Manual override failed:', err);
            alert('Connection error during reset.');
        } finally {
            DOM.errorModalOverrideBtn.disabled = false;
            DOM.errorModalOverrideBtn.textContent = 'Manual Override';
        }
    });
}

export function showErrorModal(msg, details = null) {
    DOM.errorModalMsg.textContent = msg;
    
    let isLocked = false;
    if (details) {
        const detailStr = typeof details === 'string' ? details : JSON.stringify(details);
        isLocked = detailStr.includes('LOCKED') || detailStr.includes('already running');
        DOM.errorDetailsPre.textContent = typeof details === 'string' ? details : JSON.stringify(details, null, 2);
        DOM.errorDetailsCont.classList.remove('hidden');
    } else {
        DOM.errorDetailsCont.classList.add('hidden');
    }

    // Only show override if it seems like a lock issue, or always for safety if you want
    if (isLocked) {
        DOM.errorModalOverrideBtn.classList.remove('hidden');
    } else {
        DOM.errorModalOverrideBtn.classList.add('hidden');
    }

    DOM.errorModal.classList.remove('hidden');
}

document.addEventListener('DOMContentLoaded', init);
